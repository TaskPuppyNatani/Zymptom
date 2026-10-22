from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QDate, QDateTime, QTime

from zymptom.domain import EpisodeOutcome, FunctionalEpisode
from zymptom.ui.time import local_date_bounds, to_local, to_utc


@pytest.mark.parametrize("instant,display,offset", [
    (datetime(2026, 10, 22, 1, 10, tzinfo=UTC), "2026-10-21 18:10", -7 * 3600),
    (datetime(2026, 1, 22, 1, 10, tzinfo=UTC), "2026-01-21 17:10", -8 * 3600),
    (datetime(2026, 11, 1, 8, 30, tzinfo=UTC), "2026-11-01 01:30", -7 * 3600),
    (datetime(2026, 11, 1, 9, 30, tzinfo=UTC), "2026-11-01 01:30", -8 * 3600),
])
def test_aware_timestamp_display_and_round_trip(zone, instant, display, offset):
    local = to_local(instant, zone)
    assert local.toString("yyyy-MM-dd HH:mm") == display
    assert local.offsetFromUtc() == offset
    assert to_utc(local) == instant


@pytest.mark.parametrize("instant", [
    datetime(2026, 10, 22, 1, 10, 15, 123456, tzinfo=UTC),
    datetime(2026, 11, 1, 8, 30, 0, 123456, tzinfo=UTC),
    datetime(2026, 11, 1, 9, 30, 0, 123456, tzinfo=UTC),
])
def test_unedited_time_retains_exact_instant_including_dst_fold(make_editor, service, instant):
    episode = service.record(activity="Walk", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=instant)
    editor = make_editor(episode)
    editor.activity.setText("Edited activity")
    editor.save_button.click()
    assert service.get(episode.id).occurred_at == instant


def test_changing_local_time_saves_the_correct_aware_utc_instant(make_editor, service):
    episode = service.record(
        activity="Walk", outcome=EpisodeOutcome.STOPPED_EARLY,
        occurred_at=datetime(2026, 10, 21, 21, 10, tzinfo=UTC),
    )
    editor = make_editor(episode)
    editor.when.setTime(QTime(15, 45))
    editor.save_button.click()
    loaded = service.get(episode.id)
    assert loaded.occurred_at == datetime(2026, 10, 21, 22, 45, tzinfo=UTC)
    assert loaded.occurred_at.tzinfo is UTC


def test_invalid_time_is_rejected():
    with pytest.raises(ValueError, match="valid date and time"):
        to_utc(QDateTime())


@pytest.mark.parametrize("day,start,end,hours", [
    (QDate(2026, 3, 8), datetime(2026, 3, 8, 8, tzinfo=UTC), datetime(2026, 3, 9, 7, tzinfo=UTC), 23),
    (QDate(2026, 11, 1), datetime(2026, 11, 1, 7, tzinfo=UTC), datetime(2026, 11, 2, 8, tzinfo=UTC), 25),
    (QDate(2026, 10, 21), datetime(2026, 10, 21, 7, tzinfo=UTC), datetime(2026, 10, 22, 7, tzinfo=UTC), 24),
])
def test_local_date_range_uses_calendar_days_across_dst(zone, day, start, end, hours):
    assert local_date_bounds(day, day, zone) == (start, end)
    assert (end - start).total_seconds() == hours * 3600
    assert local_date_bounds(day, None, zone) == (start, None)
    assert local_date_bounds(None, day, zone) == (None, end)
    assert local_date_bounds(None, None, zone) == (None, None)
