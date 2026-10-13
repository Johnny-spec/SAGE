"""Conservative schema-aware filtering for governed call views."""

from typing import Any


def contains_sensitive(value: Any, schema: dict[str, Any] | None = None) -> bool:
    schema = schema if isinstance(schema, dict) else {}
    if schema.get("$ref") or any(
        contains_sensitive(value, branch)
        for keyword in ("allOf", "anyOf", "oneOf") for branch in schema.get(keyword, [])
    ):
        return True
    if schema.get("writeOnly") or schema.get("format") == "password" or schema.get("x-sensitive"):
        return True
    if isinstance(value, dict):
        return any(
            any(part in key.casefold()
                for part in ("password", "token", "secret", "authorization", "api_key"))
            or contains_sensitive(item, schema.get("properties", {}).get(key, {}))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_sensitive(item, schema.get("items", {})) for item in value)
    return False


def redact(value: Any, secret: str | None = None, schema: dict[str, Any] | None = None) -> Any:
    schema = schema if isinstance(schema, dict) else {}
    if (schema.get("$ref") or schema.get("writeOnly") or schema.get("x-sensitive")
            or schema.get("format") == "password"):
        return "[redacted]"
    for keyword in ("allOf", "anyOf", "oneOf"):
        for branch in schema.get(keyword, []):
            value = redact(value, secret, branch)
    if isinstance(value, dict):
        return {
            key: "[redacted]" if contains_sensitive({key: None}) else redact(
                item, secret, schema.get("properties", {}).get(key, {}),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secret, schema.get("items", {})) for item in value]
    return value.replace(secret, "[redacted]") if isinstance(value, str) and secret else value