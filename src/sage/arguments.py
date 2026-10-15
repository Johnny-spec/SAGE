"""Server-owned construction of executable Skill arguments."""

from sage.attachments import AttachmentRecord
from sage.errors import ExecutionError


def build_skill_arguments(
    input_schema: dict[str, object],
    message: str,
    supplied: dict[str, object] | None,
    attachments: tuple[AttachmentRecord, ...],
    accepted_attachment_types: tuple[str, ...] = (),
) -> dict[str, object]:
    arguments = dict(supplied or {})
    if any(key.casefold().endswith("_path") for key in arguments):
        raise ExecutionError("Clients cannot supply host filesystem paths")

    properties = input_schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    path_properties = tuple(
        name for name in ("log_path", "netlog_path", "evidence_path") if name in properties
    )
    if path_properties:
        required = input_schema.get("required", [])
        path_required = any(name in required for name in path_properties)
        if len(attachments) > 1 or (path_required and len(attachments) != 1):
            raise ExecutionError("This Skill requires exactly one uploaded attachment")
        if attachments:
            accepted = {item.casefold() for item in accepted_attachment_types}
            actual = {item.casefold() for item in attachments[0].route_types}
            if accepted and not accepted.intersection(actual):
                raise ExecutionError(
                    "Uploaded attachment is not compatible with this Skill: "
                    f"{attachments[0].filename}"
                )
            arguments[path_properties[0]] = str(attachments[0].path)
    if "text" in properties and "text" not in arguments:
        arguments["text"] = message
    if "message" in properties and "message" not in arguments:
        arguments["message"] = message
    if "artifact_ids" in properties and attachments:
        arguments["artifact_ids"] = [item.artifact_id for item in attachments]
    return arguments
