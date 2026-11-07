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

// Initial load
loadAllState({ refreshDiscovery: true });
