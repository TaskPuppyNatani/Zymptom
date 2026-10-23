from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QDate, QTimeZone

from zymptom.domain import EpisodeOutcome
from zymptom.services import EpisodeService
from zymptom.services.exports import EpisodeExportService
from zymptom.ui.time import local_date_bounds


@pytest.fixture
def service(repository):
    return EpisodeService(repository)


@pytest.fixture
def zone():
    return QTimeZone(b"America/Los_Angeles")


def test_selection_uses_inclusive_local_dates_across_dst(service, repository, zone):
    records = [
        service.record(
            activity=activity,
            outcome=EpisodeOutcome.STOPPED_EARLY,
            occurred_at=datetime.fromisoformat(instant).replace(tzinfo=UTC),
        )
        for activity, instant in (
            ("before start", "2026-03-08T07:59:59.999999"),
            ("at start", "2026-03-08T08:00:00"),
            ("before end", "2026-03-09T06:59:59.999999"),
            ("at exclusive end", "2026-03-09T07:00:00"),
        )
    ]
    start = QDate(2026, 3, 8)
    end = QDate(2026, 3, 8)
    lower, upper = local_date_bounds(start, end, zone)

    assert (upper - lower).total_seconds() == 23 * 60 * 60
    selected = EpisodeExportService(service).select(occurred_from=lower, occurred_before=upper)
    assert [item.activity for item in selected] == ["at start", "before end"]
    assert len(repository.list()) == len(records)


def test_selection_without_bounds_includes_all_records_in_stable_order(service):
    late = service.record(
        activity="Later", outcome=EpisodeOutcome.STOPPED_EARLY,
        occurred_at=datetime(2026, 10, 21, 12, tzinfo=UTC),
    )
    early = service.record(
        activity="Earlier", outcome=EpisodeOutcome.STOPPED_EARLY,
        occurred_at=datetime(2026, 10, 20, 12, tzinfo=UTC),
    )

    selected = EpisodeExportService(service).select()

    assert selected == [early, late]
