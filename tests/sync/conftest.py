import pytest

from zymptom.sync.simulator import LocalSimulator


@pytest.fixture
def simulator(tmp_path):
    value = LocalSimulator(tmp_path / "devices")
    yield value
    value.close()


@pytest.fixture
def shared(simulator, full_episode):
    simulator.desktop.episodes.repository.create(full_episode)
    simulator.exchange()
    assert simulator.desktop.episodes.list() == simulator.phone.episodes.list()
    return full_episode.id
