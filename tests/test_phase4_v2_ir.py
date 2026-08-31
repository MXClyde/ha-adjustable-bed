"""Focused tests for the compact Phase 4 v2 protocol IR core."""

from __future__ import annotations

import copy
import json

import pytest

from tools.phase4_v2.ir import (
    SCHEMA_REVISION,
    IRValidationError,
    ProtocolIRDocument,
    dumps_ir,
    loads_ir,
    schema_document,
    semantic_fingerprint,
    validate_universe,
)


def _document() -> dict[str, object]:
    return {
        "schema_revision": SCHEMA_REVISION,
        "variant_spaces": {
            "variants": {
                "dimensions": {
                    "side": ["right", "left"],
                    "model": ["beta", "alpha"],
                },
                "constraints": [],
            }
        },
        "protocols": {"primary": {"variant_space": "variants"}},
        "actions": {
            "raise": {"summary": "Raise"},
            "lower": {"summary": "Lower"},
        },
        "expected_action_rules": {
            "expect_raise": {
                "protocol": "primary",
                "action": "raise",
                "when": {"op": "always"},
            },
            "expect_lower_beta": {
                "protocol": "primary",
                "action": "lower",
                "when": {
                    "op": "in",
                    "dimension": "model",
                    "values": ["beta"],
                },
            },
        },
        "command_bindings": {
            "bind_raise": {
                "protocol": "primary",
                "action": "raise",
                "when": {"op": "always"},
            },
            "bind_lower_beta": {
                "protocol": "primary",
                "action": "lower",
                "when": {
                    "op": "eq",
                    "dimension": "model",
                    "value": "beta",
                },
            },
        },
    }


def _load(data: dict[str, object]) -> ProtocolIRDocument:
    return loads_ir(json.dumps(data))


def test_canonical_round_trip_and_fingerprint_are_deterministic() -> None:
    first_data = _document()
    second_data = copy.deepcopy(first_data)
    second_data["variant_spaces"] = {
        "variants": {
            "constraints": [],
            "dimensions": {
                "model": ["alpha", "beta"],
                "side": ["left", "right"],
            },
        }
    }
    second_data["actions"] = {
        "lower": {"summary": "Lower"},
        "raise": {"summary": "Raise"},
    }

    first = _load(first_data)
    second = _load(second_data)
    canonical = dumps_ir(first)

    assert canonical == dumps_ir(second)
    assert dumps_ir(loads_ir(canonical)) == canonical
    assert semantic_fingerprint(first) == semantic_fingerprint(second)
    assert validate_universe(first).is_valid


def test_universe_reports_every_missing_action_variant() -> None:
    data = _document()
    bindings = data["command_bindings"]
    assert isinstance(bindings, dict)
    del bindings["bind_lower_beta"]

    result = validate_universe(_load(data))

    missing = [issue for issue in result.issues if issue.code == "missing_binding"]
    assert len(missing) == 2
    assert {dict(issue.key.profile)["side"] for issue in missing if issue.key} == {
        "left",
        "right",
    }


def test_universe_reports_bindings_outside_expected_applicability() -> None:
    data = _document()
    bindings = data["command_bindings"]
    assert isinstance(bindings, dict)
    lower = bindings["bind_lower_beta"]
    assert isinstance(lower, dict)
    lower["when"] = {"op": "always"}

    result = validate_universe(_load(data))

    extra = [issue for issue in result.issues if issue.code == "extra_binding"]
    assert len(extra) == 2
    assert {dict(issue.key.profile)["model"] for issue in extra if issue.key} == {"alpha"}


def test_universe_distinguishes_duplicate_binding_coverage() -> None:
    data = _document()
    bindings = data["command_bindings"]
    assert isinstance(bindings, dict)
    bindings["bind_raise_duplicate"] = copy.deepcopy(bindings["bind_raise"])

    result = validate_universe(_load(data))

    duplicates = [issue for issue in result.issues if issue.code == "duplicate_binding_coverage"]
    assert len(duplicates) == 1
    assert duplicates[0].binding_ids == ("bind_raise", "bind_raise_duplicate")


def test_universe_distinguishes_partially_overlapping_bindings() -> None:
    data = _document()
    bindings = data["command_bindings"]
    assert isinstance(bindings, dict)
    bindings["bind_raise_alpha"] = {
        "protocol": "primary",
        "action": "raise",
        "when": {"op": "eq", "dimension": "model", "value": "alpha"},
    }

    result = validate_universe(_load(data))

    overlaps = [issue for issue in result.issues if issue.code == "overlapping_binding_coverage"]
    assert len(overlaps) == 1
    assert overlaps[0].binding_ids == ("bind_raise", "bind_raise_alpha")


def test_loader_is_strict_and_rejects_duplicate_definition_ids() -> None:
    payload = json.dumps(_document())
    payload = payload.replace(
        '"raise": {"summary": "Raise"}',
        '"raise": {"summary": "Raise"}, "raise": {"summary": "Again"}',
    )

    with pytest.raises(IRValidationError) as caught:
        loads_ir(payload)

    assert caught.value.diagnostics[0].code == "duplicate_object_key"


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_loader_rejects_non_finite_json_numbers(number: str) -> None:
    payload = json.dumps(_document()).replace('"Raise"', number, 1)

    with pytest.raises(IRValidationError) as caught:
        loads_ir(payload)

    assert caught.value.diagnostics[0].code == "non_finite_number"


def test_invalid_selector_is_diagnostic_instead_of_crashing_profile_expansion() -> None:
    data = _document()
    spaces = data["variant_spaces"]
    assert isinstance(spaces, dict)
    variants = spaces["variants"]
    assert isinstance(variants, dict)
    variants["constraints"] = [{"op": "eq", "dimension": "missing", "value": "anything"}]

    with pytest.raises(IRValidationError) as caught:
        _load(data)

    assert caught.value.diagnostics[0].code == "unknown_selector_dimension"


def test_oversized_variant_space_is_rejected_before_expansion() -> None:
    data = _document()
    spaces = data["variant_spaces"]
    assert isinstance(spaces, dict)
    variants = spaces["variants"]
    assert isinstance(variants, dict)
    variants["dimensions"] = {f"d{index}": [False, True] for index in range(17)}

    with pytest.raises(IRValidationError) as caught:
        _load(data)

    assert caught.value.diagnostics[0].code == "variant_space_too_large"


def test_loader_rejects_unpaired_unicode_surrogates() -> None:
    data = _document()
    actions = data["actions"]
    assert isinstance(actions, dict)
    actions["raise"] = {"summary": "\ud800"}

    with pytest.raises(IRValidationError) as caught:
        _load(data)

    assert caught.value.diagnostics[0].code == "invalid_unicode"


def test_schema_document_is_pinned_strict_and_defensively_copied() -> None:
    first = schema_document()
    second = schema_document()

    assert first["additionalProperties"] is False
    assert first["properties"] != {}
    first["title"] = "changed"
    assert second["title"] == "Phase 4 protocol intermediate representation"
