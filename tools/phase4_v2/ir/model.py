"""Strict, protocol-neutral core model for the Phase 4 v2 protocol IR."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast

SCHEMA_REVISION = "phase4-protocol-ir-core-v0.1.0-2026-08-31"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_MAX_VARIANT_PROFILES = 100_000

type JsonScalar = str | int | bool
type Profile = tuple[tuple[str, JsonScalar], ...]


@dataclass(frozen=True, slots=True)
class IRDiagnostic:
    """A deterministic structural or semantic validation diagnostic."""

    code: str
    path: str
    message: str


class IRValidationError(ValueError):
    """Raised when an IR document is structurally or semantically invalid."""

    def __init__(self, diagnostics: Iterable[IRDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        detail = "; ".join(
            f"{item.code} at {item.path}: {item.message}" for item in self.diagnostics
        )
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class Predicate:
    """A closed predicate AST evaluated against one static variant profile."""

    op: str
    dimension: str | None = None
    value: JsonScalar | None = None
    values: tuple[JsonScalar, ...] = ()
    terms: tuple[Predicate, ...] = ()

    def matches(self, profile: Mapping[str, JsonScalar]) -> bool:
        """Return whether this predicate accepts a variant profile."""

        if self.op == "always":
            return True
        if self.op == "never":
            return False
        if self.op == "eq":
            assert self.dimension is not None
            return _scalars_equal(profile[self.dimension], self.value)
        if self.op == "in":
            assert self.dimension is not None
            actual = profile[self.dimension]
            return any(_scalars_equal(actual, value) for value in self.values)
        if self.op == "all":
            return all(term.matches(profile) for term in self.terms)
        if self.op == "any":
            return any(term.matches(profile) for term in self.terms)
        if self.op == "not":
            return not self.terms[0].matches(profile)
        raise AssertionError(f"unhandled predicate operation: {self.op}")

    def to_data(self) -> dict[str, object]:
        """Return the normalized JSON representation."""

        data: dict[str, object] = {"op": self.op}
        if self.op == "eq":
            data["dimension"] = self.dimension
            data["value"] = self.value
        elif self.op == "in":
            data["dimension"] = self.dimension
            data["values"] = list(self.values)
        elif self.op in {"all", "any"}:
            data["terms"] = [term.to_data() for term in self.terms]
        elif self.op == "not":
            data["term"] = self.terms[0].to_data()
        return data


@dataclass(frozen=True, slots=True)
class VariantSpace:
    """Finite static selector dimensions and constraints for one protocol."""

    dimensions: tuple[tuple[str, tuple[JsonScalar, ...]], ...]
    constraints: tuple[Predicate, ...]

    def profiles(self) -> tuple[Profile, ...]:
        """Enumerate valid profiles transiently without storing a Cartesian table."""

        names = tuple(name for name, _values in self.dimensions)
        domains = tuple(values for _name, values in self.dimensions)
        combinations = itertools.product(*domains) if domains else ((),)
        profiles: list[Profile] = []
        for combination in combinations:
            profile = tuple(zip(names, combination, strict=True))
            profile_map = dict(profile)
            if all(constraint.matches(profile_map) for constraint in self.constraints):
                profiles.append(profile)
        return tuple(profiles)

    def to_data(self) -> dict[str, object]:
        return {
            "dimensions": {name: list(values) for name, values in self.dimensions},
            "constraints": [constraint.to_data() for constraint in self.constraints],
        }


@dataclass(frozen=True, slots=True)
class ProtocolDefinition:
    variant_space: str

    def to_data(self) -> dict[str, object]:
        return {"variant_space": self.variant_space}


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    summary: str | None = None

    def to_data(self) -> dict[str, object]:
        return {} if self.summary is None else {"summary": self.summary}


@dataclass(frozen=True, slots=True)
class ExpectedActionRule:
    protocol: str
    action: str
    when: Predicate

    def to_data(self) -> dict[str, object]:
        return {
            "protocol": self.protocol,
            "action": self.action,
            "when": self.when.to_data(),
        }


@dataclass(frozen=True, slots=True)
class CommandBinding:
    protocol: str
    action: str
    when: Predicate

    def to_data(self) -> dict[str, object]:
        return {
            "protocol": self.protocol,
            "action": self.action,
            "when": self.when.to_data(),
        }


@dataclass(frozen=True, slots=True)
class ProtocolIRDocument:
    """The compact canonical subset implemented by the first v2 IR slice."""

    schema_revision: str
    variant_spaces: tuple[tuple[str, VariantSpace], ...]
    protocols: tuple[tuple[str, ProtocolDefinition], ...]
    actions: tuple[tuple[str, ActionDefinition], ...]
    expected_action_rules: tuple[tuple[str, ExpectedActionRule], ...]
    command_bindings: tuple[tuple[str, CommandBinding], ...]

    def to_data(self) -> dict[str, object]:
        """Return normalized JSON data with all definition maps ID-keyed."""

        return {
            "schema_revision": self.schema_revision,
            "variant_spaces": {
                identifier: definition.to_data() for identifier, definition in self.variant_spaces
            },
            "protocols": {
                identifier: definition.to_data() for identifier, definition in self.protocols
            },
            "actions": {
                identifier: definition.to_data() for identifier, definition in self.actions
            },
            "expected_action_rules": {
                identifier: definition.to_data()
                for identifier, definition in self.expected_action_rules
            },
            "command_bindings": {
                identifier: definition.to_data() for identifier, definition in self.command_bindings
            },
        }


@dataclass(frozen=True, slots=True)
class UniverseKey:
    protocol: str
    action: str
    profile: Profile


@dataclass(frozen=True, slots=True)
class UniverseIssue:
    code: str
    key: UniverseKey | None
    binding_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UniverseValidation:
    expected: frozenset[UniverseKey]
    actual: frozenset[UniverseKey]
    issues: tuple[UniverseIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


def loads_ir(payload: str | bytes) -> ProtocolIRDocument:
    """Load and strictly validate a canonical IR document."""

    try:
        raw = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
            parse_float=_parse_finite_float,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise IRValidationError((IRDiagnostic("invalid_json", "$", str(err)),)) from err
    except _DuplicateKeyError as err:
        raise IRValidationError(
            (
                IRDiagnostic(
                    "duplicate_object_key",
                    "$",
                    f"JSON object key {err.key!r} appears more than once",
                ),
            )
        ) from err
    except _NonFiniteNumberError as err:
        raise IRValidationError(
            (
                IRDiagnostic(
                    "non_finite_number",
                    "$",
                    f"JSON number {err.value!r} is not finite",
                ),
            )
        ) from err
    return parse_ir(raw)


def load_ir(path: Path) -> ProtocolIRDocument:
    """Load an IR document from disk without modifying it."""

    return loads_ir(path.read_bytes())


def parse_ir(raw: object) -> ProtocolIRDocument:
    """Parse already-decoded JSON-compatible data into the strict model."""

    root = _expect_object(raw, "$")
    _expect_keys(
        root,
        path="$",
        required={
            "schema_revision",
            "variant_spaces",
            "protocols",
            "actions",
            "expected_action_rules",
            "command_bindings",
        },
    )
    revision = _expect_string(root["schema_revision"], "$.schema_revision")
    if revision != SCHEMA_REVISION:
        _fail(
            "unsupported_schema_revision",
            "$.schema_revision",
            f"expected {SCHEMA_REVISION!r}, got {revision!r}",
        )

    variant_spaces = _parse_definition_map(
        root["variant_spaces"], "$.variant_spaces", _parse_variant_space
    )
    protocols = _parse_definition_map(root["protocols"], "$.protocols", _parse_protocol)
    actions = _parse_definition_map(root["actions"], "$.actions", _parse_action)
    expected_rules = _parse_definition_map(
        root["expected_action_rules"],
        "$.expected_action_rules",
        _parse_expected_rule,
    )
    bindings = _parse_definition_map(root["command_bindings"], "$.command_bindings", _parse_binding)

    document = ProtocolIRDocument(
        schema_revision=revision,
        variant_spaces=variant_spaces,
        protocols=protocols,
        actions=actions,
        expected_action_rules=expected_rules,
        command_bindings=bindings,
    )
    _validate_references_and_predicates(document)
    return document


def dumps_ir(document: ProtocolIRDocument) -> bytes:
    """Serialize an IR document to stable canonical UTF-8 JSON."""

    return _canonical_json(document.to_data()) + b"\n"


def semantic_fingerprint(document: ProtocolIRDocument) -> str:
    """Return a stable SHA-256 fingerprint of normalized semantic content."""

    return hashlib.sha256(_canonical_json(document.to_data())).hexdigest()


def validate_universe(document: ProtocolIRDocument) -> UniverseValidation:
    """Compare expected action coverage with the command-binding multiset."""

    spaces = dict(document.variant_spaces)
    profiles_by_protocol = {
        protocol_id: spaces[protocol.variant_space].profiles()
        for protocol_id, protocol in document.protocols
    }

    expected: set[UniverseKey] = set()
    for _rule_id, rule in document.expected_action_rules:
        for profile in profiles_by_protocol[rule.protocol]:
            if rule.when.matches(dict(profile)):
                expected.add(UniverseKey(rule.protocol, rule.action, profile))

    binding_coverage: dict[str, frozenset[UniverseKey]] = {}
    actual_sources: defaultdict[UniverseKey, list[str]] = defaultdict(list)
    for binding_id, binding in document.command_bindings:
        covered = frozenset(
            UniverseKey(binding.protocol, binding.action, profile)
            for profile in profiles_by_protocol[binding.protocol]
            if binding.when.matches(dict(profile))
        )
        binding_coverage[binding_id] = covered
        for key in covered:
            actual_sources[key].append(binding_id)

    actual = set(actual_sources)
    issues: list[UniverseIssue] = []
    for key in sorted(expected - actual, key=_universe_sort_key):
        issues.append(UniverseIssue("missing_binding", key))
    for key in sorted(actual - expected, key=_universe_sort_key):
        issues.append(UniverseIssue("extra_binding", key, tuple(sorted(actual_sources[key]))))

    binding_ids = sorted(binding_coverage)
    for left_index, left_id in enumerate(binding_ids):
        left = binding_coverage[left_id]
        if not left:
            issues.append(UniverseIssue("empty_binding", None, (left_id,)))
        for right_id in binding_ids[left_index + 1 :]:
            right = binding_coverage[right_id]
            overlap = left & right
            if not overlap:
                continue
            if left == right:
                issues.append(
                    UniverseIssue(
                        "duplicate_binding_coverage",
                        min(overlap, key=_universe_sort_key),
                        (left_id, right_id),
                    )
                )
            else:
                issues.append(
                    UniverseIssue(
                        "overlapping_binding_coverage",
                        min(overlap, key=_universe_sort_key),
                        (left_id, right_id),
                    )
                )

    issues.sort(
        key=lambda issue: (
            issue.code,
            _universe_sort_key(issue.key) if issue.key is not None else ("", "", b""),
            issue.binding_ids,
        )
    )
    return UniverseValidation(
        expected=frozenset(expected),
        actual=frozenset(actual),
        issues=tuple(issues),
    )


def _parse_variant_space(raw: object, path: str) -> VariantSpace:
    value = _expect_object(raw, path)
    _expect_keys(value, path=path, required={"dimensions", "constraints"})
    raw_dimensions = _expect_object(value["dimensions"], f"{path}.dimensions")
    dimensions: list[tuple[str, tuple[JsonScalar, ...]]] = []
    for name in sorted(raw_dimensions):
        _validate_id(name, f"{path}.dimensions")
        raw_values = _expect_array(raw_dimensions[name], f"{path}.dimensions.{name}")
        if not raw_values:
            _fail(
                "empty_dimension",
                f"{path}.dimensions.{name}",
                "a selector dimension must declare at least one value",
            )
        parsed_values = tuple(
            _expect_scalar(item, f"{path}.dimensions.{name}[{index}]")
            for index, item in enumerate(raw_values)
        )
        sorted_values = tuple(sorted(parsed_values, key=_scalar_sort_key))
        if len({_scalar_sort_key(item) for item in sorted_values}) != len(sorted_values):
            _fail(
                "duplicate_dimension_value",
                f"{path}.dimensions.{name}",
                "selector dimension values must be unique",
            )
        dimensions.append((name, sorted_values))

    raw_constraints = _expect_array(value["constraints"], f"{path}.constraints")
    constraints = tuple(
        sorted(
            (
                _parse_predicate(item, f"{path}.constraints[{index}]")
                for index, item in enumerate(raw_constraints)
            ),
            key=lambda predicate: _canonical_json(predicate.to_data()),
        )
    )
    return VariantSpace(tuple(dimensions), constraints)


def _parse_protocol(raw: object, path: str) -> ProtocolDefinition:
    value = _expect_object(raw, path)
    _expect_keys(value, path=path, required={"variant_space"})
    return ProtocolDefinition(
        variant_space=_expect_reference(value["variant_space"], f"{path}.variant_space")
    )


def _parse_action(raw: object, path: str) -> ActionDefinition:
    value = _expect_object(raw, path)
    _expect_keys(value, path=path, required=set(), optional={"summary"})
    summary = _expect_string(value["summary"], f"{path}.summary") if "summary" in value else None
    if summary == "":
        _fail("empty_string", f"{path}.summary", "summary must not be empty")
    return ActionDefinition(summary=summary)


def _parse_expected_rule(raw: object, path: str) -> ExpectedActionRule:
    value = _expect_object(raw, path)
    _expect_keys(value, path=path, required={"protocol", "action", "when"})
    return ExpectedActionRule(
        protocol=_expect_reference(value["protocol"], f"{path}.protocol"),
        action=_expect_reference(value["action"], f"{path}.action"),
        when=_parse_predicate(value["when"], f"{path}.when"),
    )


def _parse_binding(raw: object, path: str) -> CommandBinding:
    value = _expect_object(raw, path)
    _expect_keys(value, path=path, required={"protocol", "action", "when"})
    return CommandBinding(
        protocol=_expect_reference(value["protocol"], f"{path}.protocol"),
        action=_expect_reference(value["action"], f"{path}.action"),
        when=_parse_predicate(value["when"], f"{path}.when"),
    )


def _parse_predicate(raw: object, path: str) -> Predicate:
    value = _expect_object(raw, path)
    op = _expect_string(value.get("op"), f"{path}.op")
    if op in {"always", "never"}:
        _expect_keys(value, path=path, required={"op"})
        return Predicate(op)
    if op == "eq":
        _expect_keys(value, path=path, required={"op", "dimension", "value"})
        return Predicate(
            op,
            dimension=_expect_reference(value["dimension"], f"{path}.dimension"),
            value=_expect_scalar(value["value"], f"{path}.value"),
        )
    if op == "in":
        _expect_keys(value, path=path, required={"op", "dimension", "values"})
        raw_values = _expect_array(value["values"], f"{path}.values")
        if not raw_values:
            _fail("empty_predicate_values", f"{path}.values", "values must not be empty")
        values = tuple(
            sorted(
                (
                    _expect_scalar(item, f"{path}.values[{index}]")
                    for index, item in enumerate(raw_values)
                ),
                key=_scalar_sort_key,
            )
        )
        if len({_scalar_sort_key(item) for item in values}) != len(values):
            _fail(
                "duplicate_predicate_value",
                f"{path}.values",
                "predicate values must be unique",
            )
        return Predicate(
            op,
            dimension=_expect_reference(value["dimension"], f"{path}.dimension"),
            values=values,
        )
    if op in {"all", "any"}:
        _expect_keys(value, path=path, required={"op", "terms"})
        raw_terms = _expect_array(value["terms"], f"{path}.terms")
        if not raw_terms:
            _fail("empty_predicate_terms", f"{path}.terms", "terms must not be empty")
        terms = tuple(
            sorted(
                (
                    _parse_predicate(item, f"{path}.terms[{index}]")
                    for index, item in enumerate(raw_terms)
                ),
                key=lambda predicate: _canonical_json(predicate.to_data()),
            )
        )
        return Predicate(op, terms=terms)
    if op == "not":
        _expect_keys(value, path=path, required={"op", "term"})
        return Predicate(op, terms=(_parse_predicate(value["term"], f"{path}.term"),))
    _fail(
        "unknown_predicate_operation",
        f"{path}.op",
        f"unsupported predicate operation {op!r}",
    )


def _validate_references_and_predicates(document: ProtocolIRDocument) -> None:
    spaces = dict(document.variant_spaces)
    protocols = dict(document.protocols)
    actions = dict(document.actions)
    diagnostics: list[IRDiagnostic] = []

    for space_id, space in document.variant_spaces:
        dimensions = dict(space.dimensions)
        space_diagnostics: list[IRDiagnostic] = []
        for index, constraint in enumerate(space.constraints):
            space_diagnostics.extend(
                _predicate_diagnostics(
                    constraint,
                    dimensions,
                    f"$.variant_spaces.{space_id}.constraints[{index}]",
                )
            )
        diagnostics.extend(space_diagnostics)
        profile_count = math.prod(len(values) for values in dimensions.values())
        if profile_count > _MAX_VARIANT_PROFILES:
            diagnostics.append(
                IRDiagnostic(
                    "variant_space_too_large",
                    f"$.variant_spaces.{space_id}",
                    f"declared Cartesian space has {profile_count} profiles; "
                    f"limit is {_MAX_VARIANT_PROFILES}",
                )
            )
        elif not space_diagnostics and not space.profiles():
            diagnostics.append(
                IRDiagnostic(
                    "empty_variant_space",
                    f"$.variant_spaces.{space_id}",
                    "constraints eliminate every declared variant profile",
                )
            )

    for protocol_id, protocol in document.protocols:
        if protocol.variant_space not in spaces:
            diagnostics.append(
                IRDiagnostic(
                    "unknown_reference",
                    f"$.protocols.{protocol_id}.variant_space",
                    f"unknown variant space {protocol.variant_space!r}",
                )
            )

    for collection_name, definitions in (
        ("expected_action_rules", document.expected_action_rules),
        ("command_bindings", document.command_bindings),
    ):
        for identifier, definition in definitions:
            base_path = f"$.{collection_name}.{identifier}"
            if definition.protocol not in protocols:
                diagnostics.append(
                    IRDiagnostic(
                        "unknown_reference",
                        f"{base_path}.protocol",
                        f"unknown protocol {definition.protocol!r}",
                    )
                )
                continue
            if definition.action not in actions:
                diagnostics.append(
                    IRDiagnostic(
                        "unknown_reference",
                        f"{base_path}.action",
                        f"unknown action {definition.action!r}",
                    )
                )
            rule_space = spaces.get(protocols[definition.protocol].variant_space)
            if rule_space is not None:
                diagnostics.extend(
                    _predicate_diagnostics(
                        definition.when,
                        dict(rule_space.dimensions),
                        f"{base_path}.when",
                    )
                )

    if diagnostics:
        raise IRValidationError(diagnostics)


def _predicate_diagnostics(
    predicate: Predicate,
    dimensions: Mapping[str, tuple[JsonScalar, ...]],
    path: str,
) -> tuple[IRDiagnostic, ...]:
    diagnostics: list[IRDiagnostic] = []
    if predicate.op in {"eq", "in"}:
        assert predicate.dimension is not None
        domain = dimensions.get(predicate.dimension)
        if domain is None:
            diagnostics.append(
                IRDiagnostic(
                    "unknown_selector_dimension",
                    f"{path}.dimension",
                    f"unknown selector dimension {predicate.dimension!r}",
                )
            )
        else:
            values = (predicate.value,) if predicate.op == "eq" else predicate.values
            domain_keys = {_scalar_sort_key(value) for value in domain}
            for value in values:
                if _scalar_sort_key(cast(JsonScalar, value)) not in domain_keys:
                    diagnostics.append(
                        IRDiagnostic(
                            "selector_value_outside_domain",
                            path,
                            f"value {value!r} is not in dimension {predicate.dimension!r}",
                        )
                    )
    child_label = "term" if predicate.op == "not" else "terms"
    for index, term in enumerate(predicate.terms):
        child_path = (
            f"{path}.{child_label}" if predicate.op == "not" else f"{path}.{child_label}[{index}]"
        )
        diagnostics.extend(_predicate_diagnostics(term, dimensions, child_path))
    return tuple(diagnostics)


def _parse_definition_map[Definition](
    raw: object,
    path: str,
    parser: Callable[[object, str], Definition],
) -> tuple[tuple[str, Definition], ...]:
    value = _expect_object(raw, path)
    parsed: list[tuple[str, Definition]] = []
    for identifier in sorted(value):
        _validate_id(identifier, path)
        parsed.append(
            (
                identifier,
                parser(value[identifier], f"{path}.{identifier}"),
            )
        )
    return tuple(parsed)


def _expect_object(raw: object, path: str) -> dict[str, object]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        _fail("expected_object", path, "expected a JSON object")
    return cast(dict[str, object], raw)


def _expect_array(raw: object, path: str) -> list[object]:
    if not isinstance(raw, list):
        _fail("expected_array", path, "expected a JSON array")
    return cast(list[object], raw)


def _expect_string(raw: object, path: str) -> str:
    if not isinstance(raw, str):
        _fail("expected_string", path, "expected a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        _fail("invalid_unicode", path, "string contains an unpaired surrogate")
    return raw


def _expect_reference(raw: object, path: str) -> str:
    value = _expect_string(raw, path)
    _validate_id(value, path)
    return value


def _expect_scalar(raw: object, path: str) -> JsonScalar:
    if type(raw) not in {str, int, bool}:
        _fail(
            "expected_selector_scalar",
            path,
            "expected a string, integer, or boolean selector value",
        )
    value = cast(JsonScalar, raw)
    if isinstance(value, str):
        _expect_string(value, path)
    return value


def _expect_keys(
    value: Mapping[str, object],
    *,
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - allowed)
    diagnostics = [
        IRDiagnostic("missing_property", path, f"missing required property {key!r}")
        for key in missing
    ]
    diagnostics.extend(
        IRDiagnostic("unknown_property", path, f"unknown property {key!r}") for key in unknown
    )
    if diagnostics:
        raise IRValidationError(diagnostics)


def _validate_id(value: str, path: str) -> None:
    if not _ID_PATTERN.fullmatch(value):
        _fail(
            "invalid_identifier",
            path,
            f"identifier {value!r} does not match {_ID_PATTERN.pattern!r}",
        )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _scalar_sort_key(value: JsonScalar) -> bytes:
    return _canonical_json({"type": type(value).__name__, "value": value})


def _universe_sort_key(key: UniverseKey) -> tuple[str, str, bytes]:
    return key.protocol, key.action, _canonical_json(dict(key.profile))


def _scalars_equal(left: JsonScalar, right: JsonScalar | None) -> bool:
    return right is not None and _scalar_sort_key(left) == _scalar_sort_key(right)


def _fail(code: str, path: str, message: str) -> Never:
    raise IRValidationError((IRDiagnostic(code, path, message),))


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class _NonFiniteNumberError(ValueError):
    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(value)


def _reject_non_finite_number(value: str) -> Never:
    raise _NonFiniteNumberError(value)


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _NonFiniteNumberError(value)
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result
