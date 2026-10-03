from __future__ import annotations

import logging

from .agent import run
from .config import AgentConfig


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(AgentConfig.from_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
