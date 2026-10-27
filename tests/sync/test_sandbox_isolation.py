import os
from pathlib import Path

import pytest

from zymptom.sandbox.isolation import IsolationError, sandbox_database


def test_dedicated_database_is_owned_and_reopened(tmp_path):
    production = tmp_path / "normal" / "zymptom.sqlite3"
    path = sandbox_database(production, root=tmp_path / "data")
    assert path == (tmp_path / "data/zymptom/sandbox-sync/journal.sqlite3").resolve()
    assert path.exists() and not production.exists()
    assert sandbox_database(production, root=tmp_path / "data") == path


def test_refuses_existing_unowned_file_and_default_production_alias(tmp_path):
    root = tmp_path / "data"
    directory = root / "zymptom/sandbox-sync"
    directory.mkdir(parents=True)
    database = directory / "journal.sqlite3"
    database.write_bytes(b"production sentinel")
    with pytest.raises(IsolationError):
        sandbox_database(tmp_path / "normal.sqlite3", root=root)
    with pytest.raises(IsolationError):
        sandbox_database(database, root=root)
    assert database.read_bytes() == b"production sentinel"


def test_refuses_symlink_and_hardlink_aliases(tmp_path):
    root = tmp_path / "data"
    directory = root / "zymptom/sandbox-sync"
    directory.mkdir(parents=True)
    production = tmp_path / "production.sqlite3"
    production.write_bytes(b"unchanged")
    database = directory / "journal.sqlite3"
    database.symlink_to(production)
    with pytest.raises(IsolationError):
        sandbox_database(production, root=root)
    database.unlink()
    os.link(production, database)
    with pytest.raises(IsolationError):
        sandbox_database(production, root=root)
    assert production.read_bytes() == b"unchanged"


def test_replaced_sandbox_file_is_rejected(tmp_path):
    root = tmp_path / "data"
    database = sandbox_database(tmp_path / "production.sqlite3", root=root)
    moved = database.with_name("old")
    database.rename(moved)
    database.write_bytes(b"replacement")
    with pytest.raises(IsolationError):
        sandbox_database(tmp_path / "production.sqlite3", root=root)
    assert database.read_bytes() == b"replacement"


def test_explicit_mode_rejects_database_or_init_before_opening(tmp_path, monkeypatch):
    from zymptom.__main__ import main
    called = []
    monkeypatch.setattr("zymptom.persistence.database.connect_database", lambda *_: called.append(True))
    for arguments in (["--sandbox-sync", "--database", str(tmp_path / "normal.sqlite3")], ["--sandbox-sync", "--init-db"]):
        with pytest.raises(SystemExit):
            main(arguments)
    assert not called
