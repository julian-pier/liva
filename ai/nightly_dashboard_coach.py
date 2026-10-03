from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.parse_args(argv)
    print("AI_COACH: disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
