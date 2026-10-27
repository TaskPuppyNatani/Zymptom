"""Launch the logger, or explicitly initialize a database without a GUI."""

import argparse
from contextlib import closing
from pathlib import Path

from zymptom import __version__
from zymptom.persistence.database import connect_database
from zymptom.persistence.migrations import schema_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zymptom — a local functional journal.")
    parser.add_argument("--version", action="version", version=f"Zymptom {__version__}")
    parser.add_argument("--database", type=Path, help="Use a particular local SQLite file.")
    parser.add_argument("--init-db", action="store_true", help="Initialize/migrate and exit without opening the app.")
    parser.add_argument("--sandbox-sync", action="store_true", help="Launch the separate disposable desktop sync proof.")
    args = parser.parse_args(argv)
    if args.sandbox_sync:
        if args.database is not None or args.init_db:
            parser.error("--sandbox-sync cannot be combined with --database or --init-db")
        from zymptom.ui.sandbox_app import run_sandbox
        return run_sandbox()
    if args.init_db:
        path = args.database or Path("zymptom.sqlite3")
        with closing(connect_database(path)) as connection:
            version = schema_version(connection)
        print(f"Database ready: {path} (schema version {version})")
        return 0

    from zymptom.ui.app import run

    return run(args.database)


if __name__ == "__main__":
    raise SystemExit(main())
