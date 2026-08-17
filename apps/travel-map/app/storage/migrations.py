"""Operator-only migration and verification command for private SQLite storage."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from app.storage.database import SCHEMA_VERSION, SqliteDatabase
from app.storage.models import StorageIntegrityError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.storage.migrations")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "verify"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--database", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    database = SqliteDatabase(arguments.database)
    try:
        if arguments.command == "migrate":
            version = database.migrate()
        else:
            database.verify_current_schema()
            version = SCHEMA_VERSION
    except StorageIntegrityError:
        _parser().error("private database migration failed")
    print(f"{arguments.command}: schema version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
