"""The tools a user-created agent gets, derived rather than written.

Four sources, one list:

1. **Lake's capabilities**, from Lake's OpenAPI (never a hand-written table: the eval generator's
   first run failed on a hand-written parameter name): the App and build capabilities every agent
   has, and those the agent's **skills** grant (skills.py), the workspace engine's included under
   their Lake names (lake#674). A read calls Lake; a change becomes an exact proposal for a person
   to confirm and never calls the mutation route from this runtime, except a skill's `direct`
   engine actions, which the engine's own policy governs (agent-runtime#11). Nothing on
   skills.py's never-for-agents list is ever a tool.
2. **Granted Pipelines** (lake#238): each approved Pipeline on the record becomes
   `pipeline_<name>`, which checks (`check_pipeline`), runs (`start_run`) and returns the run's
   report.
3. **MCP connections** (lake#652): each `mcp:<prefix>` skill grants that connection's allowed
   tools, `<prefix>__<tool>`, through lake (`agent_runtime/mcp.py`): reads run, auto-approved
   changes run, other changes are proposals.
4. **The definition's own functions**: `agent/tools.py` may export `TOOLS`, plain Python callables
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
from agent_runtime.skills import direct_grant, lake_grant, never_for_agents

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


# The capabilities whose `id` names a project or a dataset (Lake's names; the engine said `name`
# and `dataset`), so the work area can read it.
PROJECT_BY_NAME = frozenset({"get_project"})
DATASET_BY_ID = frozenset({"get_dataset"})


def dataset_name(value: str) -> str:
    """A dataset's own name, whether written `<database>/<name>`, `<database>.<name>` or bare."""
    return value.replace("/", ".").split(".")[-1]


def confined(agent: Agent, arguments: dict[str, Any], capability: str = "") -> str:
    """Why a call leaves the agent's work area, or "" when it stays inside."""
    area = agent.work_area or {}
    kind, detail = area.get("kind", "workspace"), area.get("detail", "")
    if kind == "project" and detail:
        named = arguments.get("project") or (
            arguments.get("name") if capability in PROJECT_BY_NAME or not capability else None)
        if named and named != detail:
            return f"this agent works only in project '{detail}', not '{named}'"
    if kind == "dataset" and detail:
        allowed = {name.strip() for name in detail.split(",") if name.strip()}
        named = arguments.get("dataset") or arguments.get("dataset_path") or (
            arguments.get("id") if capability in DATASET_BY_ID else None)
        if (isinstance(named, str) and named
                and dataset_name(named) not in {dataset_name(name) for name in allowed}):
            return f"this agent works only on {', '.join(sorted(allowed))}, not '{named}'"
    return ""


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
    """Lake's capabilities, derived from Lake's OpenAPI.

    Every agent gets the App and build reads and may propose the App and build changes (apps.py).
    The agent's skills add the Lake capabilities they grant (skills.py): a read is a tool that
    calls Lake, a skill's direct engine action is a tool that calls Lake (the engine's policy
    decides), anything else a proposal. Nothing on the never-for-agents list is ever a tool.
    """
    from agent_runtime.apps import APP_ACTION_CAPABILITIES, APP_CAPABILITIES

    granted = lake_grant(agent.skills) if agent is not None else set()
    direct = direct_grant(agent.skills) if agent is not None else set()
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
            change = (name in APP_ACTION_CAPABILITIES if app
                      else not is_read(operation) and name not in direct)
            if change:
                derived.append(proposal_tool(name, description, parameters))
                continue

            def call(arguments: dict[str, Any], name: str = name, method: str = method,
                     fixed: list[str] = fixed, app: bool = app) -> Reply:
                if agent is not None and not app:
                    refusal = confined(agent, arguments, name)
                    if refusal:
                        return Reply(False, error=refusal)
                return lake.capability(name, arguments, method, fixed if not app else None)

            derived.append(Tool(name, description, parameters, call))
    return derived


# Every named engine capability takes the run's customer and workspace (lake#674).
WORKSPACE_FIELDS = ["customer_id", "workspace_id"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")[:48] or "pipeline"


def pipeline_tools(agent: Agent, lake: Lake, sleep: Callable[[float], None] = time.sleep,
                   deadline: float = PIPELINE_DEADLINE_SECONDS) -> list[Tool]:
    """A granted, approved Pipeline as one tool: check_pipeline, start_run, and the run's report
    (lake#238), through Lake's named engine capabilities (lake#674)."""
    tools: list[Tool] = []
    for grant in agent.pipelines:
        project, graph = grant.get("project", ""), grant.get("graph", "")
        if not project or not graph:
            continue

        def call(arguments: dict[str, Any], project: str = project, graph: str = graph) -> Reply:
            gated = lake.capability("check_pipeline", {"project": project, "graph": graph},
                                    "post", WORKSPACE_FIELDS)
            if not gated.ok:
                return gated
            if isinstance(gated.value, dict) and gated.value.get("block_deploy"):
                failing = [check for check in gated.value.get("checks", [])
                           if not check.get("passed", True)]
                return Reply(False, error="the correctness gate blocks this Pipeline: "
                                          + json.dumps(failing or gated.value)[:2000])
            run_input = {"project": project, "graph": graph,
                         **{key: value for key, value in arguments.items()
                            if key in {"dataset", "holdout_fraction", "model_name"} and value}}
            started = lake.capability("start_run", run_input, "post", WORKSPACE_FIELDS)
            if not started.ok:
                return started
            run_id = (started.value or {}).get("run_id") or (started.value or {}).get("id")
            if not run_id:
                return started
            waited = 0.0
            while waited < deadline:
                state = lake.capability("get_run", {"run_id": run_id}, "get", WORKSPACE_FIELDS)
                status = (state.value or {}).get("status") if state.ok else None
                if status not in {"running", "queued", "pending"}:
                    break
                sleep(5)
                waited += 5
            else:
                return Reply(True, {"run_id": run_id, "status": "running",
                                    "note": "still running; poll get_run with this run_id"})
            report = lake.capability("get_run_report", {"run_id": run_id}, "get",
                                     WORKSPACE_FIELDS)
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
                "holdout_fraction": {"type": "number", "description": "the share of rows held "
                                                                      "out for evaluation"},
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


def all_tools(agent: Agent, lake: Lake, lake_openapi: dict[str, Any] | None = None) -> list[Tool]:
    from agent_runtime.mcp import mcp_tools

    tools = (lake_tools(lake, lake_openapi or {}, agent) + pipeline_tools(agent, lake)
             + mcp_tools(agent, lake) + definition_tools(agent))
    if agent.tool_allow_list is not None:
        allowed = set(agent.tool_allow_list)
        tools = [tool for tool in tools if tool.name in allowed]
    return tools
