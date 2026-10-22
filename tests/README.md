# Runtime tests

Run `uv run pytest` for the full suite. The tests use fake Lake transports and scripted model
replies; they do not need a running Lake or model service.

`test_dataset_work_area.py` checks database-qualified identity, allowed aliases, malformed grants
and selectors, response filtering, both search routes, and refusal of tools with no verifiable
dataset relationship. It also covers granted pipelines, MCP, and definition tools so those paths
cannot reintroduce unrestricted model calls. `test_skills.py` checks the OpenAPI/skill contract;
its bare dataset grant refers only to Lake's default `datasets` database.
