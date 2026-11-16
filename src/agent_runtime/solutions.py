# SPDX-License-Identifier: MIT

"""The solution catalogue observed through an agent's granted tools for one turn.

Lake's `list_solutions` answers Orama's catalogue (the engine's, read-only) and the workspace's own,
with `catalogue: {available, message}` saying whether the engine could list Orama's; `get_solution`
takes the solution's `id` (lake `docs/api/solutions.md`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.lake import Reply
from agent_runtime.tools import Tool

CONTEXT_PREFIX = "## Current workspace solution registry"


@dataclass
class SolutionCatalog:
    """The current engine's catalogue and the reads used to obtain it."""

    solutions: list[dict[str, Any]] = field(default_factory=list)
    available: bool = False
    not_deployed: bool = False
    reason: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "not_deployed": self.not_deployed,
            "solutions": self.solutions,
            "reason": self.reason,
        }

    def message(self) -> dict[str, str]:
        if self.available and self.not_deployed:
            guidance = (
                "This was read from list_solutions and get_solution for this turn. Orama's "
                "solution catalogue is not deployed in this workspace's engine yet ("
                + (self.reason or "no reason given") + "); only the workspace's own solutions "
                "are listed. Do not invent catalogue entries. Never promise ROI figures, pricing, "
                "or odds of success."
            )
        elif self.available:
            guidance = (
                "This was read from list_solutions and get_solution for this turn. Treat it as "
                "authoritative. Report runnable and missing fields honestly. Never promise ROI "
                "figures, pricing, or odds of success. Solution actions remain subject to the "
                "engine's policy, holdout, approval, and audit checks."
            )
        elif self.not_deployed:
            guidance = (
                "No solution catalogue is available for this turn. Treat solution operations "
                "as not deployed yet; do not invent catalogue entries."
            )
        else:
            guidance = (
                "No solution catalogue is available through this agent's grants for this turn. "
                "Do not invent catalogue entries."
            )
        payload = json.dumps(self.as_dict(), default=str)[:50000]
        return {
            "role": "system",
            "content": f"{CONTEXT_PREFIX}\n\n{guidance}\n\n{payload}",
        }


def read_solution_catalog(tools: list[Tool]) -> SolutionCatalog:
    """Read list_solutions and every returned entry through this turn's granted tools."""
    by_name = {tool.name: tool for tool in tools}
    list_tool = by_name.get("list_solutions")
    get_tool = by_name.get("get_solution")
    if list_tool is None or get_tool is None:
        return SolutionCatalog(reason="the solution registry is not granted or not deployed")

    catalog = SolutionCatalog()
    listed = list_tool.call({})
    catalog.calls.append(call_record("list_solutions", {}, listed))
    if not listed.ok:
        if listed.code == "unknown_capability":
            catalog.not_deployed = True
            catalog.reason = "list_solutions is not deployed yet"
        else:
            catalog.reason = f"list_solutions failed: {listed.error}"
        return catalog

    value = listed.value if isinstance(listed.value, dict) else {}
    entries = value.get("solutions")
    if not isinstance(entries, list):
        catalog.reason = "list_solutions did not return a solutions list"
        return catalog
    catalogue = value.get("catalogue")
    if isinstance(catalogue, dict) and catalogue.get("available") is False:
        catalog.not_deployed = True
        catalog.reason = str(catalogue.get("message") or "Orama's catalogue is not deployed yet")

    details: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            continue
        seen.add(identifier)
        arguments = {"id": identifier}
        reply = get_tool.call(arguments)
        catalog.calls.append(call_record("get_solution", arguments, reply))
        if reply.ok and isinstance(reply.value, dict):
            detail = reply.value.get("solution")
            if isinstance(detail, dict):
                details.append(detail)
                continue
            if isinstance(reply.value.get("id"), str):
                details.append(reply.value)
                continue
        details.append(entry)
    catalog.available = True
    catalog.solutions = details
    return catalog


def call_record(name: str, arguments: dict[str, Any], reply: Reply) -> dict[str, Any]:
    """Trace one automatic catalogue read in the same shape as a model-requested call."""
    return {
        "tool": name,
        "arguments": arguments,
        "ok": reply.ok,
        "observation": reply.value if reply.ok else None,
        "error": reply.error or None,
    }


def replace_context(messages: list[dict[str, Any]], catalog: SolutionCatalog) -> None:
    """Keep only this turn's catalogue in a continuing conversation."""
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and str(message.get("content", "")).startswith(CONTEXT_PREFIX)
        )
    ]
    messages.append(catalog.message())
