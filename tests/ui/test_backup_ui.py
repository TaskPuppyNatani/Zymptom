from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QDate, QTimeZone, Qt
from PySide6.QtWidgets import QMessageBox

from zymptom.domain import EpisodeOutcome
from zymptom.persistence.backups import BackupError
from zymptom.services.journal import JournalSession
from zymptom.ui.window import MainWindow
from zymptom.ui.time import to_local_datetime

NOW = datetime(2026, 10, 21, 21, 10, tzinfo=UTC)


@pytest.fixture
def backup_window(qapp, zone, database_path):
    session = JournalSession(database_path)
    window = MainWindow(session.episodes, zone=zone, now=lambda: NOW, backup_session=session)
    window.show()
    qapp.processEvents()
    yield window, session
    window.close()
    qapp.processEvents()
    session.close()


def select_backup(dialog, path):
    for row in range(dialog.entries.count()):
        item = dialog.entries.item(row)
        if item.data(Qt.ItemDataRole.UserRole).path == path:
            dialog.entries.setCurrentItem(item)
            return
    raise AssertionError("Backup missing from list")


def prepare(window, qapp, snapshot):
    window.backup_action.trigger()
    qapp.processEvents()
    dialog = window.backup_dialog
    assert dialog is not None and dialog.isModal()
    select_backup(dialog, snapshot.path)
    dialog.restore_button.click()
    qapp.processEvents()
    assert dialog.confirmation is not None
    assert dialog.confirmation.defaultButton() == dialog.confirmation.button(QMessageBox.StandardButton.Cancel)
    return dialog


def seed(session):
    original = session.episodes.record(
        activity="Homework café ☕", outcome=EpisodeOutcome.STOPPED_EARLY,
        occurred_at=NOW, functional_demands=["sitting"], notes="First line\n雪 second line",
    )
    original = deepcopy(original)
    snapshot = session.backups.create(session.connection, now=NOW)
    changed = deepcopy(original)
    changed.activity = "Homework current"
    session.episodes.update(changed)
    extra = session.episodes.record(activity="Current only", outcome=EpisodeOutcome.COMPLETED_NORMALLY, occurred_at=NOW)
    return original, snapshot, [changed, extra]


def test_manual_backup_shows_success_and_failure(backup_window, qapp, monkeypatch):
    window, session = backup_window
    window.backup_action.trigger()
    qapp.processEvents()
    dialog = window.backup_dialog
    assert dialog.manual_button.accessibleName() == "Create backup now"
    dialog.manual_button.click()
    assert "created and verified" in dialog.status.text()
    assert any(info.kind == "manual" and info.valid for info in session.backups.list())
    assert next(info for info in session.backups.list() if info.kind == "manual").created_at == NOW

    def fail(*args, **kwargs):
        raise BackupError("Disk unavailable")

    monkeypatch.setattr(session.backups, "create", fail)
    dialog.manual_button.click()
    assert "Backup failed" in dialog.status.text() and "Disk unavailable" in dialog.status.text()


def test_restore_cancel_leaves_journal_unchanged_and_keeps_safety(backup_window, qapp):
    window, session = backup_window
    _, snapshot, _ = seed(session)
    before = session.episodes.list()
    dialog = prepare(window, qapp, snapshot)
    plan = dialog.plan
    assert plan.safety.valid and "safety backup" in dialog.confirmation.text()
    assert snapshot.path.name in dialog.confirmation.text()
    dialog.confirmation.done(QMessageBox.StandardButton.Cancel)
    assert session.episodes.list() == before
    assert plan.safety.path.exists() and not plan.staged.exists()
    assert "cancelled" in dialog.status.text()


def test_corrupt_backup_is_marked_and_cannot_be_selected_for_restore(backup_window, qapp):
    window, session = backup_window
    info = session.backups.create(session.connection)
    info.path.write_bytes(b'corrupt snapshot')
    window.open_backup_dialog()
    qapp.processEvents()
    dialog = window.backup_dialog
    select_backup(dialog, info.path)
    assert not dialog.restore_button.isEnabled()
    assert 'Cannot restore' in dialog.entries.currentItem().text()


def test_restore_refreshes_today_history_and_preserves_filters(backup_window, qapp):
    window, session = backup_window
    original, snapshot, _ = seed(session)
    window.refresh()
    window.show_history()
    history = window.history
    history.search_text.setText("Homework")
    history.outcome.setCurrentIndex(history.outcome.findData(EpisodeOutcome.STOPPED_EARLY))
    history.demand.setCurrentIndex(history.demand.findData("sitting"))
    history.start_date.setDate(QDate(2026, 10, 21))
    history.end_date.setDate(QDate(2026, 10, 21))
    history.start_enabled.setChecked(True)
    history.end_enabled.setChecked(True)
    state = (history.search_text.text(), history.outcome.currentData(), history.demand.currentData(),
             history.start_date.date(), history.end_date.date(), history.start_enabled.isChecked(), history.end_enabled.isChecked())
    dialog = prepare(window, qapp, snapshot)
    window.new_episode()
    window.open_export_dialog()
    assert window.editor is None and window.export_dialog is None
    dialog.confirmation.done(QMessageBox.StandardButton.Yes)
    qapp.processEvents()
    assert "restored and verified" in dialog.status.text()
    assert session.episodes.list() == [original]
    assert window.entries.count() == 1 and original.activity in window.entries.item(0).text()
    assert history.entries.count() == 2 and original.activity in history.entries.item(1).text()
    assert state == (history.search_text.text(), history.outcome.currentData(), history.demand.currentData(),
                     history.start_date.date(), history.end_date.date(), history.start_enabled.isChecked(), history.end_enabled.isChecked())


def test_failed_restore_surfaces_error_and_keeps_current_state(backup_window, qapp, monkeypatch):
    window, session = backup_window
    _, snapshot, _ = seed(session)
    current = session.episodes.list()
    window.refresh()
    visible = [window.entries.item(row).text() for row in range(window.entries.count())]
    dialog = prepare(window, qapp, snapshot)
    plan = dialog.plan
    real_reopen = session._reopen
    calls = []

    def fail_once():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("Cannot reopen restored snapshot")
        real_reopen()

    monkeypatch.setattr(session, "_reopen", fail_once)
    dialog.confirmation.done(QMessageBox.StandardButton.Yes)
    assert "previous working journal was recovered" in dialog.status.text()
    assert session.episodes.list() == current
    assert visible == [window.entries.item(row).text() for row in range(window.entries.count())]
    assert plan.safety.path.exists()
    assert dialog.isVisible() and dialog.manual_button.isEnabled()


def test_closing_confirmation_discards_prepared_restore(backup_window, qapp):
    window, session = backup_window
    _, snapshot, _ = seed(session)
    current = session.episodes.list()
    dialog = prepare(window, qapp, snapshot)
    plan = dialog.plan
    window.close()
    qapp.processEvents()
    assert not plan.staged.exists() and plan.safety.path.exists()
    assert session.episodes.list() == current


def test_backup_surface_does_not_open_over_active_editor(backup_window, qapp):
    window, _ = backup_window
    window.new_episode()
    qapp.processEvents()
    window.backup_action.trigger()
    assert window.backup_dialog is None
    assert "Finish or close" in window.statusBar().currentMessage()
    window.editor.reject()


def test_automatic_backup_failure_is_visible_without_blocking_logging(backup_window, monkeypatch):
    window, session = backup_window

    def fail(*args, **kwargs):
        raise BackupError("Disk full")

    monkeypatch.setattr(session.backups, "daily_if_changed", fail)
    window._daily_backup()
    assert "Automatic backup failed" in window.statusBar().currentMessage()
    window.new_episode()
    assert window.editor is not None
    window.editor.reject()


def test_startup_explains_required_migration_backup_failure(qapp, tmp_path, monkeypatch):
    from zymptom.ui import app
    observed = []

    def fail(path):
        raise BackupError('Safety snapshot verification failed')

    monkeypatch.setattr(app, 'JournalSession', fail)
    monkeypatch.setattr(app.QMessageBox, 'critical', lambda *args: observed.append(args))
    assert app.run(tmp_path / 'unused.sqlite3') == 1
    assert 'Required backup failed' in observed[0] and 'not upgraded' in observed[0][2]
    assert list(tmp_path.iterdir()) == []


def test_unavailable_session_cannot_start_drafts_exports_or_manual_backups(backup_window, qapp):
    window, session = backup_window
    session.close()
    window.new_episode()
    window.open_export_dialog()
    assert window.editor is None and window.export_dialog is None
    window.open_backup_dialog()
    qapp.processEvents()
    dialog = window.backup_dialog
    assert not dialog.manual_button.isEnabled() and not dialog.restore_button.isEnabled()
    dialog.create_backup()
    assert 'journal is unavailable' in dialog.status.text()


@pytest.mark.parametrize("instant,local,offset,fold", [
    ("2026-10-22T01:10:15.123456+00:00", "2026-10-21 18:10", -7, 0),
    ("2026-01-22T01:10:00+00:00", "2026-01-21 17:10", -8, 0),
    ("2026-03-08T09:59:00+00:00", "2026-03-08 01:59", -8, 0),
    ("2026-03-08T10:01:00+00:00", "2026-03-08 03:01", -7, 0),
    ("2026-11-01T08:30:00+00:00", "2026-11-01 01:30", -7, 0),
    ("2026-11-01T09:30:00+00:00", "2026-11-01 01:30", -8, 1),
    ("2026-10-22T06:59:00+00:00", "2026-10-21 23:59", -7, 0),
    ("2026-10-22T07:01:00+00:00", "2026-10-22 00:01", -7, 0),
])
def test_daily_window_path_passes_aware_local_time_and_creates_backup(qapp, zone, database_path, monkeypatch, instant, local, offset, fold):
    instant = datetime.fromisoformat(instant)
    session = JournalSession(database_path)
    received = []
    daily = session.backups.daily_if_changed

    def capture(connection, *, now):
        received.append(now)
        return daily(connection, now=now)

    monkeypatch.setattr(session.backups, "daily_if_changed", capture)
    window = MainWindow(session.episodes, zone=zone, now=lambda: instant, backup_session=session)
    before = session.connection.total_changes
    try:
        # Call the real window path directly so a Qt slot exception cannot just
        # be printed to stderr while pytest reports a passing test.
        window._daily_backup()
        assert received[0].utcoffset() == timedelta(hours=offset)
        assert received[0].strftime("%Y-%m-%d %H:%M") == local
        assert received[0].astimezone(UTC) == instant
        assert received[0].fold == fold
        info, = session.backups.list()
        assert info.kind == "daily" and info.valid and info.created_at == instant
        assert session.connection.total_changes == before
        assert session.episodes.list() == []
    finally:
        window.close()
        session.close()
        qapp.processEvents()


def test_daily_window_uses_dst_rules_for_previous_backup_calendar_day(qapp, zone, database_path):
    clock = [datetime(2026, 11, 1, 7, 30, tzinfo=UTC)]  # Nov 1, 00:30 PDT
    session = JournalSession(database_path)
    window = MainWindow(session.episodes, zone=zone, now=lambda: clock[0], backup_session=session)
    try:
        window._daily_backup()
        session.episodes.record(activity="Disposable DST fixture", outcome=EpisodeOutcome.STOPPED_EARLY)
        before = session.connection.total_changes
        clock[0] = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)  # Still Nov 1, 23:30 PST
        window._daily_backup()
        assert len(session.backups.list()) == 1
        clock[0] = datetime(2026, 11, 2, 8, 1, tzinfo=UTC)  # Nov 2, 00:01 PST
        window._daily_backup()
        assert len(session.backups.list()) == 2
        assert session.connection.total_changes == before
    finally:
        window.close()
        session.close()
        qapp.processEvents()


def test_normal_startup_timer_with_default_clock_creates_verified_daily(qapp, database_path):
    session = JournalSession(database_path)
    window = MainWindow(session.episodes, backup_session=session)
    before = session.connection.total_changes
    try:
        window.show()
        qapp.processEvents()
        info, = session.backups.list()
        assert info.kind == "daily" and info.valid and info.created_at.utcoffset() is not None
        assert "Automatic backup created and verified" in window.statusBar().currentMessage()
        assert session.connection.total_changes == before
    finally:
        window.close()
        session.close()
        qapp.processEvents()


@pytest.mark.parametrize("zone,offset", [
    (QTimeZone(b"UTC"), 0),
    (QTimeZone(19800), 19800),
    (QTimeZone.fromSecondsAheadOfUtc(-12600), -12600),
])
def test_aware_local_conversion_supports_utc_and_fixed_qt_zones(zone, offset):
    instant = datetime(2026, 10, 25, 12, 34, 56, 123456, tzinfo=UTC)
    local = to_local_datetime(instant, zone)
    assert local.utcoffset() == timedelta(seconds=offset)
    assert local.astimezone(UTC) == instant
