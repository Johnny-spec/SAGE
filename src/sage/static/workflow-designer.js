(() => {
  const root = document.querySelector("#workflowDesigner");
  const find = (id) => root.querySelector(`#${id}`);
  const copy = (value) => structuredClone(value);
  const base = "/v1/admin/workflow-drafts";
  let draft = null, selected = null, dirty = false, busy = false;
  let targets = [], solutions = [], records = [], history = [], future = [];

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function button(text, action, className = "secondary") {
    const item = node("button", text, className);
    item.type = "button";
    item.addEventListener("click", () => perform(() => { assertFields(); return action(); }));
    return item;
  }
  async function perform(action) {
    if (busy) return;
    busy = true;
    root.inert = true;
    try { await action(); }
    catch (error) { setNotice(typeof error.message === "string" ? error.message : JSON.stringify(error), "error"); }
    finally { busy = false; root.inert = false; }
  }
  function status() {
    find("wfStatus").textContent = draft
      ? `${dirty ? "Unsaved changes" : "Saved"} · ${draft.draft_id ? `revision ${draft.revision}` : "new draft"}`
      : "No draft selected";
    find("wfUndo").disabled = !history.length;
    find("wfRedo").disabled = !future.length;
    find("wfDelete").disabled = !draft?.draft_id;
    if (draft) find("wfSource").textContent = JSON.stringify(draft.definition, null, 2);
  }
  function change(action, refresh = true) {
    history.push(copy({ definition: draft.definition, design: draft.design, title: draft.title, solution_id: draft.solution_id }));
    if (history.length > 40) history.shift();
    future = [];
    action();
    dirty = true;
    find("wfValidation").replaceChildren();
    if (refresh) render(); else status();
  }
  function allowDiscard() { return !dirty || window.confirm("Discard unsaved workflow changes?"); }
  function assertFields() {
    const invalid = [...find("wfEditor").querySelectorAll("input,select,textarea")].find((field) => !field.checkValidity());
    if (invalid) { invalid.reportValidity(); throw new Error("Correct the invalid field before continuing."); }
  }
  function choose(record) {
    draft = copy(record); selected = draft.definition.steps?.[0]?.id || null;
    history = []; future = []; dirty = !draft.draft_id;
    find("wfValidation").replaceChildren(); render();
  }
  async function refreshLibrary() {
    const [catalog, solutionList] = await Promise.all([api("/v1/admin/workflow-targets"), api("/v1/admin/solutions")]);
    targets = catalog.targets; solutions = solutionList;
    records = [];
    let page;
    do {
      page = await api(`${base}?offset=${records.length}&limit=100`);
      records.push(...page.items);
    } while (records.length < page.total && page.items.length);
    const select = find("wfDraft");
    select.replaceChildren(new Option("Select a draft", ""));
    for (const record of records) select.add(new Option(`${record.title} · ${record.solution_id} · r${record.revision}`, record.draft_id));
    select.value = draft?.draft_id || "";
  }
  function fresh(solutionId = solutions[0]?.solution_id) {
    if (!solutions.length) throw new Error("Create a Solution before creating a workflow draft.");
    choose({ title: "Untitled workflow", solution_id: solutionId,
      definition: { apiVersion: "sage.dev/v1beta1", kind: "WorkflowTemplate", workflowId: "new-workflow",
        version: "1.0.0", maxConcurrency: 1, steps: [], outputs: {} }, design: {} });
  }
  function payload() {
    return { title: draft.title, solution_id: draft.solution_id, definition: draft.definition, design: draft.design };
  }
  async function save() {
    assertFields();
    if (draft.draft_id && !dirty) return;
    draft = await api(draft.draft_id ? `${base}/${draft.draft_id}` : base, {
      method: draft.draft_id ? "PUT" : "POST", body: JSON.stringify(payload()),
      headers: draft.draft_id ? { "If-Match": `"${draft.revision}"` } : {},
    });
    dirty = false; status();
    await refreshLibrary(); render();
  }
  function revisionHeaders() { return { "If-Match": `"${draft.revision}"` }; }
  function download(content, filename, type) {
    const url = URL.createObjectURL(new Blob([content], { type }));
    const link = node("a"); link.href = url; link.download = filename;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function steps() { return draft.definition.steps; }
  function ancestors(stepId, visited = new Set()) {
    const step = steps().find((item) => item.id === stepId);
    for (const dependency of step?.dependsOn || []) {
      if (!visited.has(dependency)) { visited.add(dependency); ancestors(dependency, visited); }
    }
    return visited;
  }
  function targetFor(step) { return targets.find((item) => item.kind === step.kind && item.target === step.target); }
  function position(step, index) {
    const saved = draft.design.positions?.[step.id];
    return { x: Number.isFinite(saved?.x) ? Math.max(16, Math.min(6000, saved.x)) : 24 + (index % 3) * 240,
      y: Number.isFinite(saved?.y) ? Math.max(16, Math.min(6000, saved.y)) : 28 + Math.floor(index / 3) * 140 };
  }
  function renderGraph() {
    const nodes = find("wfNodes"), edges = find("wfEdges");
    nodes.replaceChildren(); edges.replaceChildren();
    const positions = new Map(steps().map((step, index) => [step.id, position(step, index)]));
    const graph = find("wfGraph");
    graph.style.width = `${Math.max(720, ...[...positions.values()].map((point) => point.x + 230))}px`;
    graph.style.height = `${Math.max(580, ...[...positions.values()].map((point) => point.y + 130))}px`;
    const svg = (tag) => document.createElementNS("http://www.w3.org/2000/svg", tag);
    const defs = svg("defs"), marker = svg("marker"), arrow = svg("path");
    marker.id = "wfArrow";
    for (const [key, value] of Object.entries({ viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse" })) marker.setAttribute(key, value);
    arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z"); marker.append(arrow); defs.append(marker); edges.append(defs);
    if (!steps().length) nodes.append(node("div", "No nodes", "wf-empty"));
    for (const step of steps()) {
      const point = positions.get(step.id);
      for (const dependency of step.dependsOn || []) {
        const previous = positions.get(dependency); if (!previous) continue;
        const path = svg("path");
        const startX = previous.x + 190, startY = previous.y + 43, endX = point.x, endY = point.y + 43;
        path.setAttribute("d", `M ${startX} ${startY} C ${startX + 40} ${startY}, ${endX - 40} ${endY}, ${endX} ${endY}`);
        path.setAttribute("marker-end", "url(#wfArrow)"); edges.append(path);
      }
      const item = node("button", undefined, `wf-node${selected === step.id ? " selected" : ""}`);
      item.type = "button"; item.dataset.step = step.id;
      item.style.left = `${point.x}px`; item.style.top = `${point.y}px`;
      const target = targetFor(step);
      const name = draft.design.labels?.[step.id] || (step.kind === "tool" ? target?.name : null) || step.id;
      item.setAttribute("aria-label", `${name}, ${step.kind}`);
      item.setAttribute("aria-pressed", String(selected === step.id));
      item.title = `${step.id}: ${step.target}`;
      item.append(node("strong", name), node("span", step.kind === "tool" && target?.server_alias
        ? `${target.server_alias} · ${target.version}` : `${step.kind} · ${step.target || "Select target"}`));
      let start = null, moved = false;
      item.addEventListener("pointerdown", (event) => {
        if (window.matchMedia("(max-width: 760px)").matches) return;
        if (event.button !== 0) return;
        start = { x: event.clientX, y: event.clientY }; moved = false;
        item.setPointerCapture(event.pointerId);
      });
      item.addEventListener("pointermove", (event) => {
        if (!start) return;
        const deltaX = event.clientX - start.x, deltaY = event.clientY - start.y;
        moved ||= Math.abs(deltaX) + Math.abs(deltaY) > 5;
        if (moved) { item.style.left = `${Math.max(16, point.x + deltaX)}px`; item.style.top = `${Math.max(16, point.y + deltaY)}px`; }
      });
      item.addEventListener("pointerup", () => {
        start = null;
        if (moved) change(() => { draft.design.positions ||= {}; draft.design.positions[step.id] = { x: parseFloat(item.style.left), y: parseFloat(item.style.top) }; });
      });
      item.addEventListener("pointercancel", () => { start = null; renderGraph(); });
      item.addEventListener("click", () => {
        try { assertFields(); if (!moved) { selected = step.id; renderGraph(); renderProperties(); } }
        catch (error) { setNotice(error.message, "error"); }
      });
      nodes.append(item);
    }
  }
  function field(container, labelText, value, update, options = {}) {
    const label = node("label", labelText);
    const input = node(options.multiline ? "textarea" : "input");
    input.setAttribute("aria-label", labelText);
    input.value = value ?? "";
    if (options.multiline) input.rows = 4;
    else input.type = options.type || "text";
    if (options.min !== undefined) input.min = options.min;
    if (options.max !== undefined) input.max = options.max;
    if (options.required) input.required = true;
    input.addEventListener("change", () => {
      input.setCustomValidity("");
      try {
        if (!input.checkValidity()) return;
        const value = options.json ? JSON.parse(input.value || "{}") : options.type === "number" ? Number(input.value) : input.value;
        if (options.object && (!value || Array.isArray(value) || typeof value !== "object")) throw new Error("Enter a JSON object");
        change(() => update(value), false);
        renderGraph();
      } catch (error) { dirty = true; status(); input.setCustomValidity(error.message); input.reportValidity(); }
    });
    label.append(input); container.append(label); return input;
  }
  function dropdown(container, title, choices, value, update) {
    const label = node("label", title), select = node("select");
    select.setAttribute("aria-label", title);
    for (const choice of choices) select.add(new Option(choice.label, choice.value));
    if (value && !choices.some((choice) => choice.value === value)) select.add(new Option(`${value} (unavailable)`, value));
    select.value = value || "";
    select.addEventListener("change", () => {
      try { assertFields(); change(() => update(select.value)); }
      catch (error) { select.value = value || ""; setNotice(error.message, "error"); }
    });
    label.append(select); container.append(label); return select;
  }
  function bindingsEditor(container, mapping, owner = null) {
    const available = owner ? steps().filter((step) => ancestors(owner.id).has(step.id)) : steps();
    for (const [key, binding] of Object.entries(mapping)) {
      const row = node("div", undefined, "wf-binding");
      field(row, "Parameter", key, (value) => {
        if (!value || (value !== key && Object.hasOwn(mapping, value))) throw new Error("Parameter must be unique");
        delete mapping[key]; mapping[value] = binding;
        queueMicrotask(() => { renderProperties(); renderOutputs(); });
      }, { required: true });
      dropdown(row, "Source", ["literal", "workflow-input", "step-output", "artifact"].map((value) => ({ value, label: value })), binding.source, (value) => {
        mapping[key] = { source: value, ...(value === "step-output" ? { stepId: available[0]?.id || "", path: "" } : value === "literal" ? { value: "" } : value === "artifact" ? { artifactId: "" } : { path: "" }) };
      });
      if (binding.source === "step-output") dropdown(row, "Upstream node", [{ value: "", label: "Select upstream node" }, ...available.map((step) => ({ value: step.id, label: step.id }))], binding.stepId, (value) => { binding.stepId = value; });
      if (["step-output", "workflow-input"].includes(binding.source)) {
        const path = field(row, "JSON Pointer", binding.path || "", (value) => { binding.path = value; });
        if (binding.source === "step-output") {
          const upstream = steps().find((step) => step.id === binding.stepId);
          const schema = upstream && targetFor(upstream)?.output_schema;
          const datalist = node("datalist"); datalist.id = `wfPaths-${crypto.randomUUID()}`;
          function addPaths(current, prefix = "", depth = 0) {
            if (depth > 5) return;
            for (const [name, child] of Object.entries(current?.properties || {})) {
              const value = `${prefix}/${name.replaceAll("~", "~0").replaceAll("/", "~1")}`;
              datalist.append(new Option(value, value)); addPaths(child, value, depth + 1);
            }
          }
          addPaths(schema); path.setAttribute("list", datalist.id); row.append(datalist);
        }
      } else if (binding.source === "literal") field(row, "Value JSON", JSON.stringify(binding.value ?? null), (value) => { binding.value = value; }, { json: true });
      else field(row, "Artifact ID", binding.artifactId, (value) => { binding.artifactId = value; }, { required: true });
      row.append(button("Remove binding", () => change(() => { delete mapping[key]; }))); container.append(row);
    }
    container.append(button("Add binding", () => change(() => {
      let index = 1; while (Object.hasOwn(mapping, `value${index}`)) index++;
      mapping[`value${index}`] = { source: "workflow-input", path: "" };
    })));
  }
  function renderToolDetails(panel, target) {
    const section = node("section", undefined, "wf-tool-details");
    section.setAttribute("aria-label", "MCP tool details");
    section.append(node("h3", "Tool details"), node("p", target.description || "Description not provided"));
    const metadata = node("dl", undefined, "wf-tool-metadata");
    for (const [label, value] of [
      ["Server", target.server_alias || "Not provided"],
      ["Server ID", target.server_id || "Not provided"],
      ["Tool ID", target.target], ["Version", target.version], ["Risk", target.risk],
      ["Tool timeout", target.tool_timeout_seconds == null ? "Not provided" : `${target.tool_timeout_seconds} s`],
      ["Output limit", target.max_output_bytes == null ? "Not provided" : `${target.max_output_bytes} bytes`],
    ]) metadata.append(node("dt", label), node("dd", String(value ?? "Not provided")));
    section.append(metadata);
    const parameters = node("details", undefined, "wf-tool-parameters"); parameters.open = true;
    parameters.append(node("summary", "Input parameters"));
    const schema = target.input_schema || {}, required = new Set(schema.required || []);
    const properties = Object.entries(schema.properties || {});
    if (!properties.length) parameters.append(node("p", "No top-level parameters declared", "muted"));
    for (const [name, property] of properties) {
      const row = node("div", undefined, "wf-tool-parameter");
      const type = property?.type || (property?.$ref ? "$ref" : property?.oneOf ? "oneOf" : property?.anyOf ? "anyOf" : "unspecified type");
      row.append(node("strong", name), node("span", `${Array.isArray(type) ? type.join(" | ") : type} · ${required.has(name) ? "Required" : "Optional"}`, "muted"));
      if (property?.description) row.append(node("p", property.description));
      const constraints = node("dl", undefined, "wf-tool-metadata");
      for (const key of ["enum", "default", "minimum", "maximum", "minLength", "maxLength", "format"]) {
        if (property && Object.hasOwn(property, key)) constraints.append(node("dt", key), node("dd", JSON.stringify(property[key])));
      }
      row.append(constraints); parameters.append(row);
    }
    section.append(parameters);
    for (const [title, value] of [["Input schema", target.input_schema], ["Tool output schema", target.tool_output_schema]]) {
      const details = node("details"); details.append(node("summary", title));
      details.append(value == null ? node("p", "Not declared", "muted") : node("pre", JSON.stringify(value, null, 2)));
      section.append(details);
    }
    const digests = node("details"); digests.append(node("summary", "Published digests"));
    const hashes = node("dl", undefined, "wf-tool-metadata");
    hashes.append(node("dt", "Content"), node("dd", target.content_digest || "Not provided"),
      node("dt", "Schema"), node("dd", target.schema_digest || "Not provided"));
    digests.append(hashes); section.append(digests); panel.append(section);
  }
  function renderProperties() {
    const panel = find("wfProperties"); panel.replaceChildren();
    const step = steps().find((item) => item.id === selected);
    if (!step) { panel.append(node("div", "No node selected", "empty")); return; }
    panel.append(node("h2", step.id));
    field(panel, "Display name", draft.design.labels?.[step.id] || step.id, (value) => { draft.design.labels ||= {}; draft.design.labels[step.id] = value; });
    dropdown(panel, "Target", [{ value: "", label: "Select target" }, ...targets.filter((item) => item.kind === step.kind && item.enabled).map((item) => ({ value: item.target, label: `${item.kind === "tool" && item.server_alias ? `${item.server_alias} / ` : ""}${item.name} · ${item.version}` }))], step.target, (value) => { step.target = value; });
    const target = targetFor(step);
    if (step.kind === "tool") {
      if (target) renderToolDetails(panel, target);
      else panel.append(node("p", targets.some((item) => item.kind === "tool" && item.enabled)
        ? "Selected tool is unavailable in the active Generation."
        : "No published MCP tools in the active Generation.", "wf-tool-unavailable"));
    } else if (target?.risk) panel.append(node("div", `Risk: ${target.risk}`, "muted"));
    const deps = node("fieldset"), legend = node("legend", "Depends on"); deps.append(legend);
    for (const other of steps().filter((item) => item.id !== step.id)) {
      const label = node("label", undefined, "wf-check"), check = node("input"); check.type = "checkbox";
      check.checked = (step.dependsOn || []).includes(other.id);
      check.disabled = !check.checked && ancestors(other.id).has(step.id);
      check.addEventListener("change", () => {
        try { assertFields(); change(() => {
          step.dependsOn = check.checked ? [...new Set([...(step.dependsOn || []), other.id])] : step.dependsOn.filter((id) => id !== other.id);
        }); } catch (error) { check.checked = !check.checked; setNotice(error.message, "error"); }
      });
      label.append(check, document.createTextNode(other.id)); deps.append(label);
    }
    panel.append(deps);
    field(panel, "Timeout seconds", step.timeoutSeconds || 300, (value) => { step.timeoutSeconds = value; }, { type: "number", min: 1, max: 86400, required: true });
    field(panel, "Maximum attempts", step.retry?.maximum_attempts || 1, (value) => { step.retry = { ...step.retry, maximum_attempts: value }; }, { type: "number", min: 1, max: 20, required: true });
    dropdown(panel, "Join policy", ["all", "all-settled"].map((value) => ({ value, label: value })), step.joinPolicy || "all", (value) => { step.joinPolicy = value; });
    dropdown(panel, "Failure policy", ["fail-fast", "continue-with-errors"].map((value) => ({ value, label: value })), step.failurePolicy || "fail-fast", (value) => { step.failurePolicy = value; });
    const inherited = node("label", undefined, "wf-check"), check = node("input"); check.type = "checkbox"; check.checked = step.inheritInput !== false;
    check.addEventListener("change", () => {
      try { assertFields(); change(() => { step.inheritInput = check.checked; }); }
      catch (error) { check.checked = !check.checked; setNotice(error.message, "error"); }
    });
    inherited.append(check, document.createTextNode("Inherit workflow input")); panel.append(inherited);
    step.input ||= {};
    const schema = target?.input_schema;
    if (schema?.properties) {
      for (const [name, property] of Object.entries(schema.properties)) {
        if (!Object.hasOwn(step.input, name)) continue;
        field(panel, name, typeof step.input[name] === "string" ? step.input[name] : JSON.stringify(step.input[name]),
          (value) => { step.input[name] = value; }, { json: property.type !== "string" });
      }
      dropdown(panel, "Add input field", [{ value: "", label: "Select field" }, ...Object.keys(schema.properties).filter((name) => !Object.hasOwn(step.input, name)).map((value) => ({ value, label: value }))], "", (value) => {
        if (value) { const type = schema.properties[value].type; step.input[value] = type === "boolean" ? false : ["number", "integer"].includes(type) ? 0 : type === "array" ? [] : type === "object" ? {} : ""; }
      });
    }
    field(panel, "Input JSON", JSON.stringify(step.input, null, 2), (value) => { step.input = value; }, { json: true, object: true, multiline: true });
    const bound = node("fieldset"); bound.append(node("legend", "Bindings")); step.bindings ||= {};
    bindingsEditor(bound, step.bindings, step); panel.append(bound);
    const condition = node("fieldset"); condition.append(node("legend", "Condition"));
    if (!step.when) condition.append(button("Add condition", () => change(() => { step.when = { source: "workflow-input", path: "", operator: "equals", value: true }; })));
    else {
      dropdown(condition, "Source", ["workflow-input", "step-output"].map((value) => ({ value, label: value })), step.when.source, (value) => { step.when.source = value; delete step.when.stepId; if (value === "step-output") step.when.stepId = ""; });
      if (step.when.source === "step-output") dropdown(condition, "Upstream node", [{ value: "", label: "Select upstream node" }, ...steps().filter((item) => ancestors(step.id).has(item.id)).map((item) => ({ value: item.id, label: item.id }))], step.when.stepId, (value) => { step.when.stepId = value; });
      field(condition, "JSON Pointer", step.when.path, (value) => { step.when.path = value; });
      dropdown(condition, "Operator", ["equals", "not-equals", "in", "not-in", "exists"].map((value) => ({ value, label: value })), step.when.operator, (value) => { step.when.operator = value; });
      if (step.when.operator !== "exists") field(condition, "Comparison JSON", JSON.stringify(step.when.value ?? null), (value) => { step.when.value = value; }, { json: true });
      condition.append(button("Remove condition", () => change(() => { delete step.when; })));
    }
    panel.append(condition);
    field(panel, "Output schema JSON", JSON.stringify(step.outputSchema || {}, null, 2), (value) => { step.outputSchema = value; }, { json: true, object: true, multiline: true });
    panel.append(button("Duplicate node", () => change(() => { const duplicate = copy(step); duplicate.id = `step-${crypto.randomUUID().slice(0, 8)}`; steps().push(duplicate); selected = duplicate.id; })));
    panel.append(button("Delete node", () => {
      const referenced = steps().some((item) => (item.dependsOn || []).includes(step.id)) || Object.values(draft.definition.outputs || {}).some((binding) => binding.stepId === step.id);
      if (referenced) throw new Error("Remove downstream dependencies and output references before deleting this node.");
      change(() => { draft.definition.steps = steps().filter((item) => item.id !== step.id); selected = null; });
    }, "danger"));
  }
  function renderOutputs() {
    const container = find("wfOutputs"); container.replaceChildren();
    draft.definition.outputs ||= {}; bindingsEditor(container, draft.definition.outputs);
  }
  function render() {
    find("wfEditor").hidden = !draft; if (!draft) { status(); return; }
    for (const [id, value] of Object.entries({ wfTitle: draft.title, wfId: draft.definition.workflowId, wfVersion: draft.definition.version, wfConcurrency: draft.definition.maxConcurrency || 1 })) find(id).value = value || "";
    const solution = find("wfSolution"); solution.replaceChildren(new Option("Select Solution", ""));
    for (const item of solutions) solution.add(new Option(item.title, item.solution_id));
    if (draft.solution_id && !solutions.some((item) => item.solution_id === draft.solution_id)) solution.add(new Option(draft.solution_id, draft.solution_id));
    solution.value = draft.solution_id; solution.disabled = Boolean(draft.draft_id);
    renderGraph(); renderProperties(); renderOutputs(); status();
  }
  for (const [id, update] of Object.entries({ wfTitle: (value) => { draft.title = value; }, wfId: (value) => { draft.definition.workflowId = value; }, wfVersion: (value) => { draft.definition.version = value; }, wfConcurrency: (value) => { draft.definition.maxConcurrency = Number(value); }, wfSolution: (value) => { draft.solution_id = value; } })) find(id).addEventListener("change", (event) => change(() => update(event.target.value), false));
  const actions = {
    wfNew: async () => { if (allowDiscard()) { await refreshLibrary(); fresh(); } },
    wfReload: async () => { if (allowDiscard()) { await refreshLibrary(); if (draft?.draft_id) choose(await api(`${base}/${draft.draft_id}`)); } },
    wfSave: save,
    wfRelease: async () => {
      await save();
      window.dispatchEvent(new CustomEvent("sage:solution-review", { detail: { solutionId: draft.solution_id, draftId: draft.draft_id } }));
    },
    wfValidate: async () => {
      await save(); const report = await api(`${base}/${draft.draft_id}/validate`, { method: "POST", headers: revisionHeaders() });
      const panel = find("wfValidation"); panel.replaceChildren(node("strong", report.status === "valid" ? "Structure and active targets valid · Release validation pending" : "Validation failed"));
      for (const issue of report.issues) panel.append(button(`${issue.node_id || issue.field_path || "Workflow"}: ${issue.message}`, () => { if (issue.node_id) { selected = issue.node_id; renderGraph(); renderProperties(); } }));
    },
    wfExport: async () => { await save(); const response = await api(`${base}/${draft.draft_id}/export`, { headers: revisionHeaders() }); download(await response.text(), "workflow.yaml", "application/yaml"); },
    wfPackage: async () => { await save(); download(JSON.stringify({ format: "sage-workflow-design-v1", draft: payload() }, null, 2), "workflow-design.json", "application/json"); },
    wfDelete: async () => { if (window.confirm("Delete this workflow draft?")) { await api(`${base}/${draft.draft_id}`, { method: "DELETE", headers: revisionHeaders() }); draft = null; dirty = false; await refreshLibrary(); render(); } },
    wfAdd: () => { assertFields(); if (steps().length >= 500) throw new Error("Maximum 500 nodes"); change(() => { const kind = find("wfKind").value, target = targets.find((item) => item.kind === kind && item.enabled); const step = { id: `step-${crypto.randomUUID().slice(0, 8)}`, kind, target: target?.target || "", inheritInput: false, input: {}, dependsOn: [] }; steps().push(step); selected = step.id; }); },
    wfArrange: () => change(() => {
      draft.design.positions = {}; const layers = new Map(), rows = new Map();
      function layer(step, visiting = new Set()) { if (layers.has(step.id)) return layers.get(step.id); if (visiting.has(step.id)) return 0; visiting.add(step.id); const parents = (step.dependsOn || []).map((id) => steps().find((item) => item.id === id)).filter(Boolean); const value = parents.length ? 1 + Math.max(...parents.map((parent) => layer(parent, new Set(visiting)))) : 0; layers.set(step.id, value); return value; }
      for (const step of steps()) { const column = layer(step), row = rows.get(column) || 0; draft.design.positions[step.id] = { x: 24 + column * 240, y: 28 + row * 130 }; rows.set(column, row + 1); }
    }),
    wfUndo: () => { if (!history.length) return; future.push(copy(payload())); Object.assign(draft, history.pop()); dirty = true; find("wfValidation").replaceChildren(); render(); },
    wfRedo: () => { if (!future.length) return; history.push(copy(payload())); Object.assign(draft, future.pop()); dirty = true; find("wfValidation").replaceChildren(); render(); },
    wfImport: () => { find("wfFile").click(); },
  };
  for (const [id, action] of Object.entries(actions)) find(id).addEventListener("click", () => perform(action));
  find("wfDraft").addEventListener("change", (event) => perform(async () => {
    const id = event.target.value;
    if (!id || !allowDiscard()) { event.target.value = draft?.draft_id || ""; return; }
    choose(await api(`${base}/${id}`));
  }));
  find("wfFile").addEventListener("change", (event) => perform(async () => {
    const file = event.target.files[0]; event.target.value = "";
    if (!file || !allowDiscard()) return;
    if (file.size > 512 * 1024) throw new Error("Import exceeds 512 KiB");
    await refreshLibrary();
    const imported = await api(`${base}/import`, { method: "POST", body: JSON.stringify({ content: await file.text() }) });
    const definition = imported.definition;
    if (!Array.isArray(definition.steps) || definition.steps.some((step) => !["agent", "skill", "tool"].includes(step.kind))) throw new Error("This editor supports Agent, Skill and Tool DAG nodes only. Import was not saved.");
    if (imported.solution_id === "import") imported.solution_id = solutions[0]?.solution_id || "";
    choose(imported);
  }));
  document.querySelector('[data-view="workflows"]').addEventListener("click", () => perform(refreshLibrary));
  window.addEventListener("sage:workflow-open", (event) => perform(async () => {
    if (!allowDiscard()) return;
    await refreshLibrary();
    if (event.detail.draftId) choose(await api(`${base}/${event.detail.draftId}`));
    else fresh(event.detail.solutionId);
    navigate("workflows");
  }));
  window.addEventListener("beforeunload", (event) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
})();