#!/usr/bin/env python3
"""Vendor Lake's capability list for the skills contract test (tests/test_skills.py).

Reads Lake's OpenAPI (`/v1/openapi.json`) from a running Lake or a saved file and writes
`tests/fixtures/lake-capabilities.json`: every `/v1/<name>` capability with its HTTP method, its
`x-lake-action` (what the call does: `read`, or a change), and its input field names. Nothing is
hand-written: the file is Lake's own OpenAPI, reduced to what the contract needs.

    tools/vendor-lake-capabilities.py http://127.0.0.1:8080/v1/openapi.json
    tools/vendor-lake-capabilities.py saved-openapi.json

Start Lake with LAKE_BILLING=on so the billing capabilities are in the document.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "lake-capabilities.json"


def load(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "agent-runtime-vendor"})
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    return json.loads(Path(source).read_text())


def inputs(method: str, operation: dict) -> list[str]:
    if method == "get":
        return sorted(parameter["name"] for parameter in operation.get("parameters") or []
                      if parameter.get("in") == "query")
    schema = (((operation.get("requestBody") or {}).get("content") or {})
              .get("application/json", {}).get("schema")) or {}
    return sorted((schema.get("properties") or {}).keys())


def reduce(openapi: dict) -> dict:
    capabilities: dict[str, dict] = {}
    for path, operations in sorted((openapi.get("paths") or {}).items()):
        name = path.removeprefix("/v1/")
        # Only JSON capabilities: liquid's repository sync routes (/v1/repositories/{id}/sync/...)
        # are a transport for the liquid client, not something an agent calls.
        if not path.startswith("/v1/") or "/" in name or name == "openapi.json":
            continue
        # A read is served on GET and, for large inputs, POST as well: GET is the one to call.
        method = "get" if "get" in operations else next(iter(operations))
        operation = operations[method]
        capabilities[name] = {"method": method,
                              "action": operation.get("x-lake-action", ""),
                              "inputs": inputs(method, operation)}
    return {"lake_version": (openapi.get("info") or {}).get("version", ""),
            "capabilities": capabilities}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    reduced = reduce(load(sys.argv[1]))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(reduced, indent=1, sort_keys=True) + "\n")
    print(f"{len(reduced['capabilities'])} capabilities -> {FIXTURE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
