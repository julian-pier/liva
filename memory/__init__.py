"""Internal helpers for legacy markdown-memory consumers."""

from __future__ import annotations

from flask import current_app, has_app_context

from memory.memory_service import MemoryService


def get_memory_service() -> MemoryService:
    if not has_app_context():
        return MemoryService()
    service = current_app.extensions.get("liva_memory_service")
    if service is None:
        service = MemoryService()
        current_app.extensions["liva_memory_service"] = service
    return service


__all__ = ["get_memory_service"]
