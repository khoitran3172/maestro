"""Directed Acyclic Graph (DAG) representation of pipeline tasks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


class CycleError(ValueError):
    """Raised when a circular dependency is detected in the graph."""
    pass


@dataclass
class TaskNode:
    """A single task node in the execution DAG."""
    task_id: str
    specialist: str
    command_template: list[str]
    phase: int = 0
    input_artifacts: list[str] = field(default_factory=list)
    output_artifact: Optional[str] = None
    rubric: dict[str, Any] = field(default_factory=dict)
    timeout_sec: int = 600
    max_retries: int = 2
    prompt: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "specialist": self.specialist,
            "command_template": self.command_template,
            "phase": self.phase,
            "input_artifacts": self.input_artifacts,
            "output_artifact": self.output_artifact,
            "rubric": self.rubric,
            "timeout_sec": self.timeout_sec,
            "max_retries": self.max_retries,
            "prompt": self.prompt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TaskNode:
        return cls(
            task_id=data["task_id"],
            specialist=data["specialist"],
            command_template=data["command_template"],
            phase=data.get("phase", 0),
            input_artifacts=data.get("input_artifacts", []),
            output_artifact=data.get("output_artifact"),
            rubric=data.get("rubric", {}),
            timeout_sec=data.get("timeout_sec", 600),
            max_retries=data.get("max_retries", 2),
            prompt=data.get("prompt"),
        )


class TaskGraph:
    """Manages nodes and dependencies, validating cycles and topological order."""

    def __init__(self):
        self.nodes: dict[str, TaskNode] = {}
        # adjacency list: node_id -> set of node_ids that depend on it (outgoing edges)
        self.adj: dict[str, set[str]] = {}
        # in_degrees: node_id -> number of incoming edges
        self.in_degree: dict[str, int] = {}

    def add_node(self, node: TaskNode) -> None:
        """Add a task node to the graph."""
        if node.task_id in self.nodes:
            return
        self.nodes[node.task_id] = node
        self.adj[node.task_id] = set()
        self.in_degree[node.task_id] = 0

    def add_edge(self, from_node: str, to_node: str) -> None:
        """Add a directed edge (dependency) from -> to.

        Means 'from_node' must execute before 'to_node' can start.
        """
        if from_node not in self.nodes or to_node not in self.nodes:
            raise ValueError(f"Both nodes must exist in the graph. from: {from_node}, to: {to_node}")
        
        if to_node not in self.adj[from_node]:
            self.adj[from_node].add(to_node)
            self.in_degree[to_node] += 1

    def validate(self) -> None:
        """Validate the graph. Checks for circular dependencies (cycles)."""
        # Run Kahn's algorithm or DFS cycle detection
        visited_count = 0
        in_deg = self.in_degree.copy()
        queue = [n for n, deg in in_deg.items() if deg == 0]

        while queue:
            node = queue.pop(0)
            visited_count += 1
            for neighbor in self.adj[node]:
                in_deg[neighbor] -= 1
                if in_deg[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(self.nodes):
            raise CycleError("Circular dependency detected. Graph is not a DAG.")

    def get_dependencies(self, task_id: str) -> list[str]:
        """Get list of node IDs that the given task depends on (incoming nodes)."""
        if task_id not in self.nodes:
            raise KeyError(task_id)
        
        deps = []
        for node_id, neighbors in self.adj.items():
            if task_id in neighbors:
                deps.append(node_id)
        return deps

    def get_dependents(self, task_id: str) -> list[str]:
        """Get list of node IDs that depend on the given task (outgoing nodes)."""
        if task_id not in self.adj:
            raise KeyError(task_id)
        return list(self.adj[task_id])

    def topological_sort(self) -> list[str]:
        """Return task_ids sorted in topological execution order."""
        self.validate()
        in_deg = self.in_degree.copy()
        queue = sorted([n for n, deg in in_deg.items() if deg == 0]) # sorted for deterministic order
        order = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            for neighbor in sorted(self.adj[node]):
                in_deg[neighbor] -= 1
                if in_deg[neighbor] == 0:
                    queue.append(neighbor)

        return order

    def to_dict(self) -> dict:
        """Serialize graph to dictionary."""
        edges = []
        for from_node, neighbors in self.adj.items():
            for to_node in neighbors:
                edges.append({"from": from_node, "to": to_node})
        
        return {
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": edges,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TaskGraph:
        """Deserialize graph from dictionary."""
        graph = cls()
        for node_data in data.get("nodes", []):
            graph.add_node(TaskNode.from_dict(node_data))
        for edge_data in data.get("edges", []):
            graph.add_edge(edge_data["from"], edge_data["to"])
        graph.validate()
        return graph

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> TaskGraph:
        return cls.from_dict(json.loads(json_str))
