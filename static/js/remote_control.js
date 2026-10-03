(function () {
  const root = document.getElementById("remote-center");
  if (!root) return;

  const API = {
    snapshot: "/api/remote_snapshot",
    status: "/api/remote/pc/status",
    wake: "/api/remote/pc/wake",
    shutdown: "/api/remote/pc/shutdown",
    devices: "/api/remote/devices",
  };

  const FAST_POLL_INTERVAL_MS = 2500;
  const FAST_POLL_MAX_MS = 180000;
  const IDLE_SYNC_INTERVAL_MS = 12000;

  const STATUS_TEXT = {
    checking: "Prüfe…",
    offline: "Offline",
    booting: "Bootet...",
    ready: "Bereit",
    error: "Fehler",
  };

  const state = {
    status: "checking",
    previousStatus: "checking",
    pendingAction: null,
    lastWakeAttempt: null,
    checkedAt: null,
    pollInterval: null,
    idleInterval: null,
    pollStartedAt: null,
    polling: false,
    fetchInFlight: false,
    devicesFetchInFlight: false,
    devicesActionInFlight: new Set(),
    devicesOptimistic: new Map(),
    devices: [],
    goveeBrightness: 72,
    goveeBrightnessTimer: null,
    goveeColorTimer: null,
    goveeColorPointer: { x: 0.82, y: 0.5 },
    goveeColorDragging: false,
    goveeColorExpanded: false,
  };

  const el = {
    chip: document.getElementById("remote-status-chip"),
    chipLabel: document.getElementById("remote-status-chip-label"),
    card: document.getElementById("remote-card"),
    powerButton: document.getElementById("remote-power-button"),
    stateHeadline: document.getElementById("remote-state-headline"),
    stateCopy: document.getElementById("remote-state-copy"),
    feedback: document.getElementById("remote-inline-feedback"),
    readyCtaWrap: document.getElementById("remote-ready-cta-wrap"),
    readyCta: document.querySelector("#remote-ready-cta-wrap .remote-ready-cta"),
    lastWake: document.getElementById("remote-last-wake"),
    checkedAt: document.getElementById("remote-checked-at"),
    pollingState: document.getElementById("remote-polling-state"),
    devicesChip: document.getElementById("remote-devices-chip"),
    devicesChipLabel: document.getElementById("remote-devices-chip-label"),
    roomOff: document.getElementById("remote-room-off"),
    goveeSlot: document.getElementById("remote-govee-slot"),
    deviceGrid: document.getElementById("remote-device-grid"),
    devicesFeedback: document.getElementById("remote-devices-feedback"),
  };

  function uiStatus() {
    if (state.pendingAction === "wake" && state.status !== "ready") return "booting";
    if (state.pendingAction === "shutdown" && state.status !== "offline") return "booting";
    return state.status;
  }

  function fmtTs(value) {
    if (!value) return "-";
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return "-";
    return dt.toLocaleString("de-DE", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  async function apiFetch(url, options) {
    const response = await fetch(url, {
      method: options && options.method ? options.method : "GET",
      headers: { "Content-Type": "application/json" },
      ...options,
    });

    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }

    if (!response.ok || !payload || payload.ok === false) {
      const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
      throw new Error(message);
    }

    return payload;
  }



  function setDevicesFeedback(text, type) {
    if (!el.devicesFeedback) return;
    el.devicesFeedback.textContent = text || "";
    el.devicesFeedback.classList.remove("is-error", "is-good");
    if (type === "error") el.devicesFeedback.classList.add("is-error");
    if (type === "good") el.devicesFeedback.classList.add("is-good");
  }

  function renderDevicesChip(kind, label) {
    if (!el.devicesChip || !el.devicesChipLabel) return;
    el.devicesChip.className = `remote-status-chip is-${kind}`;
    el.devicesChipLabel.textContent = label;
  }

  function boardReachabilityKind(total, reachable) {
    if (total <= 0) return "checking";
    if (reachable <= 0) return "error";
    return "ready";
  }

  function renderBoardReachability() {
    const deviceReachable = state.devices.filter(function (d) {
      return d.is_on !== null || d.reachable === true;
    }).length;
    const pcReachable = state.status && state.status !== "error" && state.status !== "checking" ? 1 : 0;
    const total = state.devices.length + 1;
    const reachable = deviceReachable + pcReachable;
    renderDevicesChip(boardReachabilityKind(total, reachable), `${reachable}/${total} erreichbar`);
  }

  function deviceStateLabel(device) {
    if (device.is_on === true) return "Ein";
    if (device.is_on === false) return "Aus";
    return "Unbekannt";
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function isGoveeDevice(device) {
    if (!device) return false;
    const id = String(device.id || "").toLowerCase();
    const alias = String(device.alias || "").toLowerCase();
    return id === "desk_strip" || alias.includes("govee");
  }

  function rgbToHex(color) {
    if (!color || typeof color !== "object") return "#78a0ff";
    return "#" + [color.r, color.g, color.b].map(function (value) {
      return Math.max(0, Math.min(255, Number(value) || 0)).toString(16).padStart(2, "0");
    }).join("");
  }

  function deviceIcon(device) {
    const id = String(device && device.id || "").toLowerCase();
    if (id.includes("monitor")) return '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>';
    if (id.includes("ventilator")) return '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="2"/><path d="M12 10c-1-4 1-7 4-6 3 1 2 5-2 7M14 12c4-1 7 1 6 4-1 3-5 2-7-2M11 14c-1 4-5 5-7 2-2-3 1-5 5-4"/></svg>';
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6M10 22h4M8.5 15.5A6 6 0 1 1 15.5 15.5c-.8.7-1.2 1.3-1.3 2.5h-4.4c-.1-1.2-.5-1.8-1.3-2.5Z"/></svg>';
  }

  function renderSmallDeviceTile(device) {
    const isOn = device.is_on === true;
    const isOff = device.is_on === false;
    const tileClass = isOn ? "is-on" : isOff ? "is-off" : "is-error";
    const busy = state.devicesActionInFlight.has(device.id);
    const modeClass = isOn ? "is-on" : isOff ? "is-offline" : "is-checking";
    const nextAction = isOn ? "off" : "on";
    const actionLabel = isOn ? "Ausschalten" : "Einschalten";
    const alias = escapeHtml(device.alias);
    const id = escapeHtml(device.id);

    return `
      <article class="remote-device-tile ${tileClass}" data-device="${id}" data-testid="remote-device-tile">
        <div class="remote-device-icon">${deviceIcon(device)}</div>
        <div class="remote-device-head">
          <h3 class="remote-device-name">${alias}</h3>
          <span class="remote-device-dot" aria-hidden="true"></span>
        </div>
        <div class="remote-device-state">${deviceStateLabel(device)}${device.reachable === false ? " · nicht erreichbar" : ""}</div>
        <div class="remote-device-actions">
          <button class="remote-power-btn remote-power-btn--mini ${modeClass}" data-action="${nextAction}" data-device="${id}" type="button" ${busy ? "disabled" : ""} aria-label="${actionLabel}">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 2v9" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
              <path d="M7.76 4.84a8 8 0 1 0 8.48 0" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
            </svg>
          </button>
        </div>
      </article>
    `;
  }

  function renderGoveeTile(device) {
    if (!el.goveeSlot) return;
    const isOn = device && device.is_on === true;
    const isOff = device && device.is_on === false;
    const busy = device ? state.devicesActionInFlight.has(device.id) : false;
    const nextAction = isOn ? "off" : "on";
    const actionLabel = isOn ? "Ausschalten" : "Einschalten";
    const chipLabel = isOn ? "Ein" : isOff ? "Aus" : "Unbekannt";
    const modeClass = isOn ? "is-on" : isOff ? "is-offline" : "is-checking";
    const safeId = escapeHtml(device ? device.id : "desk_strip");
    const liveBrightness = Number(device && (device.pending_brightness ?? device.last_brightness));
    const brightness = Number.isFinite(liveBrightness) ? Math.max(1, Math.min(100, Math.round(liveBrightness))) : state.goveeBrightness;
    const colorObj = (device && typeof device.pending_color === "object" && device.pending_color) || (device && typeof device.last_color === "object" && device.last_color) || null;
    const colorStyle = colorObj ? `rgb(${Number(colorObj.r) || 0}, ${Number(colorObj.g) || 0}, ${Number(colorObj.b) || 0})` : "rgba(255,255,255,0.22)";
    const colorHex = rgbToHex(colorObj);
    const colorLabel = colorObj ? "Farbe gewählt" : "Standardfarbe";
    const settingsOpen = state.goveeColorExpanded;

    el.goveeSlot.innerHTML = `
      <article class="remote-govee-tile ${isOn ? "is-on" : isOff ? "is-off" : "is-error"} ${settingsOpen ? "is-expanded" : ""}" data-device="${safeId}">
        <div class="remote-govee-summary">
          <div class="remote-device-icon remote-device-icon--govee"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 16c4-6 12-6 16 0M7 19c3-4 7-4 10 0M10 22c1-2 3-2 4 0"/><path d="M12 3v8"/></svg></div>
          <div class="remote-govee-name">
            <h3>Govee Licht</h3>
            <span>${chipLabel} · ${brightness}% · ${colorLabel}</span>
          </div>
          <button class="remote-govee-expand" type="button" aria-expanded="${settingsOpen}" data-govee-expand>${settingsOpen ? "Schließen" : "Einstellen"}</button>
          <button class="remote-power-btn remote-power-btn--mini remote-power-btn--govee ${modeClass}" data-action="${nextAction}" data-device="${safeId}" type="button" ${busy ? "disabled" : ""} aria-label="${actionLabel}">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v9" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><path d="M7.76 4.84a8 8 0 1 0 8.48 0" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
          </button>
        </div>
        <div class="remote-govee-settings" ${settingsOpen ? "" : "hidden"}>
          <div class="remote-govee-slider-row">
            <div class="remote-govee-slider-head"><span>Helligkeit</span><strong id="remote-govee-brightness-value">${brightness}%</strong></div>
            <input class="remote-govee-slider" id="remote-govee-brightness" type="range" min="1" max="100" value="${brightness}">
          </div>
          <div class="remote-govee-presets">
            <button class="remote-govee-preset" data-device="${safeId}" data-color-r="255" data-color-g="190" data-color-b="140" type="button">Warm</button>
            <button class="remote-govee-preset" data-device="${safeId}" data-color-r="255" data-color-g="244" data-color-b="214" type="button">Klar</button>
            <button class="remote-govee-preset" data-device="${safeId}" data-color-r="120" data-color-g="160" data-color-b="255" type="button">Fokus</button>
            <button class="remote-govee-preset" data-device="${safeId}" data-color-r="255" data-color-g="90" data-color-b="40" type="button">Abend</button>
          </div>
          <label class="remote-native-color" title="Eigene Farbe wählen"><input id="remote-govee-color" type="color" value="${colorHex}" data-device="${safeId}" aria-label="Eigene Farbe wählen"><span class="remote-native-color__swatch" style="background:${escapeHtml(colorStyle)}" aria-hidden="true"></span></label>
        </div>
      </article>
    `;
  }

  function getColorWheelCanvas() {
    if (!el.goveeSlot) return null;
    return el.goveeSlot.querySelector("#remote-color-wheel-canvas");
  }

  function getGoveeDevice() {
    return state.devices.find(isGoveeDevice) || null;
  }

  async function setGoveeBrightness(deviceId, brightness) {
    if (!deviceId) return;
    const clamped = Math.max(1, Math.min(100, Number(brightness) || 1));
    try {
      const payload = await apiFetch(`${API.devices}/${encodeURIComponent(deviceId)}/brightness`, {
        method: "POST",
        body: JSON.stringify({ brightness: clamped }),
      });
      if (payload && payload.queued) {
        setDevicesFeedback(`Govee Stripe: Helligkeit ${clamped}% vorgemerkt (wird beim Einschalten angewendet).`, "good");
      } else {
        setDevicesFeedback(`Govee Stripe: Helligkeit ${clamped}% gesetzt.`, "good");
      }
      fetchDevices({ quiet: true });
    } catch (err) {
      setDevicesFeedback(`Govee Stripe: Helligkeit fehlgeschlagen (${err.message}).`, "error");
    }
  }

  function queueGoveeBrightness(deviceId, brightness) {
    if (state.goveeBrightnessTimer) {
      window.clearTimeout(state.goveeBrightnessTimer);
      state.goveeBrightnessTimer = null;
    }
    state.goveeBrightnessTimer = window.setTimeout(function () {
      state.goveeBrightnessTimer = null;
      setGoveeBrightness(deviceId, brightness);
    }, 180);
  }

  function hsvToRgb(h, s, v) {
    const hh = ((h % 360) + 360) % 360;
    const c = v * s;
    const x = c * (1 - Math.abs((hh / 60) % 2 - 1));
    const m = v - c;
    let rp = 0;
    let gp = 0;
    let bp = 0;
    if (hh < 60) [rp, gp, bp] = [c, x, 0];
    else if (hh < 120) [rp, gp, bp] = [x, c, 0];
    else if (hh < 180) [rp, gp, bp] = [0, c, x];
    else if (hh < 240) [rp, gp, bp] = [0, x, c];
    else if (hh < 300) [rp, gp, bp] = [x, 0, c];
    else [rp, gp, bp] = [c, 0, x];
    return {
      r: Math.round((rp + m) * 255),
      g: Math.round((gp + m) * 255),
      b: Math.round((bp + m) * 255),
    };
  }

  function hueFromVector(dx, dy) {
    return (((Math.atan2(dy, dx) * 180) / Math.PI) + 360) % 360;
  }

  function drawColorWheel() {
    const canvas = getColorWheelCanvas();
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const size = Math.max(80, Math.round(Math.min(rect.width || 260, rect.height || 260)));
    if (canvas.width !== size || canvas.height !== size) {
      canvas.width = size;
      canvas.height = size;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const image = ctx.createImageData(size, size);
    const data = image.data;
    const radius = size / 2;
    const cx = radius;
    const cy = radius;

    for (let y = 0; y < size; y += 1) {
      for (let x = 0; x < size; x += 1) {
        const dx = x + 0.5 - cx;
        const dy = y + 0.5 - cy;
        const dist = Math.sqrt(dx * dx + dy * dy);
        const idx = (y * size + x) * 4;
        if (dist > radius) {
          data[idx + 3] = 0;
          continue;
        }
        const hue = hueFromVector(dx, dy);
        const sat = Math.max(0, Math.min(1, dist / radius));
        const rgb = hsvToRgb(hue, sat, 1);
        data[idx + 0] = rgb.r;
        data[idx + 1] = rgb.g;
        data[idx + 2] = rgb.b;
        data[idx + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
  }

  async function setGoveeColor(deviceId, rgb) {
    if (!deviceId || !rgb) return;
    try {
      const payload = await apiFetch(`${API.devices}/${encodeURIComponent(deviceId)}/color`, {
        method: "POST",
        body: JSON.stringify(rgb),
      });
      if (payload && payload.queued) {
        setDevicesFeedback("Govee Stripe: Farbe vorgemerkt (wird beim Einschalten angewendet).", "good");
      } else {
        setDevicesFeedback(`Govee Stripe: Farbe gesetzt (${rgb.r},${rgb.g},${rgb.b}).`, "good");
      }
      fetchDevices({ quiet: true });
    } catch (err) {
      setDevicesFeedback(`Govee Stripe: Farbe fehlgeschlagen (${err.message}).`, "error");
    }
  }

  function queueGoveeColor(deviceId, rgb) {
    if (state.goveeColorTimer) {
      window.clearTimeout(state.goveeColorTimer);
      state.goveeColorTimer = null;
    }
    state.goveeColorTimer = window.setTimeout(function () {
      state.goveeColorTimer = null;
      setGoveeColor(deviceId, rgb);
    }, 120);
  }

  function updateColorPickerVisual(xNorm, yNorm) {
    if (!el.goveeSlot) return;
    const picker = el.goveeSlot.querySelector(".remote-color-wheel__picker");
    const ring = getColorWheelCanvas();
    if (!picker || !ring) return;
    const rect = ring.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, xNorm));
    const y = Math.max(0, Math.min(1, yNorm));
    picker.style.left = `${x * rect.width - 9}px`;
    picker.style.top = `${y * rect.height - 9}px`;
  }

  function mapPointToRgb(clientX, clientY) {
    const ring = getColorWheelCanvas();
    if (!ring) return null;
    const rect = ring.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const dxRaw = clientX - cx;
    const dyRaw = clientY - cy;
    const radius = rect.width / 2;
    const rawDist = Math.sqrt(dxRaw * dxRaw + dyRaw * dyRaw);
    const scale = rawDist > radius && rawDist > 0 ? radius / rawDist : 1;
    const dx = dxRaw * scale;
    const dy = dyRaw * scale;
    const dist = Math.sqrt(dx * dx + dy * dy);
    const hue = hueFromVector(dx, dy);
    const sat = Math.max(0, Math.min(1, dist / radius));
    const rgb = hsvToRgb(hue, sat, 1.0);
    const xNorm = (dx / (2 * radius)) + 0.5;
    const yNorm = (dy / (2 * radius)) + 0.5;
    return { rgb, xNorm, yNorm };
  }

  function renderDevicesGrid() {
    if (!el.deviceGrid || !el.goveeSlot) return;
    if (!state.devices.length) {
      el.goveeSlot.innerHTML = '<article class="remote-govee-tile is-error"><div class="remote-govee-head"><div><p class="remote-tile__kicker">Atmosphäre</p><h3 class="remote-govee-title">Govee Licht</h3><div class="remote-govee-subtitle">Die Lichtsteuerung ist gerade nicht verfügbar.</div></div><span class="remote-status-chip is-error"><span class="remote-status-chip__dot"></span><span>Offline</span></span></div></article>';
      el.deviceGrid.innerHTML = '<div class="remote-device-state">Keine Geräte gefunden.</div>';
      return;
    }

    const govee = state.devices.find(isGoveeDevice) || null;
    if (govee) {
      const pending = Number(govee.pending_brightness);
      const last = Number(govee.last_brightness);
      if (Number.isFinite(pending) && pending >= 1 && pending <= 100) {
        state.goveeBrightness = Math.round(pending);
      } else if (Number.isFinite(last) && last >= 1 && last <= 100) {
        state.goveeBrightness = Math.round(last);
      }
    }
    const compactDevices = state.devices.filter(function (device) {
      return !isGoveeDevice(device);
    });

    renderGoveeTile(govee);

    const html = compactDevices.map(renderSmallDeviceTile).join("");

    el.deviceGrid.innerHTML = html || '<div class="remote-device-state">Keine weiteren Geräte gefunden.</div>';
  }

  function mergeOptimisticDevices(devices) {
    const now = Date.now();
    return (devices || []).map(function (device) {
      const pending = state.devicesOptimistic.get(String(device.id));
      if (!pending) return device;
      if (device.is_on === Boolean(pending.isOn)) {
        state.devicesOptimistic.delete(String(device.id));
        return device;
      }
      if (now > pending.expiresAt) {
        state.devicesOptimistic.delete(String(device.id));
        return device;
      }
      return {
        ...device,
        is_on: Boolean(pending.isOn),
        reachable: true,
        error: null,
        optimistic: true,
      };
    });
  }

  function updateDeviceOptimistic(deviceId, isOn) {
    let didUpdate = false;
    state.devicesOptimistic.set(String(deviceId), {
      isOn: Boolean(isOn),
      startedAt: Date.now(),
      expiresAt: Date.now() + 8000,
    });
    state.devices = state.devices.map(function (device) {
      if (String(device.id) !== String(deviceId)) return device;
      didUpdate = true;
      return {
        ...device,
        is_on: Boolean(isOn),
        reachable: true,
        error: null,
        optimistic: true,
      };
    });
    return didUpdate;
  }

  function restoreDeviceState(deviceId, previousDevice) {
    state.devicesOptimistic.delete(String(deviceId));
    if (!previousDevice) return;
    state.devices = state.devices.map(function (device) {
      if (String(device.id) !== String(deviceId)) return device;
      return previousDevice;
    });
  }

  async function fetchDevices(options) {
    const opts = options || {};
    const quiet = Boolean(opts.quiet);
    if (state.devicesFetchInFlight) return;
    state.devicesFetchInFlight = true;
    try {
      if (!quiet) renderDevicesChip("checking", "Lade…");
      const payload = await apiFetch(API.devices);
      state.devices = mergeOptimisticDevices(Array.isArray(payload.devices) ? payload.devices : []);
      renderDevicesGrid();
      renderBoardReachability();
    } catch (err) {
      renderDevicesChip("error", "Fehler");
      if (!quiet) setDevicesFeedback(`Geräte-Status fehlgeschlagen: ${err.message}`, "error");
    } finally {
      state.devicesFetchInFlight = false;
    }
  }

  async function fetchSnapshot(options) {
    const opts = options || {};
    const quiet = Boolean(opts.quiet);
    if (state.fetchInFlight || state.devicesFetchInFlight) return;
    state.fetchInFlight = true;
    state.devicesFetchInFlight = true;
    try {
      if (!quiet) renderDevicesChip("checking", "Lade…");
      const payload = await apiFetch(API.snapshot);
      if (payload && payload.status) {
        applyServerState(payload.status);
        render();
      }
      state.devices = mergeOptimisticDevices(Array.isArray(payload?.devices) ? payload.devices : []);
      renderDevicesGrid();
      renderBoardReachability();
      const status = uiStatus();
      if (status === "booting") startPolling();
      else stopPolling();
    } catch (err) {
      if (!quiet) setFeedback(`Snapshot fehlgeschlagen: ${err.message}`, "error");
    } finally {
      state.fetchInFlight = false;
      state.devicesFetchInFlight = false;
    }
  }

  async function setDevicePower(deviceId, action) {
    if (!deviceId || !action) return;
    const key = String(deviceId);
    if (state.devicesActionInFlight.has(key)) return;
    const previousDevice = state.devices.find(function (device) {
      return String(device.id) === key;
    });
    state.devicesActionInFlight.add(key);
    updateDeviceOptimistic(key, action === "on");
    setDevicesFeedback(`${deviceId}: ${action === "on" ? "schalte ein..." : "schalte aus..."}`, null);
    renderDevicesGrid();
    renderBoardReachability();

    try {
      await apiFetch(`${API.devices}/${encodeURIComponent(deviceId)}/${encodeURIComponent(action)}`, { method: "POST" });
      setDevicesFeedback(`${deviceId}: ${action === "on" ? "eingeschaltet" : "ausgeschaltet"}.`, "good");
      await fetchSnapshot({ quiet: true });
    } catch (err) {
      restoreDeviceState(key, previousDevice);
      setDevicesFeedback(`${deviceId}: ${err.message}`, "error");
    } finally {
      state.devicesActionInFlight.delete(key);
      renderDevicesGrid();
      renderBoardReachability();
    }
  }

  function pressPowerButton(button) {
    if (!button) return;
    if (button._remotePressTimer) {
      window.clearTimeout(button._remotePressTimer);
    }
    button.classList.remove("is-pressing");
    void button.offsetWidth;
    button.classList.add("is-pressing");
    button._remotePressTimer = window.setTimeout(function () {
      button.classList.remove("is-pressing");
      button._remotePressTimer = null;
    }, 360);
  }

  function setFeedback(text, type) {
    el.feedback.textContent = text || "";
    el.feedback.classList.remove("is-error", "is-good");
    if (type === "error") el.feedback.classList.add("is-error");
    if (type === "good") el.feedback.classList.add("is-good");
  }

  function setPolling(active) {
    state.polling = Boolean(active);
    el.pollingState.textContent = state.polling ? "Aktiv" : "Inaktiv";
  }

  function stopPolling() {
    if (state.pollInterval) {
      clearInterval(state.pollInterval);
      state.pollInterval = null;
    }
    state.pollStartedAt = null;
    setPolling(false);
  }

  function startPolling() {
    if (state.pollInterval) return;
    state.pollStartedAt = Date.now();
    state.pollInterval = window.setInterval(function () {
      fetchSnapshot({ quiet: true });
    }, FAST_POLL_INTERVAL_MS);
    setPolling(true);
  }

  function startIdleSync() {
    if (state.idleInterval) return;
    state.idleInterval = window.setInterval(function () {
      fetchSnapshot({ quiet: true });
    }, IDLE_SYNC_INTERVAL_MS);
  }

  function applyServerState(payload) {
    const incomingStatus = payload.status || "checking";
    state.previousStatus = state.status || "checking";
    state.status = incomingStatus;
    state.lastWakeAttempt = payload.last_wake_attempt || state.lastWakeAttempt;
    state.checkedAt = payload.checked_at || null;

    if (state.pendingAction === "wake" && state.status === "ready") {
      state.pendingAction = null;
      setFeedback("PC ist bereit.", "good");
    }
    if (state.pendingAction === "shutdown" && state.status === "offline") {
      state.pendingAction = null;
      setFeedback("PC ist jetzt offline.", "good");
    }
  }

  function powerMode(status) {
    if (status === "checking") return "checking";
    if (status === "ready") return "on";
    if (status === "booting") return "busy";
    if (status === "error") return "error";
    return "off";
  }

  function renderStateText(status) {
    if (status === "checking") {
      el.stateHeadline.textContent = "Prüfe Status…";
      el.stateCopy.textContent = "Verbindung wird gerade verifiziert.";
      return;
    }
    if (status === "ready") {
      el.stateHeadline.textContent = "Bereit";
      el.stateCopy.textContent = "Windows läuft. Tippe für Remote-Zugriff.";
      return;
    }
    if (status === "booting") {
      if (state.pendingAction === "shutdown") {
        el.stateHeadline.textContent = "Fährt herunter...";
        el.stateCopy.textContent = "Shutdown wurde ausgelöst. Warte auf Offline-Status.";
      } else {
        el.stateHeadline.textContent = "Bootet...";
        el.stateCopy.textContent = "Windows fährt hoch und RDP wird vorbereitet.";
      }
      return;
    }
    if (status === "error") {
      el.stateHeadline.textContent = "Fehler";
      el.stateCopy.textContent = "Status konnte nicht sicher aktualisiert werden.";
      return;
    }
    el.stateHeadline.textContent = "Offline";
    el.stateCopy.textContent = "PC ist offline. Tippe auf Power zum Starten.";
  }

  function renderPowerButton(status) {
    const mode = powerMode(status);
    el.powerButton.className = `remote-power-btn is-${mode}`;
    el.powerButton.disabled = mode === "busy" || status === "checking";
  }

  function renderStatusChip(status) {
    el.chip.className = `remote-status-chip is-${status}`;
    el.chipLabel.textContent = STATUS_TEXT[status] || STATUS_TEXT.offline;
  }

  function renderReadyCta(status) {
    if (!el.readyCtaWrap) return;
    el.readyCtaWrap.classList.toggle("is-visible", status === "ready");
  }

  function render() {
    const status = uiStatus();
    renderStatusChip(status);
    renderPowerButton(status);
    renderStateText(status);
    renderReadyCta(status);
    if (el.card) {
      el.card.classList.toggle("is-ready", status === "ready");
    }
    el.lastWake.textContent = fmtTs(state.lastWakeAttempt);
    el.checkedAt.textContent = fmtTs(state.checkedAt);
  }

  async function pollStatus() {
    await fetchStatus({ quiet: true });
  }

  async function fetchStatus(options) {
    const opts = options || {};
    const quiet = Boolean(opts.quiet);
    if (state.fetchInFlight) return;
    state.fetchInFlight = true;
    try {
      const payload = await apiFetch(API.status);
      applyServerState(payload);
      render();
      renderBoardReachability();
      const status = uiStatus();
      if (status === "booting") startPolling();
      else stopPolling();
      if (state.pollStartedAt && Date.now() - state.pollStartedAt > FAST_POLL_MAX_MS) {
        stopPolling();
      }
    } catch (err) {
      if (!quiet) {
        state.status = "error";
        render();
        setFeedback(`Statusprüfung fehlgeschlagen: ${err.message}`, "error");
      }
    } finally {
      state.fetchInFlight = false;
    }
  }

  async function triggerWake() {
    state.pendingAction = "wake";
    state.status = "booting";
    setFeedback("Wake-on-LAN wird gesendet...", null);
    render();

    try {
      const payload = await apiFetch(API.wake, { method: "POST" });
      state.lastWakeAttempt = payload.last_wake_attempt || new Date().toISOString();
      state.checkedAt = payload.checked_at || new Date().toISOString();
      state.status = "booting";
      render();
      setFeedback("Wake gesendet. Warte auf Bereitschaft...", "good");
      startPolling();
      window.setTimeout(function () {
        fetchSnapshot({ quiet: true });
      }, 650);
    } catch (err) {
      state.pendingAction = null;
      state.status = "error";
      render();
      setFeedback(`Wake fehlgeschlagen: ${err.message}`, "error");
    }
  }

  async function triggerShutdown() {
    state.pendingAction = "shutdown";
    state.status = "booting";
    setFeedback("Shutdown wird über den lokalen Listener gesendet...", null);
    render();

    try {
      await apiFetch(API.shutdown, { method: "POST" });
      setFeedback("Shutdown ausgelöst. Warte auf Offline-Status...", "good");
      startPolling();
      window.setTimeout(function () {
        fetchSnapshot({ quiet: true });
      }, 850);
    } catch (err) {
      state.pendingAction = null;
      await fetchSnapshot();
      setFeedback(`Shutdown fehlgeschlagen: ${err.message}`, "error");
    }
  }

  async function handlePowerAction() {
    const status = uiStatus();
    if (status === "booting" || status === "checking") return;
    if (status === "offline") {
      await triggerWake();
      return;
    }
    if (status === "ready") {
      await triggerShutdown();
      return;
    }
    if (status === "error") {
      await fetchSnapshot();
    }
  }

  if (el.powerButton) {
    el.powerButton.addEventListener("click", handlePowerAction);
  }

  function onAnyDeviceClick(event) {
    const target = event.target;
    if (!target) return;
    const expandButton = target.closest("[data-govee-expand]");
    if (expandButton) {
      state.goveeColorExpanded = !state.goveeColorExpanded;
      renderGoveeTile(getGoveeDevice());
      return;
    }
    const presetButton = target.closest("button[data-color-r][data-color-g][data-color-b][data-device]");
    if (presetButton) {
      const rgb = {
        r: Math.max(0, Math.min(255, Number(presetButton.getAttribute("data-color-r")) || 0)),
        g: Math.max(0, Math.min(255, Number(presetButton.getAttribute("data-color-g")) || 0)),
        b: Math.max(0, Math.min(255, Number(presetButton.getAttribute("data-color-b")) || 0)),
      };
      setGoveeColor(presetButton.getAttribute("data-device"), rgb);
      return;
    }
    const actionButton = target.closest("button[data-device][data-action]");
    if (actionButton) {
      setDevicePower(actionButton.getAttribute("data-device"), actionButton.getAttribute("data-action"));
    }
  }

  if (el.deviceGrid) el.deviceGrid.addEventListener("click", onAnyDeviceClick);
  if (el.goveeSlot) el.goveeSlot.addEventListener("click", onAnyDeviceClick);
  if (el.roomOff) {
    el.roomOff.addEventListener("click", async function () {
      const activeDevices = state.devices.filter(function (device) {
        return !isGoveeDevice(device) && device.is_on === true;
      });
      if (!activeDevices.length) {
        setDevicesFeedback("Alle Raumgeräte sind bereits aus.", "good");
        return;
      }
      el.roomOff.disabled = true;
      await Promise.all(activeDevices.map(function (device) {
        return setDevicePower(device.id, "off");
      }));
      el.roomOff.disabled = false;
    });
  }
  if (el.deviceGrid) {
    el.deviceGrid.addEventListener("pointerdown", function (event) {
      const button = event.target && event.target.closest("button[data-device][data-action]");
      if (button) pressPowerButton(button);
    }, true);
  }
  if (el.goveeSlot) {
    el.goveeSlot.addEventListener("pointerdown", function (event) {
      const button = event.target && event.target.closest("button[data-device][data-action]");
      if (button) pressPowerButton(button);
    }, true);
  }

  if (el.goveeSlot) {
    el.goveeSlot.addEventListener("toggle", function (event) {
      const panel = event.target;
      if (!panel || !panel.classList || !panel.classList.contains("remote-govee-color-panel")) return;
      state.goveeColorExpanded = panel.open;
      window.requestAnimationFrame(function () {
        drawColorWheel();
        updateColorPickerVisual(state.goveeColorPointer.x, state.goveeColorPointer.y);
      });
    });

    el.goveeSlot.addEventListener("input", function (event) {
      const target = event.target;
      if (!target || target.id !== "remote-govee-brightness") return;
      state.goveeBrightness = Math.max(1, Math.min(100, Number(target.value) || 1));
      const val = el.goveeSlot.querySelector("#remote-govee-brightness-value");
      if (val) val.textContent = `${state.goveeBrightness}%`;
      const govee = getGoveeDevice();
      if (govee) queueGoveeBrightness(govee.id, state.goveeBrightness);
    });

    el.goveeSlot.addEventListener("change", function (event) {
      const target = event.target;
      if (!target || target.id !== "remote-govee-color") return;
      const hex = String(target.value || "").replace("#", "");
      if (!/^[0-9a-f]{6}$/i.test(hex)) return;
      setGoveeColor(target.getAttribute("data-device"), {
        r: parseInt(hex.slice(0, 2), 16),
        g: parseInt(hex.slice(2, 4), 16),
        b: parseInt(hex.slice(4, 6), 16),
      });
    });
  }

  if (el.goveeSlot) {
    el.goveeSlot.addEventListener("pointerdown", function (event) {
      const ring = event.target && event.target.closest("#remote-color-wheel-canvas");
      if (!ring) return;
      state.goveeColorDragging = true;
      const mapped = mapPointToRgb(event.clientX, event.clientY);
      const govee = getGoveeDevice();
      if (!mapped || !govee) return;
      state.goveeColorPointer = { x: mapped.xNorm, y: mapped.yNorm };
      updateColorPickerVisual(mapped.xNorm, mapped.yNorm);
      queueGoveeColor(govee.id, mapped.rgb);
    });

    el.goveeSlot.addEventListener("pointermove", function (event) {
      if (!state.goveeColorDragging) return;
      const mapped = mapPointToRgb(event.clientX, event.clientY);
      const govee = getGoveeDevice();
      if (!mapped || !govee) return;
      state.goveeColorPointer = { x: mapped.xNorm, y: mapped.yNorm };
      updateColorPickerVisual(mapped.xNorm, mapped.yNorm);
      queueGoveeColor(govee.id, mapped.rgb);
    });

    el.goveeSlot.addEventListener("pointerup", function () {
      state.goveeColorDragging = false;
    });

    el.goveeSlot.addEventListener("pointercancel", function () {
      state.goveeColorDragging = false;
    });
  }

  render();
  renderDevicesGrid();
  drawColorWheel();
  updateColorPickerVisual(state.goveeColorPointer.x, state.goveeColorPointer.y);
  startIdleSync();
  fetchSnapshot({ quiet: true });

  window.addEventListener("resize", function () {
    drawColorWheel();
    updateColorPickerVisual(state.goveeColorPointer.x, state.goveeColorPointer.y);
  });
})();
