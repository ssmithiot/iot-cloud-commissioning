from html import escape


APP_SCRIPT = r"""
<script type="module">
  import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";

  let supabaseClient = null;
  let currentGatewayTree = null;
  let currentUser = null;
  let currentPointCandidateDevice = null;
  let selectedSavedPointIds = new Set();
  let dashboardGateways = [];
  let dashboardJobs = [];
  let selectedDashboardGatewayId = null;
  let dashboardSort = { key: "gateway_id", direction: "desc" };
  let dashboardSearch = "";
  let mapZoom = 1;
  let mapProjection = null;
  const mapSvgFrame = {
    left: 3,
    top: 5,
    width: 94,
    height: 90,
    viewBoxWidth: 960,
    viewBoxHeight: 560
  };
  const themeStorageKey = "iot-cloud-command-theme";

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

  function applyTheme(theme) {
    const resolved = theme === "light" ? "light" : "dark";
    document.body.dataset.theme = resolved;
    const toggle = byId("theme-toggle");
    if (toggle) {
      toggle.textContent = resolved === "dark" ? "Light Mode" : "Dark Mode";
      toggle.setAttribute("aria-pressed", resolved === "light" ? "true" : "false");
    }
    try {
      window.localStorage.setItem(themeStorageKey, resolved);
    } catch {
      // Ignore storage failures; the current page still switches themes.
    }
  }

  function initThemeToggle() {
    let storedTheme = "dark";
    try {
      storedTheme = window.localStorage.getItem(themeStorageKey) || "dark";
    } catch {
      storedTheme = "dark";
    }
    applyTheme(storedTheme);
    const toggle = byId("theme-toggle");
    if (toggle) {
      toggle.addEventListener("click", () => {
        applyTheme(document.body.dataset.theme === "light" ? "dark" : "light");
      });
    }
  }

  function setText(id, value, isError = false) {
    const element = byId(id);
    if (!element) {
      return;
    }
    element.textContent = value || "";
    element.className = isError ? "notice error" : "notice";
  }

  function renderImportResult(result) {
    const panel = byId("import-result");
    if (!panel) {
      return;
    }
    const createdTotal = Number(result.created_devices || 0) + Number(result.created_points || 0) + Number(result.created_groups || 0);
    const updatedTotal = Number(result.updated_devices || 0) + Number(result.updated_points || 0) + Number(result.updated_groups || 0);
    const action = createdTotal ? "created" : updatedTotal ? "updated" : "validated";
    panel.hidden = false;
    panel.innerHTML = `
      <h3>Last Import</h3>
      <p>Template ${action} the cloud commissioning model.</p>
      <dl>
        <dt>Groups</dt><dd>${escapeHtml(result.created_groups || 0)} created, ${escapeHtml(result.updated_groups || 0)} updated</dd>
        <dt>Devices</dt><dd>${escapeHtml(result.created_devices || 0)} created, ${escapeHtml(result.updated_devices || 0)} updated</dd>
        <dt>Points</dt><dd>${escapeHtml(result.created_points || 0)} created, ${escapeHtml(result.updated_points || 0)} updated</dd>
      </dl>
    `;
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

  function metricCard(id, label, value, detail = "") {
    const element = byId(id);
    if (!element) {
      return;
    }
    element.innerHTML = `
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <em>${escapeHtml(detail)}</em>
    `;
  }

  function gatewayAddress(gateway) {
    return gateway.site_compact_address || gateway.site_address || [
      gateway.site_address_city,
      gateway.site_address_state,
      gateway.site_address_postal_code
    ].filter(Boolean).join(", ");
  }

  function stateSeed(gateway) {
    const explicit = (gateway.site_address_state || "").trim().toUpperCase();
    if (explicit) {
      return explicit;
    }
    const address = `${gateway.site_compact_address || ""} ${gateway.site_address || ""}`.toUpperCase();
    const states = ["AL","AZ","AR","CA","CO","CT","FL","GA","IA","ID","IL","IN","KS","KY","LA","MA","MD","ME","MI","MN","MO","MS","NC","ND","NE","NJ","NM","NV","NY","OH","OK","OR","PA","SC","SD","TN","TX","UT","VA","WA","WI","WV"];
    return states.find((state) => address.includes(` ${state} `) || address.endsWith(` ${state}`)) || "";
  }

  function hasAddressLocation(gateway) {
    return Boolean([
      gateway.site_address_state,
      gateway.site_address_city,
      gateway.site_address_postal_code,
      gateway.site_compact_address,
      gateway.site_address
    ].map((value) => String(value || "").trim()).find(Boolean));
  }

  function gatewayCoordinates(gateway) {
    const latitude = Number(gateway.site_latitude);
    const longitude = Number(gateway.site_longitude);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      return null;
    }
    if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
      return null;
    }
    return { latitude, longitude };
  }

  function hashNumber(value) {
    let hash = 0;
    for (const char of String(value || "")) {
      hash = ((hash << 5) - hash) + char.charCodeAt(0);
      hash |= 0;
    }
    return Math.abs(hash);
  }

  function applyMapZoom() {
    const content = byId("map-zoom-content");
    const label = byId("map-zoom-label");
    if (!content) {
      return;
    }
    const zoom = Math.round(mapZoom * 100);
    content.style.width = `${zoom}%`;
    content.style.height = `${zoom}%`;
    if (label) {
      label.textContent = `${zoom}%`;
    }
  }

  function setMapZoom(nextZoom, anchor = null) {
    const oldZoom = mapZoom;
    const newZoom = Math.max(1, Math.min(2.5, Math.round(nextZoom * 10) / 10));
    if (newZoom === oldZoom) {
      return;
    }
    let viewport = null;
    let offsetX = 0;
    let offsetY = 0;
    if (anchor && anchor.viewport) {
      viewport = anchor.viewport;
      const rect = viewport.getBoundingClientRect();
      offsetX = anchor.clientX - rect.left;
      offsetY = anchor.clientY - rect.top;
    }
    mapZoom = newZoom;
    applyMapZoom();
    if (viewport) {
      const ratio = newZoom / oldZoom;
      viewport.scrollLeft = ((viewport.scrollLeft + offsetX) * ratio) - offsetX;
      viewport.scrollTop = ((viewport.scrollTop + offsetY) * ratio) - offsetY;
    }
  }

  function setupMapControls() {
    applyMapZoom();
    const mapViewport = document.querySelector(".usa-map");
    const zoomIn = byId("map-zoom-in");
    const zoomOut = byId("map-zoom-out");
    const zoomReset = byId("map-zoom-reset");
    const shark = byId("bermuda-shark");
    if (zoomIn && zoomIn.dataset.zoomReady !== "true") {
      zoomIn.dataset.zoomReady = "true";
      zoomIn.addEventListener("click", () => setMapZoom(mapZoom + 0.2));
    }
    if (zoomOut && zoomOut.dataset.zoomReady !== "true") {
      zoomOut.dataset.zoomReady = "true";
      zoomOut.addEventListener("click", () => setMapZoom(mapZoom - 0.2));
    }
    if (zoomReset && zoomReset.dataset.zoomReady !== "true") {
      zoomReset.dataset.zoomReady = "true";
      zoomReset.addEventListener("click", () => setMapZoom(1));
    }
    if (mapViewport && mapViewport.dataset.wheelZoomReady !== "true") {
      mapViewport.dataset.wheelZoomReady = "true";
      mapViewport.addEventListener("wheel", (event) => {
        event.preventDefault();
        const direction = event.deltaY < 0 ? 1 : -1;
        setMapZoom(mapZoom + (direction * 0.15), {
          viewport: mapViewport,
          clientX: event.clientX,
          clientY: event.clientY
        });
      }, { passive: false });
    }
    if (shark && shark.dataset.sharkReady !== "true") {
      shark.dataset.sharkReady = "true";
      shark.addEventListener("click", (event) => {
        event.stopPropagation();
        window.alert("Get back to work.");
      });
    }
  }

  async function renderUsaMapBase() {
    const svg = byId("usa-map-base");
    if (!svg || svg.dataset.loaded === "true") {
      return;
    }
    try {
      const [{ geoAlbersUsa, geoPath }, { feature }] = await Promise.all([
        import("https://cdn.jsdelivr.net/npm/d3-geo@3/+esm"),
        import("https://cdn.jsdelivr.net/npm/topojson-client@3/+esm")
      ]);
      const response = await fetch("https://cdn.jsdelivr.net/npm/us-atlas@3/states-10m.json");
      if (!response.ok) {
        throw new Error("Could not load USA atlas.");
      }
      const atlas = await response.json();
      const states = feature(atlas, atlas.objects.states).features;
      const nation = feature(atlas, atlas.objects.nation);
      const projection = geoAlbersUsa().fitSize([mapSvgFrame.viewBoxWidth, mapSvgFrame.viewBoxHeight], nation);
      const path = geoPath(projection);
      mapProjection = projection;
      const nationD = path(nation);
      const nationPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
      nationPath.setAttribute("class", "usa-nation");
      if (nationD) {
        nationPath.setAttribute("d", nationD);
        svg.appendChild(nationPath);
      }
      for (const state of states) {
        const stateD = path(state);
        if (!stateD) {
          continue;
        }
        const statePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
        statePath.setAttribute("class", "usa-state");
        statePath.setAttribute("d", stateD);
        svg.appendChild(statePath);
      }
      svg.querySelector(".usa-fallback")?.setAttribute("hidden", "");
      svg.dataset.loaded = "true";
      if (dashboardGateways.length) {
        renderGatewayMap(sortedDashboardGateways());
      }
    } catch {
      svg.dataset.loaded = "fallback";
    }
  }

  function gatewayMapPosition(gateway) {
    const jitter = hashNumber(`${gateway.gateway_id}:${gateway.hostname}`);
    const coordinates = gatewayCoordinates(gateway);
    if (coordinates && mapProjection) {
      const projected = mapProjection([coordinates.longitude, coordinates.latitude]);
      if (projected) {
        return [
          Math.max(2, Math.min(98, mapSvgFrame.left + ((projected[0] / mapSvgFrame.viewBoxWidth) * mapSvgFrame.width))),
          Math.max(2, Math.min(98, mapSvgFrame.top + ((projected[1] / mapSvgFrame.viewBoxHeight) * mapSvgFrame.height)))
        ];
      }
    }
    if (!hasAddressLocation(gateway)) {
      const bermudaTriangle = [
        [83, 59],
        [89, 51],
        [91, 72],
        [86, 66],
        [94, 62],
        [88, 79],
        [80, 70],
        [93, 82]
      ];
      const base = bermudaTriangle[jitter % bermudaTriangle.length];
      return [
        Math.max(78, Math.min(96, base[0] + (((jitter >> 4) % 9) - 4) * 0.7)),
        Math.max(48, Math.min(84, base[1] + (((jitter >> 8) % 9) - 4) * 0.7))
      ];
    }
    const statePositions = {
      AL:[61,65], AZ:[27,58], AR:[55,59], CA:[15,49], CO:[39,49], CT:[84,34], FL:[70,80],
      GA:[68,66], IA:[54,42], ID:[29,33], IL:[61,47], IN:[66,46], KS:[48,52], KY:[66,54],
      LA:[55,70], MA:[86,32], MD:[78,45], ME:[90,24], MI:[66,36], MN:[52,31], MO:[56,52],
      MS:[59,65], NC:[75,57], ND:[45,26], NE:[45,44], NJ:[81,40], NM:[34,60], NV:[22,45],
      NY:[80,34], OH:[70,43], OK:[48,60], OR:[18,33], PA:[76,39], SC:[72,62], SD:[45,36],
      TN:[65,58], TX:[47,73], UT:[31,47], VA:[76,51], WA:[20,25], WI:[59,35], WV:[72,48]
    };
    const state = stateSeed(gateway);
    const base = statePositions[state] || [18 + (hashNumber(gateway.gateway_id) % 66), 28 + (hashNumber(gateway.site_id) % 45)];
    return [
      Math.max(8, Math.min(92, base[0] + ((jitter % 7) - 3) * 0.8)),
      Math.max(14, Math.min(82, base[1] + (((jitter >> 3) % 7) - 3) * 0.8))
    ];
  }

  function gatewayStatusClass(gateway) {
    const status = String(gateway.effective_status || gateway.latest_status || "unknown").toLowerCase();
    if (status.includes("online")) {
      return "online";
    }
    if (status.includes("stale")) {
      return "stale";
    }
    return "offline";
  }

  function statusPill(gateway) {
    const status = gatewayStatusClass(gateway);
    return `<span class="status-pill ${status}">${escapeHtml(gateway.effective_status || gateway.latest_status || "unknown")}</span>`;
  }

  function heartbeatLabel(gateway) {
    if (gateway.heartbeat_age_seconds === null || gateway.heartbeat_age_seconds === undefined) {
      return "no heartbeat";
    }
    if (gateway.heartbeat_age_seconds < 60) {
      return `${gateway.heartbeat_age_seconds}s ago`;
    }
    return `${Math.round(gateway.heartbeat_age_seconds / 60)}m ago`;
  }

  async function initDashboard() {
    initThemeToggle();
    setupMapControls();
    renderUsaMapBase();
    const me = await initProtectedPage(null);
    if (!me) {
      return;
    }
    currentUser = me;
    try {
      const summary = await api("/api/ui/gateways/summary");
      dashboardGateways = await api("/api/ui/gateways");
      dashboardJobs = await api("/api/edge/jobs?limit=10");
      selectedDashboardGatewayId = dashboardGateways[0]?.gateway_id || null;
      const queuedUploads = dashboardGateways.reduce((total, gateway) => total + Number(gateway.queued_upload_count || 0), 0);
      metricCard("metric-total", "Total Gateways", summary.total, "registered");
      metricCard("metric-online", "Online", summary.online, "heartbeat active");
      metricCard("metric-stale", "Stale", summary.stale, "heartbeat delayed");
      metricCard("metric-offline", "Offline", summary.offline, "no current heartbeat");
      metricCard("metric-jobs", "Recent Jobs", dashboardJobs.length, `${queuedUploads} queued uploads`);
      setupGatewaySearch();
      setupGatewaySortHeaders();
      renderGatewayMap(sortedDashboardGateways());
      renderGatewayInspector();
      renderGatewayList();
      renderEventTicker(dashboardJobs);
      if (me.role === "admin") {
        byId("admin-link").hidden = false;
      }
    } catch (error) {
      setText("status", error.message, true);
    }
  }

  function directConnectCell(gateway) {
    if (!gateway.direct_connect_available) {
      return '<span class="muted">Not configured</span>';
    }
    if (!currentUser || currentUser.role === "viewer") {
      return '<span class="muted">Configured</span>';
    }
    return `<a class="button table-command secondary" href="/api/ui/gateways/${encodeURIComponent(gateway.gateway_id)}/direct-connect" data-direct-connect="${escapeHtml(gateway.gateway_id)}">Direct Connect</a>`;
  }

  function statusLabel(gateway) {
    return `${gateway.effective_status || gateway.latest_status || "unknown"} | ${heartbeatLabel(gateway)}`;
  }

  function dashboardStatusCell(gateway) {
    if (gateway.effective_status === "online") {
      return '<span class="status-online">ONLINE</span>';
    }
    return escapeHtml(statusLabel(gateway));
  }

  function gatewaySortValue(gateway, key) {
    const values = {
      gateway_id: gateway.gateway_id,
      site: `${gateway.site_name || ""} ${gateway.site_id || ""}`,
      address: gatewayAddress(gateway) || "",
      hostname: gateway.hostname,
      status: statusLabel(gateway),
      network_status_notes: gateway.network_status_notes || "",
      direct: gateway.direct_connect_available ? "configured" : "not configured",
      configure: gateway.gateway_id
    };
    return String(values[key] ?? "").toLowerCase();
  }

  function gatewaySearchText(gateway) {
    return [
      gateway.gateway_id,
      gateway.site_name,
      gateway.site_id,
      gateway.site_address_city,
      gateway.site_address_state,
      gateway.site_address_postal_code,
      gateway.site_compact_address,
      gateway.site_address,
      gateway.hostname,
      statusLabel(gateway),
      gateway.network_status_notes,
      gateway.direct_connect_available ? "configured" : "not configured"
    ].map((value) => String(value ?? "").toLowerCase()).join(" ");
  }

  function sortedDashboardGateways() {
    const direction = dashboardSort.direction === "desc" ? -1 : 1;
    const search = dashboardSearch.trim().toLowerCase();
    const gateways = search
      ? dashboardGateways.filter((gateway) => gatewaySearchText(gateway).includes(search))
      : dashboardGateways;
    return [...gateways].sort((left, right) => {
      const leftValue = gatewaySortValue(left, dashboardSort.key);
      const rightValue = gatewaySortValue(right, dashboardSort.key);
      const compared = leftValue.localeCompare(rightValue, undefined, { numeric: true, sensitivity: "base" });
      if (compared !== 0) {
        return compared * direction;
      }
      return gatewaySortValue(left, "gateway_id").localeCompare(gatewaySortValue(right, "gateway_id"), undefined, { numeric: true, sensitivity: "base" });
    });
  }

  function updateGatewaySortHeaders() {
    document.querySelectorAll("[data-sort]").forEach((button) => {
      const isActive = button.dataset.sort === dashboardSort.key;
      button.dataset.direction = isActive ? dashboardSort.direction : "";
      button.setAttribute("aria-sort", isActive ? (dashboardSort.direction === "asc" ? "ascending" : "descending") : "none");
      const label = button.dataset.label || button.textContent;
      button.textContent = isActive ? `${label} (${dashboardSort.direction.toUpperCase()})` : label;
    });
  }

  function setupGatewaySortHeaders() {
    document.querySelectorAll("[data-sort]").forEach((button) => {
      if (button.dataset.sortReady === "true") {
        return;
      }
      button.dataset.label = button.textContent;
      button.dataset.sortReady = "true";
      button.addEventListener("click", () => {
        const key = button.dataset.sort;
        if (dashboardSort.key === key) {
          dashboardSort = { key, direction: dashboardSort.direction === "asc" ? "desc" : "asc" };
        } else {
          dashboardSort = { key, direction: "asc" };
        }
        renderGatewayMap(sortedDashboardGateways());
        renderGatewayList();
      });
    });
    updateGatewaySortHeaders();
  }

  function setupGatewaySearch() {
    const search = byId("gateway-search");
    if (!search || search.dataset.searchReady === "true") {
      return;
    }
    search.dataset.searchReady = "true";
    search.addEventListener("input", () => {
      dashboardSearch = search.value;
      renderGatewayMap(sortedDashboardGateways());
      renderGatewayList();
    });
  }

  function selectDashboardGateway(gatewayId) {
    selectedDashboardGatewayId = gatewayId;
    renderGatewayMap(sortedDashboardGateways());
    renderGatewayInspector();
    renderGatewayList();
  }

  function renderGatewayMap(gateways) {
    const layer = byId("gateway-map-nodes");
    const count = byId("map-node-count");
    if (!layer) {
      return;
    }
    layer.textContent = "";
    if (count) {
      count.textContent = `${gateways.length} visible node${gateways.length === 1 ? "" : "s"}`;
    }
    for (const gateway of gateways) {
      const [x, y] = gatewayMapPosition(gateway);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `map-node ${gatewayStatusClass(gateway)}${gateway.gateway_id === selectedDashboardGatewayId ? " selected" : ""}`;
      button.style.left = `${x}%`;
      button.style.top = `${y}%`;
      button.title = `${gateway.site_id || gateway.site_name || "Unassigned site"} - ${gateway.gateway_id}`;
      button.innerHTML = `<span></span><em>${escapeHtml(gateway.gateway_id)}</em>`;
      button.addEventListener("click", () => selectDashboardGateway(gateway.gateway_id));
      layer.appendChild(button);
    }
  }

  function selectedDashboardGateway() {
    return dashboardGateways.find((gateway) => gateway.gateway_id === selectedDashboardGatewayId) || dashboardGateways[0] || null;
  }

  function renderGatewayInspector() {
    const panel = byId("gateway-inspector");
    if (!panel) {
      return;
    }
    const gateway = selectedDashboardGateway();
    if (!gateway) {
      panel.innerHTML = `<div class="empty-state">No gateway selected.</div>`;
      return;
    }
    const encoded = encodeURIComponent(gateway.gateway_id);
    panel.innerHTML = `
      <div class="inspector-head">
        <div>
          <span class="eyebrow">Selected Node</span>
          <h3>${escapeHtml(gateway.gateway_id)}</h3>
          <p>${escapeHtml(gateway.site_name || gateway.site_id || "Unassigned site")}</p>
        </div>
        ${statusPill(gateway)}
      </div>
      <dl class="inspector-grid">
        <dt>Address</dt><dd>${escapeHtml(gatewayAddress(gateway) || "No address on file")}</dd>
        <dt>Host</dt><dd>${escapeHtml(gateway.hostname || "")}</dd>
        <dt>LAN IP</dt><dd>${escapeHtml(gateway.lan_ip || "unknown")}</dd>
        <dt>Heartbeat</dt><dd>${escapeHtml(heartbeatLabel(gateway))}</dd>
        <dt>Agent/UI</dt><dd>${escapeHtml(gateway.agent_version || "?")} / ${escapeHtml(gateway.ui_version || "?")}</dd>
        <dt>Notes</dt><dd>${escapeHtml(gateway.network_status_notes || "No network notes")}</dd>
      </dl>
      <div class="inspector-actions">
        <a class="button" href="/gateways/${encoded}">Workspace</a>
        <a class="button secondary" href="/gateways/${encoded}">Edit Site</a>
        <a class="button secondary" href="/gateways/${encoded}/tunnel/">Remote Tunnel</a>
        ${gateway.direct_connect_available && currentUser?.role !== "viewer" ? `<a class="button secondary" href="/api/ui/gateways/${encoded}/direct-connect" data-direct-connect="${escapeHtml(gateway.gateway_id)}">Direct Connect</a>` : `<span class="muted">Direct Connect not configured</span>`}
      </div>
    `;
    attachDirectConnectHandlers(panel);
  }

  function attachDirectConnectHandlers(root = document) {
    root.querySelectorAll("[data-direct-connect]").forEach((link) => {
      link.addEventListener("click", async (event) => {
        event.preventDefault();
        try {
          const gatewayId = link.getAttribute("data-direct-connect");
          const result = await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/direct-connect`);
          if (!result.available || !result.url) {
            setText("status", result.reason || "Direct Connect is not configured.", true);
            return;
          }
          window.open(result.url, "_blank", "noopener,noreferrer");
        } catch (error) {
          setText("status", error.message, true);
        }
      });
    });
  }

  function renderGatewayList() {
    const table = byId("gateway-list");
    const gateways = sortedDashboardGateways();
    const count = byId("gateway-result-count");
    if (count) {
      count.textContent = `${gateways.length} of ${dashboardGateways.length} gateways`;
    }
    updateGatewaySortHeaders();
    table.textContent = "";
    if (!gateways.length) {
      const row = document.createElement("tr");
      row.innerHTML = `<td colspan="8">No gateways found.</td>`;
      table.appendChild(row);
      return;
    }
    for (const gateway of gateways) {
      const row = document.createElement("tr");
      row.className = gateway.gateway_id === selectedDashboardGatewayId ? "selected-row" : "";
      row.innerHTML = `
        <td><a class="gateway-link" href="/gateways/${encodeURIComponent(gateway.gateway_id)}" data-select-gateway="${escapeHtml(gateway.gateway_id)}">${escapeHtml(gateway.gateway_id)}</a></td>
        <td><strong>${escapeHtml(gateway.site_name || gateway.site_id)}</strong><br><span class="muted">${escapeHtml(gateway.site_id)}</span></td>
        <td>${escapeHtml(gatewayAddress(gateway) || "")}</td>
        <td>${escapeHtml(gateway.hostname)}</td>
        <td><span class="status-text">${dashboardStatusCell(gateway)}</span></td>
        <td>${escapeHtml(gateway.network_status_notes || "")}</td>
        <td>${directConnectCell(gateway)}</td>
        <td><a class="button table-command secondary" href="/gateways/${encodeURIComponent(gateway.gateway_id)}/configure">Configure</a></td>
      `;
      table.appendChild(row);
    }
    table.querySelectorAll("[data-select-gateway]").forEach((link) => {
      link.addEventListener("mouseenter", () => selectDashboardGateway(link.dataset.selectGateway));
      link.addEventListener("focus", () => selectDashboardGateway(link.dataset.selectGateway));
    });
    attachDirectConnectHandlers(table);
  }

  function renderEventTicker(jobs) {
    const list = byId("event-ticker");
    if (!list) {
      return;
    }
    list.textContent = "";
    if (!jobs.length) {
      const item = document.createElement("li");
      item.textContent = "No recent cloud jobs.";
      list.appendChild(item);
      return;
    }
    for (const job of jobs) {
      const item = document.createElement("li");
      item.innerHTML = `
        <span>${escapeHtml(job.status)}</span>
        <strong>${escapeHtml(job.gateway_id)}</strong>
        <em>${escapeHtml(job.job_type)}</em>
      `;
      list.appendChild(item);
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

  function savedPointLabel(point) {
    return `[${point.object_type} ${point.object_instance}] ${point.object_name || "unnamed"}`;
  }

  function selectedSavedPoints() {
    const points = currentGatewayTree?.points || [];
    return points.filter((point) => selectedSavedPointIds.has(point.id));
  }

  function renderSelectedSavedPoints() {
    const panel = byId("selected-points-panel");
    const count = byId("selected-points-count");
    const list = byId("selected-points-list");
    const removeButton = byId("remove-selected-points");
    if (!panel || !count || !list || !removeButton) {
      return;
    }
    const selected = selectedSavedPoints();
    panel.hidden = false;
    count.textContent = selected.length
      ? `${selected.length} saved point(s) selected.`
      : "No saved points selected.";
    list.textContent = "";
    for (const point of selected) {
      const item = document.createElement("li");
      item.textContent = savedPointLabel(point);
      list.appendChild(item);
    }
    removeButton.disabled = !selected.length || !canEditTree();
  }

  function setSavedPointSelected(point, checked) {
    if (checked) {
      selectedSavedPointIds.add(point.id);
    } else {
      selectedSavedPointIds.delete(point.id);
    }
    renderSelectedSavedPoints();
  }

  async function removeSelectedSavedPoints() {
    if (!canEditTree()) {
      setText("status", "Your role is read-only.", true);
      return;
    }
    const selected = selectedSavedPoints();
    if (!selected.length) {
      setText("status", "Select at least one saved point first.", true);
      return;
    }
    const removeButton = byId("remove-selected-points");
    removeButton.disabled = true;
    setText("status", `Removing ${selected.length} selected saved point(s)...`);
    try {
      const result = await api("/api/ui/points/bulk-remove", {
        method: "POST",
        body: JSON.stringify({ point_ids: selected.map((point) => point.id) })
      });
      selectedSavedPointIds = new Set();
      await loadGatewayWorkspace();
      const missing = result.missing_ids?.length ? ` ${result.missing_ids.length} were already gone.` : "";
      setText("status", `Removed ${result.removed_count} selected saved point(s).${missing}`);
    } catch (error) {
      setText("status", error.message, true);
      renderSelectedSavedPoints();
    }
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

  function pointSelectionRow(point, label, meta = "", depth = 0) {
    const showPointDetails = () => setTreeDetails(label, {
      property: point.property,
      value: point.present_value,
      units: point.units,
      writable: point.writable,
      latest_read_at: point.latest_read_at
    });
    const row = document.createElement("div");
    row.className = "tree-row point-select-row";
    row.dataset.kind = "point";
    row.style.setProperty("--depth", String(depth));
    row.innerHTML = `
      <input type="checkbox" data-role="saved-point-select" aria-label="Select ${escapeHtml(label)}">
      <span class="node-icon">-></span>
      <span class="node-label">${escapeHtml(label)}</span>
      <span class="node-meta">${escapeHtml(meta)}</span>
    `;
    const checkbox = row.querySelector('[data-role="saved-point-select"]');
    checkbox.checked = selectedSavedPointIds.has(point.id);
    checkbox.addEventListener("change", (event) => {
      event.stopPropagation();
      setSavedPointSelected(point, checkbox.checked);
      showPointDetails();
    });
    row.addEventListener("click", (event) => {
      if (event.target === checkbox) {
        return;
      }
      showPointDetails();
    });
    return row;
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
    setDiscoveryProgress(25, `Queued ${job.job_id} on BACnet ${job.request_json?.bacnet_port || "configured port"}.`);
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
    selectedSavedPointIds = new Set([...selectedSavedPointIds].filter((id) => tree.points.some((point) => point.id === id)));
    const target = byId("tree");
    target.textContent = "";
    if (!tree.groups.length && !tree.devices.length) {
      target.textContent = "No saved devices or points yet.";
      renderSelectedSavedPoints();
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
          label: "Remove device",
          handler: () => removeTreeItem("device", device.id, deviceLabel)
        }
      ]);
      const container = document.createElement("div");
      addCollapsible(container, row, [], showDeviceDetails);
      for (const [folderLabel, folderPoints] of pointGroups.entries()) {
        const pointRows = folderPoints.map((point) => {
          const pointLabel = savedPointLabel(point);
          return pointSelectionRow(point, pointLabel, point.present_value ?? "", depth + 2);
        });
        addCollapsible(container.querySelector(".tree-children"), treeRow("folder", folderLabel, `${folderPoints.length}`, depth + 1), pointRows);
      }
      if (!points.length) {
        container.querySelector(".tree-children").appendChild(leafRow("empty", "No imported points", "import edge template", depth + 1));
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
    renderSelectedSavedPoints();
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
    const [gateway, tree, site, directConnect, tunnelStatus] = await Promise.all([
      api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}`),
      api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/tree`),
      api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/site`),
      api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/direct-connect`),
      api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/tunnel-status`)
    ]);
    byId("gateway-title").textContent = `${gateway.gateway_id} Workspace`;
    byId("gateway-status").textContent = `${statusLabel(gateway)} | BACnet ${gateway.bacnet_port} | ${gateway.lan_ip || "no LAN IP"}`;
    renderSiteInfo(site, directConnect, tunnelStatus);
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

  function setFieldValue(id, value) {
    const element = byId(id);
    if (element) {
      element.value = value ?? "";
    }
  }

  function optionalNumberValue(id) {
    const value = byId(id).value.trim();
    return value === "" ? null : Number(value);
  }

  function renderSiteInfo(site, directConnect, tunnelStatus) {
    setFieldValue("site-name", site.name);
    setFieldValue("site-address-street", site.address_street || site.address);
    setFieldValue("site-address-city", site.address_city);
    setFieldValue("site-address-state", site.address_state);
    setFieldValue("site-address-postal-code", site.address_postal_code);
    setFieldValue("site-latitude", site.latitude);
    setFieldValue("site-longitude", site.longitude);
    setFieldValue("direct-connect-host", site.direct_connect_host || site.cradlepoint_ip || site.external_ip);
    setFieldValue("direct-connect-port", site.direct_connect_port || 5002);
    setFieldValue("gateway-ui-port", site.gateway_ui_port || 5000);
    setFieldValue("store-hours-mf", site.store_hours_monday_friday || site.store_hours_mf);
    setFieldValue("store-hours-sat", site.store_hours_saturday || site.store_hours_sat);
    setFieldValue("store-hours-sun", site.store_hours_sunday || site.store_hours_sun);
    setFieldValue("network-status-notes", site.network_status_notes);
    byId("tunnel-status").textContent = tunnelStatus.connected ? "connected" : "not connected";

    const directLink = byId("direct-connect-link");
    const directStatus = byId("direct-connect-status");
    if (directConnect.available && directConnect.url && currentUser && currentUser.role !== "viewer") {
      directStatus.textContent = `${directConnect.host}:${directConnect.port}`;
      directLink.hidden = false;
      directLink.href = directConnect.url;
      directLink.target = "_blank";
      directLink.rel = "noopener noreferrer";
    } else if (directConnect.available) {
      directStatus.textContent = `${directConnect.host}:${directConnect.port} (read-only)`;
      directLink.hidden = true;
      directLink.removeAttribute("href");
    } else {
      directStatus.textContent = directConnect.reason || "Direct Connect is not configured.";
      directLink.hidden = true;
      directLink.removeAttribute("href");
    }
  }

  async function initGatewayWorkspace() {
    const me = await initProtectedPage(null);
    if (!me) {
      return;
    }
    currentUser = me;
    const groupForm = byId("group-form");
    const importTemplateForm = byId("import-template-form");
    const siteInfoForm = byId("site-info-form");
    const discoverButton = byId("discover-devices");
    const saveSelectedPointsButton = byId("save-selected-points");
    const selectAllPointsButton = byId("select-all-point-candidates");
    const deselectAllPointsButton = byId("deselect-all-point-candidates");
    const removeSelectedPointsButton = byId("remove-selected-points");
    const gatewayId = document.body.dataset.gatewayId;
    const technicalSection = byId("technical-section");
    if (technicalSection && me.role === "admin") {
      technicalSection.hidden = false;
    }
    const canEditSite = me.role === "admin";
    siteInfoForm.querySelectorAll("input, textarea").forEach((field) => {
      field.disabled = !canEditSite;
    });
    byId("save-site-info").hidden = !canEditSite;
    siteInfoForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!canEditSite) {
        setText("status", "Admin role required to edit site information.", true);
        return;
      }
      try {
        await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/site`, {
          method: "PATCH",
          body: JSON.stringify({
            name: byId("site-name").value.trim(),
            address_street: byId("site-address-street").value.trim(),
            address_city: byId("site-address-city").value.trim(),
            address_state: byId("site-address-state").value.trim(),
            address_postal_code: byId("site-address-postal-code").value.trim(),
            latitude: optionalNumberValue("site-latitude"),
            longitude: optionalNumberValue("site-longitude"),
            direct_connect_host: byId("direct-connect-host").value.trim() || null,
            direct_connect_port: Number(byId("direct-connect-port").value || 5002),
            gateway_ui_port: Number(byId("gateway-ui-port").value || 5000),
            store_hours_monday_friday: byId("store-hours-mf").value.trim(),
            store_hours_saturday: byId("store-hours-sat").value.trim(),
            store_hours_sunday: byId("store-hours-sun").value.trim(),
            network_status_notes: byId("network-status-notes").value.trim()
          })
        });
        setText("status", "Site information saved.");
        await loadGatewayWorkspace();
      } catch (error) {
        setText("status", error.message, true);
      }
    });
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
    importTemplateForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = byId("template-file").files?.[0];
      if (!file) {
        setText("status", "Choose an edge commissioning template JSON file first.", true);
        return;
      }
      setText("status", `Importing ${file.name}...`);
      try {
        const template = JSON.parse(await file.text());
        const result = await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/commissioning-template/import`, {
          method: "POST",
          body: JSON.stringify(template)
        });
        byId("template-file").value = "";
        await loadGatewayWorkspace();
        renderImportResult(result);
        setText("status", `Imported template: ${result.created_devices} device(s) created, ${result.updated_devices} updated, ${result.created_points} point(s) created, ${result.updated_points} updated.`);
      } catch (error) {
        setText("status", error.message, true);
      }
    });
    discoverButton.addEventListener("click", async () => {
      discoverButton.disabled = true;
      setDiscoveryProgress(5, "Queueing discovery job...");
      try {
        const job = await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/discover-devices`, { method: "POST" });
        setDiscoveryProgress(25, `Queued ${job.job_id} on BACnet ${job.request_json?.bacnet_port || "configured port"}.`);
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
    removeSelectedPointsButton.addEventListener("click", removeSelectedSavedPoints);
    saveSelectedPointsButton.addEventListener("click", async () => {
      if (!currentPointCandidateDevice) {
        setText("status", "Import an edge commissioning template first.", true);
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

  async function initTunnelConsole() {
    const me = await initProtectedPage("operator");
    if (!me) {
      return;
    }
    const gatewayId = document.body.dataset.gatewayId;
    byId("workspace-link").href = `/gateways/${encodeURIComponent(gatewayId)}`;
    try {
      const [gateway, directConnect, tunnelStatus] = await Promise.all([
        api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}`),
        api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/direct-connect`),
        api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/tunnel-status`)
      ]);
      byId("gateway-title").textContent = `${gateway.gateway_id} Remote Console`;
      byId("heartbeat-summary").textContent = `${gateway.effective_status} | heartbeat ${gateway.heartbeat_age_seconds ?? "unknown"}s ago`;
      byId("tunnel-summary").textContent = tunnelStatus.connected ? "connected" : "not connected";
      if (!tunnelStatus.connected) {
        setText(
          "status",
          "Gateway tunnel is not connected. Direct Connect may still be available. Heartbeat and job polling are separate from tunnel status.",
          true
        );
      } else {
        setText(
          "status",
          "Gateway tunnel is connected. Opening the live remote console requires the protected tunnel relay flow."
        );
      }
      const directLink = byId("direct-connect-link");
      if (directConnect.available && directConnect.url && me.role !== "viewer") {
        directLink.href = directConnect.url;
        directLink.hidden = false;
      }
      const openTunnelButton = byId("open-tunnel-console");
      const tunnelFallback = byId("tunnel-session-link");
      openTunnelButton.disabled = !tunnelStatus.connected;
      openTunnelButton.addEventListener("click", async () => {
        openTunnelButton.disabled = true;
        tunnelFallback.hidden = true;
        tunnelFallback.removeAttribute("href");
        const tunnelWindow = window.open("about:blank", "_blank");
        setText("status", "Creating short-lived tunnel console session...");
        try {
          const session = await api(`/api/ui/gateways/${encodeURIComponent(gatewayId)}/tunnel-session`, { method: "POST" });
          if (tunnelWindow) {
            try {
              tunnelWindow.opener = null;
            } catch (_) {}
            tunnelWindow.location.assign(session.url);
            setText("status", "Tunnel console opened in a new tab.");
          } else {
            tunnelFallback.href = session.url;
            tunnelFallback.hidden = false;
            setText("status", "Popup blocked. Use the manual tunnel console link.");
          }
        } catch (error) {
          if (tunnelWindow) {
            tunnelWindow.close();
          }
          setText("status", error.message, true);
        } finally {
          openTunnelButton.disabled = !tunnelStatus.connected;
        }
      });
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
  } else if (page === "tunnel-console") {
    initTunnelConsole();
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
    [hidden] {{ display: none !important; }}
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
    input, select, textarea {{
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 8px 10px;
      font: inherit;
      background: #ffffff;
    }}
    textarea {{
      resize: vertical;
    }}
    input:disabled, textarea:disabled {{
      background: #f4f6f8;
      color: var(--muted);
    }}
    .muted {{
      color: var(--muted);
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
    .sort-header {{
      min-height: 0;
      border: 0;
      padding: 0;
      color: inherit;
      background: transparent;
      font: inherit;
      font-weight: 700;
      text-transform: inherit;
      cursor: pointer;
    }}
    .sort-header:hover {{
      color: var(--accent);
    }}
    .table-actions {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: end;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }}
    .search-field {{
      width: min(340px, 100%);
    }}
    .home-link {{
      position: fixed;
      top: 12px;
      right: 12px;
      z-index: 10;
      box-shadow: 0 1px 3px rgba(23, 32, 44, 0.16);
    }}
    .status-online {{
      min-height: 30px;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid rgba(118, 247, 166, 0.62);
      border-radius: 999px;
      padding: 6px 11px;
      color: #93ffb6;
      background: rgba(24, 148, 90, 0.14);
      box-shadow: 0 0 0 1px rgba(118, 247, 166, 0.1), 0 0 18px rgba(118, 247, 166, 0.3);
      font: 900 0.9rem/1 "JetBrains Mono", Consolas, monospace;
      letter-spacing: 0;
      text-transform: uppercase;
      text-shadow: 0 0 14px rgba(118, 247, 166, 0.78);
    }}
    .status-online::before {{
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #76f7a6;
      box-shadow: 0 0 10px #76f7a6, 0 0 18px rgba(118, 247, 166, 0.72);
    }}
    body[data-page="app"][data-theme="light"] .status-online {{
      color: #09683f;
      background: rgba(12, 139, 95, 0.12);
      border-color: rgba(12, 139, 95, 0.34);
      box-shadow: 0 0 0 1px rgba(12, 139, 95, 0.08), 0 0 16px rgba(12, 139, 95, 0.18);
      text-shadow: none;
    }}
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
    .tree-shell > aside {{
      display: grid;
      gap: 12px;
      align-content: start;
    }}
    .tree-shell > aside .detail-panel {{
      min-height: 0;
    }}
    .compact-panel {{
      min-height: 0;
      margin: 12px 0;
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
    .point-select-row {{
      grid-template-columns: 28px 34px minmax(0, 1fr) auto;
      cursor: pointer;
    }}
    .point-select-row input {{
      width: 16px;
      height: 16px;
      margin: 0;
      cursor: pointer;
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
    .selected-point-list {{
      max-height: 180px;
      margin: 10px 0;
      padding-left: 20px;
      overflow: auto;
      font-size: 13px;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    body[data-page="app"] {{
      color-scheme: dark;
      --border: rgba(154, 180, 196, 0.22);
      --ink: #eef7f8;
      --muted: #91a7ad;
      --panel: rgba(14, 22, 26, 0.78);
      --accent: #22d3c5;
      --accent-strong: #76f7a6;
      --warning: #f5c542;
      --danger: #ff6b6b;
      min-height: 100vh;
      background:
        linear-gradient(180deg, rgba(5, 10, 12, 0.94), rgba(10, 14, 15, 1)),
        repeating-linear-gradient(90deg, rgba(34, 211, 197, 0.05) 0 1px, transparent 1px 104px);
      font-family: "Inter", "Segoe UI", Arial, Helvetica, sans-serif;
    }}
    body[data-page="app"][data-theme="light"] {{
      color-scheme: light;
      --border: rgba(45, 64, 75, 0.18);
      --ink: #16242a;
      --muted: #536873;
      --panel: rgba(255, 255, 255, 0.9);
      --accent: #087f86;
      --accent-strong: #0c8b5f;
      --warning: #a96f00;
      --danger: #c23b3b;
      background:
        linear-gradient(180deg, rgba(246, 250, 250, 0.98), rgba(228, 237, 239, 1)),
        repeating-linear-gradient(90deg, rgba(8, 127, 134, 0.06) 0 1px, transparent 1px 104px);
    }}
    body[data-page="app"] header {{
      border-bottom: 1px solid var(--border);
      background: rgba(8, 14, 16, 0.9);
      backdrop-filter: blur(14px);
    }}
    body[data-page="app"][data-theme="light"] header {{
      background: rgba(248, 252, 252, 0.92);
    }}
    body[data-page="app"] .cloud-header {{
      position: sticky;
      top: 0;
      z-index: 10;
      padding: 18px clamp(18px, 3vw, 38px);
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
      align-items: center;
      gap: 18px;
    }}
    body[data-page="app"] .cloud-title {{
      min-width: 0;
    }}
    body[data-page="app"] .cloud-header .toolbar {{
      justify-content: flex-end;
      min-width: 0;
    }}
    .fifth-third-logo {{
      justify-self: center;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 7px 13px 7px 8px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.06);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
      color: #f7ffff;
      font: 800 13px/1 "Inter", "Segoe UI", Arial, sans-serif;
      letter-spacing: 0;
      white-space: nowrap;
    }}
    .fifth-third-logo-mark {{
      width: 34px;
      height: 25px;
      border-radius: 5px;
      display: grid;
      place-items: center;
      color: #ffffff;
      background: linear-gradient(135deg, #1f4390 0 58%, #25aa68 58% 100%);
      font: 900 15px/1 "Inter", "Segoe UI", Arial, sans-serif;
      letter-spacing: -0.04em;
    }}
    body[data-page="app"][data-theme="light"] .fifth-third-logo {{
      border-color: rgba(31, 67, 144, 0.18);
      background: rgba(255, 255, 255, 0.7);
      color: #1f4390;
    }}
    body[data-page="app"] h1 {{
      font-size: clamp(24px, 3vw, 38px);
      line-height: 1;
      letter-spacing: 0;
    }}
    body[data-page="app"] h2,
    body[data-page="app"] h3 {{
      margin: 0;
      color: #f7ffff;
      letter-spacing: 0;
    }}
    body[data-page="app"][data-theme="light"] h1,
    body[data-page="app"][data-theme="light"] h2,
    body[data-page="app"][data-theme="light"] h3 {{
      color: #122329;
    }}
    body[data-page="app"] h2 {{
      font-size: 18px;
    }}
    body[data-page="app"] h3 {{
      font-size: 26px;
    }}
    body[data-page="app"] .eyebrow {{
      display: block;
      margin-bottom: 6px;
      color: var(--accent-strong);
      font: 700 11px/1.2 "JetBrains Mono", Consolas, monospace;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    body[data-page="app"] #identity {{
      color: var(--muted);
      font: 600 12px/1.2 "JetBrains Mono", Consolas, monospace;
    }}
    body[data-page="app"] button,
    body[data-page="app"] .button {{
      border-color: rgba(34, 211, 197, 0.55);
      border-radius: 6px;
      color: #031314;
      background: var(--accent);
      box-shadow: 0 0 0 1px rgba(34, 211, 197, 0.12), 0 12px 26px rgba(34, 211, 197, 0.12);
    }}
    body[data-page="app"] button.secondary,
    body[data-page="app"] .button.secondary {{
      color: var(--accent);
      background: rgba(34, 211, 197, 0.08);
    }}
    body[data-page="app"] .cloud-main {{
      width: 100%;
      max-width: none;
      min-height: calc(100vh - 76px);
      padding: clamp(16px, 2.5vw, 32px);
      display: grid;
      gap: 18px;
    }}
    body[data-page="app"] section {{
      border-bottom: 0;
      padding: 0;
    }}
    .command-strip {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 14px;
    }}
    .metric-card {{
      min-height: 116px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
      display: grid;
      gap: 8px;
      align-content: start;
      background:
        linear-gradient(135deg, rgba(34, 211, 197, 0.12), rgba(118, 247, 166, 0.05)),
        rgba(11, 20, 23, 0.86);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
    }}
    body[data-page="app"][data-theme="light"] .metric-card {{
      background:
        linear-gradient(135deg, rgba(8, 127, 134, 0.1), rgba(12, 139, 95, 0.05)),
        rgba(255, 255, 255, 0.86);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.9), 0 14px 34px rgba(23, 42, 49, 0.1);
    }}
    .metric-card.warn {{
      background:
        linear-gradient(135deg, rgba(245, 197, 66, 0.14), rgba(255, 107, 107, 0.06)),
        rgba(11, 20, 23, 0.86);
    }}
    .metric-card.good {{
      background:
        linear-gradient(135deg, rgba(118, 247, 166, 0.14), rgba(34, 211, 197, 0.06)),
        rgba(11, 20, 23, 0.86);
    }}
    .metric-card.bad {{
      background:
        linear-gradient(135deg, rgba(255, 107, 107, 0.16), rgba(245, 197, 66, 0.04)),
        rgba(11, 20, 23, 0.86);
    }}
    body[data-page="app"][data-theme="light"] .metric-card.warn {{
      background:
        linear-gradient(135deg, rgba(169, 111, 0, 0.12), rgba(194, 59, 59, 0.04)),
        rgba(255, 255, 255, 0.86);
    }}
    body[data-page="app"][data-theme="light"] .metric-card.good {{
      background:
        linear-gradient(135deg, rgba(12, 139, 95, 0.12), rgba(8, 127, 134, 0.04)),
        rgba(255, 255, 255, 0.86);
    }}
    body[data-page="app"][data-theme="light"] .metric-card.bad {{
      background:
        linear-gradient(135deg, rgba(194, 59, 59, 0.12), rgba(169, 111, 0, 0.04)),
        rgba(255, 255, 255, 0.86);
    }}
    .metric-card span,
    .metric-card em,
    .panel-counter {{
      color: var(--muted);
      font: 600 12px/1.3 "JetBrains Mono", Consolas, monospace;
      font-style: normal;
    }}
    .metric-card strong {{
      color: #ffffff;
      font-size: clamp(30px, 4vw, 46px);
      line-height: 0.95;
    }}
    body[data-page="app"][data-theme="light"] .metric-card strong {{
      color: #122329;
    }}
    .ops-grid {{
      min-height: 520px;
      display: grid;
      grid-template-columns: minmax(0, 1.75fr) minmax(330px, 0.7fr);
      gap: 18px;
      align-items: stretch;
    }}
    .map-panel,
    .inspector-panel,
    .gateway-panel,
    .ticker-panel {{
      border: 1px solid var(--border);
      border-radius: 8px;
      background: rgba(9, 16, 18, 0.84);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 22px 60px rgba(0, 0, 0, 0.24);
    }}
    body[data-page="app"][data-theme="light"] .map-panel,
    body[data-page="app"][data-theme="light"] .inspector-panel,
    body[data-page="app"][data-theme="light"] .gateway-panel,
    body[data-page="app"][data-theme="light"] .ticker-panel {{
      background: rgba(255, 255, 255, 0.82);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.9), 0 18px 44px rgba(23, 42, 49, 0.12);
    }}
    .map-panel,
    .gateway-panel,
    .ticker-panel {{
      padding: 18px;
    }}
    .panel-title {{
      min-height: 44px;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }}
    .map-toolbar {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .map-tool-button {{
      min-height: 30px;
      border-radius: 5px;
      padding: 5px 9px;
      font: 800 11px/1 "JetBrains Mono", Consolas, monospace;
    }}
    .usa-map {{
      position: relative;
      min-height: 430px;
      height: 56vh;
      max-height: 680px;
      overflow: auto;
      border: 1px solid rgba(34, 211, 197, 0.16);
      border-radius: 8px;
      background:
        repeating-linear-gradient(0deg, rgba(34, 211, 197, 0.055) 0 1px, transparent 1px 54px),
        repeating-linear-gradient(90deg, rgba(34, 211, 197, 0.045) 0 1px, transparent 1px 54px),
        rgba(4, 12, 14, 0.82);
      scrollbar-color: rgba(34, 211, 197, 0.7) rgba(4, 12, 14, 0.9);
      scrollbar-width: thin;
    }}
    .map-zoom-content {{
      position: relative;
      min-width: 100%;
      min-height: 100%;
      width: 100%;
      height: 100%;
    }}
    body[data-page="app"][data-theme="light"] .usa-map {{
      border-color: rgba(8, 127, 134, 0.2);
      background:
        repeating-linear-gradient(0deg, rgba(8, 127, 134, 0.07) 0 1px, transparent 1px 54px),
        repeating-linear-gradient(90deg, rgba(8, 127, 134, 0.06) 0 1px, transparent 1px 54px),
        rgba(233, 243, 243, 0.86);
    }}
    .usa-map svg {{
      position: absolute;
      inset: 5% 3%;
      width: 94%;
      height: 90%;
      filter: drop-shadow(0 0 18px rgba(34, 211, 197, 0.12));
    }}
    body[data-page="app"] .bermuda-shark {{
      position: absolute;
      left: 86%;
      top: 75%;
      width: 32px;
      height: 24px;
      min-width: 32px;
      min-height: 24px;
      padding: 0;
      border: 0 !important;
      border-radius: 0;
      background: transparent !important;
      box-shadow: none;
      appearance: none;
      cursor: pointer;
      z-index: 1;
      opacity: 0.88;
      animation: shark-swim 62s ease-in-out infinite;
      filter: drop-shadow(0 0 8px rgba(34, 211, 197, 0.22));
    }}
    .bermuda-shark::before {{
      content: "";
      position: absolute;
      left: 10px;
      top: 5px;
      width: 11px;
      height: 8px;
      background:
        linear-gradient(126deg, transparent 0 20%, rgba(245, 252, 252, 0.92) 22% 27%, transparent 30% 100%),
        linear-gradient(135deg, rgba(176, 190, 190, 0.98), rgba(75, 86, 89, 0.98));
      clip-path: path("M 0.5 7.5 C 2 2.6 6.2 -0.8 10.8 1 C 9 2.6 8.1 4.9 8.3 8 C 6.1 8.5 3 8.4 0.5 7.5 Z");
      border-radius: 60% 44% 38% 24%;
      box-shadow: inset -1px -0.5px 0 rgba(5, 9, 11, 0.82), inset 0.5px 0 0 rgba(255, 255, 255, 0.16);
      filter:
        drop-shadow(1px 0 0 rgba(5, 9, 11, 0.82))
        drop-shadow(-1px 0 0 rgba(5, 9, 11, 0.72))
        drop-shadow(0 1px 0 rgba(5, 9, 11, 0.78));
      transform-origin: 50% 100%;
      animation: shark-fin-wobble 2.2s ease-in-out infinite;
    }}
    .bermuda-shark:focus-visible {{
      outline: 1px solid rgba(34, 211, 197, 0.78);
      outline-offset: 3px;
    }}
    .bermuda-shark::after {{
      content: "";
      position: absolute;
      left: 7px;
      right: 7px;
      bottom: 9px;
      height: 2px;
      border-radius: 999px;
      background:
        radial-gradient(ellipse at 15% 50%, rgba(34, 211, 197, 0.3), transparent 58%),
        radial-gradient(ellipse at 55% 50%, rgba(34, 211, 197, 0.22), transparent 62%),
        rgba(34, 211, 197, 0.12);
      box-shadow: 0 0 10px rgba(34, 211, 197, 0.16);
      animation: shark-wake 1.8s ease-in-out infinite;
    }}
    body[data-page="app"][data-theme="light"] .bermuda-shark {{
      opacity: 0.74;
      filter: drop-shadow(0 0 10px rgba(8, 127, 134, 0.18));
    }}
    body[data-page="app"][data-theme="light"] .bermuda-shark::before {{
      background: linear-gradient(135deg, rgba(77, 125, 133, 0.9), rgba(12, 84, 93, 0.82));
    }}
    @keyframes shark-swim {{
      0% {{ transform: translate(0, 0) scaleX(1) rotate(-2deg); }}
      18% {{ transform: translate(6px, 48px) scaleX(1) rotate(5deg); }}
      24% {{ transform: translate(6px, 48px) scaleX(1) rotate(5deg); }}
      30% {{ transform: translate(6px, 48px) scaleX(1) rotate(1deg); }}
      68% {{ transform: translate(-230px, 62px) scaleX(1) rotate(-3deg); }}
      74% {{ transform: translate(-230px, 62px) scaleX(-1) rotate(-3deg); }}
      92% {{ transform: translate(6px, 48px) scaleX(-1) rotate(4deg); }}
      100% {{ transform: translate(0, 0) scaleX(-1) rotate(-2deg); }}
    }}
    @keyframes shark-fin-wobble {{
      0%, 100% {{ transform: rotate(-1deg) scaleY(0.99); }}
      50% {{ transform: rotate(2deg) scaleY(1.04); }}
    }}
    @keyframes shark-wake {{
      0%, 100% {{ opacity: 0.32; transform: scaleX(0.78); }}
      50% {{ opacity: 0.72; transform: scaleX(1.08); }}
    }}
    .usa-map .usa-mainland,
    .usa-map .usa-florida,
    .usa-map .usa-new-england,
    .usa-map .usa-great-lakes,
    .usa-map .usa-inset,
    .usa-map .usa-nation {{
      fill: rgba(30, 54, 57, 0.72);
      stroke: rgba(139, 248, 214, 0.35);
      stroke-width: 2;
    }}
    body[data-page="app"][data-theme="light"] .usa-map .usa-mainland,
    body[data-page="app"][data-theme="light"] .usa-map .usa-florida,
    body[data-page="app"][data-theme="light"] .usa-map .usa-new-england,
    body[data-page="app"][data-theme="light"] .usa-map .usa-great-lakes,
    body[data-page="app"][data-theme="light"] .usa-map .usa-inset,
    body[data-page="app"][data-theme="light"] .usa-map .usa-nation {{
      fill: rgba(142, 175, 178, 0.5);
      stroke: rgba(8, 127, 134, 0.42);
    }}
    .usa-map .usa-state {{
      fill: rgba(30, 54, 57, 0.42);
      stroke: rgba(139, 248, 214, 0.28);
      stroke-width: 0.9;
    }}
    body[data-page="app"][data-theme="light"] .usa-map .usa-state {{
      fill: rgba(151, 185, 188, 0.38);
      stroke: rgba(8, 127, 134, 0.32);
    }}
    .usa-map .usa-line {{
      fill: none;
      stroke: rgba(145, 167, 173, 0.18);
      stroke-width: 1.2;
    }}
    .map-node-layer {{
      position: absolute;
      inset: 0;
    }}
    .map-node {{
      position: absolute;
      width: 18px;
      height: 18px;
      min-height: 18px;
      padding: 0;
      transform: translate(-50%, -50%);
      border-radius: 50%;
      display: grid;
      place-items: center;
      color: #fff;
      background: transparent;
      box-shadow: none;
    }}
    .map-node span {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--accent-strong);
      box-shadow: 0 0 16px var(--accent-strong), 0 0 0 6px rgba(118, 247, 166, 0.1);
    }}
    .map-node.stale span {{
      background: var(--warning);
      box-shadow: 0 0 16px var(--warning), 0 0 0 6px rgba(245, 197, 66, 0.1);
    }}
    .map-node.offline span {{
      background: var(--danger);
      box-shadow: 0 0 16px var(--danger), 0 0 0 6px rgba(255, 107, 107, 0.1);
    }}
    .map-node.selected {{
      outline: 1px solid rgba(255, 255, 255, 0.9);
      outline-offset: 5px;
    }}
    .map-node em {{
      position: absolute;
      left: 16px;
      top: -7px;
      display: none;
      padding: 3px 6px;
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 5px;
      color: #ecfeff;
      background: rgba(3, 13, 15, 0.86);
      font: 700 11px/1 "JetBrains Mono", Consolas, monospace;
      font-style: normal;
      white-space: nowrap;
    }}
    .map-node:hover em,
    .map-node.selected em {{
      display: block;
    }}
    .inspector-panel {{
      min-height: 100%;
      padding: 18px;
      display: grid;
      align-content: start;
      gap: 16px;
    }}
    .inspector-head {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: flex-start;
    }}
    .inspector-head p {{
      margin: 8px 0 0;
      color: var(--muted);
    }}
    .status-pill {{
      display: inline-flex;
      min-height: 26px;
      align-items: center;
      border: 1px solid rgba(118, 247, 166, 0.45);
      border-radius: 999px;
      padding: 5px 10px;
      color: #02130a;
      background: var(--accent-strong);
      font: 800 11px/1 "JetBrains Mono", Consolas, monospace;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .status-pill.stale {{
      border-color: rgba(245, 197, 66, 0.48);
      color: #1b1300;
      background: var(--warning);
    }}
    .status-pill.offline {{
      border-color: rgba(255, 107, 107, 0.48);
      color: #220000;
      background: var(--danger);
    }}
    .inspector-grid {{
      display: grid;
      grid-template-columns: 96px minmax(0, 1fr);
      gap: 10px 14px;
      margin: 0;
      padding: 16px 0;
      border-top: 1px solid var(--border);
      border-bottom: 1px solid var(--border);
    }}
    .inspector-grid dt {{
      color: var(--muted);
      font: 700 11px/1.3 "JetBrains Mono", Consolas, monospace;
      text-transform: uppercase;
    }}
    .inspector-grid dd {{
      margin: 0;
      color: #edfafa;
      word-break: break-word;
    }}
    body[data-page="app"][data-theme="light"] .inspector-grid dd {{
      color: #16242a;
    }}
    .inspector-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .empty-state {{
      min-height: 180px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
    }}
    .gateway-toolbar {{
      display: grid;
      grid-template-columns: minmax(260px, 520px) minmax(0, 1fr);
      gap: 14px;
      align-items: center;
      margin-bottom: 12px;
    }}
    body[data-page="app"] input {{
      border-color: rgba(145, 167, 173, 0.26);
      border-radius: 6px;
      color: #ecfeff;
      background: rgba(4, 12, 14, 0.72);
    }}
    body[data-page="app"][data-theme="light"] input {{
      color: #16242a;
      background: rgba(255, 255, 255, 0.9);
    }}
    body[data-page="app"] input::placeholder {{
      color: rgba(145, 167, 173, 0.78);
    }}
    body[data-page="app"] .notice {{
      min-height: 0;
      margin-top: 0;
    }}
    .table-wrap {{
      overflow: auto;
    }}
    .gateway-table {{
      min-width: 980px;
      border-collapse: separate;
      border-spacing: 0;
    }}
    .gateway-table th,
    .gateway-table td {{
      border-bottom: 1px solid rgba(145, 167, 173, 0.16);
      padding: 13px 10px;
      color: #e7f6f7;
      background: transparent;
    }}
    body[data-page="app"][data-theme="light"] .gateway-table th,
    body[data-page="app"][data-theme="light"] .gateway-table td {{
      color: #1d3037;
      border-bottom-color: rgba(45, 64, 75, 0.13);
    }}
    .gateway-table th {{
      color: var(--muted);
      font: 800 11px/1.2 "JetBrains Mono", Consolas, monospace;
      text-transform: uppercase;
    }}
    .gateway-table th button {{
      min-height: 0;
      border: 0;
      padding: 0;
      color: inherit;
      background: transparent;
      box-shadow: none;
      font: inherit;
      text-transform: inherit;
    }}
    .gateway-table tr:hover td,
    .gateway-table .selected-row td {{
      background: rgba(34, 211, 197, 0.06);
    }}
    .gateway-link {{
      min-height: 0;
      border: 0;
      padding: 0;
      color: var(--accent);
      background: transparent;
      box-shadow: none;
      font: 800 13px/1.2 "JetBrains Mono", Consolas, monospace;
      text-decoration: none;
    }}
    .gateway-link:hover {{
      text-decoration: underline;
    }}
    .table-command {{
      min-height: 30px;
      padding: 6px 10px;
      border-radius: 5px;
      font: 800 11px/1 "JetBrains Mono", Consolas, monospace;
      white-space: nowrap;
    }}
    .status-text {{
      color: inherit;
      font: 700 12px/1.35 "JetBrains Mono", Consolas, monospace;
    }}
    .icon-link {{
      display: inline-flex;
      align-items: center;
    }}
    .event-ticker {{
      max-height: 180px;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 8px;
      overflow: auto;
      list-style: none;
    }}
    .event-ticker li {{
      min-height: 36px;
      border: 1px solid rgba(145, 167, 173, 0.14);
      border-radius: 6px;
      padding: 9px 12px;
      display: grid;
      grid-template-columns: 110px 130px minmax(0, 1fr);
      gap: 12px;
      align-items: center;
      color: #dff6f4;
      background: rgba(4, 12, 14, 0.46);
      font: 600 12px/1.2 "JetBrains Mono", Consolas, monospace;
    }}
    body[data-page="app"][data-theme="light"] .event-ticker li {{
      color: #1d3037;
      background: rgba(255, 255, 255, 0.66);
      border-color: rgba(45, 64, 75, 0.14);
    }}
    .event-ticker span {{
      color: var(--accent-strong);
      text-transform: uppercase;
    }}
    .event-ticker strong,
    .event-ticker em {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-style: normal;
    }}
    @media (max-width: 1080px) {{
      .command-strip {{
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
      .ops-grid {{
        grid-template-columns: 1fr;
      }}
      .usa-map {{
        height: 54vh;
      }}
    }}
    @media (max-width: 760px) {{
      main {{ padding: 16px; }}
      header {{ align-items: flex-start; flex-direction: column; }}
      body[data-page="app"] .cloud-header {{
        grid-template-columns: 1fr;
      }}
      body[data-page="app"] .cloud-header .toolbar,
      .fifth-third-logo {{
        justify-self: start;
      }}
      .grid {{ grid-template-columns: 1fr; }}
      .span-2, .span-3, .span-4, .span-6, .span-12 {{ grid-column: span 1; }}
      table {{ display: block; overflow-x: auto; white-space: nowrap; }}
      .tree-shell {{ grid-template-columns: 1fr; }}
      .command-strip,
      .gateway-toolbar {{
        grid-template-columns: 1fr;
      }}
      .usa-map {{
        min-height: 320px;
        height: 48vh;
      }}
      .inspector-head,
      .panel-title {{
        flex-direction: column;
      }}
      .event-ticker li {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body data-page="{page}" {body_attrs}>
  <a class="button secondary home-link" href="/app">Home</a>
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
  <header class="cloud-header">
    <div class="cloud-title">
      <span class="eyebrow">Edge to Cloud Operations</span>
      <h1>IOT Edge to Cloud</h1>
    </div>
    <div class="fifth-third-logo" aria-label="Fifth Third">
      <span class="fifth-third-logo-mark">53</span>
      <span>Fifth Third</span>
    </div>
    <div class="toolbar">
      <span id="identity"></span>
      <button id="theme-toggle" class="secondary" type="button" aria-pressed="false">Light Mode</button>
      <a id="admin-link" class="button secondary" href="/admin/users" hidden>Users</a>
      <button id="logout" class="secondary" type="button">Logout</button>
    </div>
  </header>
  <main class="cloud-main">
    <section class="command-strip" aria-label="Gateway metrics">
      <div id="metric-total" class="metric-card"><span>Total Gateways</span><strong>0</strong><em>Loading</em></div>
      <div id="metric-online" class="metric-card good"><span>Online</span><strong>0</strong><em>Loading</em></div>
      <div id="metric-stale" class="metric-card warn"><span>Stale</span><strong>0</strong><em>Loading</em></div>
      <div id="metric-offline" class="metric-card bad"><span>Offline</span><strong>0</strong><em>Loading</em></div>
      <div id="metric-jobs" class="metric-card"><span>Recent Jobs</span><strong>0</strong><em>Loading</em></div>
    </section>
    <section class="ops-grid">
      <div class="map-panel">
        <div class="panel-title">
          <div>
            <span class="eyebrow">USA Gateway Mesh</span>
            <h2>Cloud Service Monitor</h2>
          </div>
          <div class="map-toolbar" aria-label="Map zoom controls">
            <button id="map-zoom-out" class="map-tool-button" type="button" title="Zoom out">-</button>
            <span id="map-zoom-label" class="panel-counter">100%</span>
            <button id="map-zoom-in" class="map-tool-button" type="button" title="Zoom in">+</button>
            <button id="map-zoom-reset" class="map-tool-button" type="button" title="Reset zoom">Reset</button>
            <span id="map-node-count" class="panel-counter">0 visible nodes</span>
          </div>
        </div>
        <div class="usa-map" aria-label="USA gateway map">
          <div id="map-zoom-content" class="map-zoom-content">
            <svg id="usa-map-base" viewBox="0 0 960 560" role="img" aria-hidden="true">
              <g class="usa-fallback">
                <path class="usa-mainland" d="M113 184 L143 145 L196 136 L245 118 L302 92 L368 96 L426 116 L497 124 L554 148 L625 142 L690 162 L744 201 L781 218 L825 224 L852 255 L833 291 L806 312 L793 344 L758 356 L726 381 L683 395 L642 423 L586 430 L536 455 L464 450 L404 430 L348 418 L292 392 L239 359 L197 344 L174 315 L160 270 L129 235 Z"></path>
                <path class="usa-florida" d="M676 395 L704 420 L726 459 L747 512 L733 530 L708 488 L680 445 L656 418 Z"></path>
                <path class="usa-new-england" d="M817 224 L842 187 L876 203 L866 238 L847 265 L831 254 Z"></path>
                <path class="usa-great-lakes" d="M552 149 C574 128 607 128 626 145 C604 153 582 161 562 177 Z"></path>
                <path class="usa-inset" d="M118 407 L157 386 L205 396 L244 426 L226 460 L176 470 L131 451 Z"></path>
                <path class="usa-inset" d="M292 455 L314 448 L340 458 L360 474 L344 489 L313 480 Z"></path>
                <path class="usa-line" d="M189 348 C259 310 333 302 397 322 C461 342 514 386 582 382 C660 378 712 333 804 340"></path>
                <path class="usa-line" d="M300 94 C284 167 300 249 360 314 C392 349 420 390 464 450"></path>
                <path class="usa-line" d="M497 124 C478 206 509 295 582 382"></path>
                <path class="usa-line" d="M238 359 L244 118"></path>
                <path class="usa-line" d="M625 142 L642 423"></path>
              </g>
            </svg>
            <button id="bermuda-shark" class="bermuda-shark" type="button" aria-label="Get back to work" title="Get back to work"></button>
            <div id="gateway-map-nodes" class="map-node-layer"></div>
          </div>
        </div>
      </div>
      <aside id="gateway-inspector" class="inspector-panel">
        <div class="empty-state">Loading gateway telemetry...</div>
      </aside>
    </section>
    <section class="gateway-panel">
      <div class="panel-title">
        <div>
          <span class="eyebrow">Gateway Registry</span>
          <h2>Gateways</h2>
        </div>
        <span id="gateway-result-count" class="panel-counter">0 gateways</span>
      </div>
      <div class="gateway-toolbar">
        <input id="gateway-search" type="search" placeholder="Search gateway, site, status, address, host, notes">
        <div id="status" class="notice"></div>
      </div>
      <div class="table-wrap">
        <table class="gateway-table">
        <thead>
          <tr>
            <th><button class="sort-header" type="button" data-sort="gateway_id">Gateway</button></th>
            <th><button class="sort-header" type="button" data-sort="site">Site</button></th>
            <th><button class="sort-header" type="button" data-sort="address">Address</button></th>
            <th><button class="sort-header" type="button" data-sort="hostname">Hostname</button></th>
            <th><button class="sort-header" type="button" data-sort="status">Status</button></th>
            <th><button class="sort-header" type="button" data-sort="network_status_notes">Network<br>Notes</button></th>
            <th><button class="sort-header" type="button" data-sort="direct">Direct</button></th>
            <th><button class="sort-header" type="button" data-sort="configure">Configure</button></th>
          </tr>
        </thead>
        <tbody id="gateway-list"></tbody>
      </table>
      </div>
    </section>
    <section class="ticker-panel">
      <div class="panel-title">
        <div>
          <span class="eyebrow">Event Stream</span>
          <h2>Recent Jobs</h2>
        </div>
      </div>
      <ul id="event-ticker" class="event-ticker">
        <li>Loading cloud job activity...</li>
      </ul>
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
      <h2>Site Information</h2>
      <div class="grid">
        <div class="span-3"><label>Tunnel Status</label><pre id="tunnel-status">Loading...</pre></div>
        <div class="span-3"><label>Direct Connect</label><pre id="direct-connect-status">Loading...</pre></div>
        <div class="span-3"><label>Action</label><a id="direct-connect-link" class="button" href="#" hidden>Direct Connect</a></div>
      </div>
      <form id="site-info-form" class="grid">
        <div class="span-4">
          <label for="site-name">Site name</label>
          <input id="site-name" type="text" maxlength="200">
        </div>
        <div class="span-6">
          <label for="site-address-street">Street address</label>
          <input id="site-address-street" type="text" maxlength="255">
        </div>
        <div class="span-3">
          <label for="site-address-city">City</label>
          <input id="site-address-city" type="text" maxlength="120">
        </div>
        <div class="span-2">
          <label for="site-address-state">State</label>
          <input id="site-address-state" type="text" maxlength="80">
        </div>
        <div class="span-2">
          <label for="site-address-postal-code">ZIP</label>
          <input id="site-address-postal-code" type="text" maxlength="40">
        </div>
        <div class="span-2">
          <label for="site-latitude">Latitude</label>
          <input id="site-latitude" type="number" min="-90" max="90" step="0.000001">
        </div>
        <div class="span-2">
          <label for="site-longitude">Longitude</label>
          <input id="site-longitude" type="number" min="-180" max="180" step="0.000001">
        </div>
        <div class="span-4">
          <label for="direct-connect-host">Cradlepoint/direct-connect host</label>
          <input id="direct-connect-host" type="text" maxlength="255">
        </div>
        <div class="span-2">
          <label for="direct-connect-port">External port</label>
          <input id="direct-connect-port" type="number" min="1" max="65535" value="5002">
        </div>
        <div class="span-2">
          <label for="gateway-ui-port">Gateway UI port</label>
          <input id="gateway-ui-port" type="number" min="1" max="65535" value="5000">
        </div>
        <div class="span-3">
          <label for="store-hours-mf">Hours M-F</label>
          <input id="store-hours-mf" type="text" maxlength="120">
        </div>
        <div class="span-3">
          <label for="store-hours-sat">Hours Sat</label>
          <input id="store-hours-sat" type="text" maxlength="120">
        </div>
        <div class="span-3">
          <label for="store-hours-sun">Hours Sun</label>
          <input id="store-hours-sun" type="text" maxlength="120">
        </div>
        <div class="span-12">
          <label for="network-status-notes">Network status notes</label>
          <textarea id="network-status-notes" maxlength="500" rows="2"></textarea>
        </div>
        <div class="span-2">
          <button id="save-site-info" type="submit">Save site</button>
        </div>
      </form>
    </section>
    <section>
      <h2>Imported Commissioning Model</h2>
      <div class="notice">Use the edge commissioning UI for BACnet discovery and point selection, then import the approved JSON template here.</div>
      <form id="import-template-form" class="grid">
        <div class="span-6">
          <label for="template-file">Edge commissioning template JSON</label>
          <input id="template-file" type="file" accept="application/json,.json" required>
        </div>
        <div class="span-3">
          <button type="submit">Import template</button>
        </div>
      </form>
      <div id="import-result" class="detail-panel compact-panel" hidden></div>
      <div class="tree-shell">
        <div id="tree" class="tree-panel">Loading...</div>
        <aside>
          <div id="tree-details" class="detail-panel" hidden></div>
          <div id="selected-points-panel" class="detail-panel" hidden>
            <h2>Selected Imported Points</h2>
            <div id="selected-points-count" class="notice">No saved points selected.</div>
            <ul id="selected-points-list" class="selected-point-list"></ul>
            <button id="remove-selected-points" class="secondary" type="button" disabled>Remove selected points</button>
          </div>
        </aside>
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
      <h2>Cloud BACnet Diagnostics</h2>
      <div class="notice">Temporary diagnostics only. Normal commissioning should happen in the edge UI and be imported as a template.</div>
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


def tunnel_console_html(gateway_id: str) -> str:
    escaped_gateway_id = escape(gateway_id, quote=True)
    body = f"""
  <header>
    <div>
      <h1 id="gateway-title">{escaped_gateway_id} Remote Console</h1>
      <p class="muted">Cloud Tunnel is separate from Direct Connect, heartbeat, and job polling.</p>
    </div>
    <div class="toolbar">
      <span id="identity"></span>
      <a id="workspace-link" class="button secondary" href="/gateways/{escaped_gateway_id}">Workspace</a>
      <button id="logout" class="secondary" type="button">Logout</button>
    </div>
  </header>
  <main>
    <section>
      <h2>Tunnel Status</h2>
      <div class="grid">
        <div class="span-4"><label>Gateway</label><pre>{escaped_gateway_id}</pre></div>
        <div class="span-4"><label>Heartbeat</label><pre id="heartbeat-summary">Loading...</pre></div>
        <div class="span-4"><label>Cloud Tunnel</label><pre id="tunnel-summary">Loading...</pre></div>
      </div>
      <div id="status" class="notice">Loading tunnel status...</div>
      <div class="toolbar">
        <a id="direct-connect-link" class="button secondary" href="#" target="_blank" rel="noopener noreferrer" hidden>Direct Connect</a>
      </div>
    </section>
    <section>
      <h2>Remote Console</h2>
      <div class="notice">
        Open a short-lived authenticated tunnel session in a new tab for the full gateway UI.
      </div>
      <div class="toolbar">
        <button id="open-tunnel-console" type="button" disabled>Open Tunnel Console</button>
        <a id="tunnel-session-link" class="button secondary" href="#" target="_blank" rel="noopener noreferrer" hidden>Popup blocked? Open tunnel manually</a>
      </div>
    </section>
  </main>"""
    return _layout(
        "Gateway Tunnel - IOT Cloud Commissioning",
        body,
        "tunnel-console",
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

