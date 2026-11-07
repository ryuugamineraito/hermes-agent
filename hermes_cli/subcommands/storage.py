"""``hermes storage`` subcommand parser."""
from __future__ import annotations

from hermes_cli.subcommands._shared import add_json_flag


def _cmd_storage(args):
    from hermes_cli.storage_cmd import cmd_storage
    return cmd_storage(args)


def build_storage_parser(subparsers) -> None:
    """Attach the ``storage`` subcommand to ``subparsers``."""
    storage_parser = subparsers.add_parser(
        "storage", help="Inspect and manage the storage backend (local / postgres)",
        description="Inspect and manage the storage backend. Default is local (files + SQLite "
                    "under HERMES_HOME); storage.backend=postgres in config.yaml switches to the "
                    "per-tenant PostgreSQL schema (needs the [postgres] extra).")
    actions = storage_parser.add_subparsers(dest="storage_action", required=True)

    status_parser = actions.add_parser(
        "status", help="Show backend, tenant, connectivity and document counts")
    add_json_flag(status_parser, help="Machine-readable JSON output")

    provision_parser = actions.add_parser(
        "provision", help="Create the tenant schema and apply migrations (idempotent)")

    soul_parser = actions.add_parser(
        "soul", help="Manage the SOUL.md identity document",
        description="In postgres mode SOUL.md lives in the tenant schema (documents table); "
                    "in local mode this reads/writes the file in HERMES_HOME.")
    soul_actions = soul_parser.add_subparsers(dest="soul_action", required=True)
    soul_actions.add_parser("show", help="Print SOUL.md")
    set_parser = soul_actions.add_parser("set", help="Store a new SOUL.md version")
    content = set_parser.add_mutually_exclusive_group(required=True)
    content.add_argument("--file", metavar="PATH", help="Read SOUL.md from a file")
    content.add_argument("--text", help="SOUL.md content inline")
    soul_actions.add_parser("import", help="Import the local home's SOUL.md into the backend")

    for parser in (status_parser, provision_parser, soul_parser):
        parser.set_defaults(func=_cmd_storage)
