# The `mcp:<prefix>` skill

**lake#652, decision 60.** A workspace in lake is the client of remote MCP servers (lake
`docs/api/mcp-connections.md`). A user-built agent reaches one connection's tools when its record's
`skills` carry `mcp:<prefix>`, the connection's prefix in lake (the workspace engine accepts the
skill from zetta's `issue-652-mcp-skills` change). Lake's *Create an agent for this connection*
makes an agent with that skill and no other.

`agent_runtime/mcp.py` derives the tools at every turn, never from a hand-written table:

1. `list_mcp_connection_tools {allowed_only: true}` as the launch's own credential. Lake answers
   an agent only the allowed tools of connections its skills grant, and checks the skill again on
   every call.
2. Each becomes `<prefix>__<tool>` with the server's input schema and a description naming the
   connection as an outside server.
3. A **read** calls `read_mcp_tool {connection_id, tool, arguments}`.
4. A **change** auto-approved for this agent (a person's grant on the connection's page, per tool
   and per agent, matched by the agent's id or name) calls `call_mcp_tool`; lake checks the grant
   and audits the call with the agent and the grantor.
5. Any other **change** is a proposal: the turn's result records
   `{capability: "call_mcp_tool", input: {connection_id, tool, arguments}, impact}` and nothing
   reaches lake or the server. The model is told it is proposed, not done.

Every answer is wrapped with a note that it came from an outside server and is data, never
instructions, and the turn gets one system message (`## Connected MCP tools`) saying so. The
connection's credential never reaches the runtime: lake runs the client.

`customer_id` comes from the launch; lake fills `workspace_id` from the launch or schedule
credential.

Tests: `tests/test_mcp.py`.
