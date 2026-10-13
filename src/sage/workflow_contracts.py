"""Conservative field checks for known Workflow input and output schemas."""

from jsonschema import Draft202012Validator
from referencing import Registry


def check_workflow_fields(template, schemas):
    warnings = []
    outputs = {
        step.step_id: {
            "type": "object",
            "properties": {
                "output": schemas[(step.kind, step.target)][1],
            },
        }
        for step in template.steps
    }

    def pointer(schema, path, label):
        for encoded in path.split("/")[1:] if path else []:
            if "~" in encoded.replace("~0", "").replace("~1", ""):
                raise ValueError(f"Invalid JSON Pointer in {label}")
            part = encoded.replace("~1", "/").replace("~0", "~")
            if not isinstance(schema, dict):
                warnings.append(f"{label}: source schema cannot be resolved statically")
                return {}
            if schema.get("type") == "array" and part.isdigit():
                schema = schema.get("items", {})
            elif part in schema.get("properties", {}):
                schema = schema["properties"][part]
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"Unknown output field {path} in {label}")
            else:
                warnings.append(f"{label}: source field {path} requires runtime validation")
                return {}
        return schema

    for step in template.steps:
        input_schema = schemas[(step.kind, step.target)][0]
        validator = Draft202012Validator(input_schema, registry=Registry())
        properties = input_schema.get("properties", {})
        provided = set(step.input) | set(step.bindings)
        missing = set(input_schema.get("required", [])) - provided
        if missing and not step.inherit_input:
            raise ValueError(
                f"Node {step.step_id} is missing required fields: {', '.join(sorted(missing))}"
            )
        if step.inherit_input:
            warnings.append(f"{step.step_id}: inherited input requires runtime validation")
        for field in sorted(provided):
            if field not in properties and input_schema.get("additionalProperties") is False:
                raise ValueError(f"Unknown input field {step.step_id}.{field}")
            expected = properties.get(field, input_schema.get("additionalProperties", {}))
            binding = step.bindings.get(field)
            label = f"{step.step_id}.{field}"
            if binding is None or binding.source == "literal":
                value = binding.value if binding else step.input[field]
                try:
                    errors = list(validator.descend(value, expected))
                except Exception as exc:
                    raise ValueError(f"Unresolvable input schema for {label}") from exc
                if errors:
                    raise ValueError(
                        f"Invalid literal type or constraints for {label}: {errors[0].validator}"
                    )
            elif binding.source == "step-output":
                source = pointer(outputs[binding.step_id], binding.path, label)
                source_type = source.get("type") if isinstance(source, dict) else None
                expected_type = expected.get("type") if isinstance(expected, dict) else None
                if (isinstance(source_type, str) and isinstance(expected_type, str)
                        and source_type != expected_type
                        and (source_type, expected_type) != ("integer", "number")):
                    raise ValueError(
                        f"Binding type mismatch for {label}: {source_type} -> {expected_type}"
                    )
            else:
                warnings.append(f"{label}: {binding.source} requires runtime validation")
        if step.when and step.when.source == "step-output":
            pointer(outputs[step.when.step_id], step.when.path, f"{step.step_id}.when")
    for name, binding in template.outputs.items():
        if binding.source == "step-output":
            pointer(outputs[binding.step_id], binding.path, f"outputs.{name}")
    if template.outputs:
        warnings.append(
            "Generic Workflow runs return node results; output projection is not applied"
        )
    return sorted(set(warnings))
