"""The service's conversation memory, bounded (agent-runtime#25, LSR-076; ported from agent-ask-orama#44).

A service-mode agent (Agent Creator, and every agent a person runs as a service) kept its
conversations in a plain dict that only grew: every conversation ever started stayed in memory for
the life of the pod, whole. `Conversations` is the same mapping (key to the list of model messages a turn
continues), with four limits:

- **idle**: a conversation untouched for `idle_seconds` is dropped;
- **turns**: a conversation keeps its last `max_turns` turns (a turn runs from a person's message
  to the next one), dropping the oldest whole turns;
- **bytes per conversation**: oldest whole turns are dropped until it is under `max_bytes`;
- **conversations and total bytes**: the least recently used conversations are dropped until there
  are at most `max_conversations` and they hold at most `max_total_bytes` together.

The system message (first, when present) is kept while any of the conversation is. Whole turns are
dropped, never part of one, so an assistant's tool calls always keep their tool answers. Sizes are
the JSON length of each message, measured once (lists only grow by appends between accesses).

The key is whatever the caller keys a conversation by; this module never looks inside it. Limits
are applied whenever a conversation is read or stored. Standard library only.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator, MutableMapping
from typing import Any

Message = dict[str, Any]

MAX_CONVERSATIONS = 256
MAX_TURNS = 20
MAX_BYTES = 1024 * 1024  # one conversation
MAX_TOTAL_BYTES = 64 * 1024 * 1024  # every conversation together
IDLE_SECONDS = 3600


def message_bytes(message: Message) -> int:
    return len(json.dumps(message, default=str))


class _Entry:
    __slots__ = ("history", "used", "measured", "size")

    def __init__(self, history: list[Message], used: float) -> None:
        self.history = history
        self.used = used
        self.measured = 0  # how many messages `size` counts
        self.size = 0

    def bytes(self) -> int:
        """The conversation's size now, counting only the messages added since last time."""
        if len(self.history) < self.measured:  # shortened from outside: count again
            self.measured, self.size = 0, 0
        for message in self.history[self.measured:]:
            self.size += message_bytes(message)
        self.measured = len(self.history)
        return self.size


def _turn_starts(history: list[Message]) -> list[int]:
    return [i for i, message in enumerate(history) if message.get("role") == "user"]


def trim(history: list[Message], max_turns: int, max_bytes: int) -> None:
    """Drop the oldest whole turns, in place, until at most `max_turns` remain and the history is
    under `max_bytes` (or only its system message is left)."""
    starts = _turn_starts(history)
    if not starts:
        return
    first = starts[0]  # everything before the first turn is the system message
    sizes = [message_bytes(message) for message in history]
    total = sum(sizes)
    drop = 0  # how many of the oldest turns to drop
    while drop < len(starts) and (len(starts) - drop > max_turns or total > max_bytes):
        end = starts[drop + 1] if drop + 1 < len(starts) else len(history)
        total -= sum(sizes[starts[drop]:end])
        drop += 1
    if drop:
        end = starts[drop] if drop < len(starts) else len(history)
        del history[first:end]


def _setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


class Conversations(MutableMapping):
    """A bounded mapping of conversation key to its messages. Thread-safe."""

    def __init__(self, max_conversations: int = MAX_CONVERSATIONS, max_turns: int = MAX_TURNS,
                 max_bytes: int = MAX_BYTES, max_total_bytes: int = MAX_TOTAL_BYTES,
                 idle_seconds: float = IDLE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_conversations = max_conversations
        self.max_turns = max_turns
        self.max_bytes = max_bytes
        self.max_total_bytes = max_total_bytes
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()  # least recently used first

    @classmethod
    def from_env(cls) -> Conversations:
        """The limits from `AGENT_CONVERSATIONS`, `AGENT_CONVERSATION_TURNS`,
        `AGENT_CONVERSATION_BYTES`, `AGENT_CONVERSATIONS_BYTES` and
        `AGENT_CONVERSATION_IDLE_SECONDS`; a missing or invalid value is the default."""
        return cls(
            max_conversations=_setting("AGENT_CONVERSATIONS", MAX_CONVERSATIONS),
            max_turns=_setting("AGENT_CONVERSATION_TURNS", MAX_TURNS),
            max_bytes=_setting("AGENT_CONVERSATION_BYTES", MAX_BYTES),
            max_total_bytes=_setting("AGENT_CONVERSATIONS_BYTES", MAX_TOTAL_BYTES),
            idle_seconds=_setting("AGENT_CONVERSATION_IDLE_SECONDS", IDLE_SECONDS))

    def __getitem__(self, key: Hashable) -> list[Message]:
        with self._lock:
            self._expire()
            entry = self._entries[key]
            self._use(key, entry)
            return entry.history

    def __setitem__(self, key: Hashable, history: list[Message]) -> None:
        with self._lock:
            self._expire()
            entry = _Entry(history, self.clock())
            self._entries[key] = entry
            self._use(key, entry)

    def __delitem__(self, key: Hashable) -> None:
        with self._lock:
            del self._entries[key]

    def __iter__(self) -> Iterator[Hashable]:
        with self._lock:
            self._expire()
            return iter(list(self._entries))

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._entries)

    def total_bytes(self) -> int:
        with self._lock:
            return sum(entry.bytes() for entry in self._entries.values())

    def _use(self, key: Hashable, entry: _Entry) -> None:
        """`key` was just read or stored: most recently used, trimmed, then the others evicted
        until every limit holds."""
        entry.used = self.clock()
        self._entries.move_to_end(key)
        trim(entry.history, self.max_turns, self.max_bytes)
        total = sum(other.bytes() for other in self._entries.values())
        while len(self._entries) > 1 and (len(self._entries) > self.max_conversations
                                          or total > self.max_total_bytes):
            _, oldest = self._entries.popitem(last=False)
            total -= oldest.size

    def _expire(self) -> None:
        cutoff = self.clock() - self.idle_seconds
        while self._entries:
            key, oldest = next(iter(self._entries.items()))
            if oldest.used > cutoff:
                return
            del self._entries[key]
