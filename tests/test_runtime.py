"""agent-runtime without a network: lake is an httpx MockTransport answering the way lake and
Zetta do, the model a scripted client. The live run is the deploy's proof (lake#228)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent_runtime import definition, runner, tools
from agent_runtime.lake import Lake, LoadError, Reply
from agent_runtime.loop import run_loop, system_message

CUSTOMER = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
OPENAPI = {"openapi": "3.1.0", "paths": {
    "/v1/list_datasets": {"get": {"description": "list datasets", "parameters": []}},
    "/v1/get_dataset": {"get": {"description": "one dataset", "parameters": [
        {"name": "dataset", "in": "query", "required": True, "schema": {"type": "string"}}]}},
    "/v1/gate": {"post": {"description": "gate", "requestBody": {"content": {"application/json": {
        "schema": {"type": "object", "properties": {"project": {"type": "string"}}}}}}}},
    "/v1/create_project": {"post": {"description": "create", "requestBody": {}}},
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
        base = f"/workspaces/{CUSTOMER}/zetta/v1/"
        if path == base + "openapi.json":
            return httpx.Response(200, json=OPENAPI)
        if path == base + "list_datasets":
            return ok({"datasets": [{"name": "accounts"}]})
        if path == base + "gate":
            return ok({"block_deploy": False, "checks": []})
        if path == base + "run":
            self.runs += 1
            return ok({"run_id": "r-1"})
        if path == base + "get_run":
            return ok({"status": "succeeded"})
        if path == base + "get_run_report":
            return ok({"at_risk": ["acme"]})
        if path == base + "get_agent":
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
    derived = tools.workspace_tools(agent, lake_with(fake), OPENAPI)
    names = {tool.name for tool in derived}
    assert names == {"list_datasets", "get_dataset"}, "search grants reads, never gate or create"
    get_dataset = next(tool for tool in derived if tool.name == "get_dataset")
    assert get_dataset.parameters["required"] == ["dataset"], "derived from the OpenAPI"
    refused = get_dataset.call({"dataset": "payroll"})
    assert not refused.ok and "only on accounts" in refused.error
    assert not fake.requests, "a refused call never reaches lake"


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


def test_a_blocked_gate_runs_nothing(tmp_path):
    fake = FakeLake()
    agent = definition.from_record(
        record(pipelines=[{"project": "churn", "graph": "weekly"}],
               definition={"source": "zetta", "files": []}), workdir=tmp_path)
    lake = lake_with(fake)
    lake.zetta = lambda name, arguments, method: (  # type: ignore[method-assign]
        Reply(True, {"block_deploy": True, "checks": [{"name": "leakage", "passed": False}]})
        if name == "gate" else Reply(True, {"run_id": "never"}))
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
    derived = tools.workspace_tools(agent, lake_with(fake), OPENAPI)
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
    monkeypatch.setattr(runner, "OpenAI", lambda: None)
    summaries = []
    monkeypatch.setattr(runner, "termination_summary", summaries.append)
    assert runner.job() == 0
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
