"""Task credentials by reference (lake#871, lake docs/api/agent-bus.md).

A claim asks Lake for `credentials: by_reference`: the body arrives with no credential, and every
call made for the task carries this agent's bus credential and `X-Lake-Agent-Task: <task id>`. A
Lake from before lake#871 refuses the field; the claim is asked again and the body's own credential
is used, as before.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from typing import Any

import httpx
import pytest

from agent_runtime import definition, lake_bus, model, runner

TASK = {"task_id": "t1", "customer_id": "c", "workspace_id": "w", "kind": "ask"}


class BusLake:
    """lake's bus in memory. `supports_by_reference` False is a Lake from before lake#871."""

    def __init__(self, supports_by_reference: bool) -> None:
        self.supports_by_reference = supports_by_reference
        self.claims: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.stop = threading.Event()
        self.received = False

    def send(self, method: str, url: str, headers: dict[str, str],
             body: bytes | None) -> tuple[int, str]:
        self.headers.append(headers)
        capability = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        if capability == "receive_agent_tasks":
            tasks = [] if self.received else [{"sequence": 1, **TASK}]
            self.received = True
            if not tasks:
                self.stop.set()
            return 200, json.dumps({"ok": True, "value": {"tasks": tasks, "next_sequence": 2}})
        arguments = json.loads(body or b"{}")
        if capability == "claim_agent_task":
            self.claims.append(arguments)
            if "credentials" in arguments and not self.supports_by_reference:
                return 400, json.dumps({"ok": False, "error": {
                    "code": "invalid_input", "message": "unexpected field credentials",
                    "path": "/credentials"}})
            by_reference = arguments.get("credentials") == "by_reference"
            credential = "" if by_reference else "lat.t1.secret"
            value = {**TASK, "body": {"message": "hi", "lake_token": credential,
                                      "zetta_token": credential}}
            if by_reference:
                value["credentials"] = "by_reference"
            return 200, json.dumps({"ok": True, "value": value})
        return 200, json.dumps({"ok": True, "value": {}})


def handed_body(lake: BusLake) -> dict[str, Any]:
    handed: list[dict[str, Any]] = []
    bus = lake_bus.LakeBus("http://lake", "bus-token", "agent-creator", send=lake.send)
    lake_bus.work(bus, lambda body: handed.append(body) or {"ok": True}, lake.stop, workers=1)
    assert len(handed) == 1, handed
    return handed[0]


def test_a_claim_asks_by_reference_and_the_turn_holds_no_task_credential():
    lake = BusLake(supports_by_reference=True)
    body = handed_body(lake)
    assert lake.claims == [{"task_id": "t1", "agent": "agent-creator", "credentials": "by_reference"}]
    assert body["lake_token"] == "bus-token", "the turn calls Lake with the bus credential"
    assert body["zetta_token"] == "bus-token"
    assert body["lake_headers"] == {"X-Lake-Agent-Task": "t1"}
    assert lake_bus.task_headers(body) == {"X-Lake-Agent-Task": "t1"}
    # Bus calls themselves never name a task: Lake would answer them as the task.
    assert all(lake_bus.TASK_HEADER not in headers for headers in lake.headers)


def test_a_lake_from_before_by_reference_is_claimed_again_with_the_credential_in_the_body():
    lake = BusLake(supports_by_reference=False)
    body = handed_body(lake)
    assert lake.claims == [
        {"task_id": "t1", "agent": "agent-creator", "credentials": "by_reference"},
        {"task_id": "t1", "agent": "agent-creator"},
    ]
    assert body["lake_token"] == "lat.t1.secret", "the body's own credential is used, as before"
    assert "lake_headers" not in body
    assert lake_bus.task_headers(body) == {}


def test_a_claim_refused_for_another_reason_is_not_asked_again():
    assert not lake_bus.refuses_by_reference(
        lake_bus.BusError("invalid_input", "task_id is required"))
    assert lake_bus.refuses_by_reference(
        lake_bus.BusError("invalid_input", "unexpected field credentials"))

    claims: list[dict[str, Any]] = []

    def send(method, url, headers, body):
        claims.append(json.loads(body or b"{}"))
        return 403, json.dumps({"ok": False, "error": {"code": "forbidden", "message": "no"}})

    bus = lake_bus.LakeBus("http://lake", "bus-token", "agent-creator", send=send)
    with pytest.raises(lake_bus.BusError):
        bus.claim("t1")
    assert len(claims) == 1


def test_task_headers_take_only_the_task_header():
    assert lake_bus.task_headers({"lake_headers": {"X-Lake-Agent-Task": "t1",
                                                   "Authorization": "Bearer other"}}) == {
        "X-Lake-Agent-Task": "t1"}
    assert lake_bus.task_headers({"lake_headers": "t1"}) == {}
    assert lake_bus.task_headers({}) == {}


@pytest.fixture
def sent(monkeypatch):
    """Every request a per-turn agent's Lake client and model client make."""
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
            "paths": {}})

    real = runner.Lake

    def lake(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(answer)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "Lake", lake)
    monkeypatch.setattr(runner, "gateway_client", lambda url, token, **kw: model.gateway_client(
        url, token, http_client=httpx.Client(transport=httpx.MockTransport(answer)), **kw))
    monkeypatch.setenv("LAKE_URL", "http://lake:8080")
    monkeypatch.setenv("AGENT_PER_TURN_CREDENTIALS", "1")
    return requests


def per_turn_agent(tmp_path):
    return runner.Loaded(definition.from_record(
        {"id": "agent-creator", "name": "AgentCreator", "runtime": "python",
         "definition": {"source": "zetta", "files": []}},
        workdir=tmp_path), [], "m")


def run_turn(turn: dict[str, Any], tmp_path) -> None:
    _, lake = runner.per_turn(per_turn_agent(tmp_path), turn)
    assert lake is not None
    lake.read("list_datasets")
    lake.close()
    client = runner.turn_model(None, turn)
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}])


def test_every_call_for_a_by_reference_turn_names_its_task(sent, tmp_path):
    """The per-turn Lake client (OpenAPI, capabilities) and the model calls through the gateway
    carry the bus credential and the task header."""
    claimed = {**TASK, "credentials": "by_reference",
               "body": {"message": "hi", "lake_token": "", "customer_id": "c"}}
    run_turn(lake_bus.task_credentials("bus-token", claimed), tmp_path)
    assert len(sent) >= 3
    for request in sent:
        assert request.headers["authorization"] == "Bearer bus-token"
        assert request.headers["x-lake-agent-task"] == "t1"


def test_a_turn_with_its_credential_in_the_body_sends_no_task_header(sent, tmp_path):
    claimed = {**TASK, "body": {"message": "hi", "lake_token": "lat.t1.secret",
                                "customer_id": "c"}}
    run_turn(lake_bus.task_credentials("bus-token", claimed), tmp_path)
    assert sent
    for request in sent:
        assert request.headers["authorization"] == "Bearer lat.t1.secret"
        assert "x-lake-agent-task" not in request.headers
