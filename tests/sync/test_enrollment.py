from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import sqlite3
from uuid import UUID, uuid4

import pytest

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility
from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.persistence.backups import BackupError, content_hash, file_checksum, readonly
from zymptom.persistence.database import transaction
from zymptom.sandbox.isolation import sandbox_database
from zymptom.services.journal import JournalSession
from zymptom.sync.contract import SyncError, legacy_change
from zymptom.sync.service import SyncService
from zymptom.sync.storage import get_head, read_changes, read_enrollment


def insert_untracked(connection, episode: FunctionalEpisode) -> FunctionalEpisode:
    with transaction(connection):
        EpisodeRepository(connection)._apply_sync_snapshot(episode.id, episode)
    return episode


def test_preview_is_read_only_and_reports_counts_and_enrollment_state(tmp_path, full_episode):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    try:
        untracked_one = deepcopy(full_episode)
        untracked_two = replace(
            deepcopy(full_episode),
            id=uuid4(),
            activity="Second pre-sync entry ☕",
            occurred_at=full_episode.occurred_at + timedelta(hours=1),
        )
        insert_untracked(session.connection, untracked_one)
        insert_untracked(session.connection, untracked_two)
        tracked = session.episodes.record(
            activity="Already tracked Z5 entry",
            outcome=EpisodeOutcome.COMPLETED_NORMALLY,
        )
        before_hash = content_hash(session.connection)
        before_checksum = file_checksum(database)
        backups_before = session.backups.list()

        preview = session.inspect_enrollment()
        assert not preview.enrolled and not preview.already_enrolled
        assert preview.state == "not_enrolled"
        assert preview.journal_id is None
        assert preview.enrolled_at is None
        assert preview.entry_count == 3
        assert preview.already_tracked_count == 1
        assert preview.bootstrap_required_count == 2
        assert preview.safety_backup_required is True
        assert content_hash(session.connection) == before_hash
        assert file_checksum(database) == before_checksum
        assert session.backups.list() == backups_before
        assert get_head(session.connection, untracked_one.id) is None
        assert get_head(session.connection, untracked_two.id) is None
        assert get_head(session.connection, tracked.id) is not None
    finally:
        session.close()


def test_bootstrap_preserves_uuid_content_edge_values_and_existing_history(tmp_path, full_episode):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    try:
        edge_episode = FunctionalEpisode(
            id=UUID("12345678-1234-4234-8234-123456789abc"),
            occurred_at=datetime(2026, 2, 14, 9, 30, 15, 123456, tzinfo=UTC),
            activity="Café walk ☕ — multiline\nactivity雪",
            outcome=EpisodeOutcome.COMPLETED_WITH_ADAPTATION,
            duration_minutes=0,
            functional_demands=["walking", "standing", "walking"],
            symptoms=["fatigue", "orthostatic_intolerance"],
            symptom_notes=None,
            adaptations=["sat_down", "took_break"],
            help_level=HelpLevel.NONE,
            help_notes="",
            consequences=["delayed_other_plans"],
            recovery_required=False,
            recovery_actions=["quiet_room", "lay_down", "quiet_room"],
            recovery_minutes=0,
            resume_ability=ResumeAbility.LATER_SAME_DAY,
            notes="Line one ☕\nLine two 雪\n\"Quoted\"",
            created_at=datetime(2026, 2, 14, 9, 45, 0, 654321, tzinfo=UTC),
            updated_at=datetime(2026, 2, 15, 11, 20, 0, 111222, tzinfo=UTC),
        )
        null_episode = FunctionalEpisode(
            id=UUID("87654321-4321-4321-8321-cba987654321"),
            occurred_at=datetime(2026, 2, 16, 14, 0, tzinfo=UTC),
            activity="Minimal pre-sync entry",
            outcome=EpisodeOutcome.AVOIDED_DUE_TO_LIMITATION,
            duration_minutes=None,
            symptom_notes=None,
            help_level=None,
            help_notes=None,
            recovery_required=None,
            recovery_minutes=None,
            resume_ability=None,
            notes=None,
            created_at=datetime(2026, 2, 16, 14, 5, tzinfo=UTC),
            updated_at=datetime(2026, 2, 16, 14, 5, tzinfo=UTC),
        )
        insert_untracked(session.connection, deepcopy(edge_episode))
        insert_untracked(session.connection, deepcopy(null_episode))

        tracked = session.episodes.record(
            activity="Tracked before enrollment",
            outcome=EpisodeOutcome.STOPPED_EARLY,
            notes="Initial revision",
        )
        tracked.notes = "Edited revision before enrollment"
        session.episodes.update(tracked)
        tracked_snapshot = deepcopy(tracked)
        changes_before = read_changes(session.connection)
        assert len(changes_before) == 2

        enrolled_time = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)
        result = session.enroll_sync(now=enrolled_time)
        assert result.entry_count == 3
        assert result.bootstrapped_count == 2
        assert result.already_tracked_count == 1
        assert result.already_enrolled is False
        assert result.enrolled_at == enrolled_time
        assert isinstance(result.journal_id, UUID) and result.journal_id.int != 0
        assert result.journal_id != session._device_id
        assert result.safety_backup is not None and result.safety_backup.is_file()

        loaded_edge = session.episodes.get(edge_episode.id)
        loaded_null = session.episodes.get(null_episode.id)
        loaded_tracked = session.episodes.get(tracked.id)
        assert loaded_edge == edge_episode
        assert loaded_edge.functional_demands == ["walking", "standing", "walking"]
        assert loaded_edge.recovery_actions == ["quiet_room", "lay_down", "quiet_room"]
        assert loaded_edge.duration_minutes == 0 and loaded_edge.recovery_minutes == 0
        assert loaded_edge.recovery_required is False and loaded_edge.symptom_notes is None
        assert loaded_null == null_episode
        assert loaded_tracked == tracked_snapshot

        edge_head = get_head(session.connection, edge_episode.id)
        null_head = get_head(session.connection, null_episode.id)
        assert edge_head == legacy_change(edge_episode)
        assert edge_head.operation == "create" and edge_head.revision == 1 and edge_head.parent_ids == ()
        assert edge_head.episode == edge_episode
        assert null_head == legacy_change(null_episode)
        assert null_head.operation == "create" and null_head.revision == 1 and null_head.parent_ids == ()
        assert null_head.episode == null_episode

        all_changes = read_changes(session.connection)
        assert len(all_changes) == 4
        for change_id, previous_change in changes_before.items():
            assert all_changes[change_id] == previous_change
        assert [c for c in all_changes.values() if c.record_id == edge_episode.id] == [edge_head]
        assert [c for c in all_changes.values() if c.record_id == null_episode.id] == [null_head]

        assert session.connection.execute("SELECT count(*) FROM sync_acknowledgements").fetchone()[0] == 0
        sync = SyncService(session.episodes, before_merge=session.before_destructive_change)
        pending = sync.pending(f"firebase-production:{result.journal_id}")
        assert {c.change_id for c in pending.changes} == set(all_changes)
    finally:
        session.close()


def test_enrollment_is_idempotent_and_preserves_journal_identity_across_restart(tmp_path, full_episode):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    insert_untracked(session.connection, deepcopy(full_episode))
    first = session.enroll_sync()
    changes_after_first = read_changes(session.connection)
    hash_after_first = content_hash(session.connection)

    preview_after = session.inspect_enrollment()
    assert preview_after.enrolled and preview_after.already_enrolled
    assert preview_after.state == "local_only"
    assert preview_after.journal_id == first.journal_id
    assert preview_after.enrolled_at == first.enrolled_at
    assert preview_after.entry_count == 1
    assert preview_after.already_tracked_count == 1
    assert preview_after.bootstrap_required_count == 0
    assert preview_after.safety_backup_required is False

    second = session.enroll_sync()
    assert second.already_enrolled is True
    assert second.journal_id == first.journal_id
    assert second.enrolled_at == first.enrolled_at
    assert second.bootstrapped_count == 0
    assert second.safety_backup is None
    assert read_changes(session.connection) == changes_after_first
    assert content_hash(session.connection) == hash_after_first
    session.close()

    reopened = JournalSession(database)
    try:
        reopened_preview = reopened.inspect_enrollment()
        assert reopened_preview.enrolled is True
        assert reopened_preview.journal_id == first.journal_id
        assert reopened_preview.enrolled_at == first.enrolled_at
        assert reopened_preview.bootstrap_required_count == 0
        assert reopened_preview.already_tracked_count == 1
        assert reopened.episodes.list() == [full_episode]
        sync = SyncService(reopened.episodes, before_merge=reopened.before_destructive_change)
        assert len(sync.pending(f"firebase-production:{first.journal_id}").changes) == 1
    finally:
        reopened.close()


def test_backup_happens_before_mutation_and_backup_failure_aborts_cleanly(tmp_path, full_episode, monkeypatch):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    try:
        insert_untracked(session.connection, deepcopy(full_episode))
        before_hash = content_hash(session.connection)

        monkeypatch.setattr(
            session,
            "before_destructive_change",
            lambda: (_ for _ in ()).throw(BackupError("disk full")),
        )
        with pytest.raises(BackupError, match="disk full"):
            session.enroll_sync()
        assert content_hash(session.connection) == before_hash
        assert read_enrollment(session.connection) is None
        assert read_changes(session.connection) == {}
        assert not session.inspect_enrollment().enrolled
    finally:
        session.close()


@pytest.mark.parametrize("fail_table", ["sync_changes", "sync_heads", "sync_enrollment"])
def test_bootstrap_or_metadata_failure_rolls_back_entire_enrollment(tmp_path, full_episode, fail_table):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    try:
        first_ep = deepcopy(full_episode)
        second_ep = replace(
            deepcopy(full_episode),
            id=uuid4(),
            activity="Second episode",
            occurred_at=first_ep.occurred_at + timedelta(minutes=10),
        )
        insert_untracked(session.connection, first_ep)
        insert_untracked(session.connection, second_ep)
        if fail_table in {"sync_changes", "sync_heads"}:
            target_id = str(second_ep.id)
            session.connection.execute(
                f"CREATE TRIGGER fail_enroll BEFORE INSERT ON {fail_table} "
                f"WHEN NEW.record_id = '{target_id}' "
                "BEGIN SELECT RAISE(ABORT, 'injected enrollment failure'); END"
            )
        else:
            session.connection.execute(
                "CREATE TRIGGER fail_enroll BEFORE INSERT ON sync_enrollment "
                "BEGIN SELECT RAISE(ABORT, 'injected enrollment failure'); END"
            )

        with pytest.raises(sqlite3.IntegrityError, match="injected enrollment failure"):
            session.enroll_sync()

        assert read_enrollment(session.connection) is None
        assert read_changes(session.connection) == {}
        assert get_head(session.connection, first_ep.id) is None
        assert get_head(session.connection, second_ep.id) is None
        assert session.episodes.list() == [first_ep, second_ep]
        safety_backups = [info for info in session.backups.list() if info.kind == "safety" and info.valid]
        assert len(safety_backups) == 1
        with closing(readonly(safety_backups[0].path)) as backup_conn:
            backup_conn.row_factory = sqlite3.Row
            assert EpisodeRepository(backup_conn).list() == [first_ep, second_ep]
            assert read_enrollment(backup_conn) is None
    finally:
        session.close()


@pytest.mark.parametrize("corruption", ["orphan_change", "mismatched_head", "missing_parent", "untracked_after_enrolled"])
def test_inconsistent_partial_z5_history_fails_safely_without_mutating(tmp_path, full_episode, corruption):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    try:
        ep = deepcopy(full_episode)
        insert_untracked(session.connection, ep)
        root = legacy_change(ep)
        if corruption == "orphan_change":
            with transaction(session.connection):
                from zymptom.sync.storage import insert_change
                insert_change(session.connection, root)
        elif corruption == "mismatched_head":
            with transaction(session.connection):
                from zymptom.sync.storage import insert_change, set_head
                insert_change(session.connection, root)
                set_head(session.connection, root)
                session.connection.execute("UPDATE episodes SET activity = 'Tampered without sync revision'")
        elif corruption == "missing_parent":
            with transaction(session.connection):
                from zymptom.sync.contract import Change, canonical
                child = Change.build(
                    record_id=ep.id,
                    episode=replace(ep, notes="Edited", updated_at=ep.updated_at + timedelta(minutes=1)),
                    device_id=session._device_id,
                    operation="update",
                    parents=(root,),
                    updated_at=ep.updated_at + timedelta(minutes=1),
                )
                session.connection.execute(
                    "INSERT INTO sync_changes VALUES (?, ?, ?, ?)",
                    (str(child.change_id), str(child.record_id), child.revision, canonical(child.to_dict())),
                )
                session.connection.execute("INSERT INTO sync_heads VALUES (?, ?)", (str(ep.id), str(child.change_id)))
        elif corruption == "untracked_after_enrolled":
            with transaction(session.connection):
                session.connection.execute(
                    "INSERT INTO sync_enrollment VALUES (1, ?, 'local_only', ?)",
                    (str(uuid4()), "2026-10-27T00:00:00.000000+00:00"),
                )

        before_hash = content_hash(session.connection)
        with pytest.raises(SyncError):
            session.inspect_enrollment()
        with pytest.raises(SyncError):
            session.enroll_sync()
        assert content_hash(session.connection) == before_hash
        assert session.backups.list() == []
    finally:
        session.close()


def test_sandbox_separation_refuses_sandbox_journals_and_keeps_identities_distinct(tmp_path, full_episode):
    prod_path = tmp_path / "normal" / "zymptom.sqlite3"
    prod_path.parent.mkdir(parents=True)
    sandbox_path = sandbox_database(prod_path, root=tmp_path / "data")
    prod_session = JournalSession(prod_path)
    sandbox_session = JournalSession(sandbox_path)
    try:
        insert_untracked(prod_session.connection, deepcopy(full_episode))
        sandbox_ep = sandbox_session.episodes.record(activity="DEMO: sandbox only", outcome=EpisodeOutcome.STOPPED_EARLY)
        with pytest.raises(SyncError, match="Sandbox"):
            sandbox_session.inspect_enrollment()
        with pytest.raises(SyncError, match="Sandbox"):
            sandbox_session.enroll_sync()
        assert read_enrollment(sandbox_session.connection) is None

        prod_result = prod_session.enroll_sync()
        assert prod_result.journal_id != sandbox_ep.id
        assert [e.id for e in prod_session.episodes.list()] == [full_episode.id]
        assert read_enrollment(sandbox_session.connection) is None
    finally:
        prod_session.close()
        sandbox_session.close()


def test_backup_restore_and_recovery_preserve_enrollment_state_and_journal_id(tmp_path, full_episode):
    source_path = tmp_path / "enrolled_source.sqlite3"
    source_session = JournalSession(source_path)
    insert_untracked(source_session.connection, deepcopy(full_episode))
    enrolled = source_session.enroll_sync()
    manual_backup = source_session.backups.create(source_session.connection, "manual")

    source_session.episodes.record(activity="Added after backup", outcome=EpisodeOutcome.STOPPED_EARLY)
    source_session.restore(source_session.prepare_restore(manual_backup.path))
    restored_preview = source_session.inspect_enrollment()
    assert restored_preview.enrolled is True
    assert restored_preview.journal_id == enrolled.journal_id
    assert restored_preview.enrolled_at == enrolled.enrolled_at
    assert restored_preview.bootstrap_required_count == 0
    assert source_session.episodes.list() == [full_episode]
    source_session.close()

    target_path = tmp_path / "target.sqlite3"
    target_session = JournalSession(target_path)
    try:
        assert not target_session.inspect_enrollment().enrolled
        recovery_preview = target_session.inspect_recovery(source_path)
        plan = target_session.prepare_recovery(recovery_preview)
        target_session.restore(plan)
        after_recovery = target_session.inspect_enrollment()
        assert after_recovery.enrolled is True
        assert after_recovery.journal_id == enrolled.journal_id
        assert after_recovery.enrolled_at == enrolled.enrolled_at
        assert after_recovery.entry_count == 1
        assert after_recovery.already_tracked_count == 1
        assert after_recovery.bootstrap_required_count == 0
        assert target_session.episodes.list() == [full_episode]
        sync = SyncService(target_session.episodes, before_merge=target_session.before_destructive_change)
        assert len(sync.pending(f"firebase-production:{enrolled.journal_id}").changes) == 1
    finally:
        target_session.close()
