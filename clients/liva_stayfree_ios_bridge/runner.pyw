from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

CLIENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CLIENT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clients.liva_stayfree_ios_bridge.config import BridgeConfig, default_config_path  # noqa: E402
from clients.liva_stayfree_ios_bridge.scheduled import run_scheduled  # noqa: E402


def run_silently() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, default=default_config_path())
    args = parser.parse_args()
    config = BridgeConfig.load(args.config)
    log_path = default_config_path().parent / "bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > 1_000_000:
        rotated = log_path.with_suffix(".log.1")
        rotated.unlink(missing_ok=True)
        log_path.replace(rotated)
    with log_path.open("a", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        return run_scheduled(config)


if __name__ == "__main__":
    raise SystemExit(run_silently())
