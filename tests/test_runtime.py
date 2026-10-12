"""agent-runtime without a network: lake is an httpx MockTransport answering the way Lake does
(its named capabilities, the workspace engine's included, lake#674), the model a scripted client.
The live run is the deploy's proof (lake#228)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent_runtime import apps, definition, runner, solutions, tools
from agent_runtime.lake import Lake, LoadError, Reply
from agent_runtime.loop import run_loop, system_message

CUSTOMER = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
WORKSPACE_FIELDS = [
    {"name": "customer_id", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "workspace_id", "in": "query", "required": True, "schema": {"type": "string"}},
]


def lake_read(name: str, *fields: str, required: tuple[str, ...] = ()) -> dict:
    """One Lake read as Lake's OpenAPI describes it."""
    return {"get": {"description": name, "x-lake-action": "read", "parameters": [
        *WORKSPACE_FIELDS,
        *({"name": field, "in": "query", "required": field in required,
           "schema": {"type": "string"}} for field in fields)]}}


def lake_action(name: str, *fields: str) -> dict:
    """One Lake action (a change) as Lake's OpenAPI describes it."""
    return {"post": {"description": name, "x-lake-action": "write", "requestBody": {"content": {
        "application/json": {"schema": {"type": "object", "properties": {
            "customer_id": {"type": "string"}, "workspace_id": {"type": "string"},
            **{field: {"type": "string"} for field in fields}},
            "required": ["customer_id", "workspace_id"]}}}}}}


OPENAPI = {"openapi": "3.1.0", "paths": {
    "/v1/list_datasets": lake_read("list datasets"),
    "/v1/get_dataset": lake_read("one dataset", "id", required=("id",)),
    "/v1/check_pipeline": lake_action("check_pipeline", "project", "graph"),
    "/v1/create_project": lake_action("create", "name"),
}}
SOLUTION_READS = {
    "list_solutions",
    "get_solution",
    "get_policy",
    "list_ledger",
    "get_holdout_experiment",
    "get_monitor",
    "get_roi_readout",
}
SOLUTION_DIRECT = {"decide", "record_outcome"}
SOLUTION_ACTIONS = SOLUTION_DIRECT | {"approve_decision"}
SOLUTION_OPENAPI = {"openapi": "3.1.0", "paths": {
    **{f"/v1/{name}": lake_read(name, "id") for name in SOLUTION_READS},
    **{f"/v1/{name}": lake_action(name, "entity", "action_id") for name in SOLUTION_ACTIONS},
}}
APP_OPENAPI = {"openapi": "3.1.0", "paths": {
    **{
        f"/v1/{name}": {"get": {"description": name, "parameters": [
            {"name": "customer_id", "in": "query", "required": True,
             "schema": {"type": "string"}},
        ]}}
        for name in apps.APP_READ_CAPABILITIES
    },
    **{
        f"/v1/{name}": {"post": {"description": name, "requestBody": {"content": {
            "application/json": {"schema": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "app_id": {"type": "string"},
                    "replicas": {"type": "integer"},
                },
                "required": ["customer_id", "app_id"],
            }}
        }}}}
        for name in apps.APP_ACTION_CAPABILITIES
    },
}}
FILES = [{"path": "agent.toml", "contents": 'entrypoint = "agent.main:run"\n'},
         {"path": "agent/__init__.py", "contents": ""},
         {"path": "agent/main.py", "contents": "def run(context):\n    return 'ran ' + "
                                                "str(context.input)\n"},
         {"path": "agent/tools.py", "contents": "def greet(name):\n    \"\"\"Say hello.\"\"\"\n"
                                                "    return 'hello ' + name\n\nTOOLS = [greet]\n"}]


def record(**overrides):
    value = {"id": "a-1", "name": "Renewal watcher", "runtime": "python",
             "purpose": "Flag renewals", "manifesto": "Company data only.",
             "goal": "A month ahead.", "skills": ["search"], "model_name": "gpt-5.5",
             "work_area": {"kind": "workspace", "detail": ""},
             "definition": {"source": "zetta", "instructions": "", "files": FILES,
                            "entrypoint": ""}}
    value.update(overrides)
    return value


class FakeLake:
    """lake's routes, answered from tables; every request is recorded."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.runs = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        ok = lambda value: httpx.Response(200, json={"ok": True, "value": value})  # noqa: E731
        if path == "/v1/openapi.json":
            return httpx.Response(200, json=OPENAPI)
        if path == "/v1/list_datasets":
            return ok({"datasets": [{"name": "accounts"}]})
        if path == "/v1/check_pipeline":
            return ok({"block_deploy": False, "checks": []})
        if path == "/v1/start_run":
            self.runs += 1
            return ok({"run_id": "r-1"})
        if path == "/v1/get_run":
            return ok({"status": "succeeded"})
        if path == "/v1/get_run_report":
            return ok({"at_risk": ["acme"]})
        if path == "/v1/get_agent":
            return ok(record())
        if path == "/v1/repository_tree":
            return ok({"items": [{"path": item["path"], "kind": "file"} for item in FILES]})
        if path == "/v1/repository_file":
            wanted = request.url.params["path"]
            return ok({"contents": next(i["contents"] for i in FILES if i["path"] == wanted)})
        return httpx.Response(404, json={"ok": False, "error": {"code": "unknown_capability",
                                                                "message": path}})


def lake_with(fake: FakeLake) -> Lake:
    return Lake("http://lake", "agent-token", CUSTOMER, transport=httpx.MockTransport(fake))


class FakeSolutions:
    """A changing solution registry, as Lake's list_solutions and get_solution answer it, plus
    the engine's holdout rule."""

    def __init__(self, old_engine: bool = False, no_catalogue: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.generation = 0
        self.old_engine = old_engine
        self.no_catalogue = no_catalogue

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/openapi.json":
            return httpx.Response(200, json=SOLUTION_OPENAPI)
        if path == "/v1/list_solutions":
            if self.old_engine:
                return failed("unknown_capability", "list_solutions")
            if self.no_catalogue:
                return succeeded({"solutions": [{"id": "own-1", "source": "workspace"}],
                                  "catalogue": {"available": False, "message":
                                                "this workspace's engine does not list Orama's "
                                                "solutions yet"}})
            self.generation += 1
            return succeeded({"solutions": [
                {"id": "S-1", "runnable": True},
                {"id": "S-2", "runnable": False, "missing": ["monitor"]},
                {"id": "B-1", "runnable": False, "status": "sketch"},
            ], "catalogue": {"available": True}})
        if path == "/v1/get_solution":
            identifier = request.url.params["id"]
            return succeeded({
                "solution": {
                    "id": identifier,
                    "generation": self.generation,
                    "runnable": identifier == "S-1",
                    "status": "sketch" if identifier == "B-1" else "ready",
                    "missing": ["monitor"] if identifier == "S-2" else [],
                },
            })
        if path == "/v1/decide":
            return succeeded({"decision_id": "d-1", "holdout": True, "actionable": False})
        if path == "/v1/record_outcome":
            return failed("holdout", "holdout decisions cannot be acted on")
        return succeeded({"accepted": True})


class FakeApps:
    """lake App and build capabilities with every request recorded."""

    def __init__(self, openapi: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.openapi = openapi or APP_OPENAPI

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/openapi.json":
            return httpx.Response(200, json=self.openapi)
        if path == "/v1/list_apps":
            return succeeded({"apps": [{"id": "app-1", "phase": "Running"}]})
        if path == "/v1/receive_build_events":
            return succeeded({"events": [{"type": "done"}], "next_sequence": 2})
        return failed("unexpected_call", path)


def succeeded(value) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "value": value})


def failed(code: str, message: str) -> httpx.Response:
    return httpx.Response(
        409,
        json={"ok": False, "error": {"code": code, "message": message}},
    )


def test_the_same_definition_loads_from_zetta_and_from_a_lake_repository(tmp_path):
    fake = FakeLake()
    inline = definition.from_record(record(), workdir=tmp_path / "zetta")
    stored = definition.from_record(
        record(definition={"source": "lake", "repository_id": "r-9", "revision": "c0ffee"}),
        lake_with(fake), workdir=tmp_path / "lake")
    assert inline.entrypoint == stored.entrypoint == "agent.main:run"
    assert (tmp_path / "lake" / "agent" / "tools.py").read_text() == FILES[3]["contents"]
    assert all(r.url.params.get("ref") == "c0ffee" for r in fake.requests)


@pytest.mark.parametrize("broken, reason", [
    ({"definition": {"source": "lake", "repository_id": "r-9"}}, "pinned revision"),
    ({"definition": {"source": "zetta", "files": [{"path": "agent.toml",
                                                   "contents": 'dependencies = ["torch"]'}]}},
     "torch"),
    ({"definition": {"source": "zetta", "entrypoint": "missing.module:run", "files": []}},
     "not in the definition"),
    ({"definition": {"source": "zetta", "files": [{"path": "../escape.py", "contents": ""}]}},
     "outside the definition"),
    ({"runtime": "native"}, "not on agent-runtime"),
])
def test_a_definition_that_cannot_run_is_refused_at_load(tmp_path, broken, reason):
    with pytest.raises(LoadError, match=reason):
        definition.from_record(record(**broken), workdir=tmp_path)


def test_skills_decide_the_workspace_tools_and_the_work_area_narrows_them(tmp_path):
    fake = FakeLake()
    agent = definition.from_record(
        record(work_area={"kind": "dataset", "detail": "accounts"},
               definition={"source": "zetta", "files": []}), workdir=tmp_path)
    derived = tools.lake_tools(lake_with(fake), OPENAPI, agent)
    names = {tool.name for tool in derived}
    assert names == {"list_datasets", "get_dataset"}, "search grants reads, never gate or create"
    get_dataset = next(tool for tool in derived if tool.name == "get_dataset")
    assert get_dataset.parameters["required"] == ["id"], "derived from the OpenAPI"
    refused = get_dataset.call({"id": "main/payroll"})
    assert not refused.ok and "only on accounts" in refused.error
    assert not fake.requests, "a refused call never reaches lake"


def test_solution_tools_require_both_the_skill_and_the_filtered_openapi(tmp_path):
    fake = FakeSolutions()
    search_agent = definition.from_record(
        record(skills=["search"], definition={"source": "zetta", "files": []}),
        workdir=tmp_path / "search",
    )
    search_names = {
        tool.name
        for tool in tools.lake_tools(lake_with(fake), SOLUTION_OPENAPI, search_agent)
    }
    assert search_names == SOLUTION_READS

    action_agent = definition.from_record(
        record(skills=["call-api"], definition={"source": "zetta", "files": []}),
        workdir=tmp_path / "action",
    )
    filtered_openapi = {
        **SOLUTION_OPENAPI,
        "paths": {
            path: operation
            for path, operation in SOLUTION_OPENAPI["paths"].items()
            if path != "/v1/approve_decision"
        },
    }
    action_tools = {
        tool.name: tool
        for tool in tools.lake_tools(lake_with(fake), filtered_openapi, action_agent)
    }
    assert set(action_tools) == SOLUTION_READS | SOLUTION_DIRECT
    assert not any(tool.proposal for tool in action_tools.values())

    # A1 approval is a person's: with Lake publishing it, the agent may only propose it.
    approve = {tool.name: tool for tool in tools.lake_tools(
        lake_with(fake), SOLUTION_OPENAPI, action_agent)}["approve_decision"]
    assert approve.proposal
    assert approve.call({"decision_id": "d-1"}).value["status"] == "proposed"
    assert not any(request.url.path == "/v1/approve_decision" for request in fake.requests)


def test_each_turn_reads_the_current_solution_registry_and_full_entries(tmp_path):
    fake = FakeSolutions()
    lake = lake_with(fake)
    agent = definition.from_record(
        record(skills=["search"], definition={"source": "zetta", "files": []}),
        workdir=tmp_path,
    )
    loaded = runner.Loaded(
        agent,
        tools.lake_tools(lake, SOLUTION_OPENAPI, agent),
        "gpt-5.5",
        lake,
    )
    history = [system_message(agent)]

    first = runner.answer(loaded, history, "what can help?", {}, Scripted(message(content="S-1")))
    second = runner.answer(loaded, history, "what changed?", {}, Scripted(message(content="S-2")))

    assert [call["tool"] for call in first["calls"]] == [
        "list_solutions",
        "get_solution",
        "get_solution",
        "get_solution",
    ]
    assert len(second["calls"]) == 4
    assert fake.generation == 2
    assert sum(request.url.path.endswith("/list_solutions") for request in fake.requests) == 2
    assert sum(request.url.path.endswith("/get_solution") for request in fake.requests) == 6
    current = [
        message
        for message in history
        if str(message.get("content", "")).startswith(solutions.CONTEXT_PREFIX)
    ]
    assert len(current) == 1
    assert '"generation": 2' in current[0]["content"]
    assert '"status": "sketch"' in current[0]["content"]
    assert '"missing": ["monitor"]' in current[0]["content"]


def test_an_old_engine_has_no_solution_catalogue_and_says_not_deployed(tmp_path):
    fake = FakeSolutions(old_engine=True)
    lake = lake_with(fake)
    agent = definition.from_record(
        record(skills=["search"], definition={"source": "zetta", "files": []}),
        workdir=tmp_path,
    )
    derived = tools.lake_tools(lake, SOLUTION_OPENAPI, agent)
    catalog = solutions.read_solution_catalog(derived)

    assert not catalog.available
    assert catalog.not_deployed
    assert catalog.solutions == []
    assert catalog.reason == "list_solutions is not deployed yet"
    assert catalog.calls[0]["error"].startswith("unknown_capability")
    list_tool = next(tool for tool in derived if tool.name == "list_solutions")
    assert "not deployed yet" in list_tool.call({}).observation()


def test_a_workspace_without_oramas_catalogue_still_lists_its_own(tmp_path):
    fake = FakeSolutions(no_catalogue=True)
    agent = definition.from_record(
        record(skills=["search"], definition={"source": "zetta", "files": []}),
        workdir=tmp_path,
    )
    catalog = solutions.read_solution_catalog(
        tools.lake_tools(lake_with(fake), SOLUTION_OPENAPI, agent))

    assert catalog.available and catalog.not_deployed
    assert [entry["id"] for entry in catalog.solutions] == ["own-1"]
    assert "does not list Orama's solutions yet" in catalog.reason
    assert "not deployed in this workspace's engine yet" in catalog.message()["content"]


def test_a_holdout_refusal_from_the_engine_is_the_action_observation(tmp_path):
    fake = FakeSolutions()
    lake = lake_with(fake)
    agent = definition.from_record(
        record(skills=["call-api"], definition={"source": "zetta", "files": []}),
        workdir=tmp_path,
    )
    derived = {
        tool.name: tool
        for tool in tools.lake_tools(lake, SOLUTION_OPENAPI, agent)
    }

    decision = derived["decide"].call({"entity": "held-account"})
    refused = derived["record_outcome"].call({"action_id": decision.value["decision_id"]})

    assert decision.value == {"decision_id": "d-1", "holdout": True, "actionable": False}
    assert not refused.ok
    assert refused.code == "holdout"
    assert "holdout decisions cannot be acted on" in refused.observation()
    assert [request.url.path for request in fake.requests] == [
        "/v1/decide",
        "/v1/record_outcome",
    ]
    sent = json.loads(fake.requests[0].content)
    assert sent["customer_id"] == CUSTOMER and sent["entity"] == "held-account"


def test_app_reads_are_always_tools_and_actions_require_the_filtered_openapi(tmp_path):
    fake = FakeApps()
    lake = lake_with(fake)
    agent = definition.from_record(
        record(skills=[], definition={"source": "zetta", "files": []}),
        workdir=tmp_path,
    )
    derived = tools.all_tools(agent, lake, APP_OPENAPI)
    by_name = {tool.name: tool for tool in derived}

    assert set(by_name) == apps.APP_CAPABILITIES
    assert {name for name, tool in by_name.items() if tool.proposal} == \
        apps.APP_ACTION_CAPABILITIES
    assert "customer_id" not in by_name["list_apps"].parameters["properties"]
    assert by_name["deploy_app"].parameters["required"] == ["app_id"]

    listed = by_name["list_apps"].call({})
    events = by_name["receive_build_events"].call({"build_id": "build-1"})

    assert listed.value["apps"][0]["id"] == "app-1"
    assert events.value["next_sequence"] == 2
    assert fake.requests[-2].url.params["customer_id"] == CUSTOMER
    assert fake.requests[-1].url.params["customer_id"] == CUSTOMER

    filtered = {
        **APP_OPENAPI,
        "paths": {
            path: operation
            for path, operation in APP_OPENAPI["paths"].items()
            if path not in {"/v1/scale_app", "/v1/build_app"}
        },
    }
    filtered_names = {tool.name for tool in tools.lake_tools(lake, filtered)}
    assert "scale_app" not in filtered_names
    assert "build_app" not in filtered_names


def test_app_actions_become_exact_proposals_and_never_reach_lake(tmp_path):
    fake = FakeApps()
    lake = lake_with(fake)
    agent = definition.from_record(
        record(skills=[], definition={"source": "zetta", "files": []}),
        workdir=tmp_path,
    )
    derived = tools.lake_tools(lake, APP_OPENAPI)
    context = apps.app_context(derived)
    messages = [system_message(agent)]
    apps.replace_context(messages, context)
    call = SimpleNamespace(
        id="app-call-1",
        function=SimpleNamespace(
            name="deploy_app",
            arguments=json.dumps({"app_id": "app-1", "image": "team/app:v2"}),
        ),
    )
    client = Scripted(
        message(tool_calls=[call]),
        message(content="I proposed deploying team/app:v2 for your review."),
    )

    result = run_loop(agent, derived, messages, client, "gpt-5.5")

    assert result.finished
    assert result.proposals == [{
        "capability": "deploy_app",
        "input": {"app_id": "app-1", "image": "team/app:v2"},
        "impact": "deploy_app",
    }]
    assert result.calls[0]["observation"]["status"] == "proposed"
    assert not any(request.url.path == "/v1/deploy_app" for request in fake.requests)
    assert "agent-application" in messages[1]["content"]
    assert '"kind": "app"' in messages[1]["content"]


def test_an_entrypoint_records_app_proposals_in_its_result(tmp_path):
    fake = FakeApps()
    lake = lake_with(fake)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "__init__.py").write_text("")
    (tmp_path / "agent" / "main.py").write_text(
        "def run(context):\n"
        "    proposed = context.call_tool('scale_app', "
        "{'app_id': 'app-1', 'replicas': 3})\n"
        "    return 'proposed' if proposed.ok else 'failed'\n"
    )
    (tmp_path / "agent.toml").write_text('entrypoint = "agent.main:run"\n')
    (tmp_path / "record.json").write_text(json.dumps({"id": "app-operator"}))
    agent = definition.from_record(definition.from_directory(tmp_path))
    loaded = runner.Loaded(agent, tools.lake_tools(lake, APP_OPENAPI), "gpt-5.5", lake)

    result = runner.run_once(loaded, {"request": "scale"}, None)

    assert result.reply == "proposed"
    assert result.proposals == [{
        "capability": "scale_app",
        "input": {"app_id": "app-1", "replicas": 3},
        "impact": "scale_app",
    }]
    assert not any(request.url.path == "/v1/scale_app" for request in fake.requests)


def test_each_turn_refreshes_lake_app_grants(tmp_path):
    fake = FakeApps()
    lake = lake_with(fake)
    loaded = runner.load(
        lake,
        "app-reader",
        record(skills=[], definition={"source": "zetta", "files": []}),
    )

    assert "scale_app" in {tool.name for tool in loaded.tools}

    fake.openapi = {
        **APP_OPENAPI,
        "paths": {
            path: operation
            for path, operation in APP_OPENAPI["paths"].items()
            if path != "/v1/scale_app"
        },
    }
    refreshed, temporary_lake = runner.per_turn(loaded, {})

    assert temporary_lake is None
    assert "scale_app" not in {tool.name for tool in refreshed.tools}


def test_a_granted_pipeline_gates_runs_and_returns_its_report(tmp_path):
    fake = FakeLake()
    agent = definition.from_record(
        record(pipelines=[{"project": "churn", "graph": "weekly", "version": "2026-09-26"}],
               definition={"source": "zetta", "files": []}), workdir=tmp_path)
    [tool] = tools.pipeline_tools(agent, lake_with(fake), sleep=lambda _: None)
    assert tool.name == "pipeline_weekly"
    answer = tool.call({"dataset": "datasets.accounts"})
    assert answer.ok and answer.value["report"] == {"at_risk": ["acme"]}
    assert fake.runs == 1
    assert [request.url.path for request in fake.requests] == [
        "/v1/check_pipeline", "/v1/start_run", "/v1/get_run", "/v1/get_run_report"]
    started = json.loads(fake.requests[1].content)
    assert started == {"project": "churn", "graph": "weekly", "dataset": "datasets.accounts",
                       "customer_id": CUSTOMER}


def test_a_blocked_gate_runs_nothing(tmp_path):
    fake = FakeLake()
    agent = definition.from_record(
        record(pipelines=[{"project": "churn", "graph": "weekly"}],
               definition={"source": "zetta", "files": []}), workdir=tmp_path)
    lake = lake_with(fake)
    lake.capability = lambda name, arguments, method, fixed=None: (  # type: ignore[method-assign]
        Reply(True, {"block_deploy": True, "checks": [{"name": "leakage", "passed": False}]})
        if name == "check_pipeline" else Reply(True, {"run_id": "never"}))
    [tool] = tools.pipeline_tools(agent, lake, sleep=lambda _: None)
    blocked = tool.call({})
    assert not blocked.ok and "leakage" in blocked.error


def test_the_definitions_own_functions_are_tools(tmp_path):
    agent = definition.from_record(record(), workdir=tmp_path)
    [greet] = tools.definition_tools(agent)
    assert greet.description == "Say hello."
    assert greet.call({"name": "lake"}).value == "hello lake"


def message(content=None, tool_calls=None):
    value = SimpleNamespace(content=content, tool_calls=tool_calls)
    value.model_dump = lambda exclude_none=True: {"role": "assistant", "content": content or ""}
    return SimpleNamespace(choices=[SimpleNamespace(message=value)])


class Scripted:
    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **_):
        return self.replies.pop(0)


def test_the_loop_answers_tool_calls_with_what_came_back(tmp_path):
    fake = FakeLake()
    agent = definition.from_record(record(definition={"source": "zetta", "files": []}),
                                   workdir=tmp_path)
    derived = tools.lake_tools(lake_with(fake), OPENAPI, agent)
    call = SimpleNamespace(id="c-1", function=SimpleNamespace(name="list_datasets",
                                                              arguments="{}"))
    ghost = SimpleNamespace(id="c-2", function=SimpleNamespace(name="drop_everything",
                                                               arguments="{}"))
    client = Scripted(message(tool_calls=[call, ghost]), message(content="accounts is there"))
    messages = [system_message(agent), {"role": "user", "content": "what data is there?"}]
    result = run_loop(agent, derived, messages, client, "gpt-5.5")
    assert result.finished and result.reply == "accounts is there"
    assert result.calls[0]["ok"] and result.calls[0]["observation"]["datasets"][0]["name"] == \
        "accounts"
    assert not result.calls[1]["ok"], "an unknown tool is a failed observation, not a crash"
    assert "Company data only." in messages[0]["content"]


def test_a_job_runs_the_entrypoint_and_leaves_a_trace_without_the_key(tmp_path, monkeypatch):
    fake = FakeLake()
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("AGENT_INPUT", json.dumps({"week": 39}))
    monkeypatch.setenv("AGENT_ID", "a-1")
    monkeypatch.setattr(runner, "lake_from_environment", lambda: lake_with(fake))
    monkeypatch.setenv("LAKE_URL", "http://lake:8080")
    monkeypatch.setenv("LAKE_TOKEN", "lal.launch.secret")
    gateways = []
    monkeypatch.setattr(runner, "gateway_client",
                        lambda url, token: gateways.append((url, token)) or None)
    summaries = []
    monkeypatch.setattr(runner, "termination_summary", summaries.append)
    assert runner.job() == 0
    assert gateways == [("http://lake:8080", "lal.launch.secret")], \
        "a job calls its model through lake with its launch's key (lake#248)"
    assert summaries[0]["reply"] == "ran {'week': 39}"
    trace = json.loads(next((tmp_path / "traces").glob("*.json")).read_text())
    assert "agent-token" not in json.dumps(trace)


def test_a_definition_built_into_an_image_loads_in_place(tmp_path):
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "__init__.py").write_text("")
    (tmp_path / "agent" / "main.py").write_text(
        "def run(context):\n    return 'for ' + context.input['customer_id']\n")
    (tmp_path / "agent.toml").write_text('entrypoint = "agent.main:run"\n')
    (tmp_path / "record.json").write_text(json.dumps({"id": "agent-creator",
                                                      "name": "AgentCreator"}))
    record = definition.from_directory(tmp_path)
    agent = definition.from_record(record)
    assert agent.directory == tmp_path and agent.entrypoint == "agent.main:run"
    loaded = runner.Loaded(agent, [], "gpt-5.5")
    history: list = []
    answer = runner.answer(loaded, history, "make me an agent", {"customer_id": "c-9"}, None)
    assert answer["reply"] == "for c-9"
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_a_lake_wide_agent_needs_the_persons_key_on_every_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PER_TURN_CREDENTIALS", "1")
    monkeypatch.setenv("LAKE_URL", "http://lake")
    agent = definition.from_record(record(definition={"source": "zetta", "files": []}),
                                   workdir=tmp_path)
    with pytest.raises(LoadError, match="lake_token"):
        runner.per_turn(runner.Loaded(agent, [], "gpt-5.5"), {"message": "hi"})


def test_an_entrypoint_answers_words_and_a_structured_result(tmp_path):
    """agent-creator#1: an API caller reads `result`; a person reads `reply`."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "__init__.py").write_text("")
    (tmp_path / "agent" / "main.py").write_text(
        "def run(context):\n    return {'reply': 'Which dataset?', "
        "'result': {'kind': 'question', 'question': 'Which dataset?'}}\n")
    (tmp_path / "agent.toml").write_text('entrypoint = "agent.main:run"\n')
    (tmp_path / "record.json").write_text(json.dumps({"id": "agent-creator"}))
    agent = definition.from_record(definition.from_directory(tmp_path))
    answer = runner.answer(runner.Loaded(agent, [], "gpt-5.5"), [], "make me an agent",
                           {"customer_id": "c-9"}, None)
    assert answer["reply"] == "Which dataset?"
    assert answer["result"] == {"kind": "question", "question": "Which dataset?"}


def test_every_model_call_goes_through_lakes_gateway_with_the_runs_key(monkeypatch):
    """lake#248: the runtime and the agent code it runs reach the model only through lake."""
    import httpx

    from agent_runtime import model

    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-5.5",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(runner, "gateway_client", lambda url, token: model.gateway_client(
        url, token, http_client=httpx.Client(transport=httpx.MockTransport(gateway))))
    # A per-turn agent (AgentCreator) uses the key of whoever sent the turn.
    client = runner.turn_model(None, {"lake_url": "http://lake:8080", "lake_token": "lat.t.s"})
    client.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "x"}])
    assert str(seen[0].url) == "http://lake:8080/openai/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer lat.t.s"
    # A person's own agent keeps the client made with its launch's key.
    own = object()
    assert runner.turn_model(own, {"lake_token": "lat.t.s"}) is own
    try:
        model.gateway_client("http://lake", "")
    except ValueError:
        pass
    else:
        raise AssertionError("a model call without a lake key is refused")
