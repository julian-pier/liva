import re
from typing import Any, Dict, List, Optional

DAY_ALIASES = {
    "mo": 0,
    "mon": 0,
    "montag": 0,
    "monday": 0,
    "di": 1,
    "die": 1,
    "dienstag": 1,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "mi": 2,
    "mittwoch": 2,
    "wed": 2,
    "wednesday": 2,
    "do": 3,
    "donnerstag": 3,
    "thu": 3,
    "thursday": 3,
    "fr": 4,
    "freitag": 4,
    "fri": 4,
    "friday": 4,
    "sa": 5,
    "samstag": 5,
    "sat": 5,
    "saturday": 5,
    "so": 6,
    "sonntag": 6,
    "sun": 6,
    "sunday": 6,
}

DAY_HEADER = re.compile(r"^\s*##\s*(.+?)\s*$")
MEAL_LINE = re.compile(
    r"^\s*-\s*(?:Meal|M)\s*(\d+)\s*:\s*(.+?)(?:\s*\((\d+(?:[\.,]\d+)?)\s*x\))?\s*$",
    re.IGNORECASE,
)
MEAL_LINE_NO_PREFIX = re.compile(
    r"^\s*(?:Meal|M)\s*(\d+)\s*:\s*(.+?)(?:\s*\((\d+(?:[\.,]\d+)?)\s*x\))?\s*$",
    re.IGNORECASE,
)
MEAL_LINE_GENERIC = re.compile(
    r"^\s*(.+?)\s*:\s*(.+?)(?:\s*\((\d+(?:[\.,]\d+)?)\s*x\))?\s*$",
    re.IGNORECASE,
)
MEAL_LINE_TITLE = re.compile(
    r"^\s*(.+?)(?:\s*\((\d+(?:[\.,]\d+)?)\s*x\))?\s*$",
    re.IGNORECASE,
)
TIME_PREFIX = re.compile(r"^\s*[-–—•*]\s*\[\s*(\d{2}\s*:\s*\d{2})\s*\]\s*(.+)$")
INGREDIENT_LINE_STRICT = re.compile(r"^\s{2,}-\s*(.+)$")
INGREDIENT_LINE_LOOSE = re.compile(r"^\s+-\s*(.+)$")
AMOUNT_LINE = re.compile(r"^(\d+(?:[\.,]\d+)?)\s*([a-zA-Z]+)\s+(.+)$")
CANONICAL_LABELS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
NOISE_LINE_PATTERNS = [
    re.compile(r"^\s*ERROR\b", re.IGNORECASE),
    re.compile(r"ERROR\s*·\s*Zeile", re.IGNORECASE),
    re.compile(r"^\s*Slot\s+\d+\s+für\s+\w+\s+nicht\s+gefunden", re.IGNORECASE),
    re.compile(r"^\s*Alle\s+Items\s+zugeordnet\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*Parse-Fehler\b", re.IGNORECASE),
]


def _normalize_day_label(value: str) -> Optional[int]:
    if not value:
        return None
    cleaned = re.sub(r"[^a-zA-Z]", "", value.strip().lower())
    return DAY_ALIASES.get(cleaned)


def _parse_day_header_labels(value: str) -> List[int]:
    raw_parts = re.split(r"\s*,\s*|\s*/\s*|\s*;\s*|\s+und\s+", (value or "").strip(), flags=re.IGNORECASE)
    weekdays: List[int] = []
    seen = set()
    for part in raw_parts:
        weekday = _normalize_day_label(part)
        if weekday is None or weekday in seen:
            continue
        seen.add(weekday)
        weekdays.append(weekday)
    return weekdays


def _parse_amount_line(value: str) -> Optional[Dict[str, Any]]:
    cleaned = value.strip()
    cleaned = re.sub(r"^(ca\.|cca\.|circa|approx\.|~)\s*", "", cleaned, flags=re.IGNORECASE)
    match = AMOUNT_LINE.match(cleaned)
    if not match:
        return None
    amount_text, unit, name = match.groups()
    amount_text = amount_text.replace(",", ".")
    try:
        amount = float(amount_text)
    except ValueError:
        return None
    return {"amount": amount, "unit": unit, "name": name.strip()}


def _is_noise_line(value: str) -> bool:
    return any(pattern.search(value) for pattern in NOISE_LINE_PATTERNS)


def parse_mealplan_text(text: str, *, strict: bool = False) -> Dict[str, Any]:
    normalized_text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized_text.split("\n")
    normalized_text = re.sub(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]", " ", normalized_text)
    normalized_text = normalized_text.replace("\t", "  ")
    issues: List[Dict[str, Any]] = []
    day_map: Dict[int, Dict[str, Any]] = {}
    current_days: List[Dict[str, Any]] = []
    current_meals: List[Dict[str, Any]] = []
    max_slot_overall = 0
    max_slot_by_day: Dict[int, int] = {}

    lines = normalized_text.splitlines()
    ingredient_pattern = INGREDIENT_LINE_STRICT if strict else INGREDIENT_LINE_LOOSE
    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        raw_original = raw_lines[idx - 1] if idx - 1 < len(raw_lines) else raw_line
        stripped = line.strip()
        if not stripped:
            continue
        day_match = DAY_HEADER.match(line)
        if day_match:
            label = day_match.group(1).strip()
            weekdays = _parse_day_header_labels(label)
            if not weekdays:
                issues.append({
                    "line": idx,
                    "message": f"Unbekannter Wochentag: '{label}'",
                    "raw": line,
                    "raw_repr": repr(raw_original),
                    "hint": "Zeile sollte wie '## Mo' oder '## Mo, Do' aussehen.",
                    "source": "parse",
                    "severity": "error",
                })
                current_days = []
                current_meals = []
                continue
            current_days = []
            for weekday in weekdays:
                day_label = CANONICAL_LABELS[weekday] if 0 <= weekday < len(CANONICAL_LABELS) else label
                day_entry = day_map.setdefault(
                    weekday,
                    {"weekday": weekday, "label": day_label, "meals": [], "line": idx},
                )
                current_days.append(day_entry)
            current_meals = []
            continue
        time_match = TIME_PREFIX.match(line)
        if time_match:
            time_raw = time_match.group(1)
            rest = time_match.group(2).strip()
            try:
                hh_text, mm_text = [p.strip() for p in time_raw.split(":", 1)]
                hh = int(hh_text)
                mm = int(mm_text)
            except Exception:
                hh = -1
                mm = -1
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                issues.append({
                    "line": idx,
                    "message": "Ungültige Uhrzeit in Meal-Header",
                    "raw": line,
                    "raw_repr": repr(raw_original),
                    "hint": "Erwartet: '- [21:40] Meal 3: Titel (1x)'",
                    "source": "parse",
                    "severity": "error",
                })
                current_meals = []
                continue
            meal_match = MEAL_LINE_NO_PREFIX.match(rest)
            generic_match = None if meal_match else MEAL_LINE_GENERIC.match(rest)
            title_match = None if (meal_match or generic_match) else MEAL_LINE_TITLE.match(rest)
            if not meal_match and not generic_match and not title_match:
                issues.append({
                    "line": idx,
                    "message": "Meal header invalid format",
                    "raw": line,
                    "raw_repr": repr(raw_original),
                    "hint": "Erwartet: '- [21:40] Meal 3: Titel (1x)'.",
                    "source": "parse",
                    "severity": "error" if strict else "warning",
                })
                current_meals = []
                continue
            time_text = f"{hh:02d}:{mm:02d}"
        else:
            if re.match(r"^\s*-\s*\[", line):
                issues.append({
                    "line": idx,
                    "message": "Ungültige Uhrzeit in Meal-Header",
                    "raw": line,
                    "raw_repr": repr(raw_original),
                    "hint": "Erwartet: '- [21:40] Meal 3: Titel (1x)'.",
                    "source": "parse",
                    "severity": "error",
                })
                current_meals = []
                continue
            meal_match = MEAL_LINE.match(line)
            generic_match = None if meal_match else MEAL_LINE_GENERIC.match(line)
            time_text = None
        if meal_match or generic_match or (time_match and title_match):
            if not current_days:
                issues.append({
                    "line": idx,
                    "message": "Mahlzeit ohne Wochentag",
                    "raw": line,
                    "raw_repr": repr(raw_original),
                    "hint": "Vor Mahlzeiten-Zeilen muss ein Tages-Header (## Mo) stehen.",
                    "source": "parse",
                    "severity": "error",
                })
                current_meals = []
                continue
            if meal_match:
                slot_number = int(meal_match.group(1))
                title = meal_match.group(2).strip()
                servings_text = meal_match.group(3)
            elif generic_match:
                slot_number = len(current_days[0]["meals"]) + 1
                title = (generic_match.group(2) if generic_match else "").strip()
                servings_text = (generic_match.group(3) if generic_match else None)
            else:
                slot_number = len(current_days[0]["meals"]) + 1
                title = (title_match.group(1) if title_match else "").strip()
                servings_text = (title_match.group(2) if title_match else None)
            multiplier = float(servings_text.replace(",", ".")) if servings_text else 1.0
            current_meals = []
            for day_entry in current_days:
                meal_entry = {
                    "slot": slot_number,
                    "title": title,
                    "servings": multiplier,
                    "items": [],
                    "line": idx,
                    "time_text": time_text,
                }
                day_entry["meals"].append(meal_entry)
                current_meals.append(meal_entry)
                weekday = day_entry.get("weekday")
                if weekday is not None:
                    max_slot_by_day[weekday] = max(max_slot_by_day.get(weekday, 0), slot_number)
            max_slot_overall = max(max_slot_overall, slot_number)
            continue
        ing_match = ingredient_pattern.match(line)
        if ing_match:
            if not current_days or not current_meals:
                issues.append({
                    "line": idx,
                    "message": "Ingredient ohne Mahlzeit",
                    "raw": line,
                    "raw_repr": repr(raw_original),
                    "hint": "Ingredient-Zeilen müssen unter einer Meal-Zeile stehen.",
                    "source": "parse",
                    "severity": "error",
                })
                continue
            payload = ing_match.group(1).strip()
            cleaned = payload.split("->", 1)[0].rstrip()
            payload_clean = cleaned if cleaned else payload
            parsed = _parse_amount_line(payload_clean)
            for meal_entry in current_meals:
                meal_entry["items"].append({
                    "raw": payload_clean,
                    "raw_full": payload if payload_clean != payload else None,
                    "parsed": parsed,
                    "line": idx,
                })
            continue
        if not strict and current_meals:
            fallback_match = re.match(r"^\s*-\s+(.+)$", line)
            if fallback_match and not MEAL_LINE.match(stripped):
                payload = fallback_match.group(1).strip()
                cleaned = payload.split("->", 1)[0].rstrip()
                payload_clean = cleaned if cleaned else payload
                parsed = _parse_amount_line(payload_clean)
                for meal_entry in current_meals:
                    meal_entry["items"].append({
                        "raw": payload_clean,
                        "raw_full": payload if payload_clean != payload else None,
                        "parsed": parsed,
                        "line": idx,
                    })
                continue
        if _is_noise_line(stripped):
            issues.append({
                "line": idx,
                "message": "Ignorierte UI-Zeile",
                "raw": line,
                "raw_repr": repr(raw_original),
                "source": "parse",
                "severity": "error" if strict else "warning",
            })
            continue
        if stripped.startswith("-") and "meal" in stripped.lower() and ":" in stripped:
            issues.append({
                "line": idx,
                "message": "Meal header invalid format",
                "raw": line,
                "raw_repr": repr(raw_original),
                "hint": "Erwartet: '- Meal 3: Titel (1x)'.",
                "source": "parse",
                "severity": "error" if strict else "warning",
            })
            continue
        issues.append({
            "line": idx,
            "message": "Unbekannte Zeile",
            "raw": line,
            "raw_repr": repr(raw_original),
            "hint": "Prüfe Einrückung: Zutaten müssen eingerückt sein.",
            "source": "parse",
            "severity": "error" if strict else "warning",
        })
    sorted_days = [day_map[key] for key in sorted(day_map.keys())]
    return {
        "days": sorted_days,
        "issues": issues,
        "max_slot_overall": max_slot_overall,
        "max_slot_by_day": max_slot_by_day,
    }
