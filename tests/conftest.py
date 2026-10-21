from datetime import UTC, datetime

import pytest

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility
from zymptom.persistence import EpisodeRepository, connect_database


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
