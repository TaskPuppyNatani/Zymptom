import json
import time
from dataclasses import replace
from uuid import uuid4

import pytest

from zymptom.sandbox.auth import FirebaseSession, HttpError
from zymptom.sandbox.transport import FirestoreMailbox, IdentityCollision, TransportError
from zymptom.sync.contract import Change


class FakeFirestore:
    def __init__(self):
        self.documents = {}
        self.requests = []

    def __call__(self, url, *, headers, data=None):
        assert headers["Authorization"] == "Bearer firebase-test-token"
        self.requests.append((url, json.loads(data) if data else None))
        if data:
            write = json.loads(data)["writes"][0]
            assert write["currentDocument"] == {"exists": False}
            document = write["update"]
            key = document["name"]
            if key in self.documents:
                raise HttpError(409)
            if "updateTransforms" in write:
                assert write["updateTransforms"] == [{"fieldPath": "uploaded_at", "setToServerValue": "REQUEST_TIME"}]
                document["fields"]["uploaded_at"] = {"timestampValue": "2026-01-01T00:00:00Z"}
            self.documents[key] = document
            return {}
        path = url.split("/v1/", 1)[1].split("?", 1)[0]
        if "?" in url:
            return {"documents": [value for key, value in self.documents.items() if key.startswith(path + "/")]}
        if path not in self.documents:
            raise HttpError(404)
        return self.documents[path]


def mailbox(fake):
    return FirestoreMailbox("demo-project", FirebaseSession("user-a", "firebase-test-token", time.monotonic() + 3600), request=fake)


def test_identity_upload_duplicate_collision_and_pull(simulator, full_episode):
    fake = FakeFirestore()
    a, b = mailbox(fake), mailbox(fake)
    assert a.connect() == b.connect()
    simulator.desktop.episodes.repository.create(full_episode)
    change = simulator.desktop.sync.pending("test").changes[0]
    assert a.upload(change) == "uploaded"
    assert b.upload(change) == "already-uploaded"
    assert b.pull().changes == (change,)
    episode = replace(change.episode, notes="different")
    other = Change.build(record_id=change.record_id, episode=episode, device_id=change.device_id, operation="create", updated_at=change.updated_at, change_id=change.change_id)
    with pytest.raises(IdentityCollision):
        b.upload(other)
    assert b.pull().changes == (change,)
    urls = [url for url, _ in fake.requests]
    assert any("users/user-a/sandbox/identity" in url for url in urls)
    assert any(f"users/user-a/journals/{a.journal_id}/changes/{change.change_id}" in url for url in urls)


@pytest.mark.parametrize("status,message", [(401, "expired"), (403, "denied"), (None, "unavailable")])
def test_network_errors_are_safe(status, message):
    def fail(*_a, **_k):
        raise HttpError(status)
    with pytest.raises(TransportError, match=message):
        mailbox(fail).connect()


def test_malformed_remote_packet_is_rejected(simulator, full_episode):
    fake = FakeFirestore()
    client = mailbox(fake)
    client.connect()
    simulator.desktop.episodes.repository.create(full_episode)
    change = simulator.desktop.sync.pending("test").changes[0]
    client.upload(change)
    key = next(key for key in fake.documents if key.endswith(str(change.change_id)))
    fake.documents[key]["fields"]["canonical_payload_json"] = {"stringValue": "{broken"}
    with pytest.raises(TransportError, match="canonical"):
        client.pull()


def test_expired_auth_does_not_make_requests():
    fake = FakeFirestore()
    client = FirestoreMailbox("demo-project", FirebaseSession("user-a", "token", time.monotonic() - 1), request=fake)
    with pytest.raises(Exception, match="expired"):
        client.connect()
    assert fake.requests == []
