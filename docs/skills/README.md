# Skills

A **skill** is what a person grants their agent: *Search data*, *Use drives*, *Design pipelines*.
`src/agent_runtime/skills.py` says what each skill lets an agent reach; the agent's tools are then
derived from OpenAPI documents at run time, never from a schema written here.

## What a skill grants

Each skill names Lake capabilities (`/v1/<name>`), the workspace engine's included under the names
Lake gives them (lake#674, lake `docs/api/engine-capabilities.md`). Lake's OpenAPI says what each
call does in `x-lake-action`, whatever its HTTP method:

- a `read` is a **tool** that calls Lake as the run's customer and workspace;
- a skill's `direct` actions are tools that call Lake too ([Direct actions](#direct-actions));
- anything else is a **proposal**.

The five original skills (*search*, *generate-data*, *train*, *summarize*, *call-api*) used to
reach the engine under the engine's own names through Lake's workspace Zetta route
(`/workspaces/<customer>/zetta/v1/`), and an engine tool kept its name over a Lake tool of the same
name. Since agent-runtime#11 they grant Lake names only, and the runtime reads neither that route
nor its OpenAPI (the agent's record is Lake's `get_agent`, a granted Pipeline runs through
`check_pipeline`, `start_run`, `get_run` and `get_run_report`):

| The engine's name | Lake's name |
|---|---|
| `search` | `search_workspace_index` (`search` is Lake-wide search, which *search* also grants) |
| `get_graph` | `get_pipeline` |
| `gate` | `check_pipeline` |
| `run` | `start_run` |
| `get_experiment` (a solution's holdout) | `get_holdout_experiment` |

Every other name is the same. A *train* agent therefore reaches Lake's own `get_experiment` (a
training experiment) beside `get_holdout_experiment`. `record_action`, which *call-api* used to
name, is not on Lake's workspace allow-lists, so Lake never let it through the workspace route
either; it is gone from the skill and no agent loses a call it could make.

A proposal returns `{"status": "proposed", "capability", "input"}` with `PROPOSED, NOT DONE` and is
recorded in the run's `proposals`, exactly as App changes are ([`../apps/README.md`](../apps/README.md)).
The person confirms it in Lake, where it runs as them, authorized and audited. The model is told a
tool whose description begins "Propose" changes nothing.

`customer_id` and `workspace_id` are removed from every Lake tool's schema; the runtime sends the
launch's `CUSTOMER_ID` and `WORKSPACE_ID` only to a capability that declares them. A read Lake
also takes as POST is one tool, on GET. The work area narrows Lake reads as it narrows engine ones.

A dataset work area permits only `get_dataset` with a matching database and table, and
`list_datasets` with matching rows only. Slash/dot IDs are aliases; bare IDs use Lake's `datasets`
database. Other reads and direct actions are refused until their source datasets can be verified;
this includes search indexes, whose current source-table contract omits the database. Pipeline,
MCP and definition tools are not exposed in dataset-confined runs. Proposals remain available.
See the root README's work-area contract, including the separate Lake credential enforcement
needed to confine custom Python entrypoints.

## Direct actions

Decision. Before, the five original skills ran every engine action they granted
directly, while every change the newer skills grant is a proposal. Each of those engine actions was
reviewed against one question: does the engine's own policy already decide whether the call may
take effect, so that a person confirming it first adds nothing the engine does not check?

| Action (skill) | Decision | Why |
|---|---|---|
| `check_pipeline`, the engine's `gate` (*train*) | **direct** | It evaluates a stored pipeline against the correctness checks and changes nothing (Lake: "Evaluates only, no state change"). A granted Pipeline needs it before every run. |
| `start_run`, the engine's `run` (*train*) | **direct** | The engine runs only a stored pipeline that passes the gate, through its registered methodology runner, and refuses unknown or unavailable model kinds. A run is a new record: it changes no pipeline, model or deployment, and deleting one stays a proposal (`delete_run`, *pipelines*). |
| `generate_dataset` (*generate-data*) | **direct** | The engine admits only gated cases (references pass, negative controls fail), and it is the skill's one job. |
| `decide` (*call-api*) | **direct** | The engine applies the current policy version: the tier says whether the decision is actionable, kill-switch, eligibility, caps, budget and holdout block it, an A1 decision waits for approval, and every decision is version-stamped and audited. |
| `record_outcome` (*call-api*) | **direct** | It appends one immutable outcome, which must reference an existing action, to the engine's append-only ledger; it changes no decision and no policy. |
| `approve_decision` (*call-api*) | **proposal** | A1 exists so that a person approves the decision. A run's key acts as the person who launched it, so an agent calling it would record that person as the approver without their seeing the decision. The agent proposes it; the person confirms it in Lake. |
| `record_action` (*call-api*) | **removed** | Lake publishes no such capability and never allowed it through the workspace route. |

A direct action is still one of Lake's named capabilities: Lake authorizes it on the customer
(`write`), allows it to a run's key only through the `workspace_act` scope every launch carries
(lake `docs/api/agent-launches.md`, `granted_by_scope` in `docs/api/engine-capabilities.md`), and
audits it in the workspace's log. The engine's refusal (a blocked gate, a holdout, a policy block)
is the tool's observation; the runtime tries no other path.

A capability is direct only for the skill that names it in `direct`: `start_run` and
`check_pipeline` are direct for *train* and proposals for *pipelines*, whose job is to design
pipelines a person approves. Every other engine action (creating projects, compiling and saving
pipelines, experiments, solutions, policies, exposing models and indexes) stays a proposal. Making
another action direct is a new decision recorded here, and `tests/test_skills.py` names the set.

## The never-for-agents list

`NEVER_FOR_AGENTS` names every Lake capability no skill may grant, grouped by why: sign-in and
sessions, recovery, key and certificate minting, secrets and encryption keys, people and access,
SSO and SCIM, a person's applications' sign-in, audit and the transparency log, billing changes,
workspaces and infrastructure, webhooks, the generic workspace surface, other agents and the agent
bus (the model gateway `model_completion` is reached through the OpenAI base URL, never as a tool),
MCP connections (an agent reaches a connection's tools only through that connection's own
`mcp:<prefix>` skill, `../../src/agent_runtime/mcp.py`), events only an App, a build or the
operator publishes, and a methodology run's runner side (`methodology_run_input`,
`methodology_run_output`, called only with the run's own credential, lake#655). A workspace's own
methodologies are read by *train* and *pipelines*, and changed by *pipelines* as proposals. Deleting a dataset, model, run or index, and training, retraining, registering,
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
- a skill's `direct` action is not a Lake change that skill grants, or the direct set is not the
  one decided above;
- one of the five original skills no longer reaches, under Lake's name, a capability it reached
  under the engine's;
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
  Each skill's entry lists `reads`, `proposals` and `direct`; Lake reads `name`, `label` and
  `reads`. A direct action needs no scope of its own: every launch carries `workspace_act`.
- **The workspace engine** accepts exactly these names on an agent record (`agent_skills()` in
  Zetta's `agent_record.cpp`).
- **AgentCreator** proposes only these names (`SKILLS` in agent-creator's `agent/main.py`).

A new skill is a change in all four, in that order: this repository, Zetta, Lake, AgentCreator.
