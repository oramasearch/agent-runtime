"""The tools a user-created agent gets, derived rather than written.

Five sources, one list:

1. **The workspace's Zetta capabilities**, from lake's filtered OpenAPI (never a hand-written
   table: the eval generator's first run failed on a hand-written parameter name). Only the
   capabilities the agent's **skills** grant survive; a skill not granted is a tool the agent
   never sees.
2. **Lake's own capabilities**, from Lake's OpenAPI: the App and build capabilities every agent
   has, and those the agent's skills grant (skills.py: drives, transcripts, repositories,
   datasets, streams, pipelines, usage, Lake-wide search, ...). A read calls Lake; a change becomes
   an exact proposal for a person to confirm and never calls the mutation route from this runtime.
   Nothing on skills.py's never-for-agents list is ever a tool.
3. **Granted Pipelines** (lake#238): each approved Pipeline on the record becomes
   `pipeline_<name>`, which gates, runs and returns the run's report.
4. **MCP connections** (lake#652): each `mcp:<prefix>` skill grants that connection's allowed
   tools, `<prefix>__<tool>`, through lake (`agent_runtime/mcp.py`): reads run, auto-approved
   changes run, other changes are proposals.
5. **The definition's own functions**: `agent/tools.py` may export `TOOLS`, plain Python callables
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
from agent_runtime.skills import (  # noqa: F401 (re-exported for existing callers)
    SKILLS,
    SOLUTION_ACTION_CAPABILITIES,
    SOLUTION_READ_CAPABILITIES,
    engine_grant,
    lake_grant,
    never_for_agents,
)

# The workspace engine capabilities each skill grants (engine names), kept under this name for the
# code that reads it. The skills themselves, and the Lake capabilities they grant, are skills.py.
SKILL_CAPABILITIES: dict[str, set[str]] = {
    name: set(skill.engine) for name, skill in SKILLS.items() if skill.engine
}

PIPELINE_DEADLINE_SECONDS = 600


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    call: Callable[[dict[str, Any]], Reply]
    proposal: bool = False
    proposal_impact: str = ""
    # What a proposal asks lake to run, when it is not the tool itself (an MCP change proposes
    # `call_mcp_tool` with the connection, the tool and its arguments).
    proposal_capability: str = ""
    proposal_input: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def proposed(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The proposal a turn records for a person to confirm in lake."""
        return {"capability": self.proposal_capability or self.name,
                "input": self.proposal_input(arguments) if self.proposal_input else arguments,
                "impact": self.proposal_impact}

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name,
                                                 "description": self.description[:1024],
                                                 "parameters": self.parameters}}


def granted_capabilities(skills: list[str]) -> set[str]:
    """The workspace engine capabilities the skills grant (skills.py)."""
    return engine_grant(skills)


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


# The fields the run fixes itself: its customer, and the workspace it was launched in. The model
# never sees them, so it cannot point a call at another account or workspace.
FIXED_FIELDS = ("customer_id", "workspace_id")


def customer_scoped(parameters: dict[str, Any]) -> dict[str, Any]:
    """Remove customer_id and workspace_id: the launch credential already fixes both."""
    scoped = dict(parameters)
    scoped["properties"] = {
        name: schema
        for name, schema in (parameters.get("properties") or {}).items()
        if name not in FIXED_FIELDS
    }
    required = [
        name
        for name in parameters.get("required") or []
        if name not in FIXED_FIELDS
    ]
    if required:
        scoped["required"] = required
    else:
        scoped.pop("required", None)
    return scoped


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
        for method, operation in one_operation(operations):
            parameters = operation_parameters(method, operation)

            def call(arguments: dict[str, Any], name: str = name, method: str = method) -> Reply:
                refusal = confined(agent, arguments)
                if refusal:
                    return Reply(False, error=refusal)
                return lake.zetta(name, arguments, method)

            tools.append(Tool(name, operation.get("description") or operation.get("summary")
                              or name, parameters, call))
    return tools


def one_operation(operations: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """A capability's one operation: a read Lake also takes as POST (for large inputs) is GET."""
    if "get" in operations:
        return [("get", operations["get"])]
    return list(operations.items())[:1]


def is_read(operation: dict[str, Any]) -> bool:
    """Whether a Lake operation only reads: its `x-lake-action`, Lake's own statement of what the
    call does whatever the HTTP method (lake rest.cpp)."""
    return operation.get("x-lake-action") == "read"


def proposal_tool(name: str, description: str, parameters: dict[str, Any]) -> Tool:
    """A change the agent may propose: the exact capability and input, never sent to lake."""

    def propose(arguments: dict[str, Any], name: str = name) -> Reply:
        return Reply(
            True,
            {
                "status": "proposed",
                "capability": name,
                "input": arguments,
                "message": "PROPOSED, NOT DONE. A person must confirm this in lake.",
            },
        )

    return Tool(
        name,
        f"Propose this lake change for a person to confirm. {description}",
        parameters,
        propose,
        proposal=True,
        proposal_impact=description,
    )


def lake_tools(lake: Lake, openapi: dict[str, Any], agent: Agent | None = None) -> list[Tool]:
    """Lake's own capabilities, derived from Lake's OpenAPI.

    Every agent gets the App and build reads and may propose the App and build changes (apps.py).
    The agent's skills add the Lake capabilities they grant (skills.py): a read is a tool that
    calls Lake, anything else a proposal. Nothing on the never-for-agents list is ever a tool.
    """
    from agent_runtime.apps import APP_ACTION_CAPABILITIES, APP_CAPABILITIES

    granted = lake_grant(agent.skills) if agent is not None else set()
    never = never_for_agents()
    derived: list[Tool] = []
    for path, operations in (openapi.get("paths") or {}).items():
        name = path.removeprefix("/v1/")
        app = name in APP_CAPABILITIES
        if (not app and name not in granted) or name in never:
            continue
        for method, operation in one_operation(operations):
            declared = operation_parameters(method, operation)
            fixed = [field for field in FIXED_FIELDS
                     if field in (declared.get("properties") or {})]
            parameters = customer_scoped(declared)
            description = operation.get("description") or operation.get("summary") or name
            change = name in APP_ACTION_CAPABILITIES if app else not is_read(operation)
            if change:
                derived.append(proposal_tool(name, description, parameters))
                continue

            def call(arguments: dict[str, Any], name: str = name, method: str = method,
                     fixed: list[str] = fixed, app: bool = app) -> Reply:
                if agent is not None and not app:
                    refusal = confined(agent, arguments)
                    if refusal:
                        return Reply(False, error=refusal)
                return lake.capability(name, arguments, method, fixed if not app else None)

            derived.append(Tool(name, description, parameters, call))
    return derived


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


def all_tools(agent: Agent, lake: Lake, openapi: dict[str, Any],
              lake_openapi: dict[str, Any] | None = None) -> list[Tool]:
    from agent_runtime.mcp import mcp_tools

    engine = workspace_tools(agent, lake, openapi)
    # A Lake capability with the same name as a granted engine one is the same job: the engine
    # tool stays, so the five original skills behave as they did.
    taken = {tool.name for tool in engine}
    own = [tool for tool in lake_tools(lake, lake_openapi or {}, agent) if tool.name not in taken]
    tools = engine + own + pipeline_tools(agent, lake) + mcp_tools(agent, lake) + definition_tools(agent)
    if agent.tool_allow_list is not None:
        allowed = set(agent.tool_allow_list)
        tools = [tool for tool in tools if tool.name in allowed]
    return tools
