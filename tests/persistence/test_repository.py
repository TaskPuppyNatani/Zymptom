import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility, ValidationError
from zymptom.persistence import EpisodeNotFoundError, EpisodeRepository, connect_database

CHILD_TABLES = (
    "episode_functional_demands", "episode_symptoms", "episode_adaptations",
    "episode_consequences", "episode_recovery_actions",
)


def test_critical_acceptance_round_trip(repository):
    episode = FunctionalEpisode(
        activity="Homework",
        outcome=EpisodeOutcome.STOPPED_EARLY,
        duration_minutes=32,
        functional_demands=["sitting", "concentration"],
        adaptations=["changed_position"],
        recovery_required=True,
        recovery_minutes=45,
        resume_ability=ResumeAbility.LATER_SAME_DAY,
    )
    repository.create(episode)
    assert repository.get(episode.id) == episode


def test_complete_round_trip_survives_reopen(repository, full_episode, database_path):
    assert repository.create(full_episode) is full_episode
    with closing(connect_database(database_path)) as reopened:
        assert EpisodeRepository(reopened).get(str(full_episode.id)) == full_episode


@pytest.mark.parametrize("recovery_required", [None, False, True])
def test_optional_fields_round_trip(repository, recovery_required):
    episode = FunctionalEpisode(
        activity="Rest", outcome=EpisodeOutcome.COMPLETED_NORMALLY,
        recovery_required=recovery_required,
    )
    repository.create(episode)
    loaded = repository.get(episode.id)
    assert loaded == episode
    assert loaded.recovery_required is recovery_required


def test_enums_store_machine_values(repository, connection, full_episode):
    full_episode.help_level = HelpLevel.NONE
    repository.create(full_episode)
    row = connection.execute("SELECT outcome, help_level, resume_ability FROM episodes").fetchone()
    assert tuple(row) == ("stopped_early", "none", "later_same_day")
    assert repository.get(full_episode.id).help_level is HelpLevel.NONE


@pytest.mark.parametrize("field", ["functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions"])
def test_tags_keep_order_duplicates_and_text(repository, full_episode, field):
    values = ["z-last", "a-first", "z-last", "café ☕ 'quoted'\nnew line"]
    setattr(full_episode, field, values)
    repository.create(full_episode)
    assert getattr(repository.get(full_episode.id), field) == values


def test_update_round_trip_preserves_creation_and_advances_update(repository, full_episode):
    repository.create(full_episode)
    original_created, original_updated = full_episode.created_at, full_episode.updated_at
    full_episode.activity = "Cooking"
    full_episode.outcome = EpisodeOutcome.COMPLETED_WITH_HELP
    full_episode.occurred_at += timedelta(days=1)
    full_episode.duration_minutes = 15
    full_episode.functional_demands = ["standing"]
    full_episode.symptoms = []
    full_episode.symptom_notes = None
    full_episode.adaptations = ["sat_on_stool"]
    full_episode.help_level = HelpLevel.OTHER_PERSON_COMPLETED_PART
    full_episode.help_notes = "Someone chopped vegetables."
    full_episode.consequences = []
    full_episode.recovery_required = False
    full_episode.recovery_actions = []
    full_episode.recovery_minutes = 0
    full_episode.resume_ability = ResumeAbility.IMMEDIATELY
    full_episode.notes = "Finished."
    full_episode.created_at += timedelta(days=100)

    assert repository.update(full_episode) is full_episode
    assert full_episode.created_at == original_created
    assert full_episode.updated_at > original_updated
    assert repository.get(full_episode.id) == full_episode


def test_update_timestamp_advances_if_clock_is_behind(repository, full_episode):
    full_episode.updated_at = datetime(2100, 1, 1, tzinfo=UTC)
    repository.create(full_episode)
    previous = full_episode.updated_at
    repository.update(full_episode)
    assert full_episode.updated_at == previous + timedelta(microseconds=1)
    assert repository.get(full_episode.id) == full_episode


def test_delete(repository, full_episode):
    repository.create(full_episode)
    assert repository.delete(str(full_episode.id)) is True
    assert repository.get(full_episode.id) is None
    assert repository.list() == []
    assert repository.delete(full_episode.id) is False


def test_cascade_deletes_children_even_with_direct_sql(repository, connection, full_episode):
    repository.create(full_episode)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    for table in CHILD_TABLES:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DELETE FROM episodes WHERE id = ?", (str(full_episode.id),))
    connection.commit()
    for table in CHILD_TABLES:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_foreign_keys_reject_orphan_tags(connection):
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO episode_symptoms (episode_id, position, tag) VALUES (?, 0, 'pain')",
            (str(uuid4()),),
        )


def test_missing_read_and_update(repository, full_episode):
    assert repository.get(full_episode.id) is None
    with pytest.raises(EpisodeNotFoundError):
        repository.update(full_episode)
    assert repository.list() == []


def test_duplicate_create_does_not_change_existing_record(repository, full_episode):
    repository.create(full_episode)
    duplicate = deepcopy(full_episode)
    duplicate.activity = "Should not overwrite"
    with pytest.raises(sqlite3.IntegrityError):
        repository.create(duplicate)
    assert repository.get(full_episode.id) == full_episode


def test_deterministic_chronological_list(repository):
    when = datetime(2026, 10, 20, 12, tzinfo=UTC)
    later = FunctionalEpisode(id=UUID(int=3), activity="Later", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=when + timedelta(hours=1))
    tie_high = FunctionalEpisode(id=UUID(int=2), activity="Tie high", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=when)
    # This timezone offset denotes the same instant as `when`.
    tie_low = FunctionalEpisode(id=UUID(int=1), activity="Tie low", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=when.astimezone(timezone(timedelta(hours=-7))))
    for episode in (later, tie_high, tie_low):
        repository.create(episode)
    assert repository.list() == [tie_low, tie_high, later]
    assert repository.list() == [tie_low, tie_high, later]


@pytest.mark.parametrize("field,value", [("activity", " "), ("duration_minutes", -1), ("outcome", "invalid"), ("symptoms", [""])])
def test_mutated_records_are_revalidated_before_writes(repository, full_episode, field, value):
    repository.create(full_episode)
    original = deepcopy(full_episode)
    setattr(full_episode, field, value)
    with pytest.raises(ValidationError):
        repository.update(full_episode)
    assert repository.get(original.id) == original


def fail_on_recovery_action(connection):
    # This failure occurs after the scalar write and four other tag tables.
    connection.execute("""
        CREATE TRIGGER reject_recovery_action
        BEFORE INSERT ON episode_recovery_actions
        WHEN NEW.tag = 'reject_me'
        BEGIN
            SELECT RAISE(ABORT, 'injected tag failure');
        END
    """)


def test_create_rolls_back_all_partial_writes(repository, connection, full_episode):
    fail_on_recovery_action(connection)
    full_episode.recovery_actions.append("reject_me")
    with pytest.raises(sqlite3.IntegrityError, match="injected tag failure"):
        repository.create(full_episode)
    assert not connection.in_transaction
    assert repository.get(full_episode.id) is None
    for table in CHILD_TABLES:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    # The same connection remains usable after rollback.
    full_episode.recovery_actions.remove("reject_me")
    repository.create(full_episode)
    assert repository.get(full_episode.id) == full_episode


def test_update_rolls_back_scalar_and_all_tag_replacements(repository, connection, full_episode):
    repository.create(full_episode)
    original = deepcopy(full_episode)
    fail_on_recovery_action(connection)
    full_episode.activity = "Changed activity"
    full_episode.symptoms = ["different symptom"]
    full_episode.recovery_actions = ["reject_me"]
    with pytest.raises(sqlite3.IntegrityError, match="injected tag failure"):
        repository.update(full_episode)
    assert not connection.in_transaction
    assert full_episode.updated_at == original.updated_at
    assert full_episode.created_at == original.created_at
    assert repository.get(full_episode.id) == original


def test_delete_failure_rolls_back_cascading_changes(repository, connection, full_episode):
    repository.create(full_episode)
    connection.execute("""
        CREATE TRIGGER reject_child_delete BEFORE DELETE ON episode_recovery_actions
        BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="injected delete failure"):
        repository.delete(full_episode.id)
    assert not connection.in_transaction
    assert repository.get(full_episode.id) == full_episode


def test_write_does_not_commit_an_existing_transaction(repository, connection, full_episode):
    connection.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="already active"):
            repository.create(full_episode)
        assert connection.in_transaction
    finally:
        connection.rollback()
    assert repository.get(full_episode.id) is None
