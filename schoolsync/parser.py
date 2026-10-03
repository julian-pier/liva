from __future__ import annotations

import re
from datetime import date, datetime

from .models import RawLessonBlock, ScheduleEntry

TIME_RANGE_RE = re.compile(
    r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:-|bis|to)\s*([01]?\d|2[0-3])[:.]([0-5]\d)",
    re.IGNORECASE,
)
TIME_SINGLE_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")
DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_DMY_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
ROOM_RE = re.compile(r"\b(?:raum|room|rm\.?)\s*[:#]?\s*([a-z0-9\-/]+)\b", re.IGNORECASE)
TEACHER_RE = re.compile(r"\b(?:lehrer|teacher)\s*[:#]?\s*([^\n,;|]+)", re.IGNORECASE)
ROOM_TOKEN_RE = re.compile(r"^[A-Z]\d\.\d{2}$")
SHORT_UPPER_RE = re.compile(r"^[A-Z]{2,5}$")
SUBJECT_CODE_RE = re.compile(r"^[A-Z]{1,3}_[A-Z0-9]+$")
SUBJECT_PREFIXES = ("SP", "SW", "KU", "KR", "GE", "BI", "IF", "M", "D", "E")
SUBJECT_CODE_INLINE_RE = re.compile(r"((?:" + "|".join(SUBJECT_PREFIXES) + r")_[A-Z]{2}\d{1,2})")
TEACHER_TOKEN_RE = re.compile(r"([A-Z]{2,5})$")
ROOM_TOKEN_INLINE_RE = re.compile(r"([A-Z][A-Z0-9]?\d?\.\d{2}|[A-Z]\d\.\d{2}|[A-Z]\d{1,2}\.\d{2}|SH\d[A-Z]|Mensa\d?)$")


def normalize_blocks(
    blocks: list[RawLessonBlock], default_date: str | None = None, apply_lesson_filter: bool = True
) -> list[ScheduleEntry]:
    fallback_date = default_date or date.today().isoformat()
    normalized: list[ScheduleEntry] = []
    seen: set[tuple[str, str | None, str | None, str | None, str | None, str | None, str, str]] = set()

    for block in blocks:
        entry_date = _extract_date(block, fallback_date=fallback_date)
        start_time, end_time = _extract_time_range(block)
        subject, teacher, room = _extract_fields(block)
        status = _status_hint(block)
        raw_text = (block.raw_text or "").strip()
        if not raw_text and not any([subject, teacher, room]):
            continue
        if apply_lesson_filter:
            if not _looks_like_real_lesson(
                block=block,
                raw_text=raw_text,
                subject=subject,
                teacher=teacher,
                room=room,
                start_time=start_time,
                end_time=end_time,
            ):
                continue

        dedupe_key = (
            entry_date,
            start_time,
            end_time,
            _norm_key(subject),
            _norm_key(teacher),
            _norm_key(room),
            " ".join(raw_text.split()),
            status,
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        normalized.append(
            ScheduleEntry(
                date=entry_date,
                subject=subject,
                teacher=teacher,
                room=room,
                start_time=start_time,
                end_time=end_time,
                raw_text=raw_text,
                status_hint=status,
                source_selector=block.selector,
            )
        )

    return sorted(normalized, key=lambda x: (x.date, _sort_time(x.start_time), _sort_time(x.end_time), x.raw_text))


def _extract_date(block: RawLessonBlock, fallback_date: str) -> str:
    for raw in (
        block.data_date,
        block.data_start_datetime,
        block.data_end_datetime,
        block.data_start,
        block.data_end,
    ):
        parsed = _parse_date_token(raw)
        if parsed:
            return parsed

    blob = " | ".join(filter(None, [block.title, block.aria_label, block.raw_text]))
    parsed_blob = _parse_date_token(blob)
    if parsed_blob:
        return parsed_blob
    return fallback_date


def _extract_time_range(block: RawLessonBlock) -> tuple[str | None, str | None]:
    text_candidates = [
        block.data_start_datetime,
        block.data_end_datetime,
        block.data_start,
        block.data_end,
        block.title,
        block.aria_label,
        block.raw_text,
    ]
    joined = " | ".join([c for c in text_candidates if c])
    range_match = TIME_RANGE_RE.search(joined)
    if range_match:
        return _fmt_time(range_match.group(1), range_match.group(2)), _fmt_time(range_match.group(3), range_match.group(4))

    times: list[str] = []
    for match in TIME_SINGLE_RE.finditer(joined):
        times.append(_fmt_time(match.group(1), match.group(2)))
    if len(times) >= 2:
        return times[0], times[1]
    if block.data_start and block.data_end:
        s = _normalize_time_token(block.data_start)
        e = _normalize_time_token(block.data_end)
        if s and e:
            return s, e
    return None, None


def _extract_fields(block: RawLessonBlock) -> tuple[str | None, str | None, str | None]:
    room = None
    teacher = block.data_teacher.strip() if block.data_teacher else None
    subject = block.data_subject.strip() if block.data_subject else None
    if block.data_room:
        room = block.data_room.strip()

    metadata_prefixes = ("teacher_removed:", "card_change_indicator", "card_bg_green", "teacher_strikethrough")
    raw_lines = [ln.strip() for ln in (block.raw_text or "").splitlines() if ln.strip()]
    content_lines = [ln for ln in raw_lines if not any(ln.startswith(prefix) for prefix in metadata_prefixes)]
    source = " ".join(filter(None, ["\n".join(content_lines), block.title, block.aria_label]))
    if not room:
        room_match = ROOM_RE.search(source)
        if room_match:
            room = room_match.group(1).strip()
    if not teacher:
        t_match = TEACHER_RE.search(source)
        if t_match:
            teacher = t_match.group(1).strip()

    lines = list(content_lines)
    lines = [ln for ln in lines if not TIME_RANGE_RE.search(ln)]
    subject_looks_polluted = bool(subject and "info:" in subject.lower())
    if not subject or subject_looks_polluted:
        # Prefer explicit subject tokens (e.g. M_LK1) over teacher-like short codes.
        for ln in lines:
            if SUBJECT_CODE_RE.match(ln) or "_" in ln:
                subject = ln
                break
    if not subject and lines:
        subject = lines[0]

    # Handle swapped fields from WebUntis cards: first line often teacher, second line subject code.
    if subject and SHORT_UPPER_RE.match(subject) and lines:
        idx = next((i for i, ln in enumerate(lines) if ln == subject), -1)
        if idx >= 0 and (idx + 1) < len(lines):
            nxt = lines[idx + 1]
            if SUBJECT_CODE_RE.match(nxt) or "_" in nxt:
                teacher = teacher or subject
                subject = nxt

    # Recover subject from noisy "Info:" lines such as
    # "Info: HOR KU_GK1 X0.06" or "Info: HORKU_GK1X0.06".
    source_lower = source.lower()
    if "info:" in source_lower:
        subj_match = SUBJECT_CODE_INLINE_RE.search(source)
        if subj_match:
            subject = subj_match.group(1).strip()
            prefix = source[: subj_match.start(1)]
            suffix = source[subj_match.end(1) :]
            if not teacher:
                teacher_match = TEACHER_TOKEN_RE.search(prefix.replace("Info:", "").replace("info:", "").strip())
                if teacher_match:
                    teacher = teacher_match.group(1).strip()
            if not room:
                room_match = ROOM_TOKEN_INLINE_RE.search(suffix.strip())
                if room_match:
                    room = room_match.group(1).strip()
        if not teacher:
            teacher_match = re.search(r"\bInfo:\s*([A-Z]{2,5})\b", source)
            if teacher_match:
                teacher = teacher_match.group(1).strip()

    return _clean(subject), _clean(teacher), _clean(room)


def _status_hint(block: RawLessonBlock) -> str:
    text = " ".join(filter(None, [block.raw_text, block.class_name, block.title, block.aria_label])).lower()
    if any(k in text for k in (" eva ", "eva", "vertretungstext: eva", "substitutiontext: eva")):
        return "eva"
    if any(k in text for k in ("entf", "cancel", "ausfall", "faellt aus")):
        return "cancelled"
    if any(k in text for k in ("pruefung", "prüfung", "exam", "klausur", "test")):
        return "exam"
    if any(k in text for k in ("geaendert", "changed", "vertretung", "substit")):
        return "changed"
    if "normal" in text:
        return "normal"
    return "unknown"


def _normalize_time_token(raw: str) -> str | None:
    if not raw:
        return None
    txt = raw.strip().replace(".", ":")
    if "T" in txt:
        try:
            parsed_dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            return parsed_dt.strftime("%H:%M")
        except ValueError:
            pass
    try:
        parsed = datetime.strptime(txt, "%H:%M")
        return parsed.strftime("%H:%M")
    except ValueError:
        return None


def _fmt_time(hour: str, minute: str) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    compact = " ".join(value.split())
    return compact if compact else None


def _norm_key(value: str | None) -> str | None:
    return value.lower() if isinstance(value, str) else None


def _sort_time(value: str | None) -> str:
    return value or "99:99"


def _parse_date_token(raw: str | None) -> str | None:
    if not raw:
        return None
    txt = raw.strip()
    if not txt:
        return None
    if "T" in txt:
        try:
            return datetime.fromisoformat(txt.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass

    iso = DATE_ISO_RE.search(txt)
    if iso:
        y, m, d = iso.group(1), iso.group(2), iso.group(3)
        return f"{y}-{m}-{d}"

    dmy = DATE_DMY_RE.search(txt)
    if dmy:
        d, m, y = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
        return f"{y:04d}-{m:02d}-{d:02d}"
    return None


def _looks_like_real_lesson(
    *,
    block: RawLessonBlock,
    raw_text: str,
    subject: str | None,
    teacher: str | None,
    room: str | None,
    start_time: str | None,
    end_time: str | None,
) -> bool:
    if start_time and end_time:
        return True
    if block.data_start_datetime or block.data_end_datetime:
        return True
    if TIME_RANGE_RE.search(" ".join(filter(None, [block.title, block.aria_label, raw_text]))):
        return True
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if len(lines) >= 2:
        return True
    if not raw_text:
        return False
    token = raw_text.strip()
    if ROOM_TOKEN_RE.match(token):
        return False
    if SHORT_UPPER_RE.match(token):
        return False
    if subject and "_" in subject:
        return True
    # Keep only richer, non-trivial labels if no stronger lesson indicators are present.
    return len(token) >= 6 and (" " in token)
