"""Typed dependency and evidence binding checks for Phase 4 v2 reports."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeGuard

from tools.phase4_v2.ir import SCHEMA_REVISION, IRValidationError, parse_ir, schema_document

CONTRACT_REVISION = "phase4-v2-validation-input-v1"
VALIDATION_INPUT = "validation-input.json"
_DEPENDENCY_NAMES = ("corpus", "ir", "preflight", "schema")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_ANCHOR_COUNT = 250_000
_MAX_ANCHOR_BYTES = 256 * 1024**2
_MAX_ANCHOR_ID_LENGTH = 256
_MAX_JSON_POINTER_LENGTH = 8_192


class MemberNode(Protocol):
    """The filesystem facts needed by the binding validator."""

    @property
    def kind(self) -> str: ...

    @property
    def size(self) -> int: ...

    @property
    def sha256(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class DependencyPins:
    """Trusted content identities supplied by the orchestration layer."""

    preflight_sha256: str
    ir_sha256: str
    schema_sha256: str
    corpus_sha256: str

    def as_pairs(self) -> tuple[tuple[str, str], ...]:
        """Return stable dependency names and digests."""
        return (
            ("corpus", self.corpus_sha256),
            ("ir", self.ir_sha256),
            ("preflight", self.preflight_sha256),
            ("schema", self.schema_sha256),
        )


@dataclass(frozen=True, slots=True)
class BindingDiagnostic:
    """One deterministic contract or provenance failure."""

    code: str
    path: str
    context: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BindingResult:
    """Result of checking dependency pins and evidence anchors."""

    diagnostics: tuple[BindingDiagnostic, ...]
    dependency_digests: tuple[tuple[str, str], ...]
    anchors_checked: int
    contract_revision: str | None
    validated_evidence_members: tuple[EvidenceMemberAttestation, ...]
    validated_evidence_anchors: tuple[EvidenceAnchorAttestation, ...]


@dataclass(frozen=True, slots=True)
class EvidenceMemberAttestation:
    """One exact report member whose ownership and digest were validated."""

    member: str
    owner: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"member": self.member, "owner": self.owner, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class EvidenceAnchorAttestation:
    """One exact evidence reproduction proven against the pinned IR."""

    id: str
    owner: str
    member: str
    member_sha256: str
    start_byte: int
    end_byte: int
    ir_pointer: str
    representation: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "end_byte": self.end_byte,
            "id": self.id,
            "ir_pointer": self.ir_pointer,
            "member": self.member,
            "member_sha256": self.member_sha256,
            "owner": self.owner,
            "representation": self.representation,
            "start_byte": self.start_byte,
        }


type JsonObject = dict[str, object]
type RangeReader = Callable[[str, int, int], bytes]
type PathValidator = Callable[[str], bool]


def validate_binding_contract(
    document: object,
    *,
    expected_dependencies: DependencyPins,
    nodes: Mapping[str, MemberNode],
    json_documents: Mapping[str, object],
    path_is_safe: PathValidator,
    read_range: RangeReader,
) -> BindingResult:
    """Validate one closed, protocol-neutral provenance contract."""
    pins = expected_dependencies.as_pairs()
    diagnostics: list[BindingDiagnostic] = []
    for name, digest in pins:
        if _DIGEST.fullmatch(digest) is None:
            diagnostics.append(
                BindingDiagnostic(
                    "EXPECTED_DEPENDENCY_DIGEST_INVALID",
                    VALIDATION_INPUT,
                    (("dependency", name),),
                )
            )
    if diagnostics:
        return _result(diagnostics, pins, 0, None, (), ())

    if not isinstance(document, dict):
        return _result(
            [
                BindingDiagnostic(
                    "VALIDATION_INPUT_INVALID", VALIDATION_INPUT, (("reason", "root"),)
                )
            ],
            pins,
            0,
            None,
            (),
            (),
        )
    expected_keys = {"contract_revision", "dependencies", "evidence_members", "anchors"}
    if set(document) != expected_keys:
        return _result(
            [
                BindingDiagnostic(
                    "VALIDATION_INPUT_INVALID",
                    VALIDATION_INPUT,
                    (("reason", "top_level_keys"),),
                )
            ],
            pins,
            0,
            None,
            (),
            (),
        )
    if document["contract_revision"] != CONTRACT_REVISION:
        diagnostics.append(
            BindingDiagnostic(
                "VALIDATION_CONTRACT_REVISION_MISMATCH",
                VALIDATION_INPUT,
            )
        )
    contract_revision = (
        document["contract_revision"] if isinstance(document["contract_revision"], str) else None
    )

    dependencies = document["dependencies"]
    if not isinstance(dependencies, dict) or set(dependencies) != set(_DEPENDENCY_NAMES):
        diagnostics.append(BindingDiagnostic("DEPENDENCY_SET_MISMATCH", VALIDATION_INPUT))
    else:
        _validate_dependencies(
            dependencies,
            dict(pins),
            nodes,
            path_is_safe,
            diagnostics,
        )
    ir_document = _dependency_document(dependencies, "ir", json_documents)
    schema = _dependency_document(dependencies, "schema", json_documents)
    _validate_current_ir_and_schema(ir_document, schema, dependencies, diagnostics)

    owners, validated_members = _validate_evidence_members(
        document["evidence_members"],
        dict(pins)["preflight"],
        nodes,
        path_is_safe,
        diagnostics,
    )
    validated_anchors = _validate_anchors(
        document["anchors"],
        owners,
        {item.member: item for item in validated_members},
        nodes,
        ir_document,
        read_range,
        diagnostics,
    )
    return _result(
        diagnostics,
        pins,
        len(validated_anchors),
        contract_revision,
        validated_members,
        validated_anchors,
    )


def _validate_current_ir_and_schema(
    ir_document: object | None,
    pinned_schema: object | None,
    dependencies: object,
    diagnostics: list[BindingDiagnostic],
) -> None:
    ir_member = _dependency_member(dependencies, "ir")
    schema_member = _dependency_member(dependencies, "schema")
    if ir_document is None:
        diagnostics.append(BindingDiagnostic("PINNED_IR_INVALID", ir_member))
    else:
        try:
            parse_ir(ir_document)
        except IRValidationError as error:
            context: tuple[tuple[str, str], ...] = ()
            if error.diagnostics:
                first = error.diagnostics[0]
                context = (("ir_code", first.code), ("ir_path", first.path))
            diagnostics.append(BindingDiagnostic("PINNED_IR_INVALID", ir_member, context))

    current_schema = schema_document()
    pinned_revision = _schema_revision(pinned_schema)
    if pinned_revision != SCHEMA_REVISION:
        diagnostics.append(
            BindingDiagnostic(
                "PINNED_SCHEMA_REVISION_MISMATCH",
                schema_member,
                (("expected", SCHEMA_REVISION),),
            )
        )
    if pinned_schema != current_schema:
        diagnostics.append(BindingDiagnostic("PINNED_SCHEMA_STRUCTURE_MISMATCH", schema_member))


def _schema_revision(document: object | None) -> object | None:
    if not isinstance(document, dict):
        return None
    properties = document.get("properties")
    if not isinstance(properties, dict):
        return None
    revision = properties.get("schema_revision")
    return revision.get("const") if isinstance(revision, dict) else None


def _validate_dependencies(
    dependencies: JsonObject,
    expected: dict[str, str],
    nodes: Mapping[str, MemberNode],
    path_is_safe: PathValidator,
    diagnostics: list[BindingDiagnostic],
) -> None:
    for name in _DEPENDENCY_NAMES:
        value = dependencies[name]
        if not _is_exact_object(value, {"member", "sha256"}):
            diagnostics.append(
                BindingDiagnostic(
                    "DEPENDENCY_ENTRY_INVALID",
                    VALIDATION_INPUT,
                    (("dependency", name),),
                )
            )
            continue
        member = value["member"]
        digest = value["sha256"]
        if not isinstance(member, str) or not path_is_safe(member):
            diagnostics.append(
                BindingDiagnostic(
                    "DEPENDENCY_MEMBER_PATH_INVALID",
                    VALIDATION_INPUT,
                    (("dependency", name),),
                )
            )
            continue
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            diagnostics.append(
                BindingDiagnostic(
                    "DEPENDENCY_DIGEST_INVALID",
                    member,
                    (("dependency", name),),
                )
            )
            continue
        if digest != expected[name]:
            diagnostics.append(
                BindingDiagnostic(
                    "DEPENDENCY_PIN_MISMATCH",
                    member,
                    (("dependency", name),),
                )
            )
        node = nodes.get(member)
        if node is None:
            diagnostics.append(
                BindingDiagnostic(
                    "DEPENDENCY_MEMBER_MISSING",
                    member,
                    (("dependency", name),),
                )
            )
        elif node.kind != "file":
            diagnostics.append(
                BindingDiagnostic(
                    "DEPENDENCY_MEMBER_NOT_REGULAR",
                    member,
                    (("dependency", name),),
                )
            )
        elif node.sha256 != digest:
            diagnostics.append(
                BindingDiagnostic(
                    "DEPENDENCY_MEMBER_DIGEST_MISMATCH",
                    member,
                    (("dependency", name),),
                )
            )


def _validate_evidence_members(
    value: object,
    expected_owner: str,
    nodes: Mapping[str, MemberNode],
    path_is_safe: PathValidator,
    diagnostics: list[BindingDiagnostic],
) -> tuple[dict[str, str], tuple[EvidenceMemberAttestation, ...]]:
    owners: dict[str, str] = {}
    validated: list[EvidenceMemberAttestation] = []
    if not isinstance(value, list):
        diagnostics.append(BindingDiagnostic("EVIDENCE_MEMBERS_INVALID", VALIDATION_INPUT))
        return owners, ()
    for index, entry in enumerate(value):
        location = f"{VALIDATION_INPUT}#/evidence_members/{index}"
        if not _is_exact_object(entry, {"member", "owner", "sha256"}):
            diagnostics.append(BindingDiagnostic("EVIDENCE_MEMBER_INVALID", location))
            continue
        member = entry["member"]
        owner = entry["owner"]
        digest = entry["sha256"]
        if (
            not isinstance(member, str)
            or not path_is_safe(member)
            or not isinstance(owner, str)
            or _DIGEST.fullmatch(owner) is None
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            diagnostics.append(BindingDiagnostic("EVIDENCE_MEMBER_INVALID", location))
            continue
        if member in owners:
            diagnostics.append(BindingDiagnostic("EVIDENCE_MEMBER_DUPLICATE", member))
            continue
        owners[member] = owner
        member_valid = True
        if owner != expected_owner:
            diagnostics.append(BindingDiagnostic("EVIDENCE_OWNER_PIN_MISMATCH", member))
            member_valid = False
        node = nodes.get(member)
        if node is None:
            diagnostics.append(BindingDiagnostic("EVIDENCE_MEMBER_MISSING", member))
            member_valid = False
        elif node.kind != "file":
            diagnostics.append(BindingDiagnostic("EVIDENCE_MEMBER_NOT_REGULAR", member))
            member_valid = False
        elif node.sha256 != digest:
            diagnostics.append(BindingDiagnostic("EVIDENCE_MEMBER_DIGEST_MISMATCH", member))
            member_valid = False
        if member_valid:
            validated.append(EvidenceMemberAttestation(member, owner, digest))
    return owners, tuple(sorted(validated, key=lambda item: item.member.encode()))


def _validate_anchors(
    value: object,
    owners: Mapping[str, str],
    validated_members: Mapping[str, EvidenceMemberAttestation],
    nodes: Mapping[str, MemberNode],
    ir_document: object | None,
    read_range: RangeReader,
    diagnostics: list[BindingDiagnostic],
) -> tuple[EvidenceAnchorAttestation, ...]:
    if not isinstance(value, list) or not value:
        diagnostics.append(BindingDiagnostic("EVIDENCE_ANCHORS_INVALID", VALIDATION_INPUT))
        return ()
    if len(value) > _MAX_ANCHOR_COUNT:
        diagnostics.append(
            BindingDiagnostic(
                "EVIDENCE_ANCHOR_LIMIT_EXCEEDED",
                VALIDATION_INPUT,
                (("limit", str(_MAX_ANCHOR_COUNT)),),
            )
        )
        return ()
    cumulative_bytes = _anchor_byte_total(value)
    if cumulative_bytes > _MAX_ANCHOR_BYTES:
        diagnostics.append(
            BindingDiagnostic(
                "EVIDENCE_BYTE_BUDGET_EXCEEDED",
                VALIDATION_INPUT,
                (("limit", str(_MAX_ANCHOR_BYTES)),),
            )
        )
        return ()
    seen_ids: set[str] = set()
    attestations: list[EvidenceAnchorAttestation] = []
    for index, anchor in enumerate(value):
        location = f"{VALIDATION_INPUT}#/anchors/{index}"
        required = {
            "id",
            "owner",
            "member",
            "start_byte",
            "end_byte",
            "ir_pointer",
            "representation",
        }
        if not _is_exact_object(anchor, required):
            diagnostics.append(BindingDiagnostic("EVIDENCE_ANCHOR_INVALID", location))
            continue
        anchor_id = anchor["id"]
        owner = anchor["owner"]
        member = anchor["member"]
        start = anchor["start_byte"]
        end = anchor["end_byte"]
        ir_pointer = anchor["ir_pointer"]
        representation = anchor["representation"]
        if (
            not isinstance(anchor_id, str)
            or not anchor_id
            or len(anchor_id) > _MAX_ANCHOR_ID_LENGTH
            or not isinstance(owner, str)
            or _DIGEST.fullmatch(owner) is None
            or not isinstance(member, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(ir_pointer, str)
            or len(ir_pointer) > _MAX_JSON_POINTER_LENGTH
            or not isinstance(representation, str)
            or representation not in {"hex", "utf8"}
        ):
            diagnostics.append(BindingDiagnostic("EVIDENCE_ANCHOR_INVALID", location))
            continue
        if anchor_id in seen_ids:
            diagnostics.append(BindingDiagnostic("EVIDENCE_ANCHOR_DUPLICATE_ID", anchor_id))
            continue
        seen_ids.add(anchor_id)
        declared_owner = owners.get(member)
        if declared_owner is None:
            diagnostics.append(
                BindingDiagnostic(
                    "EVIDENCE_ANCHOR_MEMBER_UNDECLARED", member, (("anchor", anchor_id),)
                )
            )
            continue
        if declared_owner != owner:
            diagnostics.append(
                BindingDiagnostic(
                    "EVIDENCE_ANCHOR_OWNER_MISMATCH", member, (("anchor", anchor_id),)
                )
            )
            continue
        validated_member = validated_members.get(member)
        if validated_member is None:
            continue
        node = nodes.get(member)
        if node is None or node.kind != "file":
            continue
        if start < 0 or end <= start:
            diagnostics.append(
                BindingDiagnostic("EVIDENCE_RANGE_INVALID", member, (("anchor", anchor_id),))
            )
            continue
        if end > node.size:
            diagnostics.append(
                BindingDiagnostic(
                    "EVIDENCE_RANGE_OUT_OF_BOUNDS",
                    member,
                    (("anchor", anchor_id),),
                )
            )
            continue
        try:
            source = read_range(member, start, end)
        except OSError as error:
            context = (("anchor", anchor_id),)
            if error.errno is not None:
                context += (("errno", str(error.errno)),)
            diagnostics.append(
                BindingDiagnostic("EVIDENCE_MEMBER_UNREADABLE", member, tuple(sorted(context)))
            )
            continue
        if representation == "hex":
            reproduced = source.hex()
        else:
            try:
                reproduced = source.decode("utf-8")
            except UnicodeDecodeError:
                diagnostics.append(
                    BindingDiagnostic("EVIDENCE_UTF8_INVALID", member, (("anchor", anchor_id),))
                )
                continue
        try:
            expected_value = _resolve_json_pointer(ir_document, ir_pointer)
        except KeyError, TypeError, ValueError:
            diagnostics.append(
                BindingDiagnostic(
                    "EVIDENCE_IR_POINTER_INVALID",
                    member,
                    (("anchor", anchor_id),),
                )
            )
            continue
        if not isinstance(expected_value, str):
            diagnostics.append(
                BindingDiagnostic(
                    "EVIDENCE_IR_VALUE_NOT_STRING",
                    member,
                    (("anchor", anchor_id),),
                )
            )
            continue
        if reproduced != expected_value:
            diagnostics.append(
                BindingDiagnostic("EVIDENCE_VALUE_MISMATCH", member, (("anchor", anchor_id),))
            )
            continue
        attestations.append(
            EvidenceAnchorAttestation(
                id=anchor_id,
                owner=owner,
                member=member,
                member_sha256=validated_member.sha256,
                start_byte=start,
                end_byte=end,
                ir_pointer=ir_pointer,
                representation=representation,
            )
        )
    return tuple(sorted(attestations, key=lambda item: item.id.encode()))


def _anchor_byte_total(value: list[object]) -> int:
    total = 0
    for anchor in value:
        if not isinstance(anchor, dict):
            continue
        start = anchor.get("start_byte")
        end = anchor.get("end_byte")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
        ):
            continue
        total += end - start
        if total > _MAX_ANCHOR_BYTES:
            return total
    return total


def _is_exact_object(value: object, keys: set[str]) -> TypeGuard[JsonObject]:
    return isinstance(value, dict) and set(value) == keys


def _dependency_document(
    dependencies: object,
    name: str,
    json_documents: Mapping[str, object],
) -> object | None:
    if not isinstance(dependencies, dict):
        return None
    entry = dependencies.get(name)
    if not isinstance(entry, dict):
        return None
    member = entry.get("member")
    return json_documents.get(member) if isinstance(member, str) else None


def _dependency_member(dependencies: object, name: str) -> str:
    if isinstance(dependencies, dict):
        entry = dependencies.get(name)
        if isinstance(entry, dict):
            member = entry.get("member")
            if isinstance(member, str):
                return member
    return VALIDATION_INPUT


def _resolve_json_pointer(document: object | None, pointer: str) -> object:
    if document is None or not pointer.startswith("/"):
        raise ValueError("IR document or absolute pointer is missing")
    current = document
    for encoded in pointer[1:].split("/"):
        token = _decode_pointer_token(encoded)
        if isinstance(current, dict):
            if token not in current:
                raise KeyError(token)
            current = current[token]
        elif isinstance(current, list):
            if not token.isdecimal() or (len(token) > 1 and token.startswith("0")):
                raise ValueError("array index is not canonical")
            index = int(token)
            if index >= len(current):
                raise KeyError(token)
            current = current[index]
        else:
            raise TypeError("pointer traverses a scalar")
    return current


def _decode_pointer_token(token: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(token):
        if token[index] != "~":
            decoded.append(token[index])
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise ValueError("invalid JSON pointer escape")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _result(
    diagnostics: list[BindingDiagnostic],
    pins: tuple[tuple[str, str], ...],
    anchors_checked: int,
    contract_revision: str | None,
    validated_members: tuple[EvidenceMemberAttestation, ...],
    validated_anchors: tuple[EvidenceAnchorAttestation, ...],
) -> BindingResult:
    ordered = tuple(
        sorted(
            set(diagnostics),
            key=lambda item: (
                item.code,
                item.path.encode("utf-8", "surrogateescape"),
                item.context,
            ),
        )
    )
    return BindingResult(
        diagnostics=ordered,
        dependency_digests=pins,
        anchors_checked=anchors_checked,
        contract_revision=contract_revision,
        validated_evidence_members=validated_members,
        validated_evidence_anchors=validated_anchors,
    )
