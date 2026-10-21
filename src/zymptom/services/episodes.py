"""Small application-facing API without UI or framework dependencies."""

from typing import Any
from uuid import UUID

from zymptom.domain.enums import EpisodeOutcome
from zymptom.domain.models import FunctionalEpisode
from zymptom.persistence.repository import EpisodeRepository


class EpisodeService:
    def __init__(self, repository: EpisodeRepository) -> None:
        self.repository = repository

    def record(
        self, *, activity: str, outcome: EpisodeOutcome, **details: Any
    ) -> FunctionalEpisode:
        """Construct, validate, and save an episode using domain field names."""
        episode = FunctionalEpisode(activity=activity, outcome=outcome, **details)
        return self.repository.create(episode)

    def get(self, episode_id: UUID | str) -> FunctionalEpisode | None:
        return self.repository.get(episode_id)

    def update(self, episode: FunctionalEpisode) -> FunctionalEpisode:
        return self.repository.update(episode)

    def delete(self, episode_id: UUID | str) -> bool:
        return self.repository.delete(episode_id)

    def list(self) -> list[FunctionalEpisode]:
        return self.repository.list()
