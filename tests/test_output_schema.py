"""The --output-schema contract with the live structured-outputs API.

`schemas/run-result.json` is passed straight to `codex exec --output-schema`,
where it becomes an OpenAI structured-output response format.  That API is far
stricter than plain JSON Schema, and a violation fails EVERY builder run
instantly with a 400 -- which no recorded fixture can catch.  These tests pin
the three rules the live API actually enforces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor.runner.base import normalize_final_result, validate_result

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "run-result.json").read_text(encoding="utf-8")
)

SUPPORTED_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"}


def _objects(node: object, path: str = "root"):
    """Yield every (path, schema) that describes an object."""

    if isinstance(node, dict):
        types = node.get("type")
        types = types if isinstance(types, list) else [types]
        if "object" in types:
            yield path, node
        for key, value in node.get("properties", {}).items():
            yield from _objects(value, f"{path}.{key}")
        items = node.get("items")
        if items is not None:
            yield from _objects(items, f"{path}[]")


def test_every_object_forbids_additional_properties() -> None:
    for path, schema in _objects(SCHEMA):
        assert schema.get("additionalProperties") is False, (
            f"{path} must set additionalProperties:false or the API rejects the whole schema"
        )


def test_every_object_requires_all_of_its_properties() -> None:
    """Strict mode: `required` must list every key in `properties`.

    Optional fields are expressed as nullable types, not by omission.
    """

    for path, schema in _objects(SCHEMA):
        properties = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        assert properties == required, f"{path}: required must equal properties, missing {properties - required}"


def test_no_unsupported_string_format_is_used() -> None:
    """`uri-reference` is not a supported format and 400s the request."""

    def walk(node: object, path: str = "root") -> None:
        if isinstance(node, dict):
            fmt = node.get("format")
            if fmt is not None:
                assert fmt in SUPPORTED_FORMATS, f"{path}: format {fmt!r} is not supported"
            for key, value in node.items():
                if key != "format":
                    walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(SCHEMA)


def test_a_nullable_enum_includes_null_in_its_enum_list() -> None:
    for name in ("outcome", "waiting_on"):
        node = SCHEMA["properties"][name]
        assert "null" in node["type"]
        assert None in node["enum"], f"{name}: a nullable enum must allow null explicitly"


@pytest.mark.parametrize(
    "payload",
    [
        # Exactly what the live API returned for this schema.
        {"status": "failed", "outcome": None, "waiting_on": None, "pr_url": None,
         "summary": "smoke test", "next_action": None, "blocker": None},
        {"status": "waiting", "outcome": "pr_open", "waiting_on": "review",
         "pr_url": "https://example.test/pr/1", "summary": "PR open",
         "next_action": None, "blocker": None},
    ],
)
def test_a_schema_conformant_payload_validates(payload: dict) -> None:
    """Every property is present-but-null; null must mean "unset", not invalid."""

    assert validate_result(payload) == []
    normalize_final_result(payload)


def test_null_does_not_mask_a_genuinely_bad_value() -> None:
    assert validate_result({"status": "wat", "summary": "s"})
    assert validate_result({"status": "completed", "summary": "s", "outcome": "nonsense"})
    assert validate_result({"status": "completed", "summary": "s", "waiting_on": "nonsense"})
    assert validate_result({"status": "completed", "summary": "s", "pr_url": 7})
