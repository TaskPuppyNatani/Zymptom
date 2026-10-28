from copy import deepcopy
from datetime import UTC, datetime

import pytest

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility
from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.sandbox import auth as auth_module


@pytest.fixture(autouse=True)
def isolated_installation(tmp_path, monkeypatch):
    """No test may create installation identity/config in the real user home."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "installation-config"))
    monkeypatch.setattr(auth_module, "_DEFAULT_CREDENTIAL_STORE", auth_module.MemoryCredentialStore())


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "journal.sqlite3"


@pytest.fixture
def connection(database_path):
    connection = connect_database(database_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def repository(connection):
    return EpisodeRepository(connection)


@pytest.fixture
def full_episode():
    return FunctionalEpisode(
        activity="Homework — reading and writing",
        outcome=EpisodeOutcome.STOPPED_EARLY,
        occurred_at=datetime(2026, 10, 20, 15, 30, 12, 345678, tzinfo=UTC),
        duration_minutes=32,
        functional_demands=["sitting", "concentration", "sitting"],
        symptoms=["fatigue", "pain"],
        symptom_notes="Fatigue increased after reading.\nPain in back.",
        adaptations=["changed_position", "reduced_task"],
        help_level=HelpLevel.SOME,
        help_notes="Needed help carrying books.",
        consequences=["missed_next_activity", "needed_rest"],
        recovery_required=True,
        recovery_actions=["lay_down", "quiet_room"],
        recovery_minutes=45,
        resume_ability=ResumeAbility.LATER_SAME_DAY,
        notes="Resumed later; kept original notes: '☕'.",
        created_at=datetime(2026, 10, 20, 16, tzinfo=UTC),
        updated_at=datetime(2026, 10, 20, 16, 1, tzinfo=UTC),
    )


@pytest.fixture
def legacy_episode(repository, connection, full_episode):
    """Seed old duplicate child rows without using today's normalized writes."""
    episode = deepcopy(full_episode)
    fields = ("functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions")
    for field in fields:
        values = getattr(episode, field)
        values.extend([values[-1], values[0]])
    repository.create(deepcopy(episode))
    for field in fields:
        connection.execute(f"DELETE FROM episode_{field} WHERE episode_id = ?", (str(episode.id),))
        connection.executemany(
            f"INSERT INTO episode_{field} (episode_id, position, tag) VALUES (?, ?, ?)",
            [(str(episode.id), position, value) for position, value in enumerate(getattr(episode, field))],
        )
    return episode
