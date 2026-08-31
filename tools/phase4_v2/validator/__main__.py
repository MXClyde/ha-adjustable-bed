"""Command-line entry point for the Phase 4 bundle validator."""

from __future__ import annotations

import argparse
from pathlib import Path

from .binding import DependencyPins
from .bundle import validate_report_bundle


def main() -> int:
    """Validate one report directory and print its canonical receipt."""
    parser = argparse.ArgumentParser(
        description="Validate a frozen Phase 4 report bundle without modifying it."
    )
    parser.add_argument("report_root", type=Path)
    parser.add_argument("--preflight-sha256")
    parser.add_argument("--ir-sha256")
    parser.add_argument("--schema-sha256")
    parser.add_argument("--corpus-sha256")
    args = parser.parse_args()

    supplied = (
        args.preflight_sha256,
        args.ir_sha256,
        args.schema_sha256,
        args.corpus_sha256,
    )
    if any(supplied) and not all(supplied):
        parser.error("all four dependency digests must be supplied together")
    pins = (
        DependencyPins(
            preflight_sha256=args.preflight_sha256,
            ir_sha256=args.ir_sha256,
            schema_sha256=args.schema_sha256,
            corpus_sha256=args.corpus_sha256,
        )
        if all(supplied)
        else None
    )
    receipt = validate_report_bundle(args.report_root, expected_dependencies=pins)
    print(receipt.to_json())
    return 0 if receipt.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
