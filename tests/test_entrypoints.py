import subprocess
import sys
from contextlib import closing

import pytest

from zymptom.domain import EpisodeOutcome, ValidationError
from zymptom.persistence import connect_database
from zymptom.persistence.migrations import MIGRATIONS, schema_version
from zymptom.services import EpisodeService


def test_service_episode_lifecycle(repository):
    service = EpisodeService(repository)
    episode = service.record(
        activity="Walk", outcome=EpisodeOutcome.COMPLETED_WITH_ADAPTATION,
        adaptations=["shorter_route"], duration_minutes=10,
    )
    assert service.get(episode.id) == episode
    episode.notes = "Took the shorter route."
    service.update(episode)
    assert service.list() == [episode]
    assert service.delete(episode.id)
    assert service.get(episode.id) is None


def test_service_rejects_invalid_record_without_writing(repository):
    service = EpisodeService(repository)
    with pytest.raises(ValidationError):
        service.record(activity=" ", outcome=EpisodeOutcome.STOPPED_EARLY)
    assert service.list() == []


def test_module_initializes_and_reopens_database(tmp_path):
    database_path = tmp_path / "cli.sqlite3"
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "zymptom", "--init-db", "--database", str(database_path)],
            check=True, capture_output=True, text=True,
        )
        assert f"schema version {MIGRATIONS[-1].version}" in result.stdout
    with closing(connect_database(database_path)) as connection:
        assert schema_version(connection) == MIGRATIONS[-1].version


def test_module_help_and_version_have_no_database_side_effects(tmp_path):
    for option in ("--help", "--version"):
        result = subprocess.run(
            [sys.executable, "-m", "zymptom", option], cwd=tmp_path,
            check=True, capture_output=True, text=True,
        )
        assert result.stdout
    assert list(tmp_path.iterdir()) == []
