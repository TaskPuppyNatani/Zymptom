import csv
import io
import json
from datetime import UTC, date, datetime

import pytest

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility
from zymptom.exports import (
    ExportRange, render_csv, render_json, render_markdown, render_summary,
    render_summary_details, summarize,
)

ZONE = "America/Los_Angeles"


def test_json_has_version_stable_values_and_round_trips_all_episode_data(full_episode):
    export_range = ExportRange(date(2026, 10, 1), date(2026, 10, 31), ZONE)
    encoded = render_json([full_episode], export_range)
    document = json.loads(encoded)

    assert document["schema"] == "zymptom.functional-episodes"
    assert document["schema_version"] == 1
    assert document["date_range"] == {
        "start": "2026-10-01", "end": "2026-10-31", "timezone": ZONE,
    }
    assert document["episodes"] == [{
        "id": str(full_episode.id),
        "occurred_at": "2026-10-20T15:30:12.345678+00:00",
        "activity": full_episode.activity,
        "duration_minutes": 32,
        "outcome": EpisodeOutcome.STOPPED_EARLY.value,
        "functional_demands": ["sitting", "concentration", "sitting"],
        "symptoms": ["fatigue", "pain"],
        "symptom_notes": full_episode.symptom_notes,
        "adaptations": ["changed_position", "reduced_task"],
        "help_level": HelpLevel.SOME.value,
        "help_notes": full_episode.help_notes,
        "consequences": ["missed_next_activity", "needed_rest"],
        "recovery_required": True,
        "recovery_actions": ["lay_down", "quiet_room"],
        "recovery_minutes": 45,
        "resume_ability": ResumeAbility.LATER_SAME_DAY.value,
        "notes": full_episode.notes,
        "created_at": "2026-10-20T16:00:00.000000+00:00",
        "updated_at": "2026-10-20T16:01:00.000000+00:00",
    }]
    assert render_json([full_episode], export_range) == encoded


def test_json_keeps_missing_optional_values_as_null():
    episode = FunctionalEpisode(activity="A task", outcome=EpisodeOutcome.COMPLETED_NORMALLY)

    record = json.loads(render_json([episode], ExportRange(None, None, ZONE)))["episodes"][0]

    assert record["duration_minutes"] is None
    assert record["symptom_notes"] is None
    assert record["help_level"] is None
    assert record["recovery_required"] is None
    assert record["resume_ability"] is None
    assert record["notes"] is None


def test_csv_escapes_unicode_quotes_commas_and_newlines_and_encodes_lists(full_episode):
    full_episode.activity = 'Café, "reading"\nand writing'
    full_episode.notes = "Line one, with comma.\nLine two says \"☕\"."
    full_episode.symptoms = ["pain, intermittent", 'quoted "tag"', "雪"]

    encoded = render_csv([full_episode])
    rows = list(csv.DictReader(io.StringIO(encoded, newline="")))

    assert len(rows) == 1
    assert rows[0]["activity"] == full_episode.activity
    assert rows[0]["notes"] == full_episode.notes
    assert json.loads(rows[0]["symptoms"]) == full_episode.symptoms
    assert rows[0]["recovery_required"] == "true"
    assert rows[0]["outcome"] == EpisodeOutcome.STOPPED_EARLY.value
    assert render_csv([full_episode]) == encoded


def test_csv_keeps_empty_and_missing_values_distinct():
    episode = FunctionalEpisode(
        activity="Task", outcome=EpisodeOutcome.STOPPED_EARLY,
        recovery_required=False, duration_minutes=0, notes="",
    )
    row = next(csv.DictReader(io.StringIO(render_csv([episode]), newline="")))

    assert row["recovery_required"] == "false"
    assert row["duration_minutes"] == "0"
    assert row["notes"] == ""
    assert row["help_level"] == ""


def test_markdown_is_human_readable_and_preserves_multiline_notes(full_episode):
    first = FunctionalEpisode(
        activity="Café — activity\nwith `fences` and *markup*",
        outcome=EpisodeOutcome.COMPLETED_WITH_ADAPTATION,
        occurred_at=datetime(2026, 10, 21, 18, tzinfo=UTC),
        functional_demands=["sensory_tolerance"],
        symptoms=["sensory_overload", "cognitive_difficulty"],
        adaptations=["changed_position"],
        help_level=HelpLevel.NONE,
        recovery_required=False,
        consequences=["needed_rest"],
        recovery_actions=["lay_down"],
        resume_ability=ResumeAbility.IMMEDIATELY,
        notes="Line one\nLine two ☕",
    )
    full_episode.notes = "Rested after reading.\nContinued later."
    export_range = ExportRange(date(2026, 10, 20), date(2026, 10, 21), ZONE)
    episodes = [first, full_episode]
    summary = summarize(episodes)

    report = render_markdown(episodes, summary, export_range)

    assert report.index("### October 20, 2026") < report.index("### October 21, 2026")
    assert "#### 8:30 AM PDT" in report and "#### 11:00 AM PDT" in report
    assert "**Outcome:** Stopped early" in report
    assert "**Outcome:** Completed with adaptation" in report
    assert "Sensory tolerance" in report and "Sensory overload" in report
    assert "Cognitive difficulty" in report and "Changed position" in report
    assert "Needed rest" in report and "Lay down" in report
    assert "stopped_early" not in report and "sensory_overload" not in report
    assert "Functional demands" in report
    assert "- Sitting" in report and "- Concentration" in report
    assert "- Sensory tolerance" in report
    assert '["sitting"' not in report and '["sensory_tolerance"' not in report
    assert "**Activity:** Café — activity  \nwith \\`fences\\` and \\*markup\\*" in report
    assert "**Notes:** Line one  \nLine two ☕" in report
    assert "**Notes:** Rested after reading\\.  \nContinued later\\." in report
    assert "**Recovery required:** Not required" in report
    assert "Completed with adaptation: 1" in report
    assert "Stopped early: 1" in report
    assert "Recovery was recorded as required after 1 attempt" in report
    assert "not required after 1 attempt" in report
    assert str(full_episode.id) not in report
    assert "Timestamp (UTC)" not in report
    assert "Created (UTC)" not in report and "Updated (UTC)" not in report
    assert "2026-10-20T15:30" not in report
    assert render_markdown(episodes, summary, export_range) == report


def test_markdown_shows_missing_values_without_inference_and_keeps_zero_values():
    episode = FunctionalEpisode(
        activity="Short task",
        outcome=EpisodeOutcome.STOPPED_EARLY,
        occurred_at=datetime(2026, 10, 20, 15, tzinfo=UTC),
        duration_minutes=0,
        recovery_required=None,
        recovery_minutes=0,
    )
    report = render_markdown(
        [episode], summarize([episode]), ExportRange(None, None, ZONE),
    )

    assert "**Duration:** 0 minutes" in report
    assert "**Recovery required:** Not recorded" in report
    assert "**Recovery duration:** 0 minutes" in report
    assert "**Help:** Not recorded" in report
    assert "**Resume ability:** Not recorded" in report
    assert "**Symptoms:** None recorded" in report
    assert "**Notes:** Not recorded" in report
    assert "- Required: 0" in report
    assert "- Not required: 0" in report
    assert "- Not recorded: 1" in report


def test_markdown_summary_details_use_display_labels_without_changing_counts(full_episode):
    summary = summarize([full_episode])
    details = render_summary_details(summary, ExportRange(None, None, ZONE))

    assert "- Stopped early: 1" in details
    assert "- Some help: 1" in details
    assert "- Later the same day: 1" in details
    assert "- Sitting: 1" in details
    assert "- Changed position: 1" in details
    assert "stopped_early" not in details
    assert "later_same_day" not in details
    assert summary.total_attempts == 1
    assert summary.outcome_groups["stopped_early"] == 1


def test_empty_markdown_and_summary_name_the_empty_range():
    export_range = ExportRange(date(2026, 10, 1), date(2026, 10, 31), ZONE)
    summary = summarize([])

    narrative = render_summary(summary, export_range)
    report = render_markdown([], summary, export_range)

    assert narrative == "No functional attempts were recorded for October 1, 2026 through October 31, 2026."
    assert "No episodes matched this date range." in report
    assert "Activity duration: no attempts" in report


def test_export_range_rejects_reversed_dates_and_unknown_zone():
    with pytest.raises(ValueError, match="Start date"):
        ExportRange(date(2026, 10, 2), date(2026, 10, 1), ZONE)
    with pytest.raises(ValueError, match="Timezone"):
        ExportRange(None, None, "not/a-zone")
