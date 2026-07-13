from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Priority = Literal["low", "normal", "high", "urgent"]
TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


def _validate_json_size(value: Any, *, field_name: str, max_bytes: int = 100_000) -> Any:
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except Exception as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    if size > max_bytes:
        raise ValueError(f"{field_name} exceeds {max_bytes} bytes")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    metadata: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_json_size(value, field_name="metadata") if value is not None else None


class WorkspacePatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    metadata: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_json_size(value, field_name="metadata") if value is not None else None

    @model_validator(mode="after")
    def require_field(self) -> "WorkspacePatch":
        if not self.model_fields_set:
            raise ValueError("At least one workspace field is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


class TaskCreate(StrictModel):
    instruction: str = Field(min_length=1, max_length=100_000)
    context: Any = None
    priority: Priority = "normal"
    max_retries: int = Field(default=3, ge=0, le=20)

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("instruction must not be blank")
        return normalized

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: Any) -> Any:
        return _validate_json_size(value, field_name="context") if value is not None else None


class TaskPatch(StrictModel):
    status: TaskStatus | None = None
    priority: Priority | None = None

    @model_validator(mode="after")
    def require_field(self) -> "TaskPatch":
        if not self.model_fields_set:
            raise ValueError("At least one task field is required")
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        if "priority" in self.model_fields_set and self.priority is None:
            raise ValueError("priority cannot be null")
        return self


class ExecuteRequest(StrictModel):
    command: str | list[str] | None = None
    code: str | None = Field(default=None, max_length=250_000)
    language: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def validate_execution(self) -> "ExecuteRequest":
        if (self.command is None) == (self.code is None):
            raise ValueError("Provide exactly one of command or code")
        if self.command is not None:
            if isinstance(self.command, str) and not self.command.strip():
                raise ValueError("command must not be empty")
            if isinstance(self.command, list) and not self.command:
                raise ValueError("command must not be empty")
            if self.language is not None:
                raise ValueError("language is only valid with code")
        if self.code is not None and not str(self.language or "").strip():
            raise ValueError("language is required with code")
        return self
