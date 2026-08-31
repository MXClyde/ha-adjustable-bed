"""Pinned JSON Schema for the first strict Phase 4 v2 IR slice."""

from __future__ import annotations

import copy

from .model import SCHEMA_REVISION

_ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"https://local.invalid/schemas/{SCHEMA_REVISION}.json",
    "title": "Phase 4 protocol intermediate representation",
    "type": "object",
    "required": [
        "schema_revision",
        "variant_spaces",
        "protocols",
        "actions",
        "expected_action_rules",
        "command_bindings",
    ],
    "properties": {
        "schema_revision": {"const": SCHEMA_REVISION},
        "variant_spaces": {"$ref": "#/$defs/variant_space_map"},
        "protocols": {"$ref": "#/$defs/protocol_map"},
        "actions": {"$ref": "#/$defs/action_map"},
        "expected_action_rules": {"$ref": "#/$defs/rule_map"},
        "command_bindings": {"$ref": "#/$defs/rule_map"},
    },
    "additionalProperties": False,
    "$defs": {
        "identifier": {"type": "string", "pattern": _ID_PATTERN},
        "selector_scalar": {
            "oneOf": [
                {"type": "string"},
                {"type": "integer"},
                {"type": "boolean"},
            ]
        },
        "predicate": {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["op"],
                    "properties": {"op": {"enum": ["always", "never"]}},
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "required": ["op", "dimension", "value"],
                    "properties": {
                        "op": {"const": "eq"},
                        "dimension": {"$ref": "#/$defs/identifier"},
                        "value": {"$ref": "#/$defs/selector_scalar"},
                    },
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "required": ["op", "dimension", "values"],
                    "properties": {
                        "op": {"const": "in"},
                        "dimension": {"$ref": "#/$defs/identifier"},
                        "values": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"$ref": "#/$defs/selector_scalar"},
                        },
                    },
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "required": ["op", "terms"],
                    "properties": {
                        "op": {"enum": ["all", "any"]},
                        "terms": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"$ref": "#/$defs/predicate"},
                        },
                    },
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "required": ["op", "term"],
                    "properties": {
                        "op": {"const": "not"},
                        "term": {"$ref": "#/$defs/predicate"},
                    },
                    "additionalProperties": False,
                },
            ]
        },
        "variant_space": {
            "type": "object",
            "required": ["dimensions", "constraints"],
            "properties": {
                "dimensions": {
                    "type": "object",
                    "propertyNames": {"pattern": _ID_PATTERN},
                    "additionalProperties": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"$ref": "#/$defs/selector_scalar"},
                    },
                },
                "constraints": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/predicate"},
                },
            },
            "additionalProperties": False,
        },
        "protocol": {
            "type": "object",
            "required": ["variant_space"],
            "properties": {"variant_space": {"$ref": "#/$defs/identifier"}},
            "additionalProperties": False,
        },
        "action": {
            "type": "object",
            "properties": {"summary": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
        "rule": {
            "type": "object",
            "required": ["protocol", "action", "when"],
            "properties": {
                "protocol": {"$ref": "#/$defs/identifier"},
                "action": {"$ref": "#/$defs/identifier"},
                "when": {"$ref": "#/$defs/predicate"},
            },
            "additionalProperties": False,
        },
        "variant_space_map": {
            "type": "object",
            "propertyNames": {"pattern": _ID_PATTERN},
            "additionalProperties": {"$ref": "#/$defs/variant_space"},
        },
        "protocol_map": {
            "type": "object",
            "propertyNames": {"pattern": _ID_PATTERN},
            "additionalProperties": {"$ref": "#/$defs/protocol"},
        },
        "action_map": {
            "type": "object",
            "propertyNames": {"pattern": _ID_PATTERN},
            "additionalProperties": {"$ref": "#/$defs/action"},
        },
        "rule_map": {
            "type": "object",
            "propertyNames": {"pattern": _ID_PATTERN},
            "additionalProperties": {"$ref": "#/$defs/rule"},
        },
    },
}


def schema_document() -> dict[str, object]:
    """Return a defensive copy of the pinned schema document."""

    return copy.deepcopy(_SCHEMA)
