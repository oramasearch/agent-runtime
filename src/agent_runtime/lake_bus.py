# SPDX-License-Identifier: MIT

"""Take tasks from lake's agent bus and answer them (lake docs/api/agent-bus.md, lake#269).

Lake publishes each turn a person asks of this agent on Zetta pub/sub (topic
`agent-tasks.<agent>` on lake's own Zetta). The agent never holds the bus's token: it reaches the
bus through lake, with its own bus credential (`AGENT_BUS_TOKEN`, minted by the Orama operator for
this agent alone):

1. `receive_agent_tasks` long-polls for new tasks, only while a worker is free;
2. `claim_agent_task` takes one (exactly one claim wins) and answers the turn's body with the
   task's own credential in it. A task is claimed only when a worker is free to run it at once
   (agent-runtime#24, from agent-ask-orama#43); until then it stays on the bus for any replica;
3. the turn runs, and `publish_agent_event` reports `progress`, then the `result` or `failed`.

The same file is in every library agent (agent-ask-orama, agent-project, agent-autosuggest,
agent-runtime for AgentCreator). Standard library only.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

WAIT_MS = 10_000

# (method, url, headers, body or None) -> (status, body text)
Send = Callable[[str, str, dict[str, str], bytes | None], tuple[int, str]]


def _urllib_send(method: str, url: str, headers: dict[str, str],
                 body: bytes | None) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        # A long poll waits WAIT_MS inside lake; leave it room.
        with urllib.request.urlopen(request, timeout=WAIT_MS / 1000 + 20) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


class BusError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class LakeBus:
    """This agent's side of the bus, through lake."""

    def __init__(self, lake_url: str, token: str, agent: str, send: Send | None = None) -> None:
        self.lake_url = lake_url.rstrip("/")
        self.token = token
        self.agent = agent
        self.send = send or _urllib_send

    def call(self, capability: str, arguments: dict[str, Any], read: bool) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        url = f"{self.lake_url}/v1/{capability}"
        body = None
        if read:
            url += "?" + urllib.parse.urlencode(
                {k: v if isinstance(v, str) else json.dumps(v) for k, v in arguments.items()})
        else:
            headers["Content-Type"] = "application/json"
            body = json.dumps(arguments).encode()
        status, text = self.send("GET" if read else "POST", url, headers, body)
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            raise BusError("store_error", f"lake answered HTTP {status} without JSON") from None
        if not isinstance(answer, dict) or not answer.get("ok"):
            error = (answer.get("error") if isinstance(answer, dict) else None) or {}
            raise BusError(error.get("code", "store_error"), error.get("message", f"HTTP {status}"))
        return answer.get("value") or {}

    def receive(self, from_sequence: int, wait_ms: int = WAIT_MS) -> tuple[list[dict[str, Any]], int]:
        value = self.call("receive_agent_tasks", {"agent": self.agent,
                                                  "from_sequence": from_sequence,
                                                  "wait_ms": wait_ms}, read=True)
        return list(value.get("tasks") or []), int(value.get("next_sequence") or from_sequence)

    def claim(self, task_id: str) -> dict[str, Any] | None:
        """The claimed task, or None when another replica took it first."""
        try:
            return self.call("claim_agent_task", {"task_id": task_id, "agent": self.agent},
                             read=False)
        except BusError as error:
            if error.code in ("conflict", "not_found"):
                return None
            raise

    def publish(self, task_id: str, kind: str, message: str = "",
                result: dict[str, Any] | None = None) -> None:
        event: dict[str, Any] = {"task_id": task_id, "agent": self.agent, "type": kind,
                                 "message": message}
        if result is not None:
            event["result"] = result
        self.call("publish_agent_event", event, read=False)


def _log(**fields: Any) -> None:
    print(json.dumps({"bus": True, **fields}, default=str), flush=True)


def turn_body(claimed: dict[str, Any]) -> dict[str, Any]:
    """The claimed task's body, with what Lake's claim reply says of the task as `claim`: its id,
    account and workspace. They come from the reply, never from the body (agent-runtime#25, from agent-ask-orama#42)."""
    claim = {key: claimed.get(key) or "" for key in ("task_id", "customer_id", "workspace_id")}
    return {**(claimed.get("body") or {}), "claim": claim}


def run_task(bus: LakeBus, handle: Callable[[dict[str, Any]], dict[str, Any]],
             claimed: dict[str, Any]) -> dict[str, Any]:
    """One claimed task: the turn runs, and its answer goes back on the bus. Returns the answer."""
    task_id = claimed["task_id"]
    try:
        bus.publish(task_id, "progress", "Working on it")
    except BusError as error:
        _log(task=task_id, progress_not_published=str(error))
    try:
        answer = handle(turn_body(claimed))
    except Exception as error:  # a failed turn is an answer, not a dead worker
        answer = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    if answer.get("ok"):
        bus.publish(task_id, "result", "", answer)
    else:
        bus.publish(task_id, "failed", str(answer.get("error") or "the agent failed"))
    _log(task=task_id, ok=bool(answer.get("ok")))
    return answer


WORKERS = 4
# The most received task ids kept waiting for a worker. A receive answers at most 100; anything
# past this is left on the bus, where lake's recovery loop republishes unclaimed tasks.
MAX_WAITING = 256
# How long a receive waits while tasks are already waiting here for a worker, so a worker that
# frees up meanwhile is not left idle behind a full long poll.
WAITING_WAIT_MS = 1_000


def _count(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def worker_counts() -> tuple[int, int]:
    """(workers, tenant workers) from `AGENT_BUS_WORKERS` (default 4) and
    `AGENT_BUS_TENANT_WORKERS` (default one fewer than the workers, at least 1, at most all)."""
    workers = _count("AGENT_BUS_WORKERS", WORKERS)
    tenant = _count("AGENT_BUS_TENANT_WORKERS", max(1, workers - 1))
    return workers, min(tenant, workers)


def _customer(task: dict[str, Any]) -> str:
    return str(task.get("customer_id") or "")


class _Slots:
    """Worker slots: how many are free, and how many each customer holds."""

    def __init__(self, workers: int, tenant_workers: int) -> None:
        self.changed = threading.Condition()
        self.free = workers
        self.tenant_workers = tenant_workers
        self.held: dict[str, int] = {}

    def wait_for_free(self, stop: threading.Event) -> bool:
        """Block until a worker is free. False once `stop` is set."""
        with self.changed:
            while self.free <= 0 and not stop.is_set():
                self.changed.wait(0.5)
            return self.free > 0 and not stop.is_set()

    def take(self, customer: str) -> bool:
        with self.changed:
            if self.free <= 0 or self.held.get(customer, 0) >= self.tenant_workers:
                return False
            self.free -= 1
            self.held[customer] = self.held.get(customer, 0) + 1
            return True

    def give_back(self, customer: str) -> None:
        with self.changed:
            self.free += 1
            self.held[customer] -= 1
            if not self.held[customer]:
                del self.held[customer]
            self.changed.notify_all()

    def next_task(self, waiting: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """The waiting task to claim next: of the customers below their share, the one holding
        the fewest workers, its oldest task. None when no free worker may take any of them."""
        with self.changed:
            if self.free <= 0:
                return None
            best: dict[str, Any] | None = None
            best_held = 0
            for task in waiting.values():
                held = self.held.get(_customer(task), 0)
                if held < self.tenant_workers and (best is None or held < best_held):
                    best, best_held = task, held
            return best


def work(bus: LakeBus, handle: Callable[[dict[str, Any]], dict[str, Any]],
         stop: threading.Event, workers: int = WORKERS, tenant_workers: int | None = None,
         wait_ms: int = WAIT_MS) -> None:
    """Take tasks until `stop` is set, running up to `workers` at once (agent-runtime#24, from agent-ask-orama#43).

    A task is claimed only when a worker is free to run it, so no claimed task (its body and its
    credential) waits in this process. While every worker is busy nothing more is received; a
    task this replica cannot run yet stays unclaimed on the bus, where another replica may take
    it. One customer holds at most `tenant_workers` workers (default `workers - 1`, at least 1),
    and the next free worker goes to the waiting customer holding the fewest, so one tenant's
    burst never takes every worker from the others. What waits here is the dispatch row alone
    (task id, customer, kind), at most `MAX_WAITING` of them.
    """
    if tenant_workers is None:
        tenant_workers = max(1, workers - 1)
    slots = _Slots(workers, min(max(1, tenant_workers), workers))
    waiting: dict[str, dict[str, Any]] = {}  # task id -> dispatch row, oldest first
    sequence = 1

    def claim_waiting(pool: ThreadPoolExecutor) -> None:
        while not stop.is_set():
            task = slots.next_task(waiting)
            if task is None:
                return
            task_id = task["task_id"]
            del waiting[task_id]
            customer = _customer(task)
            if not slots.take(customer):
                return
            try:
                claimed = bus.claim(task_id)
            except (BusError, OSError) as error:
                _log(task=task_id, claim_failed=str(error))
                claimed = None
            if claimed is None:  # another replica took it, or lake refused: the worker is free
                slots.give_back(customer)
                continue
            try:
                future = pool.submit(run_task, bus, handle, claimed)
            except RuntimeError:
                slots.give_back(customer)
                raise
            future.add_done_callback(lambda _done, customer=customer: slots.give_back(customer))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while slots.wait_for_free(stop):
            claim_waiting(pool)
            if not slots.wait_for_free(stop):
                break
            try:
                tasks, sequence = bus.receive(
                    sequence, min(wait_ms, WAITING_WAIT_MS) if waiting else wait_ms)
            except (BusError, OSError) as error:
                _log(agent=bus.agent, receive_failed=str(error))
                stop.wait(2)
                continue
            for task in tasks:
                task_id = task.get("task_id")
                if task_id and task_id not in waiting and len(waiting) < MAX_WAITING:
                    waiting[task_id] = task
            claim_waiting(pool)


def start_from_env(handle: Callable[[dict[str, Any]], dict[str, Any]],
                   default_agent: str = "") -> threading.Thread | None:
    """Start taking tasks in the background when the operator configured this pod for the bus."""
    lake_url = os.environ.get("LAKE_URL", "")
    token = os.environ.get("AGENT_BUS_TOKEN", "")
    agent = os.environ.get("AGENT_BUS_AGENT", default_agent)
    if not (lake_url and token and agent):
        _log(agent=agent or default_agent, bus="not configured (LAKE_URL, AGENT_BUS_TOKEN)")
        return None
    workers, tenant_workers = worker_counts()
    thread = threading.Thread(target=work, args=(LakeBus(lake_url, token, agent), handle,
                                                 threading.Event()),
                              kwargs={"workers": workers, "tenant_workers": tenant_workers},
                              name="lake-bus", daemon=True)
    thread.start()
    _log(agent=agent, bus="taking tasks", lake=lake_url, workers=workers,
         tenant_workers=tenant_workers)
    return thread
