# SPDX-License-Identifier: MIT

"""Loading an agent: its record, then its instructions and code.

The record is the agent's `AgentRecord` in the workspace's Zetta (zetta#1015): Studio's shape
(purpose, work area, manifesto, goal, skills, trigger), the granted Pipelines, and for a Python
agent a `definition` whose source is either **zetta** (instructions and files inline on the
record) or **lake** (a repository at a pinned revision, never a branch).

The same layout holds in both sources:

    agent.toml       optional: entrypoint, model, tools (an allow-list), dependencies
    instructions.md  optional when the record carries instructions
    agent/           optional code: tool functions (agent/tools.py TOOLS) or a custom loop

Dependencies are the image's, never installed at run time: a definition asking for a package
outside `ALLOWED_DEPENDENCIES` is refused at load. Adding one is a new runtime release, which is
what keeps "no image per agent" true without downloading arbitrary code.
"""

from __future__ import annotations

import json
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runtime.lake import Lake, LoadError

ALLOWED_DEPENDENCIES = frozenset({"openai", "httpx", "pydantic", "pandas", "numpy"})


@dataclass
class Agent:
    """A loaded agent: everything a run needs, and nothing it would have to look up again."""

    id: str
    name: str
    purpose: str = ""
    manifesto: str = ""
    goal: str = ""
    work_area: dict[str, str] = field(default_factory=lambda: {"kind": "workspace", "detail": ""})
    skills: list[str] = field(default_factory=list)
    trigger: str = ""
    model: str = ""
    instructions: str = ""
    entrypoint: str = ""
    tool_allow_list: list[str] | None = None
    pipelines: list[dict[str, str]] = field(default_factory=list)
    directory: Path | None = None

    def system_prompt(self) -> str:
        """The instructions the definition carries, or the ones composed from Studio's boxes."""
        if self.instructions.strip():
            return self.instructions
        parts = [f"# {self.name}", self.purpose]
        if self.manifesto:
            parts += ["## How you work", self.manifesto]
        if self.goal:
            parts += ["## Your goal", self.goal]
        return "\n\n".join(part for part in parts if part)


def from_directory(directory: Path) -> dict[str, Any]:
    """The record of a definition built into an image: `record.json` beside its files."""
    record_path = directory / "record.json"
    if not record_path.exists():
        raise LoadError(f"no record.json in {directory}")
    record = json.loads(record_path.read_text())
    record["runtime"] = "python"
    record["definition"] = {**(record.get("definition") or {}), "source": "dir",
                            "path": str(directory)}
    return record


def safe_path(root: Path, relative: str) -> Path:
    """A definition's file, never outside the definition's own directory."""
    target = (root / relative).resolve()
    if not str(target).startswith(str(root.resolve())) or relative.startswith("/"):
        raise LoadError(f"'{relative}' is outside the definition")
    return target


def materialize(files: dict[str, str], root: Path) -> None:
    for relative, contents in files.items():
        target = safe_path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)


def from_record(record: dict[str, Any], lake: Lake | None = None,
                workdir: Path | None = None) -> Agent:
    """Build an `Agent` from its record, fetching and checking its definition."""
    runtime = record.get("runtime") or "native"
    if runtime != "python":
        raise LoadError(f"agent '{record.get('name')}' runs {runtime}, not on agent-runtime")
    definition = record.get("definition") or {}
    source = definition.get("source")
    if source == "dir":
        # A definition built into an image layered on this runtime (a specialized Orama agent
        # such as AgentCreator): read in place, never copied.
        directory = Path(definition.get("path", ""))
        files = {str(path.relative_to(directory)): path.read_text()
                 for path in directory.rglob("*") if path.is_file() and "__pycache__" not in
                 path.parts}
        workdir = workdir or directory
    elif source == "zetta":
        files = {item["path"]: item.get("contents", "") for item in definition.get("files") or []}
    elif source == "lake":
        if not definition.get("repository_id") or not definition.get("revision"):
            raise LoadError("a lake definition needs a repository_id and a pinned revision")
        if lake is None:
            raise LoadError("a lake definition needs lake to read it from")
        files = lake.repository_files(definition["repository_id"], definition["revision"])
    else:
        raise LoadError("the definition's source must be 'zetta' or 'lake'")

    root = workdir or Path(tempfile.mkdtemp(prefix="agent-"))
    if source != "dir":
        materialize(files, root)
    settings: dict[str, Any] = {}
    if "agent.toml" in files:
        try:
            settings = tomllib.loads(files["agent.toml"])
        except tomllib.TOMLDecodeError as error:
            raise LoadError(f"agent.toml does not parse: {error}") from error
    wanted = set(settings.get("dependencies") or [])
    if wanted - ALLOWED_DEPENDENCIES:
        raise LoadError("dependencies outside the runtime's pinned set: "
                        + ", ".join(sorted(wanted - ALLOWED_DEPENDENCIES))
                        + f" (available: {', '.join(sorted(ALLOWED_DEPENDENCIES))})")
    instructions = files.get("instructions.md") or definition.get("instructions") or ""
    entrypoint = settings.get("entrypoint") or definition.get("entrypoint") or ""
    if entrypoint:
        module = entrypoint.split(":", 1)[0]
        if not (root / (module.replace(".", "/") + ".py")).exists():
            raise LoadError(f"the entrypoint's module '{module}' is not in the definition")
    return Agent(id=record.get("id", ""),
                 name=record.get("name", ""),
                 purpose=record.get("purpose", ""),
                 manifesto=record.get("manifesto", ""),
                 goal=record.get("goal", ""),
                 work_area=record.get("work_area") or {"kind": "workspace", "detail": ""},
                 skills=list(record.get("skills") or []),
                 trigger=record.get("trigger", ""),
                 model=settings.get("model") or record.get("model_name") or "",
                 instructions=instructions,
                 entrypoint=entrypoint,
                 tool_allow_list=settings.get("tools"),
                 pipelines=list(record.get("pipelines") or []),
                 directory=root)
