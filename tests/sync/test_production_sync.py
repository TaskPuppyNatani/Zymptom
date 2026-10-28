from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import os
import time
from uuid import UUID, uuid4

import pytest

from zymptom.domain import EpisodeOutcome
from zymptom.persistence import EpisodeRepository
from zymptom.persistence.backups import BackupError, content_hash, readonly
from zymptom.persistence.database import transaction
from zymptom.sandbox.auth import DesktopConfig, FirebaseSession, HttpError, firebase_exchange, read_config, request_json
from zymptom.sandbox.sync import sync_disposable
from zymptom.sandbox.transport import FirestoreMailbox, IdentityCollision, TransportError
from zymptom.services.journal import JournalSession, production_provider, sync_production
from zymptom.sync.contract import Change, SyncBatch, SyncError, canonical
from zymptom.sync.service import SyncService
from zymptom.sync.storage import get_head, read_changes, read_enrollment


def insert_untracked(connection, episode):
    with transaction(connection):
        EpisodeRepository(connection)._apply_sync_snapshot(episode.id, episode)
    return episode


class FakeProductionFirestore:
    def __init__(self):
        self.documents = {}
        self.descriptors = {}
        self.requests = []
        self.fail_status = None
        self.fail_on_upload_index = None
        self.uploads_attempted = 0

    def __call__(self, url, *, headers, data=None):
        assert headers["Authorization"].startswith("Bearer ")
        self.requests.append((url, json.loads(data) if data else None))
        if self.fail_status is not None:
            raise HttpError(self.fail_status)
        if data:
            write = json.loads(data)["writes"][0]
            assert write["currentDocument"] == {"exists": False}
            document = write["update"]
            key = document["name"]
            if "/changes/" in key:
                self.uploads_attempted += 1
                if self.fail_on_upload_index is not None and self.uploads_attempted == self.fail_on_upload_index:
                    raise HttpError(503)
                if key in self.documents:
                    raise HttpError(409)
                if "updateTransforms" in write:
                    assert write["updateTransforms"] == [{"fieldPath": "uploaded_at", "setToServerValue": "REQUEST_TIME"}]
                    document["fields"]["uploaded_at"] = {"timestampValue": "2026-10-27T18:00:00Z"}
                self.documents[key] = document
            else:
                if key in self.descriptors:
                    raise HttpError(409)
                if "updateTransforms" in write:
                    assert write["updateTransforms"] == [{"fieldPath": "published_at", "setToServerValue": "REQUEST_TIME"}]
                    document["fields"]["published_at"] = {"timestampValue": "2026-10-27T18:00:00Z"}
                self.descriptors[key] = document
            return {}
        path = url.split("/v1/", 1)[1].split("?", 1)[0]
        if "?" in url:
            query = url.split("?", 1)[1]
            items = [value for key, value in sorted(self.documents.items()) if key.startswith(path + "/")]
            if "pageSize=1" in query:
                token = None
                for part in query.split("&"):
                    if part.startswith("pageToken="):
                        token = int(part.split("=", 1)[1])
                idx = token or 0
                page_items = items[idx:idx + 1]
                next_token = str(idx + 1) if idx + 1 < len(items) else None
                res = {"documents": page_items}
                if next_token is not None:
                    res["nextPageToken"] = next_token
                return res
            return {"documents": items}
        if path in self.descriptors:
            return self.descriptors[path]
        if path not in self.documents:
            raise HttpError(404)
        return self.documents[path]


def make_auth(uid="prod-user-1", token="secret-firebase-jwt-token", expires_offset=3600):
    return FirebaseSession(uid, token, time.monotonic() + expires_offset, "user@example.test")


def test_unenrolled_journal_cannot_production_sync_and_enrolled_requires_first_sync_approval(tmp_path, full_episode):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    fake = FakeProductionFirestore()
    try:
        insert_untracked(session.connection, deepcopy(full_episode))
        preview = session.inspect_production_sync()
        assert not preview.enrolled
        assert preview.journal_id is None

        dummy_mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=uuid4())
        with pytest.raises(SyncError, match="Prepare this journal"):
            session.sync_production(dummy_mailbox, approve_first_sync=True)
        assert fake.requests == []

        enrollment = session.enroll_sync()
        ready = session.inspect_production_sync()
        assert ready.enrolled
        assert ready.journal_id == enrollment.journal_id
        assert ready.entry_count == 1
        assert ready.pending_change_count == 1
        assert ready.acknowledged_change_count == 0
        assert ready.first_sync_required is True

        # Constructing auth and mailbox alone never contacts Firestore or uploads
        mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        assert fake.requests == []

        # Calling sync_production without explicit first-sync approval fails before any network request
        with pytest.raises(SyncError, match="Confirm the first production sync"):
            session.sync_production(mailbox, approve_first_sync=False)
        assert fake.requests == []
    finally:
        session.close()


def test_production_mailbox_uses_persisted_journal_id_and_rejects_sandbox_mailbox(tmp_path, full_episode):
    session = JournalSession(tmp_path / "production.sqlite3")
    fake = FakeProductionFirestore()
    try:
        session.episodes.repository.create(deepcopy(full_episode))
        enrollment = session.enroll_sync()

        # Sandbox mailbox (without explicit production journal_id) has identity_path set and must be rejected
        sandbox_mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake)
        with pytest.raises(SyncError, match="cannot use the disposable sandbox mailbox"):
            session.sync_production(sandbox_mailbox, approve_first_sync=True)
        assert fake.requests == []

        # Mailbox with mismatched journal_id must be rejected
        wrong_mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=uuid4())
        with pytest.raises(SyncError, match="does not match this production journal"):
            session.sync_production(wrong_mailbox, approve_first_sync=True)
        assert fake.requests == []

        # Production mailbox uses the enrolled production journal_id and never touches sandbox/identity
        prod_mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        result = session.sync_production(prod_mailbox, approve_first_sync=True)
        assert result.journal_id == enrollment.journal_id
        assert result.mailbox_state == "empty"
        assert len(result.uploaded) == 1
        assert result.up_to_date is True
        urls = [url for url, _ in fake.requests]
        assert all("sandbox/identity" not in url for url in urls)
        assert any(f"users/prod-user-1/journals/{enrollment.journal_id}/changes/" in url for url in urls)
    finally:
        session.close()


def test_initial_upload_to_empty_mailbox_preserves_entries_acknowledges_and_is_idempotent(tmp_path, full_episode):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    fake = FakeProductionFirestore()
    try:
        ep1 = deepcopy(full_episode)
        ep2 = replace(deepcopy(full_episode), id=uuid4(), activity="Second entry", occurred_at=full_episode.occurred_at + timedelta(hours=1))
        insert_untracked(session.connection, ep1)
        insert_untracked(session.connection, ep2)
        enrollment = session.enroll_sync()
        backups_after_enroll = len(session.backups.list())

        mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        result = sync_production(session, mailbox, approve_first_sync=True)

        assert result.mailbox_state == "empty"
        assert len(result.uploaded) == 2
        assert result.conflicts == 0
        assert result.up_to_date is True
        assert result.safety_backup is not None and result.safety_backup.is_file()
        assert len(session.backups.list()) == backups_after_enroll + 1

        # Empty mailbox never deletes local entries
        assert session.episodes.list() == [ep1, ep2]

        # Provider acknowledgements persisted under firebase-production:<journal_id>
        status = session.inspect_production_sync()
        assert status.pending_change_count == 0
        assert status.acknowledged_change_count == 2
        assert status.first_sync_required is False

        # Duplicate upload is idempotent and subsequent sync does not re-upload
        change = read_changes(session.connection)[result.uploaded[0]]
        assert mailbox.upload(change) == "already-uploaded"
        second = sync_production(session, mailbox)
        assert second.mailbox_state == "identical"
        assert second.uploaded == ()
        assert second.up_to_date is True
    finally:
        session.close()


def test_partial_upload_failure_leaves_remaining_changes_pending_and_preserves_succeeded_acks(tmp_path, full_episode):
    session = JournalSession(tmp_path / "production.sqlite3")
    fake = FakeProductionFirestore()
    try:
        ep1 = deepcopy(full_episode)
        ep2 = replace(deepcopy(full_episode), id=uuid4(), activity="Second", occurred_at=full_episode.occurred_at + timedelta(hours=1))
        insert_untracked(session.connection, ep1)
        insert_untracked(session.connection, ep2)
        enrollment = session.enroll_sync()
        fake.fail_on_upload_index = 2

        mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        with pytest.raises(TransportError, match="unavailable"):
            session.sync_production(mailbox, approve_first_sync=True)

        status = session.inspect_production_sync()
        assert status.acknowledged_change_count == 1
        assert status.pending_change_count == 1

        # Retry completes the remaining pending upload
        fake.fail_on_upload_index = None
        result = session.sync_production(mailbox)
        assert len(result.uploaded) == 1
        assert result.up_to_date is True
        assert session.inspect_production_sync().pending_change_count == 0
    finally:
        session.close()


def test_same_id_different_payload_collision_fails_safely(tmp_path, full_episode):
    session = JournalSession(tmp_path / "production.sqlite3")
    fake = FakeProductionFirestore()
    try:
        session.episodes.repository.create(deepcopy(full_episode))
        enrollment = session.enroll_sync()
        change = next(iter(read_changes(session.connection).values()))

        # Seed remote mailbox with same change_id but different episode notes
        altered_ep = replace(change.episode, notes="Colliding remote payload")
        colliding = Change.build(
            record_id=change.record_id,
            episode=altered_ep,
            device_id=change.device_id,
            operation="create",
            updated_at=change.updated_at,
            change_id=change.change_id,
        )
        mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        assert mailbox.upload(colliding) == "uploaded"

        # Upload collision directly raises IdentityCollision
        with pytest.raises(IdentityCollision):
            mailbox.upload(change)

        # Full sync_production catches the collision during pre-upload check and aborts safely
        before = content_hash(session.connection)
        with pytest.raises(SyncError, match="reused with different contents"):
            session.sync_production(mailbox, approve_first_sync=True)
        assert content_hash(session.connection) == before
        assert session.inspect_production_sync().acknowledged_change_count == 0
    finally:
        session.close()


def test_existing_remote_mailbox_compatible_descendants_independent_creates_and_tombstones(tmp_path, full_episode):
    fake = FakeProductionFirestore()
    desktop_a = JournalSession(tmp_path / "desktop_a.sqlite3")
    desktop_b = JournalSession(tmp_path / "desktop_b.sqlite3")
    try:
        shared = deepcopy(full_episode)
        to_delete = replace(deepcopy(full_episode), id=uuid4(), activity="Will be deleted", occurred_at=full_episode.occurred_at + timedelta(hours=1))
        insert_untracked(desktop_a.connection, deepcopy(shared))
        insert_untracked(desktop_a.connection, deepcopy(to_delete))
        enrollment = desktop_a.enroll_sync()

        # Clone desktop_a into desktop_b so both represent the same enrolled production journal
        backup_path = tmp_path / "clone.sqlite3"
        desktop_a.create_manual_backup(backup_path)
        desktop_b.restore(desktop_b.prepare_recovery(desktop_b.inspect_recovery(backup_path)))
        assert desktop_b.journal_id == enrollment.journal_id

        # On desktop_b: update `shared`, delete `to_delete`, and create an independent episode
        updated_shared = desktop_b.episodes.get(shared.id)
        updated_shared.notes = "Updated remotely on B"
        desktop_b.episodes.update(updated_shared)
        desktop_b.episodes.delete(to_delete.id)
        independent = desktop_b.episodes.record(activity="Independent remote entry", outcome=EpisodeOutcome.COMPLETED_NORMALLY)

        mailbox_b = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        res_b = desktop_b.sync_production(mailbox_b, approve_first_sync=True)
        assert res_b.up_to_date is True

        # Now desktop_a connects to the already-populated production mailbox for the first time
        mailbox_a = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        res_a = desktop_a.sync_production(mailbox_a, approve_first_sync=True)
        assert res_a.mailbox_state == "compatible"
        assert res_a.conflicts == 0
        assert res_a.up_to_date is True
        assert desktop_a.episodes.get(shared.id).notes == "Updated remotely on B"
        assert desktop_a.episodes.get(to_delete.id) is None
        assert desktop_a.episodes.get(independent.id) is not None

        # Stale ancestor cannot resurrect tombstone on subsequent sync
        res_again = desktop_a.sync_production(mailbox_a)
        assert desktop_a.episodes.get(to_delete.id) is None
        assert res_again.up_to_date is True
    finally:
        desktop_a.close()
        desktop_b.close()


@pytest.mark.parametrize("delete_remote", [False, True])
def test_existing_remote_mailbox_preserves_edit_edit_and_delete_edit_conflicts(tmp_path, full_episode, delete_remote):
    fake = FakeProductionFirestore()
    desktop_a = JournalSession(tmp_path / "desktop_a.sqlite3")
    desktop_b = JournalSession(tmp_path / "desktop_b.sqlite3")
    try:
        insert_untracked(desktop_a.connection, deepcopy(full_episode))
        enrollment = desktop_a.enroll_sync()
        clone_path = tmp_path / "clone.sqlite3"
        desktop_a.create_manual_backup(clone_path)
        desktop_b.restore(desktop_b.prepare_recovery(desktop_b.inspect_recovery(clone_path)))

        # Divergent changes on A and B
        local_ep = desktop_a.episodes.get(full_episode.id)
        local_ep.notes = "Local desktop edit"
        desktop_a.episodes.update(local_ep)

        if delete_remote:
            desktop_b.episodes.delete(full_episode.id)
        else:
            remote_ep = desktop_b.episodes.get(full_episode.id)
            remote_ep.notes = "Remote edit"
            desktop_b.episodes.update(remote_ep)

        mailbox_b = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        desktop_b.sync_production(mailbox_b, approve_first_sync=True)

        mailbox_a = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        res_a = desktop_a.sync_production(mailbox_a, approve_first_sync=True)
        assert res_a.mailbox_state == "conflicting"
        assert res_a.conflicts == 1
        assert res_a.up_to_date is False

        service_a = SyncService(desktop_a.episodes, before_merge=desktop_a.before_destructive_change)
        conflicts = service_a.unresolved_conflicts()
        assert len(conflicts) == 1
        assert len(conflicts[0].candidates) == 2

        # Resolve conflict on A and sync resolution to B
        if delete_remote:
            chosen = next(c for c in conflicts[0].candidates if c.deleted)
        else:
            chosen = next(c for c in conflicts[0].candidates if c.episode and c.episode.notes == "Local desktop edit")
        resolution = service_a.resolve(full_episode.id, chosen_change_id=chosen.change_id)
        assert resolution.operation == "resolve"
        res_a2 = desktop_a.sync_production(mailbox_a)
        assert res_a2.conflicts == 0 and res_a2.up_to_date is True

        res_b2 = desktop_b.sync_production(mailbox_b)
        assert res_b2.conflicts == 0 and res_b2.up_to_date is True
        if delete_remote:
            assert desktop_b.episodes.get(full_episode.id) is None
        else:
            assert desktop_b.episodes.get(full_episode.id).notes == "Local desktop edit"
    finally:
        desktop_a.close()
        desktop_b.close()


def test_malformed_remote_history_aborts_before_mutating_or_uploading(tmp_path, full_episode):
    fake = FakeProductionFirestore()
    session = JournalSession(tmp_path / "production.sqlite3")
    try:
        insert_untracked(session.connection, deepcopy(full_episode))
        enrollment = session.enroll_sync()
        mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)

        # Inject a malformed document directly into fake Firestore
        bad_id = str(uuid4())
        doc_name = f"{mailbox.root}/users/prod-user-1/journals/{enrollment.journal_id}/changes/{bad_id}"
        fake.documents[doc_name] = {
            "name": doc_name,
            "fields": {
                "owner_uid": {"stringValue": "prod-user-1"},
                "journal_id": {"stringValue": str(enrollment.journal_id)},
                "change_id": {"stringValue": bad_id},
                "record_id": {"stringValue": str(full_episode.id)},
                "contract_version": {"integerValue": "1"},
                "record_schema_version": {"integerValue": "1"},
                "content_hash": {"stringValue": "0" * 64},
                "canonical_payload_json": {"stringValue": "{malformed-json"},
                "uploaded_at": {"timestampValue": "2026-10-27T18:00:00Z"},
            },
        }
        before = content_hash(session.connection)
        with pytest.raises(TransportError, match="canonical"):
            session.sync_production(mailbox, approve_first_sync=True)
        assert content_hash(session.connection) == before
        assert fake.uploads_attempted == 0
        assert session.inspect_production_sync().acknowledged_change_count == 0
    finally:
        session.close()


def test_backup_failure_aborts_first_sync_and_incoming_merge_without_mutating(tmp_path, full_episode, monkeypatch):
    fake = FakeProductionFirestore()
    session_a = JournalSession(tmp_path / "a.sqlite3")
    session_b = JournalSession(tmp_path / "b.sqlite3")
    try:
        insert_untracked(session_a.connection, deepcopy(full_episode))
        enrollment = session_a.enroll_sync()
        clone_path = tmp_path / "clone.sqlite3"
        session_a.create_manual_backup(clone_path)
        session_b.restore(session_b.prepare_recovery(session_b.inspect_recovery(clone_path)))

        def fail_backup():
            raise BackupError("disk full")

        # 1. Backup failure before first production sync (empty mailbox) aborts before uploading
        monkeypatch.setattr(session_a, "before_destructive_change", fail_backup)
        mailbox_a = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        before_a = content_hash(session_a.connection)
        with pytest.raises(BackupError, match="disk full"):
            session_a.sync_production(mailbox_a, approve_first_sync=True)
        assert fake.uploads_attempted == 0
        assert content_hash(session_a.connection) == before_a

        # Allow session_b to upload a new change so the mailbox is non-empty
        session_b.episodes.record(activity="Remote entry", outcome=EpisodeOutcome.STOPPED_EARLY)
        mailbox_b = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        session_b.sync_production(mailbox_b, approve_first_sync=True)

        # 2. Backup failure before incoming merge leaves session_a untouched
        uploads_before = fake.uploads_attempted
        with pytest.raises(BackupError, match="disk full"):
            session_a.sync_production(mailbox_a, approve_first_sync=True)
        assert fake.uploads_attempted == uploads_before
        assert content_hash(session_a.connection) == before_a
        assert len(session_a.episodes.list()) == 1
    finally:
        session_a.close()
        session_b.close()


def test_manual_full_backup_and_recovery_preserve_production_sync_state(tmp_path, full_episode):
    fake = FakeProductionFirestore()
    source = JournalSession(tmp_path / "source.sqlite3")
    target = JournalSession(tmp_path / "target.sqlite3")
    try:
        insert_untracked(source.connection, deepcopy(full_episode))
        enrollment = source.enroll_sync()
        mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        res = source.sync_production(mailbox, approve_first_sync=True)
        assert res.up_to_date is True

        # Ensure token is never stored in the SQLite database dump
        dump_text = "\n".join(source.connection.iterdump())
        assert "secret-firebase-jwt-token" not in dump_text

        backup_file = tmp_path / "manual-full-backup.sqlite3"
        assert source.create_manual_backup(backup_file) == 1
        preview = target.inspect_recovery(backup_file)
        target.restore(target.prepare_recovery(preview))

        # Recovered journal retains production journal_id, Z5 history, and provider acknowledgements
        target_status = target.inspect_production_sync()
        assert target_status.enrolled is True
        assert target_status.journal_id == enrollment.journal_id
        assert target_status.acknowledged_change_count == 1
        assert target_status.pending_change_count == 0
        assert target_status.first_sync_required is False

        # Reconnecting recovered journal uses same journal_id and performs zero uploads
        uploads_before = fake.uploads_attempted
        target_mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=target.journal_id)
        res_target = target.sync_production(target_mailbox)
        assert res_target.uploaded == ()
        assert res_target.up_to_date is True
        assert fake.uploads_attempted == uploads_before
    finally:
        source.close()
        target.close()


def test_read_config_reuses_existing_sandbox_or_production_config(tmp_path, monkeypatch):
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    sandbox_cfg = config_root / "zymptom" / "sandbox-sync" / "firebase.json"
    sandbox_cfg.parent.mkdir(parents=True)
    sandbox_cfg.write_text(
        json.dumps({
            "firebase_api_key": "sandbox-key",
            "firebase_project_id": "demo-project",
            "google_client_id": "client-id",
            "google_client_secret": "secret",
        }),
        encoding="utf-8",
    )
    loaded = read_config()
    assert loaded.firebase_project_id == "demo-project"
    assert loaded.firebase_api_key == "sandbox-key"


def test_desktop_descriptor_publication_is_explicit_idempotent_and_rejects_malformed(tmp_path, full_episode):
    fake = FakeProductionFirestore()
    session = JournalSession(tmp_path / "production.sqlite3")
    try:
        insert_untracked(session.connection, deepcopy(full_episode))
        # App launch / session open / enrollment / auth / mailbox creation never write a descriptor
        enrollment = session.enroll_sync()
        session.inspect_production_sync()
        mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        mailbox.connect()
        assert fake.descriptors == {}

        # Sandbox mailbox cannot publish a production descriptor
        sandbox_mailbox = FirestoreMailbox("demo-project", make_auth(), request=fake)
        with pytest.raises(TransportError, match="cannot be published for the sandbox"):
            sandbox_mailbox.ensure_descriptor()
        assert fake.descriptors == {}

        # First sync publishes the descriptor and uploads changes
        result = session.sync_production(mailbox, approve_first_sync=True)
        assert result.up_to_date is True
        descriptor_key = f"{mailbox.root}/users/prod-user-1/journals/{enrollment.journal_id}"
        assert descriptor_key in fake.descriptors
        fields = fake.descriptors[descriptor_key]["fields"]
        assert fields["owner_uid"] == {"stringValue": "prod-user-1"}
        assert fields["journal_id"] == {"stringValue": str(enrollment.journal_id)}
        assert fields["journal_kind"] == {"stringValue": "production"}
        assert fields["contract_version"] == {"integerValue": "1"}
        assert fields["record_schema_version"] == {"integerValue": "1"}
        assert "timestampValue" in fields["published_at"]

        # Repeated sync / ensure_descriptor is idempotent
        assert mailbox.ensure_descriptor() == "already-published"
        second = session.sync_production(mailbox)
        assert second.uploaded == ()
        assert second.up_to_date is True

        # An already-synced desktop journal with 0 pending changes still ensures its descriptor on explicit Sync now
        fake.descriptors.clear()
        assert session.inspect_production_sync().pending_change_count == 0
        third = session.sync_production(mailbox)
        assert third.uploaded == ()
        assert descriptor_key in fake.descriptors

        # Incompatible existing descriptor metadata is rejected without overwriting
        fake.descriptors[descriptor_key]["fields"]["journal_kind"] = {"stringValue": "sandbox"}
        with pytest.raises(TransportError, match="descriptor is malformed"):
            session.sync_production(mailbox)
        assert fake.descriptors[descriptor_key]["fields"]["journal_kind"] == {"stringValue": "sandbox"}
    finally:
        session.close()


@pytest.mark.skipif(os.getenv("ZYMPTOM_RUN_EMULATOR_TESTS") != "1", reason="requires local Firebase emulators")
def test_production_mailbox_with_firebase_emulator_and_sandbox_isolation(tmp_path, full_episode):
    auth_host = os.environ["FIREBASE_AUTH_EMULATOR_HOST"]
    firestore_host = os.environ["FIRESTORE_EMULATOR_HOST"]

    def emulator_auth_request(url, **kwargs):
        return request_json(
            url.replace("https://identitytoolkit.googleapis.com", f"http://{auth_host}/identitytoolkit.googleapis.com"),
            **kwargs,
        )

    def emulator_request(url, **kwargs):
        return request_json(url.replace("https://firestore.googleapis.com", f"http://{firestore_host}"), **kwargs)

    mock_google = json.dumps({"sub": "prod-emulator-user", "email": "prod@example.test", "email_verified": True})
    auth = firebase_exchange(
        DesktopConfig("demo-key", "demo-zymptom-z51", "desktop-client"),
        mock_google,
        request=emulator_auth_request,
    )

    # 1. Create and sync a disposable sandbox journal under the SAME Firebase UID
    sandbox_session = JournalSession(tmp_path / "sandbox.sqlite3")
    prod_session_a = JournalSession(tmp_path / "prod_a.sqlite3")
    prod_session_b = JournalSession(tmp_path / "prod_b.sqlite3")
    try:
        sandbox_ep = sandbox_session.episodes.record(activity="DEMO: sandbox entry", outcome=EpisodeOutcome.STOPPED_EARLY)
        sandbox_box = FirestoreMailbox("demo-zymptom-z51", auth, request=emulator_request)
        sync_disposable(sandbox_session, sandbox_box)
        assert sandbox_box.journal_id is not None

        # 2. Enroll production journal A with 2 entries and sync to production mailbox
        insert_untracked(prod_session_a.connection, deepcopy(full_episode))
        second_ep = prod_session_a.episodes.record(activity="Second production entry", outcome=EpisodeOutcome.COMPLETED_NORMALLY)
        enrollment = prod_session_a.enroll_sync()
        assert str(enrollment.journal_id) != sandbox_box.journal_id

        prod_box_a = FirestoreMailbox("demo-zymptom-z51", auth, request=emulator_request, journal_id=enrollment.journal_id)
        res_a = prod_session_a.sync_production(prod_box_a, approve_first_sync=True)
        assert res_a.mailbox_state == "empty"
        assert len(res_a.uploaded) == 2
        assert res_a.up_to_date is True
        assert prod_box_a.ensure_descriptor() == "already-published"

        listed_journals = request_json(
            f"http://{firestore_host}/v1/{prod_box_a.root}/users/{auth.uid}/journals",
            headers={"Authorization": "Bearer " + auth.id_token},
        )
        doc_names = [doc["name"].rsplit("/", 1)[-1] for doc in listed_journals.get("documents", [])]
        assert doc_names == [str(enrollment.journal_id)]

        # Confirm DEMO sandbox entry never entered production, and production entries never entered sandbox
        assert prod_session_a.episodes.get(sandbox_ep.id) is None
        sync_disposable(sandbox_session, sandbox_box)
        assert sandbox_session.episodes.get(full_episode.id) is None

        # 3. Duplicate upload is idempotent and same-ID/different-payload collision is rejected against emulator
        first_change = read_changes(prod_session_a.connection)[res_a.uploaded[0]]
        assert prod_box_a.upload(first_change) == "already-uploaded"
        colliding = Change.build(
            record_id=first_change.record_id,
            episode=replace(first_change.episode, notes="Colliding notes"),
            device_id=first_change.device_id,
            operation="create",
            updated_at=first_change.updated_at,
            change_id=first_change.change_id,
        )
        with pytest.raises(IdentityCollision):
            prod_box_a.upload(colliding)

        # 4. Second session with same enrolled journal_id pulls paginated changes and merges descendant update
        clone_path = tmp_path / "prod_clone.sqlite3"
        prod_session_a.create_manual_backup(clone_path)
        prod_session_b.restore(prod_session_b.prepare_recovery(prod_session_b.inspect_recovery(clone_path)))
        edited = prod_session_b.episodes.get(second_ep.id)
        edited.notes = "Edited on B via emulator"
        prod_session_b.episodes.update(edited)

        prod_box_b = FirestoreMailbox("demo-zymptom-z51", auth, request=emulator_request, journal_id=enrollment.journal_id)
        res_b = prod_session_b.sync_production(prod_box_b)
        assert len(res_b.uploaded) == 1 and res_b.up_to_date is True

        res_a2 = prod_session_a.sync_production(prod_box_a)
        assert res_a2.mailbox_state == "compatible"
        assert prod_session_a.episodes.get(second_ep.id).notes == "Edited on B via emulator"

        # 5. Cross-user access to the production mailbox is denied (HTTP 403)
        outsider = request_json(
            f"http://{auth_host}/identitytoolkit.googleapis.com/v1/accounts:signUp?key=demo-key",
            data=json.dumps({"returnSecureToken": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HttpError) as denied:
            request_json(
                f"http://{firestore_host}/v1/{prod_box_a.root}/users/{auth.uid}/journals/{enrollment.journal_id}/changes/{first_change.change_id}",
                headers={"Authorization": "Bearer " + outsider["idToken"]},
            )
        assert denied.value.status == 403
        with pytest.raises(HttpError) as denied_desc:
            request_json(
                f"http://{firestore_host}/v1/{prod_box_a.root}/users/{auth.uid}/journals/{enrollment.journal_id}",
                headers={"Authorization": "Bearer " + outsider["idToken"]},
            )
        assert denied_desc.value.status == 403
        with pytest.raises(HttpError) as denied_list:
            request_json(
                f"http://{firestore_host}/v1/{prod_box_a.root}/users/{auth.uid}/journals",
                headers={"Authorization": "Bearer " + outsider["idToken"]},
            )
        assert denied_list.value.status == 403
    finally:
        sandbox_session.close()
        prod_session_a.close()
        prod_session_b.close()
