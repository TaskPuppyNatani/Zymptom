from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import time
import threading
from uuid import uuid4

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QTextEdit

from zymptom.domain import EpisodeOutcome
from zymptom.persistence import EpisodeRepository
from zymptom.persistence.backups import BackupError, content_hash
from zymptom.persistence.database import transaction
from zymptom.sandbox.auth import (
    AuthError,
    CredentialStoreError,
    DesktopConfig,
    FirebaseSession,
    MemoryCredentialStore,
)
from zymptom.sandbox.transport import FirestoreMailbox, TransportError
from zymptom.services.journal import JournalSession, production_provider
from zymptom.sync.contract import SyncError
from zymptom.sync.storage import read_changes
from zymptom.ui.window import MainWindow

from tests.sync.test_production_sync import FakeProductionFirestore


def acknowledged_changes(connection, provider):
    return {
        row[0]
        for row in connection.execute(
            "SELECT change_id FROM sync_acknowledgements WHERE provider = ?",
            (provider,),
        )
    }


def insert_untracked(connection, episode):
    with transaction(connection):
        EpisodeRepository(connection)._apply_sync_snapshot(episode.id, episode)
    return episode


def make_auth(uid="prod-user-1", token="prod-secret-token", email="user@example.test"):
    return FirebaseSession(uid=uid, id_token=token, expires_at=time.monotonic() + 3600, email=email)


@pytest.mark.parametrize("previously_synced", [False, True])
def test_auth_worker_completion_refreshes_the_same_open_dialog(
    qapp, zone, tmp_path, monkeypatch, previously_synced
):
    from zymptom.ui import sync_dialog as sync_module

    session = JournalSession(tmp_path / "production.sqlite3")
    session.enroll_sync()
    window = MainWindow(session.episodes, zone=zone, backup_session=session)
    gate = threading.Event()
    auth = make_auth()
    uploads = []
    monkeypatch.setattr(sync_module, "read_config", lambda: DesktopConfig("api-key", "demo-project", "client-id"))
    def sign_in(_config, *, cancel):
        assert gate.wait(3)
        return auth
    monkeypatch.setattr(sync_module, "sign_in", sign_in)
    monkeypatch.setattr(session, "sync_production", lambda *args, **kwargs: uploads.append((args, kwargs)))
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        if previously_synced:
            monkeypatch.setattr(dialog, "_load_saved_last_sync", lambda _journal_id: datetime(2026, 10, 27, tzinfo=timezone.utc))
        dialog.signin.click()
        worker = dialog.worker
        assert worker is not None and dialog.state_label.text() == "Connecting…"
        assert dialog.cancel_signin.isEnabled() and not dialog.cancel_signin.isHidden()
        assert not dialog.start_sync_button.isEnabled() and not dialog.sync_button.isEnabled()
        gate.set()
        assert worker.wait(3000)
        qapp.processEvents()
        assert window.sync_dialog is dialog and dialog.worker is None
        assert dialog.auth is auth and window._sync_auth is auth
        assert dialog.state_label.text() == "Connected"
        assert "Google account" in dialog.account_label.text()
        assert dialog.start_sync_button.isEnabled() is (not previously_synced)
        assert dialog.sync_button.isEnabled() is previously_synced
        if not previously_synced:
            assert "Safety backup" in dialog.details.text()
            assert "Nothing will be deleted locally" in dialog.details.text()
        assert not dialog.cancel_signin.isEnabled() and dialog.cancel_signin.isHidden()
        assert uploads == []
    finally:
        gate.set()
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


@pytest.mark.parametrize("cancelled", [False, True])
def test_auth_worker_failure_or_cancel_clears_connecting_state(qapp, zone, tmp_path, monkeypatch, cancelled):
    from zymptom.ui import sync_dialog as sync_module

    session = JournalSession(tmp_path / "production.sqlite3")
    session.enroll_sync()
    window = MainWindow(session.episodes, zone=zone, backup_session=session)
    gate = threading.Event()
    monkeypatch.setattr(sync_module, "read_config", lambda: DesktopConfig("api-key", "demo-project", "client-id"))
    def sign_in(_config, *, cancel):
        assert gate.wait(3)
        raise AuthError("private auth failure", kind="auth")
    monkeypatch.setattr(sync_module, "sign_in", sign_in)
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.signin.click()
        worker = dialog.worker
        assert dialog.state_label.text() == "Connecting…"
        if cancelled:
            dialog.cancel_signin.click()
        gate.set()
        assert worker.wait(3000)
        qapp.processEvents()
        assert dialog.worker is None and dialog.auth is None
        assert dialog.state_label.text() != "Connecting…"
        assert dialog.signin.isEnabled()
        assert not dialog.cancel_signin.isEnabled() and dialog.cancel_signin.isHidden()
        assert not dialog.start_sync_button.isEnabled() and not dialog.sync_button.isEnabled()
        assert "private auth failure" not in dialog.status.text()
        if cancelled:
            assert "Cancelling" not in dialog.status.text()
    finally:
        gate.set()
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


def test_cancelling_after_success_is_queued_does_not_connect(qapp, zone, tmp_path, monkeypatch):
    from zymptom.ui import sync_dialog as sync_module

    session = JournalSession(tmp_path / "production.sqlite3")
    session.enroll_sync()
    window = MainWindow(session.episodes, zone=zone, backup_session=session)
    monkeypatch.setattr(sync_module, "read_config", lambda: DesktopConfig("api-key", "demo-project", "client-id"))
    monkeypatch.setattr(sync_module, "sign_in", lambda _config, *, cancel: make_auth())
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.signin.click()
        worker = dialog.worker
        assert worker.wait(3000)  # success is queued; the UI has not processed it yet
        dialog.cancel_signin.click()
        qapp.processEvents()
        assert dialog.worker is None and dialog.auth is None and window._sync_auth is None
        assert dialog.state_label.text() == "Not connected"
        assert dialog.signin.isEnabled() and dialog.cancel_signin.isHidden()
        assert not dialog.start_sync_button.isEnabled() and not dialog.sync_button.isEnabled()
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


def test_production_sync_ui_enrollment_connect_first_sync_preview_start_sync_and_subsequent_sync(
    qapp, zone, tmp_path, full_episode, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = FakeProductionFirestore()
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    insert_untracked(session.connection, deepcopy(full_episode))
    insert_untracked(
        session.connection,
        replace(deepcopy(full_episode), id=uuid4(), activity="Second pre-sync entry"),
    )
    fixed_now = datetime(2026, 10, 27, 19, 30, tzinfo=timezone.utc)

    def mailbox_factory(config, auth, journal_id):
        return FirestoreMailbox(
            config.firebase_project_id if config else "demo-project",
            auth,
            request=fake,
            journal_id=journal_id,
        )

    window = MainWindow(session.episodes, zone=zone, now=lambda: fixed_now, backup_session=session)
    window.show()
    qapp.processEvents()
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.mailbox_factory = mailbox_factory

        # Before enrollment: Connect / Start sync / Sync now / Disconnect are disabled
        assert dialog.state_label.text() == "Sync not set up"
        assert dialog.prepare_button.isEnabled()
        assert not dialog.signin.isEnabled()
        assert not dialog.start_sync_button.isEnabled()
        assert not dialog.sync_button.isEnabled()
        assert not dialog.signout.isEnabled()
        assert len(fake.documents) == 0

        # Enroll journal
        session.enroll_sync()
        dialog.refresh(clear_status=True)
        assert dialog.state_label.text() == "Journal ready for sync"
        assert dialog.signin.isEnabled()
        assert not dialog.start_sync_button.isEnabled()
        assert not dialog.sync_button.isEnabled()
        assert not dialog.signout.isEnabled()
        assert len(fake.documents) == 0

        # Sign in with Google: shows first-sync preview, requires explicit Start sync, uploads nothing yet
        dialog.config = DesktopConfig("api-key", "demo-project", "client-id")
        dialog._signed_in(make_auth())
        qapp.processEvents()

        assert dialog.state_label.text() == "Connected"
        assert dialog.account_label.text() == "Google account: user@example.test"
        assert dialog.last_sync_label.text() == "Never synced"
        assert dialog.summary_label.text() == "Zymptom is ready to upload this journal for the first time."
        preview_text = dialog.details.text()
        assert "Local changes waiting to upload: 2" in preview_text
        assert "Journal entries: 2" in preview_text
        assert "Production journal identity: Established" in preview_text
        assert "Safety backup: A verified safety backup will be created before first sync" in preview_text
        assert "Nothing will be deleted locally if the cloud mailbox is empty" in preview_text
        assert str(session.journal_id) not in preview_text
        assert dialog.start_sync_button.isEnabled()
        assert not dialog.start_sync_button.isHidden()
        assert not dialog.sync_button.isEnabled()
        assert dialog.signout.isEnabled()
        assert len(fake.documents) == 0

        # Calling begin_sync() before Start sync refuses to upload
        dialog.begin_sync()
        assert "Start sync" in dialog.status.text()
        assert len(fake.documents) == 0

        # Explicitly click Start sync
        safety_before = sum(1 for b in session.backups.list() if b.kind == "safety" and b.valid)
        dialog.start_sync_button.click()
        qapp.processEvents()

        assert len(fake.documents) == 2
        assert sum(1 for b in session.backups.list() if b.kind == "safety" and b.valid) == safety_before + 1
        assert dialog.state_label.text() == "Up to date"
        assert dialog.last_sync_label.text() == "Last synced: Oct 27, 2026 · 12:30 PM"
        assert dialog.start_sync_button.isHidden()
        assert not dialog.start_sync_button.isEnabled()
        assert dialog.sync_button.isEnabled()
        assert not dialog.sync_button.isHidden()
        assert "Local changes waiting to upload: 0" in dialog.details.text()

        # Reopening Settings -> Sync preserves connected session state
        dialog.reject()
        qapp.processEvents()
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.mailbox_factory = mailbox_factory
        assert dialog.state_label.text() == "Up to date"
        assert dialog.last_sync_label.text() == "Last synced: Oct 27, 2026 · 12:30 PM"
        assert dialog.sync_button.isEnabled()
        assert dialog.start_sync_button.isHidden()

        # Local change in MainWindow transitions status from Up to date to Connected with 1 pending change
        dialog.reject()
        qapp.processEvents()
        created = session.episodes.record(activity="New desktop entry", outcome=EpisodeOutcome.COMPLETED_NORMALLY)
        window.refresh()

        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.mailbox_factory = mailbox_factory
        assert dialog.state_label.text() == "Connected"
        assert "Local changes waiting to upload: 1" in dialog.details.text()
        assert dialog.sync_button.isEnabled()
        assert dialog.start_sync_button.isHidden()

        # Clicking Sync now uploads only the 1 new change
        dialog.sync_button.click()
        qapp.processEvents()
        assert len(fake.documents) == 3
        assert dialog.state_label.text() == "Up to date"
        assert "Local changes waiting to upload: 0" in dialog.details.text()
        assert session.episodes.get(created.id) is not None
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


def test_disconnect_and_reconnect_preserve_journal_identity_history_and_acknowledgements(
    qapp, zone, tmp_path, full_episode, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = FakeProductionFirestore()
    session = JournalSession(tmp_path / "production.sqlite3")
    insert_untracked(session.connection, deepcopy(full_episode))
    enrollment = session.enroll_sync()
    fixed_now = datetime(2026, 10, 27, 20, 15, tzinfo=timezone.utc)

    def mailbox_factory(config, auth, journal_id):
        return FirestoreMailbox(
            config.firebase_project_id if config else "demo-project",
            auth,
            request=fake,
            journal_id=journal_id,
        )

    window = MainWindow(session.episodes, zone=zone, now=lambda: fixed_now, backup_session=session)
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.mailbox_factory = mailbox_factory
        dialog.config = DesktopConfig("api-key", "demo-project", "client-id")
        dialog._signed_in(make_auth())
        dialog.start_sync_button.click()
        qapp.processEvents()

        provider = production_provider(enrollment.journal_id)
        acks_before = acknowledged_changes(session.connection, provider)
        changes_before = read_changes(session.connection)
        saved_last_sync = dialog.last_sync_label.text()
        assert len(acks_before) == 1

        # Disconnect
        dialog.signout.click()
        qapp.processEvents()
        assert dialog.auth is None
        assert window._sync_auth is None
        assert dialog.state_label.text() == "Not connected"
        assert dialog.signin.isEnabled()
        assert not dialog.sync_button.isEnabled()
        assert not dialog.start_sync_button.isEnabled()
        assert not dialog.signout.isEnabled()
        assert session.journal_id == enrollment.journal_id
        assert acknowledged_changes(session.connection, provider) == acks_before
        assert read_changes(session.connection) == changes_before
        assert len(session.episodes.list()) == 1

        # Reconnect: uses same journal_id, restores Last synced, requires Sync now (not Start sync)
        dialog.config = DesktopConfig("api-key", "demo-project", "client-id")
        dialog._signed_in(make_auth())
        qapp.processEvents()
        assert dialog.state_label.text() == "Connected"
        assert dialog.last_sync_label.text() == saved_last_sync
        assert dialog.start_sync_button.isHidden()
        assert dialog.sync_button.isEnabled()
        assert len(fake.documents) == 1

        dialog.sync_button.click()
        qapp.processEvents()
        assert dialog.state_label.text() == "Up to date"
        assert len(fake.documents) == 1
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


@pytest.mark.parametrize(
    "exc_factory,expected_state,expected_snippet",
    [
        (lambda: TransportError("Network socket timeout secret", kind="offline"), "Offline", "entries are safe"),
        (lambda: AuthError("Bearer secret-token expired", kind="auth"), "Not connected", "connection expired"),
        (lambda: TransportError("403 Firestore rule denied secret", kind="permission"), "Needs attention", "could not access your sync data"),
        (lambda: SyncError("Raw cycle hash mismatch secret"), "Needs attention", "Sync needs attention"),
        (lambda: BackupError("Disk full /private/path secret"), "Needs attention", "safety backup"),
    ],
)
def test_production_sync_ui_surfaces_clear_failure_states_without_leaking_secrets(
    qapp, zone, tmp_path, full_episode, monkeypatch, exc_factory, expected_state, expected_snippet
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    session = JournalSession(tmp_path / "production.sqlite3")
    insert_untracked(session.connection, deepcopy(full_episode))
    session.enroll_sync()

    window = MainWindow(session.episodes, zone=zone, backup_session=session)
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.config = DesktopConfig("api-key", "demo-project", "client-id")
        dialog._signed_in(make_auth(token="super-secret-jwt"))
        monkeypatch.setattr(session, "sync_production", lambda *_a, **_kw: (_ for _ in ()).throw(exc_factory()))

        dialog.start_sync_button.click()
        qapp.processEvents()

        assert dialog.state_label.text() == expected_state
        assert expected_snippet in dialog.status.text()
        assert "secret" not in dialog.status.text()
        assert "super-secret-jwt" not in dialog.status.text()
        assert dialog.last_sync is None
        if expected_state == "Not connected":
            assert dialog.auth is None
            assert dialog.signin.isEnabled()
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


def test_production_sync_ui_conflict_review_and_resolution(qapp, zone, tmp_path, full_episode, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = FakeProductionFirestore()
    desktop_a = JournalSession(tmp_path / "desktop_a.sqlite3")
    desktop_b = JournalSession(tmp_path / "desktop_b.sqlite3")
    try:
        insert_untracked(desktop_a.connection, deepcopy(full_episode))
        enrollment = desktop_a.enroll_sync()
        clone_path = tmp_path / "clone.sqlite3"
        desktop_a.create_manual_backup(clone_path)
        desktop_b.restore(desktop_b.prepare_recovery(desktop_b.inspect_recovery(clone_path)))

        ep_a = desktop_a.episodes.get(full_episode.id)
        ep_a.notes = "Desktop A version"
        desktop_a.episodes.update(ep_a)

        ep_b = desktop_b.episodes.get(full_episode.id)
        ep_b.notes = "Desktop B version"
        desktop_b.episodes.update(ep_b)

        mailbox_b = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
        desktop_b.sync_production(mailbox_b, approve_first_sync=True)

        def mailbox_factory(config, auth, journal_id):
            return FirestoreMailbox("demo-project", auth, request=fake, journal_id=journal_id)

        window = MainWindow(desktop_a.episodes, zone=zone, backup_session=desktop_a)
        window.show()
        qapp.processEvents()
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.mailbox_factory = mailbox_factory
        dialog.config = DesktopConfig("api-key", "demo-project", "client-id")
        dialog._signed_in(make_auth())

        dialog.start_sync_button.click()
        qapp.processEvents()

        assert dialog.state_label.text() == "Needs attention"
        assert not dialog.conflict_button.isHidden()
        assert "Review the versions to continue" in dialog.status.text()

        observed = []

        def choose_version():
            modal = QApplication.activeModalWidget()
            assert isinstance(modal, QDialog)
            observed.append(modal.windowTitle())
            observed.extend(view.toPlainText() for view in modal.findChildren(QTextEdit))
            observed.append(" ".join(label.text() for label in modal.findChildren(QLabel)))
            next(btn for btn in modal.findChildren(QPushButton) if btn.text() == "Keep this version").click()

        QTimer.singleShot(0, choose_version)
        dialog.conflict_button.click()
        qapp.processEvents()

        assert observed and observed[0] == "Review conflict"
        assert any("Desktop A version" in text or "Desktop B version" in text for text in observed[1:3])
        assert all(str(full_episode.id) not in text for text in observed)
        assert dialog.conflict_button.isHidden()
        assert dialog.state_label.text() == "Connected"
        assert "Version saved. Sync now to share it." in dialog.status.text()

        dialog.sync_button.click()
        qapp.processEvents()
        assert dialog.state_label.text() == "Up to date"
    finally:
        if "window" in locals():
            if window.sync_dialog:
                window.sync_dialog.reject()
            window.close()
        desktop_a.close()
        desktop_b.close()


def test_reopening_desktop_restores_connected_sync_state_without_oauth_or_automatic_sync(
    qapp, zone, tmp_path, full_episode, monkeypatch
):
    from zymptom.ui import sync_dialog as sync_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = MemoryCredentialStore()
    fake = FakeProductionFirestore()
    session = JournalSession(tmp_path / "production.sqlite3")
    insert_untracked(session.connection, deepcopy(full_episode))
    enrollment = session.enroll_sync()
    fixed_now = datetime(2026, 10, 27, 19, 30, tzinfo=timezone.utc)
    config = DesktopConfig("api-key-1", "demo-project", "client-id-1")
    monkeypatch.setattr(sync_module, "read_config", lambda: config)

    def mailbox_factory(cfg, auth, journal_id):
        return FirestoreMailbox(cfg.firebase_project_id, auth, request=fake, journal_id=journal_id)

    window1 = MainWindow(
        session.episodes,
        zone=zone,
        now=lambda: fixed_now,
        backup_session=session,
        credential_store=store,
    )
    window2 = None
    try:
        window1.open_sync_dialog()
        dialog1 = window1.sync_dialog
        dialog1.mailbox_factory = mailbox_factory
        dialog1.config = config
        dialog1._signed_in(
            FirebaseSession(
                uid="prod-user-1",
                id_token="initial-jwt",
                expires_at=time.monotonic() + 3600,
                email="user@example.test",
                refresh_token="persisted-refresh-token-1",
            )
        )
        dialog1.start_sync_button.click()
        qapp.processEvents()
        assert dialog1.state_label.text() == "Up to date"
        assert len(fake.documents) == 1
        dialog1.reject()
        window1.close()
        qapp.processEvents()

        # Reopen a new MainWindow instance (simulating closing and reopening Zymptom)
        oauth_calls = []
        monkeypatch.setattr(
            sync_module,
            "sign_in",
            lambda *_a, **_kw: oauth_calls.append(1) or (_ for _ in ()).throw(AssertionError("OAuth must not run")),
        )
        uploads_before_reopen = fake.uploads_attempted
        journal_before_reopen = content_hash(session.connection)

        window2 = MainWindow(
            session.episodes,
            zone=zone,
            now=lambda: fixed_now,
            backup_session=session,
            credential_store=store,
        )
        qapp.processEvents()

        # Startup must restore auth state without triggering any automatic sync
        assert window2._sync_auth is not None
        assert window2._sync_auth.uid == "prod-user-1"
        assert window2._sync_auth.email == "user@example.test"
        assert fake.uploads_attempted == uploads_before_reopen
        assert content_hash(session.connection) == journal_before_reopen

        # Opening Settings -> Sync shows Connected immediately without running OAuth or automatic sync
        window2.open_sync_dialog()
        dialog2 = window2.sync_dialog
        dialog2.mailbox_factory = mailbox_factory
        assert oauth_calls == []
        assert fake.uploads_attempted == uploads_before_reopen
        assert content_hash(session.connection) == journal_before_reopen
        assert dialog2.state_label.text() == "Connected"
        assert dialog2.account_label.text() == "Google account: user@example.test"
        assert dialog2.last_sync_label.text() == "Last synced: Oct 27, 2026 · 12:30 PM"
        assert dialog2.sync_button.isEnabled()
        assert not dialog2.sync_button.isHidden()
        assert dialog2.start_sync_button.isHidden()
        assert not dialog2.signin.isEnabled()
        assert dialog2.signout.isEnabled()
    finally:
        if window1.sync_dialog:
            window1.sync_dialog.reject()
        window1.close()
        if window2 is not None:
            if window2.sync_dialog:
                window2.sync_dialog.reject()
            window2.close()
        session.close()


def test_restored_desktop_ui_session_refreshes_expired_token_on_manual_sync_now(
    qapp, zone, tmp_path, full_episode, monkeypatch
):
    from zymptom.sandbox import auth as auth_module
    from zymptom.ui import sync_dialog as sync_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = MemoryCredentialStore()
    fake = FakeProductionFirestore()
    session = JournalSession(tmp_path / "production.sqlite3")
    insert_untracked(session.connection, deepcopy(full_episode))
    enrollment = session.enroll_sync()
    fixed_now = datetime(2026, 10, 27, 19, 30, tzinfo=timezone.utc)
    config = DesktopConfig("api-key-1", "demo-project", "client-id-1")
    monkeypatch.setattr(sync_module, "read_config", lambda: config)

    refresh_requests = []

    def fake_auth_request(url, *, data=None, headers=None):
        refresh_requests.append(url)
        return {
            "user_id": "prod-user-1",
            "id_token": "fresh-id-token-after-reopen",
            "refresh_token": "rotated-refresh-token-2",
            "expires_in": "3600",
        }

    monkeypatch.setattr(auth_module, "request_json", fake_auth_request)

    def mailbox_factory(cfg, auth, journal_id):
        def check_token_and_forward(url, *, headers, data=None):
            assert headers["Authorization"] == "Bearer fresh-id-token-after-reopen"
            return fake(url, headers=headers, data=data)

        return FirestoreMailbox(
            cfg.firebase_project_id,
            auth,
            request=check_token_and_forward,
            journal_id=journal_id,
        )

    auth_module.save_persisted_session(
        config,
        FirebaseSession(
            uid="prod-user-1",
            id_token="expired-jwt",
            expires_at=0.0,
            email="user@example.test",
            refresh_token="initial-refresh-token-1",
        ),
        scope="production",
        journal_id=enrollment.journal_id,
        store=store,
    )
    # Mark initial sync already completed
    initial_box = FirestoreMailbox(
        "demo-project",
        make_auth(token="initial-jwt"),
        request=fake,
        journal_id=enrollment.journal_id,
    )
    session.sync_production(initial_box, approve_first_sync=True)
    added = session.episodes.record(activity="Logged after reopen", outcome=EpisodeOutcome.STOPPED_EARLY)

    window = MainWindow(
        session.episodes,
        zone=zone,
        now=lambda: fixed_now,
        backup_session=session,
        credential_store=store,
    )
    try:
        assert refresh_requests == []
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.mailbox_factory = mailbox_factory
        assert dialog.state_label.text() == "Connected"
        assert dialog.sync_button.isEnabled()

        dialog.sync_button.click()
        qapp.processEvents()

        assert len(refresh_requests) == 1
        assert dialog.state_label.text() == "Up to date"
        assert len(fake.documents) == 2
        assert session.episodes.get(added.id) is not None
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


def test_explicit_disconnect_persists_signed_out_state_across_desktop_reopen_while_preserving_journal(
    qapp, zone, tmp_path, full_episode, monkeypatch
):
    from zymptom.ui import sync_dialog as sync_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = MemoryCredentialStore()
    fake = FakeProductionFirestore()
    session = JournalSession(tmp_path / "production.sqlite3")
    insert_untracked(session.connection, deepcopy(full_episode))
    enrollment = session.enroll_sync()
    config = DesktopConfig("api-key-1", "demo-project", "client-id-1")
    monkeypatch.setattr(sync_module, "read_config", lambda: config)

    def mailbox_factory(cfg, auth, journal_id):
        return FirestoreMailbox(cfg.firebase_project_id, auth, request=fake, journal_id=journal_id)

    window1 = MainWindow(session.episodes, zone=zone, backup_session=session, credential_store=store)
    window2 = None
    try:
        window1.open_sync_dialog()
        dialog1 = window1.sync_dialog
        dialog1.mailbox_factory = mailbox_factory
        dialog1.config = config
        dialog1._signed_in(
            FirebaseSession(
                uid="prod-user-1",
                id_token="jwt-1",
                expires_at=time.monotonic() + 3600,
                email="user@example.test",
                refresh_token="refresh-1",
            )
        )
        dialog1.start_sync_button.click()
        qapp.processEvents()
        assert store.load("production", "demo-project", str(enrollment.journal_id)) is not None

        provider = production_provider(enrollment.journal_id)
        acks_before = acknowledged_changes(session.connection, provider)
        changes_before = read_changes(session.connection)

        # Explicit Disconnect clears stored credential
        dialog1.signout.click()
        qapp.processEvents()
        assert store.load("production", "demo-project", str(enrollment.journal_id)) is None
        dialog1.reject()
        window1.close()

        # Reopening Zymptom remains signed out while preserving journal data, journal_id, Changes, and acks
        window2 = MainWindow(session.episodes, zone=zone, backup_session=session, credential_store=store)
        assert window2._sync_auth is None
        window2.open_sync_dialog()
        dialog2 = window2.sync_dialog
        assert dialog2.auth is None
        assert dialog2.state_label.text() in {"Not connected", "Journal ready for sync"}
        assert dialog2.signin.isEnabled()
        assert not dialog2.sync_button.isEnabled()
        assert not dialog2.signout.isEnabled()
        assert session.journal_id == enrollment.journal_id
        assert acknowledged_changes(session.connection, provider) == acks_before
        assert read_changes(session.connection) == changes_before
        assert len(session.episodes.list()) == 1
    finally:
        if window1.sync_dialog:
            window1.sync_dialog.reject()
        window1.close()
        if window2 is not None:
            if window2.sync_dialog:
                window2.sync_dialog.reject()
            window2.close()
        session.close()


def test_credential_store_unavailable_falls_back_safely_in_ui_without_plaintext_secrets(
    qapp, zone, tmp_path, full_episode, monkeypatch
):
    from zymptom.ui import sync_dialog as sync_module

    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))

    class UnavailableCredentialStore:
        def load(self, *_args):
            raise CredentialStoreError("System credential store is unavailable on this session.")

        def save(self, *_args):
            raise CredentialStoreError("System credential store is unavailable on this session.")

        def delete(self, *_args):
            raise CredentialStoreError("System credential store is unavailable on this session.")

    fake = FakeProductionFirestore()
    session = JournalSession(tmp_path / "production.sqlite3")
    insert_untracked(session.connection, deepcopy(full_episode))
    session.enroll_sync()
    config = DesktopConfig("api-key-1", "demo-project", "client-id-1")
    monkeypatch.setattr(sync_module, "read_config", lambda: config)

    def mailbox_factory(cfg, auth, journal_id):
        return FirestoreMailbox(cfg.firebase_project_id, auth, request=fake, journal_id=journal_id)

    window = MainWindow(
        session.episodes,
        zone=zone,
        backup_session=session,
        credential_store=UnavailableCredentialStore(),
    )
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.mailbox_factory = mailbox_factory
        assert dialog.state_label.text() in {"Not connected", "Journal ready for sync"}
        assert "Persistent sign-in is unavailable" in dialog.status.text()
        assert "secret" not in dialog.status.text().lower()

        # Interactive sign-in and manual sync still succeed for the current session
        dialog.config = config
        dialog._signed_in(
            FirebaseSession(
                uid="prod-user-1",
                id_token="in-memory-jwt-secret",
                expires_at=time.monotonic() + 3600,
                email="user@example.test",
                refresh_token="in-memory-refresh-secret",
            )
        )
        assert dialog.state_label.text() == "Connected"
        assert "Persistent sign-in is unavailable" in dialog.status.text()

        dialog.start_sync_button.click()
        qapp.processEvents()
        assert dialog.state_label.text() == "Up to date"
        assert len(fake.documents) == 1

        # Verify no plaintext secret was written to any file under tmp_path
        for path in tmp_path.rglob("*"):
            if path.is_file():
                text = path.read_bytes().decode("utf-8", errors="ignore")
                assert "in-memory-jwt-secret" not in text
                assert "in-memory-refresh-secret" not in text
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


def test_disconnect_failure_keeps_session_and_reports_that_credential_was_not_removed(qapp, zone, tmp_path, monkeypatch):
    from zymptom.ui import sync_dialog as sync_module

    class DeleteFailStore(MemoryCredentialStore):
        def delete(self, *_args):
            raise CredentialStoreError("private credential details")

    store = DeleteFailStore()
    session = JournalSession(tmp_path / "production.sqlite3")
    enrollment = session.enroll_sync()
    config = DesktopConfig("api-key-1", "demo-project", "client-id-1")
    monkeypatch.setattr(sync_module, "read_config", lambda: config)
    window = MainWindow(session.episodes, zone=zone, backup_session=session, credential_store=store)
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        auth = FirebaseSession("prod-user-1", "short-token", time.monotonic() + 3600,
                               refresh_token="private-refresh-token")
        dialog.config = config
        dialog._signed_in(auth)
        dialog.signout.click()
        assert dialog.auth is auth and window._sync_auth is auth
        assert store.load("production", "demo-project", str(enrollment.journal_id)) is not None
        assert "Could not remove" in dialog.status.text()
        assert "private credential details" not in dialog.status.text()
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


def test_interactive_session_refreshes_its_token_during_later_manual_sync(qapp, zone, tmp_path, monkeypatch):
    from zymptom.sandbox import auth as auth_module
    from zymptom.ui import sync_dialog as sync_module

    store = MemoryCredentialStore()
    session = JournalSession(tmp_path / "production.sqlite3")
    session.enroll_sync()
    config = DesktopConfig("api-key-1", "demo-project", "client-id-1")
    monkeypatch.setattr(sync_module, "read_config", lambda: config)
    refreshes = []
    def refresh(_url, **_kwargs):
        refreshes.append(True)
        return {"user_id": "prod-user-1", "id_token": "new-id-token",
                "refresh_token": "new-refresh-token", "expires_in": "3600"}
    monkeypatch.setattr(auth_module, "request_json", refresh)
    fake = FakeProductionFirestore()
    window = MainWindow(session.episodes, zone=zone, backup_session=session, credential_store=store)
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        dialog.config = config
        auth = FirebaseSession("prod-user-1", "old-id-token", time.monotonic() + 3600,
                               refresh_token="old-refresh-token")
        dialog._signed_in(auth)
        assert refreshes == []
        object.__setattr__(auth, "expires_at", 0.0)
        dialog.mailbox_factory = lambda cfg, current, journal_id: FirestoreMailbox(
            cfg.firebase_project_id, current, request=fake, journal_id=journal_id,
        )
        dialog.start_sync_button.click()
        assert refreshes == [True] and auth.id_token == "new-id-token"
        assert dialog.state_label.text() == "Up to date"
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()


def test_revoked_restored_credential_during_sync_now_shows_not_connected(
    qapp, zone, tmp_path, full_episode, monkeypatch
):
    from zymptom.sandbox import auth as auth_module
    from zymptom.ui import sync_dialog as sync_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store = MemoryCredentialStore()
    session = JournalSession(tmp_path / "production.sqlite3")
    insert_untracked(session.connection, deepcopy(full_episode))
    enrollment = session.enroll_sync()
    config = DesktopConfig("api-key-1", "demo-project", "client-id-1")
    monkeypatch.setattr(sync_module, "read_config", lambda: config)

    # Mark initial sync done and save a credential that will fail refresh on Sync now
    fake = FakeProductionFirestore()
    initial_box = FirestoreMailbox("demo-project", make_auth(), request=fake, journal_id=enrollment.journal_id)
    session.sync_production(initial_box, approve_first_sync=True)

    auth_module.save_persisted_session(
        config,
        FirebaseSession(
            uid="prod-user-1",
            id_token="",
            expires_at=0.0,
            email="user@example.test",
            refresh_token="revoked-refresh-token-xyz",
        ),
        scope="production",
        journal_id=enrollment.journal_id,
        store=store,
    )
    monkeypatch.setattr(
        auth_module,
        "request_json",
        lambda *_a, **_kw: (_ for _ in ()).throw(auth_module.HttpError(400)),
    )

    window = MainWindow(session.episodes, zone=zone, backup_session=session, credential_store=store)
    try:
        window.open_sync_dialog()
        dialog = window.sync_dialog
        assert dialog.state_label.text() == "Connected"

        dialog.sync_button.click()
        qapp.processEvents()

        assert dialog.state_label.text() == "Not connected"
        assert "connection expired" in dialog.status.text()
        assert "revoked-refresh-token-xyz" not in dialog.status.text()
        assert dialog.auth is None
        assert window._sync_auth is None
        assert (
            store.load("production", "demo-project", str(enrollment.journal_id))
            is not None
        )
        assert len(session.episodes.list()) == 1
    finally:
        if window.sync_dialog:
            window.sync_dialog.reject()
        window.close()
        session.close()
