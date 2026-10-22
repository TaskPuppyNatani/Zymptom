"""Small application-facing API without UI or framework dependencies."""

from __future__ import annotations

from datetime import datetime
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

    def search(
        self, *, activity: str = "", outcome: EpisodeOutcome | None = None,
        functional_demand: str | None = None, occurred_from: datetime | None = None,
        occurred_before: datetime | None = None,
    ) -> list[FunctionalEpisode]:
        """Query history with composable filters, in newest-first order."""
        return self.repository.search(
            activity=activity, outcome=outcome, functional_demand=functional_demand,
            occurred_from=occurred_from, occurred_before=occurred_before,
        )

    def functional_demands(self) -> list[str]:
        return self.repository.functional_demands()
