"""Minimal database initialization command; Z0 has no GUI."""

import argparse
from contextlib import closing
from pathlib import Path

from zymptom import __version__
from zymptom.persistence.database import connect_database
from zymptom.persistence.migrations import schema_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize or migrate a local Zymptom database.")
    parser.add_argument("--version", action="version", version=f"Zymptom {__version__}")
    parser.add_argument("--database", type=Path, default=Path("zymptom.sqlite3"))
    args = parser.parse_args(argv)
    with closing(connect_database(args.database)) as connection:
        version = schema_version(connection)
    print(f"Database ready: {args.database} (schema version {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
