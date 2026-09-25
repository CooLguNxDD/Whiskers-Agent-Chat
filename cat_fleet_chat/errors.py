"""Domain errors shared by the REST and MCP adapters."""

from __future__ import annotations

from typing import Any


class HubError(Exception):
    """A request the hub will not apply.

    ``code`` is a stable machine string. ``status`` is the HTTP status the
    REST adapter should use. Adapters map this to their own error envelope.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def rest_body(self) -> dict[str, Any]:
        """REST error envelope."""
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}

    def mcp_body(self) -> dict[str, Any]:
        """MCP / plugin error envelope."""
        body: dict[str, Any] = {
            "status": "error",
            "error": self.code,
            "message": self.message,
            "http_status": self.status,
        }
        if self.details:
            body["details"] = self.details
        return body
