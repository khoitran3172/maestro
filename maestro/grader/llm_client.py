"""Helper client to call the Anthropic API without external dependencies."""

from __future__ import annotations

import base64
import json
import os
import urllib.request
import urllib.error
import asyncio
from pathlib import Path
from typing import Optional


async def call_anthropic_api(
    prompt: str,
    image_paths: Optional[list[Path]] = None,
    model: str = "claude-3-5-sonnet-20241022",
    max_tokens: int = 1500,
) -> str:
    """Send requests directly to Anthropic Messages API using standard urllib.

    Falls back to mock responses if ANTHROPIC_API_KEY is not defined.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    
    if not api_key:
        # Mock grading server when API Key is absent (e.g. unit tests)
        prompt_lower = prompt.lower()
        if "fail" in prompt_lower or "incorrect" in prompt_lower or "broken" in prompt_lower:
            return json.dumps({
                "score": 40.0,
                "passed": False,
                "failures": [
                    {"item": "quality_check", "message": "Failed quality rubric checks in mock LLM."}
                ],
                "feedback": "Mock quality checks failed."
            })
        return json.dumps({
            "score": 100.0,
            "passed": True,
            "failures": [],
            "feedback": "Mock quality checks passed."
        })

    # Prepare Anthropic Messages contents block
    content_blocks = []

    if image_paths:
        for p in image_paths:
            if p.exists():
                suffix = p.suffix.lower().lstrip(".")
                media_type = f"image/{suffix}"
                if suffix in ("jpg", "jpeg"):
                    media_type = "image/jpeg"
                elif suffix == "png":
                    media_type = "image/png"
                elif suffix == "gif":
                    media_type = "image/gif"
                elif suffix == "webp":
                    media_type = "image/webp"
                else:
                    continue  # ignore unsupported image format

                try:
                    img_data = p.read_bytes()
                    encoded = base64.b64encode(img_data).decode("utf-8")
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded,
                        }
                    })
                except Exception:
                    pass

    # Append core text prompt
    content_blocks.append({
        "type": "text",
        "text": prompt,
    })

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": content_blocks}
        ]
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    def _execute_request():
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            raise RuntimeError(f"Anthropic API response error {e.code}: {err_body}")
        except Exception as e:
            raise RuntimeError(f"Network request failed: {e}")

    try:
        response_str = await asyncio.to_thread(_execute_request)
        data = json.loads(response_str)
        blocks = data.get("content", [])
        for block in blocks:
            if block.get("type") == "text":
                return block["text"]
        raise ValueError("Response did not contain a text block.")
    except Exception as e:
        # Return fallback json to prevent pipeline crash
        return json.dumps({
            "score": 0.0,
            "passed": False,
            "failures": [{"item": "api_error", "message": f"Anthropic API call failed: {e}"}],
            "feedback": f"API request error: {e}"
        })
