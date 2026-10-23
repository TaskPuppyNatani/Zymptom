"""Factual aggregate calculations over saved FunctionalEpisode records."""

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility


@dataclass(frozen=True)
class EpisodeFrequency:
    value: str
    episodes: int


@dataclass(frozen=True)
class DurationStats:
    recorded_count: int
    missing_count: int
    total_minutes: int | None
    minimum_minutes: int | None
    maximum_minutes: int | None


@dataclass(frozen=True)
class FunctionalSummary:
    total_attempts: int
    outcome_counts: dict[str, int]
    outcome_groups: dict[str, int]
    functional_demands: tuple[EpisodeFrequency, ...]
    symptoms: tuple[EpisodeFrequency, ...]
    adaptations: tuple[EpisodeFrequency, ...]
    activities: tuple[EpisodeFrequency, ...]
    help_level_counts: dict[str, int]
    episodes_with_help: int
    recovery_required_counts: dict[str, int]
    duration: DurationStats
    recovery_duration: DurationStats
    resume_ability_counts: dict[str, int]


def _frequencies(episodes: list[FunctionalEpisode], field: str) -> tuple[EpisodeFrequency, ...]:
    counts: Counter[str] = Counter()
    for episode in episodes:
        # Count episodes that contain a value, rather than duplicate tag rows.
        counts.update(set(getattr(episode, field)))
    return tuple(
        EpisodeFrequency(value, count)
        for value, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold(), pair[0]))
    )


def _durations(episodes: list[FunctionalEpisode], field: str) -> DurationStats:
    values = [value for episode in episodes if (value := getattr(episode, field)) is not None]
    return DurationStats(
        recorded_count=len(values),
        missing_count=len(episodes) - len(values),
        total_minutes=sum(values) if values else None,
        minimum_minutes=min(values) if values else None,
        maximum_minutes=max(values) if values else None,
    )


def summarize(records: Iterable[FunctionalEpisode]) -> FunctionalSummary:
    """Calculate stable counts only; missing values remain distinct from zero."""
    episodes = list(records)
    outcomes = {outcome.value: 0 for outcome in EpisodeOutcome}
    help_levels = {level.value: 0 for level in HelpLevel}
    help_levels["not_recorded"] = 0
    resume = {ability.value: 0 for ability in ResumeAbility}
    resume["not_recorded"] = 0
    recovery_required = {"yes": 0, "no": 0, "not_recorded": 0}
    activities: Counter[str] = Counter()
    with_help = 0
    explicitly_helped = {
        HelpLevel.SOME,
        HelpLevel.SIGNIFICANT,
        HelpLevel.OTHER_PERSON_COMPLETED_PART,
        HelpLevel.OTHER_PERSON_COMPLETED_ALL,
    }

    for episode in episodes:
        outcomes[episode.outcome.value] += 1
        activities[episode.activity] += 1
        if episode.help_level is None:
            help_levels["not_recorded"] += 1
        else:
            help_levels[episode.help_level.value] += 1
        if episode.outcome is EpisodeOutcome.COMPLETED_WITH_HELP or episode.help_level in explicitly_helped:
            with_help += 1
        if episode.recovery_required is True:
            recovery_required["yes"] += 1
        elif episode.recovery_required is False:
            recovery_required["no"] += 1
        else:
            recovery_required["not_recorded"] += 1
        if episode.resume_ability is None:
            resume["not_recorded"] += 1
        else:
            resume[episode.resume_ability.value] += 1

    completed_outcomes = (
        EpisodeOutcome.COMPLETED_NORMALLY,
        EpisodeOutcome.COMPLETED_WITH_DIFFICULTY,
        EpisodeOutcome.COMPLETED_WITH_ADAPTATION,
        EpisodeOutcome.COMPLETED_WITH_HELP,
    )
    outcome_groups = {
        "completed": sum(outcomes[outcome.value] for outcome in completed_outcomes),
        "stopped_early": outcomes[EpisodeOutcome.STOPPED_EARLY.value],
        "unable_to_complete": outcomes[EpisodeOutcome.UNABLE_TO_COMPLETE.value],
        "avoided_due_to_limitation": outcomes[EpisodeOutcome.AVOIDED_DUE_TO_LIMITATION.value],
    }
    return FunctionalSummary(
        total_attempts=len(episodes),
        outcome_counts=outcomes,
        outcome_groups=outcome_groups,
        functional_demands=_frequencies(episodes, "functional_demands"),
        symptoms=_frequencies(episodes, "symptoms"),
        adaptations=_frequencies(episodes, "adaptations"),
        activities=tuple(
            EpisodeFrequency(value, count)
            for value, count in sorted(activities.items(), key=lambda pair: (-pair[1], pair[0].casefold(), pair[0]))
        ),
        help_level_counts=help_levels,
        episodes_with_help=with_help,
        recovery_required_counts=recovery_required,
        duration=_durations(episodes, "duration_minutes"),
        recovery_duration=_durations(episodes, "recovery_minutes"),
        resume_ability_counts=resume,
    )
