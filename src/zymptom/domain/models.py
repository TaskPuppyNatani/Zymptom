"""The atomic record: one real-world attempt to perform an activity."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from .enums import EpisodeOutcome, HelpLevel, ResumeAbility
from .validation import normalize_timestamp, parse_enum, parse_id, validate_episode


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(kw_only=True)
class FunctionalEpisode:
    activity: str
    outcome: EpisodeOutcome
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=_now)
    duration_minutes: int | None = None
    functional_demands: list[str] = field(default_factory=list)
    symptoms: list[str] = field(default_factory=list)
    symptom_notes: str | None = None
    adaptations: list[str] = field(default_factory=list)
    help_level: HelpLevel | None = None
    help_notes: str | None = None
    consequences: list[str] = field(default_factory=list)
    recovery_required: bool | None = None
    recovery_actions: list[str] = field(default_factory=list)
    recovery_minutes: int | None = None
    resume_ability: ResumeAbility | None = None
    notes: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        self.id = parse_id(self.id)
        self.outcome = parse_enum(EpisodeOutcome, self.outcome, "outcome")
        if self.help_level is not None:
            self.help_level = parse_enum(HelpLevel, self.help_level, "help_level")
        if self.resume_ability is not None:
            self.resume_ability = parse_enum(ResumeAbility, self.resume_ability, "resume_ability")
        for field_name in ("occurred_at", "created_at", "updated_at"):
            setattr(self, field_name, normalize_timestamp(getattr(self, field_name), field_name))
        validate_episode(self)
