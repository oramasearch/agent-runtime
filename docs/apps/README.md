# App and build capabilities

Custom agents receive Ship App and image-build capabilities from lake's filtered OpenAPI. The
runtime does not carry copied request schemas and does not infer authority from an agent's text.
For every service turn it refreshes lake's OpenAPI, then derives the tools from the operations the
turn credential can see. A definition's `agent.toml` tool allow-list may narrow those tools.

## Reads

Every custom agent may use the following operations when lake exposes them to its credential. They
do not depend on a Studio skill:

- Apps: `list_apps`, `get_app`, `app_logs`, and `receive_app_events`;
- builds: `list_builds`, `get_build`, `build_logs`, and `receive_build_events`.

The runtime removes `customer_id` from each model-facing schema and supplies the launch's customer
itself. A custom agent therefore cannot redirect an App or build read to another customer. The call
goes to lake's ordinary `/v1/<capability>` route, where policy and signed audit recording still
apply. Event tools expose lake's retained, one-based cursor contract unchanged.

## Changes are proposals

Lake's filtered OpenAPI also decides which App and build changes an agent may propose. Supported
proposal tools include create, deploy, rollback, lifecycle, scaling, environment, authentication,
binding, build, and cancellation operations. A capability absent from the filtered contract is not
a tool. Internal operator operations such as `publish_app_event` and `publish_build_event` are never
offered.

Calling one of these tools records this exact object in the turn result:

```json
{
  "capability": "scale_app",
  "input": {"app_id": "app-1", "replicas": 3},
  "impact": "Scale one App to its proposed replica count"
}
```

The runtime does not send the mutation to lake. Its tool observation says `PROPOSED, NOT DONE`, and
the model is instructed to describe it as proposed. The person confirms through lake; that later
call uses the ordinary authorization and audit path. Job and service traces retain the proposed
capability and input even when the model fails before answering.

A custom definition with an entrypoint receives the same information in
`Context.app_capabilities`. Calling an App proposal through `Context.call_tool` adds it to the
entrypoint's result just as the default model loop does.

## ApplicationAgent

The turn context identifies `agent-application` as the specialist that builds and changes Apps.
Delegating an `app` turn asks that agent for a proposal. Its first turn has only read capabilities.
After a person reviews the exact files and App settings, lake starts a later turn with the reviewed
object at `context.confirmed_proposal`; only that later turn receives repository writes,
`create_app`, and `deploy_app`.

The custom agent must not claim that delegation or a proposal deployed anything. The deployed App
state comes from a later `get_app`, App event, build record, or build event read.
