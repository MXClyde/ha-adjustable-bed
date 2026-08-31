"""Mutation tests for the Phase 4 v2 filesystem-integrity validator."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import tools.phase4_v2.validator.binding as validator_binding
import tools.phase4_v2.validator.bundle as validator_bundle
from tools.phase4_v2.ir import SCHEMA_REVISION, bind_validator_receipt, schema_document
from tools.phase4_v2.validator import (
    CONTRACT_REVISION,
    DependencyPins,
    load_json_strict,
    validate_report_bundle,
)


def _write_manifest(report: Path, members: dict[str, bytes]) -> None:
    lines = [
        f"{hashlib.sha256(data).hexdigest()}  {path}\n" for path, data in sorted(members.items())
    ]
    (report / "REPORT.SHA256").write_text("".join(lines), encoding="utf-8")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _empty_current_ir() -> dict[str, object]:
    return {
        "actions": {},
        "command_bindings": {},
        "evidence_anchors": {},
        "evidence_bindings": {},
        "evidence_files": {},
        "expected_action_rules": {},
        "protocols": {},
        "schema_revision": SCHEMA_REVISION,
        "source_packages": {},
        "source_sets": {},
        "variant_spaces": {},
    }


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


def _write_contract(
    report: Path,
    members: dict[str, bytes],
    contract: dict[str, object],
) -> None:
    encoded = (json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (report / "validation-input.json").write_bytes(encoded)
    members["validation-input.json"] = encoded
    _write_manifest(report, members)


def _bound_bundle(
    tmp_path: Path,
) -> tuple[Path, dict[str, bytes], DependencyPins, dict[str, object]]:
    report, members = _valid_bundle(tmp_path)
    evidence = f"{SCHEMA_REVISION}\n{SCHEMA_REVISION}\n".encode()
    inputs = {
        "inputs/corpus.json": b'{"kind":"synthetic-corpus"}\n',
        "inputs/ir.json": _json_bytes(_empty_current_ir()),
        "inputs/preflight.json": b'{"kind":"synthetic-preflight"}\n',
        "inputs/schema.json": _json_bytes(schema_document()),
        "evidence/source.txt": evidence,
    }
    for relative, data in inputs.items():
        destination = report / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        members[relative] = data
    digests = {path: hashlib.sha256(data).hexdigest() for path, data in inputs.items()}
    pins = DependencyPins(
        preflight_sha256=digests["inputs/preflight.json"],
        ir_sha256=digests["inputs/ir.json"],
        schema_sha256=digests["inputs/schema.json"],
        corpus_sha256=digests["inputs/corpus.json"],
    )
    contract: dict[str, object] = {
        "contract_revision": CONTRACT_REVISION,
        "dependencies": {
            name: {
                "member": f"inputs/{name}.json",
                "sha256": digest,
            }
            for name, digest in pins.as_pairs()
        },
        "evidence_members": [
            {
                "member": "evidence/source.txt",
                "owner": pins.preflight_sha256,
                "sha256": digests["evidence/source.txt"],
            }
        ],
        "anchors": [
            {
                "id": "label",
                "owner": pins.preflight_sha256,
                "member": "evidence/source.txt",
                "start_byte": 0,
                "end_byte": len(SCHEMA_REVISION),
                "ir_pointer": "/schema_revision",
                "representation": "utf8",
            },
            {
                "id": "value",
                "owner": pins.preflight_sha256,
                "member": "evidence/source.txt",
                "start_byte": len(SCHEMA_REVISION) + 1,
                "end_byte": 2 * len(SCHEMA_REVISION) + 1,
                "ir_pointer": "/schema_revision",
                "representation": "utf8",
            },
        ],
    }
    _write_contract(report, members, contract)
    return report, members, pins, contract


def _codes(report: Path) -> tuple[str, ...]:
    return tuple(
        item.code for item in validate_report_bundle(report, allow_unbound=True).diagnostics
    )


def _validate_unbound(report: Path) -> validator_bundle.ValidationReceipt:
    return validate_report_bundle(report, allow_unbound=True)


def test_valid_bundle_has_stable_receipt_and_is_not_mutated(tmp_path: Path) -> None:
    report, _ = _valid_bundle(tmp_path)
    before = validator_bundle.capture_tree_snapshot(report)

    first = _validate_unbound(report)
    second = _validate_unbound(report)

    assert first.accepted is True
    assert first.source_unchanged is True
    assert first.to_json() == second.to_json()
    assert validator_bundle.capture_tree_snapshot(report) == before
    assert json.loads(first.to_json())["diagnostics"] == []


def test_pinned_dependencies_and_evidence_anchors_are_reproduced(tmp_path: Path) -> None:
    report, _, pins, _ = _bound_bundle(tmp_path)

    first = validate_report_bundle(report, expected_dependencies=pins)
    second = validate_report_bundle(report, expected_dependencies=pins)

    assert first.accepted is True
    assert first.dependency_digests == pins.as_pairs()
    assert first.evidence_anchors_checked == 2
    assert first.validation_profile == "BOUND_V2"
    assert first.contract_revision == CONTRACT_REVISION
    assert [item.to_dict() for item in first.validated_evidence_members] == [
        {
            "member": "evidence/source.txt",
            "owner": pins.preflight_sha256,
            "sha256": hashlib.sha256(
                f"{SCHEMA_REVISION}\n{SCHEMA_REVISION}\n".encode()
            ).hexdigest(),
        }
    ]
    assert [item.id for item in first.validated_evidence_anchors] == ["label", "value"]
    assert (
        first.validation_receipt_sha256
        == hashlib.sha256(
            json.dumps(first.identity_payload(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert first.to_json() == second.to_json()


def test_canonical_receipt_binds_through_current_ir_boundary(tmp_path: Path) -> None:
    report, _, pins, _ = _bound_bundle(tmp_path)
    receipt = validate_report_bundle(report, expected_dependencies=pins)
    assert receipt.validation_receipt_sha256 is not None

    bound = bind_validator_receipt(
        receipt.to_json(),
        trusted_validator_revision=receipt.validator_revision,
        trusted_contract_revision=CONTRACT_REVISION,
        trusted_dependency_digests=dict(pins.as_pairs()),
        trusted_receipt_sha256=receipt.validation_receipt_sha256,
    )

    assert bound.validation_receipt_sha256 == receipt.validation_receipt_sha256
    assert [item.member for item in bound.validated_evidence_members] == ["evidence/source.txt"]
    assert [item.id for item in bound.validated_evidence_anchors] == ["label", "value"]


def test_unicode_receipt_uses_same_canonical_identity_as_ir(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    anchors = contract["anchors"]
    assert isinstance(anchors, list)
    anchor = anchors[0]
    assert isinstance(anchor, dict)
    anchor["id"] = "læbel"
    _write_contract(report, members, contract)
    receipt = validate_report_bundle(report, expected_dependencies=pins)
    assert receipt.validation_receipt_sha256 is not None

    bound = bind_validator_receipt(
        receipt.to_json(),
        trusted_validator_revision=receipt.validator_revision,
        trusted_contract_revision=CONTRACT_REVISION,
        trusted_dependency_digests=dict(pins.as_pairs()),
        trusted_receipt_sha256=receipt.validation_receipt_sha256,
    )

    assert [item.id for item in bound.validated_evidence_anchors] == ["læbel", "value"]


def test_pinned_ir_must_parse_with_current_ir_parser(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    invalid_ir = _empty_current_ir()
    invalid_ir["schema_revision"] = "obsolete"
    encoded = _json_bytes(invalid_ir)
    (report / "inputs" / "ir.json").write_bytes(encoded)
    members["inputs/ir.json"] = encoded
    digest = hashlib.sha256(encoded).hexdigest()
    dependencies = contract["dependencies"]
    assert isinstance(dependencies, dict)
    ir_dependency = dependencies["ir"]
    assert isinstance(ir_dependency, dict)
    ir_dependency["sha256"] = digest
    _write_contract(report, members, contract)

    receipt = validate_report_bundle(report, expected_dependencies=replace(pins, ir_sha256=digest))

    assert "PINNED_IR_INVALID" in {item.code for item in receipt.diagnostics}


def test_pinned_schema_must_equal_current_structure(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    stale_schema = schema_document()
    stale_schema["title"] = "mutated"
    encoded = _json_bytes(stale_schema)
    (report / "inputs" / "schema.json").write_bytes(encoded)
    members["inputs/schema.json"] = encoded
    digest = hashlib.sha256(encoded).hexdigest()
    dependencies = contract["dependencies"]
    assert isinstance(dependencies, dict)
    schema_dependency = dependencies["schema"]
    assert isinstance(schema_dependency, dict)
    schema_dependency["sha256"] = digest
    _write_contract(report, members, contract)

    receipt = validate_report_bundle(
        report, expected_dependencies=replace(pins, schema_sha256=digest)
    )

    assert "PINNED_SCHEMA_STRUCTURE_MISMATCH" in {item.code for item in receipt.diagnostics}


def test_pinned_schema_revision_is_checked_independently(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    stale_schema = schema_document()
    properties = stale_schema["properties"]
    assert isinstance(properties, dict)
    properties["schema_revision"] = {"const": "obsolete"}
    encoded = _json_bytes(stale_schema)
    (report / "inputs" / "schema.json").write_bytes(encoded)
    members["inputs/schema.json"] = encoded
    digest = hashlib.sha256(encoded).hexdigest()
    dependencies = contract["dependencies"]
    assert isinstance(dependencies, dict)
    schema_dependency = dependencies["schema"]
    assert isinstance(schema_dependency, dict)
    schema_dependency["sha256"] = digest
    _write_contract(report, members, contract)

    receipt = validate_report_bundle(
        report, expected_dependencies=replace(pins, schema_sha256=digest)
    )

    assert "PINNED_SCHEMA_REVISION_MISMATCH" in {item.code for item in receipt.diagnostics}


def test_anchor_count_limit_fails_before_range_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report, _, pins, _ = _bound_bundle(tmp_path)
    monkeypatch.setattr(validator_binding, "_MAX_ANCHOR_COUNT", 1)

    def unexpected_read(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("range reader must not run after the count gate")

    monkeypatch.setattr(validator_bundle, "_read_member_range", unexpected_read)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_ANCHOR_LIMIT_EXCEEDED" in {item.code for item in receipt.diagnostics}


def test_anchor_byte_budget_fails_before_range_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report, _, pins, _ = _bound_bundle(tmp_path)
    monkeypatch.setattr(validator_binding, "_MAX_ANCHOR_BYTES", 1)

    def unexpected_read(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("range reader must not run after the byte gate")

    monkeypatch.setattr(validator_bundle, "_read_member_range", unexpected_read)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_BYTE_BUDGET_EXCEEDED" in {item.code for item in receipt.diagnostics}


def test_malformed_anchor_cannot_bypass_cumulative_byte_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    anchors = contract["anchors"]
    assert isinstance(anchors, list)
    anchors[0] = {}
    _write_contract(report, members, contract)
    monkeypatch.setattr(validator_binding, "_MAX_ANCHOR_BYTES", 1)

    def unexpected_read(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("malformed entries must not disable the byte gate")

    monkeypatch.setattr(validator_bundle, "_read_member_range", unexpected_read)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_BYTE_BUDGET_EXCEEDED" in {item.code for item in receipt.diagnostics}


def test_anchor_reader_reads_only_exact_descriptor_ranges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report, _, pins, _ = _bound_bundle(tmp_path)
    original_pread = os.pread
    calls: list[tuple[int, int]] = []

    def observed_pread(fd: int, length: int, offset: int) -> bytes:
        calls.append((length, offset))
        return original_pread(fd, length, offset)

    monkeypatch.setattr(os, "pread", observed_pread)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert receipt.accepted is True
    assert calls == [
        (len(SCHEMA_REVISION), 0),
        (len(SCHEMA_REVISION), len(SCHEMA_REVISION) + 1),
    ]


@pytest.mark.parametrize(
    "field",
    ["preflight_sha256", "ir_sha256", "schema_sha256", "corpus_sha256"],
)
def test_each_dependency_pin_is_fail_closed(tmp_path: Path, field: str) -> None:
    report, _, pins, _ = _bound_bundle(tmp_path)
    wrong = replace(pins, **{field: "f" * 64})

    receipt = validate_report_bundle(report, expected_dependencies=wrong)

    assert receipt.accepted is False
    assert "DEPENDENCY_PIN_MISMATCH" in tuple(item.code for item in receipt.diagnostics)


def test_missing_evidence_member_fails_existence_gate(tmp_path: Path) -> None:
    report, _, pins, _ = _bound_bundle(tmp_path)
    (report / "evidence" / "source.txt").unlink()

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_MEMBER_MISSING" in tuple(item.code for item in receipt.diagnostics)


def test_anchor_owner_must_match_declared_member_owner(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    anchors = contract["anchors"]
    assert isinstance(anchors, list)
    anchor = anchors[0]
    assert isinstance(anchor, dict)
    anchor["owner"] = "f" * 64
    _write_contract(report, members, contract)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_ANCHOR_OWNER_MISMATCH" in tuple(item.code for item in receipt.diagnostics)


def test_evidence_owner_is_pinned_to_package_preflight(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    evidence_members = contract["evidence_members"]
    anchors = contract["anchors"]
    assert isinstance(evidence_members, list)
    assert isinstance(anchors, list)
    evidence_member = evidence_members[0]
    assert isinstance(evidence_member, dict)
    evidence_member["owner"] = "f" * 64
    for anchor in anchors:
        assert isinstance(anchor, dict)
        anchor["owner"] = "f" * 64
    _write_contract(report, members, contract)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_OWNER_PIN_MISMATCH" in tuple(item.code for item in receipt.diagnostics)


def test_anchor_range_must_be_inside_owned_member(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    anchors = contract["anchors"]
    assert isinstance(anchors, list)
    anchor = anchors[0]
    assert isinstance(anchor, dict)
    anchor["end_byte"] = 10_000
    _write_contract(report, members, contract)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_RANGE_OUT_OF_BOUNDS" in tuple(item.code for item in receipt.diagnostics)


def test_anchor_value_must_reproduce_from_exact_bytes(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    anchors = contract["anchors"]
    assert isinstance(anchors, list)
    anchor = anchors[1]
    assert isinstance(anchor, dict)
    anchor["representation"] = "hex"
    _write_contract(report, members, contract)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_VALUE_MISMATCH" in tuple(item.code for item in receipt.diagnostics)


def test_anchor_attestation_fields_are_bounded_for_external_binding(tmp_path: Path) -> None:
    report, members, pins, contract = _bound_bundle(tmp_path)
    anchors = contract["anchors"]
    assert isinstance(anchors, list)
    anchor = anchors[0]
    assert isinstance(anchor, dict)
    anchor["id"] = "a" * 257
    _write_contract(report, members, contract)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert "EVIDENCE_ANCHOR_INVALID" in {item.code for item in receipt.diagnostics}


def test_bound_contract_without_trusted_pins_is_rejected(tmp_path: Path) -> None:
    report, _, _, _ = _bound_bundle(tmp_path)

    receipt = validate_report_bundle(report)

    assert "DEPENDENCY_PINS_REQUIRED" in tuple(item.code for item in receipt.diagnostics)


def test_malformed_bound_contract_cannot_bypass_binding_validation(tmp_path: Path) -> None:
    report, members, pins, _ = _bound_bundle(tmp_path)
    malformed = b'{"dependencies":{},"dependencies":{}}\n'
    (report / "validation-input.json").write_bytes(malformed)
    members["validation-input.json"] = malformed
    _write_manifest(report, members)

    receipt = validate_report_bundle(report, expected_dependencies=pins)

    assert {item.code for item in receipt.diagnostics} >= {
        "JSON_NOT_STRICT",
        "VALIDATION_INPUT_INVALID",
    }


def test_default_validation_is_fail_closed_without_binding_contract(tmp_path: Path) -> None:
    report, _ = _valid_bundle(tmp_path)

    receipt = validate_report_bundle(report)

    assert receipt.accepted is False
    assert receipt.validation_profile == "FILESYSTEM_ONLY"
    assert tuple(item.code for item in receipt.diagnostics) == ("DEPENDENCY_PINS_REQUIRED",)


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

    receipt = _validate_unbound(report)

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

    receipt = _validate_unbound(report)

    assert _codes(report) == ("JSON_NOT_STRICT",)
    assert receipt.diagnostics[0].to_dict()["context"] == {
        "key": "status",
        "reason": "duplicate_key",
    }


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity", b"1e9999"])
def test_non_finite_json_number_is_rejected(constant: bytes) -> None:
    with pytest.raises(validator_bundle.StrictJsonError, match="non_finite_number"):
        load_json_strict(b'{"value":' + constant + b"}")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b'{"value":' + b"9" * 5_000 + b"}", "invalid_json"),
        (b'{"value":"\\ud800"}', "invalid_unicode"),
    ],
)
def test_hostile_json_values_are_deterministically_rejected(payload: bytes, reason: str) -> None:
    with pytest.raises(validator_bundle.StrictJsonError, match=reason):
        load_json_strict(payload)


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

    receipt = _validate_unbound(report)

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

    assert _validate_unbound(report).accepted is True
    assert marker.exists() is False


def test_member_read_is_bound_to_initial_snapshot(tmp_path: Path) -> None:
    report, members = _valid_bundle(tmp_path)
    snapshot = validator_bundle.capture_tree_snapshot(report)
    snapshot_nodes = {node.path: node for node in snapshot.nodes}
    replacement = b'{"status":"different"}\n'
    (report / "analysis.json").write_bytes(replacement)
    members["analysis.json"] = replacement
    _write_manifest(report, members)

    with pytest.raises(OSError, match="changed since initial snapshot"):
        validator_bundle._read_member(
            report,
            validator_bundle.PurePosixPath("analysis.json"),
            snapshot_nodes,
            max_bytes=1024,
        )


def test_range_read_is_bound_to_initial_snapshot(tmp_path: Path) -> None:
    report, _, _, _ = _bound_bundle(tmp_path)
    snapshot = validator_bundle.capture_tree_snapshot(report)
    snapshot_nodes = {node.path: node for node in snapshot.nodes}
    source = report / "evidence" / "source.txt"
    source.write_bytes(b"x" * source.stat().st_size)

    with pytest.raises(OSError, match="changed since initial snapshot"):
        validator_bundle._read_member_range(
            report,
            validator_bundle.PurePosixPath("evidence/source.txt"),
            snapshot_nodes,
            0,
            1,
        )


def test_non_utf8_member_name_is_rejected_without_receipt_crash(tmp_path: Path) -> None:
    report, _ = _valid_bundle(tmp_path)
    raw_path = os.fsencode(report) + b"/bad-\xff.txt"
    descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, b"opaque")
    finally:
        os.close(descriptor)

    receipt = _validate_unbound(report)

    assert receipt.accepted is False
    assert "MEMBER_UNDECLARED" in tuple(item.code for item in receipt.diagnostics)
    assert receipt.bundle_sha256 is not None


def test_validation_reads_do_not_change_access_time(tmp_path: Path) -> None:
    report, _ = _valid_bundle(tmp_path)
    analysis = report / "analysis.json"
    node = analysis.stat()
    old_atime = 1_600_000_000_000_000_000
    os.utime(analysis, ns=(old_atime, node.st_mtime_ns))

    receipt = _validate_unbound(report)

    assert receipt.accepted is True
    assert analysis.stat().st_atime_ns == old_atime


def test_hardlinked_report_member_is_rejected(tmp_path: Path) -> None:
    report, members = _valid_bundle(tmp_path)
    os.link(report / "analysis.json", report / "analysis-copy.json")
    members["analysis-copy.json"] = members["analysis.json"]
    _write_manifest(report, members)

    receipt = _validate_unbound(report)

    hardlinks = [item.path for item in receipt.diagnostics if item.code == "HARDLINK_FORBIDDEN"]
    assert hardlinks == ["analysis-copy.json", "analysis.json"]
