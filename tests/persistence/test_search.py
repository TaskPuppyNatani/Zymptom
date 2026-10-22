from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, ValidationError
from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.services import EpisodeService


@pytest.fixture
def episodes(repository):
    records = [
        FunctionalEpisode(
            id=UUID(int=1), activity="Homework reading", outcome=EpisodeOutcome.STOPPED_EARLY,
            occurred_at=datetime(2026, 10, 20, 18, tzinfo=UTC),
            functional_demands=["sitting", "concentration", "sitting"],
            notes="Complete round-trip", symptoms=["fatigue"],
        ),
        FunctionalEpisode(
            id=UUID(int=2), activity="HOMEWORK writing", outcome=EpisodeOutcome.COMPLETED_WITH_DIFFICULTY,
            occurred_at=datetime(2026, 10, 21, 18, tzinfo=UTC), functional_demands=["sitting"],
        ),
        FunctionalEpisode(
            id=UUID(int=3), activity="Grocery shopping", outcome=EpisodeOutcome.UNABLE_TO_COMPLETE,
            occurred_at=datetime(2026, 10, 22, 1, tzinfo=UTC), functional_demands=["walking", "custom demand"],
        ),
    ]
    for record in records:
        repository.create(record)
    return records


def test_history_query_newest_first_without_changing_list(repository, episodes):
    assert repository.search() == list(reversed(episodes))
    assert repository.list() == episodes


def test_query_round_trip_after_database_reopen(repository, episodes, database_path):
    with closing(connect_database(database_path)) as connection:
        service = EpisodeService(EpisodeRepository(connection))
        assert service.search(activity="READ") == [episodes[0]]
        assert service.functional_demands() == ["concentration", "custom demand", "sitting", "walking"]


@pytest.mark.parametrize("text", ["homework", "HoMe", " work "])
def test_case_insensitive_partial_activity_search(repository, episodes, text):
    assert repository.search(activity=text) == [episodes[1], episodes[0]]


@pytest.mark.parametrize("text", ["", "  \t\n"])
def test_blank_activity_search(repository, episodes, text):
    assert repository.search(activity=text) == list(reversed(episodes))


def test_unicode_casefold_and_literal_punctuation(repository):
    episode = FunctionalEpisode(activity="Café Straße — 100%_done 'quoted'", outcome=EpisodeOutcome.STOPPED_EARLY)
    repository.create(episode)
    for text in ("CAFÉ", "STRASSE", "%_", "'quoted'"):
        assert repository.search(activity=text) == [episode]
    assert repository.search(activity="%' OR 1=1 --") == []


@pytest.mark.parametrize("outcome", list(EpisodeOutcome))
def test_every_outcome_can_be_filtered(repository, episodes, outcome):
    expected = [episode for episode in reversed(episodes) if episode.outcome == outcome]
    assert repository.search(outcome=outcome) == expected


def test_demand_filter_matches_exact_stored_tags_without_duplicate_episodes(repository, episodes):
    assert repository.search(functional_demand="sitting") == [episodes[1], episodes[0]]
    assert repository.search(functional_demand="custom demand") == [episodes[2]]
    assert repository.search(functional_demand="sit") == []
    assert repository.search(functional_demand="Sitting") == []


def test_date_bounds_are_inclusive_start_exclusive_end(repository, episodes):
    assert repository.search(occurred_from=episodes[1].occurred_at, occurred_before=episodes[2].occurred_at) == [episodes[1]]
    assert repository.search(occurred_from=episodes[1].occurred_at) == [episodes[2], episodes[1]]
    assert repository.search(occurred_before=episodes[1].occurred_at) == [episodes[0]]
    local_start = episodes[1].occurred_at.astimezone(timezone(timedelta(hours=-7)))
    assert repository.search(occurred_from=local_start) == [episodes[2], episodes[1]]


def test_filters_compose_with_and(repository, episodes):
    assert repository.search(
        activity="homework", outcome=EpisodeOutcome.STOPPED_EARLY,
        functional_demand="sitting", occurred_from=datetime(2026, 10, 20, tzinfo=UTC),
        occurred_before=datetime(2026, 10, 21, tzinfo=UTC),
    ) == [episodes[0]]
    assert repository.search(activity="homework", functional_demand="walking") == []


def test_invalid_query_dates_and_outcome_rejected(repository):
    with pytest.raises(ValidationError, match="timezone-aware"):
        repository.search(occurred_from=datetime(2026, 10, 20))
    with pytest.raises(ValueError):
        repository.search(outcome="display label")
    with pytest.raises(ValueError, match="start"):
        repository.search(occurred_from=datetime(2026, 10, 22, tzinfo=UTC), occurred_before=datetime(2026, 10, 21, tzinfo=UTC))


def test_newest_first_ties_have_deterministic_uuid_order(repository):
    when = datetime(2026, 10, 21, tzinfo=UTC)
    low = FunctionalEpisode(id=UUID(int=1), activity="Low", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=when)
    high = FunctionalEpisode(id=UUID(int=2), activity="High", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=when)
    repository.create(low)
    repository.create(high)
    assert repository.search() == [high, low]


def test_demand_choices_follow_persisted_edits_and_deletes(repository, episodes):
    episodes[0].functional_demands = ["custom 'quoted' demand"]
    repository.update(episodes[0])
    repository.delete(episodes[2].id)
    assert repository.functional_demands() == ["custom 'quoted' demand", "sitting"]
