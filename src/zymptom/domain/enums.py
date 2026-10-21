"""Stable machine-readable values used in records and persistence."""

from enum import StrEnum


class EpisodeOutcome(StrEnum):
    COMPLETED_NORMALLY = "completed_normally"
    COMPLETED_WITH_DIFFICULTY = "completed_with_difficulty"
    COMPLETED_WITH_ADAPTATION = "completed_with_adaptation"
    COMPLETED_WITH_HELP = "completed_with_help"
    STOPPED_EARLY = "stopped_early"
    UNABLE_TO_COMPLETE = "unable_to_complete"
    AVOIDED_DUE_TO_LIMITATION = "avoided_due_to_limitation"


class HelpLevel(StrEnum):
    NONE = "none"
    SOME = "some"
    SIGNIFICANT = "significant"
    OTHER_PERSON_COMPLETED_PART = "other_person_completed_part"
    OTHER_PERSON_COMPLETED_ALL = "other_person_completed_all"


class ResumeAbility(StrEnum):
    IMMEDIATELY = "immediately"
    AFTER_SHORT_BREAK = "after_short_break"
    LATER_SAME_DAY = "later_same_day"
    NEXT_DAY = "next_day"
    COULD_NOT_RESUME = "could_not_resume"
    UNKNOWN = "unknown"
