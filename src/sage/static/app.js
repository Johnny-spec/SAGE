const messages = document.querySelector("#messages");
const events = document.querySelector("#events");
const composer = document.querySelector("#composer");
const messageInput = document.querySelector("#messageInput");
const attachmentInput = document.querySelector("#attachment");
const attachmentName = document.querySelector("#attachmentName");
const solutionSelect = document.querySelector("#solutionSelect");
const modeSelect = document.querySelector("#modeSelect");
const skillSelect = document.querySelector("#skillSelect");
const agentSelect = document.querySelector("#agentSelect");
const argumentsControl = document.querySelector("#argumentsControl");
const argumentsInput = document.querySelector("#argumentsInput");
const sendButton = document.querySelector("#sendButton");
const serviceStatus = document.querySelector("#serviceStatus");
const signInButton = document.querySelector("#signInButton");
const threadLabel = document.querySelector("#threadLabel");
const skillOverview = document.querySelector("#skillOverview");
const skillCatalog = document.querySelector("#skillCatalog");
const skillCount = document.querySelector("#skillCount");
const solutionWorkspace = document.querySelector("#solutionWorkspace");
const solutionWorkspaceTitle = document.querySelector("#solutionWorkspaceTitle");
const solutionWidgets = document.querySelector("#solutionWidgets");
const threadStorageKey = "sage.thread-id";
const solutionStorageKey = "sage.solution-id";
const authTokenKey = "sage.access-token";
const authExpiryKey = "sage.access-token-expiry";
const pkceVerifierKey = "sage.pkce-verifier";
const oauthStateKey = "sage.oauth-state";
let threadId = localStorage.getItem(threadStorageKey) || createThreadId();
let activeRunView = null;
let activeSolutions = [];
let authConfig = { mode: "anonymous" };
let durableRunId = sessionStorage.getItem("sage.durable-run") || "";
let durableRequest = null;

async function durableJson(path, body) {
  const response = await authenticatedFetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await responseError(response, "Durable request failed"));
  return response.json();
}

async function refreshDurable() {
  if (!durableRunId) return;
  const runId = durableRunId;
  const run = await durableJson(`/v1/runs/${encodeURIComponent(runId)}`);
  const query = run.solution_id ? `?solution_id=${encodeURIComponent(run.solution_id)}` : "";
  const tasks = await durableJson(`/v1/human-tasks${query}`);
  if (runId !== durableRunId) return;
  document.querySelector("#durableRun").hidden = false;
  document.querySelector("#durableStatus").textContent = `${run.status} · ${runId}`;
  document.querySelector("#cancelDurable").disabled = run.status !== "running";
  const list = document.querySelector("#durableTasks");
  list.replaceChildren();
  for (const task of tasks.filter((item) => item.run_id === runId ||
    item.subject?.root_run_id === runId)) {
    const section = document.createElement("section");
    const title = document.createElement("h3");
    title.textContent = task.title;
    const subject = document.createElement("pre");
    subject.textContent = JSON.stringify(task.subject, null, 2);
    const reason = document.createElement("textarea");
    reason.placeholder = "Decision reason";
    reason.setAttribute("aria-label", "Decision reason");
    reason.maxLength = 4000;
    section.append(title, subject, reason);
    for (const approve of [true, false]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = approve ? "Approve" : "Reject";
      button.disabled = run.status !== "running";
      button.addEventListener("click", async () => {
        if (!reason.value.trim()) { reason.focus(); return; }
        const buttons = section.querySelectorAll("button");
        buttons.forEach((item) => { item.disabled = true; });
        try {
          await durableJson(`/v1/human-tasks/${encodeURIComponent(task.task_id)}/decision`, {
            approve, reason: reason.value.trim(),
          });
          await refreshDurable();
        } catch (error) {
          addMessage("error", error.message);
          buttons.forEach((item) => { item.disabled = false; });
        }
      });
      section.append(button);
    }
    list.append(section);
  }
  const result = document.querySelector("#durableResult");
  result.textContent = "";
  if (run.status === "completed") {
    result.textContent = JSON.stringify(await durableJson(`/v1/durable-runs/${encodeURIComponent(runId)}/result`), null, 2);
  } else if (run.status === "failed") {
    const history = await durableJson(`/v1/runs/${encodeURIComponent(runId)}/events`);
    result.textContent = JSON.stringify(history.filter((item) => item.name === "run.failed"), null, 2);
  }
}

for (const [selector, action] of [
  ["#refreshDurable", refreshDurable],
  ["#cancelDurable", async () => {
    await durableJson(`/v1/durable-runs/${encodeURIComponent(durableRunId)}/cancel`, {});
    await refreshDurable();
  }],
]) {
  document.querySelector(selector).addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try { await action(); }
    catch (error) { addMessage("error", error.message); }
    finally { document.querySelector(selector).disabled = false; }
  });
}

function base64Url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function randomValue(length) {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

function activeAccessToken() {
  const token = sessionStorage.getItem(authTokenKey);
  const expiry = Number(sessionStorage.getItem(authExpiryKey) || 0);
  if (!token || expiry <= Date.now()) {
    sessionStorage.removeItem(authTokenKey);
    sessionStorage.removeItem(authExpiryKey);
    return null;
  }
  return token;
}

async function authenticatedFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = activeAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && authConfig.mode === "entra-id") {
    sessionStorage.removeItem(authTokenKey);
    sessionStorage.removeItem(authExpiryKey);
    signInButton.hidden = false;
    signInButton.textContent = "Sign in";
  }
  return response;
}

async function initializeAuthentication() {
  const configResponse = await fetch("/v1/auth/config", { cache: "no-store" });
  if (!configResponse.ok) throw new Error("Unable to load authentication settings");
  authConfig = await configResponse.json();
  if (authConfig.mode !== "entra-id") {
    signInButton.hidden = true;
    return;
  }
  const query = new URLSearchParams(location.search);
  if (query.has("error")) {
    history.replaceState({}, "", location.pathname);
    throw new Error(query.get("error_description") || query.get("error"));
  }
  if (query.has("code")) {
    const expectedState = sessionStorage.getItem(oauthStateKey);
    const verifier = sessionStorage.getItem(pkceVerifierKey);
    if (!expectedState || query.get("state") !== expectedState || !verifier) {
      throw new Error("The sign-in response state is invalid");
    }
    const redirectUri = `${location.origin}${location.pathname}`;
    const form = new URLSearchParams({
      client_id: authConfig.client_id,
      grant_type: "authorization_code",
      code: query.get("code"),
      redirect_uri: redirectUri,
      code_verifier: verifier,
      scope: `openid profile ${authConfig.scope}`,
    });
    const tokenResponse = await fetch(
      `https://login.microsoftonline.com/${encodeURIComponent(authConfig.tenant_id)}/oauth2/v2.0/token`,
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: form,
      },
    );
    if (!tokenResponse.ok) throw new Error("Unable to exchange the sign-in code");
    const token = await tokenResponse.json();
    sessionStorage.setItem(authTokenKey, token.access_token);
    sessionStorage.setItem(
      authExpiryKey,
      String(Date.now() + Math.max(0, Number(token.expires_in) - 60) * 1000),
    );
    sessionStorage.removeItem(oauthStateKey);
    sessionStorage.removeItem(pkceVerifierKey);
    history.replaceState({}, "", location.pathname);
  }
  signInButton.hidden = false;
  signInButton.textContent = activeAccessToken() ? "Sign out" : "Sign in";
}

async function startSignIn() {
  if (!authConfig.tenant_id || !authConfig.client_id || !authConfig.scope) {
    throw new Error("Entra browser authentication is not fully configured");
  }
  const verifier = randomValue(48);
  const state = randomValue(24);
  const challengeBytes = new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)),
  );
  sessionStorage.setItem(pkceVerifierKey, verifier);
  sessionStorage.setItem(oauthStateKey, state);
  const redirectUri = `${location.origin}${location.pathname}`;
  const authorize = new URL(
    `https://login.microsoftonline.com/${encodeURIComponent(authConfig.tenant_id)}/oauth2/v2.0/authorize`,
  );
  authorize.search = new URLSearchParams({
    client_id: authConfig.client_id,
    response_type: "code",
    redirect_uri: redirectUri,
    response_mode: "query",
    scope: `openid profile ${authConfig.scope}`,
    code_challenge: base64Url(challengeBytes),
    code_challenge_method: "S256",
    state,
  });
  location.assign(authorize);
}
const routeChoices = new Map();

function createThreadId() {
  const value = globalThis.crypto?.randomUUID?.() || Date.now().toString(36);
  return `thr_${value}`;
}

function updateThread(value) {
  threadId = value;
  localStorage.setItem(threadStorageKey, threadId);
  threadLabel.textContent = `Thread ${threadId}`;
}

function addMessage(role, text, result = null) {
  const element = document.createElement("div");
  element.className = `message ${role}`;
  element.append(document.createTextNode(text));
  if (result !== null) {
    const details = document.createElement("details");
    details.className = "result-details";
    const summary = document.createElement("summary");
    summary.textContent = "Structured result";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(result, null, 2);
    details.append(summary, pre);
    element.append(details);
  }
  messages.append(element);
  messages.scrollTop = messages.scrollHeight;
  return element;
}

function beginRunView() {
  const root = document.createElement("section");
  root.className = "run-view";
  root.setAttribute("aria-label", "Current Skill execution");

  const header = document.createElement("div");
  header.className = "run-view-header";
  const heading = document.createElement("strong");
  heading.textContent = "Current run";
  const status = document.createElement("span");
  status.className = "run-status active";
  status.textContent = "Preparing";
  header.append(heading, status);

  const current = document.createElement("p");
  current.className = "run-current";
  current.textContent = "Staging the request";
  const steps = document.createElement("ol");
  steps.className = "run-steps";
  root.append(header, current, steps);
  messages.append(root);
  messages.scrollTop = messages.scrollHeight;
  return { root, status, current, steps, items: new Map() };
}

function setRunStep(view, key, titleText, detailText, state = "active") {
  let item = view.items.get(key);
  if (!item) {
    const root = document.createElement("li");
    root.className = "run-step";
    const marker = document.createElement("span");
    marker.className = "run-step-marker";
    const body = document.createElement("div");
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    body.append(title, detail);
    root.append(marker, body);
    view.steps.append(root);
    item = { root, title, detail };
    view.items.set(key, item);
  }
  item.root.className = `run-step ${state}`;
  item.title.textContent = titleText;
  item.detail.textContent = detailText || "";
  messages.scrollTop = messages.scrollHeight;
}

function toolActivity(tool) {
  const descriptions = {
    "iis-logs": "Parsing W3C requests, status codes, latency, and traffic patterns",
    netlog: "Inspecting browser authentication, TLS, DNS, proxy, and request failures",
    "report-summary": "Turning structured evidence into findings and next actions",
    "skills-collection": "Coordinating imported IIS and Edge diagnostic delegates",
    echo: "Returning the supplied message",
  };
  return descriptions[tool] || "Executing the governed Skill";
}

function updateRunView(name, data) {
  const view = activeRunView;
  if (!view) return;

  if (name === "attachment.uploaded") {
    setRunStep(view, "evidence", "Evidence ready", data.filename, "completed");
  } else if (name === "run.started") {
    view.status.textContent = "Running";
    view.current.textContent = "Starting the Agent workflow";
  } else if (name === "agent.started") {
    view.current.textContent = `Coordinator started with ${data.model}`;
    setRunStep(view, "agent", "Coordinator active", data.model, "active");
  } else if (name === "route.selected") {
    view.current.textContent = `${data.skill} selected by ${data.method} routing`;
    setRunStep(
      view,
      "route",
      "Skill selected",
      `${data.skill} · ${data.reason}`,
      "completed",
    );
  } else if (name === "skill.started") {
    view.current.textContent = `${data.skill} is working`;
    setRunStep(
      view,
      `tool:${data.skill}`,
      `${data.skill} running`,
      toolActivity(data.skill),
      "active",
    );
  } else if (name === "skill.completed") {
    setRunStep(
      view,
      `tool:${data.skill}`,
      `${data.skill} completed`,
      toolActivity(data.skill),
      "completed",
    );
  } else if (name === "plan.updated") {
    const completed = data.todos.filter((todo) => todo.status === "completed").length;
    const detail = data.todos
      .map((todo) => `${todo.status === "completed" ? "✓" : "○"} ${todo.content}`)
      .join(" · ");
    setRunStep(
      view,
      "plan",
      "Execution plan",
      `${completed}/${data.todos.length} complete · ${detail}`,
      completed === data.todos.length ? "completed" : "active",
    );
  } else if (name === "model.started") {
    view.current.textContent = `${data.agent || "Agent"} is choosing the next action`;
    setRunStep(
      view,
      `model:${data.agent}`,
      `${data.agent || "Agent"} deciding`,
      `Model call ${data.call || ""}`.trim(),
      "active",
    );
  } else if (name === "model.completed") {
    setRunStep(
      view,
      `model:${data.agent}`,
      `${data.agent || "Agent"} decision ready`,
      data.model,
      "completed",
    );
  } else if (name === "subagent.started") {
    view.current.textContent = `${data.agent} is analyzing the evidence`;
    setRunStep(view, `subagent:${data.agent}`, "Evidence delegated", data.agent, "active");
  } else if (name === "subagent.completed") {
    setRunStep(view, `subagent:${data.agent}`, "Subagent completed", data.agent, "completed");
  } else if (name === "skill.loading") {
    view.current.textContent = `Loading ${data.skill} instructions`;
    setRunStep(view, `skill:${data.skill}`, `Loading ${data.skill}`, data.resource, "active");
  } else if (name === "skill.loaded") {
    setRunStep(view, `skill:${data.skill}`, `${data.skill} instructions loaded`, data.resource, "completed");
  } else if (name === "skill.resource.loaded") {
    setRunStep(view, `resource:${data.resource}`, "Reference loaded", data.resource, "completed");
  } else if (name === "tool.started") {
    view.current.textContent = `${data.tool} is working`;
    setRunStep(view, `tool:${data.tool}`, `${data.tool} running`, toolActivity(data.tool), "active");
  } else if (name === "tool.completed") {
    setRunStep(view, `tool:${data.tool}`, `${data.tool} completed`, toolActivity(data.tool), "completed");
  } else if (name === "tool.failed") {
    setRunStep(view, `tool:${data.tool}`, `${data.tool} rejected`, data.message, "failed");
  } else if (name === "agent.step.failed") {
    setRunStep(view, "agent-error", "Agent step failed", data.error, "failed");
  } else if (name === "run.completed") {
    const usage = data.response?.usage || {};
    view.status.className = "run-status completed";
    view.status.textContent = "Completed";
    view.current.textContent = `${usage.model_calls || 0} model calls · ${usage.tool_calls || 0} Skill calls · ${usage.duration_ms || 0} ms`;
    setRunStep(
      view,
      "agent",
      "Coordinator completed",
      data.response?.route?.reason || "Result ready",
      "completed",
    );
  } else if (name === "run.failed") {
    view.status.className = "run-status failed";
    view.status.textContent = "Failed";
    view.current.textContent = data.message || "The run failed";
    setRunStep(view, "run-error", "Run failed", data.message || data.code, "failed");
  } else if (name === "run.cancelled") {
    view.status.className = "run-status failed";
    view.status.textContent = "Cancelled";
    view.current.textContent = "The run was cancelled";
    setRunStep(view, "run-cancelled", "Run cancelled", data.code || "Cancelled", "failed");
  }
}

function renderSkillCatalog(skills) {
  skillCatalog.replaceChildren();
  skillCount.textContent = `${skills.length} loaded`;
  for (const skill of skills) {
    const row = document.createElement("div");
    row.className = "skill-catalog-row";
    const name = document.createElement("strong");
    name.textContent = skill.name;
    const description = document.createElement("span");
    description.textContent = skill.description;
    const risk = document.createElement("small");
    risk.textContent = `${skill.version} · ${skill.risk}`;
    row.append(name, description, risk);
    skillCatalog.append(row);
  }
}

function renderRouteChoices(routes) {
  routeChoices.clear();
  skillSelect.replaceChildren();
  const automatic = document.createElement("option");
  automatic.value = "";
  automatic.textContent = "Auto";
  skillSelect.append(automatic);

  for (const route of routes) {
    const option = document.createElement("option");
    option.value = route.id;
    option.textContent = route.selectable
      ? route.label
      : `${route.label} · ${route.availability}`;
    option.title = route.description;
    option.disabled = !route.selectable;
    option.dataset.targetSkill = route.target_skill;
    option.dataset.arguments = JSON.stringify(route.arguments || {});
    skillSelect.append(option);
    routeChoices.set(route.id, route);
  }
}

function renderAgentChoices(agents) {
  agentSelect.replaceChildren();
  for (const agent of agents) {
    const option = document.createElement("option");
    option.value = agent.agent_id;
    option.textContent = `${agent.name} · ${agent.version}`;
    option.title = `${agent.capabilities.length} governed capabilities`;
    agentSelect.append(option);
  }
}

function renderSolutionChoices(solutions) {
  activeSolutions = solutions;
  const saved = localStorage.getItem(solutionStorageKey) || "";
  solutionSelect.replaceChildren();
  if (solutions.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Legacy catalog";
    solutionSelect.append(option);
    solutionSelect.disabled = true;
    return;
  }
  solutionSelect.disabled = false;
  for (const solution of solutions) {
    const option = document.createElement("option");
    option.value = solution.solution_id;
    option.textContent = `${solution.title} · ${solution.version}`;
    option.title = solution.description;
    solutionSelect.append(option);
  }
  if (solutions.some((solution) => solution.solution_id === saved)) {
    solutionSelect.value = saved;
  }
}

function renderSolutionSchema(schema) {
  solutionWidgets.replaceChildren();
  if (!schema) {
    solutionWorkspace.hidden = true;
    return;
  }
  solutionWorkspace.hidden = false;
  solutionWorkspaceTitle.textContent = schema.title;
  for (const widget of schema.widgets || []) {
    const row = document.createElement("div");
    row.className = "solution-widget-row";
    const detail = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = widget.title || widget.widget_id;
    const kind = document.createElement("span");
    kind.textContent = widget.kind;
    detail.append(title, kind);
    const actions = document.createElement("div");
    actions.className = "solution-widget-actions";
    for (const action of widget.actions || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = action.label;
      button.addEventListener("click", () => runUiAction(action));
      actions.append(button);
    }
    row.append(detail, actions);
    solutionWidgets.append(row);
  }
}

async function runUiAction(action) {
  if (action.kind === "invoke" && action.target.startsWith("skill:")) {
    modeSelect.value = "direct";
    modeSelect.dispatchEvent(new Event("change"));
    const skill = action.target.slice("skill:".length);
    const option = [...skillSelect.options].find(
      (item) => item.dataset.targetSkill === skill || item.value === skill,
    );
    if (option) skillSelect.value = option.value;
    messageInput.focus();
    return;
  }
  if (action.kind === "start-workflow") {
    let payload = {};
    if (argumentsInput.value.trim()) {
      payload = JSON.parse(argumentsInput.value);
    }
    const response = await authenticatedFetch("/v1/durable-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        solution_id: solutionSelect.value,
        workflow_type: action.target,
        idempotency_key: createThreadId(),
        payload,
      }),
    });
    if (!response.ok) {
      addMessage("error", await responseError(response, "Workflow start failed"));
      return;
    }
    const result = await response.json();
    addMessage("assistant", `Workflow ${result.workflow_id} started.`, result);
  }
}

function selectedSolution() {
  return activeSolutions.find((solution) => solution.solution_id === solutionSelect.value) || null;
}

function scopedPath(path) {
  const solutionId = solutionSelect.value;
  return solutionId ? `${path}?solution_id=${encodeURIComponent(solutionId)}` : path;
}

function configureModes(solution) {
  const profiles = new Set(solution?.supported_profiles || ["direct", "interactive", "durable"]);
  const directOption = modeSelect.querySelector('option[value="direct"]');
  const agentOption = modeSelect.querySelector('option[value="agent"]');
  directOption.disabled = !profiles.has("direct");
  agentOption.disabled = !profiles.has("interactive");
  for (const option of modeSelect.querySelectorAll('option[value^="durable-"]')) {
    option.disabled = !profiles.has("durable");
  }
  if (modeSelect.selectedOptions[0]?.disabled) {
    modeSelect.value = profiles.has("interactive") ? "agent" : profiles.has("direct") ? "direct" : "durable-skill";
  }
  modeSelect.dispatchEvent(new Event("change"));
}

async function loadSolutionResources() {
  const solution = selectedSolution();
  configureModes(solution);
  const [skillsResponse, routesResponse, agentsResponse] = await Promise.all([
    authenticatedFetch(scopedPath("/v1/skills")),
    authenticatedFetch(scopedPath("/v1/routes")),
    authenticatedFetch(scopedPath("/v1/agents")),
  ]);
  for (const response of [skillsResponse, routesResponse, agentsResponse]) {
    if (!response.ok) throw new Error(await responseError(response, "Solution load failed"));
  }
  const skills = await skillsResponse.json();
  const routes = await routesResponse.json();
  const agents = await agentsResponse.json();
  renderSkillCatalog(skills);
  renderRouteChoices(routes);
  renderAgentChoices(agents);
  let schema = null;
  if (solution) {
    const schemaResponse = await authenticatedFetch(
      `/v1/solutions/${encodeURIComponent(solution.solution_id)}/ui-schema`,
    );
    if (schemaResponse.ok) schema = await schemaResponse.json();
  }
  renderSolutionSchema(schema);
  const agentResponse = await authenticatedFetch(scopedPath("/v1/agent"));
  if (agentResponse.ok) {
    const agent = await agentResponse.json();
    serviceStatus.textContent =
      `ready · ${skills.length} Skills · ${agent.runtime} · ${agent.model}`;
  } else {
    serviceStatus.textContent = `ready · ${skills.length} Skills · Direct`;
  }
}

function addEvent(name, data) {
  const item = document.createElement("li");
  item.className = "event";
  if (name === "route.selected") item.classList.add("route");
  if (name === "plan.updated") item.classList.add("plan");
  if (name.startsWith("subagent.")) item.classList.add("subagent");
  if (name.startsWith("model.")) item.classList.add("model");
  if (name === "run.failed") item.classList.add("failed");

  const title = document.createElement("strong");
  title.textContent = name;
  const detail = document.createElement("span");
  if (name === "route.selected") {
    detail.textContent = `${data.skill} · ${data.method} · ${data.reason}`;
  } else if (name === "plan.updated") {
    detail.textContent = data.todos
      .map((todo) => `${todo.status === "completed" ? "[x]" : "[ ]"} ${todo.content}`)
      .join("\n");
  } else if (name.startsWith("model.")) {
    detail.textContent = `${data.agent || "agent"} · ${data.model || "model"}`;
  } else if (name.startsWith("subagent.")) {
    detail.textContent = data.agent || "subagent";
  } else if (name.startsWith("skill.") && data.resource) {
    detail.textContent = `${data.skill} · ${data.resource}`;
  } else if (name === "tool.failed") {
    detail.textContent = `${data.tool || "tool"} · ${data.message || "rejected"}`;
  } else if (name.startsWith("tool.")) {
    detail.textContent = data.tool || "tool";
  } else {
    detail.textContent =
      data.skill || data.filename || data.message || data.reason || data.run_id || "";
  }
  item.append(title, detail);
  events.append(item);
}

async function responseError(response, fallback) {
  const raw = await response.text();
  let detail = raw.trim();
  try {
    const body = JSON.parse(raw);
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // Keep a non-JSON error body from a server or reverse proxy.
  }
  return `${detail || fallback} (HTTP ${response.status})`;
}

async function uploadAttachment(file) {
  const form = new FormData();
  form.append("file", file);
  const response = await authenticatedFetch(scopedPath("/v1/artifacts"), {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    throw new Error(await responseError(response, "Attachment upload failed"));
  }
  return response.json();
}

async function consumeSse(response, onEvent) {
  if (!response.body) throw new Error("Streaming response is unavailable");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastEventId = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    buffer = buffer.replaceAll("\r\n", "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let eventName = "message";
      let eventId = "";
      const dataLines = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        if (line.startsWith("id:")) eventId = line.slice(3).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length > 0) {
        if (eventId) lastEventId = eventId;
        onEvent(eventName, JSON.parse(dataLines.join("\n")), eventId);
      }
    }
    if (done) break;
  }
  return lastEventId;
}

function waitFor(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function consumeRunStream(initialResponse, onEvent) {
  let response = initialResponse;
  let runId = "";
  let lastEventId = "";
  let terminalEvent = "";

  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      const consumedId = await consumeSse(response, (wireName, payload, eventId) => {
        const envelope = payload?.schema_version && payload?.data ? payload : null;
        const name = envelope?.type || wireName;
        const data = envelope?.data || payload;
        runId = envelope?.run_id || data?.run_id || runId;
        lastEventId = eventId || lastEventId;
        if (["run.completed", "run.failed", "run.cancelled"].includes(name)) {
          terminalEvent = name;
        }
        onEvent(name, data, { eventId: lastEventId, runId });
      });
      lastEventId = consumedId || lastEventId;
    } catch (error) {
      if (!runId || attempt === 3) throw error;
    }
    if (terminalEvent) return { runId, lastEventId, terminalEvent };
    if (!runId || attempt === 3) {
      throw new Error("The event stream ended before the run reached a terminal state");
    }
    await waitFor(250 * (attempt + 1));
    const headers = lastEventId ? { "Last-Event-ID": lastEventId } : {};
    response = await authenticatedFetch(
      `/v1/runs/${encodeURIComponent(runId)}/events/stream`,
      { headers },
    );
    if (!response.ok) {
      throw new Error(await responseError(response, "Run stream recovery failed"));
    }
  }
  throw new Error("Run stream recovery was exhausted");
}

async function loadResultView(runId) {
  const response = await authenticatedFetch(`/v1/runs/${encodeURIComponent(runId)}/result`);
  if (!response.ok) throw new Error(await responseError(response, "Run result is unavailable"));
  return response.json();
}

function appendResultMetadata(element, resultView) {
  const metadata = document.createElement("p");
  metadata.className = "result-metadata";
  metadata.textContent = [
    resultView.capability_id,
    resultView.solution_release_id,
    resultView.generation_id,
  ].filter(Boolean).join(" · ");
  element.append(metadata);
}

function resultBlockTitle(block) {
  const names = {
    issues: "Issues",
    findings: "Findings",
    stories: "Stories",
    citations: "Citations",
    "evidence-citations": "Evidence citations",
    assessments: "Assessments",
    artifacts: "Artifacts",
  };
  return names[block.block_id] || block.block_id.replaceAll("-", " ");
}

function displayValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (["number", "boolean"].includes(typeof value)) return String(value);
  return JSON.stringify(value);
}

function appendTableBlock(root, content) {
  const columns = Array.isArray(content?.columns) ? content.columns : [];
  const rows = Array.isArray(content?.rows) ? content.rows : [];
  const wrapper = document.createElement("div");
  wrapper.className = "result-table-scroll";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const column of columns) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = String(column).replaceAll("_", " ");
    headRow.append(cell);
  }
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tableRow = document.createElement("tr");
    for (const column of columns) {
      const cell = document.createElement("td");
      cell.textContent = displayValue(row?.[column]);
      tableRow.append(cell);
    }
    body.append(tableRow);
  }
  table.append(head, body);
  wrapper.append(table);
  root.append(wrapper);
}

function appendCitationBlock(root, content) {
  const list = document.createElement("ol");
  list.className = "result-citations";
  for (const item of Array.isArray(content?.items) ? content.items : []) {
    const row = document.createElement("li");
    const quote = document.createElement("p");
    quote.textContent = item.quoted_text || item.quote || item.title || "Citation";
    const source = document.createElement("small");
    source.textContent = [
      item.citation_id,
      item.document_id,
      item.artifact_id,
      item.source_uri,
      item.locator?.value,
    ].filter(Boolean).join(" · ");
    row.append(quote, source);
    list.append(row);
  }
  root.append(list);
}

function appendAssessmentBlock(root, content) {
  for (const item of Array.isArray(content?.items) ? content.items : []) {
    const assessment = document.createElement("article");
    assessment.className = "result-assessment";
    const status = document.createElement("strong");
    status.textContent = item.status || "Assessment";
    const summary = document.createElement("p");
    summary.textContent = item.summary || "";
    const details = document.createElement("small");
    const confidence = typeof item.confidence === "number"
      ? `confidence ${item.confidence}` : "";
    const gaps = Array.isArray(item.gaps) && item.gaps.length
      ? `gaps: ${item.gaps.join(", ")}` : "";
    details.textContent = [confidence, gaps].filter(Boolean).join(" · ");
    assessment.append(status, summary, details);
    root.append(assessment);
  }
}

async function downloadResultArtifact(link, filename, status) {
  status.textContent = "Downloading";
  const response = await authenticatedFetch(link.content);
  if (!response.ok) {
    throw new Error(await responseError(response, "Artifact download failed"));
  }
  const blobUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = filename || link.artifact_id;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(blobUrl);
  status.textContent = "Downloaded";
}

function appendArtifactBlock(root, content, resultView) {
  const list = document.createElement("ul");
  list.className = "result-artifacts";
  const links = new Map(
    (resultView.artifacts || []).map((item) => [item.artifact_id, item]),
  );
  for (const item of Array.isArray(content?.items) ? content.items : []) {
    const row = document.createElement("li");
    const artifactId = typeof item === "string" ? item : item?.artifact_id;
    const label = typeof item === "string" ? item : [
      item?.filename, artifactId, item?.media_type,
    ].filter(Boolean).join(" · ") || displayValue(item);
    const link = links.get(artifactId);
    if (link) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "artifact-download quiet-button";
      button.textContent = label || artifactId;
      const status = document.createElement("small");
      status.setAttribute("role", "status");
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await downloadResultArtifact(link, item?.filename, status);
        } catch (error) {
          status.textContent = error instanceof Error ? error.message : String(error);
          button.disabled = false;
        }
      });
      row.append(button, status);
    } else {
      row.textContent = label;
    }
    list.append(row);
  }
  root.append(list);
}

function appendResultBlocks(element, resultView) {
  for (const block of resultView.blocks || []) {
    if (["answer", "result"].includes(block.block_id)) continue;
    const root = document.createElement("section");
    root.className = `result-block result-block-${block.type}`;
    root.dataset.blockId = block.block_id;
    const heading = document.createElement("h3");
    heading.textContent = resultBlockTitle(block);
    root.append(heading);
    if (block.type === "table") appendTableBlock(root, block.content);
    else if (block.type === "citations") appendCitationBlock(root, block.content);
    else if (block.type === "assessments") appendAssessmentBlock(root, block.content);
    else if (block.type === "artifact") appendArtifactBlock(root, block.content, resultView);
    else {
      const fallback = document.createElement("pre");
      fallback.textContent = JSON.stringify(block.content, null, 2);
      root.append(fallback);
    }
    element.append(root);
  }
}

function appendFeedbackPanel(element, resultView) {
  const form = document.createElement("form");
  form.className = "feedback-panel";
  form.setAttribute("aria-label", "Result feedback");

  const heading = document.createElement("strong");
  heading.textContent = "Result feedback";

  const ratingLabel = document.createElement("span");
  ratingLabel.className = "feedback-label";
  ratingLabel.textContent = "Rating";
  const ratingGroup = document.createElement("div");
  ratingGroup.className = "feedback-rating";
  ratingGroup.setAttribute("role", "group");
  ratingGroup.setAttribute("aria-label", "Result rating");
  let rating = null;
  for (let score = 1; score <= 5; score += 1) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = String(score);
    button.setAttribute("aria-label", `${score} out of 5`);
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      rating = score;
      for (const item of ratingGroup.querySelectorAll("button")) {
        item.setAttribute("aria-pressed", String(item === button));
      }
    });
    ratingGroup.append(button);
  }

  const issueLabel = document.createElement("label");
  issueLabel.className = "feedback-field";
  const issueText = document.createElement("span");
  issueText.textContent = "Issue";
  const issue = document.createElement("select");
  issue.setAttribute("aria-label", "Feedback issue");
  for (const [value, label] of [
    ["", "No category"],
    ["incorrect", "Incorrect"],
    ["incomplete", "Incomplete"],
    ["irrelevant", "Irrelevant"],
    ["citation-issue", "Citation issue"],
    ["format", "Format"],
    ["tool-failure", "Tool failure"],
    ["latency", "Latency"],
    ["other", "Other"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    issue.append(option);
  }
  issueLabel.append(issueText, issue);

  const targetLabel = document.createElement("label");
  targetLabel.className = "feedback-field";
  const targetText = document.createElement("span");
  targetText.textContent = "Affected result";
  const target = document.createElement("select");
  target.setAttribute("aria-label", "Affected result block");
  const overall = document.createElement("option");
  overall.value = "";
  overall.textContent = "Overall result";
  target.append(overall);
  for (const block of resultView.blocks || []) {
    const option = document.createElement("option");
    option.value = block.block_id;
    option.textContent = `${block.block_id} · ${block.type}`;
    target.append(option);
  }
  targetLabel.append(targetText, target);

  const comment = document.createElement("textarea");
  comment.rows = 3;
  comment.maxLength = 4000;
  comment.placeholder = "What should be improved?";
  comment.setAttribute("aria-label", "Feedback comment");

  const consent = document.createElement("div");
  consent.className = "feedback-consent";
  const qualityLabel = document.createElement("label");
  const quality = document.createElement("input");
  quality.type = "checkbox";
  quality.checked = true;
  qualityLabel.append(quality, document.createTextNode(" Share with quality review"));
  const evaluationLabel = document.createElement("label");
  const evaluation = document.createElement("input");
  evaluation.type = "checkbox";
  evaluationLabel.append(evaluation, document.createTextNode(" Allow regression evaluation"));
  consent.append(qualityLabel, evaluationLabel);
  quality.addEventListener("change", () => {
    evaluation.disabled = !quality.checked;
    if (!quality.checked) evaluation.checked = false;
  });

  const memoryLabel = document.createElement("label");
  const memory = document.createElement("input");
  memory.type = "checkbox";
  memory.disabled = !resultView.solution_id;
  memoryLabel.append(memory, document.createTextNode(" Propose a saved preference"));
  const memoryFields = document.createElement("div");
  memoryFields.className = "feedback-memory";
  memoryFields.hidden = true;
  const memoryKey = document.createElement("input");
  memoryKey.maxLength = 200;
  memoryKey.placeholder = "Preference key";
  memoryKey.setAttribute("aria-label", "Preference key");
  const memoryValue = document.createElement("textarea");
  memoryValue.rows = 2;
  memoryValue.maxLength = 4000;
  memoryValue.placeholder = "Preference value";
  memoryValue.setAttribute("aria-label", "Preference value");
  memoryFields.append(memoryKey, memoryValue);
  memory.addEventListener("change", () => {
    memoryFields.hidden = !memory.checked;
  });

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = "Submit feedback";
  const status = document.createElement("span");
  status.className = "feedback-status";
  status.setAttribute("role", "status");
  const actions = document.createElement("div");
  actions.className = "feedback-actions";
  actions.append(submit, status);

  form.append(
    heading,
    ratingLabel,
    ratingGroup,
    issueLabel,
    targetLabel,
    comment,
    consent,
    memoryLabel,
    memoryFields,
    actions,
  );
  element.append(form);

  let feedbackKey = crypto.randomUUID();
  form.addEventListener("input", () => {
    if (!submit.disabled) feedbackKey = crypto.randomUUID();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const commentValue = comment.value.trim();
    if (rating === null && !issue.value && !commentValue && !memory.checked) {
      status.textContent = "Add a rating, issue, comment, or preference.";
      return;
    }
    if (memory.checked && (!memoryKey.value.trim() || !memoryValue.value.trim())) {
      status.textContent = "Preference key and value are required.";
      return;
    }
    const block = (resultView.blocks || []).find((item) => item.block_id === target.value);
    const payload = {
      rating,
      categories: issue.value ? [issue.value] : [],
      comment: commentValue || null,
      annotations: block && commentValue ? [{
        block_id: block.block_id,
        block_revision: block.revision,
        target_digest: block.content_digest,
        comment: commentValue,
      }] : [],
      expected_action: memory.checked
        ? "propose-memory"
        : evaluation.checked ? "evaluation-candidate" : "none",
      quality_use_allowed: quality.checked,
      evaluation_use_allowed: evaluation.checked,
      ...(memory.checked ? {
        memory_proposal: {
          key: memoryKey.value.trim(),
          value: memoryValue.value.trim(),
          sensitivity: "normal",
        },
      } : {}),
    };
    submit.disabled = true;
    status.textContent = "Submitting";
    try {
      const response = await authenticatedFetch(
        `/v1/runs/${encodeURIComponent(resultView.run_id)}/feedback`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": feedbackKey,
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) throw new Error(await responseError(response, "Feedback failed"));
      const feedback = await response.json();
      for (const control of form.querySelectorAll("input, select, textarea, button")) {
        control.disabled = true;
      }
      status.textContent = "Feedback recorded";
      if (memory.checked) {
        const confirm = document.createElement("button");
        confirm.type = "button";
        confirm.className = "quiet-button";
        confirm.textContent = "Confirm preference";
        confirm.disabled = false;
        confirm.addEventListener("click", async () => {
          confirm.disabled = true;
          status.textContent = "Saving preference";
          try {
            const confirmation = await authenticatedFetch(
              `/v1/feedback/${encodeURIComponent(feedback.feedback_id)}/memory/confirm`,
              { method: "POST" },
            );
            if (!confirmation.ok) {
              status.textContent = await responseError(confirmation, "Preference save failed");
              confirm.disabled = false;
              return;
            }
            status.textContent = "Preference saved";
            confirm.remove();
          } catch (error) {
            status.textContent = error instanceof Error ? error.message : String(error);
            confirm.disabled = false;
          }
        });
        actions.insertBefore(confirm, status);
      }
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : String(error);
      submit.disabled = false;
    }
  });
}

async function renderCompletedRun(runId, fallback) {
  try {
    const resultView = await loadResultView(runId);
    const answer = (resultView.blocks || []).find((block) => block.type === "markdown");
    const text = answer?.content?.markdown || fallback?.message || "Result ready";
    const element = addMessage("assistant", text, resultView.data);
    appendResultBlocks(element, resultView);
    appendResultMetadata(element, resultView);
    appendFeedbackPanel(element, resultView);
  } catch (error) {
    addMessage("assistant", fallback?.message || "Result ready", fallback?.result || null);
    addEvent("result.view.failed", {
      message: error instanceof Error ? error.message : String(error),
    });
  }
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = messageInput.value.trim();
  const durableMode = modeSelect.value.startsWith("durable-");
  if (!message && !durableMode) return;

  addMessage("user", message);
  activeRunView = beginRunView();
  sendButton.disabled = true;
  sendButton.textContent = "Running";

  try {
    if (durableMode) {
      if (attachmentInput.files?.length) throw new Error("Durable inputs require explicit JSON references");
      const agentMode = modeSelect.value === "durable-agent";
      const route = routeChoices.get(skillSelect.value);
      const target = agentMode ? agentSelect.value : route?.target_skill || skillSelect.value;
      if (!target) throw new Error("Select a durable target");
      const input = argumentsInput.value.trim() ? JSON.parse(argumentsInput.value) : {};
      if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("Arguments must be a JSON object");
      const payload = {
        solution_id: solutionSelect.value || null,
        ...(agentMode ? { agent_id: target } : { skill: target }),
        payload: agentMode ? { ...input, message } : { ...(route?.arguments || {}), ...input },
      };
      const fingerprint = JSON.stringify(payload);
      if (!durableRequest || durableRequest.fingerprint !== fingerprint) {
        durableRequest = { fingerprint, key: crypto.randomUUID() };
      }
      const started = await durableJson("/v1/durable-runs", {
        ...payload, idempotency_key: durableRequest.key,
      });
      durableRunId = started.run_id;
      sessionStorage.setItem("sage.durable-run", durableRunId);
      await refreshDurable();
      addMessage("assistant", `Run ${started.run_id}`, started);
      durableRequest = null;
      return;
    }
    const attachmentIds = [];
    const file = attachmentInput.files?.[0];
    if (file) {
      const uploaded = await uploadAttachment(file);
      attachmentIds.push(uploaded.artifact_id);
      addEvent("attachment.uploaded", uploaded);
    }

    const payload = {
      message,
      mode: modeSelect.value,
      thread_id: threadId,
      artifact_ids: attachmentIds,
    };
    if (solutionSelect.value) payload.solution_id = solutionSelect.value;
    if (modeSelect.value === "agent" && agentSelect.value) {
      payload.agent_id = agentSelect.value;
    } else if (modeSelect.value === "direct") {
      const route = skillSelect.value ? routeChoices.get(skillSelect.value) : null;
      if (skillSelect.value) {
        payload.skill = route?.target_skill || skillSelect.value;
      }
      let suppliedArguments = {};
      if (argumentsInput.value.trim()) {
        suppliedArguments = JSON.parse(argumentsInput.value);
        if (!suppliedArguments || Array.isArray(suppliedArguments) || typeof suppliedArguments !== "object") {
          throw new Error("Direct arguments must be a JSON object");
        }
      }
      const routeArguments = route?.arguments || {};
      if (Object.keys(routeArguments).length > 0 || Object.keys(suppliedArguments).length > 0) {
        payload.arguments = { ...routeArguments, ...suppliedArguments };
      }
    }
    const response = await authenticatedFetch("/v1/messages/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      throw new Error(await responseError(response, "Message request failed"));
    }

    let completedResponse = null;
    const streamState = await consumeRunStream(response, (name, data) => {
      updateRunView(name, data);
      addEvent(name, data);
      if (name === "run.completed") {
        completedResponse = data.response || null;
        if (completedResponse?.thread_id) updateThread(completedResponse.thread_id);
      }
      if (name === "run.failed") {
        addMessage("error", data.message || "Skill execution failed");
      }
      if (name === "run.cancelled") {
        addMessage("error", "The run was cancelled");
      }
    });
    if (streamState.terminalEvent === "run.completed") {
      await renderCompletedRun(streamState.runId, completedResponse);
    }
  } catch (error) {
    addMessage("error", error instanceof Error ? error.message : String(error));
  } finally {
    sendButton.disabled = false;
    sendButton.textContent = "Send";
    messageInput.value = "";
    attachmentInput.value = "";
    attachmentName.textContent = "";
    messageInput.focus();
    activeRunView = null;
  }
});

attachmentInput.addEventListener("change", () => {
  const file = attachmentInput.files?.[0];
  attachmentName.textContent = file
    ? `${file.name} · ${(file.size / (1024 * 1024)).toFixed(2)} MiB`
    : "";
});

modeSelect.addEventListener("change", () => {
  const agentMode = ["agent", "durable-agent"].includes(modeSelect.value);
  skillSelect.disabled = agentMode;
  agentSelect.disabled = !agentMode;
  argumentsControl.hidden = modeSelect.value === "agent";
  messageInput.required = modeSelect.value !== "durable-skill";
});

if (durableRunId) {
  document.querySelector("#durableRun").hidden = false;
  document.querySelector("#durableStatus").textContent = durableRunId;
}

solutionSelect.addEventListener("change", async () => {
  if (solutionSelect.value) {
    localStorage.setItem(solutionStorageKey, solutionSelect.value);
  }
  updateThread(createThreadId());
  events.replaceChildren();
  try {
    await loadSolutionResources();
  } catch (error) {
    skillCount.textContent = "Unavailable";
    addMessage("error", error instanceof Error ? error.message : String(error));
  }
});

function startNewThread() {
  updateThread(createThreadId());
  events.replaceChildren();
  messages.replaceChildren();
  addMessage("assistant", "Started a new thread with empty conversation memory.");
  messages.append(skillOverview, solutionWorkspace);
}

document.querySelector("#newThread").addEventListener("click", startNewThread);
agentSelect.addEventListener("change", startNewThread);

document.querySelector("#clearActivity").addEventListener("click", () => {
  events.replaceChildren();
});

signInButton.addEventListener("click", async () => {
  try {
    if (activeAccessToken()) {
      sessionStorage.removeItem(authTokenKey);
      sessionStorage.removeItem(authExpiryKey);
      location.reload();
      return;
    }
    await startSignIn();
  } catch (error) {
    addMessage("error", error instanceof Error ? error.message : String(error));
  }
});

async function initializeRuntime() {
  updateThread(threadId);
  skillSelect.disabled = true;
  agentSelect.disabled = false;
  try {
    await initializeAuthentication();
  } catch (error) {
    serviceStatus.textContent = "Sign-in failed";
    addMessage("error", error instanceof Error ? error.message : String(error));
    return;
  }
  fetch("/health/ready")
    .then(async (healthResponse) => {
    const health = await healthResponse.json();
    serviceStatus.textContent = `${health.status} · ${health.skills} Skills`;
    })
    .catch(() => {
      serviceStatus.textContent = "Service unavailable";
    });
  if (authConfig.mode === "entra-id" && !activeAccessToken()) {
    serviceStatus.textContent = "Sign in required";
    skillCount.textContent = "Sign in required";
    return;
  }
  try {
    const response = await authenticatedFetch("/v1/solutions");
    if (!response.ok) throw new Error(await responseError(response, "Solution catalog failed"));
    renderSolutionChoices(await response.json());
    await loadSolutionResources();
  } catch (error) {
    skillCount.textContent = "Unavailable";
    addMessage("error", error instanceof Error ? error.message : String(error));
  }
}

initializeRuntime();
