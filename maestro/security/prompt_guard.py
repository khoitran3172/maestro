"""Prompt injection filter and context wrapping security tools."""

from __future__ import annotations

import re

# Common prompt injection pattern list
_INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+)?(?:previous\s+)?instructions",
    r"ignore\s+(?:the\s+)?above",
    r"system\s+override",
    r"leak\s+(?:the\s+)?(?:api\s+key|credentials|secret|token)",
    r"print\s+(?:the\s+)?system\s+prompt",
    r"you\s+are\s+now\s+a\s+bypass",
    r"ignore\s+directions",
]


def is_safe_prompt(prompt: str) -> bool:
    """Return True if prompt does not contain known injection attacks, False otherwise."""
    text = prompt.lower()
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, text):
            return False
    return True


def wrap_file_content(file_path: str, content: str) -> str:
    """Safely wrap file content using standard semantic delimiters.

    Helps model separate instruction from text content.
    """
    return f"\n=== START FILE CONTENT: {file_path} ===\n{content}\n=== END FILE CONTENT: {file_path} ===\n"
