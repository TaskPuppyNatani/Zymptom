"""Disposable two-device developer simulator. No network or user database access."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from zymptom.domain import EpisodeOutcome
from zymptom.services.journal import JournalSession

from .identity import DeviceIdentity
from .service import SyncService


class DeviceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = DeviceIdentity(self.root / "installation" / "device.json")
        self.session = JournalSession(self.root / "journal.sqlite3", device_id=self.identity.get())
        self.episodes = self.session.episodes
        self.sync = SyncService(self.episodes, before_merge=self.session.before_destructive_change)

    def close(self):
        self.session.close()


class LocalSimulator:
    def __init__(self, root: Path):
        self.desktop = DeviceStore(Path(root) / "desktop")
        self.phone = DeviceStore(Path(root) / "phone")

    def exchange(self):
        results = []
        for sender, receiver, destination, source in (
            (self.desktop, self.phone, "phone", "desktop"),
            (self.phone, self.desktop, "desktop", "phone"),
        ):
            batch = sender.sync.pending(destination)
            results.append(receiver.sync.apply(batch.to_json(), source_provider=source))
            sender.sync.acknowledge(destination, (change.change_id for change in batch.changes))
        return tuple(results)

    def close(self):
        self.desktop.close()
        self.phone.close()


def main():
    with TemporaryDirectory(prefix="zymptom-sync-simulator-") as directory:
        simulator = LocalSimulator(Path(directory))
        try:
            episode = simulator.desktop.episodes.record(activity="Synthetic desktop attempt", outcome=EpisodeOutcome.STOPPED_EARLY)
            simulator.phone.episodes.record(activity="Synthetic phone attempt", outcome=EpisodeOutcome.COMPLETED_NORMALLY)
            simulator.exchange()
            converged = simulator.desktop.episodes.list() == simulator.phone.episodes.list()
            for device, text in ((simulator.desktop, "Desktop candidate"), (simulator.phone, "Phone candidate")):
                draft = device.episodes.get(episode.id)
                draft.notes = text
                device.episodes.update(draft)
            simulator.exchange()
            print(json.dumps({
                "independent_creates_converged": converged,
                "episode_count": len(simulator.desktop.episodes.list()),
                "desktop_conflicts": len(simulator.desktop.sync.unresolved_conflicts()),
                "phone_conflicts": len(simulator.phone.sync.unresolved_conflicts()),
                "concurrent_edits_remain_distinct": simulator.desktop.episodes.list() != simulator.phone.episodes.list(),
            }, sort_keys=True))
        finally:
            simulator.close()


if __name__ == "__main__":
    main()
