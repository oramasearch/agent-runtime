# SPDX-License-Identifier: MIT

"""agent-runtime#25 (LSR-076, ported from agent-ask-orama#44): a service-mode agent's
conversation memory is bounded by count, turns, bytes and idle time.
"""

from __future__ import annotations

import pytest

from agent_runtime.conversations import Conversations, message_bytes, trim


def system() -> dict:
    return {"role": "system", "content": "prompt"}


def one_turn(n: int, size: int = 10) -> list[dict]:
    """A person's message, a tool call and its answer, then the reply: one whole turn."""
    return [{"role": "user", "content": f"q{n}" + "x" * size},
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}"}]},
            {"role": "tool", "tool_call_id": f"c{n}", "content": "r" * size},
            {"role": "assistant", "content": f"a{n}"}]


def conversation(turns: int, size: int = 10) -> list[dict]:
    history = [system()]
    for n in range(turns):
        history += one_turn(n, size)
    return history


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_oldest_whole_turns_go_first_and_the_system_message_stays():
    history = conversation(5)
    trim(history, max_turns=2, max_bytes=10**9)
    assert history == [system()] + one_turn(3) + one_turn(4)


def test_a_conversation_is_cut_to_its_byte_budget_by_whole_turns():
    history = conversation(6, size=1000)
    budget = sum(message_bytes(m) for m in [system()] + one_turn(0, 1000) * 2)
    trim(history, max_turns=100, max_bytes=budget)
    assert history == [system()] + one_turn(4, 1000) + one_turn(5, 1000)
    tool_ids = [m["tool_call_id"] for m in history if m["role"] == "tool"]
    assert tool_ids == ["c4", "c5"], "a tool answer never loses its call"


def test_a_single_turn_over_the_budget_leaves_only_the_system_message():
    history = conversation(1, size=5000)
    trim(history, max_turns=100, max_bytes=1000)
    assert history == [system()]


def test_the_least_recently_used_conversation_is_dropped_past_the_count():
    memory = Conversations(max_conversations=3)
    for key in "abc":
        memory[key] = conversation(1)
    memory["a"]  # used: b is now the oldest
    memory["d"] = conversation(1)
    assert sorted(memory) == ["a", "c", "d"]


def test_conversations_are_dropped_past_the_total_bytes():
    one = sum(message_bytes(m) for m in conversation(1, 1000))
    memory = Conversations(max_total_bytes=3 * one)
    for key in "abcde":
        memory[key] = conversation(1, 1000)
    assert sorted(memory) == ["c", "d", "e"]
    assert memory.total_bytes() <= 3 * one


def test_growth_after_a_conversation_was_handed_out_is_counted():
    one = sum(message_bytes(m) for m in conversation(1, 1000))
    memory = Conversations(max_total_bytes=3 * one, max_turns=100)
    memory["a"] = conversation(1, 1000)
    memory["a"].extend(one_turn(1, 1000) + one_turn(2, 1000))  # a turn appends in place
    memory["b"] = conversation(1, 1000)
    assert "a" not in memory, "a grew to three turns; with b it is over the total"


def test_an_idle_conversation_expires():
    clock = Clock()
    memory = Conversations(idle_seconds=60, clock=clock)
    memory["old"] = conversation(1)
    clock.now += 30
    memory["new"] = conversation(1)
    clock.now += 31
    assert "old" not in memory and "new" in memory
    clock.now += 60
    assert len(memory) == 0


def test_setdefault_and_moving_a_conversation_to_a_new_key_work():
    """The mapping is used as a dict is: `setdefault` (by id), or popped and stored again under a
    new key (agent-ask-orama#42 keys a conversation by the task that last continued it)."""
    memory = Conversations(max_turns=2)
    history = memory.setdefault("t1", [])
    history += conversation(3)
    moved = memory.pop("t1", [])
    memory["t2"] = moved
    assert "t1" not in memory and memory["t2"] is moved
    assert moved == [system()] + one_turn(1) + one_turn(2), "trimmed when stored"


def test_the_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_CONVERSATIONS", "7")
    monkeypatch.setenv("AGENT_CONVERSATION_TURNS", "3")
    monkeypatch.setenv("AGENT_CONVERSATION_BYTES", "4096")
    monkeypatch.setenv("AGENT_CONVERSATIONS_BYTES", "65536")
    monkeypatch.setenv("AGENT_CONVERSATION_IDLE_SECONDS", "120")
    memory = Conversations.from_env()
    assert (memory.max_conversations, memory.max_turns, memory.max_bytes,
            memory.max_total_bytes, memory.idle_seconds) == (7, 3, 4096, 65536, 120)
    monkeypatch.setenv("AGENT_CONVERSATIONS", "-1")
    monkeypatch.setenv("AGENT_CONVERSATION_TURNS", "lots")
    memory = Conversations.from_env()
    assert memory.max_conversations == 256 and memory.max_turns == 20

