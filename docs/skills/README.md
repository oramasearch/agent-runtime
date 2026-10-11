# Skills

A **skill** is what a person grants their agent: *Search data*, *Use drives*, *Design pipelines*.
`src/agent_runtime/skills.py` says what each skill lets an agent reach; the agent's tools are then
derived from OpenAPI documents at run time, never from a schema written here (agent-runtime#7).

## What a skill grants

Each skill has two sets of capability names:

- `engine`: the workspace engine's names, reached through Lake's filtered workspace OpenAPI
  (`/workspaces/<customer>/zetta/v1/openapi.json`). Only the original five skills have these, and
  they are called as before, actions included (the engine's own policy, holdout and approval rules
  decide; see [`../solutions/README.md`](../solutions/README.md)).
- `lake`: Lake's own capability names (`/v1/<name>`). Lake's OpenAPI says what each call does in
  `x-lake-action`, whatever its HTTP method: a `read` is a **tool** that calls Lake as the run's
  customer and workspace; anything else is a **proposal**.

A proposal returns `{"status": "proposed", "capability", "input"}` with `PROPOSED, NOT DONE` and is
recorded in the run's `proposals`, exactly as App changes are ([`../apps/README.md`](../apps/README.md)).
The person confirms it in Lake, where it runs as them, authorized and audited. The model is told a
tool whose description begins "Propose" changes nothing.

`customer_id` and `workspace_id` are removed from every Lake tool's schema; the runtime sends the
launch's `CUSTOMER_ID` and `WORKSPACE_ID` only to a capability that declares them. A read Lake
also takes as POST is one tool, on GET. The work area narrows Lake reads as it narrows engine ones.

When an engine tool and a Lake tool have the same name, the agent gets the engine tool. Lake's
`get_experiment` (an experiment) is therefore not reachable by a *train* agent while the engine's
`get_experiment` (a solution's holdout) is granted; moving the five original skills to Lake's names
is a follow-up.

## The never-for-agents list

`NEVER_FOR_AGENTS` names every Lake capability no skill may grant, grouped by why: sign-in and
sessions, recovery, key and certificate minting, secrets and encryption keys, people and access,
SSO and SCIM, a person's applications' sign-in, audit and the transparency log, billing changes,
workspaces and infrastructure, webhooks, the generic workspace surface, other agents and the agent
bus (the model gateway `model_completion` is reached through the OpenAI base URL, never as a tool),
MCP connections (an agent reaches a connection's tools only through that connection's own
`mcp:<prefix>` skill, `../../src/agent_runtime/mcp.py`), and events only an App, a build or the
operator publishes. Deleting a dataset, model, run or index, and training, retraining, registering,
loading or moving a standalone model are changes in the *datasets* and *pipelines* skills: proposals
a person confirms. `lake_grant` removes these even if
a skill named one.

## The contract

`tests/test_skills.py` reads Lake's capability list (`tests/fixtures/lake-capabilities.json`) and
fails when:

- a capability is in no skill, not in the App and build set, and not on the never list (the message
  names it: add it to a skill or to the list, with its reason);
- a skill, the App set or the never list names a capability Lake does not publish;
- a capability is both granted and never for agents;
- `skills.json` is not what `skills.py` and the fixture produce.

To refresh the fixture from a Lake (started with `LAKE_BILLING=on`, so billing is published):

```sh
tools/vendor-lake-capabilities.py http://127.0.0.1:8080/v1/openapi.json
uv run tools/write-skills-json.py
uv run pytest tests/test_skills.py
```

`LAKE_OPENAPI=<url or file> uv run pytest tests/test_skills.py` checks a live Lake without
changing the fixture.

## Where else the list lives

- **Lake** vendors `skills.json` (`tools/agents/vendor-agent-skills.py`): the Agent Factory and the
  agent pages offer these skills with their labels, and a run's lake key carries the reads of its
  agent's skills and never a change (lake `docs/api/agent-launches.md`, `docs/api/agent-factory.md`).
- **The workspace engine** accepts exactly these names on an agent record (`agent_skills()` in
  Zetta's `agent_record.cpp`).
- **AgentCreator** proposes only these names (`SKILLS` in agent-creator's `agent/main.py`).

A new skill is a change in all four, in that order: this repository, Zetta, Lake, AgentCreator.
