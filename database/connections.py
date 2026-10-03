# database/connections.py

import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.abspath(os.getenv("LIVA_DATABASE_DIR") or os.path.join(BASE_DIR, "database"))
os.makedirs(DB_DIR, exist_ok=True)

NUTRITION_DB = os.path.join(DB_DIR, "ernaehrung.sqlite3")
TRAINING_DB  = os.path.join(DB_DIR, "training.sqlite3")
RUNS_DB      = os.path.join(DB_DIR, "runs.sqlite3")
PLANS_DB     = os.path.join(DB_DIR, "plans.sqlite3")
HRV_DB       = os.path.join(DB_DIR, "hrv.sqlite3")
AUTH_DB      = os.path.join(DB_DIR, "auth.sqlite3")
CORE_DB      = os.path.join(DB_DIR, "core.sqlite3")
POLAR_DB     = os.path.join(DB_DIR, "polar.sqlite3")
ACTIVITY_DB  = os.path.join(DB_DIR, "digital_activity.sqlite3")
CONTROL_CENTER_DB = os.path.join(DB_DIR, "control_center.sqlite3")


def get_nutrition_db():
    conn = sqlite3.connect(
        NUTRITION_DB,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_training_db():
    conn = sqlite3.connect(
        TRAINING_DB,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_runs_db():
    conn = sqlite3.connect(
        RUNS_DB,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn

def get_plans_db():
    conn = sqlite3.connect(
        PLANS_DB,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def get_hrv_db():
    conn = sqlite3.connect(
        f"file:{HRV_DB}?mode=ro",
        timeout=30,
        check_same_thread=False,
        uri=True
    )
    conn.row_factory = sqlite3.Row
    return conn


def get_auth_db():
    conn = sqlite3.connect(
        AUTH_DB,
        timeout=1.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=1000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_core_db():
    conn = sqlite3.connect(
        CORE_DB,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_polar_db():
    conn = sqlite3.connect(
        POLAR_DB,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_activity_db():
    path = os.getenv("LIVA_ACTIVITY_DB") or ACTIVITY_DB
    conn = sqlite3.connect(
        path,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

__all__ = [
    "get_nutrition_db",
    "get_training_db",
    "get_runs_db",
    "get_plans_db",
    "get_hrv_db",
    "get_auth_db",
    "get_core_db",
    "get_polar_db",
    "get_activity_db",
]
