"""Backend-agnostic contracts for the Hermes storage layer.

A backend owns ONE tenant's data (per-tenant schema isolation, design D5).
Content is append-only and versioned: ``put_document`` never rewrites an
existing version, and session prompt-caching safety is built on versions
being immutable (see design/enterprise-storage-plan.md §9).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Document:
    """One immutable version of a named content item in a tenant."""
    kind: str
    key: str
    version: int
    content: str
    meta: dict = field(default_factory=dict)
    checksum: str = ""
    created_by: str = "agent"
    created_at: str = ""


class StorageBackend(ABC):
    """Storage for a single tenant. All calls are tenant-scoped by construction."""

    name: str = "abstract"
    tenant: str = ""

    @abstractmethod
    def get_document(self, kind: str, key: str, version: int | None = None) -> Document | None:
        """Latest (or the pinned *version* of) ``(kind, key)``, or None when absent."""

    @abstractmethod
    def put_document(self, kind: str, key: str, content: str, *,
                     meta: dict | None = None, created_by: str = "agent") -> Document:
        """Append the next version of ``(kind, key)`` and return it."""

    @abstractmethod
    def is_ready(self) -> bool:
        """Server reachable and the tenant schema migrated."""

    def close(self) -> None:
        """Release pooled resources. Default: nothing to release."""
