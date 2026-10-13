const tokenInput = document.querySelector("#adminToken");
const authPanel = document.querySelector("#authPanel");
const connectionState = document.querySelector("#connectionState");
const notice = document.querySelector("#notice");
const storageKey = "sage.admin-token";
const accessTokenKey = "sage.access-token";
const accessTokenExpiryKey = "sage.access-token-expiry";
let authenticationMode = "anonymous";
let environment = "production";
let toolTest = null;

const state = {
  status: null,
  solutions: [],
  solutionReleases: [],
  capabilities: [],
  agents: [],
  skills: [],
  mcpServers: [],
  tools: [],
  candidates: [],
  approvals: [],
  generations: [],
  feedback: [],
  qualitySummary: null,
  evaluationCandidates: [],
  sampleSets: [],
  qualityComparisons: [],
  selectedAgent: null,
  selectedMcp: null,
  selectedQualityFeedback: null,
  selectedSampleSet: null,
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function actionButton(label, handler, className = "secondary") {
  const button = element("button", className, label);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await handler();
    } catch (error) {
      setNotice(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function setNotice(message, kind = "") {
  notice.textContent = message;
  notice.className = `notice ${kind}`.trim();
}

function token() {
  if (authenticationMode === "entra-id") {
    const accessToken = sessionStorage.getItem(accessTokenKey) || "";
    const expiry = Number(sessionStorage.getItem(accessTokenExpiryKey) || 0);
    if (expiry <= Date.now()) {
      sessionStorage.removeItem(accessTokenKey);
      sessionStorage.removeItem(accessTokenExpiryKey);
      return "";
    }
    return accessToken;
  }
  return sessionStorage.getItem(storageKey) || "";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const authorizationToken = token();
  if (authorizationToken) {
    headers.set("Authorization", `Bearer ${authorizationToken}`);
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      message = typeof payload.detail === "string" ? payload.detail
        : payload.detail ? JSON.stringify(payload.detail) : message;
    } catch {
      // Preserve the status text for non-JSON proxy errors.
    }
    throw new Error(message);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response;
}

function statusBadge(value) {
  const normalized = value || "unknown";
  const good = new Set(["valid", "active", "approved", "reachable", "discovered", "published", "improved", "resolved"]);
  const bad = new Set(["invalid", "failed", "rejected", "activation-failed", "regressed"]);
  const kind = good.has(normalized) ? "success" : bad.has(normalized) ? "danger" : "warning";
  return element("span", `status-badge ${kind}`, normalized);
}

function activeGeneration() {
  return state.generations.find((item) => item.generation_id === state.status?.active_generation_id);
}

function sameDefinition(left, right) {
  const ordered = (_key, value) => value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).sort(([leftKey], [rightKey]) => leftKey.localeCompare(rightKey)))
    : value;
  return JSON.stringify(left, ordered) === JSON.stringify(right, ordered);
}

function publicationStatus(record, kind) {
  const active = activeGeneration();
  const current = kind === "agent"
    ? sameDefinition(active?.agent_definitions?.[record.definition.agent_id], record.definition)
    : kind === "skill"
      ? Boolean(record.validation?.content_digest) && active?.skill_digests?.[record.name] === record.validation.content_digest
      : sameDefinition(active?.tool_definitions?.[record.tool_id], record.definition);
  return current ? "active" : record.status === "published" ? "published (historical)" : record.status;
}

function renderValidation(target, report) {
  target.replaceChildren();
  if (!report || report.status === "not-run") {
    target.append(element("div", "empty", "Validation has not run."));
    return;
  }
  target.append(statusBadge(report.status));
  for (const issue of report.issues || []) {
    target.append(element("div", `issue ${issue.severity}`, `${issue.code}: ${issue.message}`));
  }
}

function navigate(view) {
  for (const button of document.querySelectorAll(".nav-item")) {
    button.classList.toggle("active", button.dataset.view === view);
  }
  for (const panel of document.querySelectorAll("[data-view-panel]")) {
    panel.classList.toggle("active", panel.dataset.viewPanel === view);
  }
}

function preparePublication(targetId, recordId, title) {
  renderCatalog();
  const input = [...document.querySelectorAll(`#${targetId} input`)]
    .find((item) => item.value === recordId);
  if (!input) throw new Error("Validate this resource before preparing publication.");
  input.checked = true;
  const titleInput = document.querySelector("#candidateTitle");
  if (!titleInput.value.trim()) titleInput.value = title;
  navigate("catalog");
  document.querySelector("#candidateForm").scrollIntoView({ block: "start" });
  titleInput.focus({ preventScroll: true });
}

let quickPublishing = false;
async function quickPublishResource(targetId, recordId, title) {
  if (environment !== "development" || quickPublishing) return;
  quickPublishing = true;
  setNotice("Publishing...");
  try {
    const fields = { candidateAgents: "agent_draft_ids", candidateSkills: "skill_package_ids", candidateTools: "tool_registration_ids" };
    if (targetId === "candidateAgents" || targetId === "candidateSkills") {
      const kind = targetId === "candidateAgents" ? "agents" : "skills";
      const validated = await api(`/v1/admin/${kind}/${recordId}/validate`, { method: "POST" });
      if (validated.status !== "valid" && validated.status !== "published") {
        throw new Error(JSON.stringify(validated.validation?.issues || validated));
      }
    }
    const candidate = await api("/v1/admin/candidates", { method: "POST", body: JSON.stringify({ title, [fields[targetId]]: [recordId] }) });
    try {
      await api(`/v1/admin/candidates/${candidate.candidate_id}/publish`, { method: "POST" });
    } catch (error) {
      await loadAll();
      const failed = state.candidates.find((item) => item.candidate_id === candidate.candidate_id);
      throw new Error(`${error.message} ${JSON.stringify(failed?.validation?.issues || [])}`);
    }
    await loadAll();
    setNotice(`${title} published`);
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    quickPublishing = false;
  }
}

for (const button of document.querySelectorAll(".nav-item")) {
  button.addEventListener("click", () => navigate(button.dataset.view));
}

function renderOverview() {
  const metrics = document.querySelector("#overviewMetrics");
  metrics.replaceChildren();
  if (!state.status) return;
  const values = [
    ["Solutions", state.status.solutions],
    ["Releases", state.status.solution_releases],
    ["Agents", state.status.agents],
    ["Skill packages", state.status.skills],
    ["MCP servers", state.status.mcp_servers],
    ["Pending approvals", state.status.pending_approvals],
    ["Generations", state.generations.length],
  ];
  for (const [label, value] of values) {
    const metric = element("div", "metric");
    metric.append(element("span", "", label), element("strong", "", String(value)));
    metrics.append(metric);
  }
  const active = document.querySelector("#activeGeneration");
  active.replaceChildren();
  active.append(
    element("code", "", state.status.active_generation_id),
    element(
      "div",
      "muted",
      state.status.plaintext_dev_secrets
        ? "DEV plaintext secrets enabled"
        : "Secret references required",
    ),
  );
}

function checkedCapabilities() {
  return [...document.querySelectorAll("#agentCapabilities input:checked")].map(
    (input) => input.value,
  );
}

function renderCapabilityOptions(selected = []) {
  const container = document.querySelector("#agentCapabilities");
  container.replaceChildren();
  const selectedSet = new Set(selected);
  for (const capability of state.capabilities) {
    const label = element("label", "check-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = capability.capability_id;
    input.checked = selectedSet.has(capability.capability_id);
    input.disabled = !capability.executable;
    label.append(input, document.createTextNode(`${capability.name} · ${capability.version}`));
    container.append(label);
  }
}

function clearAgentEditor() {
  state.selectedAgent = null;
  document.querySelector("#agentDraftId").value = "";
  document.querySelector("#agentId").value = "";
  document.querySelector("#agentVersion").value = "1.0.0";
  document.querySelector("#agentName").value = "";
  document.querySelector("#agentInstructions").value = "";
  document.querySelector("#agentMemory").value = "thread";
  document.querySelector("#agentMaxSteps").value = "40";
  document.querySelector("#agentMaxModelCalls").value = "100";
  document.querySelector("#agentMaxToolCalls").value = "100";
  document.querySelector("#agentMaxSubagentCalls").value = "20";
  document.querySelector("#agentSubagents").value = "[]";
  renderCapabilityOptions();
  document.querySelector("#agentValidation").replaceChildren();
}

function selectAgent(record) {
  state.selectedAgent = record;
  const value = record.definition;
  document.querySelector("#agentDraftId").value = record.draft_id;
  document.querySelector("#agentId").value = value.agent_id;
  document.querySelector("#agentVersion").value = value.version;
  document.querySelector("#agentName").value = value.name;
  document.querySelector("#agentInstructions").value = value.instructions;
  document.querySelector("#agentMemory").value = value.memory_policy;
  document.querySelector("#agentMaxSteps").value = String(value.max_steps);
  document.querySelector("#agentMaxModelCalls").value = String(value.max_model_calls || 100);
  document.querySelector("#agentMaxToolCalls").value = String(value.max_tool_calls || 100);
  document.querySelector("#agentMaxSubagentCalls").value = String(value.max_subagent_calls || 20);
  document.querySelector("#agentSubagents").value = JSON.stringify(value.subagents || [], null, 2);
  renderCapabilityOptions(value.capability_ids || []);
  renderValidation(document.querySelector("#agentValidation"), record.validation);
  renderAgents();
}

function renderAgents() {
  const list = document.querySelector("#agentList");
  list.replaceChildren();
  document.querySelector("#agentCount").textContent = `${state.agents.length} versions`;
  for (const record of state.agents) {
    const button = element(
      "button",
      `resource-item ${state.selectedAgent?.draft_id === record.draft_id ? "selected" : ""}`,
    );
    button.type = "button";
    button.append(
      element("strong", "", record.definition.name),
      element("span", "", `${record.definition.agent_id} · ${record.definition.version}`),
      statusBadge(publicationStatus(record, "agent")),
    );
    button.addEventListener("click", () => selectAgent(record));
    list.append(button);
    if (environment === "development" && publicationStatus(record, "agent") !== "active") {
      list.append(actionButton("Quick publish", () => quickPublishResource(
        "candidateAgents", record.draft_id, `${record.definition.name} ${record.definition.version}`,
      ), "primary"));
    }
    if (record.status === "valid") {
      list.append(actionButton("Prepare publication", () => preparePublication(
        "candidateAgents", record.draft_id, `${record.definition.name} ${record.definition.version}`,
      )));
    }
  }
}

function agentPayload() {
  let subagents;
  try {
    subagents = JSON.parse(document.querySelector("#agentSubagents").value || "[]");
  } catch {
    throw new Error("Subagents JSON is invalid");
  }
  if (!Array.isArray(subagents)) throw new Error("Subagents JSON must be an array");
  return {
    agent_id: document.querySelector("#agentId").value.trim(),
    name: document.querySelector("#agentName").value.trim(),
    version: document.querySelector("#agentVersion").value.trim(),
    instructions: document.querySelector("#agentInstructions").value.trim(),
    capability_ids: checkedCapabilities(),
    subagents,
    memory_policy: document.querySelector("#agentMemory").value,
    max_steps: Number(document.querySelector("#agentMaxSteps").value),
    max_model_calls: Number(document.querySelector("#agentMaxModelCalls").value),
    max_tool_calls: Number(document.querySelector("#agentMaxToolCalls").value),
    max_subagent_calls: Number(document.querySelector("#agentMaxSubagentCalls").value),
  };
}

function renderSkills() {
  document.querySelector("#skillPackageCount").textContent = `${state.skills.length} packages`;
  const root = document.querySelector("#skillPackageList");
  root.replaceChildren();
  const table = element("table");
  const head = element("thead");
  const header = element("tr");
  for (const label of ["Skill", "Source", "Execution", "Status", "Validation", "Actions"]) {
    header.append(element("th", "", label));
  }
  head.append(header);
  const body = element("tbody");
  for (const record of state.skills) {
    const row = element("tr");
    const name = element("td");
    name.append(
      element("strong", "", record.name || record.original_filename),
      element("div", "muted", record.version || record.package_id),
    );
    const actions = element("td", "row-actions");
    actions.append(
      actionButton("Validate", () => runAction(`/v1/admin/skills/${record.package_id}/validate`, { method: "POST" })),
    );
    if (record.original_relpath) {
      actions.append(actionButton("Original", () => downloadSkill(record, "original")));
    }
    actions.append(actionButton("Normalized", () => downloadSkill(record, "normalized")));
    if (environment === "development" && publicationStatus(record, "skill") !== "active") {
      actions.append(actionButton("Quick publish", () => quickPublishResource(
        "candidateSkills", record.package_id, `${record.name || record.original_filename} ${record.version || ""}`,
      ), "primary"));
    }
    if (record.status === "valid") {
      actions.append(actionButton("Prepare publication", () => preparePublication(
        "candidateSkills", record.package_id, `${record.name} ${record.version}`,
      )));
    }
    const validation = element("td");
    validation.textContent = (record.validation.issues || [])
      .map((issue) => `${issue.severity}: ${issue.message}`)
      .join("\n") || "-";
    row.append(
      name,
      element("td", "", record.source),
      element("td", "", `${record.execution_type || "unknown"}${record.executable ? "" : " · disabled"}`),
      element("td", "", publicationStatus(record, "skill")),
      validation,
      actions,
    );
    body.append(row);
  }
  table.append(head, body);
  root.append(table);
}

async function downloadSkill(record, kind) {
  try {
    const response = await api(`/v1/admin/skills/${record.package_id}/download/${kind}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = kind === "original"
      ? record.original_filename
      : `${record.name || record.package_id}-${record.version || "draft"}-normalized.zip`;
    anchor.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    setNotice(error.message, "error");
  }
}

function clearMcpEditor() {
  closeToolTest();
  state.selectedMcp = null;
  for (const id of ["mcpServerId", "mcpAlias", "mcpUrl", "mcpPreset", "mcpSecretRef", "mcpDevSecret", "mcpToolName", "mcpSkillName", "mcpSkillDescription", "mcpSkillInstructions"]) {
    document.querySelector(`#${id}`).value = "";
  }
  document.querySelector("#mcpTransport").value = "streamable-http";
  document.querySelector("#mcpSkillVersion").value = "1.0.0";
  document.querySelector("#mcpSkillRisk").value = "read-only";
  document.querySelector("#mcpTools").replaceChildren();
  renderMcpServers();
}

function selectMcp(record) {
  if (toolTest && (toolTest.server.server_id !== record.server_id ||
    toolTest.server.config_digest !== record.config_digest ||
    !record.tools?.some((tool) => tool.name === toolTest.tool.name &&
      tool.schema_digest === toolTest.tool.schema_digest))) closeToolTest();
  state.selectedMcp = record;
  document.querySelector("#mcpServerId").value = record.server_id;
  document.querySelector("#mcpAlias").value = record.alias;
  document.querySelector("#mcpTransport").value = record.transport;
  document.querySelector("#mcpUrl").value = record.url || "";
  document.querySelector("#mcpPreset").value = record.command_preset || "";
  document.querySelector("#mcpSecretRef").value = record.secret_ref || "";
  document.querySelector("#mcpDevSecret").value = "";
  renderMcpServers();
  renderMcpTools();
}

function renderMcpServers() {
  const list = document.querySelector("#mcpList");
  list.replaceChildren();
  document.querySelector("#mcpCount").textContent = `${state.mcpServers.length} servers`;
  for (const record of state.mcpServers) {
    const button = element(
      "button",
      `resource-item ${state.selectedMcp?.server_id === record.server_id ? "selected" : ""}`,
    );
    button.type = "button";
    button.append(
      element("strong", "", record.alias),
      element("span", "", record.transport),
      statusBadge(record.status),
    );
    button.addEventListener("click", () => selectMcp(record));
    list.append(button);
  }
}

function renderMcpTools() {
  const root = document.querySelector("#mcpTools");
  root.replaceChildren();
  for (const tool of state.selectedMcp?.tools || []) {
    const row = element("div", "tool-row");
    const button = element("button", "resource-item");
    button.type = "button";
    button.append(
      element("strong", "", tool.name),
      element("span", "", tool.description || "No description"),
      element("small", "", tool.schema_digest),
    );
    button.addEventListener("click", () => {
      document.querySelector("#mcpToolName").value = tool.name;
      document.querySelector("#mcpSkillName").value = tool.name.toLowerCase().replaceAll("_", "-");
      document.querySelector("#mcpSkillDescription").value = tool.description || `Use ${tool.name}.`;
      document.querySelector("#mcpSkillInstructions").value = `Use this Skill when the request requires the ${tool.name} MCP Tool.`;
    });
    row.append(button, actionButton("Test tool", () => openToolTest(state.selectedMcp, tool)));
    root.append(row);
  }
  const registrations = state.tools.filter((record) =>
    record.definition.binding.server_id === state.selectedMcp?.server_id);
  if (registrations.length) {
    root.append(element("h3", "", "Native tool versions"));
    for (const record of registrations) {
      const row = element("div", "tool-row");
      row.append(element("span", "", `${record.definition.binding.tool_name} · ${record.definition.version}`),
        statusBadge(publicationStatus(record, "tool")),
        actionButton("Prepare publication", () => preparePublication(
          "candidateTools", record.registration_id, `${record.definition.binding.tool_name} ${record.definition.version}`,
        )));
      if (environment === "development" && publicationStatus(record, "tool") !== "active") {
        row.append(actionButton("Quick publish", () => quickPublishResource(
          "candidateTools", record.registration_id, `${record.definition.binding.tool_name} ${record.definition.version}`,
        ), "primary"));
      }
      root.append(row);
    }
  }
}

function toolTestBinding() {
  return {
    tool_name: toolTest.tool.name,
    expected_server_config_digest: toolTest.server.config_digest,
    expected_schema_digest: toolTest.tool.schema_digest,
  };
}

async function openToolTest(server, tool) {
  toolTest = { server, tool, key: crypto.randomUUID(), record: null };
  document.querySelector("#mcpToolTest").hidden = false;
  document.querySelector("#mcpTestTitle").textContent = `${server.alias} / ${tool.name}`;
  document.querySelector("#mcpTestSchema").textContent = JSON.stringify({
    ...toolTestBinding(), discovered_at: server.discovered_at,
    input: tool.input_schema, output: tool.output_schema,
  }, null, 2);
  document.querySelector("#mcpTestConfirm").checked = false;
  document.querySelector("#mcpTestResult").textContent = "";
  document.querySelector("#mcpTestApproval").hidden = true;
  document.querySelector("#mcpTestArguments").value = "{}";
  const fields = document.querySelector("#mcpTestFields");
  fields.replaceChildren();
  const properties = Object.entries(tool.input_schema.properties || {});
  const simple = tool.input_schema.type === "object" && properties.length > 0 && properties.every(
    ([, schema]) => ["string", "integer", "number", "boolean"].includes(schema.type),
  );
  for (const [name, schema] of simple ? properties : []) {
    const required = (tool.input_schema.required || []).includes(name);
    const label = element("label", "", `${name}${required ? " *" : ""}`);
    const choices = schema.enum || (schema.type === "boolean" ? [true, false] : null);
    const input = document.createElement(choices ? "select" : "input");
    input.dataset.name = name;
    input.dataset.type = schema.type;
    input.required = required;
    if (choices) {
      const empty = element("option", "", "Not set");
      empty.value = "";
      input.append(empty);
      for (const value of choices) {
        const option = element("option", "", String(value));
        option.value = JSON.stringify(value);
        input.append(option);
      }
      input.dataset.enum = "true";
    } else {
      input.type = ["integer", "number"].includes(schema.type) ? "number" : "text";
      if (schema.type === "number") input.step = "any";
      if (schema.minimum !== undefined) input.min = schema.minimum;
      if (schema.maximum !== undefined) input.max = schema.maximum;
    }
    label.append(input);
    fields.append(label);
  }
  document.querySelector("#mcpTestMode").value = simple ? "fields" : "json";
  document.querySelector("#mcpTestMode").options[0].disabled = !simple;
  updateTestMode();
  const current = toolTest;
  const reviews = await api(`/v1/admin/mcp/servers/${server.server_id}/tool-risk-reviews`);
  if (toolTest !== current) return;
  const review = reviews.find((item) => item.tool_name === tool.name &&
    item.expected_schema_digest === tool.schema_digest &&
    item.expected_server_config_digest === server.config_digest);
  document.querySelector("#mcpTestRisk").value = review?.risk || "";
  document.querySelector("#mcpTestReview").textContent = review ? "Reviewed" : "Unreviewed";
  await refreshToolTests();
}

function updateTestMode() {
  const jsonMode = document.querySelector("#mcpTestMode").value === "json";
  document.querySelector("#mcpTestFields").hidden = jsonMode;
  document.querySelector("#mcpTestJsonLabel").hidden = !jsonMode;
  for (const input of document.querySelectorAll("#mcpTestFields input, #mcpTestFields select")) {
    input.disabled = jsonMode;
  }
}

function testArguments() {
  if (document.querySelector("#mcpTestMode").value === "json") {
    const value = JSON.parse(document.querySelector("#mcpTestArguments").value);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Arguments must be a JSON object.");
    return value;
  }
  return Object.fromEntries([...document.querySelectorAll("#mcpTestFields input, #mcpTestFields select")]
    .filter((input) => input.value !== "")
    .map((input) => [input.dataset.name, input.dataset.enum ? JSON.parse(input.value) :
      ["integer", "number"].includes(input.dataset.type) ? Number(input.value) : input.value]));
}

function showToolTest(record) {
  toolTest.record = record;
  document.querySelector("#mcpTestResult").textContent = JSON.stringify(record, (key, value) =>
    key === "data" && typeof value === "string" && value.length > 2048 ? `[${value.length} characters omitted]` : value, 2);
  document.querySelector("#mcpTestApproval").hidden = record.status !== "awaiting-approval";
}

async function refreshToolTests() {
  if (!toolTest) return;
  const current = toolTest;
  const records = await api(`/v1/admin/mcp/servers/${current.server.server_id}/tool-tests`);
  if (current !== toolTest) return;
  const rows = records.filter((record) => record.tool_name === current.tool.name).reverse().map((record) => {
    const row = element("tr");
    row.append(element("td", "", record.created_at), element("td", "", record.status),
      rowActions(actionButton("View result", () => showToolTest(record))));
    return row;
  });
  document.querySelector("#mcpTestHistory").replaceChildren(makeTable(["Created", "Status", "Result"], rows));
  if (current.record) {
    const latest = records.find((record) => record.test_id === current.record.test_id);
    if (latest) showToolTest(latest);
  }
}

document.querySelector("#mcpTestMode").addEventListener("change", updateTestMode);
function closeToolTest() {
  document.querySelector("#mcpToolTest").hidden = true;
  toolTest = null;
}
document.querySelector("#closeMcpTest").addEventListener("click", closeToolTest);
document.querySelector("#newMcpTest").addEventListener("click", () => {
  if (!toolTest) return;
  toolTest.key = crypto.randomUUID();
  toolTest.record = null;
  document.querySelector("#mcpTestConfirm").checked = false;
  document.querySelector("#mcpTestResult").textContent = "";
  document.querySelector("#mcpTestApproval").hidden = true;
});

async function testAction(button, operation) {
  button.disabled = true;
  try { await operation(); } catch (error) { setNotice(error.message, "error"); }
  finally { button.disabled = false; }
}

document.querySelector("#refreshMcpTests").addEventListener("click", (event) => testAction(event.currentTarget, refreshToolTests));
document.querySelector("#reviewMcpTestRisk").addEventListener("click", (event) => testAction(event.currentTarget, async () => {
  if (!toolTest) return;
  const current = toolTest;
  const risk = document.querySelector("#mcpTestRisk").value;
  if (!risk) throw new Error("Select a reviewed risk classification.");
  await api(`/v1/admin/mcp/servers/${toolTest.server.server_id}/tool-risk-reviews`, {
    method: "POST", body: JSON.stringify({ ...toolTestBinding(), risk }),
  });
  if (toolTest === current) document.querySelector("#mcpTestReview").textContent = "Reviewed";
}));

document.querySelector("#mcpTestForm").addEventListener("submit", (event) => {
  event.preventDefault();
  testAction(document.querySelector("#runMcpTest"), async () => {
    if (!toolTest) return;
    const current = toolTest;
    const result = await api(`/v1/admin/mcp/servers/${current.server.server_id}/tool-tests`, {
      method: "POST", body: JSON.stringify({ ...toolTestBinding(), arguments: testArguments(),
        idempotency_key: current.key, confirm_write: document.querySelector("#mcpTestConfirm").checked,
        timeout_seconds: Number(document.querySelector("#mcpTestTimeout").value) }),
    });
    if (toolTest === current) { showToolTest(result); await refreshToolTests(); }
  });
});

for (const [selector, approve] of [["#approveMcpTest", true], ["#rejectMcpTest", false]]) {
  document.querySelector(selector).addEventListener("click", (event) => testAction(event.currentTarget, async () => {
    if (!toolTest?.record) return;
    const current = toolTest;
    const result = await api(`/v1/admin/mcp/servers/${current.server.server_id}/tool-tests/${current.record.test_id}/decision`, {
      method: "POST", body: JSON.stringify({ approve, reason: document.querySelector("#mcpTestReason").value.trim() }),
    });
    if (toolTest === current) showToolTest(result);
  }));
}

function checkboxList(targetId, records, idField, label) {
  const root = document.querySelector(`#${targetId}`);
  const selected = new Set([...root.querySelectorAll("input:checked")].map((input) => input.value));
  root.replaceChildren();
  for (const record of records) {
    const option = element("label", "check-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = record[idField];
    input.checked = selected.has(input.value);
    option.append(input, document.createTextNode(label(record)));
    root.append(option);
  }
}

function componentCheckboxList(targetId, components) {
  const root = document.querySelector(`#${targetId}`);
  root.replaceChildren();
  for (const component of components) {
    const option = element("label", "check-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = component.component_id;
    input.dataset.component = JSON.stringify(component);
    option.append(
      input,
      document.createTextNode(`${component.component_id} · ${component.version}`),
    );
    root.append(option);
  }
}

function renderSolutions() {
  const list = document.querySelector("#solutionList");
  list.replaceChildren();
  document.querySelector("#solutionCount").textContent = `${state.solutions.length} definitions`;
  for (const solution of state.solutions) {
    const item = element("div", "resource-item");
    item.append(
      element("strong", "", solution.title),
      element("span", "", `${solution.solution_id} · ${solution.owner}`),
      statusBadge(solution.status),
    );
    list.append(item);
  }

  const selector = document.querySelector("#releaseSolution");
  const selected = selector.value;
  selector.replaceChildren();
  for (const solution of state.solutions) {
    const option = document.createElement("option");
    option.value = solution.solution_id;
    option.textContent = `${solution.title} (${solution.solution_id})`;
    selector.append(option);
  }
  if ([...selector.options].some((option) => option.value === selected)) {
    selector.value = selected;
  }
  const entryAgent = document.querySelector("#solutionEntryAgent");
  const selectedEntryAgent = entryAgent.value;
  entryAgent.replaceChildren();
  const noEntryAgent = document.createElement("option");
  noEntryAgent.value = "";
  noEntryAgent.textContent = "No entry Agent";
  entryAgent.append(noEntryAgent);
  const activeGeneration = state.generations.find(
    (item) => item.generation_id === state.status?.active_generation_id,
  );
  for (const [agentId, definition] of Object.entries(activeGeneration?.agent_definitions || {})) {
    const option = document.createElement("option");
    option.value = agentId;
    option.textContent = `${definition.name} (${agentId}) · ${definition.version}`;
    entryAgent.append(option);
  }
  if ([...entryAgent.options].some((option) => option.value === selectedEntryAgent)) {
    entryAgent.value = selectedEntryAgent;
  }
  const evaluationRelease = document.querySelector("#evaluationRelease");
  const selectedEvaluationRelease = evaluationRelease.value;
  evaluationRelease.replaceChildren();
  for (const release of state.solutionReleases) {
    const option = document.createElement("option");
    option.value = release.release_id;
    option.textContent = `${release.solution_id} · ${release.version}`;
    evaluationRelease.append(option);
  }
  if ([...evaluationRelease.options].some(
    (option) => option.value === selectedEvaluationRelease,
  )) {
    evaluationRelease.value = selectedEvaluationRelease;
  }

  componentCheckboxList(
    "releaseAgents",
    state.agents
      .filter((record) => ["valid", "published"].includes(record.status) && record.validation.content_digest)
      .map((record) => ({
        kind: "agent",
        component_id: record.definition.agent_id,
        version: record.definition.version,
        content_digest: record.validation.content_digest,
      })),
  );
  componentCheckboxList(
    "releaseSkills",
    state.capabilities
      .filter((capability) => capability.executable)
      .map((capability) => ({
        kind: capability.capability_id.startsWith("tool:mcp:") ? "mcp-tool" : "skill",
        component_id: capability.capability_id.startsWith("tool:mcp:") ? capability.capability_id : capability.name,
        version: capability.version,
        content_digest: capability.content_digest,
      })),
  );

  const rows = state.solutionReleases.map((release) => {
    const row = element("tr");
    row.append(
      element("td", "", release.solution_id),
      element("td", "", release.version),
      element("td", "", String(release.components.length)),
      element("td", "", release.content_digest),
    );
    return row;
  });
  const releaseList = document.querySelector("#solutionReleaseList");
  releaseList.replaceChildren(makeTable(["Solution", "Version", "Components", "Digest"], rows));
}

function rowActions(...buttons) {
  const cell = element("td", "row-actions");
  cell.append(...buttons);
  return cell;
}

function renderCatalog() {
  document.querySelector("#candidateForm").hidden = environment === "development";
  document.querySelector("#candidateSection").hidden = environment === "development";
  document.querySelector("#approvalSection").hidden = environment === "development";
  const defaultAgent = document.querySelector("#candidateDefaultAgent");
  const selectedDefault = defaultAgent.value;
  defaultAgent.replaceChildren();
  const keepCurrent = document.createElement("option");
  keepCurrent.value = "";
  keepCurrent.textContent = "Keep current default";
  defaultAgent.append(keepCurrent);
  const seenAgentIds = new Set();
  const active = state.generations.find((item) => item.generation_id === state.status?.active_generation_id);
  for (const definition of Object.values(active?.agent_definitions || {})) {
    seenAgentIds.add(definition.agent_id);
    const option = document.createElement("option");
    option.value = definition.agent_id;
    option.textContent = `${definition.name} (${definition.agent_id})`;
    defaultAgent.append(option);
  }
  for (const record of state.agents) {
    const definition = record.definition;
    if (
      !["valid", "published"].includes(record.status) ||
      seenAgentIds.has(definition.agent_id)
    ) continue;
    seenAgentIds.add(definition.agent_id);
    const option = document.createElement("option");
    option.value = definition.agent_id;
    option.textContent = `${definition.name} (${definition.agent_id}) · ${definition.version}`;
    defaultAgent.append(option);
  }
  if ([...defaultAgent.options].some((option) => option.value === selectedDefault)) {
    defaultAgent.value = selectedDefault;
  }
  checkboxList(
    "candidateAgents",
    state.agents.filter((item) => item.status === "valid"),
    "draft_id",
    (item) => `${item.definition.name} · ${item.definition.version}`,
  );
  checkboxList(
    "candidateSkills",
    state.skills.filter((item) => item.status === "valid"),
    "package_id",
    (item) => `${item.name} · ${item.version}${item.executable ? "" : " · disabled"}`,
  );
  checkboxList(
    "candidateTools",
    state.tools,
    "registration_id",
    (item) => `${item.definition.binding.server_alias}/${item.definition.binding.tool_name} · ${item.definition.version} · ${item.status}`,
  );
  checkboxList(
    "candidateReleases",
    state.solutionReleases,
    "release_id",
    (item) => `${item.solution_id} · ${item.version} · ${item.components.length} components`,
  );
  renderCandidateTable();
  renderApprovalTable();
  renderGenerationTable();
}

function makeTable(headers, rows) {
  const table = element("table");
  const head = element("thead");
  const header = element("tr");
  for (const label of headers) header.append(element("th", "", label));
  head.append(header);
  const body = element("tbody");
  for (const row of rows) body.append(row);
  table.append(head, body);
  return table;
}

function renderCandidateTable() {
  const root = document.querySelector("#candidateList");
  root.replaceChildren();
  const rows = state.candidates.map((record) => {
    const row = element("tr");
    row.dataset.candidateId = record.candidate_id;
    const actions = [];
    if (environment === "development" && ["draft", "invalid", "valid", "approved"].includes(record.status)) {
      actions.push(actionButton("Publish", () => runAction(`/v1/admin/candidates/${record.candidate_id}/publish`, { method: "POST" }), "primary"));
    }
    if (["draft", "invalid", "valid"].includes(record.status)) {
      actions.push(actionButton("Validate", () => runAction(`/v1/admin/candidates/${record.candidate_id}/validate`, { method: "POST" })));
    }
    if (record.status === "valid") {
      actions.push(actionButton("Submit", () => runAction(`/v1/admin/candidates/${record.candidate_id}/submit`, { method: "POST" })));
    }
    if (record.status === "approved") {
      actions.push(actionButton("Activate", () => runAction(`/v1/admin/candidates/${record.candidate_id}/activate`, { method: "POST" }), "primary"));
    }
    const validation = element("td");
    renderValidation(validation, record.validation);
    row.append(
      element("td", "", record.title),
      element("td", "", record.base_generation_id),
      element("td", "", record.status === "active" && record.generation_id !== state.status?.active_generation_id
        ? "activated (historical)" : record.status),
      validation,
      element("td", "", record.diff_digest || "-"),
      rowActions(...actions),
    );
    return row;
  });
  root.append(makeTable(["Candidate", "Base", "Status", "Validation", "Diff digest", "Actions"], rows));
}

function renderApprovalTable() {
  const root = document.querySelector("#approvalList");
  root.replaceChildren();
  const rows = state.approvals.map((record) => {
    const row = element("tr");
    const actions = [];
    if (record.status === "pending") {
      const reason = document.createElement("input");
      reason.type = "text";
      reason.placeholder = "Decision reason";
      reason.setAttribute("aria-label", `Reason for ${record.approval_id}`);
      actions.push(
        reason,
        actionButton("Approve", async () => {
          const value = reason.value.trim();
          if (!value) return setNotice("Approval reason is required.", "error");
          await runAction(`/v1/admin/approvals/${record.approval_id}/approve`, {
            method: "POST",
            body: JSON.stringify({ reason: value }),
          });
        }, "primary"),
        actionButton("Reject", async () => {
          const value = reason.value.trim();
          if (!value) return setNotice("Rejection reason is required.", "error");
          await runAction(`/v1/admin/approvals/${record.approval_id}/reject`, {
            method: "POST",
            body: JSON.stringify({ reason: value }),
          });
        }, "danger"),
      );
    } else if (record.kind === "rollback" && record.status === "approved") {
      actions.push(actionButton("Execute rollback", () => runAction(`/v1/admin/approvals/${record.approval_id}/rollback`, { method: "POST" }), "danger"));
    }
    row.append(
      element("td", "", record.kind),
      element("td", "", record.target_id),
      element("td", "", record.status),
      element("td", "", record.actor_fingerprint || "-"),
      rowActions(...actions),
    );
    return row;
  });
  root.append(makeTable(["Kind", "Target", "Status", "Actor", "Actions"], rows));
}

function renderGenerationTable() {
  const root = document.querySelector("#generationList");
  root.replaceChildren();
  const rows = state.generations.map((record) => {
    const row = element("tr");
    const active = record.generation_id === state.status?.active_generation_id;
    const actions = [actionButton("Diff", async () => {
      try {
        const diff = await api(`/v1/admin/generations/${record.generation_id}/diff`);
        setNotice(JSON.stringify(diff), "success");
      } catch (error) {
        setNotice(error.message, "error");
      }
    })];
    if (!active) {
      actions.push(actionButton("Request rollback", () => runAction(`/v1/admin/generations/${record.generation_id}/rollback-request`, { method: "POST" }), "danger"));
    }
    row.append(
      element("td", "", record.generation_id),
      element("td", "", active ? "active" : "historical"),
      element("td", "", String(Object.keys(record.skill_versions).length)),
      element("td", "", String(Object.keys(record.agent_definitions).length)),
      element("td", "", String(Object.keys(record.solution_releases || {}).length)),
      rowActions(...actions),
    );
    return row;
  });
  root.append(
    makeTable(["Generation", "State", "Skills", "Agents", "Solutions", "Actions"], rows),
  );
}

function shortDigest(value) {
  if (!value) return "-";
  return value.length > 24 ? `${value.slice(0, 17)}…${value.slice(-6)}` : value;
}

function renderQualityMetrics() {
  const root = document.querySelector("#qualityMetrics");
  root.replaceChildren();
  const metrics = state.qualitySummary?.overall;
  if (!metrics) return;
  const values = [
    ["Feedback", metrics.feedback_count],
    ["Average rating", metrics.average_rating ?? "-"],
    ["Low ratings", metrics.negative_rating_count],
    ["Evaluation samples", metrics.evaluation_candidate_count],
  ];
  for (const [label, value] of values) {
    const metric = element("div", "metric");
    metric.append(element("span", "", label), element("strong", "", String(value)));
    root.append(metric);
  }

  const rows = [];
  for (const [group, slices] of [
    ["Release", state.qualitySummary.by_solution_release || []],
    ["Capability", state.qualitySummary.by_capability || []],
  ]) {
    for (const slice of slices) {
      const row = element("tr");
      row.append(
        element("td", "", group),
        element("td", "", slice.key),
        element("td", "", String(slice.metrics.feedback_count)),
        element("td", "", String(slice.metrics.average_rating ?? "-")),
        element("td", "", Object.entries(slice.metrics.category_counts || {})
          .map(([key, count]) => `${key}: ${count}`).join(" · ") || "-"),
      );
      rows.push(row);
    }
  }
  const breakdown = document.querySelector("#qualityBreakdown");
  breakdown.replaceChildren(
    rows.length
      ? makeTable(["Group", "Version / capability", "Feedback", "Rating", "Issues"], rows)
      : element("div", "empty", "No quality trend data."),
  );
}

function renderQualityDispositionList(records) {
  const root = document.querySelector("#qualityDispositionList");
  const rows = records.map((record) => {
    const row = element("tr");
    row.append(
      element("td", "", record.action),
      element("td", "", record.reason),
      element("td", "", record.actor_id),
      element("td", "", new Date(record.created_at).toLocaleString()),
    );
    return row;
  });
  root.replaceChildren(
    rows.length
      ? makeTable(["Action", "Reason", "Actor", "Created"], rows)
      : element("div", "empty", "No disposition recorded."),
  );
}

async function selectQualityFeedback(record) {
  state.selectedQualityFeedback = record;
  for (const button of document.querySelectorAll("#qualityFeedbackList .resource-item")) {
    button.classList.toggle("selected", button.dataset.feedbackId === record.feedback_id);
  }
  document.querySelector("#qualityFeedbackEditor").hidden = false;
  document.querySelector("#qualityFeedbackRating").textContent = record.rating
    ? `${record.rating} / 5` : "Unrated";
  const meta = document.querySelector("#qualityFeedbackMeta");
  meta.replaceChildren(
    element("code", "", record.feedback_id),
    element("div", "muted", [
      record.capability_id || record.agent_id,
      record.solution_release_id || "unreleased",
      record.categories.join(", ") || "no category",
    ].filter(Boolean).join(" · ")),
    element("div", "muted", `Result ${shortDigest(record.result_digest)}`),
  );
  document.querySelector("#qualityFeedbackComment").textContent =
    record.comment || "No overall comment.";
  document.querySelector("#qualityDispositionReason").value = "";
  document.querySelector("#qualityRubric").value = record.comment || "";
  document.querySelector("#qualityExpectedResult").value = "{}";
  document.querySelector("#qualityTags").value = record.categories.join(", ");
  const existing = state.evaluationCandidates.find(
    (candidate) => candidate.feedback_id === record.feedback_id,
  );
  const candidateForm = document.querySelector("#qualityCandidateForm");
  for (const frozen of document.querySelectorAll(".quality-frozen")) frozen.remove();
  candidateForm.hidden = !record.evaluation_use_allowed || Boolean(existing);
  if (existing) {
    candidateForm.insertAdjacentElement(
      "beforebegin",
      element("div", "notice success quality-frozen", `Frozen as ${existing.candidate_id}`),
    );
  }
  try {
    const dispositions = await api(
      `/v1/admin/feedback/${encodeURIComponent(record.feedback_id)}/dispositions`,
    );
    if (state.selectedQualityFeedback?.feedback_id === record.feedback_id) {
      renderQualityDispositionList(dispositions);
    }
  } catch (error) {
    setNotice(error.message, "error");
  }
}

function renderQualityFeedback() {
  const root = document.querySelector("#qualityFeedbackList");
  root.replaceChildren();
  document.querySelector("#qualityFeedbackCount").textContent =
    `${state.feedback.length} authorized`;
  for (const record of state.feedback) {
    const button = element("button", "resource-item");
    button.type = "button";
    button.dataset.feedbackId = record.feedback_id;
    button.classList.toggle(
      "selected",
      record.feedback_id === state.selectedQualityFeedback?.feedback_id,
    );
    button.append(
      element("strong", "", record.capability_id || record.agent_id || "Run result"),
      element("small", "", `${record.rating || "-"}/5 · ${record.categories.join(", ") || "feedback"}`),
      element("small", "", record.comment || record.feedback_id),
    );
    button.addEventListener("click", () => selectQualityFeedback(record));
    root.append(button);
  }
  if (!state.feedback.length) root.append(element("div", "empty", "No authorized feedback."));
}

function renderQualityCandidateOptions() {
  const root = document.querySelector("#qualityCandidateOptions");
  const checked = new Set(
    [...root.querySelectorAll("input:checked")].map((input) => input.value),
  );
  root.replaceChildren();
  document.querySelector("#qualityCandidateCount").textContent =
    `${state.evaluationCandidates.length} frozen samples`;
  for (const candidate of state.evaluationCandidates) {
    const label = element("label", "check-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = candidate.candidate_id;
    input.checked = checked.has(candidate.candidate_id);
    label.append(
      input,
      element("span", "", `${candidate.capability_id || candidate.agent_id} · ${candidate.candidate_id}`),
    );
    root.append(label);
  }
  if (!state.evaluationCandidates.length) {
    root.append(element("div", "empty", "Freeze authorized feedback to create samples."));
  }
}

function renderQualityComparisons() {
  const root = document.querySelector("#qualityComparisonList");
  const rows = state.qualityComparisons.map((record) => {
    const row = element("tr");
    row.append(
      element("td", "", record.evaluator_id),
      element("td", "", record.status),
      element("td", "", String(record.metrics.baseline_average)),
      element("td", "", String(record.metrics.candidate_average)),
      element("td", "", String(record.metrics.score_delta)),
      element("td", "", shortDigest(record.candidate_target_digest)),
    );
    return row;
  });
  root.replaceChildren(
    rows.length
      ? makeTable(["Evaluator", "Trend", "Baseline", "Candidate", "Delta", "Target"], rows)
      : element("div", "empty", "No comparison imported."),
  );
}

async function selectQualitySampleSet(record) {
  state.selectedSampleSet = record;
  document.querySelector("#qualityComparisonPanel").hidden = false;
  document.querySelector("#qualitySampleSetMeta").textContent =
    `${record.target_id} · ${record.version} · ${record.samples.length} samples`;
  document.querySelector("#qualityOutcomes").value = JSON.stringify(
    record.samples.map((sample) => ({
      candidate_id: sample.candidate_id,
      baseline_score: 0,
      candidate_score: 0,
      baseline_result_digest: sample.result_digest,
      candidate_result_digest: sample.result_digest,
      notes: "",
    })),
    null,
    2,
  );
  try {
    state.qualityComparisons = await api(
      `/v1/admin/evaluation-sample-sets/${encodeURIComponent(record.sample_set_id)}/comparisons`,
    );
    if (state.selectedSampleSet?.sample_set_id === record.sample_set_id) {
      renderQualityComparisons();
    }
  } catch (error) {
    setNotice(error.message, "error");
  }
}

function renderQualitySampleSets() {
  const root = document.querySelector("#qualitySampleSetList");
  const rows = state.sampleSets.map((record) => {
    const row = element("tr");
    row.append(
      element("td", "", record.title),
      element("td", "", record.version),
      element("td", "", record.target_id),
      element("td", "", String(record.samples.length)),
      element("td", "", shortDigest(record.content_digest)),
      rowActions(actionButton("Compare", () => selectQualitySampleSet(record))),
    );
    return row;
  });
  root.replaceChildren(
    rows.length
      ? makeTable(["Sample set", "Version", "Target", "Samples", "Digest", "Actions"], rows)
      : element("div", "empty", "No versioned sample set."),
  );
}

function renderQuality() {
  renderQualityMetrics();
  renderQualityFeedback();
  renderQualityCandidateOptions();
  renderQualitySampleSets();
  if (state.selectedQualityFeedback) {
    const refreshed = state.feedback.find(
      (record) => record.feedback_id === state.selectedQualityFeedback.feedback_id,
    );
    if (refreshed) void selectQualityFeedback(refreshed);
  }
  if (state.selectedSampleSet) {
    const refreshed = state.sampleSets.find(
      (record) => record.sample_set_id === state.selectedSampleSet.sample_set_id,
    );
    if (refreshed) void selectQualitySampleSet(refreshed);
  }
}

async function loadAll() {
  try {
    const [status, solutions, solutionReleases, capabilities, agents, skills, mcpServers, candidates, approvals, generations, tools, feedback, qualitySummary, evaluationCandidates, sampleSets] = await Promise.all([
      api("/v1/admin/status"),
      api("/v1/admin/solutions"),
      api("/v1/admin/solution-releases"),
      api("/v1/admin/capabilities"),
      api("/v1/admin/agents"),
      api("/v1/admin/skills"),
      api("/v1/admin/mcp/servers"),
      api("/v1/admin/candidates"),
      api("/v1/admin/approvals"),
      api("/v1/admin/generations"),
      api("/v1/admin/tools"),
      api("/v1/admin/feedback"),
      api("/v1/admin/feedback/quality-summary"),
      api("/v1/admin/evaluation-sample-candidates"),
      api("/v1/admin/evaluation-sample-sets"),
    ]);
    Object.assign(state, {
      status,
      solutions,
      solutionReleases,
      capabilities,
      agents,
      skills,
      mcpServers,
      candidates,
      approvals,
      generations,
      tools,
      feedback,
      qualitySummary,
      evaluationCandidates,
      sampleSets,
    });
    connectionState.textContent = "Connected";
    connectionState.className = "status-badge success";
    renderOverview();
    renderSolutions();
    renderAgents();
    renderSkills();
    renderMcpServers();
    renderCatalog();
    renderQuality();
    if (state.selectedAgent) {
      const refreshed = state.agents.find((item) => item.draft_id === state.selectedAgent.draft_id);
      if (refreshed) selectAgent(refreshed);
    } else {
      renderCapabilityOptions();
    }
    if (state.selectedMcp) {
      const refreshed = state.mcpServers.find((item) => item.server_id === state.selectedMcp.server_id);
      if (refreshed) selectMcp(refreshed);
    }
    setNotice("Control plane refreshed.", "success");
    window.dispatchEvent(new Event("sage:control-loaded"));
  } catch (error) {
    connectionState.textContent = "Disconnected";
    connectionState.className = "status-badge danger";
    setNotice(error.message, "error");
  }
}

async function runAction(path, options) {
  try {
    await api(path, options);
    await loadAll();
    return true;
  } catch (error) {
    await loadAll();
    setNotice(error.message, "error");
    return false;
  }
}

document.querySelector("#connectAdmin").addEventListener("click", () => {
  sessionStorage.setItem(storageKey, tokenInput.value);
  loadAll();
});

document.querySelectorAll("[data-refresh]").forEach((button) => button.addEventListener("click", loadAll));
document.querySelector("#newAgent").addEventListener("click", clearAgentEditor);
document.querySelector("#newMcp").addEventListener("click", clearMcpEditor);

document.querySelector("#qualityDispositionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const feedback = state.selectedQualityFeedback;
  const reason = document.querySelector("#qualityDispositionReason").value.trim();
  if (!feedback || !reason) {
    setNotice("Select feedback and enter a disposition reason.", "error");
    return;
  }
  try {
    await api(`/v1/admin/feedback/${encodeURIComponent(feedback.feedback_id)}/dispositions`, {
      method: "POST",
      body: JSON.stringify({
        action: document.querySelector("#qualityDispositionAction").value,
        reason,
      }),
    });
    await loadAll();
    setNotice("Feedback disposition recorded.", "success");
  } catch (error) {
    setNotice(error.message, "error");
  }
});

document.querySelector("#qualityCandidateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const feedback = state.selectedQualityFeedback;
  if (!feedback) {
    setNotice("Select authorized feedback before freezing a sample.", "error");
    return;
  }
  let expectedResult;
  try {
    expectedResult = JSON.parse(document.querySelector("#qualityExpectedResult").value || "null");
  } catch {
    setNotice("Expected result JSON is invalid.", "error");
    return;
  }
  if (expectedResult !== null && (Array.isArray(expectedResult) || typeof expectedResult !== "object")) {
    setNotice("Expected result must be a JSON object or null.", "error");
    return;
  }
  const tags = document.querySelector("#qualityTags").value
    .split(",").map((value) => value.trim()).filter(Boolean);
  try {
    await api(
      `/v1/admin/feedback/${encodeURIComponent(feedback.feedback_id)}/evaluation-candidates`,
      {
        method: "POST",
        body: JSON.stringify({
          rubric: document.querySelector("#qualityRubric").value.trim(),
          expected_result: expectedResult,
          tags,
        }),
      },
    );
    await loadAll();
    setNotice("Feedback frozen as an evaluation sample.", "success");
  } catch (error) {
    setNotice(error.message, "error");
  }
});

document.querySelector("#qualitySampleSetForm").addEventListener("input", (event) => {
  if (!event.currentTarget.dataset.busy) delete event.currentTarget.dataset.idempotencyKey;
});

document.querySelector("#qualitySampleSetForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const candidateIds = [...document.querySelectorAll("#qualityCandidateOptions input:checked")]
    .map((input) => input.value);
  if (!candidateIds.length) {
    setNotice("Select at least one frozen sample.", "error");
    return;
  }
  form.dataset.idempotencyKey ||= crypto.randomUUID();
  form.dataset.busy = "true";
  try {
    const record = await api("/v1/admin/evaluation-sample-sets", {
      method: "POST",
      headers: { "Idempotency-Key": form.dataset.idempotencyKey },
      body: JSON.stringify({
        version: document.querySelector("#qualitySampleSetVersion").value.trim(),
        title: document.querySelector("#qualitySampleSetTitle").value.trim(),
        description: document.querySelector("#qualitySampleSetDescription").value.trim(),
        candidate_ids: candidateIds,
      }),
    });
    state.selectedSampleSet = record;
    delete form.dataset.idempotencyKey;
    form.reset();
    document.querySelector("#qualitySampleSetVersion").value = "1.0.0";
    await loadAll();
    setNotice("Versioned evaluation sample set created.", "success");
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    delete form.dataset.busy;
  }
});

document.querySelector("#qualityComparisonForm").addEventListener("input", (event) => {
  if (!event.currentTarget.dataset.busy) delete event.currentTarget.dataset.idempotencyKey;
});

document.querySelector("#qualityComparisonForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const sampleSet = state.selectedSampleSet;
  if (!sampleSet) {
    setNotice("Select a sample set before importing a comparison.", "error");
    return;
  }
  let outcomes;
  try {
    outcomes = JSON.parse(document.querySelector("#qualityOutcomes").value || "[]");
  } catch {
    setNotice("Comparison outcomes JSON is invalid.", "error");
    return;
  }
  if (!Array.isArray(outcomes)) {
    setNotice("Comparison outcomes must be a JSON array.", "error");
    return;
  }
  form.dataset.idempotencyKey ||= crypto.randomUUID();
  form.dataset.busy = "true";
  try {
    await api(
      `/v1/admin/evaluation-sample-sets/${encodeURIComponent(sampleSet.sample_set_id)}/comparisons`,
      {
        method: "POST",
        headers: { "Idempotency-Key": form.dataset.idempotencyKey },
        body: JSON.stringify({
          baseline_target_digest: document.querySelector("#qualityBaselineDigest").value.trim(),
          candidate_target_digest: document.querySelector("#qualityCandidateDigest").value.trim(),
          evaluator_id: document.querySelector("#qualityEvaluatorId").value.trim(),
          evaluator_version: document.querySelector("#qualityEvaluatorVersion").value.trim(),
          outcomes,
        }),
      },
    );
    delete form.dataset.idempotencyKey;
    await selectQualitySampleSet(sampleSet);
    setNotice("Comparison evidence imported.", "success");
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    delete form.dataset.busy;
  }
});

function openSolutionCreator() {
  document.querySelector("#solutionDefinitions").open = true;
  document.querySelector("#solutionForm").scrollIntoView({ block: "center" });
  document.querySelector("#solutionId").focus({ preventScroll: true });
}

document.querySelector("#newSolution").addEventListener("click", openSolutionCreator);

document.querySelector("#solutionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  if (form.dataset.busy === "true") return;
  form.dataset.busy = "true";
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  const solutionId = document.querySelector("#solutionId").value.trim();
  const profiles = [...document.querySelectorAll("#solutionProfiles input:checked")]
    .map((input) => input.value);
  const allowedRoles = document.querySelector("#solutionRoles").value
    .split(",")
    .map((role) => role.trim())
    .filter(Boolean);
  const succeeded = await runAction("/v1/admin/solutions", {
    method: "POST",
    body: JSON.stringify({
      solution_id: solutionId,
      title: document.querySelector("#solutionTitle").value.trim(),
      description: document.querySelector("#solutionDescription").value.trim(),
      owner: document.querySelector("#solutionOwner").value.trim(),
      entry_agent_id: document.querySelector("#solutionEntryAgent").value.trim() || null,
      allowed_roles: allowedRoles,
      supported_profiles: profiles,
    }),
  });
  form.dataset.busy = "false";
  submit.disabled = false;
  if (succeeded) {
    form.reset();
    window.dispatchEvent(new CustomEvent("sage:solution-created", { detail: { solutionId } }));
  }
});

document.querySelector("#solutionImportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.querySelector("#solutionManifest").files?.[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    await api("/v1/admin/solutions/imports", { method: "POST", body });
    event.target.reset();
    await loadAll();
  } catch (error) {
    setNotice(error.message, "error");
  }
});

document.querySelector("#solutionReleaseForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const selected = [...document.querySelectorAll(
    "#releaseAgents input:checked, #releaseSkills input:checked",
  )].map((input) => JSON.parse(input.dataset.component));
  let additional;
  try {
    additional = JSON.parse(document.querySelector("#releaseAdditionalComponents").value || "[]");
  } catch {
    return setNotice("Additional component locks JSON is invalid.", "error");
  }
  if (!Array.isArray(additional)) {
    return setNotice("Additional component locks must be a JSON array.", "error");
  }
  const solutionId = document.querySelector("#releaseSolution").value;
  await runAction(`/v1/admin/solutions/${encodeURIComponent(solutionId)}/releases`, {
    method: "POST",
    body: JSON.stringify({
      version: document.querySelector("#releaseVersion").value.trim(),
      components: [...selected, ...additional],
      source_generation_id: state.status.active_generation_id,
    }),
  });
});

document.querySelector("#evaluationForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  let metrics;
  try {
    metrics = JSON.parse(document.querySelector("#evaluationMetrics").value || "{}");
  } catch {
    return setNotice("Evaluation metrics JSON is invalid.", "error");
  }
  const releaseId = document.querySelector("#evaluationRelease").value;
  await runAction(
    `/v1/admin/solution-releases/${encodeURIComponent(releaseId)}/evaluations`,
    {
      method: "POST",
      body: JSON.stringify({
        suite_id: document.querySelector("#evaluationSuite").value.trim(),
        status: document.querySelector("#evaluationStatus").value,
        metrics,
        summary: document.querySelector("#evaluationSummary").value.trim(),
      }),
    },
  );
});

document.querySelector("#agentForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const draftId = document.querySelector("#agentDraftId").value;
    const record = await api(draftId ? `/v1/admin/agents/${draftId}` : "/v1/admin/agents", {
      method: draftId ? "PUT" : "POST",
      body: JSON.stringify(agentPayload()),
    });
    state.selectedAgent = record;
    await loadAll();
    if (environment === "development") {
      await quickPublishResource(
        "candidateAgents",
        record.draft_id,
        `${record.definition.name} ${record.definition.version}`,
      );
    }
  } catch (error) {
    setNotice(error.message, "error");
  }
});

document.querySelector("#validateAgent").addEventListener("click", async () => {
  const draftId = document.querySelector("#agentDraftId").value;
  if (!draftId) return setNotice("Save the Agent draft before validation.", "error");
  await runAction(`/v1/admin/agents/${draftId}/validate`, { method: "POST" });
});

document.querySelector("#cloneAgent").addEventListener("click", async () => {
  const draftId = document.querySelector("#agentDraftId").value;
  const version = document.querySelector("#agentVersion").value.trim();
  if (!draftId) return setNotice("Select a published Agent draft to clone.", "error");
  try {
    const record = await api(`/v1/admin/agents/${draftId}/clone`, {
      method: "POST",
      body: JSON.stringify({ version }),
    });
    state.selectedAgent = record;
    await loadAll();
  } catch (error) {
    setNotice(error.message, "error");
  }
});

document.querySelector("#skillUploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.querySelector("#skillZip").files?.[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    const record = await api("/v1/admin/skills/uploads", { method: "POST", body });
    event.target.reset();
    await loadAll();
    if (environment === "development") {
      await quickPublishResource(
        "candidateSkills",
        record.package_id,
        `${record.name || record.original_filename} ${record.version || ""}`,
      );
    }
  } catch (error) {
    setNotice(error.message, "error");
  }
});

document.querySelector("#mcpForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const serverId = document.querySelector("#mcpServerId").value;
  const payload = {
    alias: document.querySelector("#mcpAlias").value.trim(),
    transport: document.querySelector("#mcpTransport").value,
    url: document.querySelector("#mcpUrl").value.trim() || null,
    command_preset: document.querySelector("#mcpPreset").value.trim() || null,
    secret_ref: document.querySelector("#mcpSecretRef").value.trim() || null,
    dev_secret: document.querySelector("#mcpDevSecret").value || null,
  };
  try {
    const record = await api(serverId ? `/v1/admin/mcp/servers/${serverId}` : "/v1/admin/mcp/servers", {
      method: serverId ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    state.selectedMcp = record;
    await loadAll();
  } catch (error) {
    setNotice(error.message, "error");
  }
});

document.querySelector("#testMcp").addEventListener("click", async () => {
  const id = document.querySelector("#mcpServerId").value;
  if (!id) return setNotice("Save the MCP server before testing.", "error");
  await runAction(`/v1/admin/mcp/servers/${id}/test`, { method: "POST" });
});

document.querySelector("#discoverMcp").addEventListener("click", async () => {
  const id = document.querySelector("#mcpServerId").value;
  if (!id) return setNotice("Save and test the MCP server before discovery.", "error");
  await runAction(`/v1/admin/mcp/servers/${id}/discover`, { method: "POST" });
});

document.querySelector("#mcpImportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const serverId = document.querySelector("#mcpServerId").value;
  const toolName = document.querySelector("#mcpToolName").value;
  if (!serverId || !toolName) return setNotice("Select a discovered MCP Tool.", "error");
  try {
    const record = await api(`/v1/admin/mcp/servers/${serverId}/tools/${encodeURIComponent(toolName)}/import`, {
    method: "POST",
    body: JSON.stringify({
      skill_name: document.querySelector("#mcpSkillName").value.trim(),
      version: document.querySelector("#mcpSkillVersion").value.trim(),
      description: document.querySelector("#mcpSkillDescription").value.trim(),
      instructions: document.querySelector("#mcpSkillInstructions").value.trim(),
      risk: document.querySelector("#mcpSkillRisk").value,
    }),
    });
    await loadAll();
    if (environment === "development") {
      await quickPublishResource("candidateSkills", record.package_id, `${record.name} ${record.version}`);
    } else {
      preparePublication("candidateSkills", record.package_id, `${record.name} ${record.version}`);
    }
  } catch (error) {
    setNotice(error.message, "error");
  }
});

document.querySelector("#registerMcpTool").addEventListener("click", async (event) => {
  const serverId = document.querySelector("#mcpServerId").value;
  const toolName = document.querySelector("#mcpToolName").value;
  if (!serverId || !toolName) return setNotice("Select a discovered MCP Tool.", "error");
  event.target.disabled = true;
  try {
    const record = await api(`/v1/admin/mcp/servers/${serverId}/tool-registrations`, {
      method: "POST", body: JSON.stringify({
        tool_name: toolName,
        version: document.querySelector("#mcpSkillVersion").value.trim(),
        description: document.querySelector("#mcpSkillDescription").value.trim(),
        risk: document.querySelector("#mcpSkillRisk").value,
      }),
    });
    await loadAll();
    if (environment === "development") {
      await quickPublishResource("candidateTools", record.registration_id, `${toolName} ${record.definition.version}`);
    } else {
      preparePublication("candidateTools", record.registration_id, `${toolName} ${record.definition.version}`);
    }
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    event.target.disabled = false;
  }
});

document.querySelector("#candidateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.target.dataset.busy === "true") return;
  const selected = (id) => [...document.querySelectorAll(`#${id} input:checked`)].map((item) => item.value);
  const defaultAgentId = document.querySelector("#candidateDefaultAgent").value.trim() || null;
  const agentDraftIds = selected("candidateAgents");
  const active = state.generations.find((item) => item.generation_id === state.status?.active_generation_id);
  if (defaultAgentId && !active?.agent_definitions?.[defaultAgentId]) {
    const matches = state.agents.filter((item) => ["valid", "published"].includes(item.status) && item.definition.agent_id === defaultAgentId);
    if (!matches.some((item) => agentDraftIds.includes(item.draft_id))) {
      if (matches.length !== 1) return setNotice("Select the draft version for the default Agent.", "error");
      agentDraftIds.push(matches[0].draft_id);
    }
  }
  event.target.dataset.busy = "true";
  const submit = event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  const succeeded = await runAction("/v1/admin/candidates", {
    method: "POST",
    body: JSON.stringify({
      title: document.querySelector("#candidateTitle").value.trim(),
      description: document.querySelector("#candidateDescription").value.trim(),
      default_agent_id: defaultAgentId,
      agent_draft_ids: agentDraftIds,
      skill_package_ids: selected("candidateSkills"),
      tool_registration_ids: selected("candidateTools"),
      solution_release_ids: selected("candidateReleases"),
    }),
  });
  event.target.dataset.busy = "false";
  submit.disabled = false;
  if (succeeded) event.target.reset();
});

async function initializeAdmin() {
  clearAgentEditor();
  clearMcpEditor();
  try {
    const [authResponse, bootstrapResponse] = await Promise.all([
      fetch("/v1/auth/config", { cache: "no-store" }),
      fetch("/v1/control/bootstrap", { cache: "no-store" }),
    ]);
    if (!authResponse.ok || !bootstrapResponse.ok) {
      throw new Error("Unable to load control-plane authentication settings");
    }
    const auth = await authResponse.json();
    const bootstrap = await bootstrapResponse.json();
    environment = bootstrap.environment;
    authenticationMode = auth.mode;
    if (!bootstrap.admin_auth_enabled) {
      authPanel.hidden = true;
      await loadAll();
      connectionState.textContent = "Admin auth disabled";
      connectionState.className = "status-badge warning";
      setNotice("Administration authentication is temporarily disabled.", "warning");
      return;
    }
    if (authenticationMode === "entra-id") {
      authPanel.hidden = true;
      if (!token()) {
        connectionState.textContent = "Sign in required";
        connectionState.className = "status-badge warning";
        setNotice(
          "Sign in from the Runtime page with an administration app role, then return here.",
          "warning",
        );
        return;
      }
      await loadAll();
      return;
    }
    if (bootstrap.auto_admin && bootstrap.admin_token) {
      sessionStorage.setItem(storageKey, bootstrap.admin_token);
      tokenInput.value = bootstrap.admin_token;
      authPanel.hidden = true;
      await loadAll();
      connectionState.textContent = "Development auto-auth";
      connectionState.className = "status-badge warning";
      setNotice("Development mode uses the shared local administration token.", "warning");
      return;
    }
    const savedToken = sessionStorage.getItem(storageKey);
    if (savedToken) {
      tokenInput.value = savedToken;
      await loadAll();
    }
  } catch (error) {
    setNotice(error.message, "error");
  }
}

initializeAdmin();
