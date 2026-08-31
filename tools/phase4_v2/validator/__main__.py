"""Command-line entry point for the Phase 4 bundle validator."""

from __future__ import annotations

import argparse
from pathlib import Path

from .bundle import validate_report_bundle


def main() -> int:
    """Validate one report directory and print its canonical receipt."""
    parser = argparse.ArgumentParser(
        description="Validate a frozen Phase 4 report bundle without modifying it."
    )
    parser.add_argument("report_root", type=Path)
    args = parser.parse_args()

    receipt = validate_report_bundle(args.report_root)
    print(receipt.to_json())
    return 0 if receipt.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
