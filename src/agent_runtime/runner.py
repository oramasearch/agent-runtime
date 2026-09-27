"""The two modes, and the trace every run leaves.

**job** runs one task and exits: the input is `AGENT_INPUT` (JSON), the result goes to stdout and
a short form to `/dev/termination-log` (the operator reads it back into the launch's status), and
the exit code says how it went: 0 finished, 2 finished without an answer, 1 could not run.

**service** stays up and answers turns: `POST /v1/turn` with a conversation id and a message,
`GET /healthz`. Each conversation's messages are kept in memory, so a turn continues the one before
it.

Configuration is the environment the operator sets for a launch (orama-operator#185): `LAKE_URL`,
`LAKE_TOKEN` (the agent's lake key), `CUSTOMER_ID`, `AGENT_ID`, `AGENT_MODE`, `AGENT_INPUT`,
`OPENAI_API_KEY` (the general Orama key), `AGENT_MODEL`, `TRACE_DIR`, `PORT` and
`AGENT_SERVICE_TOKEN`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from openai import OpenAI

from agent_runtime import definition
from agent_runtime.lake import Lake, LoadError
from agent_runtime.loop import Context, Result, run_entrypoint, run_loop, system_message
from agent_runtime.tools import Tool, all_tools

DEFAULT_MODEL = "gpt-5.5"


@dataclass
class Loaded:
    agent: definition.Agent
    tools: list[Tool]
    model: str


def load(lake: Lake, agent_id: str, record: dict[str, Any] | None = None) -> Loaded:
    """The agent's record, its definition, and its tools. Raises LoadError before any model call."""
    if record is None:
        got = lake.get_agent(agent_id)
        if not got.ok:
            raise LoadError(f"cannot read agent {agent_id}: {got.error}")
        record = got.value
    agent = definition.from_record(record, lake)
    try:
        openapi = lake.zetta_openapi()
    except Exception as error:  # the workspace's tools are unavailable, the run still reports it
        raise LoadError(f"cannot read the workspace's tools: {error}") from error
    tools = all_tools(agent, lake, openapi)
    return Loaded(agent, tools, agent.model or os.environ.get("AGENT_MODEL", DEFAULT_MODEL))


def run_once(loaded: Loaded, task: Any, client: OpenAI) -> Result:
    if loaded.agent.entrypoint:
        return run_entrypoint(loaded.agent,
                              Context(loaded.agent, task, loaded.tools, client, loaded.model))
    text = task if isinstance(task, str) else json.dumps(task)
    messages = [system_message(loaded.agent), {"role": "user", "content": text}]
    return run_loop(loaded.agent, loaded.tools, messages, client, loaded.model)


def record_trace(trace_dir: Path, agent: definition.Agent | None, mode: str, task: Any,
                 result: dict[str, Any]) -> str:
    """Every run, including one that failed, is recorded. It never carries a credential."""
    trace_id = str(uuid.uuid4())
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / f"{trace_id}.json").write_text(json.dumps(
        {"trace_id": trace_id, "agent": agent.id if agent else os.environ.get("AGENT_ID", ""),
         "name": agent.name if agent else "", "mode": mode,
         "at": datetime.now(timezone.utc).isoformat(), "input": task, "result": result},
        indent=1, default=str))
    return trace_id


def termination_summary(summary: dict[str, Any], path: Path = Path("/dev/termination-log")) -> None:
    """At most 4 KiB, which is what Kubernetes keeps; the whole result is in the trace."""
    try:
        with path.open("w") as log:
            log.write(json.dumps(summary, default=str)[:4000])
    except OSError:
        pass


def lake_from_environment() -> Lake:
    return Lake(os.environ["LAKE_URL"], os.environ["LAKE_TOKEN"], os.environ["CUSTOMER_ID"])


def job(record: dict[str, Any] | None = None) -> int:
    trace_dir = Path(os.environ.get("TRACE_DIR", "/tmp/agent-runtime-traces"))
    task = json.loads(os.environ.get("AGENT_INPUT") or "null")
    lake = lake_from_environment()
    loaded: Loaded | None = None
    try:
        loaded = load(lake, os.environ.get("AGENT_ID", ""), record)
        result = run_once(loaded, task, OpenAI()).as_dict()
        code = 0 if result["finished"] and result["reply"] else 2
    except LoadError as error:
        result, code = {"error": str(error)}, 1
    except Exception as error:  # the run failed; say so rather than die silently
        result, code = {"error": f"{type(error).__name__}: {error}"}, 1
    finally:
        lake.close()
    trace_id = record_trace(trace_dir, loaded.agent if loaded else None, "job", task, result)
    summary = {"trace_id": trace_id, "exit": code, "reply": result.get("reply", ""),
               "error": result.get("error", ""), "calls": len(result.get("calls", []))}
    termination_summary(summary)
    print(json.dumps({**summary, "result": result}, default=str), flush=True)
    return code


class Conversations:
    """Each conversation's messages, so a turn continues the one before it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.messages: dict[str, list[dict[str, Any]]] = {}

    def history(self, conversation_id: str, loaded: Loaded) -> list[dict[str, Any]]:
        with self.lock:
            return self.messages.setdefault(conversation_id, [system_message(loaded.agent)])


def answer(loaded: Loaded, history: list[dict[str, Any]], message: str, turn: dict[str, Any],
           client: OpenAI) -> dict[str, Any]:
    """One turn: the definition's own entrypoint when it has one, else the tool loop."""
    if loaded.agent.entrypoint:
        context = Context(loaded.agent, {"message": message, "history": list(history),
                                         "page": turn.get("page") or {},
                                         "customer_id": turn.get("customer_id") or ""},
                          loaded.tools, client, loaded.model)
        result = run_entrypoint(loaded.agent, context).as_dict()
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": result["reply"]})
        return result
    history.append({"role": "user", "content": message})
    return run_loop(loaded.agent, loaded.tools, history, client, loaded.model).as_dict()


def per_turn(loaded: Loaded, turn: dict[str, Any]) -> tuple[Loaded, Lake | None]:
    """A lake-wide agent (AGENT_PER_TURN_CREDENTIALS=1) works for whoever sent the turn: lake
    hands over that person's key and space, and the tools are derived from their workspace."""
    if os.environ.get("AGENT_PER_TURN_CREDENTIALS") != "1":
        return loaded, None
    token, customer = turn.get("lake_token") or "", turn.get("customer_id") or ""
    if not token or not customer:
        raise LoadError("this agent works per turn: the turn needs lake_token and customer_id")
    lake = Lake(os.environ["LAKE_URL"], token, customer)
    try:
        openapi = lake.zetta_openapi()
    except Exception:  # a space without a ready workspace still gets an answer
        openapi = {"paths": {}}
    return Loaded(loaded.agent, all_tools(loaded.agent, lake, openapi), loaded.model), lake


def serve(record: dict[str, Any] | None = None, client: OpenAI | None = None) -> int:
    trace_dir = Path(os.environ.get("TRACE_DIR", "/tmp/agent-runtime-traces"))
    token = os.environ.get("AGENT_SERVICE_TOKEN", "")
    if os.environ.get("AGENT_PER_TURN_CREDENTIALS") == "1":
        loaded = Loaded(definition.from_record(record or {}), [],
                        (record or {}).get("model_name")
                        or os.environ.get("AGENT_MODEL", DEFAULT_MODEL))
    else:
        lake = lake_from_environment()
        try:
            loaded = load(lake, os.environ.get("AGENT_ID", ""), record)
        except LoadError as error:
            print(json.dumps({"agent-runtime": "cannot load", "error": str(error)}), flush=True)
            return 1
    conversations = Conversations()
    model_client = client or OpenAI()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
            if self.path == "/healthz":
                self._send(200, {"ok": True, "agent": loaded.agent.id, "name": loaded.agent.name,
                                 "tools": [tool.name for tool in loaded.tools],
                                 "model": loaded.model})
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/turn":
                self._send(404, {"ok": False, "error": "not found"})
                return
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                turn = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            except (json.JSONDecodeError, ValueError):
                self._send(400, {"ok": False, "error": "the body is not JSON"})
                return
            message = (turn or {}).get("message") or (turn or {}).get("request")
            if not message:
                self._send(400, {"ok": False, "error": "a turn needs a message"})
                return
            conversation = (turn or {}).get("conversation_id") or str(uuid.uuid4())
            history = conversations.history(conversation, loaded)
            turn_lake = None
            try:
                turn_loaded, turn_lake = per_turn(loaded, turn or {})
                result = answer(turn_loaded, history, message, turn or {}, model_client)
            except Exception as error:
                result = {"error": f"{type(error).__name__}: {error}"}
            finally:
                if turn_lake is not None:
                    turn_lake.close()
            trace_id = record_trace(trace_dir, loaded.agent, "service", message, result)
            ok = "error" not in result
            self._send(200 if ok else 422, {"ok": ok, "conversation_id": conversation,
                                            "trace_id": trace_id, **result})

        def log_message(self, format: str, *args: Any) -> None:
            print(json.dumps({"agent-runtime": loaded.agent.id, "request": format % args}),
                  flush=True)

    port = int(os.environ.get("PORT", "8080"))
    print(json.dumps({"agent-runtime": loaded.agent.id, "listening": port,
                      "tools": [tool.name for tool in loaded.tools]}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a user-created agent from lake.")
    parser.add_argument("--mode", choices=["job", "service"],
                        default=os.environ.get("AGENT_MODE", "job"))
    parser.add_argument("--agent-id", default=None, help="overrides AGENT_ID")
    parser.add_argument("--record", type=Path, default=None,
                        help="an agent record as JSON, instead of reading it from the workspace")
    parser.add_argument("--definition-dir", type=Path,
                        default=Path(os.environ["AGENT_DEFINITION_DIR"])
                        if os.environ.get("AGENT_DEFINITION_DIR") else None,
                        help="a definition built into the image (record.json beside its files)")
    args = parser.parse_args()
    if args.agent_id:
        os.environ["AGENT_ID"] = args.agent_id
    record = json.loads(args.record.read_text()) if args.record else None
    if args.definition_dir:
        record = definition.from_directory(args.definition_dir)
    return serve(record) if args.mode == "service" else job(record)


if __name__ == "__main__":
    sys.exit(main())
