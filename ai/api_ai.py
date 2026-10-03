from __future__ import annotations

from flask import Blueprint

from ai.api_ai_common import attach_error_handlers
from ai.api_ai_core import register as register_core
from ai.api_ai_hrv import register as register_hrv
from ai.api_ai_gym import register as register_gym
from ai.api_ai_runs import register as register_runs
from ai.api_ai_plans import register as register_plans
from ai.api_ai_nutrition import register as register_nutrition


ai_api = Blueprint("ai_api", __name__, url_prefix="/api/ai")
attach_error_handlers(ai_api)

register_core(ai_api)
register_hrv(ai_api)
register_gym(ai_api)
register_runs(ai_api)
register_plans(ai_api)
register_nutrition(ai_api)
