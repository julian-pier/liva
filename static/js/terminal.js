/* static/js/terminal.js
   Multi-window Windows-11-ish CMD for ABOUT
   - Multiple windows at once (new instance per open)
   - Rounded corners (CSS-side)
   - Draggable/resizable per window, smooth, focus brings to front
   - Public: window.openLIVACmdAbout() -> opens NEW window each time
*/

(function () {
  "use strict";

  const PROMPT = "C:\\\\Users\\\\LIVA>";
  const WINDOWS_BANNER = [
    "Microsoft Windows [Version 10.0.19045.4046]",
    "(c) Microsoft Corporation. All rights reserved.",
    "",
  ];
  const RESTART_PASSWORD = "1909";
  const ABOUT_PREFETCH_TTL_MS = 15000;
  let aboutStatsCache = null;
  let aboutStatsCacheTs = 0;
  let aboutStatsInflight = null;

  // -----------------------------
  // formatting helpers
  // -----------------------------
  function fmtInt(n, fallback = "—") {
    if (n === null || n === undefined || Number.isNaN(n)) return fallback;
    const v = Number(n);
    if (!Number.isFinite(v)) return fallback;
    return Math.round(v).toLocaleString("en-US");
  }

  function safeStr(x, fallback = "—") {
    if (x === null || x === undefined) return fallback;
    const s = String(x).trim();
    return s.length ? s : fallback;
  }

  async function fetchAboutStats() {
    const res = await fetch("/api/about_stats", { credentials: "same-origin", cache: "no-store" });
    if (!res.ok) throw new Error(`about_stats HTTP ${res.status}`);
    return res.json();
  }

  function getCachedAboutStats() {
    if (!aboutStatsCache) return null;
    if ((Date.now() - aboutStatsCacheTs) > ABOUT_PREFETCH_TTL_MS) return null;
    return aboutStatsCache;
  }

  function storeAboutStats(stats) {
    aboutStatsCache = stats || null;
    aboutStatsCacheTs = aboutStatsCache ? Date.now() : 0;
    return aboutStatsCache;
  }

  function loadAboutStats() {
    const cached = getCachedAboutStats();
    if (cached) return Promise.resolve(cached);
    if (aboutStatsInflight) return aboutStatsInflight;
    aboutStatsInflight = fetchAboutStats()
      .then((stats) => storeAboutStats(stats))
      .finally(() => {
        aboutStatsInflight = null;
      });
    return aboutStatsInflight;
  }

  // -----------------------------
  // startup lines
  // -----------------------------
  function buildStartupLines(stats) {
    const owner = safeStr(stats.owner, "LIVA operator");
    const build = safeStr(stats.build, "local-first / offline-capable");
    const codebaseLines = fmtInt(stats.codebase_lines);
    const storedValues = fmtInt(stats.stored_values);
    const activeProfiles = fmtInt(stats.active_profiles);
    const rawDataIntegrity = safeStr(stats.data_integrity, "verified");
    const dataIntegrity = rawDataIntegrity === "verified" ? "verified with warnings" : rawDataIntegrity;

    const trackedSince = safeStr(stats.tracked_since, "n/a");
    const firstRecord = safeStr(stats.first_record, "n/a");
    const lastRecord = safeStr(stats.last_record, "n/a");

    const exportMode = safeStr(stats.export_mode, "available (CSV/JSON)");
    const generatedAt = safeStr(stats.generated_at, "n/a");

    const addField = (lines, label, value) => {
      lines.push(`${label.padEnd(23, ".")} ${value}`);
    };

    const addWrapped = (lines, label, first, rest = []) => {
      addField(lines, label, first);
      const indent = " ".repeat(25);
      rest.forEach((item) => lines.push(`${indent}${item}`));
    };

    const lines = [];
    lines.push("");
    lines.push("LIVA — SYSTEM OVERVIEW");
    addField(lines, "Owner", owner);
    addField(lines, "System", "LIVA / LIVA");
    addField(lines, "Build", build);
    addField(lines, "Host", "LIVA mini-PC");
    addField(lines, "Runtime", "Flask / Python / SQLite");
    addField(lines, "Interface", "Web UI, fake CMD, GPT Actions, Telegram");
    addField(lines, "Codebase", `${codebaseLines} lines`);
    addField(lines, "Stored values", storedValues);
    addField(lines, "Active profiles", activeProfiles);
    addField(lines, "Data integrity", dataIntegrity);
    lines.push("");
    lines.push("MISSION");
    addField(lines, "Purpose", "personal coaching + tracking system");
    addField(lines, "Primary role", "training decisions, progression, recovery, planning");
    addField(lines, "Design principle", "own data > generic fitness app");
    addField(lines, "Decision layer", "LIVA / CORE");
    addField(lines, "Coach mode", "data-first, context-aware, explainable");
    lines.push("");
    lines.push("TRACKED DOMAINS");
    lines.push("");
    addWrapped(lines, "Strength Training", "workouts, sessions, exercises, sets, reps, weight, RPE", [
      "tonnage, volume, top sets, backoff sets, e1RM, PRs",
      "progression, exercise history, device variants",
    ]);
    lines.push("");
    addWrapped(lines, "Exercise Mapping", "canonical exercise names, aliases, device types", [
      "muscle groups, movement patterns, unmapped sets",
      "duplicate names, stale exercises",
    ]);
    lines.push("");
    addWrapped(lines, "Progression", "suggested loads, previous performance, rep ranges", [
      "progression attempts, repeats, holds, deload signals",
      "exercise-specific trends",
    ]);
    lines.push("");
    addWrapped(lines, "Muscle Balance", "chest, lats, upper back, shoulders, biceps, triceps", [
      "quads, hamstrings, calves, core",
      "weekly effective sets, underdosed areas, drop-off",
    ]);
    lines.push("");
    addWrapped(lines, "Running", "runs, distance, duration, pace, avg bpm, max bpm", [
      "longest run, best 5k, best 10k, season highs",
    ]);
    lines.push("");
    addWrapped(lines, "Ergo / Cardio", "ergo sessions, duration, watt, cadence, calculated distance", [
      "zone distribution, cardio load, weekly volume",
    ]);
    lines.push("");
    addWrapped(lines, "Recovery", "RMSSD, SDNN, AVNN, resting bpm, HRV baseline", [
      "readiness, recovery trend, fatigue flags",
    ]);
    lines.push("");
    addWrapped(lines, "Sleep / Daily State", "sleep quality, wakeup time, wakeup variance", [
      "motivation, sickness, stress notes, daily form",
    ]);
    lines.push("");
    addWrapped(lines, "Body Metrics", "bodyweight, weight trend, trend direction", [
      "cut/bulk context, maintenance estimate",
    ]);
    lines.push("");
    addWrapped(lines, "Nutrition", "calories, protein, carbs, fats, sugar", [
      "meals, meal timing, meal reminders, maintenance",
      "tracking freshness, macro consistency",
    ]);
    lines.push("");
    addWrapped(lines, "Planning", "training plans, active blocks, planned sessions", [
      "missed sessions, streaks, start dates, overrides",
      "calendar conflicts, next likely session",
    ]);
    lines.push("");
    addWrapped(lines, "CORE / LIVA", "daily mode, Heavy/Normal/Light decisions", [
      "overrides, sidecar state, objections, confidence",
      "readiness logic, training recommendation",
    ]);
    lines.push("");
    addWrapped(lines, "Calendar", "Google Calendar sync, training events", [
      "time windows, conflicts, planned training slots",
      "extended event metadata",
    ]);
    lines.push("");
    addWrapped(lines, "Telegram", "meal reminders, system messages", [
      "reminder timing, notification status",
    ]);
    lines.push("");
    addWrapped(lines, "Memory / Dossier", "athlete profile, long-term notes, training identity", [
      "preferences, rules, context, system assumptions",
    ]);
    lines.push("");
    addWrapped(lines, "Remote / Smart Home", "PC status, monitor status, wake-on-LAN", [
      "smart plugs, lights, late-PC automation",
      "remote dashboard state",
    ]);
    lines.push("");
    addWrapped(lines, "Dashboards", "training dashboard, cardio dashboard, CORE board", [
      "memory view, remote view, morning check-in boards",
    ]);
    lines.push("");
    addField(lines, "Exports", "CSV, JSON, internal reports");
    addField(lines, "Backups", "database backups, local snapshots");
    addField(lines, "Actions API", "LIVA Actions V2, read endpoints, write commands");
    addField(lines, "Offline status", "ready, no external dependency for core data");
    lines.push("");
    lines.push("DATA SOURCES");
    addField(lines, "Primary database", "SQLite");
    addField(lines, "Training DB", "active");
    addField(lines, "CORE DB", "active");
    addField(lines, "HRV DB", "active");
    addField(lines, "Nutrition DB", "active");
    addField(lines, "Runs/Cardio DB", "active");
    addField(lines, "Plans DB", "active");
    addField(lines, "Memory DB", "active");
    lines.push("");
    lines.push("CONNECTED SYSTEMS");
    addField(lines, "Google Calendar", "connected");
    addField(lines, "Telegram Hub", "connected");
    addField(lines, "GPT Actions", "connected");
    addField(lines, "Tailscale", "available");
    addField(lines, "Smart Home Layer", "partial / local");
    addField(lines, "Export Layer", "available");
    lines.push("");
    lines.push("DATA QUALITY");
    addField(lines, "First record", firstRecord);
    addField(lines, "Last record", lastRecord);
    addField(lines, "Tracked since", trackedSince);
    addField(lines, "HRV coverage", "active");
    addField(lines, "Training coverage", "active");
    addField(lines, "Nutrition coverage", "partial");
    addField(lines, "Cardio coverage", "active");
    addField(lines, "Bodyweight coverage", "active");
    lines.push("");
    addWrapped(lines, "Known warnings", "exercise aliases need cleanup", [
      "duplicate PR entries possible",
      "unmapped muscle sets exist",
      "hard-day classifier too broad",
    ]);
    lines.push("");
    lines.push("SYSTEM STATUS");
    addField(lines, "LIVA", "online");
    addField(lines, "LIVA Coach", "active");
    addField(lines, "CORE engine", "active");
    addField(lines, "Database freshness", "fresh");
    addField(lines, "Backup status", safeStr(stats.backup_status, "OK"));
    addField(lines, "Backup last", safeStr(stats.backup_last, "n/a"));
    addField(lines, "Export mode", exportMode);
    addField(lines, "Offline mode", "ready");
    lines.push("");
    lines.push("CURRENT CHAPTER");
    lines.push("From tracking system to decision system.");
    lines.push("");
    addField(lines, "Generated at", generatedAt);
    return lines;
  }

  // -----------------------------
  // Rattle print (8–9s)
  // -----------------------------
  function runRattlePrint(state, lines, seconds = 8.6, onDone = null, opts = null) {
    const body = state.body;
    const out = state.outputEl || body;
    const append = Boolean(opts && opts.append);
    state.running = true;
    if (!append) out.textContent = "";

    const start = performance.now();
    const durationMs = Math.max(600, Math.min(9000, seconds * 1000));
    const deadline = start + durationMs;

    let idx = 0;

    const MIN_DELAY = 12;
    const MAX_DELAY = 80;
    const MAX_BURST = 12;
    const STALL_PROB = 0.07;
    const STALL_MIN = 120;
    const STALL_MAX = 320;

    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
    const schedule = (d) => window.setTimeout(() => requestAnimationFrame(tick), d);

    function tick(now) {
      if (!state.running) return;

      const remainingLines = lines.length - idx;
      const remainingMs = Math.max(1, deadline - now);

      if (remainingLines <= 0) {
        state.running = false;
        if (typeof onDone === "function") onDone();
        return;
      }

      if (remainingMs > 700 && Math.random() < STALL_PROB) {
        if (Math.random() < 0.35 && remainingLines > 0) {
          out.textContent += lines[idx++] + "\n";
          body.scrollTop = body.scrollHeight;
        }
        schedule(clamp(STALL_MIN + Math.random() * (STALL_MAX - STALL_MIN), MIN_DELAY, MAX_DELAY + STALL_MAX));
        return;
      }

      const baseLps = remainingLines / remainingMs;
      const virtualFrameMs = 16 + Math.random() * 22;

      let mood = 0.75 + Math.random() * 0.9;
      if (Math.random() < 0.12) mood *= 1.8 + Math.random() * 1.6;
      if (Math.random() < 0.10) mood *= 0.35 + Math.random() * 0.35;

      let want = Math.round(baseLps * virtualFrameMs * mood);
      want = clamp(want, 1, MAX_BURST);

      if (remainingMs < 600) {
        want = clamp(Math.ceil(remainingLines / Math.max(1, Math.floor(remainingMs / 30))), 1, MAX_BURST);
      }

      for (let k = 0; k < want && idx < lines.length; k++) out.textContent += lines[idx++] + "\n";
      body.scrollTop = body.scrollHeight;

      const afterRemainingLines = lines.length - idx;
      const afterRemainingMs = Math.max(1, deadline - now);
      const msPerLine = afterRemainingMs / Math.max(1, afterRemainingLines);
      let delay = msPerLine * (0.35 + Math.random() * 0.55);
      delay += (Math.random() - 0.5) * 18;
      delay = clamp(delay, MIN_DELAY, MAX_DELAY);
      if (afterRemainingMs < 450) delay = clamp(delay * 0.55, MIN_DELAY, MAX_DELAY);

      schedule(delay);
    }

    requestAnimationFrame(tick);
  }

  async function execCommand(cmd) {
    const res = await fetch("/api/terminal/exec", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd }),
    });
    if (!res.ok) throw new Error(`terminal_exec HTTP ${res.status}`);
    return res.json();
  }

  async function runAbout(state) {
    showPrompt(state, false);
    (state.outputEl || state.body).textContent += "\nLIVA - SYSTEM OVERVIEW\nLoading system profile...\n\n";
    state.body.scrollTop = state.body.scrollHeight;
    try {
      const stats = await loadAboutStats();
      const lines = buildStartupLines(stats);
      runRattlePrint(
        state,
        lines,
        8.6,
        () => {
          state.running = false;
          ensurePrompt(state);
          showPrompt(state, true);
        },
        { append: false }
      );
    } catch (err) {
      const lines = ["ERROR", String(err && err.message ? err.message : err)];
      const seconds = computeRattleSecondsForLines(lines);
      runRattlePrint(
        state,
        lines,
        seconds,
        () => {
          state.running = false;
          ensurePrompt(state);
          showPrompt(state, true);
        },
        { append: false }
      );
    }
  }

  async function requestServiceRestart() {
    // Fire-and-forget: the service may restart and drop the connection mid-request.
    try {
      await fetch("/api/system/restart_liva", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ unit: "liva.service", password: RESTART_PASSWORD }),
        keepalive: true,
      });
    } catch {
      // ignore; reconnect loop will handle downtime
    }
  }

  async function waitForServiceOnline(timeoutMs = 30000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      try {
        const res = await fetch("/api/about_stats", { credentials: "same-origin", cache: "no-store" });
        if (res.ok) return true;
      } catch {
        // ignore
      }
      await new Promise((r) => setTimeout(r, 800));
    }
    return false;
  }

  function parseRestartCommand(cmd) {
    const norm = String(cmd || "").trim().replace(/\s+/g, " ");
    const low = norm.toLowerCase();

    if (!low) return null;

    // restart [password]
    if (low === "restart") return { base: "restart", password: null };
    if (low.startsWith("restart ")) return { base: "restart", password: norm.slice(8).trim() || null };

    // (sudo) systemctl restart liva.service [password]
    const m = low.match(/^(sudo )?systemctl restart liva\.service(?: (.+))?$/);
    if (m) return { base: "systemctl", password: (m[2] ? norm.slice(norm.toLowerCase().indexOf(m[2])).trim() : null) };

    return null;
  }

  async function restartTerminal(state) {
    showPrompt(state, false);
    const banner = ["", "Restarting liva.service...", ""];
    runRattlePrint(
      state,
      banner,
      2.2,
      async () => {
        requestServiceRestart();
        const ok = await waitForServiceOnline(30000);
        if (!ok) {
          const lines = ["", "ERROR", "Service did not come back online (timeout).", ""];
          runRattlePrint(
            state,
            lines,
            3.0,
            () => {
              state.running = false;
              ensurePrompt(state);
              showPrompt(state, true);
            },
            { append: true }
          );
          return;
        }

        (state.outputEl || state.body).textContent += "\nService online.\n";
        state.running = false;
        ensurePrompt(state);
        showPrompt(state, true);
      },
      { append: true }
    );
  }

  function computeRattleSecondsForLines(lines) {
    const n = Array.isArray(lines) ? lines.length : 0;
    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
    // roughly "like the startup" but scaled down for short outputs
    return clamp(0.12 * n, 1.8, 6.5);
  }

  function renderPrompt(state) {
    if (!state.inputTextEl || !state.cursorEl) return;
    const pwMode = Boolean(state.expectRestartPassword);
    if (state.promptEl) state.promptEl.textContent = pwMode ? "" : PROMPT + " ";
    state.inputTextEl.textContent = pwMode ? "" : (state.lineBuf || "");
    state.cursorEl.style.display = state.running ? "none" : "inline-block";
  }

  function showPrompt(state, show) {
    if (!state.inputLine) return;
    state.inputLine.style.display = show ? "flex" : "none";
    if (show) {
      state.body.focus({ preventScroll: true });
      renderPrompt(state);
      state.body.scrollTop = state.body.scrollHeight;
    }
  }

  function ensurePrompt(state) {
    if (state.inputLine) return;

    const line = document.createElement("div");
    line.className = "th-cmd-inputline";

    const prompt = document.createElement("span");
    prompt.className = "th-cmd-prompt";
    prompt.textContent = PROMPT + " ";

    const text = document.createElement("span");
    text.className = "th-cmd-inputtext";

    const cursor = document.createElement("span");
    cursor.className = "th-cmd-cursor";
    cursor.textContent = "_";

    line.appendChild(prompt);
    line.appendChild(text);
    line.appendChild(cursor);
    state.body.appendChild(line);

    state.inputLine = line;
    state.inputTextEl = text;
    state.cursorEl = cursor;
    state.promptEl = prompt;
    state.lineBuf = "";
    state.history = [];
    state.historyIdx = -1;

    if (!state.keyListenerAttached) {
      state.keyListenerAttached = true;

      state.body.addEventListener("pointerdown", () => {
        state.body.focus({ preventScroll: true });
        renderPrompt(state);
      });

      state.body.addEventListener("keydown", async (e) => {
        if (state.running) {
          e.preventDefault();
          return;
        }

        if (e.key === "Enter") {
          e.preventDefault();
          const cmdRaw = String(state.lineBuf || "");
          const cmd = cmdRaw.trim();

          state.lineBuf = "";
          state.historyIdx = -1;
          renderPrompt(state);

          // restart password flow: do NOT echo password input
          if (state.expectRestartPassword) {
            state.expectRestartPassword = false;
            if (cmd === RESTART_PASSWORD) {
              await restartTerminal(state);
            } else {
              (state.outputEl || state.body).textContent += "Access denied.\n";
              state.body.scrollTop = state.body.scrollHeight;
              renderPrompt(state);
              showPrompt(state, true);
            }
            return;
          }

          if (cmd.length) state.history.unshift(cmd);

          // echo typed line into output and run command
          (state.outputEl || state.body).textContent += `${PROMPT} ${cmdRaw}\n`;
          state.body.scrollTop = state.body.scrollHeight;

          if (!cmd) {
            return;
          }

          const low = cmd.toLowerCase();
          if (low === "cls" || low === "clear") {
            (state.outputEl || state.body).textContent = "";
            return;
          }
          if (low === "exit") {
            if (typeof state.closeWindow === "function") state.closeWindow();
            return;
          }
          const restartInfo = parseRestartCommand(cmd);
          if (restartInfo) {
            if (restartInfo.password == null) {
              (state.outputEl || state.body).textContent += "Password:\n";
              state.body.scrollTop = state.body.scrollHeight;
              state.expectRestartPassword = true;
              state.lineBuf = "";
              renderPrompt(state);
              return;
            }
            if (String(restartInfo.password).trim() !== RESTART_PASSWORD) {
              (state.outputEl || state.body).textContent += "Access denied.\n";
              state.body.scrollTop = state.body.scrollHeight;
              return;
            }
            await restartTerminal(state);
            return;
          }
          if (low === "about" || low === "about.exe") {
            await runAbout(state);
            return;
          }

          showPrompt(state, false);
          try {
            const payload = await execCommand(cmd);
            const lines = (payload && payload.lines) ? payload.lines : [];
            const seconds = computeRattleSecondsForLines(lines);
            runRattlePrint(
              state,
              lines,
              seconds,
              () => {
                state.running = false;
                showPrompt(state, true);
              },
              { append: true }
            );
          } catch (err) {
            const lines = ["ERROR", String(err && err.message ? err.message : err)];
            const seconds = computeRattleSecondsForLines(lines);
            runRattlePrint(
              state,
              lines,
              seconds,
              () => {
                state.running = false;
                showPrompt(state, true);
              },
              { append: true }
            );
          }

          return;
        }

        if (e.key === "Backspace") {
          e.preventDefault();
          state.lineBuf = (state.lineBuf || "").slice(0, -1);
          renderPrompt(state);
          return;
        }

        if (e.key === "ArrowUp") {
          if (!state.history.length) return;
          e.preventDefault();
          if (state.historyIdx < state.history.length - 1) state.historyIdx += 1;
          state.lineBuf = state.history[state.historyIdx] || "";
          renderPrompt(state);
          return;
        }

        if (e.key === "ArrowDown") {
          if (!state.history.length) return;
          e.preventDefault();
          if (state.historyIdx > 0) state.historyIdx -= 1;
          else state.historyIdx = -1;
          state.lineBuf = state.historyIdx === -1 ? "" : (state.history[state.historyIdx] || "");
          renderPrompt(state);
          return;
        }

        if (e.key === "Escape") {
          e.preventDefault();
          state.lineBuf = "";
          renderPrompt(state);
          return;
        }

        if (e.ctrlKey || e.metaKey || e.altKey) return;
        if (typeof e.key === "string" && e.key.length === 1) {
          e.preventDefault();
          state.lineBuf = (state.lineBuf || "") + e.key;
          renderPrompt(state);
        }
      });
    }

    state.body.focus({ preventScroll: true });
    renderPrompt(state);
  }

  // -----------------------------
  // Multi-window manager
  // -----------------------------
  let zTop = 10000;
  let winCount = 0;

  function getOverlayDesktop() {
    const overlay = document.getElementById("thCmdOverlay");
    if (!overlay) return null;

    overlay.style.display = "block";
    overlay.style.alignItems = "stretch";
    overlay.style.justifyContent = "stretch";

    return overlay;
  }

  function cmdIconDataUrl() {
    return "data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A//www.w3.org/2000/svg%27%20width%3D%2716%27%20height%3D%2716%27%20viewBox%3D%270%200%2016%2016%27%3E%3Crect%20x%3D%270%27%20y%3D%270%27%20width%3D%2716%27%20height%3D%2716%27%20rx%3D%273%27%20fill%3D%27%23000000%27/%3E%3Ctext%20x%3D%271.6%27%20y%3D%2711.3%27%20font-family%3D%27Consolas%2C%20Courier%20New%2C%20monospace%27%20font-size%3D%277.1%27%20fill%3D%27%23ffffff%27%3EC%3A%5C_%3C/text%3E%3C/svg%3E";
  }

  function createWindowElement() {
    winCount += 1;

    const w = document.createElement("div");
    w.className = "th-cmd-window th-cmd-window-instance";
    w.dataset.winId = String(winCount);
    w.style.position = "absolute";
    w.style.left = "120px";
    w.style.top = `${90 + (winCount - 1) * 26}px`;
    w.style.width = "860px";
    w.style.height = "560px";
    w.style.zIndex = String(++zTop);

    const titlebar = document.createElement("div");
    titlebar.className = "th-cmd-titlebar";

    const title = document.createElement("div");
    title.className = "th-cmd-title";

    const icon = document.createElement("span");
    icon.className = "th-cmd-icon";
    icon.style.backgroundImage = `url("${cmdIconDataUrl()}")`;

    const text = document.createElement("span");
    text.textContent = "Command Prompt - LIVA";

    title.appendChild(icon);
    title.appendChild(text);

    const controls = document.createElement("div");
    controls.className = "th-cmd-controls";

    const btnMin = document.createElement("div");
    btnMin.className = "th-cmd-ctrl";
    btnMin.title = "Minimize";
    btnMin.textContent = "—";

    const btnMax = document.createElement("div");
    btnMax.className = "th-cmd-ctrl";
    btnMax.title = "Maximize";
    btnMax.textContent = "□";

    const btnClose = document.createElement("div");
    btnClose.className = "th-cmd-ctrl close";
    btnClose.title = "Close";
    btnClose.textContent = "X";

    controls.appendChild(btnMin);
    controls.appendChild(btnMax);
    controls.appendChild(btnClose);

    titlebar.appendChild(title);
    titlebar.appendChild(controls);

    const body = document.createElement("div");
    body.className = "th-cmd-body";
    body.textContent = "";
    body.tabIndex = 0;

    const outputEl = document.createElement("div");
    outputEl.className = "th-cmd-output";
    body.appendChild(outputEl);

    w.appendChild(titlebar);
    w.appendChild(body);

    const dirs = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
    for (const d of dirs) {
      const h = document.createElement("div");
      h.className = `th-cmd-handle ${d}`;
      w.appendChild(h);
    }

    return { w, titlebar, body, outputEl, btnMin, btnMax, btnClose };
  }

  function attachInteractions(overlay, w, titlebar, btnMin, btnMax, btnClose, state) {
    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

    function bringToFront() {
      w.style.zIndex = String(++zTop);
    }

    function rectPx() {
      const r = w.getBoundingClientRect();
      return { left: r.left, top: r.top, width: r.width, height: r.height };
    }

    function ensurePxPosition() {
      const r = rectPx();
      w.style.setProperty("transform", "none", "important");
      w.style.setProperty("left", `${Math.round(r.left)}px`, "important");
      w.style.setProperty("top", `${Math.round(r.top)}px`, "important");
      w.style.setProperty("width", `${Math.round(r.width)}px`, "important");
      w.style.setProperty("height", `${Math.round(r.height)}px`, "important");
    }

    function setPosSize({ left, top, width, height }) {
      const minW = 420;
      const minH = 220;

      if (width != null) width = Math.max(minW, width);
      if (height != null) height = Math.max(minH, height);

      const wNow = width != null ? width : w.getBoundingClientRect().width;
      const hNow = height != null ? height : w.getBoundingClientRect().height;

      const viewW = window.innerWidth;
      const viewH = window.innerHeight;
      if (left != null) left = clamp(left, 0 - (wNow - 80), viewW - 80);
      if (top != null) top = clamp(top, 0 - 40, viewH - 60);

      if (left != null) w.style.setProperty("left", `${left}px`, "important");
      if (top != null) w.style.setProperty("top", `${top}px`, "important");
      if (width != null) w.style.setProperty("width", `${width}px`, "important");
      if (height != null) w.style.setProperty("height", `${height}px`, "important");
    }

    // ---- DRAG
    let drag = null;

    titlebar.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".th-cmd-controls")) return;
      if (w.classList.contains("is-maximized")) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;

      bringToFront();
      ensurePxPosition();

      const r = rectPx();
      drag = {
        id: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        startLeft: r.left,
        startTop: r.top,
      };
      titlebar.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    window.addEventListener(
      "pointermove",
      (e) => {
        if (!drag || e.pointerId !== drag.id) return;
        const dx = e.clientX - drag.startX;
        const dy = e.clientY - drag.startY;
        setPosSize({ left: Math.round(drag.startLeft + dx), top: Math.round(drag.startTop + dy) });
      },
      { passive: false }
    );

    window.addEventListener(
      "pointerup",
      (e) => {
        if (!drag) return;
        if (e.pointerId !== drag.id) return;
        drag = null;
      },
      { passive: false }
    );

    // double click -> maximize/restore
    titlebar.addEventListener("dblclick", (e) => {
      if (e.target.closest(".th-cmd-controls")) return;
      toggleMax();
    });

    // ---- RESIZE
    let rz = null;

    function flagsFromDir(dir) {
      return { n: dir.includes("n"), s: dir.includes("s"), e: dir.includes("e"), w: dir.includes("w") };
    }

    w.addEventListener("pointerdown", (e) => {
      if (w.classList.contains("is-maximized")) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;

      const h = e.target.closest(".th-cmd-handle");
      if (!h) return;

      const dir = ["n", "s", "e", "w", "ne", "nw", "se", "sw"].find((d) => h.classList.contains(d));
      if (!dir) return;

      bringToFront();
      ensurePxPosition();

      const r = rectPx();
      rz = {
        id: e.pointerId,
        f: flagsFromDir(dir),
        startX: e.clientX,
        startY: e.clientY,
        startLeft: r.left,
        startTop: r.top,
        startW: r.width,
        startH: r.height,
      };

      h.setPointerCapture(e.pointerId);
      e.preventDefault();
      e.stopPropagation();
    });

    window.addEventListener(
      "pointermove",
      (e) => {
        if (!rz || e.pointerId !== rz.id) return;

        const dx = e.clientX - rz.startX;
        const dy = e.clientY - rz.startY;

        let left = rz.startLeft;
        let top = rz.startTop;
        let wW = rz.startW;
        let wH = rz.startH;

        if (rz.f.e) wW = rz.startW + dx;
        if (rz.f.s) wH = rz.startH + dy;
        if (rz.f.w) {
          wW = rz.startW - dx;
          left = rz.startLeft + dx;
        }
        if (rz.f.n) {
          wH = rz.startH - dy;
          top = rz.startTop + dy;
        }

        setPosSize({ left: Math.round(left), top: Math.round(top), width: Math.round(wW), height: Math.round(wH) });
      },
      { passive: false }
    );

    window.addEventListener(
      "pointerup",
      (e) => {
        if (!rz) return;
        if (e.pointerId !== rz.id) return;
        rz = null;
      },
      { passive: false }
    );

    // ---- MIN/MAX/CLOSE per window
    let restoreRect = null;

    function maximize() {
      if (w.classList.contains("is-maximized")) return;

      ensurePxPosition();
      const r = rectPx();
      restoreRect = { left: r.left, top: r.top, width: r.width, height: r.height };

      w.classList.add("is-maximized");
      w.style.setProperty("left", "0px", "important");
      w.style.setProperty("top", "0px", "important");
      w.style.setProperty("width", `${window.innerWidth}px`, "important");
      w.style.setProperty("height", `${window.innerHeight}px`, "important");
      w.style.setProperty("transform", "none", "important");
    }

    function restore() {
      if (!w.classList.contains("is-maximized")) return;
      w.classList.remove("is-maximized");
      if (restoreRect) setPosSize(restoreRect);
    }

    function toggleMax() {
      if (w.classList.contains("is-maximized")) restore();
      else maximize();
    }

    function minimize() {
      w.style.display = "none";
    }

    function close() {
      state.running = false;
      w.remove();
      if (!overlay.querySelector(".th-cmd-window-instance")) {
        overlay.setAttribute("aria-hidden", "true");
      }
    }

    btnMax.addEventListener("click", toggleMax);
    btnMin.addEventListener("click", minimize);
    btnClose.addEventListener("click", close);

    w.addEventListener("pointerdown", () => {
      if (w.style.display === "none") w.style.display = "block";
      bringToFront();
    });

    window.addEventListener("resize", () => {
      if (!w.classList.contains("is-maximized")) return;
      w.style.setProperty("width", `${window.innerWidth}px`, "important");
      w.style.setProperty("height", `${window.innerHeight}px`, "important");
    });
  }

  // -----------------------------
  // Public API
  // -----------------------------
	  window.openLIVACmdAbout = async function openLIVACmdAbout() {
    const overlay = getOverlayDesktop();
    if (!overlay) return;

    overlay.querySelectorAll(".th-cmd-window:not(.th-cmd-window-instance)").forEach((el) => el.remove());
    overlay.setAttribute("aria-hidden", "false");

    const { w, titlebar, body, outputEl, btnMin, btnMax, btnClose } = createWindowElement();
    overlay.appendChild(w);

	    const state = { body, outputEl, running: false, closeWindow: () => w.remove() };
	    attachInteractions(overlay, w, titlebar, btnMin, btnMax, btnClose, state);

	    // No auto-output on open; behave like an empty CMD ready for input.
	    outputEl.textContent = WINDOWS_BANNER.join("\n") + "\n";
	    ensurePrompt(state);
      loadAboutStats().catch(() => {});
	  };
})();
