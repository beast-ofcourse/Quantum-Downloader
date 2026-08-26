"""Web UI package: FastAPI app, service layer, job store, and event bus."""

from __future__ import annotations

from .app import create_app
from .service import Service

__all__ = ["create_app", "Service"]
