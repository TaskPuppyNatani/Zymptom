"""Human-facing sandbox sync state and conflict summaries."""

from dataclasses import dataclass

from zymptom.persistence.backups import BackupError
from zymptom.sandbox.auth import AuthError
from zymptom.sandbox.isolation import IsolationError
from zymptom.sandbox.transport import IdentityCollision, TransportError
from zymptom.sync.contract import SyncError

from .labels import ADAPTATIONS, CONSEQUENCES, FUNCTIONAL_DEMANDS, HELP_LEVELS, OUTCOMES, RECOVERY_ACTIONS, RESUME_ABILITIES, SYMPTOMS
from .time import to_local_datetime


MESSAGES = {
    "offline": "Zymptom is offline. Your entries are safe on this device and can sync later.",
    "auth": "Your connection expired. Sign in again to continue syncing.",
    "permission": "Zymptom could not access your sync data.",
    "integrity": "Sync needs attention. Your local entries remain available.",
    "backup": "Zymptom could not create a safety backup, so incoming changes were not applied.",
    "signin": "Could not connect. Check your connection and try again.",
    "configuration": "Sync setup is incomplete. Check the sandbox setup and try again.",
    "cancelled": "Sign-in was cancelled.",
    "other": "Sync needs attention. Your local entries remain available.",
}

ENROLLMENT_MESSAGES = {
    "backup": "Zymptom could not create a safety backup, so your journal was not changed.",
    "history": "Zymptom found incomplete sync history and did not change your journal.",
    "other": "Zymptom could not prepare this journal for sync. Your entries were not changed.",
}


def enrollment_failure_kind(error):
    text = str(error).lower()
    if isinstance(error, BackupError) or (isinstance(error, SyncError) and "backup" in text):
        return "backup"
    if isinstance(error, SyncError):
        return "history"
    return "other"


def enrollment_failure_message(error):
    return ENROLLMENT_MESSAGES[enrollment_failure_kind(error)]


def enrollment_preview_text(preview):
    status_text = "Already prepared (local only)" if preview.enrolled else "Not yet prepared"
    identity_text = "Assigned" if preview.journal_id is not None else "Will be assigned during setup"
    backup_text = (
        "Verified safety backup created before setup"
        if preview.enrolled and not preview.safety_backup_required
        else "Required before setup (created automatically)"
    )
    return "\n".join([
        f"Enrollment status: {status_text}",
        f"Existing journal entries: {preview.entry_count}",
        f"Entries already carrying sync history: {preview.already_tracked_count}",
        f"Entries to receive bootstrap history: {preview.bootstrap_required_count}",
        f"Production journal identity: {identity_text}",
        f"Safety backup: {backup_text}",
    ])


def failure_kind(error):
    """Translate known service errors without exposing their messages to UI."""
    text = str(error).lower()
    if isinstance(error, BackupError):
        return "backup"
    if isinstance(error, SyncError) and "backup" in text:
        return "backup"
    if isinstance(error, (IdentityCollision, SyncError, IsolationError)):
        return "integrity"
    if isinstance(error, AuthError):
        if error.kind in MESSAGES:
            return error.kind
        if "cancelled or denied" in text:
            return "cancelled"
        return "auth" if "expired" in text else "offline" if "reach" in text else "signin"
    if isinstance(error, TransportError):
        if error.kind in MESSAGES:
            return error.kind
        if "expired" in text:
            return "auth"
        if "denied" in text:
            return "permission"
        if "unavailable" in text:
            return "offline"
        return "integrity"
    return "other"


@dataclass
class SyncState:
    connected: bool = False
    busy: bool = False
    connecting: bool = False
    conflicts: int = 0
    successful: bool = False
    failure: str | None = None

    @property
    def label(self):
        if self.connecting:
            return "Connecting…"
        if self.busy:
            return "Syncing…"
        if self.conflicts:
            return "Needs attention"
        if self.failure == "offline":
            return "Offline"
        if self.failure in {"auth", "cancelled"}:
            return "Not connected"
        if self.failure:
            return "Needs attention"
        if not self.connected:
            return "Not connected"
        return "Up to date" if self.successful else "Connected"

    @property
    def message(self):
        if self.conflicts and not self.busy:
            detail = MESSAGES.get(self.failure, "")
            return "An entry changed differently on two devices. Review the versions to continue." + (" " + detail if detail else "")
        return MESSAGES.get(self.failure, "")


def _tags(values, labels):
    return ", ".join(labels.get(value, value.replace("_", " ").capitalize()) for value in values) or "None recorded"


def candidate_text(change, zone):
    episode = change.episode
    if episode is None:
        return "This version deleted the entry."
    local = to_local_datetime(episode.occurred_at, zone).strftime("%b %d, %Y · %I:%M %p")
    recovery = "Not recorded" if episode.recovery_required is None else "Yes" if episode.recovery_required else "No"
    duration = "Not recorded" if episode.duration_minutes is None else f"{episode.duration_minutes} min"
    minutes = "Not recorded" if episode.recovery_minutes is None else f"{episode.recovery_minutes} min"
    return "\n".join([
        f"Activity: {episode.activity}", f"When: {local}", f"Duration: {duration}",
        f"Outcome: {OUTCOMES.get(episode.outcome, str(episode.outcome))}",
        f"Functional demands: {_tags(episode.functional_demands, FUNCTIONAL_DEMANDS)}",
        f"Symptoms: {_tags(episode.symptoms, SYMPTOMS)}", f"Symptom notes: {episode.symptom_notes if episode.symptom_notes is not None else 'Not recorded'}",
        f"Adaptations: {_tags(episode.adaptations, ADAPTATIONS)}",
        f"Help: {HELP_LEVELS.get(episode.help_level, 'Not recorded')}", f"Help notes: {episode.help_notes if episode.help_notes is not None else 'Not recorded'}",
        f"Consequences: {_tags(episode.consequences, CONSEQUENCES)}", f"Recovery required: {recovery}",
        f"Recovery actions: {_tags(episode.recovery_actions, RECOVERY_ACTIONS)}", f"Recovery time: {minutes}",
        f"Able to resume: {RESUME_ABILITIES.get(episode.resume_ability, 'Not recorded')}",
        f"Notes: {episode.notes if episode.notes is not None else 'Not recorded'}",
    ])
