"""The run: a tool-calling loop on the OpenAI SDK, or the definition's own entrypoint.

The default loop is the one every form-built agent gets: the instructions as the system prompt,
the input as the first message, and tool calls answered with what actually came back until the
model answers without calling anything. A definition with an `entrypoint` (`module:function`)
runs its own code instead, handed a `Context` with the same tools and model.
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from agent_runtime.definition import Agent
from agent_runtime.tools import Tool

MAX_ROUNDS = 12

PREAMBLE = """

---
You are a user-created agent running in lake. Everything you know about the customer's data comes
from your tools: never state a number, a table or a column you have not seen in a tool result. A
tool that fails tells you something; say what failed rather than guessing past it. Your work
area is {area}."""


@dataclass
class Result:
    reply: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    finished: bool = False
    # A structured answer beside the words (agent-creator#1): what an API caller reads without
    # parsing the reply. None for an agent that only talks.
    result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"reply": self.reply, "calls": self.calls, "rounds": self.rounds,
               "finished": self.finished}
        if self.result is not None:
            out["result"] = self.result
        return out


def area_text(agent: Agent) -> str:
    area = agent.work_area or {}
    kind, detail = area.get("kind", "workspace"), area.get("detail", "")
    if kind == "project":
        return f"the project '{detail}'"
    if kind == "dataset":
        return f"the datasets {detail}"
    return "the whole workspace"


def system_message(agent: Agent) -> dict[str, str]:
    return {"role": "system", "content": agent.system_prompt() + PREAMBLE.format(
        area=area_text(agent))}


def run_loop(agent: Agent, tools: list[Tool], messages: list[dict[str, Any]], client: OpenAI,
             model: str, max_rounds: int = MAX_ROUNDS) -> Result:
    """Continue `messages` (system first) until the model answers. Appends to `messages`."""
    by_name = {tool.name: tool for tool in tools}
    result = Result()
    for round_number in range(1, max_rounds + 1):
        result.rounds = round_number
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = [tool.spec() for tool in tools]
        message = client.chat.completions.create(**kwargs).choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            result.reply = message.content or ""
            result.finished = True
            return result
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool = by_name.get(call.function.name)
            reply = (tool.call(arguments) if tool is not None else
                     _unknown(call.function.name))
            result.calls.append({"tool": call.function.name, "arguments": arguments,
                                 "ok": reply.ok, "observation": reply.value if reply.ok else None,
                                 "error": reply.error or None})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": reply.observation()})
    result.reply = "I stopped before finishing: the run reached its limit of tool rounds."
    return result


def _unknown(name: str):
    from agent_runtime.lake import Reply
    return Reply(False, error=f"'{name}' is not one of this agent's tools")


@dataclass
class Context:
    """What a definition's own entrypoint is handed."""

    agent: Agent
    input: Any
    tools: list[Tool]
    client: OpenAI
    model: str

    def call_tool(self, name: str, arguments: dict[str, Any]):
        for tool in self.tools:
            if tool.name == name:
                return tool.call(arguments)
        return _unknown(name)

    def complete(self, messages: list[dict[str, Any]]) -> Result:
        return run_loop(self.agent, self.tools, messages, self.client, self.model)


def import_from(directory, module_name: str):
    """Import a definition's module from its own directory, never a cached one.

    Definitions share names (`agent.main`, `agent.tools`), so a module another definition loaded
    earlier in the same process must not answer for this one.
    """
    package = module_name.split(".", 1)[0]
    for loaded in [name for name in sys.modules if name == package or
                   name.startswith(package + ".")]:
        del sys.modules[loaded]
    sys.path.insert(0, str(directory))
    try:
        return importlib.import_module(module_name)
    finally:
        sys.path.remove(str(directory))


def run_entrypoint(agent: Agent, context: Context) -> Result:
    module_name, _, function_name = agent.entrypoint.partition(":")
    function = getattr(import_from(agent.directory, module_name), function_name or "run")
    answer = function(context)
    if isinstance(answer, Result):
        return answer
    # {"reply": words, "result": {...}}: a person reads the reply, a caller the result.
    if isinstance(answer, dict) and isinstance(answer.get("reply"), str):
        result = answer.get("result")
        return Result(reply=answer["reply"], finished=True,
                      result=result if isinstance(result, dict) else None)
    return Result(reply=answer if isinstance(answer, str) else json.dumps(answer, default=str),
                  finished=True)
