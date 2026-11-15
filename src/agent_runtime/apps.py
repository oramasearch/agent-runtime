# SPDX-License-Identifier: MIT

"""App and build capability context for one custom-agent turn."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_runtime.tools import Tool

APP_READ_CAPABILITIES = frozenset({
    "list_apps",
    "get_app",
    "app_logs",
    "receive_app_events",
    "list_builds",
    "get_build",
    "build_logs",
    "receive_build_events",
})

APP_ACTION_CAPABILITIES = frozenset({
    "create_app",
    "deploy_app",
    "rollback_app",
    "delete_app",
    "set_app_env",
    "unset_app_env",
    "set_app_auth",
    "restart_app",
    "stop_app",
    "start_app",
    "scale_app",
    "set_app_zetta_binding",
    "bind_service_to_app",
    "unbind_service_from_app",
    "create_service_and_bind_to_app",
    "build_image",
    "build_app",
    "cancel_build",
})

APP_CAPABILITIES = APP_READ_CAPABILITIES | APP_ACTION_CAPABILITIES
CONTEXT_PREFIX = "## Current lake App and build capabilities"


@dataclass
class AppContext:
    """The App and build capabilities lake grants for this turn."""

    reads: list[str]
    proposals: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "reads": self.reads,
            "proposals": self.proposals,
            "application_agent": {
                "agent": "agent-application",
                "kind": "app",
                "role": "builds and changes Apps after a person reviews its proposal",
            },
        }

    def message(self) -> dict[str, str]:
        guidance = (
            "Read tools call lake immediately through its customer-scoped, audited registry. "
            "A tool listed under proposals never performs the change: it records the exact "
            "capability and input in the turn result for a person to confirm. After calling one, "
            "say proposed, not completed. The ApplicationAgent (agent-application, turn kind "
            "app) is the specialist that builds and changes Apps; its first turn also returns a "
            "proposal, and lake grants writes only to the later confirmed turn."
        )
        payload = json.dumps(self.as_dict(), default=str)
        return {
            "role": "system",
            "content": f"{CONTEXT_PREFIX}\n\n{guidance}\n\n{payload}",
        }


def app_context(tools: list[Tool]) -> AppContext:
    """Describe only App and build tools granted for the current turn."""
    reads = sorted(
        tool.name
        for tool in tools
        if tool.name in APP_READ_CAPABILITIES and not tool.proposal
    )
    proposals = sorted(
        tool.name
        for tool in tools
        if tool.name in APP_ACTION_CAPABILITIES and tool.proposal
    )
    return AppContext(reads, proposals)


def replace_context(messages: list[dict[str, Any]], context: AppContext) -> None:
    """Keep only this turn's App and build grants in a continuing conversation."""
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and str(message.get("content", "")).startswith(CONTEXT_PREFIX)
        )
    ]
    messages.append(context.message())
