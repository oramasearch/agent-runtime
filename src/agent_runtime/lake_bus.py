"""Take tasks from lake's agent bus and answer them (lake docs/api/agent-bus.md, lake#269).

Lake publishes each turn a person asks of this agent on Zetta pub/sub (topic
`agent-tasks.<agent>` on lake's own Zetta). The agent never holds the bus's token: it reaches the
bus through lake, with its own bus credential (`AGENT_BUS_TOKEN`, minted by the Orama operator for
this agent alone):

1. `receive_agent_tasks` long-polls for new tasks;
2. `claim_agent_task` takes one (exactly one claim wins), `credentials: by_reference` (lake#871):
   Lake answers the turn's body with no credential in it, and the turn calls Lake with this
   agent's bus credential and the header `X-Lake-Agent-Task: <task id>`, which Lake honours only
   while the task is claimed and Running on this agent (`task_credentials`). A Lake from before
   lake#871 refuses the field; the claim is then asked again without it and the body carries the
   task's own credential, as before;
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
        self.message = message


# Task credentials by reference (lake#871, lake docs/api/agent-bus.md, "Task credentials by
# reference"): a claim asks Lake to keep the task's credential, so this process, which serves every
# tenant, never holds one. Each call made for the task carries this agent's bus credential and
# TASK_HEADER, naming the task; Lake acts as the task only while it is claimed and Running here.
TASK_HEADER = "X-Lake-Agent-Task"
BY_REFERENCE = "by_reference"
# The body fields Lake fills with the task's credential ("{{credential}}" at dispatch).
CREDENTIAL_FIELDS = ("lake_token", "zetta_token")


def refuses_by_reference(error: BusError) -> bool:
    """A Lake from before lake#871 refuses the claim's `credentials` field as unexpected, before
    it claims anything, so the claim can be asked again without it."""
    return error.code == "invalid_input" and "credentials" in error.message


def task_credentials(token: str, claimed: dict[str, Any]) -> dict[str, Any]:
    """The claimed task's body, with the credential its calls to Lake use.

    Claimed by reference, Lake answered every credential field empty: they are filled with this
    agent's bus credential (`token`), and `lake_headers` holds `X-Lake-Agent-Task: <task id>`,
    which every call made for the task sends with it. Claimed with the credential in the body (a
    Lake from before lake#871), the body is used as it came, as before.
    """
    body = dict(claimed.get("body") or {})
    if claimed.get("credentials") != BY_REFERENCE:
        return body
    for name in CREDENTIAL_FIELDS:
        if name in body:
            body[name] = token
    body["lake_headers"] = {TASK_HEADER: str(claimed["task_id"])}
    return body


def task_headers(body: dict[str, Any]) -> dict[str, str]:
    """The headers a turn sends with its credential on every call to Lake: `X-Lake-Agent-Task`
    for a task claimed by reference (`task_credentials`), none for any other turn."""
    headers = body.get("lake_headers")
    if not isinstance(headers, dict):
        return {}
    value = headers.get(TASK_HEADER)
    return {TASK_HEADER: value} if isinstance(value, str) and value else {}


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
        """The claimed task, or None when another replica took it first. Asked by reference
        (lake#871); a Lake that refuses that is asked again for the credential in the body."""
        arguments = {"task_id": task_id, "agent": self.agent}
        try:
            try:
                return self.call("claim_agent_task", {**arguments, "credentials": BY_REFERENCE},
                                 read=False)
            except BusError as error:
                if not refuses_by_reference(error):
                    raise
            return self.call("claim_agent_task", arguments, read=False)
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


def run_task(bus: LakeBus, handle: Callable[[dict[str, Any]], dict[str, Any]],
             claimed: dict[str, Any]) -> dict[str, Any]:
    """One claimed task: the turn runs, and its answer goes back on the bus. Returns the answer."""
    task_id = claimed["task_id"]
    try:
        bus.publish(task_id, "progress", "Working on it")
    except BusError as error:
        _log(task=task_id, progress_not_published=str(error))
    try:
        answer = handle(task_credentials(bus.token, claimed))
    except Exception as error:  # a failed turn is an answer, not a dead worker
        answer = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    if answer.get("ok"):
        bus.publish(task_id, "result", "", answer)
    else:
        bus.publish(task_id, "failed", str(answer.get("error") or "the agent failed"))
    _log(task=task_id, ok=bool(answer.get("ok")))
    return answer


def work(bus: LakeBus, handle: Callable[[dict[str, Any]], dict[str, Any]],
         stop: threading.Event, workers: int = 4) -> None:
    """Take tasks until `stop` is set, running up to `workers` at once."""
    sequence = 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while not stop.is_set():
            try:
                tasks, sequence = bus.receive(sequence)
            except (BusError, OSError) as error:
                _log(agent=bus.agent, receive_failed=str(error))
                stop.wait(2)
                continue
            for task in tasks:
                try:
                    claimed = bus.claim(task["task_id"])
                except (BusError, OSError) as error:
                    _log(task=task.get("task_id"), claim_failed=str(error))
                    continue
                if claimed is not None:
                    pool.submit(run_task, bus, handle, claimed)


def start_from_env(handle: Callable[[dict[str, Any]], dict[str, Any]],
                   default_agent: str = "") -> threading.Thread | None:
    """Start taking tasks in the background when the operator configured this pod for the bus."""
    lake_url = os.environ.get("LAKE_URL", "")
    token = os.environ.get("AGENT_BUS_TOKEN", "")
    agent = os.environ.get("AGENT_BUS_AGENT", default_agent)
    if not (lake_url and token and agent):
        _log(agent=agent or default_agent, bus="not configured (LAKE_URL, AGENT_BUS_TOKEN)")
        return None
    thread = threading.Thread(target=work, args=(LakeBus(lake_url, token, agent), handle,
                                                 threading.Event()),
                              name="lake-bus", daemon=True)
    thread.start()
    _log(agent=agent, bus="taking tasks", lake=lake_url)
    return thread
