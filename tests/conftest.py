from __future__ import annotations

import os
import sys

import pytest

from database import connections


_DB_PATH_NAMES = (
    "NUTRITION_DB",
    "TRAINING_DB",
    "RUNS_DB",
    "PLANS_DB",
    "HRV_DB",
    "AUTH_DB",
    "CORE_DB",
    "POLAR_DB",
)
_ORIGINAL_DB_PATHS = {name: getattr(connections, name) for name in _DB_PATH_NAMES}
_ORIGINAL_ENVIRONMENT = dict(os.environ)


def _restore_process_configuration(environment: dict[str, str]) -> None:
    for name, value in _ORIGINAL_DB_PATHS.items():
        setattr(connections, name, value)

    for name in tuple(os.environ):
        if name not in environment:
            os.environ.pop(name, None)
    for name, value in environment.items():
        os.environ[name] = value


def _clear_project_runtime_caches() -> None:
    """Reset process-global caches that must not bind one test's temp DB."""

    for module in tuple(sys.modules.values()):
        module_file = str(getattr(module, "__file__", "") or "")
        if not module_file.startswith("/opt/liva/"):
            continue
        namespace = vars(module)
        for name, value in tuple(namespace.items()):
            if name in {"_SCHEMA_READY_FOR", "_SCHEMA_OK_FOR"} and isinstance(value, (set, dict)):
                value.clear()
            elif name.endswith("_CACHE") and isinstance(value, dict):
                value.clear()
            elif name == "_SCHEMA_READY_FOR_DB":
                setattr(module, name, None)
            elif name == "CORE_SCHEMA_READY":
                setattr(module, name, False)
            value_type = type(value)
            if value_type.__module__ == "functools" and value_type.__name__ == "_lru_cache_wrapper":
                value.cache_clear()


@pytest.fixture(autouse=True)
def isolate_process_configuration():
    """Prevent one test's DB paths or environment from leaking into another."""

    pytest_marker = os.environ.get("PYTEST_CURRENT_TEST")
    _restore_process_configuration(_ORIGINAL_ENVIRONMENT)
    if pytest_marker:
        os.environ["PYTEST_CURRENT_TEST"] = pytest_marker
    _clear_project_runtime_caches()
    for name, value in _ORIGINAL_DB_PATHS.items():
        setattr(connections, name, value)
    # Unit tests must not silently read a real Google Calendar. Individual
    # integration tests can still opt in explicitly with monkeypatch.setenv().
    os.environ.setdefault("LIVA_ENABLE_GOOGLE_CALENDAR_READ", "0")
    # Most legacy Flask route tests exercise backend payloads rather than auth.
    # Give loopback an explicit test-only trust boundary; access-control tests
    # override this value when they need to model a public client.
    os.environ.setdefault("LIVA_IP_WHITELIST", "127.0.0.1,::1")
    yield
    _clear_project_runtime_caches()
    pytest_marker = os.environ.get("PYTEST_CURRENT_TEST")
    _restore_process_configuration(_ORIGINAL_ENVIRONMENT)
    if pytest_marker:
        os.environ["PYTEST_CURRENT_TEST"] = pytest_marker
