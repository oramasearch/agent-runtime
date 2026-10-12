# Solution capabilities

Custom agents use the solution foundation through the same derived tool list as every other
workspace operation, under Lake's names (lake#674, agent-runtime#11). The runtime does not carry a
catalogue of S- or B-identifiers. At the start of each turn it refreshes Lake's OpenAPI, calls the
granted `list_solutions`, and calls `get_solution` (`{id}`) for every identifier that response
contains: Orama's catalogue and the workspace's own solutions (lake `docs/api/solutions.md`). The full records are placed in the
turn's model context and in an entrypoint's `Context.solution_catalog`. The automatic reads also
appear in the run trace.

The newest turn replaces the previous catalogue context in a continuing conversation. A deployed
registry can therefore add, remove, or change a solution without restarting the runtime. Duplicate
or malformed identifiers are ignored. If `list_solutions` returns `unknown_capability`, the current
catalogue is empty and the model is told that solution operations are not deployed yet. If Lake
answers `catalogue: {available: false}` (the engine cannot list Orama's), the workspace's own
solutions are the turn's catalogue and the model is told Orama's is not deployed yet, with Lake's
reason. Other read
failures remain failures rather than being presented as an empty successful registry.

## Grants

Both checks must pass before a solution tool is visible:

1. Lake's token-filtered OpenAPI contains the capability;
2. the agent's Studio skill grants that capability.

| Skills | Solution capabilities |
|---|---|
| `search`, `train`, `summarize`, `call-api` | `list_solutions`, `get_solution`, `get_policy`, `get_holdout_experiment` (the engine's `get_experiment`), `list_ledger`, `get_monitor`, `get_roi_readout` |
| `call-api` only, direct | `decide`, `record_outcome` |
| `call-api` only, a proposal | `approve_decision` |

Why `approve_decision` is a proposal, and `record_action` is gone, is the *Direct actions* decision
in [`../skills/README.md`](../skills/README.md#direct-actions).

An `agent.toml` tool allow-list can narrow this result further. It cannot add a capability denied by
either grant. `create_solution` and `run_solution` are not granted to custom agents by this runtime.

## Governed actions

The runtime sends each direct solution action to Lake's named capability (`/v1/decide`,
`/v1/record_outcome`), which reaches the workspace's engine, and returns the exact success or
refusal as the tool observation. It does not implement a second decision policy or a
second ledger. The engine owns policy versions, A1 approval, holdout assignment, actionable state,
and immutable decision/action/outcome records. Consequently a held entity remains non-actionable:
an engine refusal is returned to the model, and the runtime has no direct connector path that could
bypass it. An A1 decision becomes actionable only when a person confirms the agent's
`approve_decision` proposal.

The catalogue context instructs the model to report `runnable`, `status`, and `missing` fields as
returned. It must not turn a preview or sketch into a production claim, or promise ROI figures,
pricing, or odds of success.
