(function () {
  "use strict";

  const workspace = document.getElementById("evaluation-workspace");
  if (!workspace) return;

  const MS_PER_DAY = 86400000;

  const areaOptions = Array.from(workspace.querySelectorAll(".evaluation-area-option[data-area]"));
  const panels = {
    gesamt: document.getElementById("evaluation-panel-gesamt"),
    training: document.getElementById("evaluation-panel-training"),
    ernaehrung: document.getElementById("evaluation-panel-ernaehrung"),
  };
  const timeline = workspace.querySelector(".evaluation-timeline");
  const timelineControl = document.getElementById("evaluation-timeline-control");
  const startInput = document.getElementById("evaluation-range-start");
  const endInput = document.getElementById("evaluation-range-end");
  const startLabel = document.getElementById("evaluation-start-label");
  const endLabel = document.getElementById("evaluation-end-label");
  const rangeValue = document.getElementById("evaluation-range-value");
  const ticks = document.getElementById("evaluation-timeline-ticks");
  const rangePresets = Array.from(workspace.querySelectorAll("[data-evaluation-range]"));

  function utcToday() {
    const now = new Date();
    return new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
  }

  const maxDate = utcToday();
  const compactTimeline = window.matchMedia("(max-width: 720px)");

  function toIso(value) {
    return value.toISOString().slice(0, 10);
  }

  function fromIso(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ""))) return null;
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  const fallbackMinDate = new Date(maxDate.getTime() - 1825 * MS_PER_DAY);
  const minDates = {
    gesamt: fromIso(workspace.dataset.gesamtMin) || fallbackMinDate,
    training: fromIso(workspace.dataset.trainingMin) || fallbackMinDate,
    ernaehrung: fromIso(workspace.dataset.ernaehrungMin) || fallbackMinDate,
  };

  function minDateFor(area) {
    return minDates[area === "ernaehrung" ? "ernaehrung" : area === "gesamt" ? "gesamt" : "training"];
  }

  function timelineBoundsFor(area) {
    const absoluteMin = minDateFor(area);
    if (!compactTimeline.matches || !intervals?.[area]) return { min: absoluteMin, max: maxDate };
    const selectedStart = fromIso(intervals[area].start) || absoluteMin;
    const selectedEnd = fromIso(intervals[area].end) || maxDate;
    const selectedDays = Math.max(1, Math.round((selectedEnd - selectedStart) / MS_PER_DAY) + 1);
    const windowDays = Math.max(90, Math.round(selectedDays * 1.8));
    const sideDays = Math.max(0, Math.round((windowDays - selectedDays) / 2));
    let min = new Date(selectedStart.getTime() - sideDays * MS_PER_DAY);
    let max = new Date(selectedEnd.getTime() + sideDays * MS_PER_DAY);
    if (min < absoluteMin) { max = new Date(Math.min(maxDate.getTime(), max.getTime() + (absoluteMin - min))); min = new Date(absoluteMin); }
    if (max > maxDate) { min = new Date(Math.max(absoluteMin.getTime(), min.getTime() - (max - maxDate))); max = new Date(maxDate); }
    return { min, max };
  }

  function historyDaysFor(area) {
    const bounds = timelineBoundsFor(area);
    return Math.max(1, Math.round((bounds.max - bounds.min) / MS_PER_DAY));
  }

  function clampDate(value, area) {
    if (!value) return null;
    const minDate = minDateFor(area);
    if (value < minDate) return new Date(minDate);
    if (value > maxDate) return new Date(maxDate);
    return value;
  }

  function dayIndex(value, area) {
    return Math.round((value.getTime() - timelineBoundsFor(area).min.getTime()) / MS_PER_DAY);
  }

  function dateAtIndex(index, area) {
    return new Date(timelineBoundsFor(area).min.getTime() + Number(index) * MS_PER_DAY);
  }

  function formatDate(value, withYear = true) {
    return new Intl.DateTimeFormat("de-DE", {
      day: "2-digit",
      month: "2-digit",
      ...(withYear ? { year: "numeric" } : {}),
      timeZone: "UTC",
    }).format(value);
  }

  function oneCalendarYearBefore(value) {
    const result = new Date(value);
    const targetDay = result.getUTCDate();
    result.setUTCDate(1);
    result.setUTCFullYear(result.getUTCFullYear() - 1);
    const lastDayOfTargetMonth = new Date(Date.UTC(
      result.getUTCFullYear(),
      result.getUTCMonth() + 1,
      0,
    )).getUTCDate();
    result.setUTCDate(Math.min(targetDay, lastDayOfTargetMonth));
    return result;
  }

  function defaultInterval(area) {
    const start = clampDate(new Date(maxDate.getTime() - 89 * MS_PER_DAY), area);
    return {
      start: toIso(start),
      end: toIso(maxDate),
    };
  }

  const intervals = {
    gesamt: defaultInterval("gesamt"),
    training: defaultInterval("training"),
    ernaehrung: defaultInterval("ernaehrung"),
  };

  function activeArea() {
    if (workspace.dataset.area === "ernaehrung") return "ernaehrung";
    if (workspace.dataset.area === "gesamt") return "gesamt";
    return "training";
  }

  function getInterval(area) {
    const normalized = area === "ernaehrung" ? "ernaehrung" : area === "gesamt" ? "gesamt" : "training";
    return { ...intervals[normalized] };
  }

  function getRange(area) {
    const interval = getInterval(area);
    return String(Math.round((fromIso(interval.end) - fromIso(interval.start)) / MS_PER_DAY) + 1);
  }

  function createTicks() {
    if (!ticks) return;
    const area = activeArea();
    const historyDays = historyDaysFor(area);
    const count = compactTimeline.matches ? 4 : 6;
    const formatter = new Intl.DateTimeFormat("de-DE", compactTimeline.matches
      ? { day: "2-digit", month: "2-digit", timeZone: "UTC" }
      : { month: "short", year: "2-digit", timeZone: "UTC" });
    ticks.innerHTML = Array.from({ length: count }, (_, index) => {
      const offset = Math.round((historyDays * index) / (count - 1));
      return `<span>${formatter.format(dateAtIndex(offset, area)).replace(" ", " ")}</span>`;
    }).join("");
  }

  function syncTimeline() {
    const area = activeArea();
    const interval = getInterval(area);
    const historyDays = historyDaysFor(area);
    const start = fromIso(interval.start);
    const end = fromIso(interval.end);
    const startPosition = dayIndex(start, area);
    const endPosition = dayIndex(end, area);
    const startPct = (startPosition / historyDays) * 100;
    const endPct = (endPosition / historyDays) * 100;

    startInput.max = String(historyDays);
    endInput.max = String(historyDays);
    startInput.value = String(startPosition);
    endInput.value = String(endPosition);
    timelineControl.style.setProperty("--start-position", `${startPct}%`);
    timelineControl.style.setProperty("--end-position", `${endPct}%`);
    timelineControl.classList.toggle("is-tight", endPct - startPct < 12);
    timelineControl.classList.toggle("is-start-edge", startPct < 6);
    timelineControl.classList.toggle("is-end-edge", endPct > 94);

    const startText = formatDate(start);
    const endText = formatDate(end);
    startLabel.textContent = formatDate(start, false);
    endLabel.textContent = end.getTime() === maxDate.getTime() ? "Heute" : formatDate(end, false);
    rangeValue.textContent = `${startText} – ${endText}`;
    const selectedDays = Math.round((end - start) / MS_PER_DAY) + 1;
    rangePresets.forEach((button) => button.classList.toggle("is-active", Number(button.dataset.evaluationRange) === selectedDays));
  }

  let notifyTimer = null;
  function notifyRangeChange() {
    window.clearTimeout(notifyTimer);
    notifyTimer = window.setTimeout(() => {
      const area = activeArea();
      const interval = getInterval(area);
      const days = Number(getRange(area));
      timeline?.classList.add("is-updating");
      window.setTimeout(() => timeline?.classList.remove("is-updating"), 650);
      window.dispatchEvent(new CustomEvent("liva:analysis-range-change", {
        detail: { area, ...interval, days },
      }));
    }, 140);
  }

  function setIntervalFromInputs(changedInput, notify) {
    let startIndex = Number(startInput.value);
    let endIndex = Number(endInput.value);
    if (startIndex > endIndex) {
      if (changedInput === startInput) endIndex = startIndex;
      else startIndex = endIndex;
    }

    const area = activeArea();
    intervals[area] = {
      start: toIso(dateAtIndex(startIndex, area)),
      end: toIso(dateAtIndex(endIndex, area)),
    };
    createTicks();
    syncTimeline();
    if (notify) notifyRangeChange();
  }

  function setIntervalForArea(area, nextInterval, notify = true) {
    const normalized = area === "ernaehrung" ? "ernaehrung" : area === "gesamt" ? "gesamt" : "training";
    let start = clampDate(fromIso(nextInterval?.start), normalized);
    let end = clampDate(fromIso(nextInterval?.end), normalized);
    if (!start || !end) return;
    if (start > end) [start, end] = [end, start];
    intervals[normalized] = { start: toIso(start), end: toIso(end) };
    if (normalized !== activeArea()) return;
    createTicks();
    syncTimeline();
    if (notify) notifyRangeChange();
  }

  function setArea(nextArea, updateUrl) {
    const area = nextArea === "ernaehrung" ? "ernaehrung" : nextArea === "gesamt" ? "gesamt" : "training";
    workspace.dataset.area = area;
    areaOptions.forEach((option) => {
      const active = option.dataset.area === area;
      option.classList.toggle("is-active", active);
      option.setAttribute("aria-selected", active ? "true" : "false");
      option.tabIndex = active ? 0 : -1;
    });
    Object.entries(panels).forEach(([name, panel]) => {
      if (panel) panel.hidden = name !== area;
    });
    createTicks();
    syncTimeline();

    if (updateUrl) {
      const url = new URL(window.location.href);
      if (area === "gesamt") url.searchParams.delete("bereich");
      else url.searchParams.set("bereich", area);
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    }
    window.dispatchEvent(new CustomEvent("liva:analysis-area-change", {
      detail: { area },
    }));
    window.requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }

  areaOptions.forEach((option, index) => {
    option.addEventListener("click", () => setArea(option.dataset.area, true));
    option.addEventListener("keydown", (event) => {
      if (!event.key.startsWith("Arrow")) return;
      event.preventDefault();
      const direction = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
      const target = areaOptions[(index + direction + areaOptions.length) % areaOptions.length];
      target.focus();
      setArea(target.dataset.area, true);
    });
  });

  [startInput, endInput].forEach((input) => {
    input.addEventListener("input", () => setIntervalFromInputs(input, true));
    input.addEventListener("change", () => setIntervalFromInputs(input, true));
  });

  rangePresets.forEach((button) => {
    button.addEventListener("click", () => {
      const area = activeArea();
      const days = Number(button.dataset.evaluationRange);
      const end = maxDate;
      const start = clampDate(new Date(end.getTime() - (days - 1) * MS_PER_DAY), area);
      intervals[area] = { start: toIso(start), end: toIso(end) };
      createTicks();
      syncTimeline();
      notifyRangeChange();
    });
  });

  window.LivaEvaluationControls = {
    getArea: activeArea,
    getRange,
    getInterval,
    setInterval: setIntervalForArea,
  };

  compactTimeline.addEventListener?.("change", () => { createTicks(); syncTimeline(); });
  setArea(workspace.dataset.area, false);
})();
