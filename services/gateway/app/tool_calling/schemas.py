from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal


SchemaMode = Literal["strict_preserve", "strict_autofix", "best_effort"]


class ToolSchemaError(ValueError):
    pass


def strict_object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": deepcopy(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


def normalize_tool_definition(tool: Any, *, mode: SchemaMode = "best_effort") -> dict[str, Any]:
    if not isinstance(tool, dict) or tool.get("type", "function") != "function":
        raise ToolSchemaError("tool must be an OpenAI function definition")
    function = tool.get("function")
    if not isinstance(function, dict) or not str(function.get("name") or "").strip():
        raise ToolSchemaError("tool.function.name must be a non-empty string")

    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        parameters = strict_object_schema({}) if mode == "strict_autofix" else {"type": "object", "properties": {}}
    else:
        parameters = deepcopy(parameters)

    if mode in {"strict_preserve", "strict_autofix"}:
        if parameters.get("type") != "object":
            raise ToolSchemaError(f"tool {function['name']} parameters.type must be object")
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            if mode == "strict_preserve":
                raise ToolSchemaError(f"tool {function['name']} parameters.properties must be an object")
            properties = {}
            parameters["properties"] = properties
        if mode == "strict_preserve":
            if parameters.get("additionalProperties") is not False:
                raise ToolSchemaError(f"tool {function['name']} must set additionalProperties=false")
            if set(parameters.get("required") or []) != set(properties):
                raise ToolSchemaError(f"tool {function['name']} must require every property")
            if function.get("strict") is not True:
                raise ToolSchemaError(f"tool {function['name']} must set strict=true")
        else:
            originally_required = set(parameters.get("required") or [])
            for name, property_schema in properties.items():
                if name in originally_required or not isinstance(property_schema, dict):
                    continue
                property_type = property_schema.get("type")
                if isinstance(property_type, str):
                    property_schema["type"] = [property_type, "null"]
                elif isinstance(property_type, list) and "null" not in property_type:
                    property_schema["type"] = [*property_type, "null"]
            parameters["required"] = list(properties)
            parameters["additionalProperties"] = False

    normalized_function: dict[str, Any] = {
        "name": str(function["name"]).strip(),
        "parameters": parameters,
    }
    if isinstance(function.get("description"), str):
        normalized_function["description"] = function["description"]
    if mode != "best_effort" or isinstance(function.get("strict"), bool):
        normalized_function["strict"] = True if mode == "strict_autofix" else function.get("strict")
    return {"type": "function", "function": normalized_function}


def validate_arguments(schema: dict[str, Any], value: Any, *, path: str = "arguments") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    if value is None and "null" in types:
        return errors
    actual_ok = (
        ("object" in types and isinstance(value, dict))
        or ("array" in types and isinstance(value, list))
        or ("string" in types and isinstance(value, str))
        or ("boolean" in types and isinstance(value, bool))
        or ("integer" in types and isinstance(value, int) and not isinstance(value, bool))
        or ("number" in types and isinstance(value, (int, float)) and not isinstance(value, bool))
    )
    if expected is not None and not actual_ok:
        return [f"{path} must have type {expected}"]
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name} is not allowed")
        for name, child in properties.items():
            if name in value and isinstance(child, dict):
                errors.extend(validate_arguments(child, value[name], path=f"{path}.{name}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(validate_arguments(schema["items"], item, path=f"{path}.{index}"))
    if "enum" in schema and value not in schema.get("enum", []):
        errors.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append(f"{path} must be >= {schema['minimum']}")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append(f"{path} must be <= {schema['maximum']}")
    return errors
