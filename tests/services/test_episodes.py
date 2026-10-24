import pytest

from zymptom.domain import EpisodeOutcome
from zymptom.services import EpisodeService


@pytest.mark.parametrize("field", ["functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions"])
def test_service_record_and_update_keep_ordered_unique_values(repository, field):
    service = EpisodeService(repository)
    episode = service.record(
        activity="An attempt", outcome=EpisodeOutcome.STOPPED_EARLY,
        **{field: ["z-last", "a-first", "z-last", "雪"]},
    )
    assert getattr(episode, field) == ["z-last", "a-first", "雪"]
    assert service.get(episode.id) == episode

    setattr(episode, field, ["雪", "z-last", "雪", "new", "z-last"])
    assert service.update(episode) is episode
    assert getattr(episode, field) == ["雪", "z-last", "new"]
    assert service.get(episode.id) == episode


def test_service_reads_leave_legacy_duplicates_unchanged(repository, connection, legacy_episode):
    service = EpisodeService(repository)
    changes = connection.total_changes
    assert service.get(legacy_episode.id) == legacy_episode
    assert service.list() == [legacy_episode]
    assert service.search(activity="Homework") == [legacy_episode]
    assert connection.total_changes == changes
