document.addEventListener("DOMContentLoaded", () => {
    const authLevel = (document.body?.dataset?.authLevel || "none").toLowerCase();
    const isMaster = authLevel === "trusted" || authLevel === "master";
    const isKey = authLevel === "key";
    const canReadAll = isMaster || isKey;
    const settingsPage = document.querySelector(".settings-page");
    const csrfToken = settingsPage?.dataset?.csrfToken || "";
    const layoutModeToggle = document.getElementById("layoutmode-toggle");

    function setLayoutModeUI(mode) {
        if (layoutModeToggle) {
            layoutModeToggle.checked = mode === "compact";
        }
    }

    async function authFetch(url, options = {}) {
        const opts = { ...options };
        opts.headers = { ...(options.headers || {}) };
        const method = (opts.method || "GET").toUpperCase();
        if (method !== "GET" && csrfToken) {
            opts.headers["X-CSRF-Token"] = csrfToken;
        }
        opts.credentials = opts.credentials || "same-origin";
        return fetch(url, opts);
    }

    const initialLayoutMode = window.LIVA?.getLayoutMode?.() || document.documentElement.dataset.layoutMode || "wide";
    setLayoutModeUI(initialLayoutMode);
    layoutModeToggle?.addEventListener("change", () => {
        const nextMode = layoutModeToggle.checked ? "compact" : "wide";
        const finalMode = window.LIVA?.applyLayoutMode
            ? window.LIVA.applyLayoutMode(nextMode, { persist: true })
            : nextMode;
        setLayoutModeUI(finalMode);
    });
    window.addEventListener("layoutmodechange", (event) => {
        setLayoutModeUI(event?.detail?.layoutMode || "wide");
    });

    // Control Center is deliberately pull-based: initial load plus an explicit
    // refresh, never a background polling loop on the settings page.
    const controlCenter = document.querySelector("[data-control-center]");
    if (controlCenter) {
        const statusNode = document.getElementById("control-center-status");
        const summaryNode = document.getElementById("control-center-summary");
        const updatedNode = document.getElementById("control-center-updated");
        const checksNode = document.getElementById("control-center-checks");
        const jobsNode = document.getElementById("control-center-jobs");
        const selfHealingNode = document.getElementById("self-healing-panel");
        const technicalConfigNode = document.getElementById("technical-config-panel");
        const notificationNode = document.getElementById("notification-panel");
        const releaseNode = document.getElementById("release-panel");
        const fullRecoveryNode = document.getElementById("full-recovery-panel");
        const incidentsNode = document.getElementById("control-center-incidents");
        const incidentDetailNode = document.getElementById("control-center-incident-detail");
        const logsNode = document.getElementById("control-center-logs");
        const logFilter = document.getElementById("control-center-log-filter");
        const logComponent = document.getElementById("control-center-log-component");
        const logSearch = document.getElementById("control-center-log-search");
        const refreshButton = document.getElementById("control-center-refresh");

        const label = (value) => ({ healthy: "Gesund", degraded: "Eingeschränkt", failing: "Fehler", unknown: "Unbekannt", inactive: "Inaktiv" }[value] || "Unbekannt");
        const timestamp = (value) => value ? new Date(value).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "–";
        const safeText = (value) => String(value || "");

        function item(title, status, reason, meta, details) {
            const article = document.createElement("article");
            article.className = `control-center-item is-${status || "unknown"}`;
            const heading = document.createElement("div");
            heading.className = "control-center-item-heading";
            heading.textContent = `${title} · ${label(status)}`;
            const why = document.createElement("p");
            why.textContent = safeText(reason);
            const extra = document.createElement("p");
            extra.className = "control-center-meta";
            extra.textContent = meta;
            article.append(heading, why, extra);
            if (details && Object.keys(details).length) {
                const disclosure = document.createElement("details");
                const caption = document.createElement("summary");
                caption.textContent = "Technische Evidenz";
                const pre = document.createElement("pre");
                pre.textContent = JSON.stringify(details, null, 2);
                disclosure.append(caption, pre);
                article.append(disclosure);
            }
            return article;
        }

        function showFullRecovery(payload) {
            if (!fullRecoveryNode) return;
            const checks = (payload.checks || []).filter((check) => check.component_id === "full-recovery");
            const job = (payload.jobs || []).find((candidate) => candidate.job_id === "full-recovery");
            const rows = checks.map((check) => item(check.name, check.status, check.reason,
                `Letzter Transport-Check: ${timestamp(check.last_success_at)} · Phase: ${safeText(check.details?.phase || "setup")}`,
                check.details));
            if (job) rows.unshift(item("Backup Engine", job.status,
                `Status: ${safeText(job.details?.status || "unknown")} · Fehlercode: ${safeText(job.details?.error_code || "–")}`,
                `Letzter Versuch: ${timestamp(job.last_run_at)} · Restore-Test: ${timestamp(job.details?.last_restore_tested_at)}`,
                { expected_device: job.details?.expected_device, observed_device: job.details?.observed_device, backup_id: job.details?.backup_id }));
            fullRecoveryNode.replaceChildren(...(rows.length ? rows : [item("Full Recovery", "unknown", "Noch keine Statusdaten verfügbar.", "", null)]));
        }

        async function showIncident(incidentId) {
            if (!incidentId || !incidentDetailNode) return;
            const response = await authFetch(`/api/control-center/incidents/${encodeURIComponent(incidentId)}`, { cache: "no-store" });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || !payload.incident) return;
            const incident = payload.incident;
            incidentDetailNode.hidden = false;
            incidentDetailNode.replaceChildren(item(
                `${incident.title} · Timeline`, incident.status,
                `First seen: ${timestamp(incident.first_seen)} · Last seen: ${timestamp(incident.last_seen)} · Ereignisse: ${incident.occurrence_count}`,
                `Betroffene Objekte: ${incident.affected_object_count || 0} · Severity: ${incident.severity}`,
                { events: incident.events || [] },
            ));
        }

        async function loadIncidents() {
            if (!incidentsNode) return;
            const response = await authFetch("/api/control-center/incidents?limit=50", { cache: "no-store" });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || !payload.ok) throw new Error("Vorfälle konnten nicht geladen werden.");
            const rows = (payload.incidents || []).map((incident) => {
                const node = item(incident.title, incident.severity === "critical" ? "failing" : incident.status === "open" ? "degraded" : "healthy", incident.status === "open" ? "Offener technischer Vorfall" : "Technisch behoben", `First: ${timestamp(incident.first_seen)} · Last: ${timestamp(incident.last_seen)} · Occurrences: ${incident.occurrence_count}`, null);
                const button = document.createElement("button");
                button.type = "button"; button.className = "ghost-btn"; button.textContent = "Details";
                button.addEventListener("click", () => showIncident(incident.incident_id));
                node.append(button);
                return node;
            });
            incidentsNode.replaceChildren(...(rows.length ? rows : [item("Keine offenen technischen Vorfälle", "healthy", "Der Collector hat keine incident-eligible Störung festgestellt.", "", null)]));
            (payload.incidents || []).filter((incident) => incident.status === "open").forEach((incident) => {
                checksNode?.querySelectorAll(`[data-component="${CSS.escape(incident.component_id)}"]`).forEach((checkNode) => {
                    if (checkNode.querySelector(".control-center-incident-link")) return;
                    const button = document.createElement("button");
                    button.type = "button"; button.className = "ghost-btn control-center-incident-link"; button.textContent = "Offenen Vorfall anzeigen";
                    button.addEventListener("click", () => showIncident(incident.incident_id));
                    checkNode.append(button);
                });
            });
        }

        async function loadLogs() {
            if (!logsNode) return;
            const query = new URLSearchParams({ limit: "100" });
            if (logComponent?.value) query.set("component", logComponent.value);
            if (logSearch?.value.trim()) query.set("search", logSearch.value.trim());
            const response = await authFetch(`/api/control-center/logs?${query}`, { cache: "no-store" });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || !payload.ok) throw new Error("Logs konnten nicht geladen werden.");
            const rows = (payload.logs || []).map((entry) => item(entry.component, entry.severity === "critical" ? "failing" : "healthy", entry.message, entry.severity, null));
            logsNode.replaceChildren(...(rows.length ? rows : [item("Keine passenden Logs", "healthy", "Im gewählten Zeitraum liegen keine passenden technischen Logzeilen vor.", "", null)]));
        }

        async function runJobAction(job, action, button) {
            if (!isMaster || !window.confirm(`${action} für „${job.name}“ wirklich ausführen?`)) return;
            button.disabled = true;
            try {
                const response = await authFetch(`/api/control-center/jobs/${encodeURIComponent(job.job_id)}/actions/${encodeURIComponent(action)}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ confirmed: true, request_id: crypto.randomUUID() }),
                });
                if (!response.ok) throw new Error("Aktion wurde abgewiesen.");
                await loadControlCenter();
            } catch (_) {
                button.disabled = false;
            }
        }

        async function setSelfHealing(enabled, button) {
            if (!isMaster || !window.confirm(`Self-Healing wirklich ${enabled ? "aktivieren" : "deaktivieren"}?`)) return;
            button.disabled = true;
            try {
                const response = await authFetch(`/api/control-center/self-healing/${enabled ? "enable" : "disable"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf_token: csrfToken }) });
                if (!response.ok) throw new Error("Self-Healing-Aktion abgewiesen.");
                await loadControlCenter();
            } finally { button.disabled = false; }
        }

        function showSelfHealing(payload) {
            if (!selfHealingNode) return;
            const state = payload.state || {};
            const node = item("Self-Healing", state.enabled ? "healthy" : "inactive", state.enabled ? "Begrenzte, freigegebene Policies aktiv." : "Global deaktiviert – der Executor liest nur.", `Automatische Aktionen letzte 24h: ${(payload.automatic_actions_24h || []).length}`, null);
            const toggle = document.createElement("button"); toggle.type = "button"; toggle.className = "ghost-btn"; toggle.textContent = state.enabled ? "Deaktivieren" : "Aktivieren"; toggle.disabled = !isMaster;
            toggle.addEventListener("click", () => setSelfHealing(!state.enabled, toggle)); node.append(toggle);
            const policies = (payload.policies || []).map((policy) => item(policy.policy_id, policy.state?.manual_intervention_required ? "failing" : policy.state?.enabled ? "healthy" : "inactive", `Action: ${policy.action} · Attempts: ${(policy.state?.attempts || []).length}/${policy.max_attempts}`, `Nächster Versuch: ${timestamp(policy.state?.next_allowed_at)} · Letztes Ergebnis: ${policy.state?.last_result || "–"}`, { transient_error_classes: policy.transient_error_classes, service: policy.service }));
            selfHealingNode.replaceChildren(node, ...(policies.length ? policies : []));
        }

        async function changeProvider(provider, button) {
            if (!isMaster || !window.confirm(`Globalen technischen Provider auf ${provider} wechseln?`)) return;
            button.disabled = true;
            try {
                const response = await authFetch("/api/control-center/notifications/provider", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider }) });
                if (!response.ok) throw new Error("Provider ist nicht gesund oder nicht konfiguriert.");
                await loadControlCenter();
            } finally { button.disabled = false; }
        }

        async function testNotification(button) {
            if (!isMaster || !window.confirm("Technische Testbenachrichtigung senden?")) return;
            button.disabled = true;
            try {
                const response = await authFetch("/api/control-center/notifications/test", { method: "POST" });
                if (!response.ok) throw new Error("Testbenachrichtigung nicht verfügbar.");
                await loadControlCenter();
            } finally { button.disabled = false; }
        }

        function showTechnicalConfig(payload) {
            if (!technicalConfigNode) return;
            const rows = (payload.configs || []).map((config) => item(config.name, config.status === "active" || config.status === "healthy" ? "healthy" : config.status === "read_only" ? "inactive" : "unknown", config.secret ? `Secret-Status: ${config.effective}` : `Effektiv: ${typeof config.effective === "object" ? JSON.stringify(config.effective) : config.effective}`, `Quelle: ${config.source} · Pending: ${config.pending ?? "–"}${config.restart_required ? " · Neustart erforderlich" : ""}`, { config_id: config.config_id, editable: config.editable, components: config.components }));
            technicalConfigNode.replaceChildren(...rows);
        }

        function showNotifications(payload) {
            if (!notificationNode) return;
            const current = payload.provider || {};
            const node = item(`Aktiver Provider: ${payload.active_provider || "–"}`, current.healthy ? "healthy" : "degraded", `Status: ${current.status || "unknown"}`, `Letzter Erfolg: ${timestamp(payload.last_success_at)} · Letzter Fehler: ${timestamp(payload.last_failure_at)}`, { capabilities: current.capabilities, diagnostics: current.diagnostics });
            const test = document.createElement("button"); test.type = "button"; test.className = "ghost-btn"; test.textContent = "Test senden"; test.disabled = !isMaster;
            test.addEventListener("click", () => testNotification(test)); node.append(test);
            (payload.providers || []).filter((provider) => provider.provider !== payload.active_provider).forEach((provider) => {
                const button = document.createElement("button"); button.type = "button"; button.className = "ghost-btn"; button.textContent = `Zu ${provider.provider} wechseln`; button.disabled = !isMaster || !provider.configured || !provider.healthy;
                button.addEventListener("click", () => changeProvider(provider.provider, button)); node.append(button);
            });
            notificationNode.replaceChildren(node);
        }

        async function createReleasePlan(button) {
            if (!isMaster) return; button.disabled = true;
            try {
                const response = await authFetch("/api/control-center/releases/plan", { method: "POST" });
                if (!response.ok) throw new Error("Release-Plan konnte nicht erstellt werden.");
                await loadControlCenter();
            } finally { button.disabled = false; }
        }

        async function deployRelease(deployment, button) {
            if (!isMaster || !window.confirm("Geprüften Release wirklich starten?")) return; button.disabled = true;
            try {
                const response = await authFetch("/api/control-center/releases/deploy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ deployment_id: deployment.deployment_id, confirmed: true }) });
                if (!response.ok) throw new Error("Deployment wurde blockiert.");
                await loadControlCenter();
            } finally { button.disabled = false; }
        }

        function showReleases(status, history) {
            if (!releaseNode) return;
            const repo = status.repo || {};
            const node = item("Produktiver Release-Stand", repo.dirty ? "degraded" : "healthy", `Commit: ${repo.commit_short || "–"} · Branch: ${repo.branch || "–"} · ${repo.dirty ? `Dirty (${repo.dirty_count || 0})` : "clean"}`, `Ziel: ${repo.target_short || "nicht verfügbar"} · Backup Gate: ${status.backup_gate?.ok ? "bereit" : "blockiert"}`, { dirty_paths: repo.dirty_paths || [], allowed_branch: repo.allowed_branch });
            const plan = document.createElement("button"); plan.type = "button"; plan.className = "ghost-btn"; plan.textContent = "Release prüfen"; plan.disabled = !isMaster;
            plan.addEventListener("click", () => createReleasePlan(plan)); node.append(plan);
            const rows = (history.deployments || []).map((deployment) => {
                const row = item(`Release ${String(deployment.deployment_id).slice(0, 8)}`, deployment.status === "succeeded" ? "healthy" : deployment.status === "planned" ? "unknown" : "degraded", deployment.status, `Preflight: ${deployment.preflight_status || "–"} · Ziel: ${String(deployment.commit_target || "–").slice(0, 12)}`, deployment.plan);
                if (deployment.status === "planned" && !deployment.plan?.no_update_available) { const button = document.createElement("button"); button.type = "button"; button.className = "ghost-btn"; button.textContent = "Deploy"; button.disabled = !isMaster; button.addEventListener("click", () => deployRelease(deployment, button)); row.append(button); }
                return row;
            });
            releaseNode.replaceChildren(node, ...rows);
        }

        async function showJobHistory(job, node) {
            const response = await authFetch(`/api/control-center/jobs/${encodeURIComponent(job.job_id)}/history?limit=20`, { cache: "no-store" });
            const payload = await response.json().catch(() => ({}));
            const history = payload.history || [];
            const details = document.createElement("details");
            details.open = true;
            const summary = document.createElement("summary"); summary.textContent = "Letzte technische Historie";
            const list = document.createElement("ul");
            history.forEach((entry) => {
                const row = document.createElement("li");
                row.textContent = entry.kind === "action_audit"
                    ? `${timestamp(entry.requested_at)} · ${entry.action}: ${entry.result}`
                    : entry.message;
                list.append(row);
            });
            if (!history.length) list.textContent = "Keine Einträge vorhanden.";
            details.append(summary, list);
            node.querySelector("details")?.remove(); node.append(details);
        }

        function jobItem(job) {
            const node = item(job.name, job.status,
                `${job.job_type || "Job"} · ${job.enabled === false ? "pausiert" : job.active ? "läuft" : "bereit"}`,
                `Letzter Erfolg: ${timestamp(job.last_success_at)} · Nächster Lauf: ${timestamp(job.next_run_at)}${job.details?.self_healing_policy ? ` · Self-Healing: ${job.details.self_healing_state?.enabled ? "bereit" : "aus"}` : ""}`,
                { source: job.source, capabilities: job.capabilities, incident_id: job.incident_id, details: job.details });
            (job.actions || []).filter((action) => action.available).forEach((action) => {
                const button = document.createElement("button");
                button.type = "button"; button.className = "ghost-btn"; button.textContent = action.action;
                button.disabled = !isMaster;
                button.title = action.requires_master ? "Master-Bestätigung erforderlich" : "";
                button.addEventListener("click", () => runJobAction(job, action.action, button));
                node.append(button);
            });
            const historyButton = document.createElement("button");
            historyButton.type = "button"; historyButton.className = "ghost-btn"; historyButton.textContent = "Historie";
            historyButton.addEventListener("click", () => showJobHistory(job, node).catch(() => {}));
            node.append(historyButton);
            return node;
        }

        async function loadControlCenter() {
            refreshButton.disabled = true;
            try {
                const response = await authFetch("/api/control-center/summary", { cache: "no-store" });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok || !payload.ok) throw new Error("Systemstatus konnte nicht geladen werden.");
                statusNode.textContent = `Gesamtstatus: ${label(payload.status)}`;
                statusNode.className = `control-center-status is-${payload.status || "unknown"}`;
                summaryNode.textContent = isMaster ? "Ausgewählte sichere Job-Aktionen erfordern eine bestätigte Master-Anfrage." : "Job-Zustände sind sichtbar; Mutationen erfordern Master-Zugriff.";
                updatedNode.textContent = `Letzte Prüfung: ${timestamp(payload.generated_at)}`;
                checksNode.replaceChildren(...(payload.checks || []).map((check) => {
                    const node = item(check.name, check.status, check.reason, `Letzter Erfolg: ${timestamp(check.last_success_at)} · Prüfung: ${timestamp(check.checked_at)}`, check.details);
                    node.dataset.component = check.component_id || "";
                    return node;
                }));
                const jobsPayload = await authFetch("/api/control-center/jobs", { cache: "no-store" });
                const jobsData = await jobsPayload.json().catch(() => ({}));
                jobsNode.replaceChildren(...(jobsData.jobs || payload.jobs || []).map(jobItem));
                const healingResponse = await authFetch("/api/control-center/self-healing", { cache: "no-store" });
                const healingData = await healingResponse.json().catch(() => ({}));
                if (healingResponse.ok) showSelfHealing(healingData);
                const configResponse = await authFetch("/api/control-center/config", { cache: "no-store" });
                const configData = await configResponse.json().catch(() => ({}));
                if (configResponse.ok) showTechnicalConfig(configData);
                const notificationResponse = await authFetch("/api/control-center/notifications", { cache: "no-store" });
                const notificationData = await notificationResponse.json().catch(() => ({}));
                if (notificationResponse.ok) showNotifications(notificationData);
                const releaseStatusResponse = await authFetch("/api/control-center/releases/status", { cache: "no-store" });
                const releaseStatusData = await releaseStatusResponse.json().catch(() => ({}));
                const releaseHistoryResponse = await authFetch("/api/control-center/releases", { cache: "no-store" });
                const releaseHistoryData = await releaseHistoryResponse.json().catch(() => ({}));
                if (releaseStatusResponse.ok && releaseHistoryResponse.ok) showReleases(releaseStatusData, releaseHistoryData);
                showFullRecovery(payload);
                await Promise.all([loadIncidents(), loadLogs()]);
            } catch (error) {
                statusNode.textContent = "Gesamtstatus: Unbekannt";
                statusNode.className = "control-center-status is-unknown";
                summaryNode.textContent = "Systemstatus konnte nicht geladen werden. Andere Einstellungen bleiben verfügbar.";
            } finally {
                refreshButton.disabled = false;
            }
        }
        refreshButton.addEventListener("click", loadControlCenter);
        logFilter?.addEventListener("submit", (event) => { event.preventDefault(); loadLogs().catch(() => {}); });
        loadControlCenter();
    }

    // ===============================
    // SETTINGS (AUTOPILOT + TELEGRAM PUSH)
    // ===============================
    const autopilotToggle = document.getElementById("autopilot-toggle");
    const autopilotStatus = document.getElementById("autopilot-status");
    const autopilotDescription = document.getElementById("autopilot-description");
    const autopilotModeNote = document.getElementById("autopilot-mode-note");
    const telegramPushStatus = document.getElementById("telegram-push-status");
    const telegramPrimaryBtn = document.getElementById("telegram-primary-btn");
    const telegramDurationPanel = document.getElementById("telegram-duration-panel");
    const telegramDurationButtons = Array.from(document.querySelectorAll(".telegram-duration-btn"));
    const telegramChangeDurationBtn = document.getElementById("telegram-change-duration-btn");
    const telegramPausedHint = document.getElementById("telegram-paused-hint");
    const telegramMealToggleBtn = document.getElementById("telegram-meal-toggle-btn");
    const telegramMealPanel = document.getElementById("telegram-meal-panel");
    const telegramMealToggleInputs = Array.from(document.querySelectorAll(".telegram-meal-toggle-input"));
    const telegramMealNameNodes = Array.from(document.querySelectorAll("[data-meal-name]"));
    const telegramMealMetaNodes = Array.from(document.querySelectorAll("[data-meal-meta]"));
    let telegramPushIsActive = true;

    function setAutopilotUI(enabled) {
        if (autopilotToggle) autopilotToggle.checked = !!enabled;
        if (autopilotStatus) {
            autopilotStatus.textContent = enabled ? "CORE" : "BASIS";
            autopilotStatus.classList.toggle("is-on", !!enabled);
        }
        if (autopilotDescription) {
            autopilotDescription.textContent = enabled
                ? "CORE-Modus: Der bewährte Autopilot bleibt aktiv und wird zusätzlich mit Learnings aus CORE Training und Calibration Deck geschärft."
                : "Basis-Modus: Der bewährte Autopilot steuert Vorschläge adaptiv und stabil. CORE-Learnings kommen hier noch nicht dazu.";
        }
        if (autopilotModeNote) {
            autopilotModeNote.textContent = enabled
                ? "Aktiv: CORE ergänzt den Basis-Modus um persönliche Learnings, Prioritäten und weitere Steuerlogik."
                : "BASIS bleibt der normale Autopilot-Modus. CORE-spezifische Learnings und spätere Erweiterungen bleiben aus.";
        }
    }

    function formatTelegramUntil(untilTs) {
        if (!untilTs || !Number.isFinite(untilTs)) return "";
        const d = new Date(untilTs * 1000);
        if (Number.isNaN(d.getTime())) return "";
        const now = new Date();
        const sameDay =
            d.getFullYear() === now.getFullYear() &&
            d.getMonth() === now.getMonth() &&
            d.getDate() === now.getDate();
        if (sameDay) return "23:59";
        return d.toLocaleString("de-DE", {
            dateStyle: "short",
            timeStyle: "short",
        });
    }

    function setTelegramPushUI(settings) {
        if (!telegramPushStatus) return null;
        const muted = !!(settings?.telegram_push_muted);
        const active = !!(settings?.telegram_push_active);
        const mutedUntilTs = Number(settings?.telegram_push_muted_until_ts || 0);
        const unmutedUntilTs = Number(settings?.telegram_push_unmuted_until_ts || 0);
        const mutedForever = !!(settings?.telegram_push_muted_forever);
        telegramPushIsActive = active;
        const mutedUntilLabel = formatTelegramUntil(mutedUntilTs);
        const unmutedUntilLabel = formatTelegramUntil(unmutedUntilTs);
        if (active && unmutedUntilTs) {
            telegramPushStatus.textContent = unmutedUntilLabel ? `Aktiv bis ${unmutedUntilLabel}` : "Aktiv";
        } else {
            telegramPushStatus.textContent = active
                ? "Aktiv"
                : (mutedForever
                    ? "Pausiert (dauerhaft)"
                    : (mutedUntilLabel ? `Pausiert bis ${mutedUntilLabel}` : "Pausiert"));
        }
        telegramPushStatus.classList.toggle("is-muted", muted);
        telegramPushStatus.classList.toggle("is-active", active);
        if (telegramPausedHint) telegramPausedHint.classList.toggle("is-hidden", active);
        setTelegramPrimaryMode(active);
        return active;
    }

    function setTelegramMealPushUI(settings) {
        const map = settings?.telegram_meal_push_enabled || {};
        telegramMealToggleInputs.forEach((checkbox) => {
            const idx = String(Number(checkbox.dataset.mealIndex || 0));
            const raw = map[idx];
            checkbox.checked = !(raw === 0 || raw === "0" || raw === false || raw === "false");
        });
    }

    function mealNodeMap(nodes) {
        const out = {};
        nodes.forEach((node) => {
            const k = String(Number(node?.dataset?.mealName || node?.dataset?.mealMeta || 0));
            if (k && k !== "0") out[k] = node;
        });
        return out;
    }

    function setTelegramMealSlotDetails(meals) {
        const nameByIdx = mealNodeMap(telegramMealNameNodes);
        const metaByIdx = mealNodeMap(telegramMealMetaNodes);
        for (let i = 1; i <= 8; i += 1) {
            const key = String(i);
            if (nameByIdx[key]) nameByIdx[key].textContent = `Meal ${i}`;
            if (metaByIdx[key]) metaByIdx[key].textContent = "Kein geplanter Slot für heute.";
        }
        const rows = Array.isArray(meals) ? meals : [];
        rows.forEach((meal) => {
            const slotIndex = Number(meal?.slot_index);
            if (!Number.isFinite(slotIndex) || slotIndex < 0) return;
            const mealIndex = slotIndex + 1;
            if (mealIndex < 1 || mealIndex > 8) return;
            const key = String(mealIndex);
            const name = String(meal?.name || "").trim() || `Meal ${mealIndex}`;
            if (nameByIdx[key]) nameByIdx[key].textContent = `Meal ${mealIndex}`;
            if (metaByIdx[key]) metaByIdx[key].textContent = name;
        });
    }

    function setTelegramControlsDisabled(disabled) {
        telegramDurationButtons.forEach((btn) => {
            btn.disabled = !!disabled || isKey;
        });
        if (telegramPrimaryBtn) telegramPrimaryBtn.disabled = !!disabled || isKey;
        if (telegramChangeDurationBtn) telegramChangeDurationBtn.disabled = !!disabled || isKey;
    }

    function setTelegramPrimaryMode(isActive) {
        if (telegramPrimaryBtn) {
            telegramPrimaryBtn.textContent = isActive ? "Pausieren" : "Wieder aktivieren";
        }
        if (telegramChangeDurationBtn) telegramChangeDurationBtn.classList.toggle("is-hidden", isActive);
    }

    function resolveMuteAction(slot) {
        const s = (slot || "").trim();
        if (s === "today") return "mute_today";
        if (s === "7d") return "mute_7d";
        if (s === "forever") return "mute_forever";
        return "";
    }

    async function loadSettings() {
        try {
            const res = await authFetch("/api/settings", { cache: "no-store" });
            const data = await parseJsonSafe(res);
            const settings = data?.settings || {};
            const enabled = !!(settings.core_mode_enabled ?? settings.autopilot_enabled);
            setAutopilotUI(enabled);
            setTelegramPushUI(settings);
            setTelegramMealPushUI(settings);
        } catch (e) {
            console.warn("Settings load failed", e);
        }
    }

    async function loadTelegramMealSlotDetails() {
        try {
            const res = await authFetch("/api/nutrition/plan/today?include_items=1", { cache: "no-store" });
            const data = await parseJsonSafe(res);
            setTelegramMealSlotDetails(data?.meals || []);
        } catch (e) {
            console.warn("Telegram meal slot details load failed", e);
            setTelegramMealSlotDetails([]);
        }
    }

    async function saveAutopilotSetting(enabled) {
        if (!autopilotToggle || isKey) return;
        const prev = autopilotToggle.checked;
        setAutopilotUI(enabled);
        try {
            const res = await authFetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ core_mode_enabled: enabled ? 1 : 0 }),
            });
            const data = await parseJsonSafe(res);
            if (!data || data.ok !== true) {
                setAutopilotUI(prev);
            }
        } catch (e) {
            console.warn("Autopilot settings save failed", e);
            setAutopilotUI(prev);
        }
    }

    async function saveTelegramPushAction(action) {
        if (!isMaster) return;
        setTelegramControlsDisabled(true);
        try {
            const res = await authFetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ telegram_push_action: action }),
            });
            const data = await parseJsonSafe(res);
            if (!data || data.ok !== true) {
                await loadSettings();
                return;
            }
            setTelegramPushUI(data.settings || {});
            if (telegramDurationPanel) telegramDurationPanel.classList.add("is-hidden");
        } catch (e) {
            console.warn("Telegram action save failed", e);
            await loadSettings();
        } finally {
            setTelegramControlsDisabled(false);
        }
    }

    async function saveTelegramMealToggle(mealIndex, enabled, checkbox) {
        if (!isMaster || !Number.isFinite(Number(mealIndex))) return;
        const prev = !enabled;
        if (checkbox) checkbox.disabled = true;
        try {
            const res = await authFetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    telegram_meal_push_enabled: {
                        [String(Number(mealIndex))]: enabled ? 1 : 0,
                    },
                }),
            });
            const data = await parseJsonSafe(res);
            if (!data || data.ok !== true) {
                if (checkbox) checkbox.checked = !enabled;
                await loadSettings();
                return;
            }
            setTelegramMealPushUI(data.settings || {});
        } catch (e) {
            console.warn("Telegram meal toggle save failed", e);
            if (checkbox) checkbox.checked = prev;
            await loadSettings();
        } finally {
            if (checkbox) checkbox.disabled = isKey;
        }
    }

    // ===============================
    // DATA EXPORT (AI JSONL)
    // ===============================
    const exportCard = document.getElementById("export-card");
    const exportToggleBtn = document.getElementById("export-toggle-btn");
    const exportPanel = document.getElementById("export-panel");
    const exportStart = document.getElementById("export-start-date");
    const exportEnd = document.getElementById("export-end-date");
    const exportLimit = document.getElementById("export-limit");
    const exportGenerateBtn = document.getElementById("export-generate-btn");
    const exportCopyBtn = document.getElementById("export-copy-btn");
    const exportOut = document.getElementById("export-output");
    const exportErr = document.getElementById("export-error");

    function exportSetError(msg) {
        if (!exportErr) return;
        exportErr.textContent = msg || "";
        exportErr.style.display = msg ? "block" : "none";
    }

    function isoTodayLocal() {
        const d = new Date();
        const yyyy = d.getFullYear();
        const mm = String(d.getMonth() + 1).padStart(2, "0");
        const dd = String(d.getDate()).padStart(2, "0");
        return `${yyyy}-${mm}-${dd}`;
    }

    function isoDaysAgo(days) {
        const d = new Date();
        d.setDate(d.getDate() - days);
        const yyyy = d.getFullYear();
        const mm = String(d.getMonth() + 1).padStart(2, "0");
        const dd = String(d.getDate()).padStart(2, "0");
        return `${yyyy}-${mm}-${dd}`;
    }

    async function parseJsonSafe(res) {
        const type = (res.headers.get("Content-Type") || "").toLowerCase();
        if (type.includes("application/json")) {
            return res.json();
        }
        const text = await res.text();
        return { ok: false, error: text || "unexpected response" };
    }

    async function copyToClipboard(text) {
        if (!text) return false;
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (e) {
            try {
                const ta = exportOut;
                if (!ta) return false;
                ta.focus();
                ta.select();
                const ok = document.execCommand("copy");
                ta.setSelectionRange(0, 0);
                return ok;
            } catch (_) {
                return false;
            }
        }
    }

    if (canReadAll && exportCard && exportStart && exportEnd) {
        const ds = exportCard.dataset || {};
        exportStart.value = ds.defaultStart || isoDaysAgo(30);
        exportEnd.value = ds.defaultEnd || isoTodayLocal();
    }

    if (canReadAll && exportToggleBtn && exportPanel) {
        exportToggleBtn.addEventListener("click", () => {
            const isHidden = exportPanel.classList.contains("is-hidden");
            exportPanel.classList.toggle("is-hidden", !isHidden);
            exportToggleBtn.textContent = isHidden ? "Export schließen" : "Export öffnen";
        });
    }

    if (canReadAll && exportGenerateBtn && exportOut && exportCopyBtn) {
        exportGenerateBtn.addEventListener("click", async () => {
            exportSetError("");

            const types = Array.from(document.querySelectorAll(".export-type:checked")).map(
                (el) => el.value
            );
            if (!types.length) {
                exportSetError("Bitte mindestens einen Datentyp auswählen.");
                return;
            }

            const startDate = (exportStart && exportStart.value) ? exportStart.value : "";
            const endDate = (exportEnd && exportEnd.value) ? exportEnd.value : "";

            if (!startDate || !endDate) {
                exportSetError("Bitte Start- und Enddatum setzen.");
                return;
            }
            if (startDate > endDate) {
                exportSetError("Ungültiger Zeitraum: Start darf nicht nach Ende liegen.");
                return;
            }

            let limit = 3000;
            if (exportLimit && exportLimit.value) {
                const n = Number(exportLimit.value);
                if (Number.isFinite(n) && n >= 1) limit = Math.min(20000, Math.floor(n));
            }

            const qs = new URLSearchParams();
            qs.set("format", "ai_compact");
            qs.set("start_date", startDate);
            qs.set("end_date", endDate);
            qs.set("include", types.join(","));
            qs.set("limit", String(limit));

            exportGenerateBtn.disabled = true;
            exportGenerateBtn.textContent = "Generiere…";
            exportCopyBtn.disabled = true;

            try {
                const res = await fetch(`/api/export?${qs.toString()}`, { cache: "no-store" });
                const text = await res.text();
                if (!res.ok) {
                    exportOut.value = "";
                    exportSetError(text || "Export fehlgeschlagen.");
                    return;
                }

                exportOut.value = text || "";
                exportCopyBtn.disabled = !exportOut.value.trim();
            } catch (e) {
                exportOut.value = "";
                exportSetError("Export fehlgeschlagen (Netzwerk/Server).");
            } finally {
                exportGenerateBtn.disabled = false;
                exportGenerateBtn.textContent = "Generate Export";
            }
        });
    }

    if (canReadAll && exportCopyBtn && exportOut) {
        exportCopyBtn.addEventListener("click", async () => {
            exportSetError("");
            const ok = await copyToClipboard(exportOut.value);
            if (!ok) {
                exportSetError("Copy fehlgeschlagen. Bitte manuell markieren und kopieren.");
                return;
            }
            exportCopyBtn.textContent = "Copied ✅";
            setTimeout(() => {
                exportCopyBtn.textContent = "Copy";
            }, 900);
        });
    }

    if (autopilotToggle && !autopilotToggle.dataset.bound) {
        autopilotToggle.dataset.bound = "1";
        autopilotToggle.addEventListener("change", () => {
            if (isKey) return;
            saveAutopilotSetting(autopilotToggle.checked);
        });
    }

    if (telegramPrimaryBtn) {
        telegramPrimaryBtn.addEventListener("click", () => {
            if (!isMaster || isKey) return;
            if (telegramPushIsActive) {
                if (telegramDurationPanel) {
                    const isHidden = telegramDurationPanel.classList.contains("is-hidden");
                    telegramDurationPanel.classList.toggle("is-hidden", !isHidden);
                }
                return;
            }
            saveTelegramPushAction("unmute");
        });
    }

    if (telegramChangeDurationBtn && telegramDurationPanel) {
        telegramChangeDurationBtn.addEventListener("click", () => {
            if (!isMaster || isKey) return;
            const isHidden = telegramDurationPanel.classList.contains("is-hidden");
            telegramDurationPanel.classList.toggle("is-hidden", !isHidden);
        });
    }

    if (telegramMealToggleBtn && telegramMealPanel) {
        telegramMealToggleBtn.addEventListener("click", () => {
            const isHidden = telegramMealPanel.classList.contains("is-hidden");
            telegramMealPanel.classList.toggle("is-hidden", !isHidden);
            telegramMealToggleBtn.textContent = isHidden ? "Meal-Slots schließen" : "Meal-Slots öffnen";
        });
    }

    telegramDurationButtons.forEach((btn) => {
        if (btn.dataset.bound) return;
        btn.dataset.bound = "1";
        btn.addEventListener("click", () => {
            const slot = (btn.dataset.telegramDurationSlot || "").trim();
            const action = resolveMuteAction(slot);
            if (!action) return;
            saveTelegramPushAction(action);
        });
    });

    telegramMealToggleInputs.forEach((checkbox) => {
        if (checkbox.dataset.bound) return;
        checkbox.dataset.bound = "1";
        checkbox.addEventListener("change", () => {
            if (!isMaster || isKey) return;
            const mealIndex = Number(checkbox.dataset.mealIndex || 0);
            if (!Number.isFinite(mealIndex) || mealIndex <= 0) return;
            saveTelegramMealToggle(mealIndex, checkbox.checked, checkbox);
        });
    });

    loadSettings();
    loadTelegramMealSlotDetails();

    // ===============================
    // BACKUP STATUS (LOAD FROM SERVER)
    // ===============================
    const backupTargetGithub = document.getElementById("backup-target-github");
    const backupTargetDropbox = document.getElementById("backup-target-dropbox");
    const backupTargetUsb = document.getElementById("backup-target-usb");
    const backupTargetGithubDetail = document.getElementById("backup-target-github-detail");
    const backupTargetDropboxDetail = document.getElementById("backup-target-dropbox-detail");
    const backupTargetUsbDetail = document.getElementById("backup-target-usb-detail");
    const backupLivePanel = document.getElementById("backup-live-panel");
    const backupStatusIcon = document.getElementById("backup-status");

    function formatDateTime(tsSeconds, fallback = "-") {
        if (!Number.isFinite(tsSeconds) || tsSeconds <= 0) return fallback;
        const date = new Date(tsSeconds * 1000);
        if (Number.isNaN(date.getTime())) return fallback;
        const dd = String(date.getDate()).padStart(2, "0");
        const mm = String(date.getMonth() + 1).padStart(2, "0");
        const yyyy = String(date.getFullYear());
        const hh = String(date.getHours()).padStart(2, "0");
        const min = String(date.getMinutes()).padStart(2, "0");
        return `${dd}.${mm}.${yyyy} - ${hh}:${min}`;
    }

    function setBackupTileState(tile, detailNode, status = "idle", detail = "") {
        if (!tile) return;
        tile.classList.remove("is-idle", "is-running", "is-ok", "is-error");
        tile.classList.add(`is-${status}`);
        if (detailNode) detailNode.textContent = detail || "-";
    }

    function setBackupStatusIcon(status = "idle") {
        if (!backupStatusIcon) return;
        backupStatusIcon.classList.remove("is-idle", "is-running", "is-ok", "is-error");
        backupStatusIcon.classList.add(`is-${status}`);
        const labels = {
            idle: "Status unbekannt",
            running: "Backup läuft",
            ok: "Backup erfolgreich",
            error: "Backup fehlgeschlagen",
        };
        backupStatusIcon.setAttribute("aria-label", labels[status] || labels.idle);
        backupStatusIcon.title = labels[status] || labels.idle;
    }

    function setBackupLiveVisible(visible) {
        if (!backupLivePanel) return;
        backupLivePanel.classList.toggle("is-hidden", !visible);
        backupLivePanel.setAttribute("aria-hidden", visible ? "false" : "true");
    }

    function applyBackupRows(github, dropbox, usb) {
        setBackupTileState(backupTargetGithub, backupTargetGithubDetail, github.status || "idle", github.detail || "Privates Repo");
        setBackupTileState(backupTargetDropbox, backupTargetDropboxDetail, dropbox.status || "idle", dropbox.detail || "Apps/Datenbanken aktuell");
        setBackupTileState(backupTargetUsb, backupTargetUsbDetail, usb.status || "idle", usb.detail || "USB-Ziele");
    }

    function renderBackupPayload(data, { running = false, showDetails = false } = {}) {
        if (!backupStatus) return;
        const lastNode = document.getElementById("backup-last");
        if (lastNode) {
            lastNode.textContent = formatDateTime(Number(data?.last_backup_ts || 0), "-");
        }

        if (running) {
            setBackupStatusIcon("running");
            setBackupLiveVisible(true);
            applyBackupRows(
                { status: "running", detail: "Commit + Push läuft" },
                { status: "running", detail: "Apps/Datenbanken wird synchronisiert" },
                { status: "running", detail: "USB-Ziele werden aktualisiert" },
            );
            return;
        }

        const targets = data?.targets || {};
        const github = targets.github || {};
        const dropbox = targets.dropbox || {};
        const usb = targets.usb || {};

        setBackupStatusIcon(data?.recent_ok ? "ok" : "error");
        setBackupLiveVisible(showDetails);
        if (showDetails) {
            applyBackupRows(github, dropbox, usb);
        }
    }

    async function loadBackupStatus({ showDetails = false } = {}) {
        if (!canReadAll) return;
        try {
            const res = await fetch("/api/settings/backup_status");
            if (!res.ok) return;

            const data = await res.json();
            if (!data.exists) return;
            renderBackupPayload(data, { showDetails });

        } catch (e) {
            console.warn("Backup status load failed", e);
        }
    }

    // ===============================
    // MANUAL BACKUP BUTTON
    // ===============================
    const backupBtn = document.getElementById("run-backup-btn");
    const backupStatus = document.getElementById("backup-status");

    if (isMaster && backupBtn) {
        backupBtn.addEventListener("click", async () => {
            backupBtn.disabled = true;
            renderBackupPayload({}, { running: true });

            try {
                const res = await authFetch("/api/settings/run_backup", {
                    method: "POST"
                });

                const data = await res.json();
                await loadBackupStatus({ showDetails: true });
                if (!data.ok) {
                    setBackupStatusIcon("error");
                }

            } catch (e) {
                setBackupStatusIcon("error");
                setBackupLiveVisible(true);
                setBackupTileState(backupTargetGithub, backupTargetGithubDetail, "error", "Status unbekannt");
                setBackupTileState(backupTargetDropbox, backupTargetDropboxDetail, "error", "Status unbekannt");
                setBackupTileState(backupTargetUsb, backupTargetUsbDetail, "error", "Status unbekannt");
            }

            backupBtn.disabled = false;
        });
    }

    // ===============================
    // INIT
    // ===============================
    if (canReadAll) {
        setBackupLiveVisible(false);
        loadBackupStatus({ showDetails: false });
    }


    // =====================================================
    // LIVE PI STATS (ONLY ON /settings)
    // =====================================================
    let cpuHistory = [];
    const CPU_WINDOW = 5;

    const isSettingsPage = window.location.pathname === "/settings";
    
    async function updatePiStats() {
        if (!isSettingsPage) return;

        try {
            const res = await fetch("/api/pi_stats");
            if (!res.ok) return;

            const d = await res.json();

            const set = (id, val) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.textContent =
                    (val !== undefined && val !== null && val !== "")
                        ? val
                        : "–";
            };

            // =====================
            // BASIC INFO
            // =====================
            set("pi-host", d.host);
            set("pi-os", d.os);
            set("pi-uptime", d.uptime);

            // =====================
            // CPU
            // Anzeige: "26.9 % (4 Cores)"
            // =====================
            if (d.cpu_percent != null) {
                cpuHistory.push(d.cpu_percent);
                if (cpuHistory.length > CPU_WINDOW) {
                    cpuHistory.shift();
                }

                const avgCpu =
                    cpuHistory.reduce((a, b) => a + b, 0) / cpuHistory.length;

                set("pi-cpu", `${avgCpu.toFixed(1)} % (${d.cpu_cores} Cores)`);
}
            // =====================
            // RAM
            // Anzeige: "2165.1 / 3796.7 MB (57.0 %)"
            // =====================
            set(
                "pi-ram",
                (d.ram_used_mb != null && d.ram_total_mb != null)
                    ? `${d.ram_used_mb} / ${d.ram_total_mb} MB`
                    : null
            );

            set(
                "pi-ram-percent",
                d.ram_percent != null ? `${d.ram_percent} %` : null
            );

            // =====================
            // DISK
            // Anzeige: "14.7 / 28.5 GB (51.6 %)"
            // =====================
            set(
                "pi-disk",
                (d.disk_used_gb != null && d.disk_total_gb != null)
                    ? `${d.disk_used_gb} / ${d.disk_total_gb} GB`
                    : null
            );

            set(
                "pi-disk-percent",
                d.disk_percent != null ? `${d.disk_percent} %` : null
            );

            // =====================
            // LOAD
            // Anzeige: "0.99 / 0.87 / 0.85"
            // =====================
            set("pi-load", d.load);

        } catch (e) {
            console.warn("Pi stats update failed", e);
        }
    }

    if (isSettingsPage) {
        updatePiStats();
        setInterval(updatePiStats, 5000);
    }

    const logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) {
        logoutBtn.addEventListener("click", () => {
            window.location.href = "/logout";
        });
    }

    if (canReadAll) {
        const accessKeysList = document.getElementById("access-keys-list");
        const accessKeysEmpty = document.getElementById("access-keys-empty");
        const accessKeyLabel = document.getElementById("access-key-label");
        const accessKeyExpires = document.getElementById("access-key-expires-at");
        const createAccessKeyBtn = document.getElementById("create-access-key-btn");
        const accessKeyPlain = document.getElementById("access-key-plain");
        const accessKeyPlainText = document.getElementById("access-key-plain-text");
        const accessKeyCopyBtn = document.getElementById("access-key-copy-btn");
        const accessKeysError = document.getElementById("access-keys-error");

        const defaultExpire = new Date();
        defaultExpire.setDate(defaultExpire.getDate() + 14);
        if (accessKeyExpires) {
            accessKeyExpires.value = toDateTimeLocal(defaultExpire);
            accessKeyExpires.min = toDateTimeLocal(new Date());
        }

        function toDateTimeLocal(date) {
            if (!date) return "";
            const d = new Date(date);
            if (Number.isNaN(d)) return "";
            const pad = (n) => String(n).padStart(2, "0");
            return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }

        function formatDatetimeDisplay(value) {
            if (!value) return "–";
            const d = new Date(value);
            if (Number.isNaN(d)) return value;
            return d.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
        }

        function formatFriendlyError(msg) {
            if (!msg) return "";
            const normalized = msg.trim().toLowerCase();
            if (
                normalized.startsWith("<!doctype") ||
                normalized.startsWith("<html") ||
                normalized.includes("internal server error")
            ) {
                return "Serverfehler (500) – bitte später erneut versuchen.";
            }
            return msg;
        }

        function showAccessKeyError(msg) {
            if (!accessKeysError) return;
            accessKeysError.textContent = formatFriendlyError(msg);
            accessKeysError.style.display = msg ? "block" : "none";
            if (msg && msg !== accessKeysError.textContent) {
                console.warn("access key error (raw):", msg);
            }
        }

        function showPlainKey(value) {
            if (!accessKeyPlain || !accessKeyPlainText) return;
            if (!value) {
                accessKeyPlain.style.display = "none";
                return;
            }
            accessKeyPlainText.textContent = value;
            accessKeyPlain.style.display = "flex";
            if (accessKeyCopyBtn) {
                accessKeyCopyBtn.disabled = false;
                accessKeyCopyBtn.textContent = "Copy";
            }
        }

        async function refreshAccessKeys() {
            if (!accessKeysList) return;
            accessKeysList.innerHTML = "";
            if (accessKeysEmpty) {
                accessKeysEmpty.style.display = "none";
            }
            try {
                const res = await authFetch("/api/settings/access_keys", { cache: "no-store" });
                const data = await parseJsonSafe(res);
                if (!res.ok) {
                    throw new Error(data.error || `Fehler ${res.status}`);
                }
                if (!Array.isArray(data.items)) {
                    throw new Error("Antwort konnte nicht verarbeitet werden.");
                }
                if (!data.items.length) {
                    if (accessKeysEmpty) {
                        accessKeysEmpty.style.display = "block";
                    }
                    return;
                }
                data.items.forEach((row) => {
                    const item = document.createElement("div");
                    item.className = "access-key-item";

                    const meta = document.createElement("div");
                    meta.className = "access-key-meta";

                    const status = row.revoked_at
                        ? "revoked"
                        : new Date(row.expires_at) <= new Date()
                        ? "expired"
                        : "active";
                    const statusLabel =
                        status === "revoked"
                            ? "gesperrt"
                            : status === "expired"
                            ? "abgelaufen"
                            : "aktiv";

                    const title = document.createElement("div");
                    title.className = "access-key-title";
                    const label = document.createElement("strong");
                    label.textContent = row.label || "Kein Label";
                    const statusTag = document.createElement("span");
                    statusTag.className = `access-key-status access-key-status-${status}`;
                    statusTag.textContent = statusLabel;
                    title.append(label, statusTag);

                    const sub = document.createElement("div");
                    sub.className = "access-key-sub";
                    const expiresSpan = document.createElement("span");
                    expiresSpan.textContent = `Expires: ${formatDatetimeDisplay(row.expires_at)}`;
                    const lastSeen = row.last_seen_at || row.created_at;
                    const lastSpan = document.createElement("span");
                    lastSpan.textContent = `Zuletzt: ${formatDatetimeDisplay(lastSeen)}`;
                    const statsSpan = document.createElement("span");
                    statsSpan.textContent = `Hits: ${row.hits_count ?? 0}`;
                    sub.append(expiresSpan, lastSpan, statsSpan);

                    meta.append(title, sub);

                    const actions = document.createElement("div");
                    actions.className = "access-key-actions";
                    if (!isKey) {
                        const deleteBtn = document.createElement("button");
                        deleteBtn.type = "button";
                        deleteBtn.className = "ghost-btn";
                        deleteBtn.dataset.action = "delete";
                        deleteBtn.dataset.id = row.id;
                        deleteBtn.textContent = "Löschen";
                        actions.append(deleteBtn);
                    }

                    const activityBtn = document.createElement("button");
                    activityBtn.type = "button";
                    activityBtn.className = "ghost-btn";
                    activityBtn.dataset.action = "toggle-activity";
                    activityBtn.dataset.id = row.id;
                    activityBtn.textContent = "Aktivität";
                    actions.append(activityBtn);

                    const extendSection = document.createElement("div");
                    extendSection.className = "access-key-extend";
                    extendSection.hidden = isKey;
                    const extendLabel = document.createElement("label");
                    extendLabel.style.display = "flex";
                    extendLabel.style.flexDirection = "column";
                    extendLabel.style.gap = "4px";
                    extendLabel.textContent = "Verlängern bis";
                    const extendInput = document.createElement("input");
                    extendInput.type = "datetime-local";
                    extendInput.className = "access-key-extend-input";
                    const defaultDate =
                        row.revoked_at
                            ? toDateTimeLocal(new Date(Date.now() + 14 * 86400000))
                            : toDateTimeLocal(row.expires_at);
                    extendInput.value = defaultDate;
                    extendLabel.appendChild(extendInput);
                    const extendBtn = document.createElement("button");
                    extendBtn.type = "button";
                    extendBtn.className = "ghost-btn";
                    extendBtn.dataset.action = row.revoked_at ? "restore" : "extend";
                    extendBtn.dataset.id = row.id;
                    extendBtn.textContent = row.revoked_at ? "Aktivieren" : "Setzen";
                    extendSection.append(extendLabel, extendBtn);

                    const activity = document.createElement("div");
                    activity.className = "access-key-activity is-hidden";
                    activity.dataset.keyId = String(row.id);
                    activity.dataset.loaded = "0";
                    activity.innerHTML = '<div class="settings-hint">Noch nicht geladen.</div>';

                    item.append(meta, actions, extendSection, activity);
                    accessKeysList.appendChild(item);
                });
            } catch (err) {
                showAccessKeyError(err.message || "Key-Liste konnte nicht geladen werden.");
                if (accessKeysEmpty) accessKeysEmpty.style.display = "block";
            }
        }

        accessKeysList?.addEventListener("click", async (evt) => {
            const button = evt.target.closest("button[data-action]");
            if (!button) return;
            const action = button.dataset.action;
            const id = button.dataset.id;
            if (!action || !id) return;
            if (action === "delete") {
                showAccessKeyError("");
                button.disabled = true;
                try {
                    const res = await authFetch(`/api/settings/access_keys/${id}`, { method: "DELETE" });
                    const data = await parseJsonSafe(res);
                    if (!res.ok || !data.ok) throw new Error(data.error || "Key konnte nicht gelöscht werden.");
                    await refreshAccessKeys();
                } catch (err) {
                    showAccessKeyError(err.message || "Key konnte nicht gelöscht werden.");
                } finally {
                    button.disabled = false;
                }
                return;
            }
            if (action === "toggle-activity") {
                const keyId = button.dataset.id;
                const card = button.closest(".access-key-item");
                const panel = card?.querySelector(".access-key-activity");
                if (!keyId || !panel) return;

                const hidden = panel.classList.contains("is-hidden");
                if (!hidden) {
                    panel.classList.add("is-hidden");
                    button.textContent = "Aktivität";
                    return;
                }

                panel.classList.remove("is-hidden");
                button.textContent = "Aktivität ausblenden";

                if (panel.dataset.loaded === "1") {
                    return;
                }

                panel.innerHTML = '<div class="settings-hint">Lade Aktivität…</div>';
                try {
                    const res = await authFetch(`/api/settings/access_keys/${keyId}/activity?summary=1`, { cache: "no-store" });
                    const data = await parseJsonSafe(res);
                    if (!res.ok || !Array.isArray(data.items)) {
                        throw new Error(data.error || "Aktivität konnte nicht geladen werden.");
                    }
                    if (!data.items.length) {
                        panel.innerHTML = '<div class="settings-hint">Keine Aktivität vorhanden.</div>';
                        panel.dataset.loaded = "1";
                        return;
                    }

                    const list = document.createElement("div");
                    list.className = "access-key-activity-list";
                    data.items.forEach((entry) => {
                        const row = document.createElement("div");
                        row.className = "access-key-activity-row";
                        const path = document.createElement("span");
                        path.className = "access-key-activity-path";
                        path.textContent = entry.path || "-";
                        const count = document.createElement("span");
                        count.textContent = `Hits: ${entry.count ?? 0}`;
                        const ts = document.createElement("span");
                        ts.textContent = formatDatetimeDisplay(entry.last_ts);
                        row.append(path, count, ts);
                        list.appendChild(row);
                    });
                    panel.innerHTML = "";
                    panel.appendChild(list);
                    panel.dataset.loaded = "1";
                } catch (err) {
                    panel.innerHTML = `<div class="access-key-error">${(err && err.message) || "Aktivität konnte nicht geladen werden."}</div>`;
                }
                return;
            }
            if (action === "restore") {
                showAccessKeyError("");
                button.disabled = true;
                const extendInput = button
                    .closest(".access-key-extend")
                    ?.querySelector(".access-key-extend-input");
                const value = extendInput?.value;
                if (!value) {
                    showAccessKeyError("Ablaufdatum wählen.");
                    button.disabled = false;
                    return;
                }
                const parsed = new Date(value);
                if (Number.isNaN(parsed) || parsed <= new Date()) {
                    showAccessKeyError("Ablaufdatum muss in der Zukunft liegen.");
                    button.disabled = false;
                    return;
                }
                try {
                    const res = await authFetch(`/api/settings/access_keys/${id}/restore`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ expires_at: parsed.toISOString() }),
                    });
                    const data = await parseJsonSafe(res);
                    if (!res.ok || !data.ok) {
                        throw new Error(data.error || "Aktivierung fehlgeschlagen.");
                    }
                    await refreshAccessKeys();
                } catch (err) {
                    showAccessKeyError(err.message || "Aktivierung fehlgeschlagen.");
                } finally {
                    button.disabled = false;
                }
                return;
            }
            if (action === "extend") {
                showAccessKeyError("");
                const input = button
                    .closest(".access-key-extend")
                    ?.querySelector(".access-key-extend-input");
                const value = input?.value;
                if (!value) {
                    showAccessKeyError("Ablaufdatum wählen.");
                    return;
                }
                const parsed = new Date(value);
                if (Number.isNaN(parsed)) {
                    showAccessKeyError("Ungültiges Datum.");
                    return;
                }
                button.disabled = true;
                try {
                    const res = await authFetch(`/api/settings/access_keys/${id}/extend`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ expires_at: parsed.toISOString() }),
                    });
                    const errData = await parseJsonSafe(res);
                    if (!res.ok) {
                        throw new Error(errData.error || "Extend fehlgeschlagen.");
                    }
                    await refreshAccessKeys();
                } catch (err) {
                    showAccessKeyError(err.message || "Extend fehlgeschlagen.");
                } finally {
                    button.disabled = false;
                }
            }
        });

        accessKeyCopyBtn?.addEventListener("click", async () => {
            const text = accessKeyPlainText?.textContent?.trim();
            if (!text) return;
            const ok = await copyToClipboard(text);
            if (!ok) {
                showAccessKeyError("Copy fehlgeschlagen.");
                return;
            }
            if (accessKeyCopyBtn) {
                accessKeyCopyBtn.textContent = "Copied ✅";
                accessKeyCopyBtn.disabled = true;
                setTimeout(() => {
                    if (accessKeyCopyBtn) {
                        accessKeyCopyBtn.textContent = "Copy";
                        accessKeyCopyBtn.disabled = false;
                    }
                }, 1000);
            }
        });

        createAccessKeyBtn?.addEventListener("click", async () => {
            showAccessKeyError("");
            const label = (accessKeyLabel?.value || "").trim();
            const expiresValue = (accessKeyExpires?.value || "").trim();
            if (!label) {
                showAccessKeyError("Label erforderlich.");
                return;
            }
            if (!expiresValue) {
                showAccessKeyError("Ablaufdatum erforderlich.");
                return;
            }
            const targetDate = new Date(expiresValue);
            if (Number.isNaN(targetDate)) {
                showAccessKeyError("Ungültiges Ablaufdatum.");
                return;
            }
            const now = new Date();
            const diffDays = Math.max(1, Math.min(365, Math.ceil((targetDate - now) / 86400000)));
            createAccessKeyBtn.disabled = true;
            try {
                const res = await authFetch("/api/settings/access_keys", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ label, days: diffDays }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok || !data.ok) {
                    throw new Error(data.error || "Key konnte nicht erstellt werden.");
                }
                showPlainKey(data.plain_key);
                if (accessKeyLabel) accessKeyLabel.value = "";
                if (accessKeyExpires) {
                    const nextDefault = new Date();
                    nextDefault.setDate(nextDefault.getDate() + 14);
                    accessKeyExpires.value = toDateTimeLocal(nextDefault);
                }
                await refreshAccessKeys();
            } catch (err) {
                showAccessKeyError(err.message || "Key konnte nicht erstellt werden.");
            } finally {
                createAccessKeyBtn.disabled = false;
            }
        });

        refreshAccessKeys();
    }

    // Master-Panel button - load terminal.js if needed
    const masterPanelBtn = document.getElementById("master-panel-btn");
    if (masterPanelBtn) {
        let terminalLoader = null;
        
        function ensureTerminalLoaded() {
            if (typeof window.openLIVACmdAbout === "function") {
                return Promise.resolve();
            }
            if (terminalLoader) return terminalLoader;
            terminalLoader = new Promise((resolve, reject) => {
                const script = document.createElement("script");
                script.src = "/static/js/terminal.js?v=20260510_perf_fix_1";
                script.defer = true;
                script.onload = () => {
                    console.log("terminal.js loaded");
                    resolve();
                };
                script.onerror = (err) => {
                    console.error("Failed to load terminal.js", err);
                    reject(err);
                };
                document.body.appendChild(script);
            });
            return terminalLoader;
        }
        
        masterPanelBtn.addEventListener("click", async (e) => {
            e.preventDefault();
            e.stopPropagation();
            try {
                console.log("Master panel clicked, loading terminal...");
                await ensureTerminalLoaded();
                console.log("Terminal loaded, opening CMD...");
                if (typeof window.openLIVACmdAbout === "function") {
                    window.openLIVACmdAbout();
                    console.log("CMD opened");
                } else {
                    console.error("openLIVACmdAbout still not available");
                }
            } catch (err) {
                console.error("Error opening master panel:", err);
            }
        });
    }
});
