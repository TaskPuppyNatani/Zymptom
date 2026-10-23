import csv
import io
import json
from datetime import UTC, date, datetime

import pytest

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility
from zymptom.exports import (
    ExportRange, render_csv, render_json, render_markdown, render_summary, summarize,
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


def test_markdown_is_chronological_grouped_by_local_date_and_preserves_exact_text(full_episode):
    first = FunctionalEpisode(
        activity="Café — activity\nwith `fences` and *markup*",
        outcome=EpisodeOutcome.COMPLETED_WITH_ADAPTATION,
        occurred_at=datetime(2026, 10, 21, 18, tzinfo=UTC),
        symptom_notes="note with <markup> & text",
        help_level=HelpLevel.NONE,
        recovery_required=False,
        resume_ability=ResumeAbility.IMMEDIATELY,
    )
    export_range = ExportRange(date(2026, 10, 20), date(2026, 10, 21), ZONE)
    episodes = [first, full_episode]
    summary = summarize(episodes)

    report = render_markdown(episodes, summary, export_range)

    assert report.index("### October 20, 2026") < report.index("### October 21, 2026")
    assert "Activity (exact stored text)" in report
    assert "Café — activity\nwith `fences` and *markup*" in report
    assert "note with <markup> & text" in report
    assert "Functional demands" in report and '"sitting","concentration","sitting"' in report
    assert "Between" not in report  # Narrative remains factual and template-derived.
    assert render_markdown(episodes, summary, export_range) == report


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
