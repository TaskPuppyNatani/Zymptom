import pytest

from zymptom.persistence.backups import BackupError
from zymptom.sandbox.auth import AuthError, DesktopConfig, firebase_exchange
from zymptom.sandbox.transport import IdentityCollision, TransportError
from zymptom.sync.contract import SyncError
from zymptom.ui.sync_presentation import SyncState, failure_kind


@pytest.mark.parametrize("state,label", [
    (SyncState(), "Not connected"),
    (SyncState(connected=True), "Connected"),
    (SyncState(busy=True, connecting=True), "Connecting…"),
    (SyncState(connected=True, busy=True), "Syncing…"),
    (SyncState(connected=True, successful=True), "Up to date"),
    (SyncState(connected=True, failure="offline"), "Offline"),
    (SyncState(failure="auth"), "Not connected"),
    (SyncState(connected=True, conflicts=1), "Needs attention"),
    (SyncState(connected=True, failure="integrity"), "Needs attention"),
])
def test_status_labels(state, label):
    assert state.label == label


@pytest.mark.parametrize("error,kind", [
    (AuthError("Sandbox sign-in expired. Sign in again."), "auth"),
    (AuthError("Google sign-in was cancelled or denied."), "cancelled"),
    (TransportError("The sandbox service is unavailable."), "offline"),
    (TransportError("Firebase denied the sandbox request."), "permission"),
    (IdentityCollision("private details"), "integrity"),
    (SyncError("private details"), "integrity"),
    (BackupError("private details"), "backup"),
    (SyncError("A verified safety backup is required"), "backup"),
    (AuthError("private details", kind="offline"), "offline"),
    (TransportError("private details", kind="permission"), "permission"),
])
def test_errors_are_mapped_without_exposing_details(error, kind):
    assert failure_kind(error) == kind
    assert "private details" not in SyncState(failure=kind).message


def test_auth_exposes_only_a_safe_display_account():
    config = DesktopConfig("key", "demo-project", "client")
    result = {"localId": "uid", "idToken": "private-token", "expiresIn": "3600", "email": "test@example.test"}
    session = firebase_exchange(config, "credential", request=lambda *_args, **_kwargs: result)
    assert session.email == "test@example.test"
    assert "private-token" not in repr(session) and "test@example.test" not in repr(session)
    result["email"] = "test@example.test\nInjected UI text"
    session = firebase_exchange(config, "credential", request=lambda *_args, **_kwargs: result)
    assert session.email is None


def test_conflict_does_not_hide_connection_error():
    state = SyncState(conflicts=1, failure="offline")
    assert state.label == "Needs attention"
    assert "Review the versions" in state.message and "offline" in state.message
