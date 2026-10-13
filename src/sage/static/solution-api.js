const solutionSelect = document.querySelector("#solutionSelect");
const modeSelect = document.querySelector("#modeSelect");
const threadInput = document.querySelector("#threadId");
const tokenInput = document.querySelector("#bearerToken");
const messageInput = document.querySelector("#messageInput");
const responseOutput = document.querySelector("#responseOutput");
const eventOutput = document.querySelector("#eventOutput");
const eventCount = document.querySelector("#eventCount");
const requestStatus = document.querySelector("#requestStatus");
const serviceStatus = document.querySelector("#serviceStatus");
const notice = document.querySelector("#notice");
const exampleOutput = document.querySelector("#exampleOutput");
let selectedExample = "curl";
let events = [];

function requestPayload() {
  const payload = {
    message: messageInput.value.trim(),
    mode: modeSelect.value,
    solution_id: solutionSelect.value,
  };
  if (!payload.solution_id) throw new Error("Select an active Solution.");
  if (!payload.message) throw new Error("Message is required.");
  if (threadInput.value.trim()) payload.thread_id = threadInput.value.trim();
  return payload;
}

function requestHeaders() {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (tokenInput.value.trim()) headers.set("Authorization", `Bearer ${tokenInput.value.trim()}`);
  return headers;
}

async function responseError(response) {
  try {
    const body = await response.json();
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function setBusy(busy, label = "Running") {
  document.querySelector("#sendHttp").disabled = busy;
  document.querySelector("#sendSse").disabled = busy;
  requestStatus.textContent = busy ? label : "Idle";
  requestStatus.className = `badge ${busy ? "" : "neutral"}`.trim();
}

function renderEvents() {
  eventCount.textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
  eventOutput.textContent = events.length
    ? events.map((item) => `${item.id ? `#${item.id} ` : ""}${item.name}\n${JSON.stringify(item.data, null, 2)}`).join("\n\n")
    : "No events yet.";
}

async function consumeSse(response) {
  if (!response.body) throw new Error("Streaming response body is unavailable.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = null;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let name = "message";
      let id = "";
      const dataLines = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("id:")) id = line.slice(3).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (!dataLines.length) continue;
      const wireData = JSON.parse(dataLines.join("\n"));
      const envelope = wireData?.schema_version && wireData?.data ? wireData : null;
      const event = {
        id,
        name: envelope?.type || name,
        data: envelope?.data || wireData,
      };
      events.push(event);
      if (event.name === "run.completed") completed = event.data.response || event.data;
      if (event.name === "run.failed") throw new Error(event.data.message || "Run failed.");
      renderEvents();
    }
    if (done) break;
  }
  return completed;
}

async function send(path, streaming) {
  notice.textContent = "";
  events = [];
  renderEvents();
  responseOutput.textContent = streaming ? "Waiting for stream..." : "Waiting for response...";
  setBusy(true, streaming ? "Streaming" : "Requesting");
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: requestHeaders(),
      body: JSON.stringify(requestPayload()),
    });
    if (!response.ok) throw new Error(await responseError(response));
    const result = streaming ? await consumeSse(response) : await response.json();
    if (result?.thread_id) threadInput.value = result.thread_id;
    responseOutput.textContent = JSON.stringify(result, null, 2);
    requestStatus.textContent = "Completed";
    requestStatus.className = "badge success";
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    notice.textContent = message;
    notice.className = "notice error";
    responseOutput.textContent = message;
    requestStatus.textContent = "Failed";
    requestStatus.className = "badge danger";
  } finally {
    document.querySelector("#sendHttp").disabled = false;
    document.querySelector("#sendSse").disabled = false;
  }
}

function payloadExample() {
  return {
    message: messageInput.value.trim() || "Hello",
    mode: modeSelect.value,
    solution_id: solutionSelect.value || "your-solution-id",
    ...(threadInput.value.trim() ? { thread_id: threadInput.value.trim() } : {}),
  };
}

function renderExample() {
  const payload = JSON.stringify(payloadExample(), null, 2);
  const origin = window.location.origin;
  if (selectedExample === "curl") {
    exampleOutput.textContent = [
      `curl -N -X POST "${origin}/v1/messages" \\`,
      '  -H "Content-Type: application/json" \\',
      '  -H "Authorization: Bearer <access-token>" \\',
      `  --data '${payload.replaceAll("'", "'\\''")}'`,
    ].join("\n");
    return;
  }
  exampleOutput.textContent = `const response = await fetch("${origin}/v1/messages/stream", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer <access-token>"
  },
  body: JSON.stringify(${payload})
});

if (!response.ok) throw new Error(await response.text());
const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {
  const { value, done } = await reader.read();
  buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
  // Parse SSE frames separated by a blank line; inspect event/data/id fields.
  console.log(buffer);
  if (done) break;
}`;
}

async function loadSolutions() {
  const [healthResponse, solutionsResponse] = await Promise.all([
    fetch("/health/ready", { cache: "no-store" }),
    fetch("/v1/solutions", { headers: requestHeaders(), cache: "no-store" }),
  ]);
  if (!healthResponse.ok) throw new Error("SAGE health check failed.");
  if (!solutionsResponse.ok) throw new Error(await responseError(solutionsResponse));
  const health = await healthResponse.json();
  const solutions = await solutionsResponse.json();
  const selected = solutionSelect.value;
  solutionSelect.replaceChildren();
  if (!solutions.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No active Solutions";
    solutionSelect.append(option);
    notice.textContent = "Activate a Solution release before using the external API.";
  } else {
    for (const solution of solutions) {
      const option = document.createElement("option");
      option.value = solution.solution_id;
      option.textContent = `${solution.title} (${solution.solution_id}) · ${solution.version}`;
      solutionSelect.append(option);
    }
    if ([...solutionSelect.options].some((option) => option.value === selected)) {
      solutionSelect.value = selected;
    }
    notice.textContent = "";
  }
  serviceStatus.textContent = `${health.status} · ${solutions.length} active`;
  serviceStatus.className = "badge success";
  renderExample();
}

document.querySelector("#sendHttp").addEventListener("click", () => send("/v1/messages", false));
document.querySelector("#sendSse").addEventListener("click", () => send("/v1/messages/stream", true));
document.querySelector("#refreshSolutions").addEventListener("click", () => loadSolutions().catch((error) => {
  notice.textContent = error.message;
  notice.className = "notice error";
}));
document.querySelector("#clearOutput").addEventListener("click", () => {
  events = [];
  renderEvents();
  responseOutput.textContent = "No response yet.";
  notice.textContent = "";
});
for (const input of [solutionSelect, modeSelect, threadInput, messageInput]) {
  input.addEventListener("input", renderExample);
}
for (const tab of document.querySelectorAll("[data-example]")) {
  tab.addEventListener("click", () => {
    selectedExample = tab.dataset.example;
    for (const item of document.querySelectorAll("[data-example]")) {
      item.classList.toggle("active", item === tab);
    }
    renderExample();
  });
}
document.querySelector("#copyExample").addEventListener("click", async () => {
  await navigator.clipboard.writeText(exampleOutput.textContent);
  notice.textContent = "Example copied.";
  notice.className = "notice";
});

renderExample();
loadSolutions().catch((error) => {
  serviceStatus.textContent = "Unavailable";
  serviceStatus.className = "badge danger";
  notice.textContent = error.message;
  notice.className = "notice error";
});
