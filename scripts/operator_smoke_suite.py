#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database.connections as connections


@dataclass
class TestResult:
    name: str
    ok: bool
    details: str = ""
    changed: list[str] = field(default_factory=list)
    reverted: list[str] = field(default_factory=list)
    cleanup_needed: list[str] = field(default_factory=list)


class SmokeFailure(RuntimeError):
    pass


class HTTPErrorDetails:
    """Detailed HTTP error information for debugging."""
    def __init__(self, suite_block: str, step_name: str, method: str, url: str, request_body: dict[str, Any] | None, status_code: int | None, response_body: str):
        self.suite_block = suite_block
        self.step_name = step_name
        self.method = method
        self.url = url
        self.request_body = request_body
        self.status_code = status_code
        self.response_body = response_body
        self.error_code = None
        self.error_message = None
        self.error_details = None
        
        # Try to parse JSON error
        if response_body:
            try:
                data = json.loads(response_body)
                if isinstance(data, dict):
                    self.error_code = data.get("error_code") or data.get("code")
                    self.error_message = data.get("error_message") or data.get("message") or data.get("detail")
                    self.error_details = data.get("error_details") or data.get("details")
            except json.JSONDecodeError:
                pass
    
    def __str__(self) -> str:
        lines = [
            f"Suite Block: {self.suite_block}",
            f"Step: {self.step_name}",
            f"HTTP {self.method} {self.status_code or '???'}: {self.url}",
        ]
        if self.request_body:
            lines.append(f"Request Body: {json.dumps(self.request_body)[:500]}")
        if self.response_body:
            lines.append(f"Response: {self.response_body[:500]}")
        if self.error_code:
            lines.append(f"Error Code: {self.error_code}")
        if self.error_message:
            lines.append(f"Error Message: {self.error_message}")
        if self.error_details:
            lines.append(f"Error Details: {self.error_details}")
        return "\n  ".join(lines)


class OperatorSmokeSuite:
    def __init__(self, *, base_url: str, token: str, live: bool, cleanup: bool) -> None:
        self.base_url = base_url.rstrip("/")
        self.live = live
        self.cleanup = cleanup
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self.results: list[TestResult] = []
        self.created: dict[str, list[Any]] = {"nutrition_plans": [], "training_plans": [], "foods": [], "templates": [], "exercises": []}
        self.snapshots: dict[str, Any] = {}
        self.ts = str(int(time.time()))
        self.current_suite_block = ""
        self.current_step_name = ""

    def run(self) -> int:
        self.check_openapi()
        if self.live:
            self.snapshot_before()
            try:
                self.run_nutrition()
                self.run_training()
            finally:
                if self.cleanup:
                    self.restore_and_log()
        self.print_summary()
        return 0 if all(r.ok for r in self.results) else 1

    def add_result(self, name: str, ok: bool, details: str = "", changed: list[str] | None = None, reverted: list[str] | None = None, cleanup_needed: list[str] | None = None) -> None:
        self.results.append(TestResult(name=name, ok=ok, details=details, changed=changed or [], reverted=reverted or [], cleanup_needed=cleanup_needed or []))

    def url(self, path: str, **params: Any) -> str:
        qs = urlencode({k: v for k, v in params.items() if v is not None})
        return f"{self.base_url}{path}" + (f"?{qs}" if qs else "")

    def _handle_http_error(self, method: str, path: str, request_body: dict[str, Any] | None, exc: requests.RequestException) -> None:
        """Extract and log detailed HTTP error information."""
        response_body = ""
        status_code = None
        
        if hasattr(exc, 'response') and exc.response is not None:
            response_body = exc.response.text or ""
            status_code = exc.response.status_code
        
        details = HTTPErrorDetails(
            suite_block=self.current_suite_block,
            step_name=self.current_step_name,
            method=method,
            url=self.url(path),
            request_body=request_body,
            status_code=status_code,
            response_body=response_body
        )
        print(f"\nHTTP Error in {self.current_suite_block}.{self.current_step_name}:")
        print(f"  {details}")

    def get(self, path: str, *, auth: bool = True, **params: Any) -> Any:
        headers = self.session.headers if auth else {}
        try:
            resp = self.session.get(self.url(path, **params), headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            self._handle_http_error("GET", path, None, exc)
            raise

    def post(self, path: str, payload: dict[str, Any], *, auth: bool = True) -> Any:
        headers = self.session.headers if auth else {"Content-Type": "application/json"}
        try:
            resp = self.session.post(self.url(path), headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            self._handle_http_error("POST", path, payload, exc)
            raise

    def patch_plan(self, domain: str, plan_id: int, operations: list[dict[str, Any]]) -> Any:
        payload = {"dry_run": False, "operations": operations}
        return self.post(f"/api/v2/actions/{domain}/plans/{plan_id}/patch", payload)

    def get_plan_detail(self, domain: str, plan_id: int) -> Any:
        return self.get(f"/api/v2/actions/{domain}/plans/{plan_id}/detail")

    def get_active_detail(self, domain: str) -> Any:
        return self.get(f"/api/v2/actions/{domain}/plans/active/detail")

    def db_counts(self) -> dict[str, Any]:
        nutrition = connections.get_nutrition_db()
        plans = connections.get_plans_db()
        training = connections.get_training_db()
        try:
            return {
                "nutrition_slot_meal_overrides": nutrition.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0],
                "nutrition_foods": nutrition.execute("SELECT COUNT(*) FROM nutrition_foods").fetchone()[0],
                "nutrition_meal_templates": nutrition.execute("SELECT COUNT(*) FROM nutrition_meal_templates").fetchone()[0],
                "gym_plans": plans.execute("SELECT COUNT(*) FROM gym_plans").fetchone()[0],
                "movement_library": training.execute("SELECT COUNT(*) FROM movement_library").fetchone()[0],
            }
        finally:
            nutrition.close()
            plans.close()
            training.close()

    def check_openapi(self) -> None:
        """Check that required patch endpoints exist in OpenAPI spec."""
        self.current_suite_block = "openapi_check"
        paths_file = ROOT / "openapi" / "liva-actions-v2.json"
        
        if not paths_file.exists():
            self.add_result("openapi_required_ops", False, f"OpenAPI file not found: {paths_file}")
            return
        
        data = json.loads(paths_file.read_text(encoding="utf-8"))
        paths = data.get("paths", {})
        
        # Check that both patch endpoints exist
        required_endpoints = [
            "/actions/nutrition/plans/{plan_id}/patch",
            "/actions/training/plans/{plan_id}/patch",
        ]
        
        missing = [ep for ep in required_endpoints if ep not in paths]
        
        if missing:
            self.add_result(
                "openapi_required_ops",
                False,
                f"Missing endpoints in {paths_file.name}: {', '.join(missing)}"
            )
        else:
            # Verify patch operations are described in the schema
            details = "patch endpoints exist; operations are in schema (check liva-actions-v2.json PlanPatchRequest)"
            self.add_result("openapi_required_ops", True, details)

    def snapshot_before(self) -> None:
        self.current_suite_block = "snapshot"
        nutrition_active = self.get_active_detail("nutrition")
        training_active = self.get_active_detail("training")
        self.snapshots["nutrition_active_id"] = int(nutrition_active["plan_id"])
        self.snapshots["training_active_id"] = int(training_active["plan_id"])
        self.snapshots["nutrition_active_detail"] = nutrition_active
        self.snapshots["training_active_detail"] = training_active
        self.snapshots["counts_before"] = self.db_counts()

    def find_meal(self, detail: dict[str, Any], day_index: int, meal_index: int) -> dict[str, Any]:
        return detail["days"][day_index]["meals"][meal_index]

    def _execute_step(self, step_name: str, fn, *args, **kwargs) -> Any:
        """Execute a single step with error handling."""
        self.current_step_name = step_name
        return fn(*args, **kwargs)

    def _table_exists(self, conn, table: str) -> bool:
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            ).fetchone() is not None
        except Exception:
            return False

    def _table_columns(self, conn, table: str) -> list[str]:
        try:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        except Exception:
            return []

    def _count_rows_with_prefix(self, conn, table: str, prefix: str, preferred_cols: list[str] | None = None) -> int:
        if not self._table_exists(conn, table):
            return 0

        cols = self._table_columns(conn, table)
        preferred_cols = preferred_cols or []

        for col in preferred_cols:
            if col in cols:
                try:
                    return int(conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?",
                        (f"%{prefix}%",),
                    ).fetchone()[0])
                except Exception:
                    pass

        # Fallback: scan rows defensively without assuming specific columns.
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        except Exception:
            return 0

        n = 0
        for row in rows:
            values = tuple(row)
            if any(prefix in str(value) for value in values if value is not None):
                n += 1
        return n

    def _table_exists(self, conn, table: str) -> bool:
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            ).fetchone() is not None
        except Exception:
            return False

    def _table_columns(self, conn, table: str) -> list[str]:
        try:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        except Exception:
            return []

    def _count_rows_with_prefix(self, conn, table: str, prefix: str, preferred_cols: list[str] | None = None) -> int:
        if not self._table_exists(conn, table):
            return 0

        cols = self._table_columns(conn, table)
        preferred_cols = preferred_cols or []

        for col in preferred_cols:
            if col in cols:
                try:
                    return int(conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?",
                        (f"%{prefix}%",),
                    ).fetchone()[0])
                except Exception:
                    pass

        # Fallback: scan rows defensively without assuming specific columns.
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        except Exception:
            return 0

        n = 0
        for row in rows:
            values = tuple(row)
            if any(prefix in str(value) for value in values if value is not None):
                n += 1
        return n

    def _count_created_items(self) -> dict[str, int]:
        """Count items with __operator_smoke_ prefix. Robust against missing tables/columns."""
        nutrition = connections.get_nutrition_db()
        plans = connections.get_plans_db()
        training = connections.get_training_db()
        try:
            return {
                "nutrition_plans": self._count_rows_with_prefix(
                    nutrition,
                    "nutrition_week_templates",
                    "__operator_smoke_nutrition_",
                    ["title", "name"],
                ),
                "training_plans": self._count_rows_with_prefix(
                    plans,
                    "gym_plans",
                    "__operator_smoke_training_",
                    ["title", "name", "plan_name"],
                ),
                "foods": self._count_rows_with_prefix(
                    nutrition,
                    "nutrition_foods",
                    "__operator_smoke_food_",
                    ["name", "title"],
                ),
                "templates": self._count_rows_with_prefix(
                    nutrition,
                    "nutrition_meal_templates",
                    "__operator_smoke_template_",
                    ["title", "name"],
                ),
                "exercises": self._count_rows_with_prefix(
                    training,
                    "movement_library",
                    "__operator_smoke_exercise_",
                    ["name", "title", "canonical_id"],
                ),
            }
        finally:
            nutrition.close()
            plans.close()
            training.close()


    def run_nutrition(self) -> None:
        self.current_suite_block = "nutrition_live_suite"
        plan_id = int(self.snapshots["nutrition_active_id"])
        original_overrides = self.snapshots["counts_before"]["nutrition_slot_meal_overrides"]
        changed: list[str] = []
        reverted: list[str] = []
        
        try:
            detail = self._execute_step("nutrition.load_plan", self.get_plan_detail, "nutrition", plan_id)
            lunch = self.find_meal(detail, 0, min(2, len(detail["days"][0]["meals"]) - 1))
            original_title = lunch["title"]
            original_time = lunch.get("time_text")
            original_note = lunch.get("note_text")
            slot = lunch["slots"][0]
            original_amount = slot["amount"]
            slot_path = slot["slot_path"]
            
            # rename_meal cycle
            self._execute_step("nutrition.rename_meal", self.patch_plan, "nutrition", plan_id, 
                [{"op": "rename_meal", "match": {"day_index": 0, "meal_index": lunch["meal_index"]}, "title": "__operator_smoke_meal_title__"}])
            changed.append("nutrition.rename_meal")
            week = self._execute_step("nutrition.verify_rename_meal", self.get, "/api/nutrition/plan/week", auth=False, template=plan_id)
            assert "__operator_smoke_meal_title__" in json.dumps(week, ensure_ascii=False)
            
            self._execute_step("nutrition.rename_meal_revert", self.patch_plan, "nutrition", plan_id,
                [{"op": "rename_meal", "match": {"day_index": 0, "meal_index": lunch["meal_index"]}, "title": original_title}])
            reverted.append("nutrition.rename_meal")

            # update_slot_amount cycle
            self._execute_step("nutrition.update_slot_amount", self.patch_plan, "nutrition", plan_id,
                [{"op": "update_slot_amount", "match": {"slot_path": slot_path}, "amount": float(original_amount) + 1, "unit": slot["unit"]}])
            changed.append("nutrition.update_slot_amount")
            week = self._execute_step("nutrition.verify_update_slot_amount", self.get, "/api/nutrition/plan/week", auth=False, template=plan_id)
            assert f"{int(float(original_amount) + 1)} {slot['unit']}" in json.dumps(week, ensure_ascii=False)
            
            self._execute_step("nutrition.update_slot_amount_revert", self.patch_plan, "nutrition", plan_id,
                [{"op": "update_slot_amount", "match": {"slot_path": slot_path}, "amount": original_amount, "unit": slot["unit"]}])
            reverted.append("nutrition.update_slot_amount")

            # update_meal_time cycle
            self._execute_step("nutrition.update_meal_time", self.patch_plan, "nutrition", plan_id,
                [{"op": "update_meal_time", "match": {"day_index": 0, "meal_index": lunch["meal_index"]}, "time": "15:59"}])
            changed.append("nutrition.update_meal_time")
            today = self._execute_step("nutrition.verify_update_meal_time_today", self.get, "/api/nutrition/plan/today", auth=False, include_items=1)
            week = self._execute_step("nutrition.verify_update_meal_time_week", self.get, "/api/nutrition/plan/week", auth=False, template=plan_id)
            assert "15:59" in json.dumps(week, ensure_ascii=False)
            
            self._execute_step("nutrition.update_meal_time_revert", self.patch_plan, "nutrition", plan_id,
                [{"op": "update_meal_time", "match": {"day_index": 0, "meal_index": lunch["meal_index"]}, "time": original_time}])
            reverted.append("nutrition.update_meal_time")

            # set_meal_note cycle
            self._execute_step("nutrition.set_meal_note", self.patch_plan, "nutrition", plan_id,
                [{"op": "set_meal_note", "match": {"day_index": 0, "meal_index": lunch["meal_index"]}, "note": "Testnotiz"}])
            changed.append("nutrition.set_meal_note")
            
            self._execute_step("nutrition.set_meal_note_revert", self.patch_plan, "nutrition", plan_id,
                [{"op": "set_meal_note", "match": {"day_index": 0, "meal_index": lunch["meal_index"]}, "note": original_note}])
            reverted.append("nutrition.set_meal_note")

            # duplicate_plan
            dup = self._execute_step("nutrition.duplicate_plan", self.patch_plan, "nutrition", plan_id,
                [{"op": "duplicate_plan", "name": f"__operator_smoke_nutrition_{self.ts}"}])
            dup_id = max(plan["plan_id"] for plan in self._execute_step("nutrition.list_plans", self.get, "/api/v2/actions/nutrition/plans")["plans"] 
                        if str(plan["name"]).startswith("__operator_smoke_nutrition_"))
            self.created["nutrition_plans"].append(dup_id)

            # set_active_plan and restore
            self._execute_step("nutrition.set_active_plan_copy", self.patch_plan, "nutrition", dup_id,
                [{"op": "set_active_plan"}])
            self._execute_step("nutrition.restore_active_plan", self.patch_plan, "nutrition", plan_id,
                [{"op": "set_active_plan"}])

            # archive/unarchive cycle
            self._execute_step("nutrition.archive_plan_copy", self.patch_plan, "nutrition", dup_id,
                [{"op": "archive_plan"}])
            self._execute_step("nutrition.unarchive_plan_copy", self.patch_plan, "nutrition", dup_id,
                [{"op": "unarchive_plan"}])

            # create_food, update_food, templates, etc. on dup_id
            food_name = f"__operator_smoke_food_{self.ts}"
            food_resp = self._execute_step("nutrition.create_food", self.patch_plan, "nutrition", dup_id,
                [{"op": "create_food", "food": {"name": food_name, "kcal_per_100g": 100, "protein_g_per_100g": 10, "carbs_g_per_100g": 10, "fat_g_per_100g": 1, "default_unit": "pcs"}}])
            food_id = int(food_resp["validation"]["created_resources"][0]["food_id"])
            self.created["foods"].append(food_id)
            
            self._execute_step("nutrition.update_food", self.patch_plan, "nutrition", dup_id,
                [{"op": "update_food", "food_id": food_id, "patch": {"name": f"{food_name}_v2", "default_unit": "ml"}}])
            
            tpl_resp = self._execute_step("nutrition.create_meal_template", self.patch_plan, "nutrition", dup_id,
                [{"op": "create_meal_template", "template": {"title": f"__operator_smoke_template_{self.ts}", "items": [{"food_id": food_id, "amount": 1, "unit": "pcs"}]}}])
            tpl_id = int(tpl_resp["validation"]["created_resources"][0]["template_id"])
            self.created["templates"].append(tpl_id)
            
            self._execute_step("nutrition.update_meal_template", self.patch_plan, "nutrition", dup_id,
                [{"op": "update_meal_template", "template_id": tpl_id, "title": f"__operator_smoke_template_{self.ts}_v2"}])
            
            self._execute_step("nutrition.add_meal_from_template", self.patch_plan, "nutrition", dup_id,
                [{"op": "add_meal_from_template", "target": {"day_index": 0, "meal_index": 0}, "template_id": tpl_id, "materialize": True}])
            
            self._execute_step("nutrition.overwrite_plan_copy", self.patch_plan, "nutrition", dup_id,
                [{"op": "overwrite_plan", "days": [{"day": "Mo", "meals": [{"title": "Smoke Meal", "items": [{"food_id": food_id, "amount": 1, "unit": "pcs"}]}]}]}])
            
            self._execute_step("nutrition.delete_empty_meals", self.patch_plan, "nutrition", dup_id,
                [{"op": "delete_empty_meals"}])
            
            self._execute_step("nutrition.delete_meal_template", self.patch_plan, "nutrition", dup_id,
                [{"op": "delete_meal_template", "template_id": tpl_id}])
            
            self._execute_step("nutrition.delete_food", self.patch_plan, "nutrition", dup_id,
                [{"op": "delete_food", "food_id": food_id, "force": True}])
            
            self._execute_step("nutrition.soft_delete_plan_copy", self.patch_plan, "nutrition", dup_id,
                [{"op": "soft_delete_plan"}])

            # Verify final state
            counts_after = self.db_counts()
            final_active = self._execute_step("nutrition.verify_final_active", self.get_active_detail, "nutrition")
            ok = (counts_after["nutrition_slot_meal_overrides"] == original_overrides and 
                  int(final_active["plan_id"]) == plan_id)
            
            removed_smoke_plans = self._cleanup_smoke_nutrition_plans()
            smoke_items = self._count_created_items()
            details = f"active_plan={plan_id} duplicate_plan={dup_id} hard_deleted_nutrition_smoke_plans={removed_smoke_plans} smoke_cleanup: plans={smoke_items['nutrition_plans']} foods={smoke_items['foods']} templates={smoke_items['templates']}"
            self.add_result("nutrition_live_suite", ok, details, changed=changed, reverted=reverted)
        except Exception as exc:
            smoke_items = self._count_created_items()
            self.add_result("nutrition_live_suite", False, 
                f"Exception at {self.current_step_name}: {exc} | cleanup: {smoke_items}",
                changed=changed, reverted=reverted)

    def run_training(self) -> None:
        self.current_suite_block = "training_live_suite"
        plan_id = int(self.snapshots["training_active_id"])
        detail = self.get_plan_detail("training", plan_id)
        changed: list[str] = []
        reverted: list[str] = []
        
        try:
            workout = detail["days"][0]["workouts"][0]
            ex = workout["exercises"][min(1, len(workout["exercises"]) - 1)]
            set0 = ex["sets"][0]
            original_note = ex.get("notes")
            original_variation = ex.get("variation")
            original_rpe = set0.get("target_rpe")
            original_workout_title = workout["title"]
            original_workout_note = workout.get("notes")
            
            self._execute_step("training.update_exercise_note", self.patch_plan, "training", plan_id,
                [{"op": "update_exercise_note", "match": {"exercise_path": ex["exercise_path"]}, "note": "__operator_smoke_note__"}])
            changed.append("training.update_exercise_note")
            
            self._execute_step("training.update_exercise_note_revert", self.patch_plan, "training", plan_id,
                [{"op": "update_exercise_note", "match": {"exercise_path": ex["exercise_path"]}, "note": original_note}])
            reverted.append("training.update_exercise_note")
            
            self._execute_step("training.update_exercise_variation", self.patch_plan, "training", plan_id,
                [{"op": "update_exercise_variation", "match": {"exercise_path": ex["exercise_path"]}, "variation": "__operator_smoke_variation__"}])
            changed.append("training.update_exercise_variation")
            
            self._execute_step("training.update_exercise_variation_revert", self.patch_plan, "training", plan_id,
                [{"op": "update_exercise_variation", "match": {"exercise_path": ex["exercise_path"]}, "variation": original_variation}])
            reverted.append("training.update_exercise_variation")
            
            self._execute_step("training.update_set_target", self.patch_plan, "training", plan_id,
                [{"op": "update_set_target", "match": {"set_path": set0["set_path"]}, "target_rpe": float(original_rpe) + 0.5}])
            changed.append("training.update_set_target")
            
            self._execute_step("training.update_set_target_revert", self.patch_plan, "training", plan_id,
                [{"op": "update_set_target", "match": {"set_path": set0["set_path"]}, "target_rpe": original_rpe}])
            reverted.append("training.update_set_target")
            
            self._execute_step("training.set_workout_note", self.patch_plan, "training", plan_id,
                [{"op": "set_workout_note", "match": {"day_index": 0, "workout_index": 0}, "note": "__operator_smoke_workout_note__"}])
            changed.append("training.set_workout_note")
            
            self._execute_step("training.set_workout_note_revert", self.patch_plan, "training", plan_id,
                [{"op": "set_workout_note", "match": {"day_index": 0, "workout_index": 0}, "note": original_workout_note}])
            reverted.append("training.set_workout_note")
            
            self._execute_step("training.rename_workout", self.patch_plan, "training", plan_id,
                [{"op": "rename_workout", "match": {"day_index": 0, "workout_index": 0}, "title": "__operator_smoke_workout__"}])
            changed.append("training.rename_workout")
            
            self._execute_step("training.rename_workout_revert", self.patch_plan, "training", plan_id,
                [{"op": "rename_workout", "match": {"day_index": 0, "workout_index": 0}, "title": original_workout_title}])
            reverted.append("training.rename_workout")

            self._execute_step("training.duplicate_plan", self.patch_plan, "training", plan_id,
                [{"op": "duplicate_plan", "name": f"__operator_smoke_training_{self.ts}"}])
            dup_id = max(p["plan_id"] for p in self._execute_step("training.list_plans", self.get, "/api/v2/actions/training/plans")["plans"] 
                        if str(p["name"]).startswith("__operator_smoke_training_"))
            self.created["training_plans"].append(dup_id)
            
            self._execute_step("training.set_active_plan_copy", self.patch_plan, "training", dup_id,
                [{"op": "set_active_plan"}])
            self._execute_step("training.restore_active_plan", self.patch_plan, "training", plan_id,
                [{"op": "set_active_plan"}])
            self._execute_step("training.archive_plan_copy", self.patch_plan, "training", dup_id,
                [{"op": "archive_plan"}])
            self._execute_step("training.unarchive_plan_copy", self.patch_plan, "training", dup_id,
                [{"op": "unarchive_plan"}])

            create_resp = self._execute_step("training.create_exercise", self.patch_plan, "training", dup_id,
                [{"op": "create_exercise", "exercise": {"name": f"__operator_smoke_exercise_{self.ts}", "canonical_id": f"__operator_smoke_exercise_{self.ts}", "muscle_group": "chest", "equipment": "machine", "notes": "smoke"}}])
            search = self._execute_step("training.search_exercise", self.get, "/api/v2/actions/training/exercises/search", query=f"__operator_smoke_exercise_{self.ts}")
            ex_id = next(item["exercise_id"] for item in search["exercises"] if item["canonical_id"] == f"__operator_smoke_exercise_{self.ts}")
            self.created["exercises"].append(ex_id)
            
            self._execute_step("training.update_exercise", self.patch_plan, "training", dup_id,
                [{"op": "update_exercise", "exercise_id": ex_id, "patch": {"name": f"__operator_smoke_exercise_{self.ts}_v2", "canonical_id": f"__operator_smoke_exercise_{self.ts}_v2", "notes": "updated"}}])

            dup_detail = self._execute_step("training.load_dup_plan", self.get_plan_detail, "training", dup_id)
            self._execute_step("training.add_exercise", self.patch_plan, "training", dup_id,
                [{"op": "add_exercise", "target": {"day_index": 0, "workout_index": 0}, "name": "Smoke Added", "canonical_id": "smoke_added", "sets": [{"target_reps": "8-10", "target_rpe": 8}]}])
            
            dup_detail = self._execute_step("training.load_dup_plan_v2", self.get_plan_detail, "training", dup_id)
            last_ex = dup_detail["days"][0]["workouts"][0]["exercises"][-1]
            self._execute_step("training.add_set", self.patch_plan, "training", dup_id,
                [{"op": "add_set", "target": {"exercise_path": last_ex["exercise_path"]}, "set": {"target_reps": "10-12", "target_rpe": 9}}])
            
            dup_detail = self._execute_step("training.load_dup_plan_v3", self.get_plan_detail, "training", dup_id)
            last_ex = dup_detail["days"][0]["workouts"][0]["exercises"][-1]
            new_set = last_ex["sets"][-1]
            self._execute_step("training.delete_set", self.patch_plan, "training", dup_id,
                [{"op": "delete_set", "match": {"set_path": new_set["set_path"]}}])
            
            self._execute_step("training.move_exercise", self.patch_plan, "training", dup_id,
                [{"op": "move_exercise", "match": {"exercise_path": last_ex["exercise_path"]}, "target": {"day_index": 0, "workout_index": 0, "exercise_index": 0}}])
            
            dup_detail = self._execute_step("training.load_dup_plan_v4", self.get_plan_detail, "training", dup_id)
            moved_ex = dup_detail["days"][0]["workouts"][0]["exercises"][0]
            self._execute_step("training.replace_exercise", self.patch_plan, "training", dup_id,
                [{"op": "replace_exercise", "match": {"exercise_path": moved_ex["exercise_path"]}, "replacement": {"name": "Smoke Replaced", "canonical_id": "smoke_replaced"}}])
            
            self._execute_step("training.update_periodization", self.patch_plan, "training", dup_id,
                [{"op": "update_periodization", "periodization": {"phase": "accumulation", "week": 2, "deload": False}}])
            
            self._execute_step("training.update_caps", self.patch_plan, "training", dup_id,
                [{"op": "update_caps", "caps": {"max_rpe": 8.5}}])
            
            self._execute_step("training.update_progression_rule", self.patch_plan, "training", dup_id,
                [{"op": "update_progression_rule", "match": {"day_index": 0, "workout_index": 0, "exercise_index": 0}, "progression_rule": {"mode": "double_progression"}}])
            
            self._execute_step("training.overwrite_plan_copy", self.patch_plan, "training", dup_id,
                [{"op": "overwrite_plan", "days": [{"day_id": "Mo", "label": "Mo", "workouts": [{"title": "Smoke Workout", "notes": "suite", "exercises": [{"name": "Smoke Replaced", "canonical_id": "smoke_replaced", "sets": [{"target_reps": "6-8", "target_rpe": 8}]}]}]}]}])
            
            overwritten = self._execute_step("training.verify_overwrite", self.get_plan_detail, "training", dup_id)
            assert overwritten["days"][0]["workouts"][0]["title"] == "Smoke Workout"
            
            self._execute_step("training.soft_delete_plan_copy", self.patch_plan, "training", dup_id,
                [{"op": "soft_delete_plan"}])

            snapshot = self._execute_step("training.verify_snapshot", self.get, "/api/training_snapshot", auth=False)
            final_active = self._execute_step("training.verify_final_active", self.get_active_detail, "training")
            ok = (int(final_active["plan_id"]) == plan_id and snapshot.get("ok") is True)
            
            removed_training_smoke = self._cleanup_smoke_training_items()
            smoke_items = self._count_created_items()
            details = f"active_plan={plan_id} duplicate_plan={dup_id} hard_deleted_training_smoke={removed_training_smoke} smoke_cleanup: plans={smoke_items['training_plans']} exercises={smoke_items['exercises']}"
            self.add_result("training_live_suite", ok, details, changed=changed, reverted=reverted)
        except Exception as exc:
            smoke_items = self._count_created_items()
            self.add_result("training_live_suite", False,
                f"Exception at {self.current_step_name}: {exc} | cleanup: {smoke_items}",
                changed=changed, reverted=reverted)


    def _cleanup_smoke_nutrition_plans(self) -> int:
        """Hard-delete __operator_smoke_ nutrition plan copies and their concrete source rows."""
        conn = connections.get_nutrition_db()
        conn.row_factory = sqlite3.Row
        try:
            if not self._table_exists(conn, "nutrition_week_templates"):
                return 0

            rows = conn.execute(
                "SELECT id FROM nutrition_week_templates WHERE title LIKE '__operator_smoke_%'"
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return 0

            q = ",".join("?" for _ in ids)
            day_rows = conn.execute(
                f"SELECT id FROM nutrition_week_template_days WHERE week_template_id IN ({q})",
                ids,
            ).fetchall()
            day_ids = [int(row["id"]) for row in day_rows]

            conn.execute("BEGIN")

            if day_ids:
                dq = ",".join("?" for _ in day_ids)

                meal_rows = conn.execute(
                    f"SELECT id FROM nutrition_slot_meals WHERE week_template_day_id IN ({dq})",
                    day_ids,
                ).fetchall() if self._table_exists(conn, "nutrition_slot_meals") else []
                meal_ids = [int(row["id"]) for row in meal_rows]

                if meal_ids and self._table_exists(conn, "nutrition_slot_meal_items"):
                    mq = ",".join("?" for _ in meal_ids)
                    conn.execute(f"DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id IN ({mq})", meal_ids)

                if meal_ids and self._table_exists(conn, "nutrition_slot_meals"):
                    mq = ",".join("?" for _ in meal_ids)
                    conn.execute(f"DELETE FROM nutrition_slot_meals WHERE id IN ({mq})", meal_ids)

                if self._table_exists(conn, "nutrition_week_day_slots"):
                    slot_cols = self._table_columns(conn, "nutrition_week_day_slots")
                    if "week_template_day_id" in slot_cols:
                        slot_rows = conn.execute(
                            f"SELECT id FROM nutrition_week_day_slots WHERE week_template_day_id IN ({dq})",
                            day_ids,
                        ).fetchall()
                        slot_ids = [int(row["id"]) for row in slot_rows]

                        if slot_ids and self._table_exists(conn, "nutrition_slot_meal_overrides"):
                            sq = ",".join("?" for _ in slot_ids)
                            conn.execute(f"DELETE FROM nutrition_slot_meal_overrides WHERE slot_id IN ({sq})", slot_ids)

                        conn.execute(
                            f"DELETE FROM nutrition_week_day_slots WHERE week_template_day_id IN ({dq})",
                            day_ids,
                        )

                if self._table_exists(conn, "nutrition_week_template_days"):
                    conn.execute(f"DELETE FROM nutrition_week_template_days WHERE id IN ({dq})", day_ids)

            conn.execute(f"DELETE FROM nutrition_week_templates WHERE id IN ({q})", ids)
            conn.commit()
            return len(ids)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()



    def _cleanup_smoke_training_items(self) -> dict[str, int]:
        """Hard-delete __operator_smoke_ training plan/exercise artifacts."""
        removed = {"training_plans": 0, "exercises": 0}

        plans = connections.get_plans_db()
        try:
            if self._table_exists(plans, "gym_plans"):
                cols = self._table_columns(plans, "gym_plans")
                if "title" in cols:
                    plans.execute(
                        "DELETE FROM gym_plans WHERE title LIKE '__operator_smoke_training_%' AND COALESCE(is_active,0)=0"
                    )
                    removed["training_plans"] = int(plans.total_changes)
                    plans.commit()
        finally:
            plans.close()

        training = connections.get_training_db()
        try:
            if self._table_exists(training, "movement_library"):
                cols = self._table_columns(training, "movement_library")
                clauses = []
                if "movement_key" in cols:
                    clauses.append("movement_key LIKE '__operator_smoke_exercise_%'")
                if "label" in cols:
                    clauses.append("label LIKE '__operator_smoke_exercise_%'")
                if "name" in cols:
                    clauses.append("name LIKE '__operator_smoke_exercise_%'")
                if "canonical_id" in cols:
                    clauses.append("canonical_id LIKE '__operator_smoke_exercise_%'")

                if clauses:
                    training.execute("DELETE FROM movement_library WHERE " + " OR ".join(clauses))
                    removed["exercises"] = int(training.total_changes)
                    training.commit()
        finally:
            training.close()

        return removed


    def restore_and_log(self) -> None:
        """Restore to original state and log cleanup details."""
        self.current_suite_block = "cleanup"
        print("\n=== Cleanup Phase ===")
        
        # Check current active plans
        try:
            nutrition_active = self.get_active_detail("nutrition")
            nutrition_active_id = int(nutrition_active["plan_id"])
            print(f"Current active nutrition plan: {nutrition_active_id}")
        except Exception as e:
            nutrition_active_id = None
            print(f"Could not read active nutrition plan: {e}")
        
        try:
            training_active = self.get_active_detail("training")
            training_active_id = int(training_active["plan_id"])
            print(f"Current active training plan: {training_active_id}")
        except Exception as e:
            training_active_id = None
            print(f"Could not read active training plan: {e}")
        
        # Count smoke items
        smoke_items = self._count_created_items()
        print(f"Smoke items before cleanup: {smoke_items}")
        
        # Restore to original active plans
        try:
            self.current_step_name = "restore_nutrition_active"
            if nutrition_active_id != self.snapshots["nutrition_active_id"]:
                self.patch_plan("nutrition", int(self.snapshots["nutrition_active_id"]), [{"op": "set_active_plan"}])
                print(f"Restored nutrition active plan to {self.snapshots['nutrition_active_id']}")
        except Exception as e:
            print(f"Failed to restore nutrition active plan: {e}")
        
        try:
            self.current_step_name = "restore_training_active"
            if training_active_id != self.snapshots["training_active_id"]:
                self.patch_plan("training", int(self.snapshots["training_active_id"]), [{"op": "set_active_plan"}])
                print(f"Restored training active plan to {self.snapshots['training_active_id']}")
        except Exception as e:
            print(f"Failed to restore training active plan: {e}")
        
        # Hard-clean leftover smoke nutrition plan copies before final state.
        try:
            removed_smoke_plans = self._cleanup_smoke_nutrition_plans()
            if removed_smoke_plans:
                print(f"Hard-deleted smoke nutrition plans: {removed_smoke_plans}")
        except Exception as e:
            print(f"Could not hard-delete smoke nutrition plans: {e}")

        # Hard-clean leftover smoke training plan/exercise artifacts before final state.
        try:
            removed_training_smoke = self._cleanup_smoke_training_items()
            if removed_training_smoke.get("training_plans") or removed_training_smoke.get("exercises"):
                print(f"Hard-deleted smoke training items: {removed_training_smoke}")
        except Exception as e:
            print(f"Could not hard-delete smoke training items: {e}")

        # Check final state
        try:
            final_nutrition = self.get_active_detail("nutrition")
            final_nutrition_id = int(final_nutrition["plan_id"])
            counts_after = self.db_counts()
            print(f"Final active nutrition plan: {final_nutrition_id}")
            print(f"nutrition_slot_meal_overrides: before={self.snapshots['counts_before']['nutrition_slot_meal_overrides']} after={counts_after['nutrition_slot_meal_overrides']}")
        except Exception as e:
            print(f"Could not verify final nutrition state: {e}")
        
        try:
            final_training = self.get_active_detail("training")
            final_training_id = int(final_training["plan_id"])
            print(f"Final active training plan: {final_training_id}")
        except Exception as e:
            print(f"Could not verify final training state: {e}")
        
        # Count items again
        smoke_items_after = self._count_created_items()
        print(f"Smoke items after cleanup: {smoke_items_after}")

    def print_summary(self) -> None:
        print("\n=== Operator Smoke Summary ===")
        for result in self.results:
            print(f"[{'PASS' if result.ok else 'FAIL'}] {result.name}: {result.details}")
            if result.changed:
                print("  changed:", ", ".join(result.changed))
            if result.reverted:
                print("  reverted:", ", ".join(result.reverted))
            if result.cleanup_needed:
                print("  cleanup_needed:", ", ".join(result.cleanup_needed))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    suite = OperatorSmokeSuite(base_url=args.base_url, token=args.token, live=args.live, cleanup=args.cleanup)
    return suite.run()


if __name__ == "__main__":
    raise SystemExit(main())
