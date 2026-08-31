"""Mutation tests for the Phase 4 v2 filesystem-integrity validator."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import tools.phase4_v2.validator.bundle as validator_bundle
from tools.phase4_v2.validator import load_json_strict, validate_report_bundle


def _write_manifest(report: Path, members: dict[str, bytes]) -> None:
    lines = [
        f"{hashlib.sha256(data).hexdigest()}  {path}\n" for path, data in sorted(members.items())
    ]
    (report / "REPORT.SHA256").write_text("".join(lines), encoding="utf-8")


def _valid_bundle(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    report = tmp_path / "report"
    scripts = report / "reproducers"
    scripts.mkdir(parents=True)
    members = {
        "ANALYSIS.md": b"Synthetic validation fixture.\n",
        "SEARCH_LOG.md": b"Synthetic validation fixture.\n",
        "analysis.json": b'{"schema_revision":"synthetic-v1","status":"COMPLETE"}\n',
        "reproducers/vector.py": b"# Stored evidence only. The validator never executes this.\n",
    }
    for relative, data in members.items():
        destination = report / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    _write_manifest(report, members)
    return report, members


def _codes(report: Path) -> tuple[str, ...]:
    return tuple(item.code for item in validate_report_bundle(report).diagnostics)


def test_valid_bundle_has_stable_receipt_and_is_not_mutated(tmp_path: Path) -> None:
    report, _ = _valid_bundle(tmp_path)
    before = validator_bundle.capture_tree_snapshot(report)

    first = validate_report_bundle(report)
    second = validate_report_bundle(report)

    assert first.accepted is True
    assert first.source_unchanged is True
    assert first.to_json() == second.to_json()
    assert validator_bundle.capture_tree_snapshot(report) == before
    assert json.loads(first.to_json())["diagnostics"] == []


def test_stale_member_hash_is_rejected(tmp_path: Path) -> None:
    report, _ = _valid_bundle(tmp_path)
    analysis = report / "analysis.json"
    analysis.write_bytes(b'{"schema_revision":"synthetic-v2","status":"COMPLETE"}\n')

    assert _codes(report) == ("MEMBER_DIGEST_MISMATCH",)


def test_extra_member_is_rejected(tmp_path: Path) -> None:
    report, _ = _valid_bundle(tmp_path)
    (report / "unhashed.txt").write_text("extra", encoding="utf-8")

    assert _codes(report) == ("MEMBER_UNDECLARED",)


def test_missing_member_is_rejected(tmp_path: Path) -> None:
    report, _ = _valid_bundle(tmp_path)
    (report / "ANALYSIS.md").unlink()

    assert _codes(report) == ("MEMBER_MISSING",)


def test_manifest_path_traversal_is_rejected_without_reading_target(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":true}', encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    (report / "REPORT.SHA256").write_text(f"{digest}  ../outside.json\n", encoding="utf-8")

    receipt = validate_report_bundle(report)

    assert tuple(item.code for item in receipt.diagnostics) == ("PATH_ESCAPE",)
    assert outside.read_text(encoding="utf-8") == '{"secret":true}'


def test_symlink_escape_is_rejected_without_following_it(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":true}', encoding="utf-8")
    os.symlink(outside, report / "analysis.json")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    (report / "REPORT.SHA256").write_text(f"{digest}  analysis.json\n", encoding="utf-8")

    assert _codes(report) == ("MEMBER_NOT_REGULAR", "SYMLINK_FORBIDDEN")
    assert outside.read_text(encoding="utf-8") == '{"secret":true}'


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    report, members = _valid_bundle(tmp_path)
    duplicate = b'{"status":"COMPLETE","status":"PARTIAL"}\n'
    (report / "analysis.json").write_bytes(duplicate)
    members["analysis.json"] = duplicate
    _write_manifest(report, members)

    receipt = validate_report_bundle(report)

    assert _codes(report) == ("JSON_NOT_STRICT",)
    assert receipt.diagnostics[0].to_dict()["context"] == {
        "key": "status",
        "reason": "duplicate_key",
    }


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity", b"1e9999"])
def test_non_finite_json_number_is_rejected(constant: bytes) -> None:
    with pytest.raises(validator_bundle.StrictJsonError, match="non_finite_number"):
        load_json_strict(b'{"value":' + constant + b"}")


def test_validator_detects_concurrent_source_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report, _ = _valid_bundle(tmp_path)
    analysis = report / "analysis.json"
    original_capture = validator_bundle.capture_tree_snapshot
    captures = 0

    def mutate_before_second_capture(root: Path) -> validator_bundle._TreeSnapshot:
        nonlocal captures
        captures += 1
        if captures == 2:
            analysis.write_bytes(b'{"status":"changed-after-validation"}\n')
        return original_capture(root)

    monkeypatch.setattr(validator_bundle, "capture_tree_snapshot", mutate_before_second_capture)

    receipt = validate_report_bundle(report)

    assert "SOURCE_TREE_MUTATED" in tuple(item.code for item in receipt.diagnostics)
    assert receipt.source_unchanged is False
    assert receipt.accepted is False


def test_validator_never_executes_report_scripts(tmp_path: Path) -> None:
    report, members = _valid_bundle(tmp_path)
    marker = tmp_path / "executed"
    script = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n".encode()
    (report / "reproducers" / "vector.py").write_bytes(script)
    members["reproducers/vector.py"] = script
    _write_manifest(report, members)

    assert validate_report_bundle(report).accepted is True
    assert marker.exists() is False
