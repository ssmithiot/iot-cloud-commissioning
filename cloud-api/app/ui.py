from html import escape


APP_SCRIPT = r"""
<script type="module">
  import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";

  let supabaseClient = null;
  let currentGatewayTree = null;
  let currentUser = null;
  let currentPointCandidateDevice = null;

  const statePaths = {
    login: "/login",
    signup: "/signup",
    checkEmail: "/auth/check-email",
    waiting: "/auth/waiting-approval",
    unauthorized: "/auth/unauthorized",
    app: "/app",
    adminUsers: "/admin/users"
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, value, isError = false) {
    const element = byId(id);
    if (!element) {
      return;
    }
    element.textContent = value || "";
    element.className = isError ? "notice error" : "notice";
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[char]);
  }

  async function getConfig() {
    const response = await fetch("/api/auth/public-config");
    if (!response.ok) {
      throw new Error("Could not load public auth configuration.");
    }
    const config = await response.json();
    if (!config.configured) {
      throw new Error("Supabase browser auth is not configured on this deployment.");
    }
    return config;
  }

  async function getSupabase() {
    if (supabaseClient) {
      return supabaseClient;
    }
    const config = await getConfig();
    supabaseClient = createClient(config.supabase_url, config.supabase_anon_key);
    return supabaseClient;
  }

  async function getSession() {
    const client = await getSupabase();
    const { data, error } = await client.auth.getSession();
    if (error) {
      throw error;
    }
    return data.session;
  }

  async function api(path, options = {}) {
    const session = await getSession();
    if (!session) {
      window.location.assign(statePaths.login);
      throw new Error("Login required.");
    }
    const headers = {
      "Authorization": `Bearer ${session.access_token}`,
      "Content-Type": "application/json",
      ...(options.headers || {})
    };
    const response = await fetch(path, { ...options, headers });
    const text = await response.text();
    let body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = { detail: text };
      }
    }
    if (!response.ok) {
      const message = body?.detail || `HTTP ${response.status}`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  async function ensureProfile() {
    try {
      await api("/api/auth/register", { method: "POST" });
    } catch (error) {
      if (error.status !== 403) {
        throw error;
      }
    }
    return api("/api/auth/me");
  }

  function redirectForRole(me, requiredRole) {
    if (me.status === "disabled") {
      window.location.assign(statePaths.unauthorized);
      return false;
    }
    if (me.status !== "active" || me.role === "pending") {
      window.location.assign(statePaths.waiting);
      return false;
    }
    if (requiredRole === "admin" && me.role !== "admin") {
      window.location.assign(statePaths.unauthorized);
      return false;
    }
    if (requiredRole === "operator" && !["admin", "operator"].includes(me.role)) {
      window.location.assign(statePaths.unauthorized);
      return false;
    }
    return true;
  }

  async function logout() {
    const client = await getSupabase();
    await client.auth.signOut();
    window.location.assign(statePaths.login);
  }

  async function initLogin() {
    const form = byId("login-form");
    if (!form) {
      return;
    }
    try {
      await getSupabase();
      setText("status", "");
    } catch (error) {
      setText("status", error.message, true);
      return;
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      setText("status", "Signing in...");
      try {
        const client = await getSupabase();
        const email = byId("email").value.trim().toLowerCase();
        const password = byId("password").value;
        const { data, error } = await client.auth.signInWithPassword({ email, password });
        if (error) {
          throw error;
        }
        if (!data.session) {
          window.location.assign(statePaths.checkEmail);
          return;
        }
        const me = await ensureProfile();
        if (redirectForRole(me, null)) {
          window.location.assign(statePaths.app);
        }
      } catch (error) {
        setText("status", error.message, true);
      }
    });
  }

  async function initSignup() {
    const form = byId("signup-form");
    if (!form) {
      return;
    }
    try {
      await getSupabase();
      setText("status", "");
    } catch (error) {
      setText("status", error.message, true);
      return;
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      setText("status", "Creating account...");
      try {
        const client = await getSupabase();
        const email = byId("email").value.trim().toLowerCase();
        const password = byId("password").value;
        const redirectTo = `${window.location.origin}${statePaths.login}`;
        const { error } = await client.auth.signUp({
          email,
          password,
          options: { emailRedirectTo: redirectTo }
        });
        if (error) {
          throw error;
        }
        window.location.assign(statePaths.checkEmail);
      } catch (error) {
        setText("status", error.message, true);
      }
    });
  }

  async function initProtectedPage(requiredRole) {
    const logoutButton = byId("logout");
    if (logoutButton) {
      logoutButton.addEventListener("click", logout);
    }
    try {
      const session = await getSession();
      if (!session) {
        window.location.assign(statePaths.login);
        return null;
      }
      const me = await ensureProfile();
      if (!redirectForRole(me, requiredRole)) {
        return null;
      }
      const identity = byId("identity");
      if (identity) {
        identity.textContent = `${me.email || "automation"} - ${me.role}`;
      }
      return me;
    } catch (error) {
      setText("status", error.message, true);
      return null;
    }
  }

  async function initDashboard() {
    const me = await initProtectedPage(null);
    if (!me) {
      return;
    }
    try {
      const summary = await api("/api/ui/gateways/summary");
      const gateways = await api("/api/ui/gateways");
      const jobs = await api("/api/edge/jobs?limit=10");
      byId("gateway-count").textContent = `${summary.total} total\n${summary.online} online\n${summary.stale} stale\n${summary.offline} offline`;
      byId("job-count").textContent = String(jobs.length);
      renderGatewayList(gateways);
      byId("job-list").textContent = jobs.map((job) => (
        `${job.job_id} | ${job.gateway_id} | ${job.job_type} | ${job.status}`
      )).join("\n") || "No jobs found.";
      if (me.role === "admin") {
        byId("admin-link").hidden = false;
      }
    } catch (error) {
      setText("status", error.message, true);
    }
  }

  function statusLabel(gateway) {
    if (gateway.heartbeat_age_seconds === null || gateway.heartbeat_age_seconds === undefined) {
      return `${gateway.effective_status} | no heartbeat`;
    }
    return `${gateway.effective_status} | heartbeat ${gateway.heartbeat_age_seconds}s ago`;
  }

  function renderGatewayList(gateways) {
    const table = byId("gateway-list");
    table.textContent = "";
    if (!gateways.length) {
      const row = document.createElement("tr");
      row.innerHTML = `<td colspan="6">No gateways found.</td>`;
      table.appendChild(row);
      return;
    }
    for (const gateway of gateways) {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td><a href="/gateways/${encodeURIComponent(gateway.gateway_id)}">${gateway.gateway_id}</a></td>
        <td>${gateway.site_id}</td>
        <td>${gateway.hostname}</td>
        <td>${statusLabel(gateway)}</td>
        <td>${gateway.bacnet_port}</td>
        <td>${gateway.agent_version || ""}</td>
      `;
      table.appendChild(row);
    }
  }

  function objectFolderLabel(objectType) {
    const labels = {
      "analog-input": "Analog Input Objects",
      "analog-output": "Analog Output Objects",
      "analog-value": "Analog Value Objects",
      "binary-input": "Input Objects",
      "binary-output": "Output Objects",
      "binary-value": "Binary Value Objects",
      "multi-state-input": "Multistate Input Objects",
      "multi-state-output": "Multistate Output Objects",
      "multi-state-value": "Multistate Value Objects",
      "schedule": "Schedule Objects",
      "trend-log": "Trend Log Objects",
      "calendar": "Calendar Objects",
      "event-enrollment": "Event Enrollment Objects",
      "file": "File Objects",
      "loop": "Loop Objects",
      "notification-class": "Notification Class Objects",
      "program": "Program Objects",
      "command": "Command Objects"
    };
    return labels[objectType] || `${String(objectType || "unknown").replaceAll("-", " ")} objects`;
  }

  function treeRow(kind, label, meta = "", depth = 0, expanded = true) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "tree-row";
    row.dataset.kind = kind;
    row.style.setProperty("--depth", String(depth));
    row.innerHTML = `
      <span class="twisty">${expanded ? "[-]" : "[+]"}</span>
      <span class="node-icon">${kind === "point" ? "->" : kind === "device" ? "[D]" : "[F]"}</span>
      <span class="node-label">${escapeHtml(label)}</span>
      <span class="node-meta">${escapeHtml(meta)}</span>
    `;
    return row;
  }

  function leafRow(kind, label, meta = "", depth = 0) {
    const row = treeRow(kind, label, meta, depth, false);
    row.querySelector(".twisty").textContent = "";
    return row;
  }

  function canEditTree() {
    return currentUser && ["admin", "operator"].includes(currentUser.role);
  }

  async function removeTreeItem(kind, id, label) {
    if (!canEditTree()) {
      setText("status", "Your role is read-only.", true);
      return;
    }
    const endpoint = kind === "device" ? `/api/ui/devices/${encodeURIComponent(id)}` : `/api/ui/points/${encodeURIComponent(id)}`;
    await api(endpoint, { method: "DELETE" });
    setText("status", `Removed ${label} from the saved tree.`);
    await loadGatewayWorkspace();
  }

  function setTreeDetails(title, details, action = null) {
    const panel = byId("tree-details");
    if (!panel) {
      return;
    }
    const actions = Array.isArray(action) ? action : (action ? [action] : []);
    panel.hidden = false;
    panel.innerHTML = `
      <h2>${escapeHtml(title)}</h2>
      <dl>
        ${Object.entries(details).map(([key, value]) => `
          <dt>${escapeHtml(key)}</dt>
          <dd>${escapeHtml(value ?? "")}</dd>
        `).join("")}
      </dl>
      ${actions.length && canEditTree() ? `<div class="button-row">${actions.map((item, index) => (
        `<button class="secondary" type="button" data-tree-action="${index}">${escapeHtml(item.label)}</button>`
      )).join("")}</div>` : ""}
    `;
    if (actions.length && canEditTree()) {
      panel.querySelectorAll("[data-tree-action]").forEach((button) => {
        button.addEventListener("click", () => actions[Number(button.dataset.treeAction)].handler());
      });
    }
  }

  async function saveLoadedPoints(device, points) {
    let saved = 0;
    let duplicates = 0;
    for (const point of points) {
      try {
        await api(`/api/ui/devices/${encodeURIComponent(device.id)}/points`, {
          method: "POST",
          body: JSON.stringify({
            object_type: point.object_type,
            object_instance: point.object_instance,
            object_name: point.object_name || null,
            property: point.property_name || point.property || "present-value",
            present_value: point.present_value == null ? null : String(point.present_value),
            units: point.units || null,
            writable: point.writable ?? null
          })
        });
        saved += 1;
      } catch (error) {
        if (error.message.includes("already exists")) {
          duplicates += 1;
          continue;
        }
        throw error;
      }
    }
    return { saved, duplicates };
  }

  function renderPointCandidates(device, points, job) {
    const panel = byId("point-candidates-panel");
    const body = byId("point-candidates");
    const count = byId("point-candidates-count");
    if (!panel || !body || !count) {
      return;
    }
    panel.hidden = false;
    body.textContent = "";
    count.textContent = points.length
      ? `${points.length} point candidate(s) loaded from ${job.job_id}. Select the points to save.`
      : `No point candidates were returned by ${job.job_id}.`;

    const sortedPoints = [...points].sort((left, right) => (
      String(left.object_type || "").localeCompare(String(right.object_type || ""))
      || Number(left.object_instance || 0) - Number(right.object_instance || 0)
    ));

    for (const point of sortedPoints) {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td><input type="checkbox" data-role="point-candidate" checked></td>
        <td>${escapeHtml(objectFolderLabel(point.object_type))}</td>
        <td>${escapeHtml(point.object_type)}</td>
        <td>${escapeHtml(point.object_instance)}</td>
        <td>${escapeHtml(point.object_name || "")}</td>
        <td>${escapeHtml(point.property_name || point.property || "present-value")}</td>
      `;
      row.querySelector('[data-role="point-candidate"]').dataset.point = JSON.stringify(point);
      body.appendChild(row);
    }
  }

  function selectedPointCandidates() {
    return [...document.querySelectorAll('[data-role="point-candidate"]:checked')].map((checkbox) => (
      JSON.parse(checkbox.dataset.point || "{}")
    ));
  }

  async function loadPointsForDevice(device) {
    if (!canEditTree()) {
      setText("status", "Your role is read-only.", true);
      return;
    }
    currentPointCandidateDevice = null;
    setDiscoveryProgress(5, `Queueing point load for device ${device.device_instance}...`);
    const job = await api(`/api/ui/devices/${encodeURIComponent(device.id)}/load-points`, { method: "POST" });
    setDiscoveryProgress(25, `Queued ${job.job_id} on BACnet 47814.`);
    const completedJob = await pollDiscoveryJob(job.job_id, "Point load");
    if (completedJob.status !== "completed") {
      setText("status", `Point load ${completedJob.status}: ${completedJob.job_id}.`, true);
      return;
    }
    const points = completedJob.result_json?.points || [];
    currentPointCandidateDevice = device;
    renderPointCandidates(device, points, completedJob);
    setText("status", `Loaded ${points.length} point candidate(s). Select the ones to save.`);
  }

  function addCollapsible(parent, row, children, onSelect = null) {
    parent.appendChild(row);
    const childWrap = document.createElement("div");
    childWrap.className = "tree-children";
    for (const child of children) {
      childWrap.appendChild(child);
    }
    parent.appendChild(childWrap);
    row.addEventListener("click", () => {
      const hidden = childWrap.hidden;
      childWrap.hidden = !hidden;
      row.querySelector(".twisty").textContent = hidden ? "[-]" : "[+]";
      if (onSelect) {
        onSelect();
      }
    });
  }

  function renderTree(tree) {
    currentGatewayTree = tree;
    const target = byId("tree");
    target.textContent = "";
    if (!tree.groups.length && !tree.devices.length) {
      target.textContent = "No saved devices or points yet.";
      return;
    }
    const groupNames = new Map(tree.groups.map((group) => [group.id, group.name]));
    const root = document.createElement("div");
    root.className = "tree-view";

    function deviceNode(device, depth) {
      const points = tree.points.filter((item) => item.saved_device_id === device.id);
      const pointGroups = new Map();
      for (const point of points) {
        const label = objectFolderLabel(point.object_type);
        pointGroups.set(label, [...(pointGroups.get(label) || []), point]);
      }
      const deviceLabel = `[${device.device_instance}] ${device.device_name || "Device " + device.device_instance}`;
      const row = treeRow("device", deviceLabel, device.network_number ? `network ${device.network_number}` : "", depth);
      const showDeviceDetails = () => setTreeDetails(deviceLabel, {
        gateway_id: device.gateway_id,
        device_instance: device.device_instance,
        network_number: device.network_number,
        mac_address: device.mac_address,
        vendor_name: device.vendor_name,
        latest_discovered_at: device.latest_discovered_at,
        points: points.length
      }, [
        {
          label: "Load points",
          handler: () => loadPointsForDevice(device)
        },
        {
          label: "Remove device",
          handler: () => removeTreeItem("device", device.id, deviceLabel)
        }
      ]);
      const container = document.createElement("div");
      addCollapsible(container, row, [], showDeviceDetails);
      for (const [folderLabel, folderPoints] of pointGroups.entries()) {
        const pointRows = folderPoints.map((point) => {
          const pointLabel = `[${point.object_type} ${point.object_instance}] ${point.object_name || "unnamed"}`;
          const pointRow = leafRow("point", pointLabel, point.present_value ?? "", depth + 2);
          pointRow.addEventListener("click", () => setTreeDetails(pointLabel, {
            property: point.property,
            value: point.present_value,
            units: point.units,
            writable: point.writable,
            latest_read_at: point.latest_read_at
          }, {
            label: "Remove point",
            handler: () => removeTreeItem("point", point.id, pointLabel)
          }));
          return pointRow;
        });
        addCollapsible(container.querySelector(".tree-children"), treeRow("folder", folderLabel, `${folderPoints.length}`, depth + 1), pointRows);
      }
      if (!points.length) {
        container.querySelector(".tree-children").appendChild(leafRow("empty", "No saved points", "load points to select", depth + 1));
      }
      return container;
    }

    for (const group of tree.groups) {
      const groupedDevices = tree.devices.filter((device) => device.group_id === group.id);
      const children = groupedDevices.length
        ? groupedDevices.map((device) => deviceNode(device, 1))
        : [leafRow("empty", "No devices saved", "", 1)];
      const row = treeRow("folder", group.name, `${groupedDevices.length}`, 0);
      const showGroupDetails = () => setTreeDetails(group.name, {
        gateway_id: group.gateway_id,
        devices: groupedDevices.length
      });
      addCollapsible(root, row, children, showGroupDetails);
    }
    const ungroupedDevices = tree.devices.filter((item) => !item.group_id || !groupNames.has(item.group_id));
    if (ungroupedDevices.length) {
      addCollapsible(root, treeRow("folder", "Ungrouped", `${ungroupedDevices.length}`, 0), ungroupedDevices.map((device) => deviceNode(device, 1)));
    }
    target.appendChild(root);
  }

  function groupOptions() {
    const groups = currentGatewayTree?.groups || [];
    if (!groups.length) {
      return `<option value="">Ungrouped</option>`;
    }
    return `<option value="">Ungrouped</option>${groups.map((group) => (
      `<option value="${escapeHtml(group.id)}">${escapeHtml(group.name)}</option>`
    )).join("")}`;
  }

  function renderDiscoveredDevices(job) {
    const panel = byId("discovered-devices-panel");
    const body = byId("discovered-devices");
    const count = byId("discovered-devices-count");
    if (!panel || !body || !count) {
      return;
    }
    const devices = job?.result_json?.devices || [];
    panel.hidden = false;
    count.textContent = devices.length ? `${devices.length} device(s) discovered.` : "No devices discovered.";
    body.textContent = "";
    for (const device of devices) {
      const deviceInstance = device.device_id;
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${escapeHtml(deviceInstance)}</td>
        <td>${escapeHtml(device.network)}</td>
        <td>${escapeHtml(device.mac)}</td>
        <td>${escapeHtml(device.sadr)}</td>
        <td>${escapeHtml(device.apdu)}</td>
        <td>
          <select data-role="device-group">
            ${groupOptions()}
          </select>
        </td>
        <td>
          <button type="button" data-role="save-device">Save</button>
        </td>
        <td>
          <button class="secondary" type="button" data-role="load-points">Load points</button>
        </td>
      `;
      row.querySelector('[data-role="save-device"]').addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        const groupId = row.querySelector('[data-role="device-group"]').value || null;
        try {
          await api(`/api/ui/gateways/${encodeURIComponent(job.gateway_id)}/devices`, {
            method: "POST",
            body: JSON.stringify({
              group_id: groupId,
              device_instance: deviceInstance,
              device_name: `Device ${deviceInstance}`,
              network_number: device.network ?? null,
              mac_address: device.sadr ? `${device.mac || ""} sadr ${device.sadr}`.trim() : (device.mac || null)
            })
          });
          setText("status", `Saved device ${deviceInstance}.`);
          await loadGatewayWorkspace();
        } catch (error) {
          setText("status", error.message, true);
        } finally {
          button.disabled = false;
        }
      });
      row.querySelector('[data-role="load-points"]').addEventListener("click", () => {
        setText("status", `Save device ${deviceInstance}, then use Load points from the saved tree.`);
      });
      body.appendChild(row);
    }
  }

  function setDiscoveryProgress(percent, label, isError = false) {
    const panel = byId("discovery-progress-panel");
    const bar = byId("discovery-progress");
    const labelEl = byId("discovery-progress-label");
    if (!panel || !bar || !labelEl) {
      return;
    }
    panel.hidden = false;
    bar.value = percent;
    labelEl.textContent = label;
    labelEl.className = isError ? "notice error" : "notice";
  }

  function progressForJob(job) {
    if (!job) {
      return { percent: 10, label: "Waiting for job record..." };
    }
    if (job.status === "queued") {
      return { percent: 25, label: `Queued ${job.job_id}` };
    }
    if (job.status === "claimed") {
      return { percent: 65, label: `Running ${job.job_id}` };
    }
    if (job.status === "completed") {
      return { percent: 100, label: `Completed ${job.job_id}` };
    }
    if (job.status === "deferred") {
      return { percent: 100, label: `Deferred ${job.job_id}: ${job.error_message || "gateway busy"}`, isError: true };
    }
    if (job.status === "failed") {
      return { percent: 100, label: `Failed ${job.job_id}: ${job.error_message || "unknown error"}`, isError: true };
    }
    return { percent: 40, label: `${job.status} ${job.job_id}` };
  }

  async function pollDiscoveryJob(jobId, label = "Discovery") {
    const startedAt = Date.now();
    while (Date.now() - startedAt < 300000) {
      const jobs = await api("/api/edge/jobs?limit=50");
      const job = jobs.find((item) => item.job_id === jobId);
      const progress = progressForJob(job);
      setDiscoveryProgress(progress.percent, progress.label, Boolean(progress.isError));
      if (job && ["completed", "failed", "deferred"].includes(job.status)) {
        return job;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    setDiscoveryProgress(100, `Timed out waiting for ${jobId}`, true);
    throw new Error(`${label} job did not finish before timeout.`);
  }

  async function loadGatewayWorkspace() {
    const gatewayId = document.body.dataset.gatewayId;
    const gateway = await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}`);
    const tree = await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/tree`);
    byId("gateway-title").textContent = `${gateway.gateway_id} Workspace`;
    byId("gateway-status").textContent = `${statusLabel(gateway)} | BACnet ${gateway.bacnet_port} | ${gateway.lan_ip || "no LAN IP"}`;
    const details = byId("gateway-details");
    if (details) {
      details.textContent = JSON.stringify({
      site_id: gateway.site_id,
      hostname: gateway.hostname,
      latest_status: gateway.latest_status,
      latest_heartbeat_at: gateway.latest_heartbeat_at,
      agent_version: gateway.agent_version,
      ui_version: gateway.ui_version
      }, null, 2);
    }
    renderTree(tree);
  }

  async function initGatewayWorkspace() {
    const me = await initProtectedPage(null);
    if (!me) {
      return;
    }
    currentUser = me;
    const groupForm = byId("group-form");
    const discoverButton = byId("discover-devices");
    const saveSelectedPointsButton = byId("save-selected-points");
    const selectAllPointsButton = byId("select-all-point-candidates");
    const deselectAllPointsButton = byId("deselect-all-point-candidates");
    const gatewayId = document.body.dataset.gatewayId;
    const technicalSection = byId("technical-section");
    if (technicalSection && me.role === "admin") {
      technicalSection.hidden = false;
    }
    groupForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/groups`, {
          method: "POST",
          body: JSON.stringify({ name: byId("group-name").value.trim() })
        });
        byId("group-name").value = "";
        setText("status", "Group saved.");
        await loadGatewayWorkspace();
      } catch (error) {
        setText("status", error.message, true);
      }
    });
    discoverButton.addEventListener("click", async () => {
      discoverButton.disabled = true;
      setDiscoveryProgress(5, "Queueing discovery job...");
      try {
        const job = await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/discover-devices`, { method: "POST" });
        setDiscoveryProgress(25, `Queued ${job.job_id} on BACnet 47814.`);
        const completedJob = await pollDiscoveryJob(job.job_id);
        if (completedJob.status === "completed") {
          renderDiscoveredDevices(completedJob);
          setText("status", `Discovery completed: ${completedJob.job_id}.`);
        } else {
          setText("status", `Discovery ${completedJob.status}: ${completedJob.job_id}.`, true);
        }
      } catch (error) {
        setText("status", error.message, true);
      } finally {
        discoverButton.disabled = false;
      }
    });
    selectAllPointsButton.addEventListener("click", () => {
      document.querySelectorAll('[data-role="point-candidate"]').forEach((checkbox) => {
        checkbox.checked = true;
      });
    });
    deselectAllPointsButton.addEventListener("click", () => {
      document.querySelectorAll('[data-role="point-candidate"]').forEach((checkbox) => {
        checkbox.checked = false;
      });
    });
    saveSelectedPointsButton.addEventListener("click", async () => {
      if (!currentPointCandidateDevice) {
        setText("status", "Load points for a saved device first.", true);
        return;
      }
      const selected = selectedPointCandidates();
      if (!selected.length) {
        setText("status", "Select at least one point to save.", true);
        return;
      }
      saveSelectedPointsButton.disabled = true;
      try {
        const counts = await saveLoadedPoints(currentPointCandidateDevice, selected);
        currentPointCandidateDevice = null;
        byId("point-candidates-panel").hidden = true;
        await loadGatewayWorkspace();
        setText("status", `Saved ${counts.saved} selected point(s), skipped ${counts.duplicates} duplicate(s).`);
      } catch (error) {
        setText("status", error.message, true);
      } finally {
        saveSelectedPointsButton.disabled = false;
      }
    });
    try {
      await loadGatewayWorkspace();
    } catch (error) {
      setText("status", error.message, true);
    }
  }

  function renderUsers(users) {
    const usersEl = byId("users");
    usersEl.textContent = "";
    for (const user of users) {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${user.email}</td>
        <td>${user.display_name || ""}</td>
        <td>${user.role}</td>
        <td>${user.status}</td>
        <td>${user.last_login_at || ""}</td>
        <td><button class="secondary" type="button">Edit</button></td>
      `;
      row.querySelector("button").addEventListener("click", () => {
        byId("email").value = user.email;
        byId("display-name").value = user.display_name || "";
        byId("role").value = user.role;
        byId("user-status").value = user.status;
      });
      usersEl.appendChild(row);
    }
  }

  async function loadUsers() {
    const users = await api("/api/admin/users");
    renderUsers(users);
    setText("status", `Loaded ${users.length} user(s).`);
  }

  async function initAdminUsers() {
    const me = await initProtectedPage("admin");
    if (!me) {
      return;
    }
    const form = byId("user-form");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const email = byId("email").value.trim().toLowerCase();
      const userUpdate = {
        email,
        display_name: byId("display-name").value.trim() || null,
        role: byId("role").value,
        status: byId("user-status").value
      };
      try {
        await api(`/api/admin/users/${encodeURIComponent(email)}`, {
          method: "PUT",
          body: JSON.stringify(userUpdate)
        });
        setText("status", `Saved ${email}.`);
        await loadUsers();
      } catch (error) {
        setText("status", error.message, true);
      }
    });
    try {
      await loadUsers();
    } catch (error) {
      setText("status", error.message, true);
    }
  }

  const page = document.body.dataset.page;
  if (page === "login") {
    initLogin();
  } else if (page === "signup") {
    initSignup();
  } else if (page === "app") {
    initDashboard();
  } else if (page === "gateway-workspace") {
    initGatewayWorkspace();
  } else if (page === "admin-users") {
    initAdminUsers();
  } else if (page === "waiting" || page === "unauthorized") {
    const logoutButton = byId("logout");
    if (logoutButton) {
      logoutButton.addEventListener("click", logout);
    }
  }
</script>
"""


def _layout(title: str, body: str, page: str, body_attrs: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --border: #c9d3df;
      --ink: #17202c;
      --muted: #5d6b7c;
      --panel: #f5f7fa;
      --accent: #0f766e;
      --danger: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      color: var(--ink);
      background: #ffffff;
    }}
    header {{
      border-bottom: 1px solid var(--border);
      padding: 18px 24px;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{ font-size: 22px; margin: 0; }}
    h2 {{ font-size: 16px; margin: 0 0 12px; }}
    a {{ color: var(--accent); }}
    label {{
      display: block;
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    input, select {{
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 8px 10px;
      font: inherit;
      background: #ffffff;
    }}
    button, .button {{
      min-height: 38px;
      border: 1px solid var(--accent);
      border-radius: 4px;
      padding: 8px 14px;
      font: inherit;
      font-weight: 700;
      color: #ffffff;
      background: var(--accent);
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }}
    button.secondary, .button.secondary {{
      color: var(--accent);
      background: #ffffff;
    }}
    button:disabled {{
      cursor: wait;
      opacity: 0.65;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 14px;
      align-items: end;
    }}
    .span-2 {{ grid-column: span 2; }}
    .span-3 {{ grid-column: span 3; }}
    .span-4 {{ grid-column: span 4; }}
    .span-6 {{ grid-column: span 6; }}
    .span-12 {{ grid-column: span 12; }}
    section {{
      border-bottom: 1px solid var(--border);
      padding: 20px 0;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }}
    pre {{
      min-height: 80px;
      padding: 12px;
      overflow: auto;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 4px;
      white-space: pre-wrap;
    }}
    .notice {{
      min-height: 24px;
      margin-top: 10px;
      font-size: 14px;
      color: var(--muted);
    }}
    .notice.error {{ color: var(--danger); }}
    progress {{
      width: min(420px, 100%);
      height: 18px;
      accent-color: var(--accent);
    }}
    .progress-panel {{
      margin-top: 14px;
      display: grid;
      gap: 6px;
      align-items: start;
    }}
    .progress-panel[hidden] {{ display: none; }}
    .tree-shell {{
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(260px, 0.8fr);
      gap: 16px;
      align-items: stretch;
      margin-bottom: 16px;
    }}
    .tree-panel, .detail-panel {{
      min-height: 280px;
      border: 1px solid var(--border);
      background: var(--panel);
      border-radius: 4px;
      overflow: auto;
      padding: 10px;
    }}
    .tree-view {{
      display: grid;
      gap: 2px;
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
    }}
    .tree-row {{
      width: 100%;
      min-height: 28px;
      border: 0;
      border-radius: 3px;
      padding: 3px 8px 3px calc(8px + (var(--depth) * 22px));
      color: var(--ink);
      background: transparent;
      display: grid;
      grid-template-columns: 34px 34px minmax(0, 1fr) auto;
      gap: 4px;
      align-items: center;
      text-align: left;
      font: inherit;
    }}
    .tree-row:hover {{
      background: #e8eef5;
    }}
    .tree-row[data-kind="point"] .node-icon {{
      color: var(--accent);
    }}
    .tree-row[data-kind="device"] .node-label {{
      font-weight: 700;
    }}
    .node-label {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .node-meta {{
      color: var(--muted);
      font-size: 12px;
    }}
    .detail-panel[hidden] {{ display: none; }}
    .detail-panel dl {{
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 8px 12px;
      font-size: 13px;
    }}
    .detail-panel dt {{
      color: var(--muted);
      font-weight: 700;
    }}
    .detail-panel dd {{
      margin: 0;
      word-break: break-word;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    @media (max-width: 760px) {{
      main {{ padding: 16px; }}
      header {{ align-items: flex-start; flex-direction: column; }}
      .grid {{ grid-template-columns: 1fr; }}
      .span-2, .span-3, .span-4, .span-6, .span-12 {{ grid-column: span 1; }}
      table {{ display: block; overflow-x: auto; white-space: nowrap; }}
      .tree-shell {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body data-page="{page}" {body_attrs}>
  {body}
  {APP_SCRIPT}
</body>
</html>"""


def login_html() -> str:
    body = """
  <header>
    <h1>IOT Cloud Commissioning</h1>
    <a class="button secondary" href="/signup">Sign up</a>
  </header>
  <main>
    <section>
      <h2>Login</h2>
      <form id="login-form" class="grid">
        <div class="span-4">
          <label for="email">Email</label>
          <input id="email" type="email" autocomplete="username" required>
        </div>
        <div class="span-4">
          <label for="password">Password</label>
          <input id="password" type="password" autocomplete="current-password" required>
        </div>
        <div class="span-2">
          <button type="submit">Login</button>
        </div>
      </form>
      <div id="status" class="notice"></div>
    </section>
  </main>"""
    return _layout("Login - IOT Cloud Commissioning", body, "login")


def signup_html() -> str:
    body = """
  <header>
    <h1>IOT Cloud Commissioning</h1>
    <a class="button secondary" href="/login">Login</a>
  </header>
  <main>
    <section>
      <h2>Sign Up</h2>
      <form id="signup-form" class="grid">
        <div class="span-4">
          <label for="email">Email</label>
          <input id="email" type="email" autocomplete="username" required>
        </div>
        <div class="span-4">
          <label for="password">Password</label>
          <input id="password" type="password" autocomplete="new-password" minlength="8" required>
        </div>
        <div class="span-2">
          <button type="submit">Sign up</button>
        </div>
      </form>
      <div id="status" class="notice"></div>
    </section>
  </main>"""
    return _layout("Sign Up - IOT Cloud Commissioning", body, "signup")


def check_email_html() -> str:
    body = """
  <header><h1>IOT Cloud Commissioning</h1></header>
  <main>
    <section>
      <h2>Check Your Email</h2>
      <p>Supabase sent a confirmation link to your email address. Confirm your email, then return to login.</p>
      <a class="button" href="/login">Back to login</a>
    </section>
  </main>"""
    return _layout("Check Your Email - IOT Cloud Commissioning", body, "message")


def waiting_approval_html() -> str:
    body = """
  <header>
    <h1>IOT Cloud Commissioning</h1>
    <button id="logout" class="secondary" type="button">Logout</button>
  </header>
  <main>
    <section>
      <h2>Waiting For Approval</h2>
      <p>Your email is confirmed, but an admin still needs to assign an app role before you can use commissioning pages.</p>
      <div id="status" class="notice"></div>
    </section>
  </main>"""
    return _layout("Waiting For Approval - IOT Cloud Commissioning", body, "waiting")


def unauthorized_html() -> str:
    body = """
  <header>
    <h1>IOT Cloud Commissioning</h1>
    <button id="logout" class="secondary" type="button">Logout</button>
  </header>
  <main>
    <section>
      <h2>Unauthorized</h2>
      <p>Your current role does not allow this action.</p>
      <a class="button secondary" href="/app">Return to dashboard</a>
      <div id="status" class="notice"></div>
    </section>
  </main>"""
    return _layout("Unauthorized - IOT Cloud Commissioning", body, "unauthorized")


def app_html() -> str:
    body = """
  <header>
    <h1>IOT Cloud Commissioning</h1>
    <div class="toolbar">
      <span id="identity"></span>
      <a id="admin-link" class="button secondary" href="/admin/users" hidden>Users</a>
      <button id="logout" class="secondary" type="button">Logout</button>
    </div>
  </header>
  <main>
    <section>
      <h2>Dashboard</h2>
      <div class="grid">
        <div class="span-3"><label>Gateways</label><pre id="gateway-count">0</pre></div>
        <div class="span-3"><label>Recent jobs</label><pre id="job-count">0</pre></div>
      </div>
      <div id="status" class="notice"></div>
    </section>
    <section>
      <h2>Gateways</h2>
      <table>
        <thead>
          <tr>
            <th>Gateway</th>
            <th>Site</th>
            <th>Hostname</th>
            <th>Status</th>
            <th>BACnet</th>
            <th>Agent</th>
          </tr>
        </thead>
        <tbody id="gateway-list"></tbody>
      </table>
    </section>
    <section>
      <h2>Recent Jobs</h2>
      <pre id="job-list">Loading...</pre>
    </section>
  </main>"""
    return _layout("Dashboard - IOT Cloud Commissioning", body, "app")


def gateway_workspace_html(gateway_id: str) -> str:
    escaped_gateway_id = escape(gateway_id, quote=True)
    body = """
  <header>
    <h1 id="gateway-title">Gateway Workspace</h1>
    <div class="toolbar">
      <span id="identity"></span>
      <a class="button secondary" href="/app">Dashboard</a>
      <button id="logout" class="secondary" type="button">Logout</button>
    </div>
  </header>
  <main>
    <section>
      <h2>Status</h2>
      <pre id="gateway-status">Loading...</pre>
      <div id="status" class="notice"></div>
    </section>
    <section>
      <h2>Saved Groups, Devices, And Points</h2>
      <div class="tree-shell">
        <div id="tree" class="tree-panel">Loading...</div>
        <aside id="tree-details" class="detail-panel" hidden></aside>
      </div>
      <form id="group-form" class="grid">
        <div class="span-4">
          <label for="group-name">Group name</label>
          <input id="group-name" type="text" maxlength="120" required>
        </div>
        <div class="span-2">
          <button type="submit">Add group</button>
        </div>
      </form>
    </section>
    <section>
      <h2>Discovery</h2>
      <div class="toolbar">
        <button id="discover-devices" type="button">Discover devices</button>
      </div>
      <div id="discovery-progress-panel" class="progress-panel" hidden>
        <progress id="discovery-progress" max="100" value="0"></progress>
        <div id="discovery-progress-label" class="notice"></div>
      </div>
      <div id="discovered-devices-panel" hidden>
        <h2>Discovered Devices</h2>
        <div id="discovered-devices-count" class="notice"></div>
        <table>
          <thead>
            <tr>
              <th>Device</th>
              <th>Network</th>
              <th>MAC</th>
              <th>SADR</th>
              <th>APDU</th>
              <th>Group</th>
              <th>Save</th>
              <th>Points</th>
            </tr>
          </thead>
          <tbody id="discovered-devices"></tbody>
        </table>
      </div>
      <div id="point-candidates-panel" hidden>
        <h2>Loaded Point Candidates</h2>
        <div id="point-candidates-count" class="notice"></div>
        <div class="toolbar">
          <button id="select-all-point-candidates" class="secondary" type="button">Select all</button>
          <button id="deselect-all-point-candidates" class="secondary" type="button">Deselect all</button>
          <button id="save-selected-points" type="button">Save selected points</button>
        </div>
        <table>
          <thead>
            <tr>
              <th>Select</th>
              <th>Folder</th>
              <th>Object Type</th>
              <th>Instance</th>
              <th>Object Name</th>
              <th>Property</th>
            </tr>
          </thead>
          <tbody id="point-candidates"></tbody>
        </table>
      </div>
    </section>
    <section id="technical-section" hidden>
      <h2>Technical</h2>
      <pre id="gateway-details">Loading...</pre>
    </section>
  </main>"""
    return _layout(
        "Gateway Workspace - IOT Cloud Commissioning",
        body,
        "gateway-workspace",
        f'data-gateway-id="{escaped_gateway_id}"',
    )


def admin_users_html() -> str:
    body = """
  <header>
    <h1>IOT Cloud Commissioning Admin</h1>
    <div class="toolbar">
      <span id="identity"></span>
      <a class="button secondary" href="/app">Dashboard</a>
      <button id="logout" class="secondary" type="button">Logout</button>
    </div>
  </header>
  <main>
    <section>
      <h2>Assign User</h2>
      <form id="user-form" class="grid">
        <div class="span-3">
          <label for="email">Email</label>
          <input id="email" type="email" required>
        </div>
        <div class="span-3">
          <label for="display-name">Display name</label>
          <input id="display-name" type="text">
        </div>
        <div class="span-2">
          <label for="role">Role</label>
          <select id="role">
            <option value="operator">operator</option>
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
            <option value="pending">pending</option>
          </select>
        </div>
        <div class="span-2">
          <label for="user-status">Status</label>
          <select id="user-status">
            <option value="active">active</option>
            <option value="pending">pending</option>
            <option value="disabled">disabled</option>
          </select>
        </div>
        <div class="span-2">
          <button type="submit">Save user</button>
        </div>
      </form>
      <div id="status" class="notice"></div>
    </section>
    <section>
      <h2>Users</h2>
      <table>
        <thead>
          <tr>
            <th>Email</th>
            <th>Name</th>
            <th>Role</th>
            <th>Status</th>
            <th>Last login</th>
            <th></th>
          </tr>
        </thead>
        <tbody id="users"></tbody>
      </table>
    </section>
  </main>"""
    return _layout("Users - IOT Cloud Commissioning", body, "admin-users")
