"""What every operator tool is made of (docs/operator-api.md): the Tool record, the ToolError a tool raises with a
plain sentence for the model, and the argument helpers. app.services.operator_tools (the read tools and the CRM
write tools) and app.services.operator_writes (the screens' write tools) both build on it."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..models import User


class ToolError(Exception):
    """A plain-English reason the tool could not do what was asked."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[[Session, User, dict], Any]
    write: bool = False

    def listing(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


def _int(args: dict, name: str) -> int:
    raw = args.get(name)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > 2_000_000_000:
        raise ToolError(f"{name} must be a positive whole number")
    return raw


def _text(args: dict, name: str, limit: int, *, required: bool = False) -> str:
    raw = args.get(name, "")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ToolError(f"{name} must be text")
    text = raw.strip()
    if required and not text:
        raise ToolError(f"{name} is required")
    if len(text) > limit:
        raise ToolError(f"{name} is longer than {limit} characters")
    return text


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema
