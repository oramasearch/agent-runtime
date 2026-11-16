# SPDX-License-Identifier: MIT

"""agent-ask-orama#43 (LSR-075): a task is claimed only when a worker is free to run it.

The bus worker used to claim every task it received and queue it on the executor, so a burst from
one tenant sat in this process's memory (bodies and task credentials included) behind four busy
workers, and other tenants waited behind it. These tests block every worker and offer more tasks
than there are workers.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from typing import Any

from agent_runtime import lake_bus


class BusyLake:
    """lake's bus capabilities in memory, closer to lake than test_lake_bus.FakeLake: a receive
    answers only unclaimed tasks, a claim hands over the body with its credential, and a terminal
    event finishes the task. It counts what this replica holds."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = tasks
        self.lock = threading.Lock()
        self.claimed: list[str] = []
        self.claimed_elsewhere: set[str] = set()
        self.finished: list[str] = []
        self.held_most = 0  # the most tasks claimed here and not yet finished, at any moment
        self.receives = 0

    def holding(self) -> int:
        return len(self.claimed) - len(self.finished)

    def take_elsewhere(self, task_id: str) -> bool:
        """Another replica claims a task. False when it was already taken."""
        with self.lock:
            if task_id in self.claimed or task_id in self.claimed_elsewhere:
                return False
            self.claimed_elsewhere.add(task_id)
            return True

    def send(self, method: str, url: str, headers: dict[str, str],
             body: bytes | None) -> tuple[int, str]:
        parsed = urllib.parse.urlparse(url)
        capability = parsed.path.rsplit("/", 1)[-1]
        with self.lock:
            if capability == "receive_agent_tasks":
                self.receives += 1
                start = int(dict(urllib.parse.parse_qsl(parsed.query))["from_sequence"])
                taken = set(self.claimed) | self.claimed_elsewhere
                waiting = [{"sequence": i, **t} for i, t in enumerate(self.tasks, 1)
                           if i >= start and t["task_id"] not in taken]
                return 200, json.dumps({"ok": True, "value": {
                    "tasks": waiting, "next_sequence": len(self.tasks) + 1}})
            arguments = json.loads(body or b"{}")
            task_id = arguments.get("task_id")
            if capability == "claim_agent_task":
                if task_id in self.claimed or task_id in self.claimed_elsewhere:
                    return 409, json.dumps({"ok": False, "error": {"code": "conflict",
                                                                   "message": "taken"}})
                self.claimed.append(task_id)
                self.held_most = max(self.held_most, self.holding())
                return 200, json.dumps({"ok": True, "value": {
                    "task_id": task_id,
                    "body": {"message": task_id, "lake_token": f"lat.{task_id}.secret"}}})
            if capability == "publish_agent_event":
                if arguments["type"] in ("result", "failed"):
                    self.finished.append(task_id)
                return 200, json.dumps({"ok": True, "value": {"event_id": "e"}})
        return 404, json.dumps({"ok": False, "error": {"code": "unknown_capability"}})


def wait_until(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class BlockedHandler:
    """Every turn waits until the test lets it go."""

    def __init__(self) -> None:
        self.go = threading.Event()
        self.lock = threading.Lock()
        self.started: list[str] = []

    def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.started.append(body["message"])
        self.go.wait(10)
        return {"ok": True, "reply": body["message"]}


def start(lake: BusyLake, handle, workers: int, **options: Any):
    stop = threading.Event()
    bus = lake_bus.LakeBus("http://lake", "bus-token", "agent-x", send=lake.send)
    thread = threading.Thread(target=lake_bus.work, args=(bus, handle, stop),
                              kwargs={"workers": workers, "wait_ms": 20, **options}, daemon=True)
    thread.start()
    return stop, thread


def burst(customer: str, count: int) -> list[dict[str, Any]]:
    return [{"task_id": f"{customer}{i}", "customer_id": customer, "kind": "api"}
            for i in range(count)]


def test_a_burst_is_claimed_only_as_workers_free_up_and_the_rest_stays_on_the_bus():
    lake = BusyLake(burst("a", 12))
    handle = BlockedHandler()
    stop, thread = start(lake, handle, workers=2, tenant_workers=2)
    try:
        assert wait_until(lambda: len(handle.started) == 2), "both workers start a turn"
        time.sleep(0.3)  # time for an unbounded consumer to claim the whole burst
        assert len(lake.claimed) == 2, f"claimed {len(lake.claimed)} tasks with 2 workers busy"
        unclaimed = [t["task_id"] for t in lake.tasks if t["task_id"] not in lake.claimed]
        assert len(unclaimed) == 10
        # An unclaimed task is still the bus's: another replica can take it, and this one then
        # never runs it.
        assert lake.take_elsewhere(unclaimed[0])
        handle.go.set()
        assert wait_until(lambda: len(lake.finished) == 11), "the rest run as workers free up"
    finally:
        handle.go.set()
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert lake.held_most <= 2, f"held {lake.held_most} claimed tasks with 2 workers"
    assert unclaimed[0] not in handle.started
    assert sorted(handle.started) == sorted(t for t in lake.claimed)


def test_a_second_tenant_gets_a_worker_while_the_first_floods_the_bus():
    lake = BusyLake(burst("a", 10) + burst("b", 1))
    handle = BlockedHandler()
    stop, thread = start(lake, handle, workers=2)  # one tenant may hold workers - 1 of them
    try:
        assert wait_until(lambda: len(handle.started) == 2)
        time.sleep(0.3)
        assert sorted(handle.started) == ["a0", "b0"], "b is served beside a's burst"
        assert len(lake.claimed) == 2
        handle.go.set()
        assert wait_until(lambda: len(lake.finished) == 11)
    finally:
        handle.go.set()
        stop.set()
        thread.join(5)
    assert lake.held_most <= 2


def test_a_tenant_alone_never_holds_more_than_its_share():
    lake = BusyLake(burst("a", 8))
    handle = BlockedHandler()
    stop, thread = start(lake, handle, workers=4)
    try:
        assert wait_until(lambda: len(handle.started) == 3)
        time.sleep(0.3)
        assert len(handle.started) == 3, "one worker stays free for another tenant"
        assert len(lake.claimed) == 3
        handle.go.set()
        assert wait_until(lambda: len(lake.finished) == 8)
    finally:
        handle.go.set()
        stop.set()
        thread.join(5)


def test_no_receive_is_made_while_every_worker_is_busy():
    lake = BusyLake(burst("a", 1) + burst("b", 1))
    handle = BlockedHandler()
    stop, thread = start(lake, handle, workers=2)
    try:
        assert wait_until(lambda: len(handle.started) == 2)
        time.sleep(0.05)
        receives = lake.receives
        time.sleep(0.3)
        assert lake.receives == receives, "a full worker pool asks lake for nothing more"
    finally:
        handle.go.set()
        stop.set()
        thread.join(5)


def test_a_turn_that_raises_gives_its_worker_back():
    lake = BusyLake(burst("a", 3))

    def handle(body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("model unavailable")

    stop, thread = start(lake, handle, workers=1)
    try:
        assert wait_until(lambda: len(lake.finished) == 3), "each failure frees the one worker"
    finally:
        stop.set()
        thread.join(5)
    assert lake.held_most == 1


def test_the_worker_counts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_WORKERS", "6")
    monkeypatch.setenv("AGENT_BUS_TENANT_WORKERS", "2")
    assert lake_bus.worker_counts() == (6, 2)
    monkeypatch.delenv("AGENT_BUS_TENANT_WORKERS")
    assert lake_bus.worker_counts() == (6, 5)
    monkeypatch.setenv("AGENT_BUS_WORKERS", "1")
    assert lake_bus.worker_counts() == (1, 1)
    for bad in ("0", "-3", "many", ""):
        monkeypatch.setenv("AGENT_BUS_WORKERS", bad)
        assert lake_bus.worker_counts() == (4, 3)
    monkeypatch.setenv("AGENT_BUS_WORKERS", "4")
    monkeypatch.setenv("AGENT_BUS_TENANT_WORKERS", "9")
    assert lake_bus.worker_counts() == (4, 4), "a tenant never has more than every worker"
