(() => {
  const root = document.querySelector("#solutionWorkbench");
  const find = (id) => root.querySelector(`#${id}`);
  const base = "/v1/admin/workflow-drafts";
  let solutions = [], drafts = [], preview = null, proposal = null, busy = false;
  let testRun = null;
  const node = (tag, text) => { const item = document.createElement(tag); item.textContent = text; return item; };
  const selectedDraft = () => drafts.find((item) => item.draft_id === find("swDraft").value);
  const selectedSolution = () => find("swSolution").value;
  function clearReview() {
    preview = null; find("swReview").hidden = true; find("swConfirm").checked = false;
    find("swResult").replaceChildren(); find("swSigned").value = "";
  }
  function clearProposal() {
    proposal = null; find("swProposal").hidden = true; find("swAiConfirm").checked = false;
    find("swApply").disabled = true;
  }
  async function perform(action) {
    if (busy) return;
    busy = true; root.inert = true; find("swStatus").textContent = "Working..."; setNotice("");
    try { await action(); }
    catch (error) { setNotice(error.message, "error"); find("swStatus").textContent = error.message; }
    finally { busy = false; root.inert = false; }
  }
  function draftOptions(preferred = find("swDraft").value) {
    const select = find("swDraft"); select.replaceChildren(new Option("Select a workflow draft", ""));
    for (const draft of drafts.filter((item) => item.solution_id === selectedSolution())) {
      select.add(new Option(`${draft.title} · r${draft.revision}`, draft.draft_id));
    }
    select.value = [...select.options].some((item) => item.value === preferred) ? preferred : "";
    find("swAiId").value = selectedSolution(); find("swAiId").disabled = Boolean(selectedSolution());
    find("swNew").disabled = !selectedSolution(); find("swEdit").disabled = !selectedDraft();
    find("swResolve").disabled = !selectedDraft();
    find("swQuick").hidden = environment !== "development";
    find("swQuick").disabled = !selectedDraft();
    find("swRuntime").hidden = environment !== "development" || !selectedDraft();
    find("swRun").disabled = !activeGeneration()?.solution_releases?.[selectedSolution()];
    find("swStatus").textContent = selectedDraft() ? `Draft revision ${selectedDraft().revision}` : "Select a workflow draft";
    renderReleaseHistory();
  }
  function renderReleaseHistory() {
    const releases = state.solutionReleases.filter((item) => item.solution_id === selectedSolution());
    find("swHistory").hidden = releases.length === 0;
    const rows = releases.map((release) => {
      const row = document.createElement("tr");
      const candidates = state.candidates.filter((item) => item.solution_release_ids.includes(release.release_id));
      const current = activeGeneration()?.solution_releases?.[release.solution_id] === release.release_id;
      const actions = document.createElement("td");
      if (!candidates.length) actions.append(actionButton("Prepare publication", () => preparePublication("candidateReleases", release.release_id, `${release.solution_id} ${release.version}`)));
      for (const candidate of candidates) actions.append(
        node("div", candidate.status === "active" ? "activated" : candidate.status),
        actionButton("Open Candidate", () => {
          navigate("catalog");
          document.querySelector(`[data-candidate-id="${CSS.escape(candidate.candidate_id)}"]`)?.scrollIntoView({ block: "center" });
        }),
      );
      row.append(node("td", release.version), node("td", current ? "active" : "inactive"), actions);
      return row;
    });
    find("swHistoryList").replaceChildren(makeTable(["Version", "Runtime", "Publication"], rows));
  }
  async function refresh(solutionId = selectedSolution(), draftId = find("swDraft").value) {
    const [items, catalog] = await Promise.all([api("/v1/admin/solutions"), api("/v1/admin/workflow-targets")]);
    const records = []; let page;
    do { page = await api(`${base}?offset=${records.length}&limit=100`); records.push(...page.items); }
    while (records.length < page.total && page.items.length);
    solutions = items; drafts = records;
    find("swSolution").replaceChildren(new Option("New Solution", ""));
    for (const item of solutions) find("swSolution").add(new Option(`${item.title} (${item.solution_id})`, item.solution_id));
    find("swSolution").value = solutionId;
    const scoped = new Set([...find("swAiTargets").selectedOptions].map((item) => item.value));
    find("swAiTargets").replaceChildren();
    for (const target of catalog.targets.filter((item) => item.enabled)) {
      const option = new Option(`${target.kind} · ${target.server_alias ? target.server_alias + " / " : ""}${target.name} · ${target.version}`, target.target);
      option.selected = scoped.has(target.target); find("swAiTargets").add(option);
    }
    draftOptions(draftId);
  }
  function openWorkflow(draftId) {
    window.dispatchEvent(new CustomEvent("sage:workflow-open", { detail: { solutionId: selectedSolution(), draftId } }));
    find("swStatus").textContent = "Workflow selected";
  }
  function requestPreview(suffix, body) {
    if (!preview) throw new Error("Resolve dependencies before building.");
    return api(`${base}/${preview.draft_id}/${suffix}`, { method: "POST",
      headers: { "If-Match": `"${preview.draft_revision}"` },
      body: body || JSON.stringify({ version: preview.manifest.metadata.version, build_digest: preview.build_digest }),
    });
  }
  function renderReview() {
    const rows = preview.dependencies.map((dependency) => {
      const row = document.createElement("tr");
      const risk = preview.risks[dependency.kind === "skill" ? `skill:${dependency.id}` : dependency.id] || "-";
      for (const value of [dependency.kind, dependency.id, dependency.version, risk, dependency.required_by.join(", "), dependency.contentDigest]) row.append(node("td", value));
      return row;
    });
    find("swDependencies").replaceChildren(makeTable(["Kind", "Capability", "Version", "Risk", "Required by", "Digest"], rows));
    find("swSnapshot").textContent = `r${preview.draft_revision} · ${preview.generation_id}`;
    find("swManifest").textContent = JSON.stringify(preview.manifest, null, 2);
    find("swWarnings").replaceChildren(...preview.warnings.map((warning) => node("li", warning)));
    find("swBuild").hidden = preview.signature_required;
    find("swSignedLabel").hidden = !preview.signature_required;
    find("swBuildSigned").hidden = !preview.signature_required;
    find("swReview").hidden = false; find("swConfirm").checked = false;
    updateConfirmation(); find("swStatus").textContent = "Dependencies resolved";
  }
  function updateConfirmation() {
    find("swBuild").disabled = !find("swConfirm").checked;
    find("swBundle").disabled = !find("swConfirm").checked;
    find("swBuildSigned").disabled = !find("swConfirm").checked || !find("swSigned").files.length;
  }
  function renderResult(result) {
    const output = find("swResult"); const candidate = result.candidate;
    output.replaceChildren(node("h3", `${result.release.release_id} · ${candidate.status}`), node("p", candidate.candidate_id));
    const validation = document.createElement("div"); renderValidation(validation, candidate.validation); output.append(validation);
    const actions = node("div", ""); actions.className = "form-actions";
    actions.append(actionButton("Open Catalog", () => navigate("catalog")));
    if (candidate.status === "valid") actions.append(actionButton("Submit for approval", () => perform(async () => {
      await api(`/v1/admin/candidates/${candidate.candidate_id}/submit`, { method: "POST" });
      await loadAll(); navigate("catalog"); find("swStatus").textContent = "Submitted for approval";
      document.querySelector("#approvalList").scrollIntoView({ block: "start" });
    })));
    output.append(actions); find("swStatus").textContent = result.replayed ? "Existing build returned" : "Release and Candidate created";
  }
  async function applyDevelopmentBuild(result) {
    if (environment === "development" && result.candidate.status !== "active") {
      await api(`/v1/admin/candidates/${result.candidate.candidate_id}/publish`, { method: "POST" });
      await loadAll();
      result.candidate = state.candidates.find((item) => item.candidate_id === result.candidate.candidate_id);
    }
    renderResult(result);
  }
  function runPayload() {
    const payload = JSON.parse(find("swRunInput").value || "{}");
    if (!payload || Array.isArray(payload) || typeof payload !== "object") throw new Error("Workflow input must be a JSON object.");
    return payload;
  }
  async function refreshRun() {
    if (!testRun) return;
    const run = await api(`/v1/durable-runs/${encodeURIComponent(testRun.run_id)}`);
    find("swRunStatus").textContent = `${run.status} · ${run.run_id}`;
    find("swRunCancel").disabled = run.status !== "running";
    const events = await api(`/v1/runs/${encodeURIComponent(run.run_id)}/events`);
    const result = run.status === "completed" ? await api(`/v1/durable-runs/${encodeURIComponent(run.run_id)}/result`) : null;
    find("swRunResult").textContent = JSON.stringify({ result, events }, null, 2);
  }
  async function startRun() {
    if (environment !== "development") throw new Error("Workbench test runs are development-only.");
    const draft = selectedDraft();
    if (!draft) throw new Error("Select a workflow first.");
    testRun = await api("/v1/durable-runs", { method: "POST", body: JSON.stringify({
      solution_id: draft.solution_id, workflow_type: draft.definition.workflowId,
      idempotency_key: crypto.randomUUID(), payload: runPayload(),
    }) });
    find("swRunRefresh").disabled = false;
    find("swRunCancel").disabled = false;
    find("swRunResult").textContent = JSON.stringify(testRun, null, 2);
    find("swRunStatus").textContent = `started · ${testRun.run_id}`;
    find("swStatus").textContent = "Run started";
    await refreshRun();
  }
  const actions = {
    swNew: () => openWorkflow(null), swEdit: () => openWorkflow(selectedDraft().draft_id),
    swRun: startRun,
    swQuickRun: async () => { runPayload(); await actions.swQuick(); await startRun(); },
    swRunRefresh: refreshRun,
    swRunCancel: async () => {
      if (!testRun) return;
      await api(`/v1/durable-runs/${encodeURIComponent(testRun.run_id)}/cancel`, { method: "POST", body: "{}" });
      await refreshRun();
    },
    swQuick: async () => {
      if (environment !== "development") throw new Error("Quick publish is development-only.");
      const draft = selectedDraft();
      if (!draft) throw new Error("Save and select a workflow first.");
      clearReview();
      const existing = new Set(state.solutionReleases.filter((item) => item.solution_id === draft.solution_id).map((item) => item.version));
      let version = find("swVersion").value.trim();
      while (existing.has(version) && /^\d+\.\d+\.\d+$/.test(version)) {
        const parts = version.split("."); parts[2] = String(Number(parts[2]) + 1); version = parts.join(".");
      }
      find("swVersion").value = version;
      preview = await api(`${base}/${draft.draft_id}/resolve`, { method: "POST", headers: { "If-Match": `"${draft.revision}"` }, body: JSON.stringify({ version }) });
      if (preview.signature_required) throw new Error("This environment requires a signed Bundle; use the signed build workflow.");
      const result = await requestPreview("build");
      await loadAll(); renderResult(result);
      const published = await api(`/v1/admin/candidates/${result.candidate.candidate_id}/publish`, { method: "POST" });
      await loadAll(); clearReview(); renderReleaseHistory();
      find("swResult").replaceChildren(node("h3", `${result.release.release_id} · published`), node("p", `Generation: ${published.generation_id}`));
      find("swStatus").textContent = "Published";
      find("swRun").disabled = false;
    },
    swResolve: async () => {
      const draft = selectedDraft(); clearReview();
      if (!draft) throw new Error("Select a workflow draft.");
      preview = await api(`${base}/${draft.draft_id}/resolve`, { method: "POST", headers: { "If-Match": `"${draft.revision}"` }, body: JSON.stringify({ version: find("swVersion").value.trim() }) });
      renderReview();
    },
    swBundle: async () => {
      const response = await requestPreview("bundle");
      const url = URL.createObjectURL(await response.blob()), link = document.createElement("a");
      link.href = url; link.download = `${preview.manifest.metadata.name}-${preview.manifest.metadata.version}.zip`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      find("swStatus").textContent = "Unsigned Bundle downloaded";
    },
    swBuild: async () => { const result = await requestPreview("build"); await loadAll(); await applyDevelopmentBuild(result); },
    swBuildSigned: async () => {
      const form = new FormData(); form.append("file", find("swSigned").files[0]);
      form.append("version", preview.manifest.metadata.version); form.append("build_digest", preview.build_digest);
      const result = await requestPreview("build-signed", form); await loadAll(); await applyDevelopmentBuild(result);
    },
    swSuggest: async () => {
      clearProposal();
      const solutionId = selectedSolution() || find("swAiId").value.trim();
      if (!solutionId || !find("swGoal").value.trim()) throw new Error("Solution ID and goal are required.");
      proposal = await api("/v1/admin/solution-assist", { method: "POST", body: JSON.stringify({ solution_id: solutionId, goal: find("swGoal").value.trim(), targets: [...find("swAiTargets").selectedOptions].map((item) => item.value) }) });
      find("swAiTitle").value = proposal.title; find("swAiDescription").value = proposal.description;
      find("swRecommendations").replaceChildren(...proposal.recommendations.map((item) => node("li", `${item.step_id}: ${item.reason}`)), ...proposal.warnings.map((item) => node("li", item)));
      find("swAiSource").textContent = JSON.stringify({ definition: proposal.draft.definition, risks: proposal.risks }, null, 2);
      find("swProposal").hidden = false; find("swStatus").textContent = "Proposal ready for review · not saved";
    },
    swApply: async () => {
      if (!proposal || !find("swAiConfirm").checked) throw new Error("Review the proposal first.");
      const catalog = await api("/v1/admin/workflow-targets");
      if (catalog.generation_id !== proposal.generation_id) throw new Error("Capabilities changed; generate a new proposal.");
      const title = find("swAiTitle").value.trim(), owner = find("swAiOwner").value.trim();
      if (!title || !owner) throw new Error("Title and owner are required.");
      const current = await api("/v1/admin/solutions");
      if (!current.some((item) => item.solution_id === proposal.solution_id)) await api("/v1/admin/solutions", { method: "POST", body: JSON.stringify({ solution_id: proposal.solution_id, title, description: find("swAiDescription").value, owner, supported_profiles: ["durable"] }) });
      const draft = await api(base, { method: "POST", body: JSON.stringify({ ...proposal.draft, title }) });
      clearProposal(); clearReview(); await loadAll(); await refresh(draft.solution_id, draft.draft_id);
      openWorkflow(draft.draft_id);
    },
  };
  for (const [id, action] of Object.entries(actions)) find(id).addEventListener("click", () => perform(action));
  find("swSolution").addEventListener("change", () => {
    clearReview(); clearProposal(); draftOptions("");
    testRun = null; find("swRunStatus").textContent = ""; find("swRunResult").textContent = "";
    find("swRunRefresh").disabled = true; find("swRunCancel").disabled = true;
    if (!selectedSolution()) openSolutionCreator();
  });
  find("swDraft").addEventListener("change", () => { clearReview(); draftOptions(); });
  find("swVersion").addEventListener("input", clearReview);
  find("swConfirm").addEventListener("change", updateConfirmation);
  find("swSigned").addEventListener("change", updateConfirmation);
  for (const id of ["swGoal", "swAiId", "swAiTargets"]) find(id).addEventListener("input", clearProposal);
  for (const id of ["swAiTitle", "swAiDescription", "swAiOwner"]) find(id).addEventListener("input", () => { find("swAiConfirm").checked = false; find("swApply").disabled = true; });
  find("swAiConfirm").addEventListener("change", () => { find("swApply").disabled = !find("swAiConfirm").checked; });
  let pendingSolution = null;
  window.addEventListener("sage:solution-created", (event) => {
    pendingSolution = event.detail.solutionId;
    if (!busy) perform(async () => { await refresh(pendingSolution, ""); pendingSolution = null; });
    document.querySelector("#solutionDefinitions").open = false;
    root.scrollIntoView({ block: "start" });
  });
  window.addEventListener("sage:control-loaded", () => { if (!busy) perform(async () => { clearReview(); clearProposal(); await refresh(); if (pendingSolution) { await refresh(pendingSolution, ""); pendingSolution = null; } }); });
  window.addEventListener("sage:solution-review", (event) => perform(async () => {
    clearReview(); clearProposal(); await refresh(event.detail.solutionId, event.detail.draftId); navigate("solutions");
  }));
  document.querySelector('[data-view="solutions"]').addEventListener("click", () => perform(async () => { clearReview(); await refresh(); }));
})();