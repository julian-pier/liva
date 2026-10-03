#!/usr/bin/env python3

import argparse
import difflib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.connections import get_nutrition_db
from nutrition.nutrition_planning_db import (  # noqa: E402
    _insert_logged_meal_items,
    _normalize_import_label,
    _record_recent_foods,
    _record_recent_meal,
    _sync_logging_totals_to_weight_logs,
    _utcnow_iso,
    ensure_nutrition_planning_schema,
)


MONTHS_DE = {
    "jan": 1, "januar": 1,
    "feb": 2, "februar": 2,
    "mär": 3, "maer": 3, "mrz": 3, "maerz": 3, "märz": 3,
    "apr": 4, "april": 4,
    "mai": 5,
    "jun": 6, "juni": 6,
    "jul": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "dez": 12, "dezember": 12,
}

DATE_RE = re.compile(r"^\s*(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\.?\s*(\d{4})\s*$")
SPLIT_RE = re.compile(r"\t+|\s{2,}")
HEADER_MARKERS = ("NAHRUNGSMITTEL", "Kalorien", "Kohlenhydrate", "Fette", "Eiweiß")
MEAL_LABELS = {
    "breakfast": ("Frühstück", "08:00"),
    "frühstück": ("Frühstück", "08:00"),
    "schule": ("Schule", "11:00"),
    "mittag": ("Mittag", "13:00"),
    "mittagessen": ("Mittag", "13:00"),
    "nachmittags": ("Nachmittags", "16:00"),
    "abendessen": ("Abendessen", "19:00"),
    "dinner": ("Abendessen", "19:00"),
    "snacks": ("Snacks", "21:00"),
    "snack": ("Snacks", "21:00"),
}


@dataclass
class ParsedFoodRow:
    raw_name: str
    calories: Optional[float]
    amount: Optional[float]
    unit: Optional[str]
    carbs: Optional[float] = None
    fat: Optional[float] = None
    protein: Optional[float] = None
    sugar: Optional[float] = None


@dataclass
class ParsedMeal:
    meal_slot: str
    time_text: str
    items: List[ParsedFoodRow] = field(default_factory=list)


@dataclass
class ParsedDay:
    date_iso: str
    meals: List[ParsedMeal] = field(default_factory=list)


def _parse_num(token: Optional[str]) -> Optional[float]:
    if token is None:
        return None
    text = str(token).strip()
    if not text or text == "--":
        return None
    text = text.replace(",", ".")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _date_iso_from_line(line: str) -> Optional[str]:
    match = DATE_RE.match(line.strip())
    if not match:
        return None
    day = int(match.group(1))
    month_key = match.group(2).strip().lower()
    month_key = month_key.replace("ä", "ä")
    month = MONTHS_DE.get(month_key) or MONTHS_DE.get(month_key[:3])
    if month is None:
        return None
    year = int(match.group(3))
    return f"{year:04d}-{month:02d}-{day:02d}"


def _is_header_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return stripped.startswith("NAHRUNGSMITTEL") or sum(1 for marker in HEADER_MARKERS if marker in stripped) >= 3


def _is_totals_line(line: str) -> bool:
    return line.strip().startswith("GESAMT")


def _normalize_unit_hint(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    unit = value.strip().lower()
    if unit in {"g", "gr", "gram", "grams", "gramm", "gram(s)"}:
        return "g"
    if unit in {"ml", "milliliter", "millilitre"}:
        return "ml"
    if unit in {"pcs", "piece", "pieces", "stück", "stueck", "scheibe", "scheiben", "brötchen", "brot", "cup", "scoop", "tasse", "teller", "bowl", "serving", "servings", "serving(s)", "flasche", "donut", "bolo", "medium", "mittlere", "kleine", "große", "large", "regular", "slice"}:
        return "pcs"
    if "teelöffel" in unit or unit == "tl":
        return "g"
    return unit


def _parse_label_amount(label: str) -> Tuple[str, Optional[float], Optional[str]]:
    text = (label or "").strip()
    if not text:
        return "", None, None
    if "," not in text:
        return text, None, None
    base, suffix = text.rsplit(",", 1)
    suffix = suffix.strip()
    amount = _parse_num(suffix)
    if amount is None:
        return text, None, None
    parts = suffix.split()
    unit = None
    if len(parts) >= 2:
        unit = _normalize_unit_hint(parts[1])
    elif len(parts) == 1:
        token = re.sub(r"[-+]?\d+(?:[.,]\d+)?", "", parts[0]).strip()
        unit = _normalize_unit_hint(token) if token else None
    return base.strip(), amount, unit


def parse_dump(path: Path) -> List[ParsedDay]:
    days: List[ParsedDay] = []
    current_day: Optional[ParsedDay] = None
    current_meal: Optional[ParsedMeal] = None

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            date_iso = _date_iso_from_line(line)
            if date_iso:
                current_day = ParsedDay(date_iso=date_iso)
                days.append(current_day)
                current_meal = None
                continue

            if current_day is None:
                continue

            stripped = line.strip()
            if not stripped or _is_header_line(stripped) or _is_totals_line(stripped):
                continue

            meal_meta = MEAL_LABELS.get(stripped.lower())
            if meal_meta:
                current_meal = ParsedMeal(meal_slot=meal_meta[0], time_text=meal_meta[1])
                current_day.meals.append(current_meal)
                continue

            if current_meal is None:
                continue

            cols = [part for part in SPLIT_RE.split(stripped) if part]
            if len(cols) < 2:
                continue
            food_label = cols[0].strip()
            calories = _parse_num(cols[1])
            carbs = _parse_num(cols[2]) if len(cols) > 2 else None
            fat = _parse_num(cols[3]) if len(cols) > 3 else None
            protein = _parse_num(cols[4]) if len(cols) > 4 else None
            sugar = _parse_num(cols[7]) if len(cols) > 7 else None
            name, amount, unit = _parse_label_amount(food_label)
            current_meal.items.append(
                ParsedFoodRow(
                    raw_name=name or food_label,
                    calories=calories,
                    amount=amount,
                    unit=unit,
                    carbs=carbs,
                    fat=fat,
                    protein=protein,
                    sugar=sugar,
                )
            )
    return days


def _load_food_index(cur) -> List[Dict[str, Any]]:
    rows = cur.execute(
        """
        SELECT id, name, unit_default, common_portion_size, kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100, mfp_search_hint
        FROM nutrition_foods
        WHERE COALESCE(is_active, 1) = 1
        ORDER BY is_favorite DESC, name ASC
        """
    ).fetchall()
    foods: List[Dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["name_norm"] = _normalize_import_label(data.get("name"))
        data["hint_norm"] = _normalize_import_label(data.get("mfp_search_hint"))
        foods.append(data)
    return foods


def _candidate_foods(label: str, foods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = _normalize_import_label(label)
    if not normalized:
        return []
    brandless = normalized.split(" ", 1)[1] if " " in normalized and " - " in label else normalized
    normalized_with_boundaries = f" {normalized} "
    brandless_with_boundaries = f" {brandless} "
    exact_candidates = []
    fuzzy_candidates = []
    label_tokens = set(normalized.split())
    for food in foods:
        name_norm = food.get("name_norm") or ""
        hint_norm = food.get("hint_norm") or ""
        if normalized in {name_norm, hint_norm} or brandless in {name_norm, hint_norm}:
            exact_candidates.append((0 if normalized == name_norm else 1, len(name_norm or hint_norm), food))
            continue
        if name_norm and len(name_norm) >= 4 and (
            name_norm in {normalized, brandless}
            or f" {name_norm} " in normalized_with_boundaries
            or f" {name_norm} " in brandless_with_boundaries
        ):
            fuzzy_candidates.append((0, abs(len(normalized) - len(name_norm)), food))
            continue
        if hint_norm and len(hint_norm) >= 4 and (
            hint_norm in {normalized, brandless}
            or f" {hint_norm} " in normalized_with_boundaries
            or f" {hint_norm} " in brandless_with_boundaries
        ):
            fuzzy_candidates.append((1, abs(len(normalized) - len(hint_norm)), food))
            continue
        name_tokens = set(name_norm.split())
        hint_tokens = set(hint_norm.split())
        best_overlap = max(len(label_tokens & name_tokens), len(label_tokens & hint_tokens))
        if best_overlap > 0:
            fuzzy_candidates.append((3 - min(best_overlap, 3), 10 - best_overlap, food))
            continue
        similarity = max(
            difflib.SequenceMatcher(None, brandless, name_norm).ratio() if name_norm else 0.0,
            difflib.SequenceMatcher(None, brandless, hint_norm).ratio() if hint_norm else 0.0,
        )
        if similarity >= 0.72:
            fuzzy_candidates.append((4, 100 - int(similarity * 100), food))
    if exact_candidates:
        exact_candidates.sort(key=lambda item: (item[0], item[1], item[2]["name"]))
        return [item[2] for item in exact_candidates]
    if fuzzy_candidates:
        fuzzy_candidates.sort(key=lambda item: (item[0], item[1], item[2]["name"]))
        return [item[2] for item in fuzzy_candidates]
    return []


def _expected_kcal_for_item(food: Dict[str, Any], amount: float, unit: str) -> Optional[float]:
    kcal_ref = float(food.get("kcal_per_100") or 0.0)
    if kcal_ref <= 0:
        return None
    unit_default = (food.get("unit_default") or "g").lower()
    normalized_unit = (unit or unit_default or "g").lower()
    common_portion = float(food.get("common_portion_size") or 0.0)

    if unit_default in {"g", "ml"}:
        if normalized_unit not in {"g", "ml"}:
            return None
        return kcal_ref * (float(amount) / 100.0)

    if unit_default == "pcs":
        if normalized_unit == "pcs":
            return kcal_ref * float(amount)
        if normalized_unit in {"g", "ml"} and common_portion > 0:
            return kcal_ref * (float(amount) / common_portion)
        return None
    return None


def _kcal_match_ok(food: Dict[str, Any], row: ParsedFoodRow, amount: float, unit: str) -> bool:
    if row.calories is None or row.calories <= 0:
        return True
    expected = _expected_kcal_for_item(food, amount, unit)
    if expected is None:
        return True
    actual = float(row.calories)
    diff = abs(expected - actual)
    rel = diff / max(actual, 1.0)
    return diff <= 90.0 or rel <= 0.35


def _build_logged_item(row: ParsedFoodRow, food: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    unit_default = (food.get("unit_default") or "g").lower()
    common_portion = float(food.get("common_portion_size") or 0.0)
    raw_amount = row.amount
    raw_unit = (row.unit or "").lower()
    kcal_ref = float(food.get("kcal_per_100") or 0.0)

    amount = None
    unit = unit_default

    if unit_default in {"g", "ml"} and raw_amount is not None:
        if raw_unit in {"g", "ml"}:
            amount = raw_amount
            unit = unit_default
        elif raw_unit in {"g", "ml"}:
            amount = raw_amount
            unit = raw_unit
    elif unit_default == "pcs" and raw_amount is not None:
        if raw_unit == "pcs":
            amount = raw_amount
            unit = "pcs"
        elif raw_unit in {"g", "ml"} and common_portion > 0:
            amount = raw_amount / common_portion
            unit = "pcs"
        elif row.calories is not None and kcal_ref > 0:
            # pcs-based foods store macros per piece in this codebase.
            amount = float(row.calories) / kcal_ref
            unit = "pcs"

    if amount is None and row.calories is not None and kcal_ref > 0:
        if unit_default == "pcs":
            amount = row.calories / kcal_ref
            unit = "pcs"
        else:
            amount = (row.calories * 100.0) / kcal_ref
            unit = unit_default

    if amount is None:
        return None
    if not _kcal_match_ok(food, row, float(amount), unit):
        return None

    return {
        "item_type": "food",
        "food_id": int(food["id"]),
        "food_name": food.get("name"),
        "amount": round(float(amount), 3),
        "unit": unit,
        "calories": None,
    }


def _delete_existing_mfp_logs(cur, date_iso: str) -> int:
    rows = cur.execute(
        "SELECT id FROM nutrition_logged_meals WHERE log_date=? AND source='mfp_import'",
        (date_iso,),
    ).fetchall()
    meal_ids = [int(row["id"]) for row in rows]
    if not meal_ids:
        return 0
    placeholders = ",".join("?" for _ in meal_ids)
    cur.execute(f"DELETE FROM nutrition_logged_meal_items WHERE logged_meal_id IN ({placeholders})", tuple(meal_ids))
    cur.execute(f"DELETE FROM nutrition_logged_meals WHERE id IN ({placeholders})", tuple(meal_ids))
    return len(meal_ids)


def _existing_log_sources(cur, date_iso: str) -> List[str]:
    rows = cur.execute(
        "SELECT DISTINCT source FROM nutrition_logged_meals WHERE log_date=? ORDER BY source",
        (date_iso,),
    ).fetchall()
    return [str(row[0] or "") for row in rows]


def _delete_existing_logs_by_sources(cur, date_iso: str, sources: List[str]) -> int:
    normalized = [str(src).strip() for src in (sources or []) if str(src).strip()]
    if not normalized:
        return 0
    placeholders = ",".join("?" for _ in normalized)
    rows = cur.execute(
        f"SELECT id FROM nutrition_logged_meals WHERE log_date=? AND source IN ({placeholders})",
        (date_iso, *normalized),
    ).fetchall()
    meal_ids = [int(row["id"]) for row in rows]
    if not meal_ids:
        return 0
    meal_placeholders = ",".join("?" for _ in meal_ids)
    cur.execute(f"DELETE FROM nutrition_logged_meal_items WHERE logged_meal_id IN ({meal_placeholders})", tuple(meal_ids))
    cur.execute(f"DELETE FROM nutrition_logged_meals WHERE id IN ({meal_placeholders})", tuple(meal_ids))
    cur.execute(
        "DELETE FROM nutrition_planned_meal_status WHERE log_date=? AND (status IN ('logged','manual_override','adjusted_by_core','telegram_confirmed') OR logged_meal_id IS NOT NULL)",
        (date_iso,),
    )
    return len(meal_ids)


def import_dump(dump_path: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    days = parse_dump(dump_path)
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    foods = _load_food_index(cur)
    now = _utcnow_iso()

    stats: Dict[str, Any] = {
        "days_total": len(days),
        "days_imported": 0,
        "days_skipped_existing_logs": 0,
        "days_replaced_planned_logs": 0,
        "deleted_previous_mfp_meals": 0,
        "logged_meals_created": 0,
        "food_rows_total": 0,
        "food_rows_matched": 0,
        "food_rows_skipped": 0,
        "quick_add_rows": 0,
        "skipped_dates": [],
        "sample_unmatched": [],
    }

    for day in days:
        if not dry_run:
            conn.execute("BEGIN")
        stats["food_rows_total"] += sum(len(meal.items) for meal in day.meals)

        existing_sources = _existing_log_sources(cur, day.date_iso)
        existing_non_mfp = [src for src in existing_sources if src != "mfp_import"]
        replace_sources: List[str] = []
        if existing_non_mfp and set(existing_non_mfp).issubset({"planned"}):
            replace_sources = existing_non_mfp
        elif existing_non_mfp:
            stats["days_skipped_existing_logs"] += 1
            if len(stats["skipped_dates"]) < 25:
                stats["skipped_dates"].append(day.date_iso)
            if not dry_run:
                conn.commit()
            continue

        if not dry_run:
            stats["deleted_previous_mfp_meals"] += _delete_existing_mfp_logs(cur, day.date_iso)
            if replace_sources:
                stats["days_replaced_planned_logs"] += 1
                _delete_existing_logs_by_sources(cur, day.date_iso, replace_sources)

        created_for_day = 0
        for meal_index, meal in enumerate(day.meals):
            items: List[Dict[str, Any]] = []
            for row in meal.items:
                label = row.raw_name.strip()
                if not label:
                    continue
                if "quick add" in label.lower():
                    if row.calories is not None:
                        items.append(
                            {
                                "item_type": "kcal_only",
                                "food_id": None,
                                "food_name": "Quick Add",
                                "amount": float(row.calories),
                                "unit": "kcal",
                                "calories": float(row.calories),
                                "protein_override": row.protein,
                                "carbs_override": row.carbs,
                                "fat_override": row.fat,
                                "sugar_override": row.sugar,
                            }
                        )
                        stats["quick_add_rows"] += 1
                    else:
                        stats["food_rows_skipped"] += 1
                    continue
                candidates = _candidate_foods(label, foods)
                item = None
                for food in candidates:
                    item = _build_logged_item(row, food)
                    if item is not None:
                        break
                if item is None:
                    if row.calories is not None:
                        items.append(
                            {
                                "item_type": "kcal_only",
                                "food_id": None,
                                "food_name": label,
                                "amount": float(row.calories),
                                "unit": "kcal",
                                "calories": float(row.calories),
                                "protein_override": row.protein,
                                "carbs_override": row.carbs,
                                "fat_override": row.fat,
                                "sugar_override": row.sugar,
                            }
                        )
                        continue
                    else:
                        stats["food_rows_skipped"] += 1
                        if len(stats["sample_unmatched"]) < 30:
                            stats["sample_unmatched"].append(label)
                        continue
                items.append(item)
                stats["food_rows_matched"] += 1

            if not items:
                continue

            created_for_day += 1
            if dry_run:
                continue

            logged_at = f"{day.date_iso}T{meal.time_text}:00"
            cur.execute(
                """
                INSERT INTO nutrition_logged_meals
                    (log_date, slot_id, title, meal_slot, source, created_from_planned_slot_id, created_from_template_id,
                     edited_from_template, original_planned_time, logged_at, is_favorite, created_at, updated_at, action_source, trace_json)
                VALUES (?, NULL, ?, ?, 'mfp_import', NULL, NULL, 0, NULL, ?, 0, ?, ?, 'mfp_import', ?)
                """,
                (
                    day.date_iso,
                    meal.meal_slot,
                    meal.meal_slot,
                    logged_at,
                    now,
                    now,
                    json.dumps(
                        {
                            "import": "mfp_dump",
                            "dump_path": str(dump_path),
                            "meal_index": meal_index,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            logged_meal_id = int(cur.lastrowid)
            _insert_logged_meal_items(cur, logged_meal_id, items)
            _record_recent_foods(cur, items=items, meal_slot=meal.meal_slot, log_date=day.date_iso, logged_at=logged_at)
            _record_recent_meal(cur, title=meal.meal_slot, meal_slot=meal.meal_slot, items=items, log_date=day.date_iso, logged_at=logged_at)

        if created_for_day > 0:
            stats["days_imported"] += 1
            stats["logged_meals_created"] += created_for_day
            if not dry_run:
                _sync_logging_totals_to_weight_logs(cur, day.date_iso)
        if not dry_run:
            conn.commit()

    conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dump_path", nargs="?", default=str(ROOT / "import" / "mfp_dump.txt"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = import_dump(Path(args.dump_path), dry_run=args.dry_run)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
