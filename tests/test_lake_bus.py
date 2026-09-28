"""The agent bus worker (lake docs/api/agent-bus.md): receive, claim, run, publish."""

from __future__ import annotations

import json
import threading
import urllib.parse
from typing import Any

from agent_runtime import lake_bus


class FakeLake:
    """lake's bus capabilities in memory: one task waiting, claimable once."""

    def __init__(self, tasks: list[dict[str, Any]], claimed_elsewhere: set[str] | None = None):
        self.tasks = tasks
        self.claimed: set[str] = set(claimed_elsewhere or ())
        self.events: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.stop = threading.Event()

    def send(self, method: str, url: str, headers: dict[str, str],
             body: bytes | None) -> tuple[int, str]:
        self.headers.append(headers)
        parsed = urllib.parse.urlparse(url)
        capability = parsed.path.rsplit("/", 1)[-1]
        if capability == "receive_agent_tasks":
            query = dict(urllib.parse.parse_qsl(parsed.query))
            start = int(query["from_sequence"])
            waiting = [t for i, t in enumerate(self.tasks, 1) if i >= start]
            if not waiting:
                self.stop.set()
            return 200, json.dumps({"ok": True, "value": {
                "tasks": [{"sequence": i, **t} for i, t in enumerate(self.tasks, 1) if i >= start],
                "next_sequence": len(self.tasks) + 1}})
        arguments = json.loads(body or b"{}")
        if capability == "claim_agent_task":
            if arguments["task_id"] in self.claimed:
                return 409, json.dumps({"ok": False, "error": {"code": "conflict",
                                                               "message": "taken"}})
            self.claimed.add(arguments["task_id"])
            return 200, json.dumps({"ok": True, "value": {
                "task_id": arguments["task_id"],
                "body": {"message": "hi", "lake_token": "lat." + arguments["task_id"] + ".x"}}})
        if capability == "publish_agent_event":
            self.events.append(arguments)
            return 200, json.dumps({"ok": True, "value": {"event_id": "e"}})
        return 404, json.dumps({"ok": False, "error": {"code": "unknown_capability"}})


def run(lake: FakeLake, handle) -> None:
    bus = lake_bus.LakeBus("http://lake", "bus-token", "agent-x", send=lake.send)
    lake_bus.work(bus, handle, lake.stop, workers=1)


def test_a_task_is_claimed_run_and_answered_on_the_bus():
    lake = FakeLake([{"task_id": "t1", "customer_id": "c", "kind": "ask"}])
    handed: list[dict[str, Any]] = []
    run(lake, lambda body: handed.append(body) or {"ok": True, "reply": "hello"})
    assert handed == [{"message": "hi", "lake_token": "lat.t1.x"}], "the body arrives with its credential"
    assert [e["type"] for e in lake.events] == ["progress", "result"]
    assert lake.events[-1]["result"] == {"ok": True, "reply": "hello"}
    assert all(e["agent"] == "agent-x" for e in lake.events)
    assert all(h["Authorization"] == "Bearer bus-token" for h in lake.headers)


def test_a_task_another_replica_took_is_skipped():
    lake = FakeLake([{"task_id": "t1"}], claimed_elsewhere={"t1"})
    handed: list[dict[str, Any]] = []
    run(lake, lambda body: handed.append(body) or {"ok": True})
    assert not handed and not lake.events


def test_a_failed_turn_is_reported_as_failed():
    lake = FakeLake([{"task_id": "t1"}, {"task_id": "t2"}])

    def handle(body: dict[str, Any]) -> dict[str, Any]:
        if body["lake_token"].startswith("lat.t1"):
            raise RuntimeError("model unavailable")
        return {"ok": False, "error": "no workspace"}

    run(lake, handle)
    failed = [e for e in lake.events if e["type"] == "failed"]
    assert [e["task_id"] for e in failed] == ["t1", "t2"]
    assert "model unavailable" in failed[0]["message"]
    assert failed[1]["message"] == "no workspace"


def test_the_worker_starts_only_when_the_operator_configured_the_bus(monkeypatch):
    monkeypatch.delenv("AGENT_BUS_TOKEN", raising=False)
    monkeypatch.setenv("LAKE_URL", "http://lake")
    assert lake_bus.start_from_env(lambda body: {"ok": True}, "agent-x") is None
