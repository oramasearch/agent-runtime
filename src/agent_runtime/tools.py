# SPDX-License-Identifier: MIT

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


# The capability whose `name` selects a project.
PROJECT_BY_NAME = frozenset({"get_project"})


def dataset_confined(agent: Agent) -> bool:
    return (agent.work_area or {}).get("kind") == "dataset"


def dataset_id(value: object) -> tuple[str, str] | None:
    """Lake dataset IDs: slash/dot aliases, with `datasets` as the bare ID's database.

    Accept only unambiguous SQL names. Do not decode, case-fold, strip paths, or discard the
    database: the resulting ID must name exactly the resource Lake will read.
    """
    if not isinstance(value, str):
        return None
    parts = re.split(r"[/.]", value)
    if len(parts) == 1:
        parts.insert(0, "datasets")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts):
        return None
    return parts[0], parts[1]


def dataset_grant(agent: Agent) -> set[tuple[str, str]]:
    detail = (agent.work_area or {}).get("detail")
    if not isinstance(detail, str):
        return set()
    ids = [dataset_id(item.strip()) for item in detail.split(",")]
    # A malformed or empty entry invalidates the grant, including an empty work area.
    return {item for item in ids if item is not None} if all(ids) else set()


def dataset_row_id(row: object) -> tuple[str, str] | None:
    """Require the response's explicit database and table, checking any redundant ID too."""
    if not isinstance(row, dict):
        return None
    database, name = row.get("database"), row.get("name")
    if not isinstance(database, str) or not isinstance(name, str):
        return None
    identity = dataset_id(f"{database}/{name}")
    if "id" in row and dataset_id(row["id"]) != identity:
        return None
    return identity


def dataset_reply(agent: Agent, capability: str, arguments: dict[str, Any], reply: Reply) -> Reply:
    """Keep unscoped list metadata and unexpected dataset identities out of observations."""
    if not reply.ok:
        # A server error may itself quote an outside dataset or its schema.
        return Reply(False, error="Lake could not read the dataset work area", code=reply.code)
    allowed = dataset_grant(agent)
    if capability == "get_dataset":
        identity = dataset_row_id(reply.value)
        if identity in allowed and identity == dataset_id(arguments.get("id")):
            return reply
    elif (capability == "list_datasets" and isinstance(reply.value, dict)
          and isinstance(reply.value.get("datasets"), list)):
        return Reply(True, {"datasets": [row for row in reply.value["datasets"]
                                        if dataset_row_id(row) in allowed]})
    return Reply(False, error="Lake returned an unverifiable dataset work-area response")


def confined(agent: Agent, arguments: dict[str, Any], capability: str = "") -> str:
    """Why a call leaves the agent's work area, or "" when it stays inside."""
    area = agent.work_area or {}
    kind, detail = area.get("kind", "workspace"), area.get("detail", "")
    if kind == "project" and detail:
        named = arguments.get("project") or (
            arguments.get("name") if capability in PROJECT_BY_NAME or not capability else None)
        if named and named != detail:
            return f"this agent works only in project '{detail}', not '{named}'"
    if kind == "dataset":
        allowed = dataset_grant(agent)
        refusal = f"this agent works only on {detail or 'its named datasets'}"
        if not allowed:
            return refusal + "; the dataset work area is empty or invalid"
        if not isinstance(arguments, dict):
            return refusal + "; tool arguments must be an object"
        if capability == "get_dataset":
            if (set(arguments) - {"id", "revision", *FIXED_FIELDS}
                    or dataset_id(arguments.get("id")) not in allowed):
                return refusal + "; the dataset selector is outside the work area or invalid"
        elif capability == "list_datasets":
            if set(arguments) - set(FIXED_FIELDS):
                return refusal + "; unrecognized dataset-list selectors"
        else:
            # Indexes currently name only a bare source table, resolved across databases by the
            # engine. Neither an index ID nor a matching basename proves dataset membership.
            # All other capabilities need an explicit resource policy before they can run here.
            return refusal + f"; cannot verify '{capability}' belongs to the dataset work area"
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
                if agent is not None and (not app or dataset_confined(agent)):
                    refusal = confined(agent, arguments, name)
                    if refusal:
                        return Reply(False, error=refusal)
                if agent is not None and dataset_confined(agent):
                    arguments = dict(arguments)
                    if name == "get_dataset":
                        # Send the identity we authorized, including the default database.
                        identity = dataset_id(arguments["id"])
                        assert identity is not None  # confined checked the selector
                        arguments["id"] = "/".join(identity)
                    reply = lake.capability(name, arguments, method, fixed)
                    return dataset_reply(agent, name, arguments, reply)
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
    # A pipeline's stored graph may read datasets beyond a model-supplied override.
    if dataset_confined(agent):
        return []
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
    if (dataset_confined(agent) or agent.directory is None
            or not (agent.directory / "agent" / "tools.py").exists()):
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
