"""Episode selection for portable exports, through the existing service API."""

from datetime import datetime

from zymptom.domain import FunctionalEpisode

from .episodes import EpisodeService


class EpisodeExportService:
    def __init__(self, episodes: EpisodeService) -> None:
        self.episodes = episodes

    def select(
        self, *, occurred_from: datetime | None = None, occurred_before: datetime | None = None,
    ) -> list[FunctionalEpisode]:
        if occurred_from is not None and occurred_before is not None and occurred_from >= occurred_before:
            raise ValueError("The start must be earlier than the exclusive end.")
        selected = self.episodes.search(occurred_from=occurred_from, occurred_before=occurred_before)
        return sorted(selected, key=lambda episode: (episode.occurred_at, str(episode.id)))
