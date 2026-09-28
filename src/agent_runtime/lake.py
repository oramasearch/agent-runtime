"""Everything the runtime reaches, it reaches through lake.

A user-created agent never holds a workspace's admin token. Its Zetta calls go to lake's
Zetta-compatible path for the customer's workspace (`/workspaces/<customer>/zetta/v1/`,
lake#235), which applies lake's allow-lists, authorizes the call on the customer and audits it as
the agent's token. Code stored in a lake repository is read through lake's REST surface
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
                 transport: httpx.BaseTransport | None = None, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.customer_id = customer_id
        self.client = httpx.Client(base_url=self.base_url,
                                   headers={"Authorization": f"Bearer {token}"},
                                   timeout=timeout, transport=transport)

    def close(self) -> None:
        self.client.close()

    # -- the workspace's Zetta, through lake -------------------------------------------------

    @property
    def zetta_base(self) -> str:
        return f"/workspaces/{self.customer_id}/zetta"

    def zetta_openapi(self) -> dict[str, Any]:
        """The workspace's OpenAPI, already reduced by lake to what may be called."""
        response = self.client.get(f"{self.zetta_base}/v1/openapi.json")
        response.raise_for_status()
        return response.json()

    def openapi(self) -> dict[str, Any]:
        """lake's OpenAPI, filtered to the capabilities this agent credential may use."""
        response = self.client.get("/v1/openapi.json")
        response.raise_for_status()
        return response.json()

    def zetta(self, capability: str, arguments: dict[str, Any], method: str) -> Reply:
        """GET /v1/<capability>?<args> for a read, POST with a JSON body for an action."""
        path = f"{self.zetta_base}/v1/{capability}"
        try:
            if method == "get":
                params = {key: value if isinstance(value, str) else json.dumps(value)
                          for key, value in arguments.items() if value is not None}
                response = self.client.get(path, params=params)
            else:
                response = self.client.post(path, json=arguments)
        except httpx.HTTPError as error:
            return Reply(False, error=f"{type(error).__name__}: {error}")
        return envelope(response)

    # -- lake's own capabilities -------------------------------------------------------------

    def read(self, capability: str, **arguments: Any) -> Reply:
        try:
            response = self.client.get(f"/v1/{capability}", params=arguments)
        except httpx.HTTPError as error:
            return Reply(False, error=f"{type(error).__name__}: {error}")
        return envelope(response)

    def capability(self, capability: str, arguments: dict[str, Any], method: str) -> Reply:
        """Call one customer-scoped lake capability through its audited registry route."""
        path = f"/v1/{capability}"
        scoped = {**arguments, "customer_id": self.customer_id}
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
        return self.zetta("get_agent", {"id": agent_id}, "get")

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
