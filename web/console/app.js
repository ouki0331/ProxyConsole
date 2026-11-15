const DOMAIN_RE =
  /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/;
const HOST_RE = /^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$/;

const appState = {
  routes: [],
  applyStatus: null,
  discoveryAvailable: true,
  discoveryError: null,
  containers: [],
  targets: [],
  copySourceRoute: null,
  deleteTargetRoute: null,
  pendingOps: 0,
  // DEV-031 state
  workspaceView: "all",
  captures: [],
  capturesTotal: 0,
  capturesOffset: 0,
  selectedCaptureId: null,
  selectedCapture: null,
  savedDrafts: [],
  activeDraftId: null,
  activeDraft: null,
  draftDirty: false,
  lastReplayResult: null,
  lastSourceCaptureIdForRecovery: null,
  // DEV-041 state
  rules: [],
  rulesAvailable: true,
  rulesError: null,
  selectedRuleId: null,
  ruleFormDirty: false,
  copySourceRule: null,
  deleteTargetRule: null,
  pendingRuleDiscardCallback: null,
  lastRulePreviewResult: null,
};

function beginOp() {
  appState.pendingOps += 1;
  document.body.dataset.busy = "true";
}

function endOp() {
  appState.pendingOps = Math.max(0, appState.pendingOps - 1);
  if (appState.pendingOps === 0) {
    document.body.dataset.busy = "false";
  }
}

// DOM Elements
const feedbackBanner = document.getElementById("feedback-banner");
const feedbackTitle = document.getElementById("feedback-title");
const feedbackMessage = document.getElementById("feedback-message");
const feedbackHint = document.getElementById("feedback-hint");

const applySyncBadge = document.getElementById("apply-sync-badge");
const applyStatusText = document.getElementById("apply-status-text");
const applyGenerationText = document.getElementById("apply-generation-text");
const applyShaText = document.getElementById("apply-sha-text");
const applyTimeText = document.getElementById("apply-time-text");
const routesCountBadge = document.getElementById("routes-count-badge");
const applyErrorBox = document.getElementById("apply-error-box");
const applyErrorMessage = document.getElementById("apply-error-message");
const applyRollbackNote = document.getElementById("apply-rollback-note");
const retryApplyBtn = document.getElementById("retry-apply-btn");

const routesEmptyState = document.getElementById("routes-empty-state");
const routesTbody = document.getElementById("routes-tbody");

const discoveryAvailabilityBadge = document.getElementById(
  "discovery-availability-badge"
);
const discoveryErrorBox = document.getElementById("discovery-error-box");
const discoveryErrorMessage = document.getElementById(
  "discovery-error-message"
);
const discoveryEmptyState = document.getElementById("discovery-empty-state");
const dockerTbody = document.getElementById("docker-tbody");
const includeSelfCheckbox = document.getElementById("include-self-checkbox");
const refreshDiscoveryBtn = document.getElementById("refresh-discovery-btn");
const switchToManualBtn = document.getElementById("switch-to-manual-btn");

const routeForm = document.getElementById("route-form");
const routeFormTitle = document.getElementById("route-form-title");
const routeFormModeBadge = document.getElementById("route-form-mode-badge");
const routeEditModeInput = document.getElementById("route-edit-mode");
const routeOriginalIdInput = document.getElementById("route-original-id");
const routeDomainInput = document.getElementById("route-domain");
const routeDomainError = document.getElementById("route-domain-error");
const routeIdInput = document.getElementById("route-id-input");
const modeDockerRadio = document.getElementById("mode-docker");
const modeManualRadio = document.getElementById("mode-manual");
const dockerTargetFields = document.getElementById("docker-target-fields");
const manualTargetFields = document.getElementById("manual-target-fields");
const routeTargetSelect = document.getElementById("route-target-select");
const routeTargetError = document.getElementById("route-target-error");
const routeUpstreamScheme = document.getElementById("route-upstream-scheme");
const routeUpstreamHost = document.getElementById("route-upstream-host");
const routeHostError = document.getElementById("route-host-error");
const routeUpstreamPort = document.getElementById("route-upstream-port");
const routePortError = document.getElementById("route-port-error");
const routeDescriptionInput = document.getElementById("route-description");
const routeEnabledCheckbox = document.getElementById("route-enabled-checkbox");
const routeSubmitBtn = document.getElementById("route-submit-btn");
const routeCancelEditBtn = document.getElementById("route-cancel-edit-btn");

const copyRouteDialog = document.getElementById("copy-route-dialog");
const copyRouteForm = document.getElementById("copy-route-form");
const copySourceSummary = document.getElementById("copy-source-summary");
const copySourceRouteIdInput = document.getElementById("copy-source-route-id");
const copyNewDomainInput = document.getElementById("copy-new-domain");
const copyDomainError = document.getElementById("copy-domain-error");
const copyNewRouteIdInput = document.getElementById("copy-new-route-id");
const copyNewDescriptionInput = document.getElementById("copy-new-description");
const copyCancelBtn = document.getElementById("copy-cancel-btn");
const copyConfirmBtn = document.getElementById("copy-confirm-btn");

const deleteRouteDialog = document.getElementById("delete-route-dialog");
const deleteRouteForm = document.getElementById("delete-route-form");
const deleteTargetRouteId = document.getElementById("delete-target-route-id");
const deleteTargetDomain = document.getElementById("delete-target-domain");
const deleteRouteIdInput = document.getElementById("delete-route-id-input");
const deleteCancelBtn = document.getElementById("delete-cancel-btn");
const deleteConfirmBtn = document.getElementById("delete-confirm-btn");

function setFeedback(state, title, message, { errorCode = "", hint = "" } = {}) {
  feedbackBanner.dataset.state = state;
  feedbackBanner.dataset.errorCode = errorCode || "";
  feedbackTitle.textContent = title;
  feedbackMessage.textContent = message;
  feedbackHint.textContent = hint;
}

function clearFieldError(inputEl, errorEl) {
  if (inputEl) {
    inputEl.removeAttribute("aria-invalid");
    inputEl.setCustomValidity("");
  }
  if (errorEl) {
    errorEl.textContent = "";
  }
}

function setFieldError(inputEl, errorEl, message) {
  if (inputEl) {
    inputEl.setAttribute("aria-invalid", "true");
    inputEl.setCustomValidity(message);
  }
  if (errorEl) {
    errorEl.textContent = message;
  }
}

function clearAllFormErrors() {
  clearFieldError(routeDomainInput, routeDomainError);
  clearFieldError(routeTargetSelect, routeTargetError);
  clearFieldError(routeUpstreamHost, routeHostError);
  clearFieldError(routeUpstreamPort, routePortError);
}

function getSelectedTargetMode() {
  return modeManualRadio.checked ? "manual" : "docker";
}

function syncTargetModeUI() {
  const mode = getSelectedTargetMode();
  if (mode === "docker") {
    dockerTargetFields.hidden = false;
    manualTargetFields.hidden = true;
  } else {
    dockerTargetFields.hidden = true;
    manualTargetFields.hidden = false;
  }
  clearFieldError(routeTargetSelect, routeTargetError);
  clearFieldError(routeUpstreamHost, routeHostError);
  clearFieldError(routeUpstreamPort, routePortError);
}

async function apiRequest(path, options = {}) {
  const resp = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  let data = {};
  try {
    data = await resp.json();
  } catch (_err) {
    data = {};
  }
  if (!resp.ok) {
    const err = new Error(data.message || data.last_error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.code = data.error || data.status || "api_error";
    err.payload = data;
    throw err;
  }
  return data;
}

function renderApplyStatus(applyStatus, routes) {
  if (!applyStatus) {
    return;
  }
  const inSync = Boolean(applyStatus.in_sync);
  applySyncBadge.dataset.inSync = String(inSync);
  applySyncBadge.textContent = inSync ? "In Sync" : "Out of Sync / Error";
  applySyncBadge.className = `badge ${inSync ? "badge-success" : "badge-danger"}`;

  const lastStatus = applyStatus.last_apply_status || "applied";
  applyStatusText.textContent = lastStatus;
  applyStatusText.className = `badge ${
    lastStatus === "applied" ? "badge-outline" : "badge-danger"
  }`;

  applyGenerationText.textContent = `${applyStatus.desired_generation ?? 1} / ${
    applyStatus.applied_generation ?? 1
  }`;
  const sha = applyStatus.active_config_sha256 || "";
  applyShaText.textContent = sha ? `${sha.slice(0, 12)}...` : "-";
  applyTimeText.textContent = applyStatus.last_apply_at || "-";

  const enabledCount = routes.filter((r) => r.enabled).length;
  routesCountBadge.textContent = `${enabledCount} / ${routes.length}`;

  if (lastStatus !== "applied" || applyStatus.last_error) {
    applyErrorBox.hidden = false;
    applyErrorMessage.textContent =
      applyStatus.last_error || `Last apply ended with status: ${lastStatus}`;
    applyRollbackNote.textContent = applyStatus.rolled_back
      ? "Automatic rollback completed: previous working Nginx config remains active."
      : "Candidate config was rejected before replacing the active Nginx config.";
  } else {
    applyErrorBox.hidden = true;
    applyErrorMessage.textContent = "";
    applyRollbackNote.textContent = "";
  }
}

function renderRoutesTable(routes, applyStatus) {
  routesTbody.textContent = "";
  if (!routes || routes.length === 0) {
    routesEmptyState.hidden = false;
    return;
  }
  routesEmptyState.hidden = true;

  const applyStatusLabel = applyStatus?.in_sync
    ? "applied"
    : applyStatus?.last_apply_status || "out_of_sync";

  for (const route of routes) {
    const tr = document.createElement("tr");
    tr.dataset.routeId = route.route_id;
    tr.dataset.domain = route.domain;

    // 1. Domain row header
    const thDomain = document.createElement("th");
    thDomain.scope = "row";
    const domainStrong = document.createElement("strong");
    domainStrong.textContent = route.domain;
    thDomain.appendChild(domainStrong);

    const idMeta = document.createElement("span");
    idMeta.className = "cell-meta";
    idMeta.textContent = route.copied_from_route_id
      ? `${route.route_id} (copied from ${route.copied_from_route_id})`
      : route.route_id;
    thDomain.appendChild(idMeta);

    if (route.description) {
      const descMeta = document.createElement("span");
      descMeta.className = "cell-meta";
      descMeta.textContent = route.description;
      thDomain.appendChild(descMeta);
    }
    tr.appendChild(thDomain);

    // 2. Enabled badge
    const tdEnabled = document.createElement("td");
    const enBadge = document.createElement("span");
    enBadge.className = `badge ${
      route.enabled ? "badge-success" : "badge-neutral"
    }`;
    enBadge.dataset.enabled = String(Boolean(route.enabled));
    enBadge.textContent = route.enabled ? "Enabled" : "Disabled";
    tdEnabled.appendChild(enBadge);
    tr.appendChild(tdEnabled);

    // 3. Upstream URL
    const tdUpstream = document.createElement("td");
    const upCode = document.createElement("code");
    upCode.textContent = route.upstream_url;
    tdUpstream.appendChild(upCode);
    tr.appendChild(tdUpstream);

    // 4. Target Type (Docker vs Manual)
    const tdType = document.createElement("td");
    const isDocker = Boolean(route.target_metadata?.target_id);
    const typeBadge = document.createElement("span");
    typeBadge.className = "badge badge-outline";
    typeBadge.dataset.targetType = isDocker ? "docker" : "manual";
    typeBadge.textContent = isDocker
      ? `Docker (${route.target_metadata.target_id})`
      : "Manual";
    tdType.appendChild(typeBadge);
    tr.appendChild(tdType);

    // 5. Target State
    const tdState = document.createElement("td");
    const tState = route.target_state || "unknown";
    const stateBadge = document.createElement("span");
    stateBadge.dataset.targetState = tState;
    if (tState === "running") {
      stateBadge.className = "badge badge-success";
    } else if (tState === "stopped") {
      stateBadge.className = "badge badge-danger";
    } else {
      stateBadge.className = "badge badge-neutral";
    }
    stateBadge.textContent = tState;
    tdState.appendChild(stateBadge);
    tr.appendChild(tdState);

    // 6. Apply Status
    const tdApply = document.createElement("td");
    const routeApplyBadge = document.createElement("span");
    const routeApplyText = route.enabled ? applyStatusLabel : "disabled";
    routeApplyBadge.className = `badge ${
      routeApplyText === "applied"
        ? "badge-success"
        : routeApplyText === "disabled"
        ? "badge-neutral"
        : "badge-warn"
    }`;
    routeApplyBadge.textContent = routeApplyText;
    tdApply.appendChild(routeApplyBadge);
    tr.appendChild(tdApply);

    // 7. Actions
    const tdActions = document.createElement("td");
    const actionsWrap = document.createElement("div");
    actionsWrap.className = "row-actions";

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "btn btn-sm btn-secondary btn-toggle-enable";
    toggleBtn.dataset.routeId = route.route_id;
    toggleBtn.textContent = route.enabled ? "Disable" : "Enable";
    toggleBtn.addEventListener("click", () =>
      handleToggleRoute(route.route_id, !route.enabled)
    );
    actionsWrap.appendChild(toggleBtn);

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "btn btn-sm btn-secondary btn-edit-route";
    editBtn.dataset.routeId = route.route_id;
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", () => populateFormForEdit(route));
    actionsWrap.appendChild(editBtn);

    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "btn btn-sm btn-secondary btn-copy-route";
    copyBtn.dataset.routeId = route.route_id;
    copyBtn.textContent = "Copy";
    copyBtn.addEventListener("click", () => openCopyDialog(route));
    actionsWrap.appendChild(copyBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "btn btn-sm btn-danger btn-delete-route";
    deleteBtn.dataset.routeId = route.route_id;
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => openDeleteDialog(route));
    actionsWrap.appendChild(deleteBtn);

    tdActions.appendChild(actionsWrap);
    tr.appendChild(tdActions);
    routesTbody.appendChild(tr);
  }
}

function renderDockerDiscovery() {
  const available = Boolean(appState.discoveryAvailable);
  discoveryAvailabilityBadge.dataset.available = String(available);
  discoveryAvailabilityBadge.textContent = available
    ? "Docker Available"
    : "Docker Unavailable";
  discoveryAvailabilityBadge.className = `badge ${
    available ? "badge-success" : "badge-danger"
  }`;

  if (!available) {
    discoveryErrorBox.hidden = false;
    discoveryErrorMessage.textContent =
      appState.discoveryError ||
      "Cannot connect to Docker Engine socket. Manual upstream target entry remains available.";
  } else {
    discoveryErrorBox.hidden = true;
    discoveryErrorMessage.textContent = "";
  }

  // Populate <select id="route-target-select">
  const prevSelected = routeTargetSelect.value;
  routeTargetSelect.textContent = "";
  const defaultOpt = document.createElement("option");
  defaultOpt.value = "";
  defaultOpt.textContent = available
    ? "-- Select a container:port target --"
    : "-- Docker discovery unavailable (use Manual Mode) --";
  routeTargetSelect.appendChild(defaultOpt);

  for (const t of appState.targets) {
    const opt = document.createElement("option");
    opt.value = t.target_id;
    opt.dataset.selectable = String(Boolean(t.selectable));
    opt.dataset.state = t.state;
    opt.dataset.isSelf = String(Boolean(t.is_self));
    let suffix = "";
    if (t.is_self) {
      suffix = " [SYSTEM - not selectable]";
    } else if (!t.running) {
      suffix = " [STOPPED - not selectable]";
    } else {
      suffix = ` (${t.upstream_url}, running)`;
    }
    opt.textContent = `${t.target_id}${suffix}`;
    routeTargetSelect.appendChild(opt);
  }

  if (
    prevSelected &&
    Array.from(routeTargetSelect.options).some((o) => o.value === prevSelected)
  ) {
    routeTargetSelect.value = prevSelected;
  }

  // Render Docker Containers & Ports table
  dockerTbody.textContent = "";
  if (!appState.containers || appState.containers.length === 0) {
    discoveryEmptyState.hidden = false;
    return;
  }
  discoveryEmptyState.hidden = true;

  for (const c of appState.containers) {
    const ports = c.ports && c.ports.length > 0 ? c.ports : [null];
    for (const p of ports) {
      const tr = document.createElement("tr");
      tr.dataset.serviceName = c.service_name;
      tr.dataset.containerState = c.state;
      if (p) {
        tr.dataset.targetId = p.target_id;
      }

      // 1. Container / Service
      const thContainer = document.createElement("th");
      thContainer.scope = "row";
      const svcStrong = document.createElement("strong");
      svcStrong.textContent = c.service_name || c.container_name;
      thContainer.appendChild(svcStrong);

      if (c.is_self) {
        const sysBadge = document.createElement("span");
        sysBadge.className = "badge badge-warn";
        sysBadge.style.marginInlineStart = "0.4rem";
        sysBadge.textContent = "SYSTEM";
        thContainer.appendChild(sysBadge);
      }

      const metaSpan = document.createElement("span");
      metaSpan.className = "cell-meta";
      metaSpan.textContent = `${c.container_name} (${c.container_short_id}) · ${c.image}`;
      thContainer.appendChild(metaSpan);
      tr.appendChild(thContainer);

      // 2. State
      const tdState = document.createElement("td");
      const stBadge = document.createElement("span");
      stBadge.dataset.state = c.state;
      stBadge.className = `badge ${
        c.running ? "badge-success" : "badge-danger"
      }`;
      stBadge.textContent = c.state;
      tdState.appendChild(stBadge);
      tr.appendChild(tdState);

      // 3. Network & Host
      const tdNet = document.createElement("td");
      const netNames = (c.networks || []).map((n) => n.network_name).join(", ");
      const hostCode = document.createElement("code");
      hostCode.textContent = c.preferred_host;
      tdNet.appendChild(hostCode);
      const netMeta = document.createElement("span");
      netMeta.className = "cell-meta";
      netMeta.textContent = netNames || "no network";
      tdNet.appendChild(netMeta);
      tr.appendChild(tdNet);

      // 4. Port Candidate
      const tdPort = document.createElement("td");
      if (p) {
        const portCode = document.createElement("code");
        portCode.textContent = `${p.internal_port}/${p.protocol} (${p.target_id})`;
        tdPort.appendChild(portCode);
      } else {
        tdPort.textContent = "No exposed TCP ports";
      }
      tr.appendChild(tdPort);

      // 5. Published
      const tdPub = document.createElement("td");
      if (p && p.published_ports && p.published_ports.length > 0) {
        tdPub.textContent = p.published_ports
          .map((pub) => `${pub.host_ip || "0.0.0.0"}:${pub.host_port}`)
          .join(", ");
      } else {
        tdPub.textContent = "internal only";
      }
      tr.appendChild(tdPub);

      // 6. Action
      const tdAction = document.createElement("td");
      if (p) {
        const selectBtn = document.createElement("button");
        selectBtn.type = "button";
        selectBtn.className = "btn btn-sm btn-secondary btn-select-target";
        selectBtn.dataset.targetId = p.target_id;
        if (p.selectable) {
          selectBtn.textContent = "Use Target";
          selectBtn.addEventListener("click", () => {
            modeDockerRadio.checked = true;
            syncTargetModeUI();
            routeTargetSelect.value = p.target_id;
            clearFieldError(routeTargetSelect, routeTargetError);
            setFeedback(
              "idle",
              "Target Selected",
              `Selected Docker target ${p.target_id} (${p.upstream_url}). Enter a domain and click Save & Apply Route.`
            );
            routeDomainInput.focus();
          });
        } else {
          selectBtn.disabled = true;
          selectBtn.textContent = c.is_self
            ? "System (Guarded)"
            : "Stopped (Unavailable)";
        }
        tdAction.appendChild(selectBtn);
      } else {
        tdAction.textContent = "-";
      }
      tr.appendChild(tdAction);

      dockerTbody.appendChild(tr);
    }
  }
}

async function loadAllState({
  refreshDiscovery = false,
  preserveBanner = false,
} = {}) {
  beginOp();
  try {
    if (!preserveBanner) {
      setFeedback("loading", "Loading...", "Fetching state from local control API...");
    }
    const incSelf = includeSelfCheckbox.checked ? "true" : "false";

    const [routesData, statusData, containersData, targetsData] =
      await Promise.all([
        apiRequest("/api/v1/routes"),
        apiRequest("/api/v1/routes/status"),
        refreshDiscovery
          ? apiRequest("/api/v1/discovery/refresh", {
              method: "POST",
              body: JSON.stringify({
                include_self: includeSelfCheckbox.checked,
                include_stopped: true,
              }),
            })
          : apiRequest(`/api/v1/discovery/containers?include_self=${incSelf}`),
        apiRequest(
          `/api/v1/discovery/targets?include_self=${incSelf}&include_stopped=true`
        ),
      ]);

    appState.routes = routesData.items || [];
    appState.applyStatus = statusData.apply_status || routesData.apply_status;
    appState.discoveryAvailable = Boolean(containersData.available);
    appState.discoveryError = containersData.error || null;
    appState.containers =
      containersData.items || containersData.containers || [];
    appState.targets = targetsData.items || [];

    renderApplyStatus(appState.applyStatus, appState.routes);
    renderRoutesTable(appState.routes, appState.applyStatus);
    renderDockerDiscovery();

    await Promise.all([
      loadCapturesList({ preserveBanner: true }),
      loadSavedDraftsList({ preserveBanner: true }),
      loadRulesList({ preserveBanner: true }),
    ]);

    if (!preserveBanner) {
      if (!appState.discoveryAvailable) {
        setFeedback(
          "error",
          "Docker Discovery Unavailable",
          appState.discoveryError ||
            "Docker socket could not be reached. Manual upstream target mode is available.",
          {
            errorCode: "discovery_unavailable",
            hint: "Switch to 'Manual Scheme / Host / Port' to create or edit routes while Docker discovery is unavailable.",
          }
        );
      } else if (
        appState.applyStatus &&
        appState.applyStatus.last_apply_status !== "applied"
      ) {
        setFeedback(
          "error",
          `Nginx Apply Error (${appState.applyStatus.last_apply_status})`,
          appState.applyStatus.last_error ||
            "Last router configuration apply failed.",
          {
            errorCode: appState.applyStatus.last_apply_status,
            hint: "Existing routes remain active. Review the error box above or click Retry Apply Config.",
          }
        );
      } else {
        setFeedback(
          "idle",
          "Ready",
          `Loaded ${appState.routes.length} routes and ${appState.containers.length} Docker containers.`
        );
      }
    }
  } finally {
    endOp();
  }
}

function resetRouteForm() {
  routeEditModeInput.value = "create";
  routeOriginalIdInput.value = "";
  routeFormTitle.textContent = "Create New Route";
  routeFormModeBadge.textContent = "Create Mode";
  routeDomainInput.value = "";
  routeIdInput.value = "";
  routeIdInput.readOnly = false;
  routeDescriptionInput.value = "";
  routeEnabledCheckbox.checked = true;
  routeUpstreamScheme.value = "http";
  routeUpstreamHost.value = "";
  routeUpstreamPort.value = "8080";
  routeTargetSelect.value = "";
  if (appState.discoveryAvailable) {
    modeDockerRadio.checked = true;
  } else {
    modeManualRadio.checked = true;
  }
  syncTargetModeUI();
  clearAllFormErrors();
}

function populateFormForEdit(route) {
  clearAllFormErrors();
  routeEditModeInput.value = "edit";
  routeOriginalIdInput.value = route.route_id;
  routeFormTitle.textContent = `Edit Route: ${route.route_id}`;
  routeFormModeBadge.textContent = "Edit Mode";
  routeDomainInput.value = route.domain;
  routeIdInput.value = route.route_id;
  routeIdInput.readOnly = true;
  routeDescriptionInput.value = route.description || "";
  routeEnabledCheckbox.checked = Boolean(route.enabled);
  routeUpstreamScheme.value = route.upstream_scheme || "http";
  routeUpstreamHost.value = route.upstream_host || "";
  routeUpstreamPort.value = String(route.upstream_port || 8080);

  const dockerTid = route.target_metadata?.target_id;
  if (
    dockerTid &&
    appState.discoveryAvailable &&
    Array.from(routeTargetSelect.options).some((o) => o.value === dockerTid)
  ) {
    modeDockerRadio.checked = true;
    syncTargetModeUI();
    routeTargetSelect.value = dockerTid;
  } else {
    modeManualRadio.checked = true;
    syncTargetModeUI();
  }
  routeDomainInput.focus();
  setFeedback(
    "idle",
    "Editing Route",
    `Loaded ${route.route_id} (${route.domain}) into the form for editing.`
  );
}

function validateDomainField() {
  const val = routeDomainInput.value.trim().toLowerCase();
  if (!val) {
    setFieldError(
      routeDomainInput,
      routeDomainError,
      "Domain is required (e.g. app.local.test)."
    );
    return false;
  }
  if (val.length > 253 || !DOMAIN_RE.test(val)) {
    setFieldError(
      routeDomainInput,
      routeDomainError,
      `Invalid domain format: '${val}'. Use lowercase letters, digits, hyphens, and dots.`
    );
    return false;
  }
  clearFieldError(routeDomainInput, routeDomainError);
  return true;
}

function validateTargetSelectionField() {
  const tid = routeTargetSelect.value.trim();
  if (!tid) {
    setFieldError(
      routeTargetSelect,
      routeTargetError,
      "Please select a running Docker container:port target, or switch to Manual Mode."
    );
    return false;
  }
  const selectedOpt = routeTargetSelect.selectedOptions[0];
  if (selectedOpt && selectedOpt.dataset.isSelf === "true") {
    setFieldError(
      routeTargetSelect,
      routeTargetError,
      `Target '${tid}' is a ProxyConsole system container and cannot be selected as a route upstream.`
    );
    return false;
  }
  if (selectedOpt && selectedOpt.dataset.state === "stopped") {
    setFieldError(
      routeTargetSelect,
      routeTargetError,
      `Target '${tid}' container is stopped and cannot be selected as a live target.`
    );
    return false;
  }
  clearFieldError(routeTargetSelect, routeTargetError);
  return true;
}

function validateManualHostField() {
  const val = routeUpstreamHost.value.trim();
  if (!val) {
    setFieldError(
      routeUpstreamHost,
      routeHostError,
      "Upstream host is required in Manual Mode."
    );
    return false;
  }
  if (!HOST_RE.test(val)) {
    setFieldError(
      routeUpstreamHost,
      routeHostError,
      `Invalid upstream host format: '${val}'.`
    );
    return false;
  }
  clearFieldError(routeUpstreamHost, routeHostError);
  return true;
}

function validateManualPortField() {
  const raw = routeUpstreamPort.value.trim();
  if (!raw || !/^\d+$/.test(raw)) {
    setFieldError(
      routeUpstreamPort,
      routePortError,
      "Upstream port must be an integer between 1 and 65535."
    );
    return false;
  }
  const num = Number(raw);
  if (!Number.isInteger(num) || num < 1 || num > 65535) {
    setFieldError(
      routeUpstreamPort,
      routePortError,
      `Invalid port ${raw}: must be between 1 and 65535.`
    );
    return false;
  }
  clearFieldError(routeUpstreamPort, routePortError);
  return true;
}

async function handleRouteFormSubmit(event) {
  event.preventDefault();
  clearAllFormErrors();

  const domainOk = validateDomainField();
  const mode = getSelectedTargetMode();
  let targetOk = true;
  if (mode === "docker") {
    targetOk = validateTargetSelectionField();
  } else {
    const hostOk = validateManualHostField();
    const portOk = validateManualPortField();
    targetOk = hostOk && portOk;
  }

  if (!domainOk || !targetOk) {
    const firstInvalid = routeForm.querySelector('[aria-invalid="true"]');
    if (firstInvalid) {
      firstInvalid.focus();
    }
    const errCode = !domainOk
      ? "invalid_domain"
      : mode === "docker"
      ? "invalid_target"
      : "invalid_route";
    setFeedback(
      "error",
      "Validation Error",
      "Please fix the highlighted form field(s) before saving.",
      {
        errorCode: errCode,
        hint: "Check the field error message directly under the highlighted input.",
      }
    );
    return;
  }

  const isEdit = routeEditModeInput.value === "edit";
  const editRouteId = routeOriginalIdInput.value;
  const payload = {
    domain: routeDomainInput.value.trim().toLowerCase(),
    enabled: routeEnabledCheckbox.checked,
    description: routeDescriptionInput.value.trim(),
  };
  if (!isEdit && routeIdInput.value.trim()) {
    payload.route_id = routeIdInput.value.trim();
  }

  if (mode === "docker") {
    payload.target_id = routeTargetSelect.value.trim();
    payload.upstream_scheme = "http";
  } else {
    payload.upstream_scheme = routeUpstreamScheme.value;
    payload.upstream_host = routeUpstreamHost.value.trim();
    payload.upstream_port = Number(routeUpstreamPort.value.trim());
  }

  routeSubmitBtn.disabled = true;
  beginOp();
  try {
    let res;
    if (isEdit) {
      res = await apiRequest(
        `/api/v1/routes/${encodeURIComponent(editRouteId)}`,
        {
          method: "PUT",
          body: JSON.stringify(payload),
        }
      );
    } else if (mode === "docker") {
      res = await apiRequest("/api/v1/routes/from-target", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    } else {
      res = await apiRequest("/api/v1/routes", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    }

    await loadAllState({ preserveBanner: true });
    const savedRoute = res.route;
    const applyRes = res.apply_result;
    if (applyRes && applyRes.applied === false) {
      setFeedback(
        "error",
        `Route Saved, But Nginx Apply Failed (${applyRes.status})`,
        applyRes.last_error || "Nginx validation or reload failed.",
        {
          errorCode: applyRes.status || "apply_failed",
          hint: "Existing routes remain active. Fix the route configuration or click Retry Apply Config.",
        }
      );
    } else {
      resetRouteForm();
      setFeedback(
        "success",
        isEdit ? "Route Updated" : "Route Created",
        `Route ${savedRoute.route_id} (${savedRoute.domain} -> ${savedRoute.upstream_url}) saved and applied.`
      );
    }
  } catch (err) {
    await loadAllState({ preserveBanner: true });
    if (err.code === "duplicate_domain") {
      setFieldError(routeDomainInput, routeDomainError, err.message);
      routeDomainInput.focus();
    } else if (
      err.code === "invalid_target" ||
      err.code === "target_not_found" ||
      err.code === "discovery_unavailable"
    ) {
      setFieldError(routeTargetSelect, routeTargetError, err.message);
      routeTargetSelect.focus();
    }
    setFeedback("error", `Save Failed (${err.code})`, err.message, {
      errorCode: err.code,
      hint:
        err.code === "discovery_unavailable"
          ? "Switch to 'Manual Scheme / Host / Port' mode to specify upstream_host and upstream_port manually."
          : "Correct the highlighted field and try again.",
    });
  } finally {
    routeSubmitBtn.disabled = false;
    endOp();
  }
}

async function handleToggleRoute(routeId, enableFlag) {
  const action = enableFlag ? "enable" : "disable";
  beginOp();
  try {
    const res = await apiRequest(
      `/api/v1/routes/${encodeURIComponent(routeId)}/${action}`,
      {
        method: "POST",
        body: JSON.stringify({}),
      }
    );
    await loadAllState({ preserveBanner: true });
    setFeedback(
      "success",
      enableFlag ? "Route Enabled" : "Route Disabled",
      `Route ${routeId} (${res.route.domain}) is now ${
        enableFlag ? "enabled" : "disabled"
      } in Nginx.`
    );
  } catch (err) {
    await loadAllState({ preserveBanner: true });
    setFeedback("error", `Failed to ${action} route`, err.message, {
      errorCode: err.code,
    });
  } finally {
    endOp();
  }
}

function openCopyDialog(route) {
  appState.copySourceRoute = route;
  copySourceRouteIdInput.value = route.route_id;
  copySourceSummary.textContent = `Copying from ${route.route_id} (${route.domain} -> ${route.upstream_url}). The source route will not be modified.`;
  copyNewDomainInput.value = "";
  copyNewRouteIdInput.value = "";
  copyNewDescriptionInput.value = `Copied from ${route.route_id}`;
  clearFieldError(copyNewDomainInput, copyDomainError);
  copyRouteDialog.returnValue = "";
  copyRouteDialog.showModal();
}

async function handleCopyRouteSubmit(event) {
  event.preventDefault();
  const source = appState.copySourceRoute;
  if (!source) {
    copyRouteDialog.close("cancel");
    return;
  }
  const newDomain = copyNewDomainInput.value.trim().toLowerCase();
  if (!newDomain) {
    setFieldError(
      copyNewDomainInput,
      copyDomainError,
      "A new domain is required to copy a route."
    );
    copyNewDomainInput.focus();
    return;
  }
  if (!DOMAIN_RE.test(newDomain)) {
    setFieldError(
      copyNewDomainInput,
      copyDomainError,
      `Invalid domain format: '${newDomain}'.`
    );
    copyNewDomainInput.focus();
    return;
  }
  if (newDomain === source.domain) {
    setFieldError(
      copyNewDomainInput,
      copyDomainError,
      `New domain must be different from source domain '${source.domain}'.`
    );
    copyNewDomainInput.focus();
    return;
  }

  copyConfirmBtn.disabled = true;
  beginOp();
  try {
    const payload = {
      domain: newDomain,
      description: copyNewDescriptionInput.value.trim(),
    };
    if (copyNewRouteIdInput.value.trim()) {
      payload.route_id = copyNewRouteIdInput.value.trim();
    }
    const res = await apiRequest(
      `/api/v1/routes/${encodeURIComponent(source.route_id)}/copy`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
    copyRouteDialog.close("copied");
    await loadAllState({ preserveBanner: true });
    setFeedback(
      "success",
      "Route Copied",
      `Created ${res.route.route_id} (${res.route.domain} -> ${res.route.upstream_url}) from ${source.route_id}.`
    );
  } catch (err) {
    setFieldError(copyNewDomainInput, copyDomainError, err.message);
    copyNewDomainInput.focus();
    setFeedback("error", `Copy Failed (${err.code})`, err.message, {
      errorCode: err.code,
    });
  } finally {
    copyConfirmBtn.disabled = false;
    endOp();
  }
}

function openDeleteDialog(route) {
  appState.deleteTargetRoute = route;
  deleteTargetRouteId.textContent = route.route_id;
  deleteTargetDomain.textContent = route.domain;
  deleteRouteIdInput.value = route.route_id;
  deleteRouteDialog.returnValue = "";
  deleteRouteDialog.showModal();
}

async function handleDeleteRouteSubmit(event) {
  event.preventDefault();
  const target = appState.deleteTargetRoute;
  if (!target) {
    deleteRouteDialog.close("cancel");
    return;
  }
  deleteConfirmBtn.disabled = true;
  beginOp();
  try {
    await apiRequest(
      `/api/v1/routes/${encodeURIComponent(target.route_id)}`,
      {
        method: "DELETE",
      }
    );
    deleteRouteDialog.close("deleted");
    await loadAllState({ preserveBanner: true });
    setFeedback(
      "success",
      "Route Deleted",
      `Deleted route ${target.route_id} (${target.domain}) and reloaded Nginx router.`
    );
  } catch (err) {
    deleteRouteDialog.close("error");
    await loadAllState({ preserveBanner: true });
    setFeedback("error", `Delete Failed (${err.code})`, err.message, {
      errorCode: err.code,
    });
  } finally {
    deleteConfirmBtn.disabled = false;
    endOp();
  }
}

async function handleRetryApply() {
  retryApplyBtn.disabled = true;
  beginOp();
  try {
    const res = await apiRequest("/api/v1/routes/apply", {
      method: "POST",
      body: JSON.stringify({ trigger_action: "console_retry_apply" }),
    });
    await loadAllState({ preserveBanner: true });
    setFeedback(
      "success",
      "Nginx Config Applied",
      `Successfully validated and applied generation ${res.applied_generation}.`
    );
  } catch (err) {
    await loadAllState({ preserveBanner: true });
    setFeedback(
      "error",
      `Apply Failed (${err.payload?.status || err.code})`,
      err.payload?.last_error || err.message,
      {
        errorCode: err.payload?.status || err.code,
        hint: "Existing routes remain active. Review the error details above.",
      }
    );
  } finally {
    retryApplyBtn.disabled = false;
    endOp();
  }
}

// Event Listeners
document.getElementById("refresh-all-btn").addEventListener("click", () => {
  loadAllState({ refreshDiscovery: true });
});

document.getElementById("new-route-btn").addEventListener("click", () => {
  resetRouteForm();
  routeDomainInput.focus();
});

refreshDiscoveryBtn.addEventListener("click", () => {
  loadAllState({ refreshDiscovery: true });
});

includeSelfCheckbox.addEventListener("change", () => {
  loadAllState({ refreshDiscovery: true });
});

switchToManualBtn.addEventListener("click", () => {
  modeManualRadio.checked = true;
  syncTargetModeUI();
  routeUpstreamHost.focus();
});

modeDockerRadio.addEventListener("change", syncTargetModeUI);
modeManualRadio.addEventListener("change", syncTargetModeUI);

// Form validation timing: validate on blur, clear on input (modern-web-guidance: forms)
routeDomainInput.addEventListener("input", () =>
  clearFieldError(routeDomainInput, routeDomainError)
);
routeDomainInput.addEventListener("blur", () => {
  if (routeDomainInput.value.trim()) {
    validateDomainField();
  }
});

routeTargetSelect.addEventListener("change", () => {
  clearFieldError(routeTargetSelect, routeTargetError);
  if (routeTargetSelect.value) {
    validateTargetSelectionField();
  }
});

routeUpstreamHost.addEventListener("input", () =>
  clearFieldError(routeUpstreamHost, routeHostError)
);
routeUpstreamHost.addEventListener("blur", () => {
  if (routeUpstreamHost.value.trim()) {
    validateManualHostField();
  }
});

routeUpstreamPort.addEventListener("input", () =>
  clearFieldError(routeUpstreamPort, routePortError)
);
routeUpstreamPort.addEventListener("blur", () => {
  if (routeUpstreamPort.value.trim()) {
    validateManualPortField();
  }
});

routeForm.addEventListener("submit", handleRouteFormSubmit);
routeCancelEditBtn.addEventListener("click", () => {
  resetRouteForm();
  setFeedback("idle", "Form Reset", "Route form reset to Create Mode.");
});

copyNewDomainInput.addEventListener("input", () =>
  clearFieldError(copyNewDomainInput, copyDomainError)
);
copyCancelBtn.addEventListener("click", () => copyRouteDialog.close("cancel"));
copyRouteForm.addEventListener("submit", handleCopyRouteSubmit);
copyRouteDialog.addEventListener("close", () => {
  copyRouteDialog.returnValue = "";
});

deleteCancelBtn.addEventListener("click", () =>
  deleteRouteDialog.close("cancel")
);
deleteRouteForm.addEventListener("submit", handleDeleteRouteSubmit);
deleteRouteDialog.addEventListener("close", () => {
  deleteRouteDialog.returnValue = "";
});

retryApplyBtn.addEventListener("click", handleRetryApply);

// ================================================================
// DEV-031: Requests List, Capture Detail, Draft Editor, Replay & Export
// ================================================================
const viewAllBtn = document.getElementById("view-all-btn");
const viewRoutesBtn = document.getElementById("view-routes-btn");
const viewRequestsBtn = document.getElementById("view-requests-btn");
const viewRulesBtn = document.getElementById("view-rules-btn");
const routesWorkspace = document.getElementById("routes-workspace");
const requestsWorkspace = document.getElementById("requests-workspace");
const rulesWorkspace = document.getElementById("rules-workspace");

const capturesTotalBadge = document.getElementById("captures-total-badge");
const refreshCapturesBtn = document.getElementById("refresh-captures-btn");
const capturesFilterForm = document.getElementById("captures-filter-form");
const filterMethodSelect = document.getElementById("filter-method-select");
const filterHostInput = document.getElementById("filter-host-input");
const filterPathPrefixInput = document.getElementById("filter-path-prefix-input");
const filterPathInput = document.getElementById("filter-path-input");
const filterResponseStatusInput = document.getElementById("filter-response-status-input");
const filterCaptureStatusSelect = document.getElementById("filter-capture-status-select");
const filterSourceCaptureInput = document.getElementById("filter-source-capture-input");
const filterDraftIdInput = document.getElementById("filter-draft-id-input");
const filterRuleIdInput = document.getElementById("filter-rule-id-input");
const filterAppliedRuleIdInput = document.getElementById("filter-applied-rule-id-input");
const filterFailedRuleIdInput = document.getElementById("filter-failed-rule-id-input");
const filterSkippedRuleIdInput = document.getElementById("filter-skipped-rule-id-input");
const filterRuleOutcomeSelect = document.getElementById("filter-rule-outcome-select");
const filterLimitSelect = document.getElementById("filter-limit-select");
const filterSortSelect = document.getElementById("filter-sort-select");
const capturesFilterResetBtn = document.getElementById("captures-filter-reset-btn");

const capturesEmptyState = document.getElementById("captures-empty-state");
const capturesTbody = document.getElementById("captures-tbody");
const capturesPrevPageBtn = document.getElementById("captures-prev-page-btn");
const capturesNextPageBtn = document.getElementById("captures-next-page-btn");
const capturesPageInfo = document.getElementById("captures-page-info");

const savedDraftsCount = document.getElementById("saved-drafts-count");
const savedDraftsList = document.getElementById("saved-drafts-list");
const lookupDraftIdInput = document.getElementById("lookup-draft-id-input");
const lookupDraftBtn = document.getElementById("lookup-draft-btn");

// Capture Detail DOM
const captureDetailEmpty = document.getElementById("capture-detail-empty");
const captureDetailContent = document.getElementById("capture-detail-content");
const detailCaptureId = document.getElementById("detail-capture-id");
const detailBodySha256 = document.getElementById("detail-body-sha256");
const detailBodySize = document.getElementById("detail-body-size");
const detailMethod = document.getElementById("detail-method");
const detailHttpVersion = document.getElementById("detail-http-version");
const detailResponseStatus = document.getElementById("detail-response-status");
const detailCaptureStatus = document.getElementById("detail-capture-status");
const detailRouteTarget = document.getElementById("detail-route-target");
const detailTimestamps = document.getElementById("detail-timestamps");
const detailWireUrl = document.getElementById("detail-wire-url");
const detailDecodedPath = document.getElementById("detail-decoded-path");
const detailRawPath = document.getElementById("detail-raw-path");
const detailRawQuery = document.getElementById("detail-raw-query");

const detailReplayabilityBox = document.getElementById("detail-replayability-box");
const detailReplayabilityBadge = document.getElementById("detail-replayability-badge");
const detailReplayabilityReason = document.getElementById("detail-replayability-reason");
const detailReplayabilityMessage = document.getElementById("detail-replayability-message");
const detailCreateDraftBtn = document.getElementById("detail-create-draft-btn");

const detailParentLineage = document.getElementById("detail-parent-lineage");
const detailNavSourceCaptureBtn = document.getElementById("detail-nav-source-capture-btn");
const detailNavSourceDraftWrap = document.getElementById("detail-nav-source-draft-wrap");
const detailNavSourceDraftBtn = document.getElementById("detail-nav-source-draft-btn");
const detailChildDraftsList = document.getElementById("detail-child-drafts-list");
const detailChildReplaysList = document.getElementById("detail-child-replays-list");

const detailQueryCount = document.getElementById("detail-query-count");
const detailQueryTbody = document.getElementById("detail-query-tbody");
const detailHeadersCount = document.getElementById("detail-headers-count");
const detailHeadersTbody = document.getElementById("detail-headers-tbody");
const detailCookiesCount = document.getElementById("detail-cookies-count");
const detailCookiesTbody = document.getElementById("detail-cookies-tbody");

const detailContentType = document.getElementById("detail-content-type");
const detailContentKind = document.getElementById("detail-content-kind");
const detailCapturedBytes = document.getElementById("detail-captured-bytes");
const detailBodyParseErrors = document.getElementById("detail-body-parse-errors");
const detailDerivedJsonWrap = document.getElementById("detail-derived-json-wrap");
const detailDerivedJson = document.getElementById("detail-derived-json");
const detailDerivedFormWrap = document.getElementById("detail-derived-form-wrap");
const detailDerivedFormTbody = document.getElementById("detail-derived-form-tbody");
const detailRawBodyPreview = document.getElementById("detail-raw-body-preview");

// Draft Editor DOM
const draftSyncBadge = document.getElementById("draft-sync-badge");
const draftErrorRecoveryBox = document.getElementById("draft-error-recovery-box");
const draftErrorTitle = document.getElementById("draft-error-title");
const draftErrorMessage = document.getElementById("draft-error-message");
const draftErrorHint = document.getElementById("draft-error-hint");
const draftRecoverRecreateBtn = document.getElementById("draft-recover-recreate-btn");
const draftRecoverReloadBtn = document.getElementById("draft-recover-reload-btn");

const draftEditorEmpty = document.getElementById("draft-editor-empty");
const draftEditorContent = document.getElementById("draft-editor-content");
const draftIdBadge = document.getElementById("draft-id-badge");
const draftViewSourceBtn = document.getElementById("draft-view-source-btn");
const draftSaveBtn = document.getElementById("draft-save-btn");
const draftReloadBtn = document.getElementById("draft-reload-btn");
const draftDeleteBtn = document.getElementById("draft-delete-btn");

const draftValidationErrorBox = document.getElementById("draft-validation-error-box");
const draftValidationErrorCode = document.getElementById("draft-validation-error-code");
const draftValidationErrorText = document.getElementById("draft-validation-error-text");

const draftMethodSelect = document.getElementById("draft-method-select");
const draftSchemeSelect = document.getElementById("draft-scheme-select");
const draftAuthorityInput = document.getElementById("draft-authority-input");
const draftDecodedPathInput = document.getElementById("draft-decoded-path-input");

const draftWireRequestLine = document.getElementById("draft-wire-request-line");
const draftWireUrl = document.getElementById("draft-wire-url");
const draftWirePath = document.getElementById("draft-wire-path");
const draftWireQuery = document.getElementById("draft-wire-query");
const draftWireContentLength = document.getElementById("draft-wire-content-length");
const draftWireExcludedHeaders = document.getElementById("draft-wire-excluded-headers");
const draftSideEffectBadge = document.getElementById("draft-side-effect-badge");
const draftSideEffectText = document.getElementById("draft-side-effect-text");

const draftQueryRows = document.getElementById("draft-query-rows");
const addQueryPairBtn = document.getElementById("add-query-pair-btn");
const draftHeaderRows = document.getElementById("draft-header-rows");
const addHeaderPairBtn = document.getElementById("add-header-pair-btn");
const draftCookieRows = document.getElementById("draft-cookie-rows");
const addCookiePairBtn = document.getElementById("add-cookie-pair-btn");

const draftBodyModeSelect = document.getElementById("draft-body-mode-select");
const draftContentTypeInput = document.getElementById("draft-content-type-input");
const draftBodyParseNotice = document.getElementById("draft-body-parse-notice");
const draftBodyJsonPanel = document.getElementById("draft-body-json-panel");
const draftJsonPathInput = document.getElementById("draft-json-path-input");
const draftJsonPathValInput = document.getElementById("draft-json-path-val-input");
const draftJsonPathSetBtn = document.getElementById("draft-json-path-set-btn");
const draftJsonPathRemoveBtn = document.getElementById("draft-json-path-remove-btn");
const draftJsonTextarea = document.getElementById("draft-json-textarea");
const draftJsonFormatBtn = document.getElementById("draft-json-format-btn");
const draftBodyFormPanel = document.getElementById("draft-body-form-panel");
const draftFormRows = document.getElementById("draft-form-rows");
const addFormPairBtn = document.getElementById("add-form-pair-btn");
const draftBodyRawPanel = document.getElementById("draft-body-raw-panel");
const draftRawLabel = document.getElementById("draft-raw-label");
const draftRawTextarea = document.getElementById("draft-raw-textarea");

const draftReplayBtn = document.getElementById("draft-replay-btn");
const replayResultBox = document.getElementById("replay-result-box");
const replayResultStatus = document.getElementById("replay-result-status");
const replayResultCaptureId = document.getElementById("replay-result-capture-id");
const replayViewNewCaptureBtn = document.getElementById("replay-view-new-capture-btn");
const replayResultLineage = document.getElementById("replay-result-lineage");
const replayResultBody = document.getElementById("replay-result-body");

const draftExportPanel = document.getElementById("draft-export-panel");
const exportCopyStatus = document.getElementById("export-copy-status");
const draftExportRefreshBtn = document.getElementById("draft-export-refresh-btn");
const exportCurlText = document.getElementById("export-curl-text");
const copyCurlBtn = document.getElementById("copy-curl-btn");
const exportCurlEdgeText = document.getElementById("export-curl-edge-text");
const copyCurlEdgeBtn = document.getElementById("copy-curl-edge-btn");
const exportFetchText = document.getElementById("export-fetch-text");
const copyFetchBtn = document.getElementById("copy-fetch-btn");

const replayConfirmDialog = document.getElementById("replay-confirm-dialog");
const replayConfirmForm = document.getElementById("replay-confirm-form");
const replayConfirmMethod = document.getElementById("replay-confirm-method");
const replayConfirmUrl = document.getElementById("replay-confirm-url");
const replayConfirmDraftId = document.getElementById("replay-confirm-draft-id");
const replayConfirmWarning = document.getElementById("replay-confirm-warning");
const replayConfirmCancelBtn = document.getElementById("replay-confirm-cancel-btn");
const replayConfirmSendBtn = document.getElementById("replay-confirm-send-btn");

function setWorkspaceView(mode) {
  appState.workspaceView = mode;
  viewAllBtn.setAttribute("aria-pressed", String(mode === "all"));
  viewRoutesBtn.setAttribute("aria-pressed", String(mode === "routes"));
  viewRequestsBtn.setAttribute("aria-pressed", String(mode === "requests"));
  if (viewRulesBtn) {
    viewRulesBtn.setAttribute("aria-pressed", String(mode === "rules"));
  }
  routesWorkspace.hidden = mode !== "all" && mode !== "routes";
  requestsWorkspace.hidden = mode !== "all" && mode !== "requests";
  if (rulesWorkspace) {
    rulesWorkspace.hidden = mode !== "all" && mode !== "rules";
  }
}

viewAllBtn.addEventListener("click", () => setWorkspaceView("all"));
viewRoutesBtn.addEventListener("click", () => setWorkspaceView("routes"));
viewRequestsBtn.addEventListener("click", () => setWorkspaceView("requests"));
if (viewRulesBtn) {
  viewRulesBtn.addEventListener("click", () => setWorkspaceView("rules"));
}

function buildCapturesQueryString() {
  const params = new URLSearchParams();
  const limit = Number(filterLimitSelect.value || 10);
  params.set("limit", String(limit));
  params.set("offset", String(appState.capturesOffset || 0));
  params.set("sort", filterSortSelect.value || "desc");

  if (filterMethodSelect.value.trim()) {
    params.set("method", filterMethodSelect.value.trim());
  }
  if (filterHostInput.value.trim()) {
    params.set("host", filterHostInput.value.trim());
  }
  if (filterPathPrefixInput.value.trim()) {
    params.set("path_prefix", filterPathPrefixInput.value.trim());
  }
  if (filterPathInput.value.trim()) {
    params.set("path", filterPathInput.value.trim());
  }
  if (filterResponseStatusInput.value.trim()) {
    params.set("response_status", filterResponseStatusInput.value.trim());
  }
  if (filterCaptureStatusSelect.value.trim()) {
    params.set("capture_status", filterCaptureStatusSelect.value.trim());
  }
  if (filterSourceCaptureInput.value.trim()) {
    params.set("source_capture_id", filterSourceCaptureInput.value.trim());
  }
  if (filterDraftIdInput.value.trim()) {
    params.set("draft_id", filterDraftIdInput.value.trim());
  }
  if (filterRuleIdInput && filterRuleIdInput.value.trim()) {
    params.set("rule_id", filterRuleIdInput.value.trim());
  }
  if (filterAppliedRuleIdInput && filterAppliedRuleIdInput.value.trim()) {
    params.set("applied_rule_id", filterAppliedRuleIdInput.value.trim());
  }
  if (filterFailedRuleIdInput && filterFailedRuleIdInput.value.trim()) {
    params.set("failed_rule_id", filterFailedRuleIdInput.value.trim());
  }
  if (filterSkippedRuleIdInput && filterSkippedRuleIdInput.value.trim()) {
    params.set("skipped_rule_id", filterSkippedRuleIdInput.value.trim());
  }
  if (filterRuleOutcomeSelect && filterRuleOutcomeSelect.value.trim()) {
    params.set("rule_outcome", filterRuleOutcomeSelect.value.trim());
  }
  return params.toString();
}

function renderCapturesTable() {
  capturesTbody.textContent = "";
  const items = appState.captures || [];
  const total = Number(appState.capturesTotal || 0);
  const limit = Number(filterLimitSelect.value || 10);
  const offset = Number(appState.capturesOffset || 0);

  capturesTotalBadge.textContent = `${total} capture${total === 1 ? "" : "s"}`;

  if (items.length === 0) {
    capturesEmptyState.hidden = false;
  } else {
    capturesEmptyState.hidden = true;
  }

  for (const item of items) {
    const tr = document.createElement("tr");
    tr.dataset.captureId = item.capture_id;
    tr.dataset.method = item.method;
    tr.dataset.captureStatus = item.capture_status;
    const rpl = item.replayability || {
      replayable: Boolean(item.replayable),
      reason: item.capture_status === "complete" ? null : item.capture_status,
      message: "",
    };
    const isReplayable = Boolean(rpl.replayable);
    const rplReason = rpl.reason || (isReplayable ? "complete" : item.capture_status);
    tr.dataset.replayable = String(isReplayable);
    tr.dataset.replayReason = rplReason;
    tr.dataset.isReplay = String(Boolean(item.replay_lineage?.is_replay));
    if (appState.selectedCaptureId === item.capture_id) {
      tr.dataset.selected = "true";
    }

    // 1. Capture ID & Timestamp + Replay Lineage Badge
    const thId = document.createElement("th");
    thId.scope = "row";
    const idCode = document.createElement("code");
    idCode.textContent = item.capture_id;
    thId.appendChild(idCode);
    const tsMeta = document.createElement("span");
    tsMeta.className = "cell-meta";
    tsMeta.textContent = item.captured_at;
    thId.appendChild(tsMeta);

    if (item.replay_lineage?.is_replay) {
      const linBadge = document.createElement("span");
      linBadge.className = "badge badge-warn badge-replay-lineage";
      linBadge.dataset.sourceCaptureId = item.replay_lineage.source_capture_id || "";
      linBadge.dataset.draftId = item.replay_lineage.draft_id || "";
      linBadge.textContent = item.replay_lineage.draft_id
        ? `Replay of ${item.replay_lineage.source_capture_id} (via ${item.replay_lineage.draft_id})`
        : `Replay of ${item.replay_lineage.source_capture_id}`;
      thId.appendChild(linBadge);
    }

    const ruleSummary = item.rule_evaluation || null;
    if (ruleSummary && ruleSummary.outcome && ruleSummary.outcome !== "no_match") {
      const ruleBadge = document.createElement("span");
      const out = ruleSummary.outcome;
      ruleBadge.className = `badge ${
        out === "applied"
          ? "badge-success"
          : out === "failed"
          ? "badge-danger"
          : "badge-warn"
      } badge-rule-evaluation`;
      ruleBadge.style.marginInlineStart = "0.35rem";
      ruleBadge.dataset.ruleOutcome = out;
      const rIds = [
        ...(ruleSummary.applied_rule_ids || []),
        ...(ruleSummary.failed_rule_ids || []),
        ...(ruleSummary.skipped_rule_ids || []),
      ];
      ruleBadge.textContent = `Rules: ${out}${rIds.length ? ` (${rIds.join(", ")})` : ""}`;
      thId.appendChild(ruleBadge);
    }
    tr.appendChild(thId);

    // 2. Method
    const tdMethod = document.createElement("td");
    const mBadge = document.createElement("span");
    mBadge.className = "badge badge-outline";
    mBadge.textContent = item.method;
    tdMethod.appendChild(mBadge);
    tr.appendChild(tdMethod);

    // 3. Host & Target Path
    const tdUrl = document.createElement("td");
    const urlCode = document.createElement("code");
    urlCode.textContent = `${item.scheme}://${item.authority}${item.raw_target}`;
    tdUrl.appendChild(urlCode);
    tr.appendChild(tdUrl);

    // 4. Response & Capture Status
    const tdStatus = document.createElement("td");
    const respBadge = document.createElement("span");
    const sc = item.response_status;
    respBadge.className = `badge ${
      sc && sc >= 200 && sc < 400
        ? "badge-success"
        : sc && sc >= 400
        ? "badge-danger"
        : "badge-neutral"
    }`;
    respBadge.textContent = sc !== null && sc !== undefined ? `HTTP ${sc}` : "No Resp";
    tdStatus.appendChild(respBadge);

    const capStBadge = document.createElement("span");
    capStBadge.className = `badge ${
      item.capture_status === "complete" ? "badge-outline" : "badge-danger"
    }`;
    capStBadge.style.marginInlineStart = "0.35rem";
    capStBadge.textContent = item.capture_status;
    tdStatus.appendChild(capStBadge);
    tr.appendChild(tdStatus);

    // 5. Route / Target
    const tdRoute = document.createElement("td");
    const rtCode = document.createElement("code");
    rtCode.textContent = `${item.route_id || "-"} -> ${item.target || "-"}`;
    tdRoute.appendChild(rtCode);
    tr.appendChild(tdRoute);

    // 6. Replayable
    const tdReplayable = document.createElement("td");
    const rplBadge = document.createElement("span");
    rplBadge.dataset.replayable = String(isReplayable);
    rplBadge.dataset.reason = rplReason;
    rplBadge.className = `badge ${isReplayable ? "badge-success" : "badge-danger"}`;
    rplBadge.textContent = isReplayable ? "Replayable" : `Disabled (${rplReason})`;
    tdReplayable.appendChild(rplBadge);
    tr.appendChild(tdReplayable);

    // 7. Actions
    const tdActions = document.createElement("td");
    const actWrap = document.createElement("div");
    actWrap.className = "row-actions";

    const viewBtn = document.createElement("button");
    viewBtn.type = "button";
    viewBtn.className = "btn btn-sm btn-secondary btn-view-capture";
    viewBtn.dataset.captureId = item.capture_id;
    viewBtn.textContent = "View Detail";
    viewBtn.addEventListener("click", () => loadCaptureDetail(item.capture_id));
    actWrap.appendChild(viewBtn);

    const draftBtn = document.createElement("button");
    draftBtn.type = "button";
    draftBtn.className = "btn btn-sm btn-primary btn-create-draft";
    draftBtn.dataset.captureId = item.capture_id;
    if (isReplayable) {
      draftBtn.textContent = "Create Draft";
      draftBtn.addEventListener("click", () =>
        createDraftFromCapture(item.capture_id)
      );
    } else {
      draftBtn.disabled = true;
      draftBtn.textContent = `Non-Replayable (${rplReason})`;
    }
    actWrap.appendChild(draftBtn);

    tdActions.appendChild(actWrap);
    tr.appendChild(tdActions);
    capturesTbody.appendChild(tr);
  }

  const startIdx = total === 0 ? 0 : offset + 1;
  const endIdx = Math.min(total, offset + items.length);
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.floor(offset / limit) + 1;
  capturesPageInfo.dataset.offset = String(offset);
  capturesPageInfo.dataset.limit = String(limit);
  capturesPageInfo.dataset.total = String(total);
  capturesPageInfo.textContent =
    total === 0
      ? "Showing 0 of 0"
      : `Showing ${startIdx}–${endIdx} of ${total} (Page ${currentPage} of ${totalPages})`;

  capturesPrevPageBtn.disabled = offset <= 0;
  capturesNextPageBtn.disabled = offset + limit >= total;
}

async function loadCapturesList({
  resetOffset = false,
  preserveBanner = false,
} = {}) {
  if (resetOffset) {
    appState.capturesOffset = 0;
  }
  beginOp();
  try {
    const qs = buildCapturesQueryString();
    const data = await apiRequest(`/api/v1/captures?${qs}`);
    appState.captures = data.items || [];
    appState.capturesTotal = Number(data.total || 0);
    renderCapturesTable();
    if (!preserveBanner) {
      setFeedback(
        "idle",
        "Requests Loaded",
        `Loaded ${appState.captures.length} of ${appState.capturesTotal} captured requests.`
      );
    }
  } catch (err) {
    setFeedback("error", `Failed to Load Requests (${err.code})`, err.message, {
      errorCode: err.code,
    });
  } finally {
    endOp();
  }
}

function renderSavedDraftsBar() {
  const items = appState.savedDrafts || [];
  savedDraftsCount.textContent = String(items.length);
  savedDraftsList.textContent = "";
  if (items.length === 0) {
    const span = document.createElement("span");
    span.className = "field-hint";
    span.textContent = "No persistent drafts saved.";
    savedDraftsList.appendChild(span);
    return;
  }
  for (const d of items) {
    const chip = document.createElement("div");
    chip.className = "draft-chip";
    chip.dataset.draftId = d.draft_id;

    const label = document.createElement("code");
    label.textContent = `${d.draft_id} (${d.method} ${d.decoded_path || d.raw_path})`;
    chip.appendChild(label);

    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "btn btn-sm btn-secondary btn-load-saved-draft";
    openBtn.dataset.draftId = d.draft_id;
    openBtn.textContent = "Open";
    openBtn.addEventListener("click", () => loadDraftById(d.draft_id));
    chip.appendChild(openBtn);

    savedDraftsList.appendChild(chip);
  }
}

async function loadSavedDraftsList({ preserveBanner = true } = {}) {
  beginOp();
  try {
    const data = await apiRequest("/api/v1/drafts?limit=50");
    appState.savedDrafts = data.items || [];
    renderSavedDraftsBar();
    if (!preserveBanner) {
      setFeedback(
        "idle",
        "Drafts Loaded",
        `Loaded ${appState.savedDrafts.length} saved drafts.`
      );
    }
  } catch (_err) {
    // Non-fatal on background refresh
  } finally {
    endOp();
  }
}

function renderOrderedReadOnlyPairs(tbody, pairs) {
  tbody.textContent = "";
  const list = Array.isArray(pairs) ? pairs : [];
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.textContent = "(none)";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  list.forEach((pair, idx) => {
    const tr = document.createElement("tr");
    tr.dataset.pairIndex = String(idx);
    const tdIdx = document.createElement("td");
    tdIdx.textContent = String(idx + 1);
    const tdKey = document.createElement("td");
    const keyCode = document.createElement("code");
    keyCode.textContent = String(pair[0] ?? "");
    tdKey.appendChild(keyCode);
    const tdVal = document.createElement("td");
    const valCode = document.createElement("code");
    valCode.textContent = String(pair[1] ?? "");
    tdVal.appendChild(valCode);
    tr.appendChild(tdIdx);
    tr.appendChild(tdKey);
    tr.appendChild(tdVal);
    tbody.appendChild(tr);
  });
}

function renderCaptureDetail(detail) {
  if (!detail) {
    captureDetailEmpty.hidden = false;
    captureDetailContent.hidden = true;
    return;
  }
  captureDetailEmpty.hidden = true;
  captureDetailContent.hidden = false;

  const req = detail.request || {};
  const bodyMeta = req.body || {};
  const views = detail.derived_views || {};
  const rpl = detail.replayability || {
    replayable: Boolean(detail.replayable),
    reason: detail.capture_status === "complete" ? null : detail.capture_status,
    message: "",
  };
  const isReplayable = Boolean(rpl.replayable);
  const rplReason = rpl.reason || (isReplayable ? "complete" : detail.capture_status);

  detailCaptureId.textContent = detail.capture_id;
  detailBodySha256.textContent = bodyMeta.sha256 || "(none)";
  detailBodySize.textContent = String(bodyMeta.size ?? 0);

  detailMethod.textContent = req.method || "GET";
  detailHttpVersion.textContent = req.http_version || "HTTP/1.1";

  const respStatus = detail.response?.status;
  detailResponseStatus.textContent =
    respStatus !== null && respStatus !== undefined
      ? `HTTP ${respStatus}`
      : "No Response";
  detailResponseStatus.className = `badge ${
    respStatus && respStatus >= 200 && respStatus < 400
      ? "badge-success"
      : respStatus && respStatus >= 400
      ? "badge-danger"
      : "badge-neutral"
  }`;

  detailCaptureStatus.textContent = detail.capture_status || "unknown";
  detailCaptureStatus.className = `badge ${
    detail.capture_status === "complete" ? "badge-outline" : "badge-danger"
  }`;

  const routing = detail.routing || {};
  detailRouteTarget.textContent = `${routing.route_id || "-"} -> ${
    routing.target || "-"
  } (${routing.upstream_url || "-"})`;
  detailTimestamps.textContent = `${detail.captured_at || "-"} (${
    detail.duration_ms ?? 0
  } ms)`;

  detailWireUrl.textContent = `${req.scheme || "https"}://${
    req.authority || req.host || ""
  }${req.raw_target || "/"}`;
  detailDecodedPath.textContent = views.decoded_path || req.raw_path || "/";
  detailRawPath.textContent = req.raw_path || "/";
  detailRawQuery.textContent = req.raw_query || "(empty)";

  // Replayability Box
  detailReplayabilityBox.dataset.replayable = String(isReplayable);
  detailReplayabilityBox.dataset.reason = rplReason;
  detailReplayabilityBadge.textContent = isReplayable
    ? "Replayable"
    : "Non-Replayable";
  detailReplayabilityBadge.className = `badge ${
    isReplayable ? "badge-success" : "badge-danger"
  }`;
  detailReplayabilityReason.textContent = rplReason;
  detailReplayabilityMessage.textContent =
    rpl.message ||
    (isReplayable
      ? "Capture record is complete and replayable."
      : `Cannot replay capture (${rplReason}).`);

  detailCreateDraftBtn.disabled = !isReplayable;
  detailCreateDraftBtn.dataset.captureId = detail.capture_id;

  // Lineage Box
  const rel = detail.replay_relations || {};
  if (rel.is_replay && rel.replayed_from) {
    detailParentLineage.hidden = false;
    const srcCid = rel.replayed_from.source_capture_id;
    detailNavSourceCaptureBtn.textContent = srcCid;
    detailNavSourceCaptureBtn.dataset.captureId = srcCid;
    if (rel.replayed_from.draft_id) {
      detailNavSourceDraftWrap.hidden = false;
      detailNavSourceDraftBtn.textContent = rel.replayed_from.draft_id;
      detailNavSourceDraftBtn.dataset.draftId = rel.replayed_from.draft_id;
    } else {
      detailNavSourceDraftWrap.hidden = true;
    }
  } else {
    detailParentLineage.hidden = true;
  }

  detailChildDraftsList.textContent = "";
  const childDrafts = rel.drafts_from_this_capture || [];
  if (childDrafts.length === 0) {
    detailChildDraftsList.textContent = "None";
  } else {
    for (const cd of childDrafts) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-sm btn-secondary btn-open-child-draft";
      btn.dataset.draftId = cd.draft_id;
      btn.textContent = `${cd.draft_id} (${cd.method})`;
      btn.addEventListener("click", () => loadDraftById(cd.draft_id));
      detailChildDraftsList.appendChild(btn);
    }
  }

  detailChildReplaysList.textContent = "";
  const childReplays = rel.replays_from_this_capture || [];
  if (childReplays.length === 0) {
    detailChildReplaysList.textContent = "None";
  } else {
    for (const cr of childReplays) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-sm btn-secondary btn-open-child-replay";
      btn.dataset.resultCaptureId = cr.result_capture_id || "";
      btn.dataset.replayId = cr.replay_id;
      btn.textContent = cr.result_capture_id
        ? `${cr.result_capture_id} (HTTP ${cr.response_status})`
        : `${cr.replay_id} (HTTP ${cr.response_status})`;
      if (cr.result_capture_id) {
        btn.addEventListener("click", () =>
          loadCaptureDetail(cr.result_capture_id)
        );
      }
      detailChildReplaysList.appendChild(btn);
    }
  }

  // DEV-041: Rule Evaluation Metadata & Navigation in Capture Detail
  renderCaptureDetailRuleEvaluation(detail);

  // Ordered Query, Headers, Cookies
  const qParams = views.query_params || [];
  detailQueryCount.textContent = String(qParams.length);
  renderOrderedReadOnlyPairs(detailQueryTbody, qParams);

  const reqHeaders = req.headers || [];
  detailHeadersCount.textContent = String(reqHeaders.length);
  renderOrderedReadOnlyPairs(detailHeadersTbody, reqHeaders);

  const cookies = views.cookies_view || views.cookies || [];
  detailCookiesCount.textContent = String(cookies.length);
  renderOrderedReadOnlyPairs(detailCookiesTbody, cookies);

  // Body & Derived Views
  detailContentType.textContent = bodyMeta.content_type || "(none)";
  detailContentKind.textContent =
    views.content_kind || bodyMeta.content_kind || "empty";
  detailCapturedBytes.textContent = `${bodyMeta.captured_size ?? 0} / ${
    bodyMeta.size ?? 0
  }`;

  const parseErrors = views.parse_errors || [];
  if (parseErrors.length > 0) {
    detailBodyParseErrors.hidden = false;
    detailBodyParseErrors.textContent = `Body Parse Fallback Notice: ${parseErrors
      .map((e) => (typeof e === "string" ? e : `${e.parser}: ${e.error}`))
      .join("; ")}`;
  } else {
    detailBodyParseErrors.hidden = true;
    detailBodyParseErrors.textContent = "";
  }

  const jsonBodyVal =
    views.json_view !== undefined ? views.json_view : views.json_body;
  if (jsonBodyVal !== null && jsonBodyVal !== undefined) {
    detailDerivedJsonWrap.hidden = false;
    detailDerivedJson.textContent = JSON.stringify(jsonBodyVal, null, 2);
  } else {
    detailDerivedJsonWrap.hidden = true;
    detailDerivedJson.textContent = "";
  }

  const formViewVal = Array.isArray(views.form_view)
    ? views.form_view
    : views.urlencoded_form;
  if (Array.isArray(formViewVal)) {
    detailDerivedFormWrap.hidden = false;
    renderOrderedReadOnlyPairs(detailDerivedFormTbody, formViewVal);
  } else {
    detailDerivedFormWrap.hidden = true;
    detailDerivedFormTbody.textContent = "";
  }

  if (views.text_preview !== null && views.text_preview !== undefined) {
    detailRawBodyPreview.textContent = views.text_preview || "(empty body)";
  } else if (views.raw_body_base64) {
    detailRawBodyPreview.textContent = `[base64] ${views.raw_body_base64}`;
  } else if (views.hex_preview) {
    detailRawBodyPreview.textContent = `[hex] ${views.hex_preview}`;
  } else if (!isReplayable) {
    detailRawBodyPreview.textContent = `(Body unavailable — ${rplReason}: ${
      rpl.message || ""
    })`;
  } else {
    detailRawBodyPreview.textContent = "(empty body)";
  }
}

async function loadCaptureDetail(captureId, { preserveBanner = false } = {}) {
  beginOp();
  try {
    const detail = await apiRequest(
      `/api/v1/captures/${encodeURIComponent(captureId)}`
    );
    appState.selectedCaptureId = detail.capture_id;
    appState.selectedCapture = detail;
    appState.lastSourceCaptureIdForRecovery = detail.capture_id;
    renderCapturesTable();
    renderCaptureDetail(detail);
    if (!preserveBanner) {
      const rpl = detail.replayability || {};
      if (!rpl.replayable) {
        setFeedback(
          "error",
          `Non-Replayable Capture (${rpl.reason || detail.capture_status})`,
          rpl.message || `Capture ${detail.capture_id} cannot be replayed.`,
          {
            errorCode: rpl.reason || "non_replayable_capture",
            hint: "Replay and draft creation are disabled for incomplete or missing-blob records.",
          }
        );
      } else {
        setFeedback(
          "idle",
          "Capture Detail Loaded",
          `Inspecting authoritative capture ${detail.capture_id} (${detail.request.method} ${detail.request.raw_target}).`
        );
      }
    }
  } catch (err) {
    setFeedback(
      "error",
      `Failed to Load Capture (${err.code})`,
      err.message,
      { errorCode: err.code }
    );
  } finally {
    endOp();
  }
}

function setDraftDirtyState(isDirty) {
  appState.draftDirty = Boolean(isDirty);
  if (!appState.activeDraft) {
    draftSyncBadge.dataset.dirty = "false";
    draftSyncBadge.className = "badge badge-neutral";
    draftSyncBadge.textContent = "No Active Draft";
    return;
  }
  draftSyncBadge.dataset.dirty = String(appState.draftDirty);
  if (appState.draftDirty) {
    draftSyncBadge.className = "badge badge-warn";
    draftSyncBadge.textContent = "Unsaved Edits (Will Save Before Replay/Export)";
    draftExportPanel.dataset.stale = "true";
  } else {
    draftSyncBadge.className = "badge badge-success";
    draftSyncBadge.textContent = "Saved on Server";
    draftExportPanel.dataset.stale = "false";
  }
}

function hideDraftErrors() {
  draftErrorRecoveryBox.hidden = true;
  draftErrorRecoveryBox.dataset.errorCode = "";
  draftValidationErrorBox.hidden = true;
  draftValidationErrorBox.dataset.errorCode = "";
}

function showDraftValidationError(code, message) {
  draftValidationErrorBox.hidden = false;
  draftValidationErrorBox.dataset.errorCode = code || "validation_error";
  draftValidationErrorCode.textContent = code || "validation_error";
  draftValidationErrorText.textContent = message;
  setFeedback("error", `Draft Validation Error (${code})`, message, {
    errorCode: code,
    hint: "Fix the invalid input in the Draft Editor or click Reload Draft to restore the last saved server state.",
  });
}

function showDraftRecoveryError(code, title, message, hint = "") {
  draftErrorRecoveryBox.hidden = false;
  draftErrorRecoveryBox.dataset.errorCode = code || "draft_error";
  draftErrorTitle.textContent = title;
  draftErrorMessage.textContent = message;
  draftErrorHint.textContent =
    hint ||
    "You can recreate a fresh draft from the source capture or reload saved state.";
  setFeedback("error", `${title} (${code})`, message, {
    errorCode: code,
    hint,
  });
}

function renderEditablePairList(container, pairs, kind) {
  container.textContent = "";
  const list = Array.isArray(pairs) ? pairs.map((p) => [String(p[0] ?? ""), String(p[1] ?? "")]) : [];

  function rebuildRows() {
    container.textContent = "";
    list.forEach((pair, idx) => {
      const row = document.createElement("div");
      row.className = "pair-row";
      row.dataset.pairIndex = String(idx);
      row.dataset.pairKind = kind;

      const idxSpan = document.createElement("span");
      idxSpan.className = "pair-index";
      idxSpan.textContent = `#${idx + 1}`;
      row.appendChild(idxSpan);

      const keyInput = document.createElement("input");
      keyInput.type = "text";
      keyInput.className = `pair-key-input ${kind}-key-input`;
      keyInput.value = pair[0];
      keyInput.placeholder = `${kind} key / name`;
      keyInput.setAttribute("aria-label", `${kind} #${idx + 1} key`);
      keyInput.autocorrect = "off";
      keyInput.autocapitalize = "off";
      keyInput.spellcheck = false;
      keyInput.addEventListener("input", () => {
        list[idx][0] = keyInput.value;
        setDraftDirtyState(true);
      });
      row.appendChild(keyInput);

      const valInput = document.createElement("input");
      valInput.type = "text";
      valInput.className = `pair-val-input ${kind}-val-input`;
      valInput.value = pair[1];
      valInput.placeholder = `${kind} value`;
      valInput.setAttribute("aria-label", `${kind} #${idx + 1} value`);
      valInput.autocorrect = "off";
      valInput.autocapitalize = "off";
      valInput.spellcheck = false;
      valInput.addEventListener("input", () => {
        list[idx][1] = valInput.value;
        setDraftDirtyState(true);
      });
      row.appendChild(valInput);

      const actions = document.createElement("div");
      actions.className = "row-actions";

      const upBtn = document.createElement("button");
      upBtn.type = "button";
      upBtn.className = `btn btn-sm btn-secondary btn-move-up`;
      upBtn.disabled = idx === 0;
      upBtn.textContent = "↑";
      upBtn.setAttribute("aria-label", `Move ${kind} #${idx + 1} up`);
      upBtn.addEventListener("click", () => {
        if (idx > 0) {
          const tmp = list[idx - 1];
          list[idx - 1] = list[idx];
          list[idx] = tmp;
          rebuildRows();
          setDraftDirtyState(true);
        }
      });
      actions.appendChild(upBtn);

      const downBtn = document.createElement("button");
      downBtn.type = "button";
      downBtn.className = `btn btn-sm btn-secondary btn-move-down`;
      downBtn.disabled = idx === list.length - 1;
      downBtn.textContent = "↓";
      downBtn.setAttribute("aria-label", `Move ${kind} #${idx + 1} down`);
      downBtn.addEventListener("click", () => {
        if (idx < list.length - 1) {
          const tmp = list[idx + 1];
          list[idx + 1] = list[idx];
          list[idx] = tmp;
          rebuildRows();
          setDraftDirtyState(true);
        }
      });
      actions.appendChild(downBtn);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = `btn btn-sm btn-danger btn-remove-pair`;
      delBtn.textContent = "Remove";
      delBtn.setAttribute("aria-label", `Remove ${kind} #${idx + 1}`);
      delBtn.addEventListener("click", () => {
        list.splice(idx, 1);
        rebuildRows();
        setDraftDirtyState(true);
      });
      actions.appendChild(delBtn);

      row.appendChild(actions);
      container.appendChild(row);
    });
  }

  rebuildRows();
  container._pairList = list;
  container._addPair = (k = "", v = "") => {
    list.push([String(k), String(v)]);
    rebuildRows();
    setDraftDirtyState(true);
  };
}

function readPairsFromContainer(container) {
  const rows = Array.from(container.querySelectorAll(".pair-row"));
  return rows.map((row) => [
    row.querySelector(".pair-key-input")?.value ?? "",
    row.querySelector(".pair-val-input")?.value ?? "",
  ]);
}

function syncDraftBodyModeUI() {
  const mode = draftBodyModeSelect.value;
  draftBodyJsonPanel.hidden = mode !== "json";
  draftBodyFormPanel.hidden = mode !== "urlencoded_form";
  draftBodyRawPanel.hidden = mode !== "raw_text" && mode !== "raw_base64";
  if (mode === "raw_base64") {
    draftRawLabel.textContent = "Raw Base64 Payload";
  } else {
    draftRawLabel.textContent = "Raw Text Payload";
  }
}

function renderDraftEditor(draft) {
  hideDraftErrors();
  if (!draft) {
    draftEditorEmpty.hidden = false;
    draftEditorContent.hidden = true;
    setDraftDirtyState(false);
    return;
  }

  draftEditorEmpty.hidden = true;
  draftEditorContent.hidden = false;
  appState.activeDraftId = draft.draft_id;
  appState.activeDraft = draft;
  appState.lastSourceCaptureIdForRecovery = draft.source_capture_id;

  const req = draft.request || {};
  const body = req.body || {};
  const norm = draft.normalized_preview || {};
  const warn = draft.side_effect_warning || {};

  draftIdBadge.textContent = draft.draft_id;
  draftViewSourceBtn.textContent = draft.source_capture_id;
  draftViewSourceBtn.dataset.captureId = draft.source_capture_id;

  draftMethodSelect.value = req.method || "GET";
  draftSchemeSelect.value = req.scheme || "https";
  draftAuthorityInput.value = req.authority || req.host || "";
  draftDecodedPathInput.value = req.decoded_path || req.raw_path || "/";

  // Wire Preview Box
  draftWireRequestLine.textContent = `${norm.method || req.method} ${
    norm.target_uri || req.raw_path || "/"
  } HTTP/1.1`;
  draftWireUrl.textContent = norm.url || "-";
  draftWirePath.textContent = norm.raw_path || req.raw_path || "/";
  draftWireQuery.textContent = norm.raw_query || "(empty)";
  draftWireContentLength.textContent = String(norm.content_length ?? 0);

  const excl = norm.excluded_hop_by_hop_headers || [];
  draftWireExcludedHeaders.textContent =
    excl.length > 0 ? excl.map(([k, v]) => `${k}: ${v}`).join("; ") : "None";

  if (warn.has_side_effect_warning) {
    draftSideEffectBadge.className = "badge badge-warn";
    draftSideEffectBadge.textContent = `${warn.method} (Requires Confirmation)`;
    draftSideEffectText.textContent = warn.message || "";
  } else {
    draftSideEffectBadge.className = "badge badge-success";
    draftSideEffectBadge.textContent = `${warn.method || req.method} (Safe)`;
    draftSideEffectText.textContent = "Idempotent / safe method.";
  }

  // Ordered Query, Headers, Cookies
  renderEditablePairList(draftQueryRows, req.query_params || [], "query");
  renderEditablePairList(draftHeaderRows, req.headers || [], "header");
  renderEditablePairList(draftCookieRows, req.cookies || [], "cookie");

  // Body Editor
  const bMode = body.body_mode || "empty";
  draftBodyModeSelect.value = bMode;
  draftContentTypeInput.value = body.content_type || "";

  const pErrors = body.parse_errors || [];
  if (pErrors.length > 0) {
    draftBodyParseNotice.hidden = false;
    draftBodyParseNotice.textContent = `Fallback Notice: Source body had parse error(s) (${pErrors
      .map((e) => `${e.parser}: ${e.error}`)
      .join("; ")}) and opened in ${bMode} fallback mode.`;
  } else {
    draftBodyParseNotice.hidden = true;
    draftBodyParseNotice.textContent = "";
  }

  draftJsonTextarea.value =
    body.json_text ||
    (body.json_value !== null && body.json_value !== undefined
      ? JSON.stringify(body.json_value, null, 2)
      : "");
  renderEditablePairList(draftFormRows, body.form_fields || [], "form");
  draftRawTextarea.value =
    bMode === "raw_base64" ? body.raw_base64 || "" : body.raw_text || "";

  syncDraftBodyModeUI();
  setDraftDirtyState(false);
}

async function createDraftFromCapture(captureId) {
  beginOp();
  hideDraftErrors();
  try {
    const draft = await apiRequest(
      `/api/v1/captures/${encodeURIComponent(captureId)}/drafts`,
      {
        method: "POST",
        body: JSON.stringify({}),
      }
    );
    renderDraftEditor(draft);
    await Promise.all([
      loadSavedDraftsList({ preserveBanner: true }),
      refreshActiveDraftExport({ saveFirst: false, preserveBanner: true }),
      appState.selectedCaptureId === captureId
        ? loadCaptureDetail(captureId, { preserveBanner: true })
        : Promise.resolve(),
    ]);
    setFeedback(
      "success",
      "Draft Created",
      `Created editable draft ${draft.draft_id} from ${captureId}. Original capture remains unchanged.`
    );
  } catch (err) {
    showDraftRecoveryError(
      err.code,
      "Cannot Create Draft",
      err.message,
      err.payload?.reason
        ? `Capture is non-replayable (reason: ${err.payload.reason}).`
        : ""
    );
  } finally {
    endOp();
  }
}

async function loadDraftById(draftId, { preserveBanner = false } = {}) {
  const cleanId = String(draftId || "").trim();
  if (!cleanId) {
    return;
  }
  beginOp();
  hideDraftErrors();
  try {
    const draft = await apiRequest(
      `/api/v1/drafts/${encodeURIComponent(cleanId)}`
    );
    renderDraftEditor(draft);
    await refreshActiveDraftExport({ saveFirst: false, preserveBanner: true });
    if (!preserveBanner) {
      setFeedback(
        "idle",
        "Draft Loaded",
        `Loaded persistent draft ${draft.draft_id} (source: ${draft.source_capture_id}).`
      );
    }
  } catch (err) {
    showDraftRecoveryError(
      err.code,
      err.status === 404 ? "Draft Expired or Deleted" : "Failed to Load Draft",
      err.message,
      "Click 'Recreate Draft from Source Capture' to create a new draft from the original capture."
    );
  } finally {
    endOp();
  }
}

function collectDraftPayloadFromUI() {
  const mode = draftBodyModeSelect.value;
  const payload = {
    method: draftMethodSelect.value,
    scheme: draftSchemeSelect.value,
    authority: draftAuthorityInput.value.trim(),
    decoded_path: draftDecodedPathInput.value.trim() || "/",
    query_params: readPairsFromContainer(draftQueryRows),
    headers: readPairsFromContainer(draftHeaderRows),
    cookies: readPairsFromContainer(draftCookieRows),
    body_mode: mode,
    content_type: draftContentTypeInput.value.trim(),
  };

  if (mode === "json") {
    const rawJson = draftJsonTextarea.value.trim();
    if (!rawJson) {
      const err = new Error("JSON payload cannot be empty when Body Mode is 'json'.");
      err.code = "invalid_json_body";
      throw err;
    }
    try {
      payload.json_value = JSON.parse(rawJson);
    } catch (parseErr) {
      const err = new Error(`Invalid JSON syntax: ${parseErr.message}`);
      err.code = "invalid_json_body";
      throw err;
    }
  } else if (mode === "urlencoded_form") {
    payload.form_fields = readPairsFromContainer(draftFormRows);
  } else if (mode === "raw_text") {
    payload.raw_text = draftRawTextarea.value;
  } else if (mode === "raw_base64") {
    payload.raw_base64 = draftRawTextarea.value.trim();
  }
  return payload;
}

async function saveActiveDraft({ preserveBanner = false } = {}) {
  if (!appState.activeDraftId) {
    return null;
  }
  hideDraftErrors();
  let payload;
  try {
    payload = collectDraftPayloadFromUI();
  } catch (valErr) {
    showDraftValidationError(valErr.code || "invalid_json_body", valErr.message);
    throw valErr;
  }

  beginOp();
  try {
    const updated = await apiRequest(
      `/api/v1/drafts/${encodeURIComponent(appState.activeDraftId)}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      }
    );
    renderDraftEditor(updated);
    await Promise.all([
      loadSavedDraftsList({ preserveBanner: true }),
      refreshActiveDraftExport({ saveFirst: false, preserveBanner: true }),
      appState.selectedCaptureId === updated.source_capture_id
        ? loadCaptureDetail(updated.source_capture_id, { preserveBanner: true })
        : Promise.resolve(),
    ]);
    if (!preserveBanner) {
      setFeedback(
        "success",
        "Draft Saved",
        `Saved draft ${updated.draft_id} and updated normalized wire preview and exports.`
      );
    }
    return updated;
  } catch (err) {
    if (err.status === 404 || err.code === "draft_not_found") {
      showDraftRecoveryError(
        err.code || "draft_not_found",
        "Draft Expired or Deleted",
        err.message,
        "Click 'Recreate Draft from Source Capture' to start a new draft."
      );
    } else {
      showDraftValidationError(err.code || "validation_error", err.message);
    }
    throw err;
  } finally {
    endOp();
  }
}

async function applyNestedJsonMutationFromUI(op) {
  if (!appState.activeDraftId) {
    return;
  }
  hideDraftErrors();
  const pathStr = draftJsonPathInput.value.trim();
  if (!pathStr) {
    showDraftValidationError("invalid_json_path", "Nested JSON path is required.");
    return;
  }

  let parsedVal = null;
  if (op === "set") {
    const rawVal = draftJsonPathValInput.value.trim();
    try {
      parsedVal = JSON.parse(rawVal);
    } catch (_e) {
      parsedVal = rawVal;
    }
  }

  // If the draft is currently in json mode and textarea has valid JSON, first sync any pending header/query/json edits
  // and apply json_mutations on the server so server-side structured error handling is exercised too!
  beginOp();
  try {
    const basePayload =
      draftBodyModeSelect.value === "json"
        ? collectDraftPayloadFromUI()
        : {
            method: draftMethodSelect.value,
            scheme: draftSchemeSelect.value,
            authority: draftAuthorityInput.value.trim(),
            decoded_path: draftDecodedPathInput.value.trim() || "/",
            query_params: readPairsFromContainer(draftQueryRows),
            headers: readPairsFromContainer(draftHeaderRows),
            cookies: readPairsFromContainer(draftCookieRows),
          };
    basePayload.json_mutations = [{ op, path: pathStr, value: parsedVal }];

    const updated = await apiRequest(
      `/api/v1/drafts/${encodeURIComponent(appState.activeDraftId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(basePayload),
      }
    );
    renderDraftEditor(updated);
    await refreshActiveDraftExport({ saveFirst: false, preserveBanner: true });
    setFeedback(
      "success",
      "JSON Field Updated",
      `Applied ${op} on JSON path '${pathStr}' in draft ${updated.draft_id}.`
    );
  } catch (err) {
    showDraftValidationError(err.code || "structured_body_unavailable", err.message);
  } finally {
    endOp();
  }
}

async function deleteActiveDraft() {
  if (!appState.activeDraftId) {
    return;
  }
  const targetId = appState.activeDraftId;
  beginOp();
  hideDraftErrors();
  try {
    await apiRequest(`/api/v1/drafts/${encodeURIComponent(targetId)}`, {
      method: "DELETE",
    });
    appState.activeDraftId = null;
    appState.activeDraft = null;
    renderDraftEditor(null);
    await loadSavedDraftsList({ preserveBanner: true });
    if (appState.selectedCaptureId) {
      await loadCaptureDetail(appState.selectedCaptureId, { preserveBanner: true });
    }
    setFeedback("success", "Draft Deleted", `Deleted draft ${targetId}.`);
  } catch (err) {
    showDraftRecoveryError(
      err.code,
      "Delete Draft Failed",
      err.message,
      "The draft may have already expired or been deleted."
    );
  } finally {
    endOp();
  }
}

async function refreshActiveDraftExport({
  saveFirst = true,
  preserveBanner = false,
} = {}) {
  if (!appState.activeDraftId) {
    return null;
  }
  if (saveFirst && appState.draftDirty) {
    await saveActiveDraft({ preserveBanner: true });
  }
  beginOp();
  try {
    const exp = await apiRequest(
      `/api/v1/drafts/${encodeURIComponent(appState.activeDraftId)}/export`
    );
    exportCurlText.value = exp.curl || "";
    exportCurlEdgeText.value = exp.curl_local_edge || "";
    exportFetchText.value = exp.fetch || "";
    draftExportPanel.dataset.stale = "false";
    exportCopyStatus.className = "badge badge-success";
    exportCopyStatus.textContent = "Synced with Saved Draft";
    if (!preserveBanner) {
      setFeedback(
        "success",
        "Export Refreshed",
        `Generated cURL and browser fetch() snippets from saved draft ${appState.activeDraftId}.`
      );
    }
    return exp;
  } catch (err) {
    exportCopyStatus.className = "badge badge-danger";
    exportCopyStatus.textContent = `Export Error (${err.code})`;
    showDraftRecoveryError(
      err.code || "export_error",
      "Export Failed",
      err.message,
      "Verify that the draft still exists and contains valid request state."
    );
    throw err;
  } finally {
    endOp();
  }
}

async function copyOrSelectTextarea(textareaEl, label) {
  const text = textareaEl.value || "";
  textareaEl.focus();
  textareaEl.select();
  exportCopyStatus.className = "badge badge-outline";
  exportCopyStatus.textContent = `Selected ${label}`;
  setFeedback(
    "idle",
    `Selected ${label}`,
    `${label} text is selected in the box (press Cmd/Ctrl+C to copy).`
  );
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await Promise.race([
        navigator.clipboard.writeText(text),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error("clipboard_timeout")), 150)
        ),
      ]);
      exportCopyStatus.className = "badge badge-success";
      exportCopyStatus.textContent = `Copied ${label}`;
      setFeedback("success", `Copied ${label}`, `${label} copied to clipboard.`);
    }
  } catch (_err) {
    // Fallback to text selection when Clipboard API is unavailable/denied/timed out
  }
}

async function handleReplayButtonClick() {
  if (!appState.activeDraftId) {
    return;
  }
  hideDraftErrors();
  // Rule 8: Always save pending UI edits to server before replay so unsaved old/new values never mix
  try {
    if (appState.draftDirty) {
      await saveActiveDraft({ preserveBanner: true });
    }
  } catch (_saveErr) {
    return;
  }

  const draft = appState.activeDraft;
  if (!draft) {
    return;
  }
  const warn = draft.side_effect_warning || {};
  const method = (draft.request?.method || "GET").toUpperCase();
  const isUnsafe =
    Boolean(warn.has_side_effect_warning) ||
    !["GET", "HEAD", "OPTIONS", "TRACE"].includes(method);

  if (isUnsafe) {
    // Rule 7, AC-7: Show confirmation modal with method, URL, and side-effect warning before sending
    replayConfirmMethod.textContent = method;
    replayConfirmUrl.textContent =
      draft.normalized_preview?.url ||
      `${draft.request.scheme}://${draft.request.authority}${draft.request.raw_path}`;
    replayConfirmDraftId.textContent = draft.draft_id;
    replayConfirmWarning.textContent =
      warn.message ||
      `HTTP ${method} is a non-safe method and replaying it may mutate state or trigger side effects on the target service.`;
    replayConfirmDialog.returnValue = "";
    replayConfirmDialog.showModal();
    return;
  }

  await executeConfirmedReplay();
}

async function executeConfirmedReplay() {
  if (!appState.activeDraftId) {
    return;
  }
  const draftId = appState.activeDraftId;
  draftReplayBtn.disabled = true;
  replayConfirmSendBtn.disabled = true;
  beginOp();
  hideDraftErrors();
  try {
    const res = await apiRequest(
      `/api/v1/drafts/${encodeURIComponent(draftId)}/replay`,
      {
        method: "POST",
        body: JSON.stringify({}),
      }
    );
    appState.lastReplayResult = res;

    replayResultBox.hidden = false;
    replayResultBox.dataset.status = String(res.response_status ?? "");
    replayResultStatus.textContent = `HTTP ${res.response_status}`;
    replayResultStatus.className = `badge ${
      res.response_status >= 200 && res.response_status < 400
        ? "badge-success"
        : "badge-danger"
    }`;
    replayResultCaptureId.textContent = res.result_capture_id || "(pending)";
    replayViewNewCaptureBtn.dataset.captureId = res.result_capture_id || "";
    replayViewNewCaptureBtn.disabled = !res.result_capture_id;
    replayResultLineage.textContent = `Source Capture: ${res.source_capture_id} → Draft: ${res.draft_id} → Replay Run: ${res.replay_id} → New Capture: ${res.result_capture_id || "none"}`;
    replayResultBody.textContent =
      res.response_json !== null && res.response_json !== undefined
        ? JSON.stringify(res.response_json, null, 2)
        : res.response_body_preview || "(empty response body)";

    await Promise.all([
      loadCapturesList({ preserveBanner: true }),
      loadSavedDraftsList({ preserveBanner: true }),
      appState.selectedCaptureId
        ? loadCaptureDetail(appState.selectedCaptureId, { preserveBanner: true })
        : Promise.resolve(),
    ]);

    if (res.response_status >= 500) {
      setFeedback(
        "error",
        `Replay Returned HTTP ${res.response_status}`,
        `Target or proxy returned HTTP ${res.response_status} for replay ${res.replay_id}.`,
        {
          errorCode: `http_${res.response_status}`,
          hint: "Check whether the target container is running or inspect the response body below.",
        }
      );
    } else {
      setFeedback(
        "success",
        `Replay Succeeded (HTTP ${res.response_status})`,
        `Replayed draft ${draftId} -> new capture ${res.result_capture_id || res.replay_id}.`
      );
    }
  } catch (err) {
    replayResultBox.hidden = true;
    showDraftRecoveryError(
      err.code || "replay_failed",
      "Replay Failed",
      err.message,
      "Check target availability or recreate/reload the draft."
    );
  } finally {
    draftReplayBtn.disabled = false;
    replayConfirmSendBtn.disabled = false;
    endOp();
  }
}

// DEV-031 Event Listeners
refreshCapturesBtn.addEventListener("click", () => {
  loadCapturesList({ preserveBanner: false });
  loadSavedDraftsList({ preserveBanner: true });
});

capturesFilterForm.addEventListener("submit", (e) => {
  e.preventDefault();
  loadCapturesList({ resetOffset: true });
});

capturesFilterResetBtn.addEventListener("click", () => {
  filterMethodSelect.value = "";
  filterHostInput.value = "";
  filterPathPrefixInput.value = "";
  filterPathInput.value = "";
  filterResponseStatusInput.value = "";
  filterCaptureStatusSelect.value = "";
  filterSourceCaptureInput.value = "";
  filterDraftIdInput.value = "";
  if (filterRuleIdInput) filterRuleIdInput.value = "";
  if (filterAppliedRuleIdInput) filterAppliedRuleIdInput.value = "";
  if (filterFailedRuleIdInput) filterFailedRuleIdInput.value = "";
  if (filterSkippedRuleIdInput) filterSkippedRuleIdInput.value = "";
  if (filterRuleOutcomeSelect) filterRuleOutcomeSelect.value = "";
  filterLimitSelect.value = "10";
  filterSortSelect.value = "desc";
  loadCapturesList({ resetOffset: true });
});

capturesPrevPageBtn.addEventListener("click", () => {
  const limit = Number(filterLimitSelect.value || 10);
  appState.capturesOffset = Math.max(0, appState.capturesOffset - limit);
  loadCapturesList();
});

capturesNextPageBtn.addEventListener("click", () => {
  const limit = Number(filterLimitSelect.value || 10);
  if (appState.capturesOffset + limit < appState.capturesTotal) {
    appState.capturesOffset += limit;
    loadCapturesList();
  }
});

lookupDraftBtn.addEventListener("click", () => {
  const id = lookupDraftIdInput.value.trim();
  if (id) {
    loadDraftById(id);
  }
});

detailCreateDraftBtn.addEventListener("click", () => {
  const cid = detailCreateDraftBtn.dataset.captureId;
  if (cid && !detailCreateDraftBtn.disabled) {
    createDraftFromCapture(cid);
  }
});

detailNavSourceCaptureBtn.addEventListener("click", () => {
  const cid = detailNavSourceCaptureBtn.dataset.captureId;
  if (cid) {
    loadCaptureDetail(cid);
  }
});

detailNavSourceDraftBtn.addEventListener("click", () => {
  const did = detailNavSourceDraftBtn.dataset.draftId;
  if (did) {
    loadDraftById(did);
  }
});

draftViewSourceBtn.addEventListener("click", () => {
  const cid = draftViewSourceBtn.dataset.captureId;
  if (cid) {
    loadCaptureDetail(cid);
  }
});

draftSaveBtn.addEventListener("click", () => {
  saveActiveDraft().catch(() => {});
});

draftReloadBtn.addEventListener("click", () => {
  if (appState.activeDraftId) {
    loadDraftById(appState.activeDraftId);
  }
});

draftDeleteBtn.addEventListener("click", () => {
  deleteActiveDraft();
});

draftRecoverRecreateBtn.addEventListener("click", () => {
  const srcCid =
    appState.lastSourceCaptureIdForRecovery || appState.selectedCaptureId;
  if (srcCid) {
    createDraftFromCapture(srcCid);
  } else {
    hideDraftErrors();
    setFeedback(
      "idle",
      "Select a Capture",
      "Select a replayable capture from the table above to create a new draft."
    );
  }
});

draftRecoverReloadBtn.addEventListener("click", () => {
  if (appState.activeDraftId) {
    loadDraftById(appState.activeDraftId);
  } else {
    hideDraftErrors();
  }
});

for (const el of [
  draftMethodSelect,
  draftSchemeSelect,
  draftAuthorityInput,
  draftDecodedPathInput,
  draftContentTypeInput,
  draftJsonTextarea,
  draftRawTextarea,
]) {
  el.addEventListener("input", () => setDraftDirtyState(true));
  el.addEventListener("change", () => setDraftDirtyState(true));
}

draftBodyModeSelect.addEventListener("change", () => {
  syncDraftBodyModeUI();
  setDraftDirtyState(true);
});

addQueryPairBtn.addEventListener("click", () => {
  if (draftQueryRows._addPair) {
    draftQueryRows._addPair("", "");
  }
});

addHeaderPairBtn.addEventListener("click", () => {
  if (draftHeaderRows._addPair) {
    draftHeaderRows._addPair("", "");
  }
});

addCookiePairBtn.addEventListener("click", () => {
  if (draftCookieRows._addPair) {
    draftCookieRows._addPair("", "");
  }
});

addFormPairBtn.addEventListener("click", () => {
  if (draftFormRows._addPair) {
    draftFormRows._addPair("", "");
  }
});

draftJsonFormatBtn.addEventListener("click", () => {
  hideDraftErrors();
  try {
    const parsed = JSON.parse(draftJsonTextarea.value);
    draftJsonTextarea.value = JSON.stringify(parsed, null, 2);
    setFeedback("idle", "Valid JSON", "JSON payload formatted and validated.");
  } catch (err) {
    showDraftValidationError("invalid_json_body", `Invalid JSON syntax: ${err.message}`);
  }
});

draftJsonPathSetBtn.addEventListener("click", () => {
  applyNestedJsonMutationFromUI("set");
});

draftJsonPathRemoveBtn.addEventListener("click", () => {
  applyNestedJsonMutationFromUI("remove");
});

draftReplayBtn.addEventListener("click", () => {
  handleReplayButtonClick();
});

replayConfirmCancelBtn.addEventListener("click", () => {
  replayConfirmDialog.close("cancel");
  setFeedback(
    "idle",
    "Replay Cancelled",
    "Non-safe request replay was cancelled before sending."
  );
});

replayConfirmForm.addEventListener("submit", (e) => {
  e.preventDefault();
  replayConfirmDialog.close("confirm");
  executeConfirmedReplay();
});

replayConfirmDialog.addEventListener("close", () => {
  replayConfirmDialog.returnValue = "";
});

replayViewNewCaptureBtn.addEventListener("click", () => {
  const newCid = replayViewNewCaptureBtn.dataset.captureId;
  if (newCid) {
    loadCaptureDetail(newCid);
  }
});

draftExportRefreshBtn.addEventListener("click", () => {
  refreshActiveDraftExport({ saveFirst: true }).catch(() => {});
});

copyCurlBtn.addEventListener("click", () => {
  copyOrSelectTextarea(exportCurlText, "cURL Command");
});

copyCurlEdgeBtn.addEventListener("click", () => {
  copyOrSelectTextarea(exportCurlEdgeText, "Local Edge cURL");
});

copyFetchBtn.addEventListener("click", () => {
  copyOrSelectTextarea(exportFetchText, "Browser fetch()");
});

// ================================================================
// DEV-041: Modification Rules Management & Dry-Run Preview Inspector
// ================================================================
const detailRuleEvaluationBox = document.getElementById("detail-rule-evaluation-box");
const detailRuleOutcomeBadge = document.getElementById("detail-rule-outcome-badge");
const detailPreviewRulesBtn = document.getElementById("detail-preview-rules-btn");
const detailRuleOutcomeText = document.getElementById("detail-rule-outcome-text");
const detailRuleModifiedText = document.getElementById("detail-rule-modified-text");
const detailRuleChangeCount = document.getElementById("detail-rule-change-count");
const detailMatchedRulesList = document.getElementById("detail-matched-rules-list");
const detailAppliedRulesList = document.getElementById("detail-applied-rules-list");
const detailSkippedRulesList = document.getElementById("detail-skipped-rules-list");
const detailFailedRulesList = document.getElementById("detail-failed-rules-list");
const detailRuleResultsWrap = document.getElementById("detail-rule-results-wrap");
const detailRuleResultsTbody = document.getElementById("detail-rule-results-tbody");
const detailRuleChangesWrap = document.getElementById("detail-rule-changes-wrap");
const detailRuleChangesTbody = document.getElementById("detail-rule-changes-tbody");

const rulesAvailabilityBadge = document.getElementById("rules-availability-badge");
const rulesCountBadge = document.getElementById("rules-count-badge");
const rulesFilterEnabledSelect = document.getElementById("rules-filter-enabled-select");
const refreshRulesBtn = document.getElementById("refresh-rules-btn");
const newRuleBtn = document.getElementById("new-rule-btn");
const rulesErrorBox = document.getElementById("rules-error-box");
const rulesErrorTitle = document.getElementById("rules-error-title");
const rulesErrorMessage = document.getElementById("rules-error-message");
const retryRulesLoadBtn = document.getElementById("retry-rules-load-btn");
const rulesEmptyState = document.getElementById("rules-empty-state");
const rulesTbody = document.getElementById("rules-tbody");

const ruleForm = document.getElementById("rule-form");
const ruleFormTitle = document.getElementById("rule-form-title");
const ruleFormModeBadge = document.getElementById("rule-form-mode-badge");
const ruleFormDirtyBadge = document.getElementById("rule-form-dirty-badge");
const ruleFormErrorBox = document.getElementById("rule-form-error-box");
const ruleFormErrorCode = document.getElementById("rule-form-error-code");
const ruleFormErrorText = document.getElementById("rule-form-error-text");

const ruleEditModeInput = document.getElementById("rule-edit-mode");
const ruleOriginalIdInput = document.getElementById("rule-original-id");
const ruleNameInput = document.getElementById("rule-name-input");
const ruleNameError = document.getElementById("rule-name-error");
const ruleIdInput = document.getElementById("rule-id-input");
const ruleIdError = document.getElementById("rule-id-error");
const rulePriorityInput = document.getElementById("rule-priority-input");
const rulePriorityError = document.getElementById("rule-priority-error");
const ruleDescriptionInput = document.getElementById("rule-description-input");
const ruleEnabledCheckbox = document.getElementById("rule-enabled-checkbox");

const ruleMatchRouteIdInput = document.getElementById("rule-match-route-id-input");
const ruleMatchDomainInput = document.getElementById("rule-match-domain-input");
const ruleMatchMethodInput = document.getElementById("rule-match-method-input");
const ruleMatchPathExactInput = document.getElementById("rule-match-path-exact-input");
const ruleMatchPathPrefixInput = document.getElementById("rule-match-path-prefix-input");
const ruleMatchPathRegexInput = document.getElementById("rule-match-path-regex-input");
const rulePathRegexError = document.getElementById("rule-path-regex-error");
const ruleMatchContentTypeInput = document.getElementById("rule-match-content-type-input");
const addRuleQueryMatchBtn = document.getElementById("add-rule-query-match-btn");
const ruleQueryMatchRows = document.getElementById("rule-query-match-rows");
const addRuleHeaderMatchBtn = document.getElementById("add-rule-header-match-btn");
const ruleHeaderMatchRows = document.getElementById("rule-header-match-rows");
const ruleMatchError = document.getElementById("rule-match-error");

const ruleActionRows = document.getElementById("rule-action-rows");
const addRuleActionBtn = document.getElementById("add-rule-action-btn");
const ruleActionsError = document.getElementById("rule-actions-error");
const rulePayloadPreview = document.getElementById("rule-payload-preview");

const ruleSubmitBtn = document.getElementById("rule-submit-btn");
const rulePreviewCandidateBtn = document.getElementById("rule-preview-candidate-btn");
const ruleCancelEditBtn = document.getElementById("rule-cancel-edit-btn");

const previewSourceInlineRadio = document.getElementById("preview-source-inline");
const previewSourceCaptureRadio = document.getElementById("preview-source-capture");
const previewIncludeCandidateCheckbox = document.getElementById("preview-include-candidate-checkbox");
const previewIncludeDisabledCheckbox = document.getElementById("preview-include-disabled-checkbox");
const previewRuleIdsInput = document.getElementById("preview-rule-ids-input");
const previewCapturePanel = document.getElementById("preview-capture-panel");
const previewCaptureSelect = document.getElementById("preview-capture-select");
const previewCaptureIdInput = document.getElementById("preview-capture-id-input");
const previewInlinePanel = document.getElementById("preview-inline-panel");
const previewInlineMethod = document.getElementById("preview-inline-method");
const previewInlineScheme = document.getElementById("preview-inline-scheme");
const previewInlineAuthority = document.getElementById("preview-inline-authority");
const previewInlinePath = document.getElementById("preview-inline-path");
const previewInlineRouteId = document.getElementById("preview-inline-route-id");
const previewInlineContentType = document.getElementById("preview-inline-content-type");
const previewInlineWebsocket = document.getElementById("preview-inline-websocket");
const previewInlineStreamed = document.getElementById("preview-inline-streamed");
const addPreviewQueryBtn = document.getElementById("add-preview-query-btn");
const previewInlineQueryRows = document.getElementById("preview-inline-query-rows");
const addPreviewHeaderBtn = document.getElementById("add-preview-header-btn");
const previewInlineHeaderRows = document.getElementById("preview-inline-header-rows");
const addPreviewCookieBtn = document.getElementById("add-preview-cookie-btn");
const previewInlineCookieRows = document.getElementById("preview-inline-cookie-rows");
const previewInlineBodyMode = document.getElementById("preview-inline-body-mode");
const previewInlineJsonWrap = document.getElementById("preview-inline-json-wrap");
const previewInlineJsonTextarea = document.getElementById("preview-inline-json-textarea");
const previewInlineFormWrap = document.getElementById("preview-inline-form-wrap");
const addPreviewFormBtn = document.getElementById("add-preview-form-btn");
const previewInlineFormRows = document.getElementById("preview-inline-form-rows");
const previewInlineRawWrap = document.getElementById("preview-inline-raw-wrap");
const previewInlineRawTextarea = document.getElementById("preview-inline-raw-textarea");

const runRulePreviewBtn = document.getElementById("run-rule-preview-btn");
const previewErrorBox = document.getElementById("preview-error-box");
const previewErrorCode = document.getElementById("preview-error-code");
const previewErrorMessage = document.getElementById("preview-error-message");
const previewResultContainer = document.getElementById("preview-result-container");
const previewOutcomeBox = document.getElementById("preview-outcome-box");
const previewOutcomeBadge = document.getElementById("preview-outcome-badge");
const previewOutcomeSummaryText = document.getElementById("preview-outcome-summary-text");
const previewRulesTbody = document.getElementById("preview-rules-tbody");
const previewChangeCount = document.getElementById("preview-change-count");
const previewChangesTbody = document.getElementById("preview-changes-tbody");
const previewBeforeJson = document.getElementById("preview-before-json");
const previewAfterJson = document.getElementById("preview-after-json");

const copyRuleDialog = document.getElementById("copy-rule-dialog");
const copyRuleForm = document.getElementById("copy-rule-form");
const copyRuleSourceSummary = document.getElementById("copy-rule-source-summary");
const copySourceRuleIdInput = document.getElementById("copy-source-rule-id");
const copyNewRuleNameInput = document.getElementById("copy-new-rule-name");
const copyRuleNameError = document.getElementById("copy-rule-name-error");
const copyNewRuleIdInput = document.getElementById("copy-new-rule-id");
const copyRuleIdError = document.getElementById("copy-rule-id-error");
const copyNewRulePriorityInput = document.getElementById("copy-new-rule-priority");
const copyRuleCancelBtn = document.getElementById("copy-rule-cancel-btn");
const copyRuleConfirmBtn = document.getElementById("copy-rule-confirm-btn");

const deleteRuleDialog = document.getElementById("delete-rule-dialog");
const deleteRuleForm = document.getElementById("delete-rule-form");
const deleteTargetRuleId = document.getElementById("delete-target-rule-id");
const deleteTargetRuleName = document.getElementById("delete-target-rule-name");
const deleteRuleIdInput = document.getElementById("delete-rule-id-input");
const deleteRuleCancelBtn = document.getElementById("delete-rule-cancel-btn");
const deleteRuleConfirmBtn = document.getElementById("delete-rule-confirm-btn");

const discardRuleEditsDialog = document.getElementById("discard-rule-edits-dialog");
const discardRuleEditsForm = document.getElementById("discard-rule-edits-form");
const discardRuleCancelBtn = document.getElementById("discard-rule-cancel-btn");
const discardRuleConfirmBtn = document.getElementById("discard-rule-confirm-btn");

const RULE_ACTION_TYPES = [
  { value: "set_header", label: "Set Header (replace all)" },
  { value: "add_header", label: "Add Header (append pair)" },
  { value: "remove_header", label: "Remove Header" },
  { value: "set_query_param", label: "Set Query Parameter (replace key)" },
  { value: "add_query_param", label: "Add Query Parameter (append pair)" },
  { value: "remove_query_param", label: "Remove Query Parameter" },
  { value: "set_cookie", label: "Set Cookie" },
  { value: "remove_cookie", label: "Remove Cookie" },
  { value: "set_json_field", label: "Set JSON Body Field (dot/index path)" },
  { value: "remove_json_field", label: "Remove JSON Body Field (dot/index path)" },
  { value: "set_form_field", label: "Set Form Body Field (replace key)" },
  { value: "add_form_field", label: "Add Form Body Field (append pair)" },
  { value: "remove_form_field", label: "Remove Form Body Field" },
];

function parseCommaListOrSingle(raw) {
  const parts = String(raw || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (parts.length === 0) {
    return undefined;
  }
  return parts.length === 1 ? parts[0] : parts;
}

function formatListOrSingle(val) {
  if (val === undefined || val === null) {
    return "";
  }
  if (Array.isArray(val)) {
    return val.join(", ");
  }
  return String(val);
}

function summarizeRuleMatch(matchObj) {
  const m = matchObj || {};
  const parts = [];
  if (m.route_id || m.route_ids) {
    parts.push(`route=${formatListOrSingle(m.route_id ?? m.route_ids)}`);
  }
  if (m.domain || m.domains) {
    parts.push(`domain=${formatListOrSingle(m.domain ?? m.domains)}`);
  }
  if (m.method || m.methods) {
    parts.push(`method=${formatListOrSingle(m.method ?? m.methods)}`);
  }
  if (m.path_exact) {
    parts.push(`path==${m.path_exact}`);
  }
  if (m.path_prefix) {
    parts.push(`path^=${m.path_prefix}`);
  }
  if (m.path_regex) {
    parts.push(`path~=${m.path_regex}`);
  }
  if (m.content_type || m.content_types) {
    parts.push(`ct=${formatListOrSingle(m.content_type ?? m.content_types)}`);
  }
  const qList = m.query || m.query_pairs || [];
  if (Array.isArray(qList) && qList.length > 0) {
    parts.push(`query[${qList.length}]`);
  }
  const hList = m.headers || m.header_pairs || [];
  if (Array.isArray(hList) && hList.length > 0) {
    parts.push(`headers[${hList.length}]`);
  }
  return parts.length > 0 ? parts.join(" · ") : "Match All Requests (*)";
}

function normalizeActionToCanonicalType(a) {
  if (a.type && RULE_ACTION_TYPES.some((d) => d.value === a.type)) {
    return a.type;
  }
  const t = String(a.target || a.type || "header").toLowerCase();
  const op = String(a.op || a.action || "set").toLowerCase();
  if (t === "header") {
    return `${op}_header`;
  }
  if (t === "query") {
    return `${op}_query_param`;
  }
  if (t === "cookie") {
    return `${op}_cookie`;
  }
  if (t === "json") {
    return `${op}_json_field`;
  }
  if (t === "form") {
    return `${op}_form_field`;
  }
  return a.type || "set_header";
}

function summarizeRuleActions(actionsList) {
  const acts = Array.isArray(actionsList) ? actionsList : [];
  if (acts.length === 0) {
    return "(no actions)";
  }
  return acts
    .map((a, idx) => {
      const t = normalizeActionToCanonicalType(a);
      const target = a.name ?? a.key ?? a.path ?? a.field ?? "";
      return `#${idx + 1} ${t}(${target})`;
    })
    .join(" → ");
}

function setRuleFormDirty(isDirty) {
  appState.ruleFormDirty = Boolean(isDirty);
  if (ruleFormDirtyBadge) {
    ruleFormDirtyBadge.dataset.dirty = String(appState.ruleFormDirty);
    ruleFormDirtyBadge.className = `badge ${
      appState.ruleFormDirty ? "badge-warn" : "badge-neutral"
    }`;
    ruleFormDirtyBadge.textContent = appState.ruleFormDirty
      ? "Unsaved Edits"
      : "Clean";
  }
  updateRulePayloadPreview();
}

function clearAllRuleFormErrors() {
  clearFieldError(ruleNameInput, ruleNameError);
  clearFieldError(ruleIdInput, ruleIdError);
  clearFieldError(rulePriorityInput, rulePriorityError);
  clearFieldError(ruleMatchPathRegexInput, rulePathRegexError);
  if (ruleMatchError) {
    ruleMatchError.textContent = "";
  }
  if (ruleActionsError) {
    ruleActionsError.textContent = "";
  }
  if (ruleFormErrorBox) {
    ruleFormErrorBox.hidden = true;
    ruleFormErrorBox.dataset.errorCode = "";
  }
}

function showRuleFormError(code, message) {
  if (ruleFormErrorBox) {
    ruleFormErrorBox.hidden = false;
    ruleFormErrorBox.dataset.errorCode = code || "validation_error";
    ruleFormErrorCode.textContent = code || "validation_error";
    ruleFormErrorText.textContent = message;
  }
  if (code === "invalid_rule_name") {
    setFieldError(ruleNameInput, ruleNameError, message);
  } else if (code === "duplicate_rule_id" || code === "invalid_rule_id") {
    setFieldError(ruleIdInput, ruleIdError, message);
  } else if (code === "invalid_priority") {
    setFieldError(rulePriorityInput, rulePriorityError, message);
  } else if (code === "invalid_regex") {
    if (message.includes("path_regex") || ruleMatchPathRegexInput.value.trim()) {
      setFieldError(ruleMatchPathRegexInput, rulePathRegexError, message);
    } else if (ruleMatchError) {
      ruleMatchError.textContent = message;
    }
  } else if (code === "invalid_match_schema") {
    if (ruleMatchError) {
      ruleMatchError.textContent = message;
    }
  } else if (
    code === "invalid_actions" ||
    code === "invalid_action_schema" ||
    code === "invalid_json_path"
  ) {
    if (ruleActionsError) {
      ruleActionsError.textContent = message;
    }
  }
  setFeedback("error", `Rule Validation Error (${code})`, message, {
    errorCode: code,
    hint: "Your input values have been preserved in the form. Correct the highlighted field and click Save Rule again.",
  });
}

function renderMatchPairRows(container, initialItems, kind) {
  container.textContent = "";
  const keyField = kind === "header" ? "name" : "key";
  const list = Array.isArray(initialItems)
    ? initialItems.map((it) => {
        if (Array.isArray(it)) {
          return {
            [keyField]: String(it[0] ?? ""),
            value: String(it[1] ?? ""),
            op: "equals",
          };
        }
        let op = "equals";
        let val = "";
        if (it.regex !== undefined || it.value_regex !== undefined) {
          op = "regex";
          val = String(it.regex ?? it.value_regex ?? "");
        } else if (it.prefix !== undefined) {
          op = "prefix";
          val = String(it.prefix);
        } else if (it.contains !== undefined) {
          op = "contains";
          val = String(it.contains);
        } else if (
          (it.present === true || it.absent === false) &&
          it.value === undefined &&
          it.equals === undefined
        ) {
          op = "present";
          val = "true";
        } else if (it.present === false || it.absent === true) {
          op = "absent";
          val = "false";
        } else {
          op = "equals";
          val = String(it.value ?? it.equals ?? "");
        }
        return {
          [keyField]: String(it[keyField] ?? it.name ?? it.key ?? ""),
          value: val,
          op,
        };
      })
    : [];

  function rebuild() {
    container.textContent = "";
    list.forEach((item, idx) => {
      const row = document.createElement("div");
      row.className = "match-pair-row";
      row.dataset.matchIndex = String(idx);
      row.dataset.matchKind = kind;

      const idxSpan = document.createElement("span");
      idxSpan.className = "pair-index";
      idxSpan.textContent = `#${idx + 1}`;
      row.appendChild(idxSpan);

      const keyInput = document.createElement("input");
      keyInput.type = "text";
      keyInput.className = `match-key-input ${kind}-match-key-input`;
      keyInput.value = item[keyField] || "";
      keyInput.placeholder = kind === "header" ? "Header name (e.g. X-Env)" : "Query key (e.g. tag)";
      keyInput.setAttribute("aria-label", `${kind} matcher #${idx + 1} ${keyField}`);
      keyInput.autocorrect = "off";
      keyInput.autocapitalize = "off";
      keyInput.spellcheck = false;
      keyInput.addEventListener("input", () => {
        item[keyField] = keyInput.value;
        setRuleFormDirty(true);
      });
      row.appendChild(keyInput);

      const opSelect = document.createElement("select");
      opSelect.className = `match-op-select ${kind}-match-op-select`;
      opSelect.setAttribute("aria-label", `${kind} matcher #${idx + 1} operator`);
      for (const [opVal, opLabel] of [
        ["equals", "equals (exact)"],
        ["prefix", "prefix"],
        ["contains", "contains"],
        ["regex", "regex"],
        ["present", "present (exists)"],
        ["absent", "absent (not present)"],
      ]) {
        const opt = document.createElement("option");
        opt.value = opVal;
        opt.textContent = opLabel;
        opSelect.appendChild(opt);
      }
      opSelect.value = item.op || "equals";
      row.appendChild(opSelect);

      const valInput = document.createElement("input");
      valInput.type = "text";
      valInput.className = `match-val-input ${kind}-match-val-input`;
      valInput.value = item.value ?? "";
      valInput.placeholder = "Expected value / pattern";
      valInput.setAttribute("aria-label", `${kind} matcher #${idx + 1} value`);
      valInput.autocorrect = "off";
      valInput.autocapitalize = "off";
      valInput.spellcheck = false;
      valInput.disabled = item.op === "present" || item.op === "absent";
      valInput.addEventListener("input", () => {
        item.value = valInput.value;
        setRuleFormDirty(true);
      });
      opSelect.addEventListener("change", () => {
        item.op = opSelect.value;
        valInput.disabled = item.op === "present" || item.op === "absent";
        setRuleFormDirty(true);
      });
      row.appendChild(valInput);

      const actions = document.createElement("div");
      actions.className = "row-actions";

      const upBtn = document.createElement("button");
      upBtn.type = "button";
      upBtn.className = "btn btn-sm btn-secondary btn-move-up";
      upBtn.disabled = idx === 0;
      upBtn.textContent = "↑";
      upBtn.setAttribute("aria-label", `Move ${kind} matcher #${idx + 1} up`);
      upBtn.addEventListener("click", () => {
        if (idx > 0) {
          const tmp = list[idx - 1];
          list[idx - 1] = list[idx];
          list[idx] = tmp;
          rebuild();
          setRuleFormDirty(true);
        }
      });
      actions.appendChild(upBtn);

      const downBtn = document.createElement("button");
      downBtn.type = "button";
      downBtn.className = "btn btn-sm btn-secondary btn-move-down";
      downBtn.disabled = idx === list.length - 1;
      downBtn.textContent = "↓";
      downBtn.setAttribute("aria-label", `Move ${kind} matcher #${idx + 1} down`);
      downBtn.addEventListener("click", () => {
        if (idx < list.length - 1) {
          const tmp = list[idx + 1];
          list[idx + 1] = list[idx];
          list[idx] = tmp;
          rebuild();
          setRuleFormDirty(true);
        }
      });
      actions.appendChild(downBtn);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn-sm btn-danger btn-remove-match";
      delBtn.textContent = "Remove";
      delBtn.setAttribute("aria-label", `Remove ${kind} matcher #${idx + 1}`);
      delBtn.addEventListener("click", () => {
        list.splice(idx, 1);
        rebuild();
        setRuleFormDirty(true);
      });
      actions.appendChild(delBtn);

      row.appendChild(actions);
      container.appendChild(row);
    });
  }

  rebuild();
  container._matchList = list;
  container._addMatch = (k = "", v = "", op = "equals") => {
    list.push({ [keyField]: String(k), value: String(v), op });
    rebuild();
    setRuleFormDirty(true);
  };
}

function collectMatchRowsFromContainer(container, kind) {
  const keyField = kind === "header" ? "name" : "key";
  const rows = Array.from(container.querySelectorAll(".match-pair-row"));
  const out = [];
  for (const row of rows) {
    const k = row.querySelector(".match-key-input")?.value ?? "";
    const op = row.querySelector(".match-op-select")?.value ?? "equals";
    const v = row.querySelector(".match-val-input")?.value ?? "";
    if (!k && !v) {
      continue;
    }
    const entry = { [keyField]: k };
    if (op === "regex") {
      entry.regex = v;
    } else if (op === "prefix") {
      entry.prefix = v;
    } else if (op === "contains") {
      entry.contains = v;
    } else if (op === "present") {
      entry.present = true;
    } else if (op === "absent") {
      entry.present = false;
    } else {
      entry.value = v;
    }
    out.push(entry);
  }
  return out;
}

function getActionTargetKind(actionType) {
  if (actionType.endsWith("_header") || actionType.endsWith("_cookie")) {
    return "name";
  }
  if (actionType.endsWith("_json_field")) {
    return "path";
  }
  return "key";
}

function actionRequiresValue(actionType) {
  return !actionType.startsWith("remove_");
}

function formatActionValueForInput(action) {
  if (!actionRequiresValue(action.type || "")) {
    return "";
  }
  if ((action.type || "") === "set_json_field") {
    if (action.value === undefined) {
      return "";
    }
    return typeof action.value === "string"
      ? JSON.stringify(action.value)
      : JSON.stringify(action.value);
  }
  return String(action.value ?? "");
}

function parseActionValueFromInput(actionType, rawVal) {
  if (actionType === "set_json_field") {
    const trimmed = String(rawVal ?? "").trim();
    if (!trimmed) {
      return "";
    }
    try {
      return JSON.parse(trimmed);
    } catch (_err) {
      return rawVal;
    }
  }
  return String(rawVal ?? "");
}

function renderRuleActionRows(container, initialActions) {
  container.textContent = "";
  const list = Array.isArray(initialActions)
    ? initialActions.map((a) => {
        const type = normalizeActionToCanonicalType(a);
        const targetField = getActionTargetKind(type);
        return {
          type,
          target: String(a[targetField] ?? a.name ?? a.key ?? a.path ?? a.field ?? ""),
          valueText: formatActionValueForInput({ ...a, type }),
        };
      })
    : [];

  function rebuild() {
    container.textContent = "";
    list.forEach((item, idx) => {
      const row = document.createElement("div");
      row.className = "rule-action-row";
      row.dataset.actionIndex = String(idx);

      const idxSpan = document.createElement("span");
      idxSpan.className = "pair-index";
      idxSpan.textContent = `#${idx + 1}`;
      row.appendChild(idxSpan);

      const typeSelect = document.createElement("select");
      typeSelect.className = "rule-action-type-select";
      typeSelect.setAttribute("aria-label", `Action #${idx + 1} type`);
      for (const def of RULE_ACTION_TYPES) {
        const opt = document.createElement("option");
        opt.value = def.value;
        opt.textContent = def.label;
        typeSelect.appendChild(opt);
      }
      if (!RULE_ACTION_TYPES.some((d) => d.value === item.type)) {
        const customOpt = document.createElement("option");
        customOpt.value = item.type;
        customOpt.textContent = item.type;
        typeSelect.appendChild(customOpt);
      }
      typeSelect.value = item.type;
      row.appendChild(typeSelect);

      const targetInput = document.createElement("input");
      targetInput.type = "text";
      targetInput.className = "rule-action-target-input";
      targetInput.value = item.target;
      const targetKind = getActionTargetKind(item.type);
      targetInput.placeholder =
        targetKind === "path"
          ? "JSON path (e.g. order.debug or items.0.qty)"
          : targetKind === "name"
          ? "Header / Cookie name"
          : "Query / Form key";
      targetInput.setAttribute("aria-label", `Action #${idx + 1} target ${targetKind}`);
      targetInput.autocorrect = "off";
      targetInput.autocapitalize = "off";
      targetInput.spellcheck = false;
      targetInput.addEventListener("input", () => {
        item.target = targetInput.value;
        setRuleFormDirty(true);
      });
      row.appendChild(targetInput);

      const valInput = document.createElement("input");
      valInput.type = "text";
      valInput.className = "rule-action-value-input";
      valInput.value = item.valueText;
      const hasVal = actionRequiresValue(item.type);
      valInput.disabled = !hasVal;
      valInput.placeholder = !hasVal
        ? "(not used for remove)"
        : item.type === "set_json_field"
        ? 'JSON value (e.g. true, 42, "text", {"k":1})'
        : "Value (preserves spaces & Unicode)";
      valInput.setAttribute("aria-label", `Action #${idx + 1} value`);
      valInput.autocorrect = "off";
      valInput.autocapitalize = "off";
      valInput.spellcheck = false;
      valInput.addEventListener("input", () => {
        item.valueText = valInput.value;
        setRuleFormDirty(true);
      });
      row.appendChild(valInput);

      typeSelect.addEventListener("change", () => {
        item.type = typeSelect.value;
        const needsV = actionRequiresValue(item.type);
        valInput.disabled = !needsV;
        const tk = getActionTargetKind(item.type);
        targetInput.placeholder =
          tk === "path"
            ? "JSON path (e.g. order.debug or items.0.qty)"
            : tk === "name"
            ? "Header / Cookie name"
            : "Query / Form key";
        setRuleFormDirty(true);
      });

      const actions = document.createElement("div");
      actions.className = "row-actions";

      const upBtn = document.createElement("button");
      upBtn.type = "button";
      upBtn.className = "btn btn-sm btn-secondary btn-action-up";
      upBtn.disabled = idx === 0;
      upBtn.textContent = "↑";
      upBtn.setAttribute("aria-label", `Move action #${idx + 1} up`);
      upBtn.addEventListener("click", () => {
        if (idx > 0) {
          const tmp = list[idx - 1];
          list[idx - 1] = list[idx];
          list[idx] = tmp;
          rebuild();
          setRuleFormDirty(true);
        }
      });
      actions.appendChild(upBtn);

      const downBtn = document.createElement("button");
      downBtn.type = "button";
      downBtn.className = "btn btn-sm btn-secondary btn-action-down";
      downBtn.disabled = idx === list.length - 1;
      downBtn.textContent = "↓";
      downBtn.setAttribute("aria-label", `Move action #${idx + 1} down`);
      downBtn.addEventListener("click", () => {
        if (idx < list.length - 1) {
          const tmp = list[idx + 1];
          list[idx + 1] = list[idx];
          list[idx] = tmp;
          rebuild();
          setRuleFormDirty(true);
        }
      });
      actions.appendChild(downBtn);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn-sm btn-danger btn-remove-action";
      delBtn.textContent = "Remove";
      delBtn.setAttribute("aria-label", `Remove action #${idx + 1}`);
      delBtn.addEventListener("click", () => {
        list.splice(idx, 1);
        rebuild();
        setRuleFormDirty(true);
      });
      actions.appendChild(delBtn);

      row.appendChild(actions);
      container.appendChild(row);
    });
  }

  rebuild();
  container._actionList = list;
  container._addAction = (
    type = "set_header",
    target = "",
    valueText = ""
  ) => {
    list.push({ type, target: String(target), valueText: String(valueText) });
    rebuild();
    setRuleFormDirty(true);
  };
}

function collectRuleActionsFromContainer(container) {
  const rows = Array.from(container.querySelectorAll(".rule-action-row"));
  return rows.map((row) => {
    const type = row.querySelector(".rule-action-type-select")?.value ?? "set_header";
    const target = row.querySelector(".rule-action-target-input")?.value ?? "";
    const rawVal = row.querySelector(".rule-action-value-input")?.value ?? "";
    const targetKind = getActionTargetKind(type);
    const act = {
      type,
      [targetKind]: target,
    };
    if (actionRequiresValue(type)) {
      act.value = parseActionValueFromInput(type, rawVal);
    }
    return act;
  });
}

function collectRulePayloadFromUI() {
  const match = {};
  const routeIdVal = parseCommaListOrSingle(ruleMatchRouteIdInput.value);
  if (routeIdVal !== undefined) match.route_id = routeIdVal;

  const domainVal = parseCommaListOrSingle(ruleMatchDomainInput.value);
  if (domainVal !== undefined) match.domain = domainVal;

  const methodVal = parseCommaListOrSingle(ruleMatchMethodInput.value);
  if (methodVal !== undefined) match.method = methodVal;

  if (ruleMatchPathExactInput.value.trim()) {
    match.path_exact = ruleMatchPathExactInput.value.trim();
  }
  if (ruleMatchPathPrefixInput.value.trim()) {
    match.path_prefix = ruleMatchPathPrefixInput.value.trim();
  }
  if (ruleMatchPathRegexInput.value.trim()) {
    match.path_regex = ruleMatchPathRegexInput.value.trim();
  }
  if (ruleMatchContentTypeInput.value.trim()) {
    match.content_type = ruleMatchContentTypeInput.value.trim();
  }

  const queryMatchers = collectMatchRowsFromContainer(ruleQueryMatchRows, "query");
  if (queryMatchers.length > 0) {
    match.query = queryMatchers;
  }

  const headerMatchers = collectMatchRowsFromContainer(ruleHeaderMatchRows, "header");
  if (headerMatchers.length > 0) {
    match.headers = headerMatchers;
  }

  const rawPriority = rulePriorityInput.value.trim();
  const parsedPriority =
    rawPriority !== "" && /^-?\d+$/.test(rawPriority)
      ? Number(rawPriority)
      : rawPriority;

  const payload = {
    name: ruleNameInput.value.trim(),
    enabled: Boolean(ruleEnabledCheckbox.checked),
    priority: parsedPriority,
    description: ruleDescriptionInput.value.trim(),
    match,
    actions: collectRuleActionsFromContainer(ruleActionRows),
  };

  const rId = ruleIdInput.value.trim();
  if (rId) {
    payload.rule_id = rId;
  }
  return payload;
}

function updateRulePayloadPreview() {
  if (!rulePayloadPreview) {
    return;
  }
  try {
    const payload = collectRulePayloadFromUI();
    rulePayloadPreview.textContent = JSON.stringify(payload, null, 2);
  } catch (_err) {
    rulePayloadPreview.textContent = "{}";
  }
}

function resetRuleForm({ skipDirtyClear = false } = {}) {
  clearAllRuleFormErrors();
  ruleEditModeInput.value = "create";
  ruleOriginalIdInput.value = "";
  ruleFormTitle.textContent = "Create New Rule";
  ruleFormModeBadge.textContent = "Create Mode";
  appState.selectedRuleId = null;

  ruleNameInput.value = "";
  ruleIdInput.value = "";
  ruleIdInput.readOnly = false;
  rulePriorityInput.value = "100";
  ruleDescriptionInput.value = "";
  ruleEnabledCheckbox.checked = true;

  ruleMatchRouteIdInput.value = "";
  ruleMatchDomainInput.value = "";
  ruleMatchMethodInput.value = "";
  ruleMatchPathExactInput.value = "";
  ruleMatchPathPrefixInput.value = "";
  ruleMatchPathRegexInput.value = "";
  ruleMatchContentTypeInput.value = "";

  renderMatchPairRows(ruleQueryMatchRows, [], "query");
  renderMatchPairRows(ruleHeaderMatchRows, [], "header");
  renderRuleActionRows(ruleActionRows, [
    { type: "set_header", name: "X-Proxy-Rule", value: "active" },
  ]);

  if (!skipDirtyClear) {
    setRuleFormDirty(false);
  }
  renderRulesTable();
}

function populateRuleFormForEdit(rule) {
  clearAllRuleFormErrors();
  ruleEditModeInput.value = "edit";
  ruleOriginalIdInput.value = rule.rule_id;
  appState.selectedRuleId = rule.rule_id;
  ruleFormTitle.textContent = `Edit Rule: ${rule.rule_id}`;
  ruleFormModeBadge.textContent = "Edit Mode";

  ruleNameInput.value = rule.name || "";
  ruleIdInput.value = rule.rule_id || "";
  ruleIdInput.readOnly = true;
  rulePriorityInput.value = String(rule.priority ?? 100);
  ruleDescriptionInput.value = rule.description || "";
  ruleEnabledCheckbox.checked = Boolean(rule.enabled);

  const m = rule.match || {};
  ruleMatchRouteIdInput.value = formatListOrSingle(m.route_id ?? m.route_ids);
  ruleMatchDomainInput.value = formatListOrSingle(m.domain ?? m.domains);
  ruleMatchMethodInput.value = formatListOrSingle(m.method ?? m.methods);
  ruleMatchPathExactInput.value = m.path_exact || "";
  ruleMatchPathPrefixInput.value = m.path_prefix || "";
  ruleMatchPathRegexInput.value = m.path_regex || "";
  ruleMatchContentTypeInput.value = formatListOrSingle(m.content_type ?? m.content_types);

  renderMatchPairRows(ruleQueryMatchRows, m.query || m.query_pairs || [], "query");
  renderMatchPairRows(ruleHeaderMatchRows, m.headers || m.header_pairs || [], "header");
  renderRuleActionRows(ruleActionRows, rule.actions || []);

  setRuleFormDirty(false);
  renderRulesTable();
  ruleNameInput.focus();
  setFeedback(
    "idle",
    "Editing Rule",
    `Loaded rule ${rule.rule_id} (${rule.name}) into the structured editor.`
  );
}

function requestRuleFormResetOrSwitch(callback) {
  if (!appState.ruleFormDirty) {
    callback();
    return;
  }
  appState.pendingRuleDiscardCallback = callback;
  discardRuleEditsDialog.returnValue = "";
  discardRuleEditsDialog.showModal();
}

function renderRulesTable() {
  if (!rulesTbody) {
    return;
  }
  rulesTbody.textContent = "";
  const rules = appState.rules || [];
  const enabledCount = rules.filter((r) => r.enabled).length;
  rulesCountBadge.textContent = `${enabledCount} / ${rules.length} enabled`;

  if (rules.length === 0) {
    rulesEmptyState.hidden = false;
    return;
  }
  rulesEmptyState.hidden = true;

  rules.forEach((rule, idx) => {
    const tr = document.createElement("tr");
    tr.dataset.ruleId = rule.rule_id;
    tr.dataset.priority = String(rule.priority);
    tr.dataset.enabled = String(Boolean(rule.enabled));
    if (appState.selectedRuleId === rule.rule_id) {
      tr.dataset.selected = "true";
    }

    // 1. Name & ID
    const thName = document.createElement("th");
    thName.scope = "row";
    const nameStrong = document.createElement("strong");
    nameStrong.textContent = rule.name;
    thName.appendChild(nameStrong);
    const idMeta = document.createElement("span");
    idMeta.className = "cell-meta";
    idMeta.textContent = rule.rule_id;
    thName.appendChild(idMeta);
    if (rule.description) {
      const descMeta = document.createElement("span");
      descMeta.className = "cell-meta";
      descMeta.textContent = rule.description;
      thName.appendChild(descMeta);
    }
    tr.appendChild(thName);

    // 2. Enabled badge
    const tdEnabled = document.createElement("td");
    const enBadge = document.createElement("span");
    enBadge.className = `badge ${rule.enabled ? "badge-success" : "badge-neutral"}`;
    enBadge.dataset.enabled = String(Boolean(rule.enabled));
    enBadge.textContent = rule.enabled ? "Enabled" : "Disabled";
    tdEnabled.appendChild(enBadge);
    tr.appendChild(tdEnabled);

    // 3. Priority & Quick Reorder
    const tdPriority = document.createElement("td");
    const prioWrap = document.createElement("div");
    prioWrap.className = "priority-inline-controls";
    const prioCode = document.createElement("code");
    prioCode.className = "rule-priority-badge";
    prioCode.textContent = String(rule.priority);
    prioWrap.appendChild(prioCode);

    const prioUpBtn = document.createElement("button");
    prioUpBtn.type = "button";
    prioUpBtn.className = "btn btn-sm btn-secondary btn-rule-priority-earlier";
    prioUpBtn.dataset.ruleId = rule.rule_id;
    prioUpBtn.textContent = "↑ Earlier (-10)";
    prioUpBtn.setAttribute(
      "aria-label",
      `Move rule ${rule.rule_id} earlier by decreasing priority`
    );
    prioUpBtn.addEventListener("click", () => {
      const targetPrio =
        idx > 0
          ? Math.min(Number(rule.priority) - 10, Number(rules[idx - 1].priority) - 5)
          : Number(rule.priority) - 10;
      handleQuickPriorityUpdate(rule.rule_id, Math.max(-100000, targetPrio));
    });
    prioWrap.appendChild(prioUpBtn);

    const prioDownBtn = document.createElement("button");
    prioDownBtn.type = "button";
    prioDownBtn.className = "btn btn-sm btn-secondary btn-rule-priority-later";
    prioDownBtn.dataset.ruleId = rule.rule_id;
    prioDownBtn.textContent = "↓ Later (+10)";
    prioDownBtn.setAttribute(
      "aria-label",
      `Move rule ${rule.rule_id} later by increasing priority`
    );
    prioDownBtn.addEventListener("click", () => {
      const targetPrio =
        idx < rules.length - 1
          ? Math.max(Number(rule.priority) + 10, Number(rules[idx + 1].priority) + 5)
          : Number(rule.priority) + 10;
      handleQuickPriorityUpdate(rule.rule_id, Math.min(100000, targetPrio));
    });
    prioWrap.appendChild(prioDownBtn);

    tdPriority.appendChild(prioWrap);
    tr.appendChild(tdPriority);

    // 4. Match Summary
    const tdMatch = document.createElement("td");
    const matchCode = document.createElement("code");
    matchCode.textContent = summarizeRuleMatch(rule.match);
    tdMatch.appendChild(matchCode);
    tr.appendChild(tdMatch);

    // 5. Actions Summary
    const tdActs = document.createElement("td");
    const actsCode = document.createElement("code");
    actsCode.textContent = summarizeRuleActions(rule.actions);
    tdActs.appendChild(actsCode);
    tr.appendChild(tdActs);

    // 6. Row Actions
    const tdRowActions = document.createElement("td");
    const actWrap = document.createElement("div");
    actWrap.className = "row-actions";

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "btn btn-sm btn-secondary btn-toggle-rule";
    toggleBtn.dataset.ruleId = rule.rule_id;
    toggleBtn.textContent = rule.enabled ? "Disable" : "Enable";
    toggleBtn.addEventListener("click", () =>
      handleToggleRule(rule.rule_id, !rule.enabled)
    );
    actWrap.appendChild(toggleBtn);

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "btn btn-sm btn-secondary btn-edit-rule";
    editBtn.dataset.ruleId = rule.rule_id;
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", () =>
      requestRuleFormResetOrSwitch(() => populateRuleFormForEdit(rule))
    );
    actWrap.appendChild(editBtn);

    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "btn btn-sm btn-secondary btn-copy-rule";
    copyBtn.dataset.ruleId = rule.rule_id;
    copyBtn.textContent = "Copy";
    copyBtn.addEventListener("click", () => openCopyRuleDialog(rule));
    actWrap.appendChild(copyBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn btn-sm btn-danger btn-delete-rule";
    delBtn.dataset.ruleId = rule.rule_id;
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => openDeleteRuleDialog(rule));
    actWrap.appendChild(delBtn);

    tdRowActions.appendChild(actWrap);
    tr.appendChild(tdRowActions);
    rulesTbody.appendChild(tr);
  });
}

async function loadRulesList({ preserveBanner = false } = {}) {
  beginOp();
  try {
    const enabledFilter = rulesFilterEnabledSelect?.value || "";
    const qs = enabledFilter ? `?enabled=${encodeURIComponent(enabledFilter)}` : "";
    const data = await apiRequest(`/api/v1/rules${qs}`);
    appState.rules = data.items || [];
    appState.rulesAvailable = true;
    appState.rulesError = null;

    rulesAvailabilityBadge.dataset.available = "true";
    rulesAvailabilityBadge.className = "badge badge-success";
    rulesAvailabilityBadge.textContent = "Rules API Ready";
    rulesErrorBox.hidden = true;
    rulesErrorBox.dataset.errorCode = "";

    renderRulesTable();
    populatePreviewCaptureSelect();

    if (!preserveBanner) {
      setFeedback(
        "idle",
        "Rules Loaded",
        `Loaded ${appState.rules.length} automatic modification rules.`
      );
    }
  } catch (err) {
    appState.rulesAvailable = false;
    appState.rulesError = err.message;
    rulesAvailabilityBadge.dataset.available = "false";
    rulesAvailabilityBadge.className = "badge badge-danger";
    rulesAvailabilityBadge.textContent = `Rules Unavailable (${err.code || "error"})`;

    rulesErrorBox.hidden = false;
    rulesErrorBox.dataset.errorCode = err.code || "rules_unavailable";
    rulesErrorTitle.textContent = `Rules API Unavailable (${err.code || "error"})`;
    rulesErrorMessage.textContent = err.message;

    if (!preserveBanner) {
      setFeedback("error", `Rules API Error (${err.code})`, err.message, {
        errorCode: err.code,
        hint: "Routes, Docker Discovery, Captured Requests, and Replay workspaces remain available.",
      });
    }
  } finally {
    endOp();
  }
}

async function handleRuleFormSubmit(event) {
  event.preventDefault();
  clearAllRuleFormErrors();

  const isEdit = ruleEditModeInput.value === "edit";
  const origId = ruleOriginalIdInput.value.trim();
  const payload = collectRulePayloadFromUI();

  if (!payload.name) {
    showRuleFormError("invalid_rule_name", "Rule Name is required.");
    ruleNameInput.focus();
    return;
  }
  if (!Array.isArray(payload.actions) || payload.actions.length === 0) {
    showRuleFormError(
      "invalid_actions",
      "At least one modification action is required."
    );
    return;
  }

  ruleSubmitBtn.disabled = true;
  beginOp();
  try {
    let saved;
    if (isEdit && origId) {
      const updateBody = { ...payload };
      delete updateBody.rule_id;
      saved = await apiRequest(`/api/v1/rules/${encodeURIComponent(origId)}`, {
        method: "PUT",
        body: JSON.stringify(updateBody),
      });
    } else {
      saved = await apiRequest("/api/v1/rules", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    }

    await loadRulesList({ preserveBanner: true });
    setRuleFormDirty(false);
    populateRuleFormForEdit(saved);
    setFeedback(
      "success",
      isEdit ? "Rule Updated" : "Rule Created",
      `Saved rule ${saved.rule_id} (${saved.name}, priority ${saved.priority}). Active immediately for subsequent requests.`
    );
  } catch (err) {
    // Rule 7: Preserve all user form inputs on validation/API error so the user can fix and retry
    showRuleFormError(err.code || "validation_error", err.message);
  } finally {
    ruleSubmitBtn.disabled = false;
    endOp();
  }
}

async function handleToggleRule(ruleId, enabled) {
  beginOp();
  try {
    const endpoint = enabled ? "enable" : "disable";
    const updated = await apiRequest(
      `/api/v1/rules/${encodeURIComponent(ruleId)}/${endpoint}`,
      {
        method: "POST",
        body: JSON.stringify({}),
      }
    );
    await loadRulesList({ preserveBanner: true });
    if (appState.selectedRuleId === ruleId && !appState.ruleFormDirty) {
      populateRuleFormForEdit(updated);
    }
    setFeedback(
      "success",
      enabled ? "Rule Enabled" : "Rule Disabled",
      `Rule ${updated.rule_id} is now ${
        updated.enabled ? "enabled" : "disabled"
      } for subsequent requests.`
    );
  } catch (err) {
    setFeedback("error", `Failed to Toggle Rule (${err.code})`, err.message, {
      errorCode: err.code,
    });
  } finally {
    endOp();
  }
}

async function handleQuickPriorityUpdate(ruleId, newPriority) {
  beginOp();
  try {
    const updated = await apiRequest(
      `/api/v1/rules/${encodeURIComponent(ruleId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ priority: newPriority }),
      }
    );
    await loadRulesList({ preserveBanner: true });
    if (appState.selectedRuleId === ruleId && !appState.ruleFormDirty) {
      populateRuleFormForEdit(updated);
    }
    setFeedback(
      "success",
      "Rule Priority Updated",
      `Updated priority of ${updated.rule_id} to ${updated.priority}.`
    );
  } catch (err) {
    setFeedback("error", `Priority Update Failed (${err.code})`, err.message, {
      errorCode: err.code,
    });
  } finally {
    endOp();
  }
}

function openCopyRuleDialog(rule) {
  appState.copySourceRule = rule;
  clearFieldError(copyNewRuleNameInput, copyRuleNameError);
  clearFieldError(copyNewRuleIdInput, copyRuleIdError);
  copySourceRuleIdInput.value = rule.rule_id;
  copyRuleSourceSummary.textContent = `Copying from ${rule.rule_id} (${rule.name}, priority ${rule.priority}). Enter a new Rule Name and distinct Rule ID.`;
  copyNewRuleNameInput.value = `Copy of ${rule.name}`;
  copyNewRuleIdInput.value = `${rule.rule_id}-copy`;
  copyNewRulePriorityInput.value = String(rule.priority ?? 100);
  copyRuleDialog.returnValue = "";
  copyRuleDialog.showModal();
}

async function handleCopyRuleSubmit(event) {
  event.preventDefault();
  const src = appState.copySourceRule;
  if (!src) {
    copyRuleDialog.close("cancel");
    return;
  }
  clearFieldError(copyNewRuleNameInput, copyRuleNameError);
  clearFieldError(copyNewRuleIdInput, copyRuleIdError);

  const newName = copyNewRuleNameInput.value.trim();
  const newId = copyNewRuleIdInput.value.trim();
  const rawPrio = copyNewRulePriorityInput.value.trim();

  if (!newName) {
    setFieldError(copyNewRuleNameInput, copyRuleNameError, "New Rule Name is required.");
    return;
  }
  if (!newId) {
    setFieldError(copyNewRuleIdInput, copyRuleIdError, "New Rule ID is required when copying.");
    return;
  }
  if (newId === src.rule_id) {
    setFieldError(
      copyNewRuleIdInput,
      copyRuleIdError,
      `New Rule ID must be different from source '${src.rule_id}'.`
    );
    return;
  }

  copyRuleConfirmBtn.disabled = true;
  beginOp();
  try {
    const prio =
      rawPrio !== "" && /^-?\d+$/.test(rawPrio) ? Number(rawPrio) : src.priority;
    const created = await apiRequest("/api/v1/rules", {
      method: "POST",
      body: JSON.stringify({
        rule_id: newId,
        name: newName,
        enabled: Boolean(src.enabled),
        priority: prio,
        description: src.description
          ? `${src.description} (copied from ${src.rule_id})`
          : `Copied from ${src.rule_id}`,
        match: src.match || {},
        actions: src.actions || [],
      }),
    });
    copyRuleDialog.close("confirm");
    await loadRulesList({ preserveBanner: true });
    if (!appState.ruleFormDirty) {
      populateRuleFormForEdit(created);
    }
    setFeedback(
      "success",
      "Rule Copied",
      `Created rule ${created.rule_id} (${created.name}) copied from ${src.rule_id}. Source rule remains unchanged.`
    );
  } catch (err) {
    setFieldError(copyNewRuleIdInput, copyRuleIdError, err.message);
    setFeedback("error", `Copy Rule Failed (${err.code})`, err.message, {
      errorCode: err.code,
    });
  } finally {
    copyRuleConfirmBtn.disabled = false;
    endOp();
  }
}

function openDeleteRuleDialog(rule) {
  appState.deleteTargetRule = rule;
  deleteTargetRuleId.textContent = rule.rule_id;
  deleteTargetRuleName.textContent = rule.name;
  deleteRuleIdInput.value = rule.rule_id;
  deleteRuleDialog.returnValue = "";
  deleteRuleDialog.showModal();
}

async function handleDeleteRuleSubmit(event) {
  event.preventDefault();
  const target = appState.deleteTargetRule;
  if (!target) {
    deleteRuleDialog.close("cancel");
    return;
  }
  deleteRuleConfirmBtn.disabled = true;
  beginOp();
  try {
    await apiRequest(`/api/v1/rules/${encodeURIComponent(target.rule_id)}`, {
      method: "DELETE",
    });
    deleteRuleDialog.close("delete");
    if (appState.selectedRuleId === target.rule_id) {
      setRuleFormDirty(false);
      resetRuleForm();
    }
    await loadRulesList({ preserveBanner: true });
    setFeedback(
      "success",
      "Rule Deleted",
      `Deleted rule ${target.rule_id} (${target.name}).`
    );
  } catch (err) {
    deleteRuleDialog.close("error");
    setFeedback("error", `Delete Rule Failed (${err.code})`, err.message, {
      errorCode: err.code,
    });
  } finally {
    deleteRuleConfirmBtn.disabled = false;
    appState.deleteTargetRule = null;
    endOp();
  }
}

// Dry-Run Preview Inspector Functions
function syncPreviewSourceModeUI() {
  const isCapture = previewSourceCaptureRadio.checked;
  previewCapturePanel.hidden = !isCapture;
  previewInlinePanel.hidden = isCapture;
}

function syncPreviewInlineBodyModeUI() {
  const mode = previewInlineBodyMode.value;
  previewInlineJsonWrap.hidden = mode !== "json";
  previewInlineFormWrap.hidden = mode !== "urlencoded_form";
  previewInlineRawWrap.hidden = mode !== "raw_text";
}

function populatePreviewCaptureSelect() {
  if (!previewCaptureSelect) {
    return;
  }
  const prev = previewCaptureSelect.value;
  previewCaptureSelect.textContent = "";
  const defOpt = document.createElement("option");
  defOpt.value = "";
  defOpt.textContent = "-- Select from loaded captures --";
  previewCaptureSelect.appendChild(defOpt);

  for (const c of appState.captures || []) {
    const opt = document.createElement("option");
    opt.value = c.capture_id;
    opt.textContent = `${c.capture_id} (${c.method} ${c.authority}${c.raw_target})`;
    previewCaptureSelect.appendChild(opt);
  }
  if (
    prev &&
    Array.from(previewCaptureSelect.options).some((o) => o.value === prev)
  ) {
    previewCaptureSelect.value = prev;
  }
}

function collectPreviewPayloadFromUI() {
  const payload = {
    include_disabled: Boolean(previewIncludeDisabledCheckbox.checked),
  };

  const ruleIdsRaw = previewRuleIdsInput.value.trim();
  if (ruleIdsRaw) {
    payload.rule_ids = ruleIdsRaw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  if (previewIncludeCandidateCheckbox.checked) {
    const candidate = collectRulePayloadFromUI();
    if (!candidate.name) {
      candidate.name = "Unsaved Candidate Rule Preview";
    }
    if (!candidate.rule_id) {
      candidate.rule_id = "rule:candidate-preview";
    }
    payload.candidate_rule = candidate;
    payload.rule = candidate;
  }

  if (previewSourceCaptureRadio.checked) {
    const cid =
      previewCaptureIdInput.value.trim() || previewCaptureSelect.value.trim();
    if (!cid) {
      const err = new Error(
        "Select a stored capture or enter a Capture ID for Capture Preview mode."
      );
      err.code = "invalid_preview_request";
      throw err;
    }
    payload.capture_id = cid;
  } else {
    const bMode = previewInlineBodyMode.value;
    const reqObj = {
      method: previewInlineMethod.value,
      scheme: previewInlineScheme.value,
      authority: previewInlineAuthority.value.trim() || "app-a.local.test:8443",
      path: previewInlinePath.value.trim() || "/",
      content_type: previewInlineContentType.value.trim(),
      is_websocket: Boolean(previewInlineWebsocket.checked),
      is_streamed: Boolean(previewInlineStreamed.checked),
      query_params: readPairsFromContainer(previewInlineQueryRows),
      headers: readPairsFromContainer(previewInlineHeaderRows),
      cookies: readPairsFromContainer(previewInlineCookieRows),
    };
    if (previewInlineRouteId.value.trim()) {
      reqObj.route_id = previewInlineRouteId.value.trim();
    }
    if (bMode === "json") {
      reqObj.body_text = previewInlineJsonTextarea.value;
    } else if (bMode === "urlencoded_form") {
      reqObj.form_fields = readPairsFromContainer(previewInlineFormRows);
    } else if (bMode === "raw_text") {
      reqObj.body_text = previewInlineRawTextarea.value;
    } else {
      reqObj.body_text = "";
    }
    payload.request = reqObj;
  }
  return payload;
}

function getOutcomeBadgeClass(outcome) {
  if (outcome === "applied") {
    return "badge-success";
  }
  if (outcome === "failed") {
    return "badge-danger";
  }
  if (outcome === "skipped" || String(outcome || "").startsWith("skipped")) {
    return "badge-warn";
  }
  return "badge-neutral";
}

function renderRulePreviewResult(res) {
  appState.lastRulePreviewResult = res;
  previewErrorBox.hidden = true;
  previewErrorBox.dataset.errorCode = "";
  previewResultContainer.hidden = false;

  const outcome = res.outcome || "no_match";
  previewOutcomeBox.dataset.outcome = outcome;
  previewOutcomeBox.dataset.replayable =
    outcome === "failed" ? "false" : "true";
  previewOutcomeBadge.className = `badge ${getOutcomeBadgeClass(outcome)}`;
  previewOutcomeBadge.textContent = outcome;

  const matchedCount = (res.matched_rule_ids || []).length;
  const appliedCount = (res.applied_rule_ids || []).length;
  const skippedCount = (res.skipped_rule_ids || []).length;
  const failedCount = (res.failed_rule_ids || []).length;
  previewOutcomeSummaryText.textContent = `matched=${matchedCount}, applied=${appliedCount}, skipped=${skippedCount}, failed=${failedCount}`;

  // Per-Rule Breakdown Table
  previewRulesTbody.textContent = "";
  const ruleResults = res.rule_results || res.rules || [];
  if (ruleResults.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = "No rules matched or evaluated.";
    tr.appendChild(td);
    previewRulesTbody.appendChild(tr);
  } else {
    for (const rr of ruleResults) {
      const tr = document.createElement("tr");
      tr.dataset.ruleId = rr.rule_id;
      tr.dataset.outcome = rr.outcome;
      tr.dataset.reason = rr.reason || "";

      const tdId = document.createElement("td");
      const strong = document.createElement("strong");
      strong.textContent = rr.name || rr.rule_id;
      tdId.appendChild(strong);
      const meta = document.createElement("span");
      meta.className = "cell-meta";
      meta.textContent = rr.rule_id;
      tdId.appendChild(meta);
      tr.appendChild(tdId);

      const tdPrio = document.createElement("td");
      tdPrio.textContent = String(rr.priority ?? 100);
      tr.appendChild(tdPrio);

      const tdOut = document.createElement("td");
      const b = document.createElement("span");
      b.className = `badge ${getOutcomeBadgeClass(rr.outcome)}`;
      b.dataset.outcome = rr.outcome;
      b.textContent = rr.outcome;
      tdOut.appendChild(b);
      tr.appendChild(tdOut);

      const tdReason = document.createElement("td");
      const reasonParts = [];
      if (rr.reason) {
        reasonParts.push(`reason: ${rr.reason}`);
      }
      if (Array.isArray(rr.errors) && rr.errors.length > 0) {
        for (const err of rr.errors) {
          reasonParts.push(
            `${err.code || err.reason || "error"}: ${err.message || ""}`
          );
        }
      }
      if (Array.isArray(rr.skipped_actions) && rr.skipped_actions.length > 0) {
        for (const sa of rr.skipped_actions) {
          reasonParts.push(
            `${sa.target}_${sa.op}(${sa.field}): skipped (${sa.reason})`
          );
        }
      }
      if (Array.isArray(rr.changes) && rr.changes.length > 0) {
        for (const ch of rr.changes) {
          reasonParts.push(`${ch.target}_${ch.op}(${ch.field})`);
        }
      }
      if (Array.isArray(rr.actions) && rr.actions.length > 0) {
        for (const a of rr.actions) {
          const st = a.status || "";
          const rsn = a.reason ? ` (${a.reason})` : "";
          const msg = a.message ? ` - ${a.message}` : "";
          reasonParts.push(`${a.type}: ${st}${rsn}${msg}`);
        }
      }
      const code = document.createElement("code");
      code.textContent = reasonParts.join("; ") || "-";
      tdReason.appendChild(code);
      tr.appendChild(tdReason);

      previewRulesTbody.appendChild(tr);
    }
  }

  // Change Summary Table
  const changes = res.change_summary || [];
  previewChangeCount.textContent = String(changes.length);
  previewChangesTbody.textContent = "";
  if (changes.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.textContent = "(no changes applied)";
    tr.appendChild(td);
    previewChangesTbody.appendChild(tr);
  } else {
    for (const ch of changes) {
      const tr = document.createElement("tr");
      tr.dataset.ruleId = ch.rule_id || "";
      const actLabel = ch.type || (ch.target && ch.op ? `${ch.target}_${ch.op}` : "modify");
      tr.dataset.actionType = actLabel;
      tr.dataset.status = ch.status || "applied";
      tr.dataset.reason = ch.reason || "";

      const tdR = document.createElement("td");
      const rCode = document.createElement("code");
      rCode.textContent = ch.rule_id || "-";
      tdR.appendChild(rCode);
      tr.appendChild(tdR);

      const tdType = document.createElement("td");
      const tCode = document.createElement("code");
      tCode.textContent = actLabel;
      tdType.appendChild(tCode);
      tr.appendChild(tdType);

      const tdKey = document.createElement("td");
      const kCode = document.createElement("code");
      kCode.textContent = ch.name ?? ch.key ?? ch.path ?? ch.field ?? "-";
      tdKey.appendChild(kCode);
      tr.appendChild(tdKey);

      const tdSt = document.createElement("td");
      const stBadge = document.createElement("span");
      const stVal = ch.status || "applied";
      stBadge.className = `badge ${getOutcomeBadgeClass(stVal)}`;
      stBadge.textContent = ch.reason ? `${stVal} (${ch.reason})` : stVal;
      tdSt.appendChild(stBadge);
      tr.appendChild(tdSt);

      const tdDiff = document.createElement("td");
      const diffCode = document.createElement("code");
      diffCode.textContent =
        ch.before !== undefined || ch.after !== undefined
          ? `${JSON.stringify(ch.before)} → ${JSON.stringify(ch.after)}`
          : JSON.stringify(ch.value ?? "(removed)");
      tdDiff.appendChild(diffCode);
      tr.appendChild(tdDiff);

      previewChangesTbody.appendChild(tr);
    }
  }

  previewBeforeJson.textContent = JSON.stringify(
    res.before || res.before_summary || {},
    null,
    2
  );
  previewAfterJson.textContent = JSON.stringify(
    res.after || res.modified_request || res.after_summary || {},
    null,
    2
  );
}

async function runRulePreview() {
  previewErrorBox.hidden = true;
  previewErrorBox.dataset.errorCode = "";
  let payload;
  try {
    payload = collectPreviewPayloadFromUI();
  } catch (err) {
    previewErrorBox.hidden = false;
    previewErrorBox.dataset.errorCode = err.code || "invalid_preview_request";
    previewErrorCode.textContent = err.code || "invalid_preview_request";
    previewErrorMessage.textContent = err.message;
    setFeedback("error", `Preview Error (${err.code})`, err.message, {
      errorCode: err.code,
    });
    return;
  }

  runRulePreviewBtn.disabled = true;
  beginOp();
  try {
    const res = await apiRequest("/api/v1/rules/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderRulePreviewResult(res);

    // Rule 6: Never present skipped / failed outcomes as a success banner!
    if (res.outcome === "failed") {
      setFeedback(
        "error",
        `Dry-Run Preview Outcome: FAILED`,
        `One or more rules or actions failed during dry-run preview (failed_rule_ids: ${(
          res.failed_rule_ids || []
        ).join(", ")}). No traffic was sent.`,
        {
          errorCode: "rule_preview_failed",
          hint: "Inspect the Evaluated Rules Breakdown and Change Summary tables for exact failure reasons.",
        }
      );
    } else if (
      res.outcome === "skipped" ||
      String(res.outcome || "").startsWith("skipped")
    ) {
      setFeedback(
        "error",
        `Dry-Run Preview Outcome: SKIPPED`,
        `Rules were skipped during dry-run preview (skipped_rule_ids: ${(
          res.skipped_rule_ids || []
        ).join(", ")}). No traffic was sent.`,
        {
          errorCode: "rule_preview_skipped",
          hint: "Check the skip reason (e.g. protected_header, streamed_request, websocket_handshake, unsupported_body_type).",
        }
      );
    } else {
      setFeedback(
        "success",
        `Dry-Run Preview Complete (${res.outcome})`,
        `Evaluated rules in dry-run mode (matched=${
          (res.matched_rule_ids || []).length
        }, applied=${
          (res.applied_rule_ids || []).length
        }). No traffic was sent and capture count did not change.`
      );
    }
  } catch (err) {
    previewErrorBox.hidden = false;
    previewErrorBox.dataset.errorCode = err.code || "preview_error";
    previewErrorCode.textContent = err.code || "preview_error";
    previewErrorMessage.textContent = err.message;
    showRuleFormError(err.code || "preview_error", err.message);
  } finally {
    runRulePreviewBtn.disabled = false;
    endOp();
  }
}

// Capture Detail Rule Evaluation Rendering & Navigation (AC-11)
function createRuleNavigationChip(ruleId, badgeClass = "badge-outline") {
  const wrap = document.createElement("span");
  wrap.className = "draft-chip";
  wrap.dataset.ruleId = ruleId;

  const code = document.createElement("code");
  code.className = badgeClass;
  code.textContent = ruleId;
  wrap.appendChild(code);

  const openRuleBtn = document.createElement("button");
  openRuleBtn.type = "button";
  openRuleBtn.className = "btn btn-sm btn-secondary btn-open-rule-from-detail";
  openRuleBtn.dataset.ruleId = ruleId;
  openRuleBtn.textContent = "Open in Rules";
  openRuleBtn.addEventListener("click", () => navigateToRuleFromCapture(ruleId));
  wrap.appendChild(openRuleBtn);

  const filterBtn = document.createElement("button");
  filterBtn.type = "button";
  filterBtn.className = "btn btn-sm btn-secondary btn-filter-captures-by-rule";
  filterBtn.dataset.ruleId = ruleId;
  filterBtn.textContent = "Filter Requests";
  filterBtn.addEventListener("click", () => filterCapturesByRuleId(ruleId));
  wrap.appendChild(filterBtn);

  return wrap;
}

function renderRuleChipList(container, ruleIds, badgeClass) {
  if (!container) {
    return;
  }
  container.textContent = "";
  const ids = Array.isArray(ruleIds) ? ruleIds : [];
  if (ids.length === 0) {
    container.textContent = "None";
    return;
  }
  for (const rid of ids) {
    container.appendChild(createRuleNavigationChip(rid, badgeClass));
  }
}

function renderCaptureDetailRuleEvaluation(detail) {
  if (!detailRuleEvaluationBox) {
    return;
  }
  const ev = detail.rule_evaluation || {
    evaluated: false,
    outcome: "no_match",
    matched_rule_ids: [],
    applied_rule_ids: [],
    skipped_rule_ids: [],
    failed_rule_ids: [],
    modified: false,
    failed: false,
    rule_results: [],
    change_summary: [],
  };
  const outcome = ev.outcome || "no_match";
  detailRuleEvaluationBox.dataset.outcome = outcome;
  detailRuleOutcomeBadge.className = `badge ${getOutcomeBadgeClass(outcome)}`;
  detailRuleOutcomeBadge.textContent = outcome;
  detailRuleOutcomeText.textContent = outcome;
  detailRuleModifiedText.textContent = `modified=${Boolean(
    ev.modified
  )}, failed=${Boolean(ev.failed)}`;
  const changes = ev.change_summary || [];
  detailRuleChangeCount.textContent = String(changes.length);

  if (detailPreviewRulesBtn) {
    detailPreviewRulesBtn.dataset.captureId = detail.capture_id;
  }

  renderRuleChipList(detailMatchedRulesList, ev.matched_rule_ids, "badge-outline");
  renderRuleChipList(detailAppliedRulesList, ev.applied_rule_ids, "badge-success");
  renderRuleChipList(detailSkippedRulesList, ev.skipped_rule_ids, "badge-warn");
  renderRuleChipList(detailFailedRulesList, ev.failed_rule_ids, "badge-danger");

  const rResults = ev.rule_results || ev.rules || [];
  if (rResults.length > 0) {
    detailRuleResultsWrap.hidden = false;
    detailRuleResultsTbody.textContent = "";
    for (const rr of rResults) {
      const tr = document.createElement("tr");
      tr.dataset.ruleId = rr.rule_id;
      tr.dataset.outcome = rr.outcome;

      const tdId = document.createElement("td");
      const code = document.createElement("code");
      code.textContent = `${rr.rule_id}${rr.name ? ` (${rr.name})` : ""}`;
      tdId.appendChild(code);
      tr.appendChild(tdId);

      const tdPrio = document.createElement("td");
      tdPrio.textContent = String(rr.priority ?? "-");
      tr.appendChild(tdPrio);

      const tdOut = document.createElement("td");
      const b = document.createElement("span");
      b.className = `badge ${getOutcomeBadgeClass(rr.outcome)}`;
      b.textContent = rr.outcome;
      tdOut.appendChild(b);
      tr.appendChild(tdOut);

      const tdReason = document.createElement("td");
      const rCode = document.createElement("code");
      const actNotes = [
        ...((rr.changes || []).map((c) => `${c.target}_${c.op}(${c.field})`)),
        ...((rr.skipped_actions || []).map((sa) => `${sa.target}_${sa.op}(${sa.field}):skipped(${sa.reason})`)),
        ...((rr.errors || []).map((er) => `${er.code || er.reason}:${er.message || ""}`)),
        ...((rr.actions || []).map((a) => `${a.type}:${a.status}${a.reason ? `(${a.reason})` : ""}`)),
      ].join(", ");
      rCode.textContent = [rr.reason, actNotes].filter(Boolean).join(" | ") || "-";
      tdReason.appendChild(rCode);
      tr.appendChild(tdReason);

      const tdNav = document.createElement("td");
      const navBtn = document.createElement("button");
      navBtn.type = "button";
      navBtn.className = "btn btn-sm btn-secondary btn-open-rule-from-detail";
      navBtn.dataset.ruleId = rr.rule_id;
      navBtn.textContent = "Inspect Rule";
      navBtn.addEventListener("click", () => navigateToRuleFromCapture(rr.rule_id));
      tdNav.appendChild(navBtn);
      tr.appendChild(tdNav);

      detailRuleResultsTbody.appendChild(tr);
    }
  } else {
    detailRuleResultsWrap.hidden = true;
    detailRuleResultsTbody.textContent = "";
  }

  if (changes.length > 0) {
    detailRuleChangesWrap.hidden = false;
    detailRuleChangesTbody.textContent = "";
    for (const ch of changes) {
      const tr = document.createElement("tr");
      const tdR = document.createElement("td");
      tdR.textContent = ch.rule_id || "-";
      tr.appendChild(tdR);

      const tdT = document.createElement("td");
      tdT.textContent = ch.type || (ch.target && ch.op ? `${ch.target}_${ch.op}` : "-");
      tr.appendChild(tdT);

      const tdK = document.createElement("td");
      tdK.textContent = ch.name ?? ch.key ?? ch.path ?? ch.field ?? "-";
      tr.appendChild(tdK);

      const tdS = document.createElement("td");
      const stVal = ch.status || "applied";
      tdS.textContent = ch.reason ? `${stVal} (${ch.reason})` : stVal;
      tr.appendChild(tdS);

      const tdD = document.createElement("td");
      const dCode = document.createElement("code");
      dCode.textContent =
        ch.before !== undefined || ch.after !== undefined
          ? `${JSON.stringify(ch.before)} → ${JSON.stringify(ch.after)}`
          : JSON.stringify(ch.value ?? "(removed)");
      tdD.appendChild(dCode);
      tr.appendChild(tdD);

      detailRuleChangesTbody.appendChild(tr);
    }
  } else {
    detailRuleChangesWrap.hidden = true;
    detailRuleChangesTbody.textContent = "";
  }
}

async function navigateToRuleFromCapture(ruleId) {
  setWorkspaceView("rules");
  const existing = (appState.rules || []).find((r) => r.rule_id === ruleId);
  if (existing) {
    requestRuleFormResetOrSwitch(() => populateRuleFormForEdit(existing));
    return;
  }
  beginOp();
  try {
    const rule = await apiRequest(`/api/v1/rules/${encodeURIComponent(ruleId)}`);
    requestRuleFormResetOrSwitch(() => populateRuleFormForEdit(rule));
  } catch (err) {
    setFeedback(
      "error",
      `Rule Not Found (${err.code})`,
      `Rule '${ruleId}' may have been deleted: ${err.message}`,
      { errorCode: err.code }
    );
  } finally {
    endOp();
  }
}

function filterCapturesByRuleId(ruleId) {
  setWorkspaceView("requests");
  if (filterRuleIdInput) {
    filterRuleIdInput.value = ruleId;
  }
  loadCapturesList({ resetOffset: true });
}

// DEV-041 Event Listeners
if (refreshRulesBtn) {
  refreshRulesBtn.addEventListener("click", () => {
    loadRulesList({ preserveBanner: false });
  });
}

if (retryRulesLoadBtn) {
  retryRulesLoadBtn.addEventListener("click", () => {
    loadRulesList({ preserveBanner: false });
  });
}

if (rulesFilterEnabledSelect) {
  rulesFilterEnabledSelect.addEventListener("change", () => {
    loadRulesList({ preserveBanner: false });
  });
}

if (newRuleBtn) {
  newRuleBtn.addEventListener("click", () => {
    requestRuleFormResetOrSwitch(() => {
      resetRuleForm();
      ruleNameInput.focus();
      setFeedback("idle", "Create Rule Mode", "Reset form to create a new modification rule.");
    });
  });
}

if (ruleCancelEditBtn) {
  ruleCancelEditBtn.addEventListener("click", () => {
    requestRuleFormResetOrSwitch(() => {
      resetRuleForm();
      setFeedback("idle", "Rule Form Reset", "Cleared unsaved edits and reset rule form.");
    });
  });
}

if (discardRuleCancelBtn) {
  discardRuleCancelBtn.addEventListener("click", () => {
    appState.pendingRuleDiscardCallback = null;
    discardRuleEditsDialog.close("cancel");
    setFeedback(
      "idle",
      "Keep Editing",
      "Cancelled form reset; your unsaved rule edits remain intact."
    );
  });
}

if (discardRuleEditsForm) {
  discardRuleEditsForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const cb = appState.pendingRuleDiscardCallback;
    appState.pendingRuleDiscardCallback = null;
    discardRuleEditsDialog.close("discard");
    if (typeof cb === "function") {
      cb();
    }
  });
}

if (discardRuleEditsDialog) {
  discardRuleEditsDialog.addEventListener("close", () => {
    discardRuleEditsDialog.returnValue = "";
  });
}

if (ruleForm) {
  ruleForm.addEventListener("submit", handleRuleFormSubmit);
}

for (const [inputEl, errEl] of [
  [ruleNameInput, ruleNameError],
  [ruleIdInput, ruleIdError],
  [rulePriorityInput, rulePriorityError],
  [ruleMatchPathRegexInput, rulePathRegexError],
]) {
  if (inputEl) {
    inputEl.addEventListener("input", () => {
      clearFieldError(inputEl, errEl);
      setRuleFormDirty(true);
    });
  }
}

for (const el of [
  ruleDescriptionInput,
  ruleEnabledCheckbox,
  ruleMatchRouteIdInput,
  ruleMatchDomainInput,
  ruleMatchMethodInput,
  ruleMatchPathExactInput,
  ruleMatchPathPrefixInput,
  ruleMatchContentTypeInput,
]) {
  if (el) {
    el.addEventListener("input", () => setRuleFormDirty(true));
    el.addEventListener("change", () => setRuleFormDirty(true));
  }
}

if (addRuleQueryMatchBtn) {
  addRuleQueryMatchBtn.addEventListener("click", () => {
    if (ruleQueryMatchRows._addMatch) {
      ruleQueryMatchRows._addMatch("", "", "equals");
    }
  });
}

if (addRuleHeaderMatchBtn) {
  addRuleHeaderMatchBtn.addEventListener("click", () => {
    if (ruleHeaderMatchRows._addMatch) {
      ruleHeaderMatchRows._addMatch("", "", "equals");
    }
  });
}

if (addRuleActionBtn) {
  addRuleActionBtn.addEventListener("click", () => {
    if (ruleActionRows._addAction) {
      ruleActionRows._addAction("set_header", "", "");
    }
  });
}

if (copyRuleCancelBtn) {
  copyRuleCancelBtn.addEventListener("click", () => {
    copyRuleDialog.close("cancel");
  });
}

if (copyRuleForm) {
  copyRuleForm.addEventListener("submit", handleCopyRuleSubmit);
}

if (copyRuleDialog) {
  copyRuleDialog.addEventListener("close", () => {
    copyRuleDialog.returnValue = "";
  });
}

if (deleteRuleCancelBtn) {
  deleteRuleCancelBtn.addEventListener("click", () => {
    deleteRuleDialog.close("cancel");
    setFeedback(
      "idle",
      "Deletion Cancelled",
      `Rule ${appState.deleteTargetRule?.rule_id || ""} was not deleted.`
    );
    appState.deleteTargetRule = null;
  });
}

if (deleteRuleForm) {
  deleteRuleForm.addEventListener("submit", handleDeleteRuleSubmit);
}

if (deleteRuleDialog) {
  deleteRuleDialog.addEventListener("close", () => {
    deleteRuleDialog.returnValue = "";
  });
}

if (previewSourceInlineRadio && previewSourceCaptureRadio) {
  previewSourceInlineRadio.addEventListener("change", syncPreviewSourceModeUI);
  previewSourceCaptureRadio.addEventListener("change", syncPreviewSourceModeUI);
}

if (previewCaptureSelect) {
  previewCaptureSelect.addEventListener("change", () => {
    if (previewCaptureSelect.value) {
      previewCaptureIdInput.value = previewCaptureSelect.value;
    }
  });
}

if (previewInlineBodyMode) {
  previewInlineBodyMode.addEventListener("change", syncPreviewInlineBodyModeUI);
}

if (addPreviewQueryBtn) {
  addPreviewQueryBtn.addEventListener("click", () => {
    if (previewInlineQueryRows._addPair) {
      previewInlineQueryRows._addPair("", "");
    }
  });
}

if (addPreviewHeaderBtn) {
  addPreviewHeaderBtn.addEventListener("click", () => {
    if (previewInlineHeaderRows._addPair) {
      previewInlineHeaderRows._addPair("", "");
    }
  });
}

if (addPreviewCookieBtn) {
  addPreviewCookieBtn.addEventListener("click", () => {
    if (previewInlineCookieRows._addPair) {
      previewInlineCookieRows._addPair("", "");
    }
  });
}

if (addPreviewFormBtn) {
  addPreviewFormBtn.addEventListener("click", () => {
    if (previewInlineFormRows._addPair) {
      previewInlineFormRows._addPair("", "");
    }
  });
}

if (runRulePreviewBtn) {
  runRulePreviewBtn.addEventListener("click", () => {
    runRulePreview();
  });
}

if (rulePreviewCandidateBtn) {
  rulePreviewCandidateBtn.addEventListener("click", () => {
    previewIncludeCandidateCheckbox.checked = true;
    runRulePreview();
  });
}

if (detailPreviewRulesBtn) {
  detailPreviewRulesBtn.addEventListener("click", () => {
    const cid = detailPreviewRulesBtn.dataset.captureId || appState.selectedCaptureId;
    if (!cid) {
      return;
    }
    setWorkspaceView("rules");
    previewSourceCaptureRadio.checked = true;
    syncPreviewSourceModeUI();
    previewCaptureIdInput.value = cid;
    if (
      Array.from(previewCaptureSelect.options).some((o) => o.value === cid)
    ) {
      previewCaptureSelect.value = cid;
    }
    previewIncludeCandidateCheckbox.checked = false;
    runRulePreview();
  });
}

// Initialize DEV-041 editors & preview defaults
renderEditablePairList(
  previewInlineQueryRows,
  [
    ["tag", "alpha"],
    ["tag", "beta"],
  ],
  "preview-query"
);
renderEditablePairList(
  previewInlineHeaderRows,
  [["X-Client-Env", "local"]],
  "preview-header"
);
renderEditablePairList(
  previewInlineCookieRows,
  [["session_mode", "dev"]],
  "preview-cookie"
);
renderEditablePairList(
  previewInlineFormRows,
  [
    ["field", "one"],
    ["field", "two"],
  ],
  "preview-form"
);
syncPreviewSourceModeUI();
syncPreviewInlineBodyModeUI();
resetRuleForm();

// Initial load
loadAllState({ refreshDiscovery: true });
