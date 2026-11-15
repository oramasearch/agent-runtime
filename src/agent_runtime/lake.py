# SPDX-License-Identifier: MIT

"""Everything the runtime reaches, it reaches through lake.

A user-created agent never holds a workspace's admin token. Every call, the workspace engine's
included, is one of Lake's named capabilities (`/v1/<name>`): since lake#674 each engine
capability has a Lake name (`check_pipeline` for the engine's `gate`, `start_run` for `run`, ...),
which Lake authorizes on the customer and audits in the workspace as the agent's token
(agent-runtime#11). Code stored in a lake repository is read through lake's REST surface
(`repository_tree`, `repository_file`).

A failed call is returned as a `Reply` with `ok=False`, never raised: the agent is supposed to
adapt to what came back, and an exception would end the run instead of informing it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Reply:
    ok: bool
    value: Any = None
    error: str = ""
    code: str = ""

    def observation(self) -> str:
        """What the model reads: the value, or the refusal in lake's or Zetta's own words."""
        if self.ok:
            return json.dumps(self.value, default=str)[:20000]
        if self.code == "unknown_capability":
            return f"FAILED: not deployed yet ({self.error})"
        return f"FAILED: {self.error}"


class Lake:
    """lake's REST surface, as the agent's token."""

    def __init__(self, base_url: str, token: str, customer_id: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 120.0,
                 workspace_id: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.customer_id = customer_id
        # The launch's workspace (WORKSPACE_ID). A launch's LAKE_URL already ends in /w/<id>, from
        # which lake takes it too; sent explicitly it must match.
        self.workspace_id = workspace_id
        self.client = httpx.Client(base_url=self.base_url,
                                   headers={"Authorization": f"Bearer {token}"},
                                   timeout=timeout, transport=transport)

    def close(self) -> None:
        self.client.close()

    # -- lake's capabilities -----------------------------------------------------------------

    def openapi(self) -> dict[str, Any]:
        """lake's OpenAPI, filtered to the capabilities this agent credential may use."""
        response = self.client.get("/v1/openapi.json")
        response.raise_for_status()
        return response.json()

    def read(self, capability: str, **arguments: Any) -> Reply:
        try:
            response = self.client.get(f"/v1/{capability}", params=arguments)
        except httpx.HTTPError as error:
            return Reply(False, error=f"{type(error).__name__}: {error}")
        return envelope(response)

    def capability(self, capability: str, arguments: dict[str, Any], method: str,
                   fixed: list[str] | None = None) -> Reply:
        """Call one lake capability through its audited registry route, as this run's customer.

        `fixed` names the fields the capability declares that the run fills itself (customer_id,
        workspace_id); None means customer_id alone, as every App and build capability takes.
        """
        path = f"/v1/{capability}"
        values = {"customer_id": self.customer_id, "workspace_id": self.workspace_id}
        scoped = dict(arguments)
        for field in (["customer_id"] if fixed is None else fixed):
            if values.get(field):
                scoped[field] = values[field]
        try:
            if method == "get":
                params = {
                    key: value if isinstance(value, str) else json.dumps(value)
                    for key, value in scoped.items()
                    if value is not None
                }
                response = self.client.get(path, params=params)
            else:
                response = self.client.post(path, json=scoped)
        except httpx.HTTPError as error:
            return Reply(False, error=f"{type(error).__name__}: {error}")
        return envelope(response)

    def get_agent(self, agent_id: str) -> Reply:
        """The agent's record, from its workspace's engine through Lake's `get_agent`."""
        return self.capability("get_agent", {"id": agent_id}, "get",
                               ["customer_id", "workspace_id"])

    def repository_files(self, repository_id: str, revision: str) -> dict[str, str]:
        """Every file of a lake repository at a pinned revision, path to text."""
        tree = self.read("repository_tree", id=repository_id, ref=revision)
        if not tree.ok:
            raise LoadError(f"cannot read repository {repository_id} at {revision}: {tree.error}")
        files: dict[str, str] = {}
        for item in tree.value.get("items", []):
            if item.get("kind") not in (None, "file", "blob"):
                continue
            path = item.get("path", "")
            got = self.read("repository_file", id=repository_id, ref=revision, path=path)
            if not got.ok:
                raise LoadError(f"cannot read {path} at {revision}: {got.error}")
            files[path] = got.value.get("contents", "")
        return files


class LoadError(Exception):
    """The agent cannot be loaded; the run fails before any model call."""


def envelope(response: httpx.Response) -> Reply:
    """lake and Zetta answer `{ok, value}` or `{ok: false, error: {code, message}}`."""
    try:
        body = response.json()
    except ValueError:
        return Reply(False, error=f"HTTP {response.status_code} without JSON")
    if isinstance(body, dict) and body.get("ok"):
        return Reply(True, body.get("value"))
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code", response.status_code))
        return Reply(False, error=f"{code}: {error.get('message', '')}", code=code)
    return Reply(False, error=f"HTTP {response.status_code}")
