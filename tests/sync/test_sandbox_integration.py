import os

import pytest

from zymptom.persistence.backups import BackupError
from zymptom.domain import EpisodeOutcome
from zymptom.sandbox.auth import DesktopConfig, HttpError, firebase_exchange, request_json
from zymptom.sandbox.sync import sync_disposable
from zymptom.sandbox.transport import FirestoreMailbox
from zymptom.services.journal import JournalSession
from zymptom.sync.contract import SyncBatch
from zymptom.sync.service import SyncService


class MemoryMailbox:
    def __init__(self):
        self.changes = {}
        self.fail_pull = False

    def connect(self):
        return "00000000-0000-0000-0000-000000000123"

    def upload(self, change):
        existing = self.changes.get(change.change_id)
        if existing is not None and existing != change:
            raise ValueError("collision")
        self.changes[change.change_id] = change

    def pull(self):
        if self.fail_pull:
            raise RuntimeError("offline")
        return SyncBatch(tuple(self.changes.values()))


def test_sync_persists_ack_and_retries_after_pull_failure(simulator, full_episode):
    a, b = simulator.desktop, simulator.phone
    a.episodes.repository.create(full_episode)
    box = MemoryMailbox()
    box.fail_pull = True
    with pytest.raises(RuntimeError, match="offline"):
        sync_disposable(a.session, box)
    provider = "firebase-sandbox:00000000-0000-0000-0000-000000000123"
    assert not a.sync.pending(provider).changes
    box.fail_pull = False
    sync_disposable(b.session, box)
    assert b.episodes.get(full_episode.id) is not None
    assert b.sync.pending(provider).changes == ()
    result, _ = sync_disposable(b.session, box)
    assert result.duplicates and not result.applied


def test_merge_backup_failure_leaves_journal_and_receipts_untouched(simulator, full_episode, monkeypatch):
    a, b = simulator.desktop, simulator.phone
    a.episodes.repository.create(full_episode)
    box = MemoryMailbox()
    sync_disposable(a.session, box)
    def fail():
        raise BackupError("injected backup failure")
    monkeypatch.setattr(b.session, "before_destructive_change", fail)
    with pytest.raises(BackupError):
        sync_disposable(b.session, box)
    assert b.episodes.list() == []
    assert b.sync.known_revision(full_episode.id) is None
    assert b.session.connection.execute("SELECT count(*) FROM sync_acknowledgements").fetchone()[0] == 0


@pytest.mark.parametrize("delete_left", [False, True])
def test_mailbox_conflict_branches_and_explicit_resolution(simulator, full_episode, delete_left):
    a, b = simulator.desktop, simulator.phone
    box = MemoryMailbox()
    a.episodes.repository.create(full_episode)
    sync_disposable(a.session, box)
    sync_disposable(b.session, box)
    if delete_left:
        a.episodes.delete(full_episode.id)
    else:
        left = a.episodes.get(full_episode.id)
        left.notes = "Left branch"
        a.episodes.update(left)
    right = b.episodes.get(full_episode.id)
    right.notes = "Right branch"
    b.episodes.update(right)
    sync_disposable(a.session, box)
    sync_disposable(b.session, box)
    sync_disposable(a.session, box)
    conflict, = a.sync.unresolved_conflicts()
    assert len(conflict.candidates) == 2
    assert {candidate.deleted for candidate in conflict.candidates} == ({False, True} if delete_left else {False})
    choice = next(candidate for candidate in conflict.candidates if candidate.episode and candidate.episode.notes == "Right branch")
    resolution = a.sync.resolve(full_episode.id, chosen_change_id=choice.change_id)
    assert set(resolution.parent_ids) == {candidate.change_id for candidate in conflict.candidates}
    sync_disposable(a.session, box)
    sync_disposable(b.session, box)
    assert b.sync.unresolved_conflicts() == ()
    assert b.episodes.get(full_episode.id).notes == "Right branch"


def test_provider_acknowledgements_survive_restart(tmp_path, full_episode):
    database = tmp_path / "disposable.sqlite3"
    box = MemoryMailbox()
    first = JournalSession(database)
    device = first.episodes.repository.device_id
    first.episodes.repository.create(full_episode)
    sync_disposable(first, box)
    first.close()
    second = JournalSession(database, device_id=device)
    try:
        service = SyncService(second.episodes, before_merge=second.before_destructive_change)
        assert not service.pending("firebase-sandbox:00000000-0000-0000-0000-000000000123").changes
        assert second.episodes.get(full_episode.id) is not None
    finally:
        second.close()


@pytest.mark.skipif(os.getenv("ZYMPTOM_RUN_EMULATOR_TESTS") != "1", reason="requires local Firebase emulators")
def test_authenticated_rest_mailbox_with_emulator_rules(simulator, full_episode):
    auth_host = os.environ["FIREBASE_AUTH_EMULATOR_HOST"]
    firestore_host = os.environ["FIRESTORE_EMULATOR_HOST"]
    import json
    def emulator_auth_request(url, **kwargs):
        return request_json(url.replace("https://identitytoolkit.googleapis.com", f"http://{auth_host}/identitytoolkit.googleapis.com"), **kwargs)
    mock_google = json.dumps({"sub": "disposable-google-user", "email": "disposable@example.test", "email_verified": True})
    auth = firebase_exchange(DesktopConfig("demo-key", "demo-zymptom-z51", "desktop-client"), mock_google, request=emulator_auth_request)
    def emulator_request(url, **kwargs):
        return request_json(url.replace("https://firestore.googleapis.com", f"http://{firestore_host}"), **kwargs)
    a = FirestoreMailbox("demo-zymptom-z51", auth, request=emulator_request)
    b = FirestoreMailbox("demo-zymptom-z51", auth, request=emulator_request)
    simulator.desktop.episodes.repository.create(full_episode)
    other = simulator.phone.episodes.record(activity="DEMO: independent phone", outcome=EpisodeOutcome.STOPPED_EARLY)
    result, _ = sync_disposable(simulator.desktop.session, a)
    assert result.duplicates
    received, _ = sync_disposable(simulator.phone.session, b)
    assert received.applied and simulator.phone.episodes.get(full_episode.id) is not None
    assert a.journal_id == b.journal_id
    sync_disposable(simulator.desktop.session, a)
    assert simulator.desktop.episodes.get(other.id) is not None
    second = request_json(f"http://{auth_host}/identitytoolkit.googleapis.com/v1/accounts:signUp?key=demo-key",
                          data=json.dumps({"returnSecureToken": True}).encode(), headers={"Content-Type": "application/json"})
    with pytest.raises(HttpError) as denied:
        request_json(f"http://{firestore_host}/v1/{a.root}/{a.identity_path}", headers={"Authorization": "Bearer " + second["idToken"]})
    assert denied.value.status == 403
    deleted = simulator.phone.episodes.get(full_episode.id)
    simulator.phone.episodes.delete(deleted.id)
    sync_disposable(simulator.phone.session, b)
    sync_disposable(simulator.desktop.session, a)
    assert simulator.desktop.episodes.get(full_episode.id) is None
