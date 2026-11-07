"""``hermes storage`` — inspect and manage the configured storage backend."""
from __future__ import annotations

import json
import sys
from urllib.parse import urlsplit, urlunsplit


def _redact_dsn(dsn: str) -> str:
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "<unparseable dsn>"
    netloc = parts.netloc.rsplit("@", 1)[-1]  # drop user:password
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _backend_or_die():
    from storage import get_backend
    from storage.config import StorageConfigError
    try:
        backend = get_backend()
    except StorageConfigError as e:
        print(f"storage: {e}", file=sys.stderr)
        raise SystemExit(2)
    if backend is None:
        print("storage: backend is 'local' (no storage: section in config.yaml). "
              "Set storage.backend: postgres + storage.dsn (or dsn_env) for Postgres.")
        raise SystemExit(1)
    return backend


def _soul_from_local_home() -> str | None:
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "SOUL.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8-sig")


def _cmd_soul(args) -> int:
    from storage import get_backend
    action = args.soul_action
    if action == "show":
        backend = get_backend()
        if backend is not None:
            doc = backend.get_document("SOUL", "SOUL.md")
            if doc is None:
                print("storage: no SOUL.md in tenant schema", file=sys.stderr)
                return 1
            print(doc.content, end="")
            return 0
        content = _soul_from_local_home()
        if content is None:
            print("storage: no SOUL.md in HERMES_HOME", file=sys.stderr)
            return 1
        print(content, end="")
        return 0
    backend = _backend_or_die()
    if action == "set":
        if args.file:
            with open(args.file, "r", encoding="utf-8-sig") as fh:
                content = fh.read()
        else:
            content = args.text
        doc = backend.put_document("SOUL", "SOUL.md", content, created_by="cli")
        print(json.dumps({"kind": "SOUL", "key": "SOUL.md", "version": doc.version,
                          "checksum": doc.checksum}, indent=2))
        return 0
    if action == "import":
        content = _soul_from_local_home()
        if content is None:
            print("storage: no SOUL.md in HERMES_HOME to import", file=sys.stderr)
            return 1
        doc = backend.put_document("SOUL", "SOUL.md", content, created_by="import")
        print(json.dumps({"kind": "SOUL", "key": "SOUL.md", "version": doc.version,
                          "checksum": doc.checksum}, indent=2))
        return 0
    return 2


def _cmd_status(args) -> int:
    from hermes_constants import display_hermes_home, get_hermes_home
    from storage import get_backend
    try:
        backend = get_backend()
    except Exception as e:
        print(f"storage: config error: {e}", file=sys.stderr)
        return 2
    if backend is None:
        out = {"backend": "local", "home": str(display_hermes_home())}
    else:
        from storage.config import get_storage_config
        cfg = get_storage_config()
        out = {
            "backend": backend.name,
            "tenant": backend.tenant,
            "schema": backend.schema_name,
            "dsn": _redact_dsn(cfg.dsn),
            "ready": backend.is_ready(),
            "documents": [{"kind": kind, "versions": n} for kind, n in
                          backend.document_counts()] if backend.is_ready() else [],
        }
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
    else:
        for key, value in out.items():
            print(f"{key}: {value}")
    return 0


def cmd_storage(args) -> int:
    from storage import clear_backend_cache
    try:
        action = args.storage_action
        if action == "status":
            return _cmd_status(args)
        if action == "provision":
            backend = _backend_or_die()
            print(json.dumps({"backend": backend.name, "tenant": backend.tenant,
                              "schema": backend.schema_name, "ready": backend.is_ready()},
                             indent=2))
            return 0
        if action == "soul":
            return _cmd_soul(args)
        return 2
    finally:
        # One-shot process: dispose the pooled connections before interpreter
        # finalization; an engine left to GC there races psycopg's C extension
        # (intermittent SIGSEGV at exit on CPython 3.13+).
        clear_backend_cache()
