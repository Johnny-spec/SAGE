(() => {
  const root = document.querySelector("#solutionQuickStart");
  const find = (id) => root.querySelector(`#${id}`);
  let busy = false;

  function selectedCapabilities() {
    return [...find("qsCapabilities").querySelectorAll("input:checked")]
      .map((input) => JSON.parse(input.value));
  }

  function option(value, label, pending) {
    const control = document.createElement("label");
    control.className = "check-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = JSON.stringify(value);
    control.append(input, document.createTextNode(`${label}${pending ? " · publish first" : ""}`));
    return control;
  }

  async function catalogTargets() {
    return (await api("/v1/admin/workflow-targets")).targets.filter((item) => item.enabled);
  }

  async function render() {
    const targetRoot = find("qsCapabilities");
    const checked = new Set(selectedCapabilities().map((item) => `${item.source}:${item.id}`));
    const targets = await catalogTargets();
    const entries = targets.map((item) => ({
      source: "active",
      id: `${item.kind}:${item.target}`,
      kind: item.kind,
      target: item.target,
      label: `${item.kind} · ${item.server_alias ? `${item.server_alias} / ` : ""}${item.name}`,
      pending: false,
    }));
    if (environment === "development") {
      for (const record of state.agents) {
        if (publicationStatus(record, "agent") !== "active") entries.push({
          source: "agent", id: record.draft_id, kind: "agent",
          target: record.definition.agent_id,
          label: `agent · ${record.definition.name}`, pending: true,
        });
      }
      for (const record of state.skills) {
        if (record.name && publicationStatus(record, "skill") !== "active") entries.push({
          source: "skill", id: record.package_id, kind: "skill",
          target: record.name,
          label: `skill · ${record.name}`, pending: true,
        });
      }
      for (const record of state.tools) {
        if (publicationStatus(record, "tool") !== "active") entries.push({
          source: "tool", id: record.registration_id, kind: "tool",
          target: record.tool_id,
          label: `tool · ${record.definition.binding.server_alias} / ${record.definition.binding.tool_name}`,
          pending: true,
        });
      }
    }
    targetRoot.replaceChildren(...entries.map((entry) => {
      const control = option(entry, entry.label, entry.pending);
      control.querySelector("input").checked = checked.has(`${entry.source}:${entry.id}`);
      return control;
    }));
    find("qsEmpty").hidden = entries.length > 0;
    find("qsCreate").textContent = environment === "development"
      ? "Create and publish"
      : "Create reviewable draft";
  }

  async function publishDependency(item) {
    const fields = {
      agent: "agent_draft_ids",
      skill: "skill_package_ids",
      tool: "tool_registration_ids",
    };
    if (item.source === "agent" || item.source === "skill") {
      const validated = await api(`/v1/admin/${item.source}s/${item.id}/validate`, {
        method: "POST",
      });
      if (!["valid", "published"].includes(validated.status)) {
        throw new Error(`${item.label} validation failed: ${JSON.stringify(validated.validation?.issues || [])}`);
      }
    }
    const candidate = await api("/v1/admin/candidates", {
      method: "POST",
      body: JSON.stringify({
        title: `Quick start · ${item.label}`,
        [fields[item.source]]: [item.id],
      }),
    });
    await api(`/v1/admin/candidates/${candidate.candidate_id}/publish`, { method: "POST" });
  }

  function workflowDefinition(solutionId, version, capabilities) {
    return {
      apiVersion: "sage.dev/v1beta1",
      kind: "WorkflowTemplate",
      workflowId: `${solutionId}-workflow`,
      version,
      steps: capabilities.map((item, index) => ({
        id: `step-${index + 1}`,
        kind: item.kind,
        target: item.target,
        dependsOn: index ? [`step-${index}`] : [],
        inheritInput: true,
      })),
    };
  }

  async function create(event) {
    event.preventDefault();
    if (busy) return;
    const capabilities = selectedCapabilities();
    if (!capabilities.length) {
      find("qsStatus").textContent = "Select at least one capability.";
      return;
    }
    busy = true;
    root.inert = true;
    find("qsStatus").textContent = "Creating...";
    try {
      if (environment !== "development" && capabilities.some((item) => item.pending)) {
        throw new Error("Production quick start only supports active capabilities.");
      }
      for (const item of capabilities.filter((entry) => entry.pending)) {
        find("qsStatus").textContent = `Publishing ${item.label}...`;
        await publishDependency(item);
      }
      const currentTargets = await catalogTargets();
      const available = new Set(currentTargets.map((item) => `${item.kind}:${item.target}`));
      for (const item of capabilities) {
        if (!available.has(`${item.kind}:${item.target}`)) {
          throw new Error(`${item.label} is not available after publication.`);
        }
      }
      const solutionId = find("qsSolutionId").value.trim();
      const title = find("qsTitle").value.trim();
      const version = find("qsVersion").value.trim();
      await api("/v1/admin/solutions", {
        method: "POST",
        body: JSON.stringify({
          solution_id: solutionId,
          title,
          owner: find("qsOwner").value.trim(),
          supported_profiles: ["durable"],
        }),
      });
      const draft = await api("/v1/admin/workflow-drafts", {
        method: "POST",
        body: JSON.stringify({
          title: `${title} workflow`,
          solution_id: solutionId,
          definition: workflowDefinition(solutionId, version, capabilities),
          design: {},
        }),
      });
      if (environment === "development") {
        const preview = await api(`/v1/admin/workflow-drafts/${draft.draft_id}/resolve`, {
          method: "POST",
          headers: { "If-Match": `"${draft.revision}"` },
          body: JSON.stringify({ version }),
        });
        if (preview.signature_required) {
          throw new Error("A signed Bundle is required; continue from Solution workbench.");
        }
        const built = await api(`/v1/admin/workflow-drafts/${draft.draft_id}/build`, {
          method: "POST",
          headers: { "If-Match": `"${draft.revision}"` },
          body: JSON.stringify({ version, build_digest: preview.build_digest }),
        });
        await api(`/v1/admin/candidates/${built.candidate.candidate_id}/publish`, {
          method: "POST",
        });
      }
      await loadAll();
      window.dispatchEvent(new CustomEvent("sage:solution-review", {
        detail: { solutionId, draftId: draft.draft_id },
      }));
      find("qsStatus").textContent = environment === "development"
        ? "Published."
        : "Draft created for review.";
      find("qsForm").reset();
      delete find("qsTitle").dataset.edited;
      find("qsOwner").value = "solution-owner";
      find("qsVersion").value = "1.0.0";
    } catch (error) {
      find("qsStatus").textContent = error.message;
      setNotice(error.message, "error");
    } finally {
      busy = false;
      root.inert = false;
    }
  }

  find("qsForm").addEventListener("submit", create);
  find("qsSolutionId").addEventListener("input", () => {
    if (!find("qsTitle").dataset.edited) find("qsTitle").value = find("qsSolutionId").value;
  });
  find("qsTitle").addEventListener("input", () => { find("qsTitle").dataset.edited = "true"; });
  find("qsOpenSkills").addEventListener("click", () => navigate("skills"));
  find("qsOpenMcp").addEventListener("click", () => navigate("mcp"));
  window.addEventListener("sage:control-loaded", () => {
    if (!busy) render().catch((error) => { find("qsStatus").textContent = error.message; });
  });
})();
