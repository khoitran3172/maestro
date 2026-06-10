"""LLM-driven task graph planner."""

from __future__ import annotations

import json
from typing import Any

from maestro.dag.task_graph import TaskGraph, TaskNode
from maestro.grader.llm_client import call_anthropic_api


class Planner:
    """Generates and validates task execution DAGs from text prompts."""

    async def generate_plan(self, prompt: str) -> TaskGraph:
        """Query the LLM to generate a structured TaskGraph JSON representing execution steps."""
        system_prompt = """You are a software architect planner for Maestro. Break down a user project description into a DAG of execution steps.
Each step should run on a specific specialist agent:
- 'claude_code': stateful coding, specifications, complex refactorings.
- 'codex': stateless code generation, writing clean features.
- 'stitch': visual UI/UX layout mockup generator.
- 'grok_build': branch-aware coding tool.
- 'antigravity': deployments and integrations.

Return ONLY a single valid JSON block containing nodes and edges. Do not wrap in markdown fences (like ```json), and do not provide any text explanations outside the JSON.

JSON Schema:
{
  "nodes": [
    {
      "task_id": "string (unique task identifier)",
      "specialist": "claude_code | codex | stitch | grok_build | antigravity",
      "command_template": ["command", "tokens", "here"],
      "phase": integer (relative phase number),
      "input_artifacts": ["list", "of", "input", "paths"],
      "output_artifact": "output/path/string",
      "rubric": {
        "builds": true/false (optional),
        "tests_pass": true/false (optional),
        "coverage": float (optional)
      },
      "timeout_sec": integer,
      "max_retries": integer
    }
  ],
  "edges": [
    {
      "from": "from_node_id",
      "to": "to_node_id"
    }
  ]
}
"""

        response = await call_anthropic_api(
            prompt=f"{system_prompt}\n\nProject Prompt: {prompt}",
            model="claude-3-5-sonnet-20241022",
        )

        return self.parse_and_validate(response)

    def parse_and_validate(self, response_text: str) -> TaskGraph:
        """Parse raw response text, clean markdown fences, and validate cycle integrity."""
        cleaned = response_text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
            graph = TaskGraph.from_dict(data)
            # Cycle detection validation
            graph.validate()
            return graph
        except Exception as e:
            raise ValueError(f"Failed to parse or validate generated planner TaskGraph: {e}. Raw response: {response_text[:200]}")
