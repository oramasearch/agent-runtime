# Runtime tools

`tools.py` derives model tools from Lake's OpenAPI and skills. A dataset work area permits only
`get_dataset` and filtered `list_datasets` reads; other reads and direct actions fail closed.
Dataset identity includes both database and table, accepts slash/dot aliases, and uses Lake's
`datasets` database for bare IDs. A successful response must carry a matching database and table.
List responses expose only matching dataset rows, with no unscoped summary metadata.

Index searches currently cannot prove their source database because the engine resolves a bare
table name across databases. They are refused before any request, along with other capabilities
whose dataset membership is unknown. Pipelines, MCP tools (`mcp.py`), and definition functions are
not exposed to the model in dataset-confined runs. Exact proposals remain available.

These checks cover runtime-provided model tools. A custom Python entrypoint can still use its
launch token directly; enforcing the work area at that boundary requires Lake credential changes.
See the root README for the runtime's other modules and launch contract.
