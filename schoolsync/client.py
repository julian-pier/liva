from __future__ import annotations

import logging
import time
from datetime import date, timedelta
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.parse import urljoin

from playwright.sync_api import Frame, Locator, Page, sync_playwright

from .config import SchoolSyncConfig
from .models import RawLessonBlock

LOGGER = logging.getLogger(__name__)


class SchoolSyncError(RuntimeError):
    pass


class AuthStateExpiredError(SchoolSyncError):
    pass


class WebUntisClient:
    def __init__(self, config: SchoolSyncConfig) -> None:
        self.config = config

    def fetch_visible_raw_blocks(self) -> tuple[str, list[RawLessonBlock]]:
        if not self.config.base_url:
            raise SchoolSyncError("WEBUNTIS_BASE_URL is not configured.")
        if not self.config.state_file.exists():
            raise AuthStateExpiredError(
                f"Auth state missing: {self.config.state_file}. "
                "Run scripts/webuntis_auth_bootstrap.py first."
            )

        today_iso = date.today().isoformat()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.config.headless)
            context = browser.new_context(storage_state=str(self.config.state_file))
            page = context.new_page()
            try:
                try:
                    self._load_with_retries(page, self.config.base_url)
                except AuthStateExpiredError:
                    if not (self.config.webuntis_username and self.config.webuntis_password):
                        raise
                    LOGGER.info("Stored WebUntis session is invalid; logging in again inside the fetch browser.")
                    context.clear_cookies()
                    page.goto(
                        self.config.base_url,
                        wait_until="domcontentloaded",
                        timeout=self.config.navigation_timeout_ms,
                    )
                    # Keep authentication and timetable extraction in this same
                    # browser process. This tenant invalidates the new session
                    # when the bootstrap browser closes before extraction.
                    from .auth import _wait_until_schedule_visible

                    _wait_until_schedule_visible(
                        page,
                        self.config,
                        max_wait_seconds=180,
                        allow_credential_login=True,
                    )
                self._ensure_logged_in(page)
                self._go_to_my_timetable(page)
                try:
                    self.wait_for_any_visible(
                        page=page,
                        selectors=(
                            "[data-testid='timetable-grid']",
                            "[data-testid^='timetable-grid-card']",
                            ".lesson-card",
                        ),
                        timeout_ms=min(8_000, self.config.wait_timeout_ms),
                    )
                except Exception:  # noqa: BLE001
                    # Only fall back to the legacy embedded timetable when the
                    # modern timetable did not materialize after navigation.
                    self._open_embedded_timetable_page(page)
                candidates: list[tuple[str, list[RawLessonBlock]]] = []

                blocks_initial = self._read_blocks(page, context_label="initial")
                candidates.append(("initial", blocks_initial))

                self._go_to_today(page)
                blocks_today = self._read_blocks(page, context_label="after_today")
                candidates.append(("after_today", blocks_today))

                self._go_to_week(page)
                blocks_week = self._read_blocks(page, context_label="after_week")
                candidates.append(("after_week", blocks_week))
                if self._block_set_contains_date(blocks_week, today_iso):
                    best_label, best_blocks = "after_week", blocks_week
                else:
                    month_blocks: list[RawLessonBlock] = list(blocks_week)
                    for week_idx in range(1, 4):
                        if not self._go_to_next_week(page):
                            break
                        nxt = self._read_blocks(page, context_label=f"after_next_week_{week_idx}")
                        month_blocks.extend(nxt)
                    if month_blocks:
                        candidates.append(("after_month", self._dedupe_raw_blocks(month_blocks)))
                    direct_blocks = self._collect_weeks_via_date_param(page, weeks=4)
                    if direct_blocks:
                        candidates.append(("direct_4weeks", direct_blocks))

                    if not blocks_week:
                        page.wait_for_timeout(400)
                        self._go_to_week(page)
                        blocks_week_retry = self._read_blocks(page, context_label="after_week_retry")
                        candidates.append(("after_week_retry", blocks_week_retry))

                    best_label, best_blocks = self._choose_best_block_set(candidates, today_iso=today_iso)
                LOGGER.info("Using timetable block set '%s' with %s entries.", best_label, len(best_blocks))
                blocks = self._dedupe_raw_blocks(best_blocks)
                if self.config.debug_enabled:
                    self._write_debug_artifacts(page, prefix="last")
                if not blocks and self.config.debug_enabled:
                    self._write_debug_artifacts(page, prefix="empty")
                self._persist_auth_state(context)
                return today_iso, blocks
            finally:
                context.close()
                browser.close()

    def fetch_today_raw_blocks(self) -> tuple[str, list[RawLessonBlock]]:
        # Backward-compatible alias: now returns all visible timetable blocks, with today as focus date.
        return self.fetch_visible_raw_blocks()

    def _load_with_retries(self, page: Page, url: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.config.retries + 2):
            try:
                LOGGER.info("Opening WebUntis (attempt %s/%s)", attempt, self.config.retries + 1)
                page.goto(url, wait_until="domcontentloaded", timeout=self.config.navigation_timeout_ms)
                if self._has_any_visible(page, self.config.selectors.login_form_selectors):
                    raise AuthStateExpiredError(
                        "Login state appears expired. Please run scripts/webuntis_auth_bootstrap.py again."
                    )
                if self._was_redirected_to_empty_start_page(page):
                    # WebUntis invalidates an existing session by redirecting it to
                    # the host root. That page is intentionally blank and does not
                    # contain the usual login controls, so detecting only the login
                    # form turns an expired session into a generic timetable timeout.
                    raise AuthStateExpiredError(
                        "Login state appears expired (WebUntis redirected to its empty start page). "
                        "Please run scripts/webuntis_auth_bootstrap.py again."
                    )
                try:
                    self.wait_for_any_visible(
                        page=page,
                        selectors=self.config.selectors.schedule_visible_selectors,
                        timeout_ms=self.config.wait_timeout_ms,
                    )
                except Exception:
                    # Some tenants land on a dashboard first. If timetable navigation is
                    # visible, continue and open the timetable explicitly in later steps.
                    if not self._has_timetable_navigation_surface(page):
                        raise
                return
            except AuthStateExpiredError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                LOGGER.warning("Load attempt %s failed: %s", attempt, exc)
                time.sleep(1.0)
        raise SchoolSyncError(f"Failed to load timetable after retries: {last_error}") from last_error

    @staticmethod
    def _was_redirected_to_empty_start_page(page: Page) -> bool:
        """Recognize WebUntis' blank host-root response for an invalid session."""
        parsed = urlsplit(page.url or "")
        if parsed.path.rstrip("/"):
            return False
        try:
            return page.locator("body").inner_text(timeout=1_000).strip() == ""
        except Exception:  # noqa: BLE001
            # A host-root redirect is never a valid WebUntis timetable route.
            return True

    def _persist_auth_state(self, context) -> None:
        """Keep WebUntis' rotated session cookie for the next scheduled run."""
        try:
            context.storage_state(path=str(self.config.state_file))
        except Exception as exc:  # noqa: BLE001
            # The timetable result remains useful even if the state cannot be
            # written; a later run can recover through the auth bootstrap.
            LOGGER.warning("Could not persist refreshed WebUntis auth state: %s", exc)

    def _has_timetable_navigation_surface(self, page: Page) -> bool:
        try:
            frame_loc = page.locator("iframe#embedded-webuntis").first
            if frame_loc.count() > 0 and frame_loc.is_visible():
                return True
        except Exception:  # noqa: BLE001
            pass
        for selector in self.config.selectors.timetable_menu_selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _ensure_logged_in(self, page: Page) -> None:
        if self._has_any_visible(page, self.config.selectors.login_form_selectors):
            raise AuthStateExpiredError(
                "Login state appears expired. Please run scripts/webuntis_auth_bootstrap.py again."
            )

    def _go_to_today(self, page: Page) -> None:
        for selector in self.config.selectors.today_button_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0 and locator.is_visible():
                    LOGGER.info("Clicking today button via selector: %s", selector)
                    locator.click(timeout=self.config.wait_timeout_ms)
                    break
            except Exception:  # noqa: BLE001
                continue

        # Even if click wasn't possible, we still wait for visible schedule and continue.
        try:
            self.wait_for_any_visible(
                page=page,
                selectors=self.config.selectors.schedule_visible_selectors,
                timeout_ms=self.config.wait_timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Schedule visibility check after today click failed (continuing): %s", exc)

    def _go_to_my_timetable(self, page: Page) -> None:
        for selector in self.config.selectors.timetable_menu_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0 and locator.is_visible():
                    LOGGER.info("Clicking timetable menu via selector: %s", selector)
                    locator.click(timeout=self.config.wait_timeout_ms)
                    page.wait_for_timeout(600)
                    return
            except Exception:  # noqa: BLE001
                continue

    def _open_embedded_timetable_page(self, page: Page) -> None:
        original_url = page.url
        try:
            iframe = page.locator("iframe#embedded-webuntis").first
            if iframe.count() <= 0:
                return
            src = iframe.get_attribute("src")
            if not src:
                return
            target = urljoin(page.url, src)
            LOGGER.info("Opening embedded timetable URL directly: %s", target)
            page.goto(target, wait_until="domcontentloaded", timeout=self.config.navigation_timeout_ms)
            self.wait_for_any_visible(
                page=page,
                selectors=self.config.selectors.schedule_visible_selectors,
                timeout_ms=self.config.wait_timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Could not open embedded timetable directly: %s", exc)
            try:
                if original_url:
                    page.goto(original_url, wait_until="domcontentloaded", timeout=self.config.navigation_timeout_ms)
                    page.wait_for_timeout(400)
                    self._go_to_my_timetable(page)
            except Exception as recovery_exc:  # noqa: BLE001
                LOGGER.info("Recovery after embedded open failure did not complete cleanly: %s", recovery_exc)

    def _go_to_week(self, page: Page) -> None:
        for selector in self.config.selectors.week_button_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0 and locator.is_visible():
                    LOGGER.info("Clicking week button via selector: %s", selector)
                    locator.click(timeout=self.config.wait_timeout_ms)
                    try:
                        self.wait_for_any_visible(
                            page=page,
                            selectors=self.config.selectors.schedule_visible_selectors,
                            timeout_ms=self.config.wait_timeout_ms,
                        )
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.info("Schedule visibility check after week click failed (continuing): %s", exc)
                    return
            except Exception:  # noqa: BLE001
                continue

    def _go_to_next_week(self, page: Page) -> bool:
        for selector in self.config.selectors.next_week_button_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0 and locator.is_visible():
                    LOGGER.info("Clicking next-week button via selector: %s", selector)
                    locator.click(timeout=self.config.wait_timeout_ms)
                    page.wait_for_timeout(700)
                    try:
                        self.wait_for_any_visible(
                            page=page,
                            selectors=self.config.selectors.schedule_visible_selectors,
                            timeout_ms=self.config.wait_timeout_ms,
                        )
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.info("Schedule visibility check after next-week click failed (continuing): %s", exc)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _collect_weeks_via_date_param(self, page: Page, *, weeks: int = 4) -> list[RawLessonBlock]:
        if weeks <= 0:
            return []
        base_url = page.url or self.config.base_url
        if not base_url:
            return []
        monday = date.today() - timedelta(days=date.today().weekday())
        all_blocks: list[RawLessonBlock] = []
        for idx in range(weeks):
            week_date = (monday + timedelta(days=7 * idx)).isoformat()
            target_url = self._with_date_query(base_url, week_date)
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=self.config.navigation_timeout_ms)
                self.wait_for_any_visible(
                    page=page,
                    selectors=self.config.selectors.schedule_visible_selectors,
                    timeout_ms=self.config.wait_timeout_ms,
                )
                self._go_to_week(page)
                page.wait_for_timeout(450)
                blocks = self._read_blocks(page, context_label=f"direct_week_{idx + 1}")
                all_blocks.extend(blocks)
            except Exception as exc:  # noqa: BLE001
                LOGGER.info("Direct week fetch failed for %s: %s", week_date, exc)
                continue
        return self._dedupe_raw_blocks(all_blocks)

    @staticmethod
    def _with_date_query(url: str, date_iso: str) -> str:
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["date"] = str(date_iso or "").strip()
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    def _read_blocks(self, page: Page, context_label: str = "default") -> list[RawLessonBlock]:
        selector_counts: list[tuple[str, int]] = []
        frame = self._find_embedded_frame(page)
        counts_page, blocks_page = self._read_blocks_in_context(page, context_label=f"{context_label}:page")
        selector_counts.extend(counts_page)
        counts_frame: list[tuple[str, int]] = []
        blocks_frame: list[RawLessonBlock] = []
        if frame is not None:
            counts_frame, blocks_frame = self._read_blocks_in_context(frame, context_label=f"{context_label}:frame")
            selector_counts.extend(counts_frame)
        else:
            LOGGER.info("No embedded timetable iframe detected for %s.", context_label)
        LOGGER.info("Lesson selector counts (%s): %s", context_label, selector_counts)
        merged = self._dedupe_raw_blocks([*blocks_page, *blocks_frame])
        if not merged:
            LOGGER.info("No lesson blocks found in visible timetable (%s).", context_label)
        return merged

    def _read_blocks_in_context(
        self, root: Page | Frame, context_label: str
    ) -> tuple[list[tuple[str, int]], list[RawLessonBlock]]:
        selector_counts: list[tuple[str, int]] = []
        for selector in self.config.selectors.lesson_block_selectors:
            try:
                count = root.locator(selector).count()
            except Exception:  # noqa: BLE001
                count = 0
            selector_counts.append((selector, count))

        structured = self._extract_structured_timetable_blocks(root)
        if structured:
            card_selector_count = max(
                (
                    count
                    for selector, count in selector_counts
                    if selector in {".lesson-card.clickable", ".lesson-card.no-shadow.clickable"}
                ),
                default=0,
            )
            if card_selector_count > 0 and len(structured) * 2 < card_selector_count:
                LOGGER.info(
                    "Structured extraction looks incomplete (%s vs lesson-card count %s) in %s; falling back.",
                    len(structured),
                    card_selector_count,
                    context_label,
                )
            else:
                selector_counts.append(("__structured_timetable__", len(structured)))
                return selector_counts, structured

        matching = [(selector, count) for selector, count in selector_counts if count > 0]
        if not matching:
            return selector_counts, []
        preferred = [
            (selector, count)
            for selector, count in matching
            if selector in {".lesson-card.clickable", ".lesson-card.no-shadow.clickable"}
        ]
        if preferred:
            matching = preferred

        time_markers, day_markers, year_hint = self._collect_grid_markers(root)
        time_labels, day_labels, year_hint_text = self._collect_text_markers(root)
        if year_hint is None:
            year_hint = year_hint_text
        matching.sort(key=lambda x: x[1])

        result: list[RawLessonBlock] = []
        for selector, count in matching:
            LOGGER.info("Reading %s candidates via selector '%s' (%s).", count, selector, context_label)
            locator = root.locator(selector)
            for idx in range(count):
                item = locator.nth(idx)
                raw_text = self._safe_inner_text(item)
                bbox = self._safe_bounding_box(item)
                derived_date = self._derive_date_from_bbox(bbox, day_markers, year_hint=year_hint)
                derived_start, derived_end = self._derive_time_range_from_bbox(bbox, time_markers)
                data_start = self._safe_attr(item, "data-start")
                data_end = self._safe_attr(item, "data-end")
                if not data_start and derived_start:
                    data_start = derived_start
                if not data_end and derived_end:
                    data_end = derived_end
                result.append(
                    RawLessonBlock(
                        raw_text=raw_text,
                        selector=selector,
                        class_name=self._safe_attr(item, "class"),
                        title=self._safe_attr(item, "title"),
                        aria_label=self._safe_attr(item, "aria-label"),
                        data_date=self._safe_attr(item, "data-date") or self._safe_attr(item, "data-day") or derived_date,
                        data_start=data_start,
                        data_end=data_end,
                        data_start_datetime=self._safe_attr(item, "data-start-date")
                        or self._safe_attr(item, "data-start-datetime"),
                        data_end_datetime=self._safe_attr(item, "data-end-date")
                        or self._safe_attr(item, "data-end-datetime"),
                        data_subject=self._safe_attr(item, "data-subject"),
                        data_teacher=self._safe_attr(item, "data-teacher"),
                        data_room=self._safe_attr(item, "data-room"),
                        bbox_top=(bbox or {}).get("y"),
                        bbox_bottom=((bbox or {}).get("y", 0.0) + (bbox or {}).get("height", 0.0)) if bbox else None,
                        bbox_left=(bbox or {}).get("x"),
                    )
                )
        result = self._dedupe_raw_blocks(result)
        if result:
            result = self._enrich_blocks_from_text_layout(
                result,
                time_labels=time_labels,
                day_labels=day_labels,
                year_hint=year_hint,
            )
        return selector_counts, result

    def _extract_structured_timetable_blocks(self, root: Page | Frame) -> list[RawLessonBlock]:
        try:
            payload = root.evaluate(
                """
                async () => {
                  const normalize = (s) => (s || "").replace(/\\s+/g, " ").trim();
                  const yearFromText = (s) => {
                    const m = (s || "").match(/\\b(20\\d{2})\\b/);
                    return m ? Number(m[1]) : null;
                  };
                  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                  const isGreenBackground = (color) => {
                    if (!color) return false;
                    const m = color.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                    if (!m) return false;
                    const r = Number(m[1]); const g = Number(m[2]); const b = Number(m[3]);
                    return g >= r + 10 && g >= b + 10;
                  };
                  const isYellowBackground = (color) => {
                    if (!color) return false;
                    const m = color.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                    if (!m) return false;
                    const r = Number(m[1]); const g = Number(m[2]); const b = Number(m[3]);
                    return r >= 220 && g >= 180 && b <= 170;
                  };
                  const hasAnyToken = (text, tokens) => {
                    const hay = normalize(text || "");
                    const normTokens = (tokens || []).map((t) => normalize(t)).filter(Boolean);
                    if (!normTokens.length) return true;
                    return normTokens.some((t) => hay.includes(t));
                  };
                  const readDetailPanelText = () => {
                    const selectors = [
                      ".calendar-entry-detail-view",
                      ".ant-modal-content",
                      "[data-testid='calendar-entry-details']",
                    ];
                    for (const sel of selectors) {
                      const el = document.querySelector(sel);
                      if (!el) continue;
                      const txt = normalize(el.innerText || "");
                      if (txt) return txt;
                    }
                    return "";
                  };
                  const readSubstitutionText = (tokens) => {
                    const detailText = readDetailPanelText();
                    if (detailText) {
                      if (/Vertretungstext\\s*:\\s*EVA/i.test(detailText)) return "EVA";
                      if (/Vertretungstext\\s+EVA/i.test(detailText)) return "EVA";
                    }
                    const body = document.body;
                    if (!body) return null;
                    const allText = normalize(body.innerText || "");
                    if (!hasAnyToken(allText, tokens)) return null;
                    if (/Vertretungstext\\s*:\\s*EVA/i.test(allText)) return "EVA";
                    if (/Vertretungstext\\s+EVA/i.test(allText)) return "EVA";
                    return null;
                  };
                  const readExamText = (tokens) => {
                    const detailText = readDetailPanelText();
                    if (detailText) {
                      if (/Prüfung\\s*:\\s*Klausur/i.test(detailText) || /Pruefung\\s*:\\s*Klausur/i.test(detailText)) {
                        return "Klausur";
                      }
                      if (/Prüfung\\s*:\\s*Klassenarbeit/i.test(detailText) || /Pruefung\\s*:\\s*Klassenarbeit/i.test(detailText)) {
                        return "Klausur";
                      }
                      if (/Prüfung\\s*:\\s*Kursarbeit/i.test(detailText) || /Pruefung\\s*:\\s*Kursarbeit/i.test(detailText)) {
                        return "Klausur";
                      }
                      if (/Klausur\\s+Block/i.test(detailText)) return "Klausur";
                      if (/Nachschreib\\w*\\s*Klausur/i.test(detailText)) return "Klausur";
                      const lowerDetail = detailText.toLowerCase();
                      if (lowerDetail.includes("klassenarbeit")) return "Klausur";
                      if (lowerDetail.includes("kursarbeit")) return "Klausur";
                      if (lowerDetail.includes("klausur")) return "Klausur";
                    }
                    const body = document.body;
                    if (!body) return null;
                    const allText = normalize(body.innerText || "");
                    if (!hasAnyToken(allText, tokens)) return null;
                    if (/Prüfung\\s*:\\s*Klausur/i.test(allText) || /Pruefung\\s*:\\s*Klausur/i.test(allText)) {
                      return "Klausur";
                    }
                    if (/Prüfung\\s*:\\s*Klassenarbeit/i.test(allText) || /Pruefung\\s*:\\s*Klassenarbeit/i.test(allText)) {
                      return "Klausur";
                    }
                    if (/Prüfung\\s*:\\s*Kursarbeit/i.test(allText) || /Pruefung\\s*:\\s*Kursarbeit/i.test(allText)) {
                      return "Klausur";
                    }
                    if (/Klausur\\s+Block/i.test(allText)) return "Klausur";
                    if (/Nachschreib\\w*\\s*Klausur/i.test(allText)) return "Klausur";
                    if (allText.toLowerCase().includes("klassenarbeit")) return "Klausur";
                    if (allText.toLowerCase().includes("kursarbeit")) return "Klausur";
                    if (allText.toLowerCase().includes("klausur")) return "Klausur";
                    return null;
                  };
                  const closeDetailPanel = async () => {
                    const selectors = [
                      ".ant-modal .ant-modal-close",
                      ".calendar-entry-detail-view .ant-modal-close",
                      "[data-testid='calendar-entry-details-close']",
                      "button[aria-label='Close']",
                      "button[aria-label='Schließen']",
                    ];
                    for (const sel of selectors) {
                      const btn = document.querySelector(sel);
                      if (!btn) continue;
                      try {
                        btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                        await wait(120);
                        return;
                      } catch (_) {}
                    }
                    try {
                      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
                      await wait(80);
                    } catch (_) {}
                  };
                  const bodyText = (document.body && document.body.innerText) ? document.body.innerText : "";
                  const urlYearFromDateParam = (() => {
                    try {
                      const params = new URLSearchParams(window.location.search || "");
                      const val = params.get("date") || "";
                      const m = val.match(/^(\d{4})-\d{2}-\d{2}$/);
                      return m ? Number(m[1]) : null;
                    } catch (_) {
                      return null;
                    }
                  })();
                  const dateHeader = document.querySelector("[data-testid='date-picker-with-arrows-date-text']");
                  const printHeader = document.querySelector("[data-testid='timetable-print-header-date']");
                  const yearHint =
                    urlYearFromDateParam ||
                    yearFromText(dateHeader && dateHeader.textContent) ||
                    yearFromText(printHeader && printHeader.textContent) ||
                    yearFromText(bodyText);

                  const times = [];
                  for (const el of document.querySelectorAll(".timetable-grid-slot-time")) {
                    const tEl = el.querySelector("[data-testid='timetable-grid-slot-time--time-value']");
                    const timeTxt = normalize(tEl && tEl.textContent);
                    const topRaw = (el.style && el.style.top) || "";
                    const top = Number.parseFloat(topRaw);
                    if (!timeTxt || !Number.isFinite(top)) continue;
                    times.push({ top, time: timeTxt });
                  }

                  const entries = [];
                  const cols = document.querySelectorAll("[data-testid='timetable-grid--column-container']");
                  for (const col of cols) {
                    const dayLabelEl = col.querySelector("[data-testid='timetable-grid-column-header-label-text']");
                    const dayLabel = normalize(dayLabelEl && dayLabelEl.textContent);
                    const cards = col.querySelectorAll("[data-testid^='timetable-grid-card']");
                    for (const card of cards) {
                      const cardStyle = card.getAttribute("style") || "";
                      const topMatch = cardStyle.match(/(?:^|;)\\s*top\\s*:\\s*([-+]?\\d*\\.?\\d+)px/i);
                      const hMatch = cardStyle.match(/(?:^|;)\\s*height\\s*:\\s*([-+]?\\d*\\.?\\d+)px/i);
                      const top = topMatch ? Number.parseFloat(topMatch[1]) : NaN;
                      const height = hMatch ? Number.parseFloat(hMatch[1]) : NaN;
                      if (!Number.isFinite(top) || !Number.isFinite(height) || height <= 0) continue;

                      const lesson = card.querySelector(".lesson-card");
                      if (!lesson) continue;
                      const cls = lesson.getAttribute("class") || null;
                      const title = lesson.getAttribute("title") || null;
                      const aria = lesson.getAttribute("aria-label") || null;
                      const teacher = normalize(
                        lesson.querySelector("[data-testid='lesson-card-resources-with-change-teachers'] [data-testid='regular-resource']")?.textContent || ""
                      );
                      const teacherEl = lesson.querySelector("[data-testid='lesson-card-resources-with-change-teachers'] [data-testid='regular-resource']");
                      const removedTeacher = normalize(
                        lesson.querySelector("[data-testid='lesson-card-resources-with-change-teachers'] [data-testid='removed-resource']")?.textContent || ""
                      );
                      const teacherDecoration = teacherEl ? normalize(getComputedStyle(teacherEl).textDecoration || "") : "";
                      const teacherStrike = teacherDecoration.includes("line-through");
                      const subject = normalize(
                        lesson.querySelector("[data-testid='lesson-card-resources-with-change-subject'] [data-testid='regular-resource']")?.textContent || ""
                      );
                      const roomTokens = Array.from(
                        lesson.querySelectorAll("[data-testid='lesson-card-resources-with-change-rooms'] [data-testid='regular-resource'], [data-testid='lesson-card-resources-with-change-rooms'] [data-testid='added-resource']")
                      )
                        .map((el) => normalize(el.textContent))
                        .filter(Boolean);
                      const room = roomTokens.join(", ");
                      const infoText = normalize(
                        card.querySelector("[data-testid='lesson-card-info-container']")?.innerText || ""
                      );
                      const bgColor = normalize(getComputedStyle(lesson).backgroundColor || getComputedStyle(card).backgroundColor || "");
                      const greenCard = isGreenBackground(bgColor);
                      const yellowCard = isYellowBackground(bgColor);
                      const examIndicator = !!card.querySelector("[data-testid='lesson-card-indicator-exam']");
                      const changeIndicator = !!card.querySelector("[data-testid='lesson-card-indicator-change'], [data-testid='lesson-card-indicator-moved-here']");
                      const contextTokens = [subject || null, teacher || null].filter(Boolean);

                      let substitutionText = null;
                      let examText = null;
                      const infoIcon = card.querySelector("[data-testid='lesson-card-icon-info']");
                      const clickable = infoIcon || lesson;
                      const shouldOpenDetails = !!clickable;
                      if (shouldOpenDetails) {
                        try {
                          clickable.scrollIntoView({ block: "center", inline: "center" });
                          clickable.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                          await wait(520);
                          substitutionText = readSubstitutionText(contextTokens);
                          examText = readExamText(contextTokens);
                          await closeDetailPanel();
                        } catch (_) {}
                      }
                      if (!examText && (yellowCard || examIndicator)) {
                        examText = "Klausur";
                      }

                      const extra = [];
                      if (!substitutionText && removedTeacher && !teacher && (greenCard || changeIndicator)) {
                        substitutionText = "EVA";
                      }
                      if (!substitutionText && greenCard) {
                        substitutionText = "EVA";
                      }
                      if (substitutionText) extra.push(`Vertretungstext: ${substitutionText}`);
                      if (examText) extra.push(`Prüfung: ${examText}`);
                      if (infoText) extra.push(`Info: ${infoText}`);
                      if (teacherStrike) extra.push("teacher_strikethrough");
                      if (removedTeacher) extra.push(`teacher_removed:${removedTeacher}`);
                      if (changeIndicator) extra.push("card_change_indicator");
                      if (greenCard) extra.push("card_bg_green");
                      const rawText = [teacher, subject, room, ...extra].filter(Boolean).join("\\n");
                      const endTop = top + height;
                      const classNameParts = [cls];
                      if (teacherStrike) classNameParts.push("teacher-strike");
                      if (greenCard) classNameParts.push("card-green");
                      if (substitutionText) classNameParts.push(`subst-${substitutionText.toLowerCase()}`);
                      if (examText) classNameParts.push(`exam-${examText.toLowerCase()}`);

                      entries.push({
                        dayLabel,
                        top,
                        endTop,
                        className: normalize(classNameParts.filter(Boolean).join(" ")),
                        title,
                        ariaLabel: aria,
                        rawText,
                        teacher: teacher || null,
                        subject: subject || null,
                        room: room || null,
                      });
                    }
                  }
                  return { yearHint, times, entries };
                }
                """
            )
        except Exception:  # noqa: BLE001
            return []

        raw_entries = payload.get("entries") or []
        raw_times = payload.get("times") or []
        year_hint = payload.get("yearHint")
        if not raw_entries:
            return []

        times: list[tuple[float, str]] = []
        for t in raw_times:
            try:
                top = float(t.get("top"))
                txt = str(t.get("time") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not txt:
                continue
            times.append((top, txt))
        # preserve order from DOM; only remove exact duplicates
        deduped_times: list[tuple[float, str]] = []
        seen_times: set[tuple[int, str]] = set()
        for top, txt in times:
            key = (int(round(top)), txt)
            if key in seen_times:
                continue
            seen_times.add(key)
            deduped_times.append((top, txt))
        times = deduped_times

        result: list[RawLessonBlock] = []
        for item in raw_entries:
            try:
                day_label = str(item.get("dayLabel") or "")
                top = float(item.get("top"))
                end_top = float(item.get("endTop"))
            except Exception:  # noqa: BLE001
                continue

            date_iso = self._parse_day_label_to_iso(day_label, year_hint=year_hint if isinstance(year_hint, int) else None)
            start_time, end_time = self._derive_time_range_from_tops(top=top, end_top=end_top, time_markers=times)

            teacher = str(item.get("teacher") or "").strip() or None
            subject = str(item.get("subject") or "").strip() or None
            room = str(item.get("room") or "").strip() or None
            raw_text = str(item.get("rawText") or "").strip()
            if not raw_text:
                lines = [v for v in (teacher, subject, room) if v]
                raw_text = "\n".join(lines)

            result.append(
                RawLessonBlock(
                    raw_text=raw_text,
                    selector=".lesson-card.clickable",
                    class_name=str(item.get("className") or "").strip() or None,
                    title=str(item.get("title") or "").strip() or None,
                    aria_label=str(item.get("ariaLabel") or "").strip() or None,
                    data_date=date_iso,
                    data_start=start_time,
                    data_end=end_time,
                    data_start_datetime=None,
                    data_end_datetime=None,
                    data_subject=subject,
                    data_teacher=teacher,
                    data_room=room,
                    bbox_top=top,
                    bbox_bottom=end_top,
                    bbox_left=None,
                )
            )
        return self._dedupe_raw_blocks(result)

    @staticmethod
    def _derive_time_range_from_tops(
        *, top: float, end_top: float, time_markers: list[tuple[float, str]]
    ) -> tuple[str | None, str | None]:
        if not time_markers:
            return None, None

        start_idx = -1
        end_idx = -1
        for idx, (mark_top, _) in enumerate(time_markers):
            if mark_top <= top + 0.5:
                start_idx = idx
            if mark_top <= end_top + 0.5:
                end_idx = idx
        if start_idx < 0:
            start_idx = 0
        if end_idx < 0:
            end_idx = 0
        if end_idx <= start_idx and start_idx + 1 < len(time_markers):
            end_idx = start_idx + 1
        start = time_markers[start_idx][1] if 0 <= start_idx < len(time_markers) else None
        end = time_markers[end_idx][1] if 0 <= end_idx < len(time_markers) else None
        return start, end

    @staticmethod
    def _dedupe_raw_blocks(blocks: list[RawLessonBlock]) -> list[RawLessonBlock]:
        out: list[RawLessonBlock] = []
        seen: set[tuple[str, str | None, str | None, str | None, str | None, str | None, str | None]] = set()
        for block in blocks:
            key = (
                " ".join((block.raw_text or "").split()),
                block.data_date,
                block.data_start_datetime or block.data_start,
                block.data_end_datetime or block.data_end,
                block.data_subject,
                block.data_teacher,
                block.class_name,
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(block)
        return out

    def _write_debug_artifacts(self, page: Page, prefix: str = "debug") -> None:
        try:
            self.config.debug_dir.mkdir(parents=True, exist_ok=True)
            html_path = self.config.debug_dir / f"webuntis_{prefix}_page.html"
            shot_path = self.config.debug_dir / f"webuntis_{prefix}_page.png"
            html_path.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(shot_path), full_page=True)
            frame = self._find_embedded_frame(page)
            if frame is not None:
                frame_html = self.config.debug_dir / f"webuntis_{prefix}_iframe.html"
                frame_html.write_text(frame.content(), encoding="utf-8")
            LOGGER.info("Wrote debug files: %s and %s", html_path, shot_path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed writing debug artifacts: %s", exc)

    @staticmethod
    def _find_embedded_frame(page: Page) -> Frame | None:
        frame = page.frame(name="embedded-webuntis")
        if frame is not None:
            return frame
        for item in page.frames:
            if "embedded.do" in (item.url or ""):
                return item
            if re.search(r"/WebUntis/", item.url or "", flags=re.IGNORECASE):
                return item
        return None

    @staticmethod
    def _score_block_set(blocks: list[RawLessonBlock], *, today_iso: str | None = None) -> tuple[int, int, int, int]:
        if not blocks:
            return (0, 0, 0, 0)
        today_hits = sum(1 for b in blocks if today_iso and b.data_date == today_iso)
        dated = sum(1 for b in blocks if b.data_date)
        timed = sum(1 for b in blocks if b.data_start and b.data_end)
        unique_days = len({b.data_date for b in blocks if b.data_date})
        return (today_hits, unique_days, timed, dated)

    @staticmethod
    def _block_set_contains_date(blocks: list[RawLessonBlock], target_date: str | None) -> bool:
        if not target_date:
            return False
        return any(block.data_date == target_date for block in blocks)

    @classmethod
    def _choose_best_block_set(
        cls, candidates: list[tuple[str, list[RawLessonBlock]]], *, today_iso: str | None = None
    ) -> tuple[str, list[RawLessonBlock]]:
        non_empty = [(label, blocks) for label, blocks in candidates if blocks]
        if not non_empty:
            return ("initial", [])

        today_candidates = [(label, blocks) for label, blocks in non_empty if cls._block_set_contains_date(blocks, today_iso)]
        if today_candidates:
            week_like = [
                (label, blocks)
                for label, blocks in today_candidates
                if label in {"after_today", "after_week", "after_week_retry", "initial"}
            ]
            if week_like:
                today_candidates = week_like
            return max(
                today_candidates,
                key=lambda item: (
                    cls._score_block_set(item[1], today_iso=today_iso)[0],
                    -len({b.data_date for b in item[1] if b.data_date}),
                    cls._score_block_set(item[1], today_iso=today_iso)[2],
                    cls._score_block_set(item[1], today_iso=today_iso)[3],
                ),
            )
        return max(non_empty, key=lambda item: cls._score_block_set(item[1], today_iso=today_iso))

    @staticmethod
    def _safe_bounding_box(locator: Locator) -> dict | None:
        try:
            return locator.bounding_box()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _collect_grid_markers(root: Page | Frame) -> tuple[list[tuple[float, str]], list[tuple[float, str]], int | None]:
        try:
            raw = root.evaluate(
                """
                () => {
                  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
                  const timeRe = /\\b\\d{1,2}:\\d{2}\\b/g;
                  const dayRe = /^(Mo|Di|Mi|Do|Fr|Sa|So)\\s*\\d{1,2}\\.\\d{1,2}\\.?$/i;
                  const yearRe = /\\b(20\\d{2})\\b/;
                  const urlYearFromDateParam = (() => {
                    try {
                      const params = new URLSearchParams(window.location.search || "");
                      const val = params.get("date") || "";
                      const m = val.match(/^(\d{4})-\d{2}-\d{2}$/);
                      return m ? Number(m[1]) : null;
                    } catch (_) {
                      return null;
                    }
                  })();
                  const outTimes = [];
                  const outDays = [];
                  // Preferred: explicit timetable slots with style.top
                  for (const el of Array.from(document.querySelectorAll(".timetable-grid-slot-time"))) {
                    const t = norm(el.querySelector("[data-testid='timetable-grid-slot-time--time-value']")?.textContent || "");
                    const topRaw = (el.style && el.style.top) || "";
                    const y = Number.parseFloat(topRaw);
                    if (!t || !Number.isFinite(y)) continue;
                    outTimes.push({ y, text: t });
                  }
                  // Preferred day headers
                  for (const el of Array.from(document.querySelectorAll("[data-testid='timetable-grid-column-header-label-text']"))) {
                    const txt = norm(el.textContent || "");
                    if (!txt || !dayRe.test(txt)) continue;
                    const r = el.getBoundingClientRect();
                    if (!r || r.width <= 0 || r.height <= 0) continue;
                    outDays.push({ x: r.left + r.width / 2, text: txt });
                  }
                  // Fallbacks
                  if (outTimes.length === 0 || outDays.length === 0) {
                    const all = Array.from(document.querySelectorAll("*"));
                    for (const el of all) {
                      const txt = norm(el.textContent || "");
                      if (!txt || txt.length > 80) continue;
                      const r = el.getBoundingClientRect();
                      if (!r || r.width <= 0 || r.height <= 0) continue;

                      if (outTimes.length === 0) {
                        const times = Array.from(txt.matchAll(timeRe)).map((m) => m[0]);
                        if (times.length > 0 && r.left < 260) {
                          for (let i = 0; i < times.length; i += 1) {
                            const y = r.top + ((i + 0.5) / times.length) * r.height;
                            outTimes.push({ y, text: times[i] });
                          }
                        }
                      }
                      if (outDays.length === 0 && dayRe.test(txt) && r.top < 280) {
                        outDays.push({ x: r.left + r.width / 2, text: txt });
                      }
                    }
                  }
                  const bodyText = (document.body && document.body.innerText) ? document.body.innerText : "";
                  const ym = bodyText.match(yearRe);
                  const year = urlYearFromDateParam || (ym ? Number(ym[1]) : null);
                  return { times: outTimes, days: outDays, year };
                }
                """
            )
        except Exception:  # noqa: BLE001
            return [], [], None

        times: list[tuple[float, str]] = []
        by_y: dict[int, tuple[float, str]] = {}
        for item in raw.get("times", []):
            try:
                y = float(item.get("y"))
                txt = str(item.get("text") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not txt:
                continue
            yy = int(round(y))
            prev = by_y.get(yy)
            if prev is None:
                by_y[yy] = (y, txt)
            else:
                # For duplicate y (e.g. 09:30/09:55 on same line), keep the later time token.
                by_y[yy] = (y, max(prev[1], txt))
        times.extend(by_y.values())
        times.sort(key=lambda x: x[0])

        days: list[tuple[float, str]] = []
        seen_d: set[tuple[int, str]] = set()
        for item in raw.get("days", []):
            try:
                x = float(item.get("x"))
                txt = str(item.get("text") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            key = (int(round(x)), txt)
            if key in seen_d:
                continue
            seen_d.add(key)
            days.append((x, txt))
        days.sort(key=lambda x: x[0])

        year_hint = raw.get("year")
        if not isinstance(year_hint, int):
            year_hint = None
        return times, days, year_hint

    @staticmethod
    def _derive_time_range_from_bbox(
        bbox: dict | None, time_markers: list[tuple[float, str]]
    ) -> tuple[str | None, str | None]:
        if not bbox or not time_markers:
            return None, None
        top = float(bbox.get("y", 0.0))
        bottom = top + float(bbox.get("height", 0.0))

        def at_or_before(y: float) -> str | None:
            hit = None
            for marker_y, marker_t in time_markers:
                if marker_y <= y + 1.0:
                    hit = marker_t
                else:
                    break
            return hit

        def first_after(y: float) -> str | None:
            for marker_y, marker_t in time_markers:
                if marker_y > y + 1.0:
                    return marker_t
            return None

        start = at_or_before(top)
        end = at_or_before(bottom - 1.0)
        if end is None:
            end = first_after(bottom - 1.0)
        if start and end and end <= start:
            nxt = first_after(bottom - 1.0)
            if nxt:
                end = nxt
        if start == end:
            nxt = first_after(bottom - 1.0)
            if nxt:
                end = nxt
        return start, end

    @staticmethod
    def _derive_date_from_bbox(
        bbox: dict | None, day_markers: list[tuple[float, str]], year_hint: int | None
    ) -> str | None:
        if not bbox or not day_markers:
            return None
        x_center = float(bbox.get("x", 0.0)) + (float(bbox.get("width", 0.0)) / 2.0)
        nearest = min(day_markers, key=lambda m: abs(m[0] - x_center))
        txt = nearest[1]
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.?$", txt)
        if not match:
            return None
        day = int(match.group(1))
        month = int(match.group(2))
        year = year_hint or date.today().year
        return f"{year:04d}-{month:02d}-{day:02d}"

    @staticmethod
    def _collect_text_markers(root: Page | Frame) -> tuple[list[str], list[str], int | None]:
        try:
            raw = root.evaluate(
                """
                () => {
                  const text = (document.body && document.body.innerText) ? document.body.innerText : "";
                  return { text };
                }
                """
            )
            text = str(raw.get("text") or "")
        except Exception:  # noqa: BLE001
            return [], [], None

        times = re.findall(r"\b\d{1,2}:\d{2}\b", text)
        # keep order, dedupe
        seen_t = set()
        ordered_times: list[str] = []
        for t in times:
            if t in seen_t:
                continue
            seen_t.add(t)
            ordered_times.append(t)

        day_matches = re.findall(r"\b(?:Mo|Di|Mi|Do|Fr|Sa|So)\s*\d{1,2}\.\d{1,2}\.?\b", text)
        seen_d = set()
        ordered_days: list[str] = []
        for d in day_matches:
            dd = " ".join(d.split())
            if dd in seen_d:
                continue
            seen_d.add(dd)
            ordered_days.append(dd)

        ym = re.search(r"\b(20\d{2})\b", text)
        year_hint = int(ym.group(1)) if ym else None
        return ordered_times, ordered_days, year_hint

    @staticmethod
    def _enrich_blocks_from_text_layout(
        blocks: list[RawLessonBlock],
        *,
        time_labels: list[str],
        day_labels: list[str],
        year_hint: int | None,
    ) -> list[RawLessonBlock]:
        blocks_with_bbox = [b for b in blocks if b.bbox_top is not None and b.bbox_bottom is not None and b.bbox_left is not None]
        if not blocks_with_bbox:
            return blocks

        x_centers = [float(b.bbox_left or 0.0) for b in blocks_with_bbox]
        x_clusters = WebUntisClient._cluster_values(sorted(x_centers), gap_threshold=80.0)
        y_values = []
        for b in blocks_with_bbox:
            y_values.append(float(b.bbox_top or 0.0))
            y_values.append(float(b.bbox_bottom or 0.0))
        y_clusters = WebUntisClient._cluster_values(sorted(y_values), gap_threshold=10.0)

        parsed_days = [WebUntisClient._parse_day_label_to_iso(day, year_hint=year_hint) for day in day_labels]
        parsed_days = [d for d in parsed_days if d]

        enriched: list[RawLessonBlock] = []
        for b in blocks:
            date_guess = b.data_date
            start_guess = b.data_start
            end_guess = b.data_end

            if b.bbox_left is not None and x_clusters and parsed_days:
                xi = WebUntisClient._nearest_index(float(b.bbox_left), x_clusters)
                if 0 <= xi < len(parsed_days):
                    date_guess = date_guess or parsed_days[xi]

            if b.bbox_top is not None and b.bbox_bottom is not None and y_clusters and time_labels:
                si = WebUntisClient._nearest_index(float(b.bbox_top), y_clusters)
                ei = WebUntisClient._nearest_index(float(b.bbox_bottom), y_clusters)
                if si >= 0:
                    si = min(si, len(time_labels) - 1)
                    start_guess = start_guess or time_labels[si]
                if ei >= 0:
                    ei = min(ei, len(time_labels) - 1)
                    if ei <= si and si + 1 < len(time_labels):
                        ei = si + 1
                    end_guess = end_guess or time_labels[ei]

            enriched.append(
                RawLessonBlock(
                    raw_text=b.raw_text,
                    selector=b.selector,
                    class_name=b.class_name,
                    title=b.title,
                    aria_label=b.aria_label,
                    data_date=date_guess,
                    data_start=start_guess,
                    data_end=end_guess,
                    data_start_datetime=b.data_start_datetime,
                    data_end_datetime=b.data_end_datetime,
                    data_subject=b.data_subject,
                    data_teacher=b.data_teacher,
                    data_room=b.data_room,
                    bbox_top=b.bbox_top,
                    bbox_bottom=b.bbox_bottom,
                    bbox_left=b.bbox_left,
                )
            )
        return enriched

    @staticmethod
    def _cluster_values(values: list[float], gap_threshold: float) -> list[float]:
        if not values:
            return []
        groups: list[list[float]] = [[values[0]]]
        for val in values[1:]:
            if abs(val - groups[-1][-1]) <= gap_threshold:
                groups[-1].append(val)
            else:
                groups.append([val])
        return [sum(group) / len(group) for group in groups]

    @staticmethod
    def _nearest_index(value: float, anchors: list[float]) -> int:
        if not anchors:
            return -1
        return min(range(len(anchors)), key=lambda i: abs(anchors[i] - value))

    @staticmethod
    def _parse_day_label_to_iso(label: str, year_hint: int | None) -> str | None:
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.?", label)
        if not match:
            return None
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(year_hint or date.today().year)
        return f"{year:04d}-{month:02d}-{day:02d}"

    @staticmethod
    def _safe_attr(locator: Locator, attr: str) -> str | None:
        try:
            value = locator.get_attribute(attr)
            return value.strip() if value else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _safe_inner_text(locator: Locator) -> str:
        try:
            value = locator.inner_text(timeout=1000)
            return (value or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _has_any_visible(page: Page, selectors: tuple[str, ...]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    @staticmethod
    def _search_roots(page: Page) -> list[Page | Frame]:
        roots: list[Page | Frame] = [page]
        try:
            roots.extend(list(page.frames))
        except Exception:  # noqa: BLE001
            pass
        return roots

    @staticmethod
    def wait_for_any_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int) -> str:
        end = time.monotonic() + (timeout_ms / 1000.0)
        last_error: Exception | None = None
        while time.monotonic() < end:
            for root in WebUntisClient._search_roots(page):
                for selector in selectors:
                    try:
                        locator = root.locator(selector).first
                        if locator.count() > 0 and locator.is_visible():
                            return selector
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        continue
            page.wait_for_timeout(250)
        raise SchoolSyncError(f"Timed out waiting for schedule visibility ({selectors}). Last error: {last_error}")
