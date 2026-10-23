from pathlib import Path

import pytest

from zymptom.exports.files import ExportWriteError, write_atomically


def test_atomic_writer_creates_and_overwrites_export(tmp_path):
    destination = tmp_path / "report.json"

    write_atomically(destination, "first ☕\n")
    assert destination.read_text(encoding="utf-8") == "first ☕\n"
    write_atomically(destination, "second\n")
    assert destination.read_text(encoding="utf-8") == "second\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_replace_failure_preserves_existing_file_and_cleans_temporary_file(tmp_path, monkeypatch):
    destination = tmp_path / "report.csv"
    destination.write_text("valid existing contents\n", encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr("zymptom.exports.files.os.replace", fail_replace)
    with pytest.raises(ExportWriteError, match="simulated replacement failure"):
        write_atomically(destination, "new contents\n")

    assert destination.read_text(encoding="utf-8") == "valid existing contents\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_invalid_parent_surfaces_write_error(tmp_path):
    destination = Path(tmp_path / "missing" / "report.md")

    with pytest.raises(ExportWriteError, match="Could not write export file"):
        write_atomically(destination, "report")


def test_encoding_failure_preserves_existing_file_and_removes_temporary_file(tmp_path):
    destination = tmp_path / "report.txt"
    destination.write_text("existing report", encoding="utf-8")

    with pytest.raises(UnicodeEncodeError):
        write_atomically(destination, "unpaired surrogate: \ud800")

    assert destination.read_text(encoding="utf-8") == "existing report"
    assert list(tmp_path.iterdir()) == [destination]
