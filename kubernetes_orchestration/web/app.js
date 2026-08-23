"use strict";

var state = {
  csrf: "",
  role: "viewer",
  permissions: ["read"],
  mutationsEnabled: false,
  snapshot: null,
  plans: [],
  audit: [],
  agents: [],
  desiredOriginal: "",
  currentPlan: null,
  currentPlanStatus: null,
  busy: false
};

function byId(id) {
  return document.getElementById(id);
}

function element(tag, className, text) {
  var node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

function fetchOptions(method, body) {
  var headers = {"Accept": "application/json"};
  var options = {method: method, headers: headers, credentials: "same-origin"};
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  if (state.csrf && method !== "GET") {
    headers["X-CSRF-Token"] = state.csrf;
  }
  return options;
}

function parseResponse(response) {
  return response.json().then(function parsePayload(payload) {
    if (!response.ok) {
      throw new Error(payload.message || payload.error || "Request failed");
    }
    return payload;
  });
}

function request(path, method, body) {
  return fetch(path, fetchOptions(method || "GET", body)).then(parseResponse);
}

function showToast(message) {
  var toast = byId("toast");
  toast.textContent = message;
  toast.classList.remove("is-hidden");
  window.setTimeout(hideToast, 3600);
}

function hideToast() {
  byId("toast").classList.add("is-hidden");
}

function showError(message) {
  var target = byId("global-error");
  target.textContent = message;
  target.classList.remove("is-hidden");
}

function clearError() {
  byId("global-error").classList.add("is-hidden");
}

function setBusy(value) {
  state.busy = value;
  var buttons = document.querySelectorAll("button");
  var index;
  for (index = 0; index < buttons.length; index += 1) {
    if (buttons[index].id !== "logout-button") {
      buttons[index].disabled = value;
    }
  }
  if (!value) {
    updatePermissionControls();
  }
}

function hasPermission(permission) {
  return state.permissions.indexOf(permission) !== -1;
}

function updatePermissionControls() {
  byId("plan-button").disabled = !hasPermission("plan");
  byId("approve-button").disabled = !hasPermission("approve");
  byId("approve-wave-button").disabled = !hasPermission("approve");
  byId("apply-button").disabled = !hasPermission("execute") || !state.mutationsEnabled;
  byId("pause-button").disabled = !hasPermission("pause") || !state.mutationsEnabled;
  byId("rollback-button").disabled = !hasPermission("execute") || !state.mutationsEnabled;
  byId("evidence-button").disabled = !hasPermission("evidence");
  var auditNavigation = document.querySelector('[data-view="audit"]');
  auditNavigation.classList.toggle("is-hidden", !hasPermission("audit"));
}

function openApp(session) {
  state.csrf = session.csrf_token || "";
  state.role = session.role;
  state.permissions = session.permissions || ["read"];
  state.mutationsEnabled = Boolean(session.mutations_enabled);
  byId("role-badge").textContent = state.role.toUpperCase();
  byId("mutation-badge").textContent = state.mutationsEnabled ? "WRITE ENABLED" : "READ ONLY";
  byId("mutation-badge").classList.toggle("is-write", state.mutationsEnabled);
  byId("login-panel").classList.add("is-hidden");
  byId("app-shell").classList.remove("is-hidden");
  updatePermissionControls();
  refreshAll();
}

function handleSession(payload) {
  openApp(payload);
}

function sessionFailed() {
  byId("login-panel").classList.remove("is-hidden");
  byId("app-shell").classList.add("is-hidden");
}

function checkSession() {
  request("/api/v1/session", "GET").then(handleSession).catch(sessionFailed);
}

function handleLogin(event) {
  event.preventDefault();
  byId("login-error").textContent = "";
  var token = byId("login-token").value;
  request("/api/v1/session", "POST", {token: token}).then(handleSession).catch(handleLoginError);
}

function handleLoginError(error) {
  byId("login-error").textContent = error.message;
}

function handleLogout() {
  request("/api/v1/session", "DELETE").then(completeLogout).catch(showOperationError);
}

function completeLogout() {
  state.csrf = "";
  state.currentPlan = null;
  byId("login-token").value = "";
  sessionFailed();
}

function metricCard(label, value, foot) {
  var card = element("article", "metric-card");
  card.appendChild(element("span", "metric-label", label));
  card.appendChild(element("strong", "metric-value", value));
  card.appendChild(element("div", "metric-foot", foot));
  return card;
}

function renderMetrics() {
  var grid = byId("metric-grid");
  grid.replaceChildren();
  if (!state.snapshot) {
    return;
  }
  var summary = state.snapshot.summary;
  grid.appendChild(metricCard("Targets", summary.targets, "registriert"));
  grid.appendChild(metricCard("Healthy", summary.healthy, "vollständig bereit"));
  grid.appendChild(metricCard("Degraded", summary.degraded, "Aufmerksamkeit"));
  grid.appendChild(metricCard("Unreachable", summary.unreachable, "nicht erreichbar"));
  grid.appendChild(metricCard("Resources", summary.resources, "beobachtete Objekte"));
  grid.appendChild(metricCard("Agents", summary.agents_online || 0, "online"));
}

function targetStatusClass(status) {
  if (status === "HEALTHY") {
    return "status-dot status-ok";
  }
  if (status === "DEGRADED") {
    return "status-dot status-warn";
  }
  return "status-dot status-bad";
}

function renderTopology() {
  var grid = byId("topology-grid");
  grid.replaceChildren();
  if (!state.snapshot || state.snapshot.targets.length === 0) {
    grid.appendChild(element("div", "empty-state", "Keine Targets registriert."));
    return;
  }
  var index;
  for (index = 0; index < state.snapshot.targets.length; index += 1) {
    var target = state.snapshot.targets[index];
    var card = element("article", "target-card");
    var title = element("div", "target-title");
    var titleLeft = element("div", "state-chip");
    titleLeft.appendChild(element("span", targetStatusClass(target.status)));
    titleLeft.appendChild(element("strong", "", target.target_id));
    title.appendChild(titleLeft);
    title.appendChild(element("span", "mini-badge", target.status));
    card.appendChild(title);
    var meta = element("div", "target-meta");
    var cluster = element("div", "", "Cluster");
    cluster.appendChild(element("span", "", target.cluster_id));
    var namespace = element("div", "", "Namespace");
    namespace.appendChild(element("span", "", target.namespace));
    var resources = element("div", "", "Resources");
    resources.appendChild(element("span", "", target.resource_count || 0));
    var unhealthy = element("div", "", "Unhealthy");
    unhealthy.appendChild(element("span", "", target.unhealthy_count || 0));
    meta.appendChild(cluster);
    meta.appendChild(namespace);
    meta.appendChild(resources);
    meta.appendChild(unhealthy);
    if (target.agent_status) {
      var agent = element("div", "", "Agent");
      agent.appendChild(element("span", "", target.agent_status));
      meta.appendChild(agent);
    }
    card.appendChild(meta);
    grid.appendChild(card);
  }
}

function allResources() {
  var rows = [];
  if (!state.snapshot) {
    return rows;
  }
  var targetIndex;
  for (targetIndex = 0; targetIndex < state.snapshot.targets.length; targetIndex += 1) {
    var target = state.snapshot.targets[targetIndex];
    var resourceIndex;
    for (resourceIndex = 0; resourceIndex < target.resources.length; resourceIndex += 1) {
      var resource = target.resources[resourceIndex];
      rows.push({target: target.target_id, resource: resource});
    }
  }
  return rows;
}

function resourceReadyText(resource) {
  if (resource.ready === true) {
    return "Ready";
  }
  if (resource.ready === false) {
    return "Not Ready";
  }
  if (resource.ready_replicas !== undefined && resource.ready_replicas !== null) {
    return String(resource.ready_replicas) + "/" + String(resource.replicas || 0);
  }
  if (resource.phase) {
    return resource.phase;
  }
  return "–";
}

function renderWorkloads() {
  var body = byId("workload-body");
  body.replaceChildren();
  var filter = byId("workload-filter").value.trim().toLowerCase();
  var rows = allResources();
  var index;
  for (index = 0; index < rows.length; index += 1) {
    var rowData = rows[index];
    var resource = rowData.resource;
    var searchable = [resource.name, resource.kind, rowData.target].join(" ").toLowerCase();
    if (filter && searchable.indexOf(filter) === -1) {
      continue;
    }
    var row = element("tr");
    var statusCell = element("td");
    statusCell.appendChild(element("span", resource.ready === false ? "status-dot status-bad" : "status-dot status-ok"));
    row.appendChild(statusCell);
    row.appendChild(element("td", "resource-name", resource.name));
    row.appendChild(element("td", "", resource.kind));
    row.appendChild(element("td", "", rowData.target));
    row.appendChild(element("td", "", resourceReadyText(resource)));
    body.appendChild(row);
  }
  if (body.children.length === 0) {
    var empty = element("tr");
    var cell = element("td", "subtle", "Keine passenden Ressourcen.");
    cell.colSpan = 5;
    empty.appendChild(cell);
    body.appendChild(empty);
  }
}

function timeText(value) {
  if (!value) {
    return "–";
  }
  return new Date(Number(value) * 1000).toLocaleString("de-DE");
}

function shortHash(value) {
  if (!value) {
    return "–";
  }
  return value.slice(0, 18) + "…" + value.slice(-8);
}

function renderPlans() {
  var body = byId("plans-body");
  body.replaceChildren();
  var index;
  for (index = 0; index < state.plans.length; index += 1) {
    var plan = state.plans[index];
    var row = element("tr");
    row.appendChild(element("td", "", plan.status));
    row.appendChild(element("td", "hash", shortHash(plan.plan_id)));
    row.appendChild(element("td", "", plan.principal || "Nicht freigegeben"));
    row.appendChild(element("td", "", timeText(plan.updated_at)));
    var actionCell = element("td");
    var openButton = element("button", "button button-quiet table-button", "Öffnen");
    openButton.type = "button";
    openButton.dataset.planId = plan.plan_id;
    openButton.addEventListener("click", openPlanFromRow);
    actionCell.appendChild(openButton);
    row.appendChild(actionCell);
    body.appendChild(row);
  }
  if (state.plans.length === 0) {
    var empty = element("tr");
    var cell = element("td", "subtle", "Noch keine Pläne journalisiert.");
    cell.colSpan = 5;
    empty.appendChild(cell);
    body.appendChild(empty);
  }
}

function openPlanFromRow(event) {
  var planId = event.currentTarget.dataset.planId;
  request("/api/v1/plans/" + planId, "GET").then(renderCurrentPlanStatus).catch(showRefreshError);
}

function renderAgents(payload) {
  state.agents = payload.agents || [];
  var body = byId("agents-body");
  body.replaceChildren();
  var index;
  for (index = 0; index < state.agents.length; index += 1) {
    var agent = state.agents[index];
    var row = element("tr");
    var status = element("td");
    status.appendChild(element("span", agent.status === "ONLINE" ? "status-dot status-ok" : "status-dot status-bad"));
    status.appendChild(document.createTextNode(" " + agent.status));
    row.appendChild(status);
    row.appendChild(element("td", "resource-name", agent.target_id));
    row.appendChild(element("td", "hash", agent.holder_id));
    row.appendChild(element("td", "", agent.lease_epoch));
    row.appendChild(element("td", "", timeText(agent.expires_at)));
    row.appendChild(element("td", "", (agent.capabilities || []).join(", ")));
    body.appendChild(row);
  }
  if (state.agents.length === 0) {
    var empty = element("tr");
    var cell = element("td", "subtle", "Keine outbound Cluster-Agenten registriert.");
    cell.colSpan = 6;
    empty.appendChild(cell);
    body.appendChild(empty);
  }
}

function auditReference(item) {
  var payload = item.event.payload || {};
  return payload.step_id || payload.plan_id || "–";
}

function renderAudit() {
  var body = byId("audit-body");
  body.replaceChildren();
  var index;
  for (index = 0; index < state.audit.length; index += 1) {
    var item = state.audit[index];
    var row = element("tr");
    row.appendChild(element("td", "", item.sequence));
    row.appendChild(element("td", "resource-name", item.event.event_type));
    row.appendChild(element("td", "hash", shortHash(auditReference(item))));
    row.appendChild(element("td", "hash", shortHash(item.record_hash)));
    row.appendChild(element("td", "", timeText(item.created_at)));
    body.appendChild(row);
  }
}

function renderOverallHealth() {
  if (!state.snapshot) {
    return;
  }
  var pill = byId("overall-health");
  var dot = pill.querySelector(".status-dot");
  var label = pill.querySelector("span:last-child");
  dot.className = "status-dot";
  if (state.snapshot.summary.unreachable > 0) {
    dot.classList.add("status-bad");
    label.textContent = "Nicht erreichbar";
  } else if (state.snapshot.summary.degraded > 0) {
    dot.classList.add("status-warn");
    label.textContent = "Degradiert";
  } else {
    dot.classList.add("status-ok");
    label.textContent = "Healthy";
  }
}

function renderSnapshot(payload) {
  state.snapshot = payload;
  byId("environment-title").textContent = payload.environment_id;
  byId("last-updated").textContent = "Stand " + new Date(payload.observed_at_unix * 1000).toLocaleTimeString("de-DE");
  renderMetrics();
  renderTopology();
  renderWorkloads();
  renderOverallHealth();
}

function renderPlanList(payload) {
  state.plans = payload.plans || [];
  renderPlans();
}

function renderAuditList(payload) {
  state.audit = payload.audit || [];
  renderAudit();
}

function renderDesired(payload) {
  var formatted = JSON.stringify(payload, null, 2);
  state.desiredOriginal = formatted;
  if (!byId("desired-editor").value.trim()) {
    byId("desired-editor").value = formatted;
  }
}

function showRefreshError(error) {
  showError(error.message);
}

function refreshAll() {
  if (byId("app-shell").classList.contains("is-hidden")) {
    return;
  }
  clearError();
  request("/api/v1/snapshot", "GET").then(renderSnapshot).catch(showRefreshError);
  request("/api/v1/plans", "GET").then(renderPlanList).catch(showRefreshError);
  request("/api/v1/agents", "GET").then(renderAgents).catch(showRefreshError);
  if (hasPermission("audit")) {
    request("/api/v1/audit", "GET").then(renderAuditList).catch(showRefreshError);
  }
  if (!state.desiredOriginal) {
    request("/api/v1/desired", "GET").then(renderDesired).catch(showRefreshError);
  }
  if (state.currentPlan) {
    request("/api/v1/plans/" + state.currentPlan.plan_id, "GET")
      .then(renderCurrentPlanStatus).catch(showRefreshError);
  }
}

function renderCurrentPlanStatus(payload) {
  state.currentPlanStatus = payload;
  if (payload.plan) {
    state.currentPlan = payload.plan;
    var summary = {
      plan_id: payload.plan.plan_id,
      steps: payload.plan.steps.length,
      waves: payload.plan.waves || [],
      targets: payload.plan.target_order || [],
      components: payload.plan.component_order || [],
      providers: [payload.plan.provider],
      risk_summary: payload.plan.risk_summary || {},
      placement_decisions: payload.plan.placement_decisions || []
    };
    renderPlanDetails(summary);
    byId("plan-actions").classList.remove("is-hidden");
  }
  byId("plan-status").textContent = payload.status;
  updatePermissionControls();
}

function parseDesiredEditor() {
  try {
    return JSON.parse(byId("desired-editor").value);
  } catch (error) {
    throw new Error("Desired State ist kein gültiges JSON: " + error.message);
  }
}

function listTags(values) {
  var container = element("div", "plan-list");
  var index;
  for (index = 0; index < values.length; index += 1) {
    container.appendChild(element("span", "", values[index]));
  }
  return container;
}

function planKpi(label, value) {
  var item = element("div", "plan-kpi");
  item.appendChild(element("small", "", label));
  item.appendChild(element("strong", "", value));
  return item;
}

function selectedPlacementCount(decisions) {
  var count = 0;
  var index;
  for (index = 0; index < decisions.length; index += 1) {
    if (decisions[index].selected) {
      count += 1;
    }
  }
  return count;
}

function renderPlanDetails(summary) {
  var container = byId("plan-summary");
  container.className = "plan-details";
  container.replaceChildren();
  container.appendChild(element("div", "hash plan-id", summary.plan_id));
  var kpis = element("div", "plan-kpis");
  kpis.appendChild(planKpi("Schritte", summary.steps));
  kpis.appendChild(planKpi("Wellen", summary.waves.length));
  kpis.appendChild(planKpi("Targets", summary.targets.length));
  kpis.appendChild(planKpi("Komponenten", summary.components.length));
  container.appendChild(kpis);
  container.appendChild(element("p", "subtle", "Provider"));
  container.appendChild(listTags(summary.providers || []));
  container.appendChild(element("p", "subtle", "Komponenten"));
  container.appendChild(listTags(summary.components));
  container.appendChild(element("p", "subtle", "Targets"));
  container.appendChild(listTags(summary.targets));
  var decisions = summary.placement_decisions || [];
  container.appendChild(element(
    "p", "subtle",
    "Placement: " + String(selectedPlacementCount(decisions)) + " ausgewählt, "
      + String(Math.max(0, decisions.length - selectedPlacementCount(decisions))) + " ausgeschlossen"
  ));
  var risk = summary.risk_summary || {};
  container.appendChild(element("div", "risk-badge risk-" + (risk.level || "low"),
    "Risiko: " + String(risk.level || "low").toUpperCase()));
}

function renderPlanPreview(payload) {
  state.currentPlan = payload.plan;
  var summary = payload.summary;
  renderPlanDetails(summary);
  byId("plan-status").textContent = "VALIDATED";
  byId("plan-actions").classList.remove("is-hidden");
  setBusy(false);
  showToast("Plan deterministisch erzeugt und journalisiert.");
  refreshAll();
}

function handlePlanError(error) {
  setBusy(false);
  showError(error.message);
}

function createPlan() {
  clearError();
  if (!hasPermission("plan")) {
    showError("Für Planerzeugung ist die Operator-Berechtigung erforderlich.");
    return;
  }
  var desired;
  try {
    desired = parseDesiredEditor();
  } catch (error) {
    showError(error.message);
    return;
  }
  setBusy(true);
  request("/api/v1/plans", "POST", desired).then(renderPlanPreview).catch(handlePlanError);
}

function currentPlanPath(action) {
  if (!state.currentPlan) {
    throw new Error("Zuerst einen Plan erzeugen.");
  }
  return "/api/v1/plans/" + state.currentPlan.plan_id + "/" + action;
}

function approved() {
  byId("plan-status").textContent = "APPROVED";
  setBusy(false);
  showToast("Freigabe ist exakt an die Plan-ID gebunden.");
  refreshAll();
}

function approvePlan() {
  setBusy(true);
  try {
    request(currentPlanPath("approve"), "POST", {}).then(approved).catch(handlePlanError);
  } catch (error) {
    handlePlanError(error);
  }
}

function nextUnapprovedWave() {
  if (!state.currentPlanStatus || !state.currentPlan) {
    throw new Error("Planstatus muss zuerst geladen werden.");
  }
  var approvedWaves = {};
  var approvals = state.currentPlanStatus.wave_approvals || [];
  var index;
  for (index = 0; index < approvals.length; index += 1) {
    approvedWaves[String(approvals[index].wave)] = true;
  }
  var waves = state.currentPlan.waves || [];
  for (index = 0; index < waves.length; index += 1) {
    if (!approvedWaves[String(waves[index].wave)]) {
      return waves[index].wave;
    }
  }
  throw new Error("Alle Rollout-Wellen sind bereits freigegeben.");
}

function approveNextWave() {
  setBusy(true);
  try {
    var wave = nextUnapprovedWave();
    var path = "/api/v1/plans/" + state.currentPlan.plan_id + "/waves/" + String(wave) + "/approve";
    request(path, "POST", {}).then(approved).catch(handlePlanError);
  } catch (error) {
    handlePlanError(error);
  }
}

function rolloutCompleted(payload) {
  byId("plan-status").textContent = payload.status;
  setBusy(false);
  if (payload.status === "QUEUED" || payload.status === "ROLLBACK_QUEUED") {
    showToast("Operation angenommen; der Fortschritt wird live aktualisiert.");
  } else {
    showToast("Operation abgeschlossen: " + payload.status);
  }
  refreshAll();
}

function applyPlan() {
  if (!state.mutationsEnabled) {
    showError("Mutationen sind serverseitig deaktiviert.");
    return;
  }
  setBusy(true);
  try {
    request(currentPlanPath("apply"), "POST", {}).then(rolloutCompleted).catch(handlePlanError);
  } catch (error) {
    handlePlanError(error);
  }
}

function rollbackCurrentPlan() {
  if (!state.mutationsEnabled) {
    showError("Mutationen sind serverseitig deaktiviert.");
    return;
  }
  setBusy(true);
  try {
    request(currentPlanPath("rollback"), "POST", {}).then(rolloutCompleted).catch(handlePlanError);
  } catch (error) {
    handlePlanError(error);
  }
}

function pauseCurrentPlan() {
  if (!state.mutationsEnabled) {
    showError("Mutationen sind serverseitig deaktiviert.");
    return;
  }
  setBusy(true);
  try {
    request(currentPlanPath("pause"), "POST", {}).then(rolloutCompleted).catch(handlePlanError);
  } catch (error) {
    handlePlanError(error);
  }
}

function evidenceDownloaded(payload) {
  var data = JSON.stringify(payload, null, 2) + "\n";
  var blob = new Blob([data], {type: "application/json"});
  var url = URL.createObjectURL(blob);
  var anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "automation-fleet-evidence-" + state.currentPlan.plan_id.slice(7, 19) + ".json";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  setBusy(false);
  showToast("Evidence-Bundle mit Hash-Bindung exportiert.");
}

function exportEvidence() {
  setBusy(true);
  try {
    request(currentPlanPath("evidence"), "GET").then(evidenceDownloaded).catch(handlePlanError);
  } catch (error) {
    handlePlanError(error);
  }
}

function resetDesired() {
  byId("desired-editor").value = state.desiredOriginal;
}

function switchView(event) {
  var target = event.currentTarget;
  var viewName = target.getAttribute("data-view");
  var views = document.querySelectorAll(".view");
  var buttons = document.querySelectorAll(".nav-item");
  var index;
  for (index = 0; index < views.length; index += 1) {
    views[index].classList.toggle("is-hidden", views[index].id !== "view-" + viewName);
  }
  for (index = 0; index < buttons.length; index += 1) {
    buttons[index].classList.toggle("is-active", buttons[index] === target);
  }
}

function showOperationError(error) {
  setBusy(false);
  showError(error.message);
}

function bindNavigation() {
  var buttons = document.querySelectorAll(".nav-item");
  var index;
  for (index = 0; index < buttons.length; index += 1) {
    buttons[index].addEventListener("click", switchView);
  }
}

function bindActions() {
  byId("login-form").addEventListener("submit", handleLogin);
  byId("logout-button").addEventListener("click", handleLogout);
  byId("refresh-button").addEventListener("click", refreshAll);
  byId("workload-filter").addEventListener("input", renderWorkloads);
  byId("plan-button").addEventListener("click", createPlan);
  byId("reset-desired-button").addEventListener("click", resetDesired);
  byId("approve-button").addEventListener("click", approvePlan);
  byId("approve-wave-button").addEventListener("click", approveNextWave);
  byId("apply-button").addEventListener("click", applyPlan);
  byId("pause-button").addEventListener("click", pauseCurrentPlan);
  byId("rollback-button").addEventListener("click", rollbackCurrentPlan);
  byId("evidence-button").addEventListener("click", exportEvidence);
}

function initialize() {
  bindNavigation();
  bindActions();
  checkSession();
  window.setInterval(refreshAll, 10000);
}

document.addEventListener("DOMContentLoaded", initialize);
