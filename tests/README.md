# Runtime tests

Run `uv run pytest` for the full suite. The tests use fake Lake transports and scripted model
replies; they do not need a running Lake or model service.

`test_dataset_work_area.py` checks database-qualified identity, allowed aliases, malformed grants
and selectors, response filtering, both search routes, and refusal of tools with no verifiable
dataset relationship. It also covers granted pipelines, MCP, and definition tools so those paths
cannot reintroduce unrestricted model calls. `test_skills.py` checks the OpenAPI/skill contract;
its bare dataset grant refers only to Lake's default `datasets` database.

`test_by_reference.py` (lake#871) claims by reference: the claim asks `credentials: by_reference`,
the turn gets the bus credential and `X-Lake-Agent-Task`, every per-turn Lake call and model call
sends both, and no bus call sends the header. Against a Lake from before lake#871, which refuses the
field, the claim is asked again without it and the body's own credential is used with no task
header; any other refusal is not retried.
