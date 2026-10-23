from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility
from zymptom.exports import summarize


def test_summary_aggregates_stored_values_and_distinguishes_missing_from_zero(full_episode):
    with_help = FunctionalEpisode(
        activity="Café — errands",
        outcome=EpisodeOutcome.COMPLETED_WITH_HELP,
        duration_minutes=0,
        functional_demands=["walking", "walking"],
        symptoms=["fatigue"],
        adaptations=["changed_position"],
        help_level=HelpLevel.NONE,
        recovery_required=False,
        recovery_minutes=0,
        resume_ability=ResumeAbility.UNKNOWN,
    )
    unable = FunctionalEpisode(activity="Rest", outcome=EpisodeOutcome.UNABLE_TO_COMPLETE)

    summary = summarize([full_episode, with_help, unable])

    assert summary.total_attempts == 3
    assert summary.outcome_counts == {
        "completed_normally": 0,
        "completed_with_difficulty": 0,
        "completed_with_adaptation": 0,
        "completed_with_help": 1,
        "stopped_early": 1,
        "unable_to_complete": 1,
        "avoided_due_to_limitation": 0,
    }
    assert summary.outcome_groups == {
        "completed": 1,
        "stopped_early": 1,
        "unable_to_complete": 1,
        "avoided_due_to_limitation": 0,
    }
    assert [(item.value, item.episodes) for item in summary.functional_demands] == [
        ("concentration", 1), ("sitting", 1), ("walking", 1),
    ]
    assert [(item.value, item.episodes) for item in summary.symptoms] == [
        ("fatigue", 2), ("pain", 1),
    ]
    assert [(item.value, item.episodes) for item in summary.adaptations] == [
        ("changed_position", 2), ("reduced_task", 1),
    ]
    assert [(item.value, item.episodes) for item in summary.activities] == [
        ("Café — errands", 1), ("Homework — reading and writing", 1), ("Rest", 1),
    ]
    assert summary.help_level_counts == {
        "none": 1,
        "some": 1,
        "significant": 0,
        "other_person_completed_part": 0,
        "other_person_completed_all": 0,
        "not_recorded": 1,
    }
    assert summary.episodes_with_help == 2
    assert summary.recovery_required_counts == {"yes": 1, "no": 1, "not_recorded": 1}
    assert summary.duration.recorded_count == 2
    assert summary.duration.missing_count == 1
    assert summary.duration.total_minutes == 32
    assert summary.duration.minimum_minutes == 0
    assert summary.duration.maximum_minutes == 32
    assert summary.recovery_duration.recorded_count == 2
    assert summary.recovery_duration.missing_count == 1
    assert summary.recovery_duration.total_minutes == 45
    assert summary.recovery_duration.minimum_minutes == 0
    assert summary.recovery_duration.maximum_minutes == 45
    assert summary.resume_ability_counts == {
        "immediately": 0,
        "after_short_break": 0,
        "later_same_day": 1,
        "next_day": 0,
        "could_not_resume": 0,
        "unknown": 1,
        "not_recorded": 1,
    }


def test_empty_summary_has_zero_attempts_and_no_duration_values():
    summary = summarize([])

    assert summary.total_attempts == 0
    assert all(count == 0 for count in summary.outcome_counts.values())
    assert summary.duration.recorded_count == 0
    assert summary.duration.missing_count == 0
    assert summary.duration.total_minutes is None
    assert summary.recovery_duration.total_minutes is None
    assert summary.functional_demands == ()
