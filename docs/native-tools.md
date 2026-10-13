# Native MCP Tools

## Publication and Execution

Native MCP tools now participate in Candidates, immutable Generations, exact
Solution `mcp-tool` component locks, Agent capability selection, and
`POST /v1/invocations`. No generated Skill package is required. Registration alone
does not publish a tool or grant it to every Agent.

Development Publish validates, records an explicit development auto-approval and
activates. Production retains the separate approval path. Validation reports
missing default Agents and dependencies instead of silently changing selections.
Release components carry matching draft/package/registration IDs into Candidates.

Development refreshes expired discovery once under a per-server lock. Disabled
servers, failed connections, changed configuration or schema remain blocking;
production freshness remains strict. Hashes detect interface drift, not remote
implementation changes.

High-risk direct calls return 202 with a durable run and exact-call HumanTask.
YAML Tool steps and model Skill/Agent calls wait for approval through Temporal.
Model execution resumes a LangGraph checkpoint. Missing required infrastructure
returns 503 before dispatch. Unknown external write outcomes are not retried.

Control schema 12 adds native publication and separate test records; version
10/11 migration preserves historical generation and approval digests.
See [Windows setup and limitations](local-durable.zh-CN.md).

## Standalone Skills

`execution.type: workflow` binds a package-contained WorkflowTemplate;
`execution.type: model` uses SKILL.md through a restricted execution-scoped Agent,
without creating an Agent record. Both require explicit `capabilityIds`, validate
input/output, and are started through `/v1/durable-runs`.
Nested model/workflow Skills are currently rejected. Synchronous dependencies and
native tools must be executable and, in a Solution, explicitly locked.

Examples: [quote/skill.yaml](examples/quote/skill.yaml) and
[brief/skill.yaml](examples/brief/skill.yaml). Replace every `REPLACE_SERVER_ID`
with the registered demo server ID before packaging. Register and publish the
demo `calculate_total` and `lookup_runbook` tools first, or include their exact
registrations in the same Candidate. Examples intentionally stay outside the
default Skills directory. A high-risk approved-write API/graph example is covered
by `tests/test_orchestrated_skills.py` and `tests/test_execution_approvals.py`.

## Unpublished Tool Tests

MCP Servers provides Test tool after connection discovery. Testing neither
registers nor publishes a capability. Explicit administrator risk review is bound
to config/schema identity; writes require confirmation and high-risk tests require
approval. Results retain MCP `isError`, structured content and content blocks,
with bounded escaped rendering and known-secret redaction. Unknown upstream
sensitive text cannot be exhaustively detected.

Endpoints under `/v1/admin/mcp/servers/{server_id}`:

- `POST /tool-risk-reviews`: tool name, risk, expected config/schema digests.
- `POST /tool-tests`: exact bindings, arguments, idempotency key and write confirmation.
- `GET /tool-tests/{test_id}`: retrieve without rerunning.
- `POST /tool-tests/{test_id}/decision`: approve/reject with reason.

The remaining sections document the original internal execution contract.

## Definition Contract

Native definitions use a snapshot-scoped executor. Existing MCP Skill imports
remain compatible and optional.

Implemented:

- `McpToolBinding` records the server ID, operational alias, original tool name,
  discovered schema digest, and server configuration digest.
- Stable IDs use `tool:mcp:<server-id>:<tool-name>`. Each segment is percent-encoded;
  an alias rename does not change identity, but does change the binding digest.
- `McpToolDefinition` records the explicit registration version, schemas, risk,
  execution bounds, and enablement. Its content digest covers these fields.
- Registration requires fresh discovery and stores an immutable draft. Repeating
  the same tool/version/content returns the same record; different content at the
  same version is rejected. Registered versions are platform versions, not a claim
  that the MCP server advertises or pins its implementation version.
- `ToolExecutor` receives copied definitions and an explicit allowed-ID set from
  its caller. It checks generation identity, membership, enablement, existing
  `PolicyEnforcer` decisions, input/output schemas, timeouts and output size.
- Calls use the existing MCP Gateway, which checks live discovery freshness and
  expected configuration/schema digests. Existing high-risk policy requires a
  verified approval in the runtime context. Publication never approves execution.
- State version 12 includes `tool_registrations`. Migration from version 10 preserves
  existing Solution, Release, Evaluation and Generation records without digest
  recalculation. The established migrations for older versions remain in place.
- Every new discovery/invocation session completes the MCP initialization
  handshake. Stdio stderr uses a real null-device file handle instead of StringIO.

## Admin API

Both endpoints use the existing admin router authentication policy. Whether that
policy is enabled still depends on the existing deployment configuration.

`POST /v1/admin/mcp/servers/{server_id}/tool-registrations`

```json
{
  "tool_name": "search",
  "version": "1.0.0",
  "description": "Query approved contract clauses",
  "risk": "read-only"
}
```

The tool name is in the body so names containing slashes are supported. Unknown
servers return 404; missing/stale discovery, unknown tools, invalid definitions,
and conflicting versions return 422. Successful registration returns a draft
record with its stable tool ID, definition, and content digest.

`GET /v1/admin/tools` lists native registrations. It does not list generated
MCP Skill packages.

Neither endpoint changes the active Generation, creates a Skill package, or makes
the tool available to an Agent. Candidate activation makes selected native
definitions available through the unified invocation API and explicit Agent scope.

## Execution Boundary

The new executor is an internal primitive, not a publication authority. A caller
constructing it is responsible for providing the intersection of approved
Generation membership, Solution Release locks, and Agent/delegation allowlists.
The runtime constructs these executors from the pinned Generation and Solution.

Results follow the existing Gateway contract: structured MCP content is returned
as a dictionary, while text-only results use a `content` array. An available
output schema validates that returned dictionary. Upstream tool errors propagate
as execution failures, not successful business output. The separate administrative
test API preserves the original MCP response envelope.

Schemas and configuration hashes detect drift in the declared interface; they do
not pin remote implementation behavior or external data. Stdio presets remain
administrator-trusted code, not an untrusted-code sandbox.

## Operational Limits

Local JSON/SQLite control stores assume one API instance. Automated tests use
mocked Temporal and an in-memory checkpoint saver for deterministic approval
resume; a real Postgres/Temporal process-restart acceptance test remains required.
Ambiguous model turns and external writes fail closed for operator reconciliation.
Do not infer external exactly-once execution from invocation idempotency.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_native_tools.py tests/test_tool_registration.py tests/test_control_store.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

The native registration tests use temporary state, fake connectors for deterministic
failure cases, and a real official-SDK stdio connection to the repository's demo
MCP server. The protocol test runs `calculate_total` without creating a Skill
package and cleans up its subprocess sessions. It does not contact configured
external MCP services or validate real-model Agent behavior.