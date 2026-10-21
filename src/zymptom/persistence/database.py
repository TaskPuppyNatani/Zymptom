"""Configured SQLite connections and explicit atomic write transactions."""

import sqlite3
from contextlib import contextmanager
from os import PathLike
from typing import Iterator


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Own one write transaction; callers must not nest transaction owners."""
    if connection.in_transaction:
        raise RuntimeError("An existing transaction is already active")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def connect_database(path: str | PathLike[str]) -> sqlite3.Connection:
    """Open a local database, enforce foreign keys, and apply pending migrations.

    The caller owns the returned connection and must close it.
    ``:memory:`` is supported, though file-backed databases are the normal use.
    """
    from .migrations import apply_migrations

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        apply_migrations(connection)
    except BaseException:
        connection.close()
        raise
    return connection
