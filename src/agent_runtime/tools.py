"""The tools a user-created agent gets, derived rather than written.

Three sources, one list:

1. **The workspace's Zetta capabilities**, from lake's filtered OpenAPI (never a hand-written
   table: the eval generator's first run failed on a hand-written parameter name). Only the
   capabilities the agent's **skills** grant survive; a skill not granted is a tool the agent
   never sees.
2. **Granted Pipelines** (lake#238): each approved Pipeline on the record becomes
   `pipeline_<name>`, which gates, runs and returns the run's report.
3. **The definition's own functions**: `agent/tools.py` may export `TOOLS`, plain Python callables
   whose docstrings describe them.

The **work area** narrows every call: an agent confined to one project cannot name another, and
one confined to named datasets cannot read the rest. A refused call is an observation, like any
failure.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from agent_runtime.definition import Agent
from agent_runtime.lake import Lake, Reply

# Solution reads are useful to agents that inspect, train, summarize, or call a deployed API.
# Mutations belong only to call-api, and lake's filtered OpenAPI must also grant each one.
SOLUTION_READ_CAPABILITIES = frozenset({
    "list_solutions",
    "get_solution",
    "get_policy",
    "list_ledger",
    "get_experiment",
    "get_monitor",
    "get_roi_readout",
})
SOLUTION_ACTION_CAPABILITIES = frozenset({
    "decide",
    "record_action",
    "record_outcome",
    "approve_decision",
})

# Studio's skills (orama-design studioAgents.js) and the workspace capabilities each grants.
SKILL_CAPABILITIES: dict[str, set[str]] = {
    "search": {"list_datasets", "get_dataset", "list_connections", "list_source_tables",
               "list_indexes", "get_index", "search"} | SOLUTION_READ_CAPABILITIES,
    "generate-data": {"list_datasets", "list_dataset_generators", "generate_dataset"},
    "train": {"list_datasets", "get_dataset", "list_projects", "get_project", "get_graph",
              "list_runs", "get_run", "get_run_report", "list_models", "get_model", "gate",
              "run"} | SOLUTION_READ_CAPABILITIES,
    "summarize": {"list_projects", "get_project", "list_runs", "get_run", "get_run_report",
                  "list_models", "get_model", "model_health"} | SOLUTION_READ_CAPABILITIES,
    "call-api": {"list_models", "get_model", "score", "explain", "list_indexes", "search"}
                | SOLUTION_READ_CAPABILITIES | SOLUTION_ACTION_CAPABILITIES,
}

PIPELINE_DEADLINE_SECONDS = 600


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    call: Callable[[dict[str, Any]], Reply]

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name,
                                                 "description": self.description[:1024],
                                                 "parameters": self.parameters}}


def granted_capabilities(skills: list[str]) -> set[str]:
    granted: set[str] = set()
    for skill in skills:
        granted |= SKILL_CAPABILITIES.get(skill, set())
    return granted


def operation_parameters(method: str, operation: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema of one OpenAPI operation's input."""
    if method == "post":
        schema = (((operation.get("requestBody") or {}).get("content") or {})
                  .get("application/json", {}).get("schema"))
        if isinstance(schema, dict) and schema.get("type") == "object":
            return schema
        return {"type": "object", "properties": {}}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in operation.get("parameters") or []:
        if parameter.get("in") != "query":
            continue
        properties[parameter["name"]] = parameter.get("schema") or {"type": "string"}
        if parameter.get("required"):
            required.append(parameter["name"])
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def confined(agent: Agent, arguments: dict[str, Any]) -> str:
    """Why a call leaves the agent's work area, or "" when it stays inside."""
    area = agent.work_area or {}
    kind, detail = area.get("kind", "workspace"), area.get("detail", "")
    if kind == "project" and detail:
        named = arguments.get("project") or arguments.get("name")
        if named and named != detail:
            return f"this agent works only in project '{detail}', not '{named}'"
    if kind == "dataset" and detail:
        allowed = {name.strip() for name in detail.split(",") if name.strip()}
        named = arguments.get("dataset") or arguments.get("dataset_path")
        if named and named.split(".")[-1] not in {name.split(".")[-1] for name in allowed}:
            return f"this agent works only on {', '.join(sorted(allowed))}, not '{named}'"
    return ""


def workspace_tools(agent: Agent, lake: Lake, openapi: dict[str, Any]) -> list[Tool]:
    granted = granted_capabilities(agent.skills)
    tools: list[Tool] = []
    for path, operations in (openapi.get("paths") or {}).items():
        name = path.removeprefix("/v1/")
        if name not in granted:
            continue
        for method, operation in operations.items():
            parameters = operation_parameters(method, operation)

            def call(arguments: dict[str, Any], name: str = name, method: str = method) -> Reply:
                refusal = confined(agent, arguments)
                if refusal:
                    return Reply(False, error=refusal)
                return lake.zetta(name, arguments, method)

            tools.append(Tool(name, operation.get("description") or operation.get("summary")
                              or name, parameters, call))
    return tools


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")[:48] or "pipeline"


def pipeline_tools(agent: Agent, lake: Lake, sleep: Callable[[float], None] = time.sleep,
                   deadline: float = PIPELINE_DEADLINE_SECONDS) -> list[Tool]:
    """A granted, approved Pipeline as one tool: gate, run, and the run's report (lake#238)."""
    tools: list[Tool] = []
    for grant in agent.pipelines:
        project, graph = grant.get("project", ""), grant.get("graph", "")
        if not project or not graph:
            continue

        def call(arguments: dict[str, Any], project: str = project, graph: str = graph) -> Reply:
            gated = lake.zetta("gate", {"project": project, "graph": graph}, "post")
            if not gated.ok:
                return gated
            if isinstance(gated.value, dict) and gated.value.get("block_deploy"):
                failing = [check for check in gated.value.get("checks", [])
                           if not check.get("passed", True)]
                return Reply(False, error="the correctness gate blocks this Pipeline: "
                                          + json.dumps(failing or gated.value)[:2000])
            run_input = {"project": project, "graph": graph,
                         **{key: value for key, value in arguments.items()
                            if key in {"dataset", "holdout", "model_name"} and value}}
            started = lake.zetta("run", run_input, "post")
            if not started.ok:
                return started
            run_id = (started.value or {}).get("run_id") or (started.value or {}).get("id")
            if not run_id:
                return started
            waited = 0.0
            while waited < deadline:
                state = lake.zetta("get_run", {"run_id": run_id}, "get")
                status = (state.value or {}).get("status") if state.ok else None
                if status not in {"running", "queued", "pending"}:
                    break
                sleep(5)
                waited += 5
            else:
                return Reply(True, {"run_id": run_id, "status": "running",
                                    "note": "still running; poll get_run with this run_id"})
            report = lake.zetta("get_run_report", {"run_id": run_id}, "get")
            return Reply(report.ok, {"run_id": run_id, "report": report.value}, report.error)

        tools.append(Tool(
            f"pipeline_{slug(graph)}",
            f"Run the approved Pipeline '{graph}' in project '{project}' (approved "
            f"{grant.get('version', '')}): checks it against the correctness gate, runs it, and "
            "returns the run's report (what it learned from, what it leans on, the most at risk, "
            "caveats).",
            {"type": "object", "properties": {
                "dataset": {"type": "string", "description": "the dataset to run on, if not the "
                                                             "Pipeline's own"},
                "holdout": {"type": "number"},
                "model_name": {"type": "string"}}},
            call))
    return tools


def definition_tools(agent: Agent) -> list[Tool]:
    """`agent/tools.py`'s TOOLS: plain callables, described by their docstrings."""
    if agent.directory is None or not (agent.directory / "agent" / "tools.py").exists():
        return []
    from agent_runtime.loop import import_from
    module = import_from(agent.directory, "agent.tools")
    tools: list[Tool] = []
    for function in getattr(module, "TOOLS", []):
        signature = inspect.signature(function)
        properties = {name: {"type": "string"} for name in signature.parameters}

        def call(arguments: dict[str, Any], function: Callable[..., Any] = function) -> Reply:
            try:
                return Reply(True, function(**arguments))
            except Exception as error:  # a failing tool is an observation
                return Reply(False, error=f"{type(error).__name__}: {error}")

        tools.append(Tool(function.__name__, inspect.getdoc(function) or function.__name__,
                          {"type": "object", "properties": properties}, call))
    return tools


def all_tools(agent: Agent, lake: Lake, openapi: dict[str, Any]) -> list[Tool]:
    tools = workspace_tools(agent, lake, openapi) + pipeline_tools(agent, lake) \
        + definition_tools(agent)
    if agent.tool_allow_list is not None:
        allowed = set(agent.tool_allow_list)
        tools = [tool for tool in tools if tool.name in allowed]
    return tools
