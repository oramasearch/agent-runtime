# CLAUDE.md — agent-runtime

The blank Python agent. Read the zetta-bridge `CLAUDE.md` (*Agents in lake*) and
[`docs/agents/scaffold.md`](https://github.com/oramasearch/zetta-bridge/blob/main/docs/agents/scaffold.md)
first; the org rules in `oramasearch/orama-bridge` `CLAUDE.md` apply in full.

Rules specific to this repo:

- **It runs only user-created agents.** The specialized agents (Ask Orama, `agent-project`,
  `agent-autosuggest`) have their own repos and images. Never make them definitions here.
- **No agent gets an image.** An agent is a definition loaded at start. A dependency outside
  `ALLOWED_DEPENDENCIES` (`definition.py`) is refused at load, never installed at run time; adding
  one is a runtime release.
- **Everything goes through lake.** The workspace engine through Lake's named capabilities
  (`/v1/<name>`, lake#674; never the `/workspaces/<customer>/zetta/v1/` route, agent-runtime#11),
  code through `repository_tree` / `repository_file`. The runtime never holds a workspace's admin
  token.
- **Tools are derived**, from lake's filtered OpenAPI and the agent's skills, never hand-written.
  App and build reads are available without a Studio skill; App and build changes are exact
  proposals for a person to confirm, never writes from this runtime.
- **Every Lake capability is accounted for.** It belongs to a skill, to the App and build set, or
  to `NEVER_FOR_AGENTS` (`skills.py`); `tests/test_skills.py` fails otherwise. A skill's Lake reads
  are tools and its changes are proposals, except its `direct` engine actions, which the engine's
  policy governs (`docs/skills/README.md`, *Direct actions*: adding one is a recorded decision); `skills.json` is regenerated with the skills and vendored
  by lake (`docs/skills/README.md`).
- **A failing tool is an observation**, never an exception that ends the run.
- **Every run is a trace**, failures included, without any credential.
- uv, never pip. Private repo, private image (`oramasearch/agent-runtime`).
