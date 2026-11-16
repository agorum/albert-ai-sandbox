"""Domain errors with an HTTP status, shared by core, REST API and CLI."""
from __future__ import annotations

from typing import Any, Dict, Optional


class SandboxError(Exception):
    status = 500
    code = "internal"

    def __init__(self, message: str, *, code: Optional[str] = None, status: Optional[int] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"error": self.code, "message": self.message}
        payload.update(self.details)
        return payload


class NotFound(SandboxError):
    status = 404
    code = "not_found"


class BadRequest(SandboxError):
    status = 400
    code = "bad_request"


class Forbidden(SandboxError):
    """The client is authenticated but its settings do not allow the operation."""
    status = 403
    code = "forbidden"


class Conflict(SandboxError):
    status = 409
    code = "conflict"


class QuotaExceeded(SandboxError):
    status = 409
    code = "quota_exceeded"


class Unavailable(SandboxError):
    status = 503
    code = "unavailable"


class HostFull(SandboxError):
    """The host has no memory left for another running sandbox (memory guard)."""
    status = 503
    code = "host_full"
