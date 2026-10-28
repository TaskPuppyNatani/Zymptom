"""Firebase user-token Firestore REST adapter for the existing immutable mailbox."""

import json
import re
import urllib.parse
from datetime import datetime
from uuid import UUID, uuid4

from zymptom.sync.contract import Change, SyncBatch, SyncError, canonical, parse_json
from .auth import FirebaseSession, HttpError, request_json


class TransportError(RuntimeError):
    def __init__(self, message, *, kind=None):
        super().__init__(message)
        self.kind = kind


class IdentityCollision(TransportError):
    pass


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid UUID")
    return value


def _fields(values):
    return {key: ({"integerValue": str(value)} if type(value) is int else {"stringValue": value}) for key, value in values.items()}


def _decode(document):
    try:
        fields = document["fields"]
        result = {}
        for key, value in fields.items():
            if set(value) == {"stringValue"} and isinstance(value["stringValue"], str):
                result[key] = value["stringValue"]
            elif set(value) == {"integerValue"} and isinstance(value["integerValue"], str):
                result[key] = int(value["integerValue"])
            elif set(value) == {"timestampValue"} and isinstance(value["timestampValue"], str):
                result[key] = value["timestampValue"]
            else:
                raise ValueError()
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TransportError("A sandbox mailbox document is malformed.") from None


class FirestoreMailbox:
    def __init__(self, project_id: str, session: FirebaseSession, *, request=request_json, journal_id: UUID | str | None = None):
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", session.uid):
            raise TransportError("Invalid sandbox project or account identity.")
        self.session = session
        self.request = request
        self.database = f"projects/{project_id}/databases/(default)"
        self.root = f"{self.database}/documents"
        self.url = f"https://firestore.googleapis.com/v1/{self.root}"
        if journal_id is not None:
            try:
                parsed = UUID(_uuid(str(journal_id)))
                if parsed.int == 0:
                    raise ValueError("Zero UUID")
            except (ValueError, TypeError, AttributeError):
                raise TransportError("The production journal identity is malformed.") from None
            self.identity_path = None
            self.journal_id = str(parsed)
        else:
            self.identity_path = f"users/{session.uid}/sandbox/identity"
            self.journal_id = None

    def _request(self, url, *, body=None):
        headers = {"Authorization": "Bearer " + self.session.valid_token()}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.request(url, headers=headers, data=json.dumps(body).encode() if body is not None else None)

    def _get(self, path):
        try:
            return self._request(f"{self.url}/{path}")
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def _create(self, path, fields, *, timestamp=False, timestamp_field="uploaded_at"):
        write = {"update": {"name": f"{self.root}/{path}", "fields": _fields(fields)}, "currentDocument": {"exists": False}}
        if timestamp:
            write["updateTransforms"] = [{"fieldPath": timestamp_field, "setToServerValue": "REQUEST_TIME"}]
        self._request(f"https://firestore.googleapis.com/v1/{self.database}/documents:commit", body={"writes": [write]})

    def _descriptor(self, document):
        data = _decode(document)
        required = {
            "owner_uid",
            "journal_id",
            "journal_kind",
            "contract_version",
            "record_schema_version",
            "published_at",
        }
        if (
            set(data) != required
            or set(document.get("fields", {}).get("published_at", {})) != {"timestampValue"}
            or data["owner_uid"] != self.session.uid
            or data["journal_id"] != self.journal_id
            or data["journal_kind"] != "production"
            or data["contract_version"] != 1
            or data["record_schema_version"] != 1
        ):
            raise TransportError("The production journal descriptor is malformed.")
        try:
            timestamp = datetime.fromisoformat(data["published_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError()
            return _uuid(data["journal_id"])
        except (ValueError, TypeError, AttributeError):
            raise TransportError("The production journal descriptor is malformed.") from None

    def ensure_descriptor(self):
        if self.identity_path is not None or self.journal_id is None:
            raise TransportError("Production journal descriptors cannot be published for the sandbox mailbox.")
        path = f"users/{self.session.uid}/journals/{self.journal_id}"
        fields = {
            "owner_uid": self.session.uid,
            "journal_id": self.journal_id,
            "journal_kind": "production",
            "contract_version": 1,
            "record_schema_version": 1,
        }
        try:
            document = self._get(path)
            if document is None:
                try:
                    self._create(path, fields, timestamp=True, timestamp_field="published_at")
                    return "published"
                except HttpError as failure:
                    document = self._get(path)
                    if document is None:
                        raise failure
            self._descriptor(document)
            return "already-published"
        except HttpError as exc:
            raise _network_error(exc) from None

    def _journal(self, document):
        data = _decode(document)
        if set(data) != {"owner_uid", "journal_id", "contract_version"} or data["owner_uid"] != self.session.uid or data["contract_version"] != 1:
            raise TransportError("The sandbox journal identity is malformed.")
        try:
            return _uuid(data["journal_id"])
        except (ValueError, TypeError, AttributeError):
            raise TransportError("The sandbox journal identity is malformed.") from None

    def connect(self):
        if self.identity_path is None and self.journal_id is not None:
            self.session.valid_token()
            return self.journal_id
        try:
            document = self._get(self.identity_path)
            if document is None:
                fields = {"owner_uid": self.session.uid, "journal_id": str(uuid4()), "contract_version": 1}
                try:
                    self._create(self.identity_path, fields)
                except HttpError as failure:
                    document = self._get(self.identity_path)
                    if document is None:
                        raise failure
                else:
                    document = {"fields": _fields(fields)}
            self.journal_id = self._journal(document)
            return self.journal_id
        except HttpError as exc:
            raise _network_error(exc) from None

    def _collection(self):
        if self.journal_id is None:
            raise TransportError("Open the sandbox journal before syncing.")
        return f"users/{self.session.uid}/journals/{self.journal_id}/changes"

    def _change(self, document, expected_id=None):
        data = _decode(document)
        required = {"owner_uid", "journal_id", "change_id", "record_id", "contract_version", "record_schema_version", "content_hash", "canonical_payload_json", "uploaded_at"}
        if set(data) != required or set(document["fields"]["uploaded_at"]) != {"timestampValue"} or data["owner_uid"] != self.session.uid or data["journal_id"] != self.journal_id or data["contract_version"] != 1 or data["record_schema_version"] != 1:
            raise TransportError("A sandbox mailbox document is malformed.")
        try:
            timestamp = datetime.fromisoformat(data["uploaded_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError()
            raw = data["canonical_payload_json"]
            change = Change.from_dict(parse_json(raw))
            if canonical(change.to_dict()) != raw or data["change_id"] != str(change.change_id) or data["record_id"] != str(change.record_id) or data["content_hash"] != change.content_hash or (expected_id is not None and data["change_id"] != expected_id):
                raise ValueError()
            return change, raw
        except (SyncError, ValueError, TypeError):
            raise TransportError("A sandbox Change failed canonical validation.") from None

    def upload(self, change: Change):
        raw = canonical(change.to_dict())
        path = f"{self._collection()}/{change.change_id}"
        fields = {"owner_uid": self.session.uid, "journal_id": self.journal_id, "change_id": str(change.change_id),
                  "record_id": str(change.record_id), "contract_version": 1, "record_schema_version": 1,
                  "content_hash": change.content_hash, "canonical_payload_json": raw}
        try:
            document = self._get(path)
            if document is None:
                try:
                    self._create(path, fields, timestamp=True)
                    return "uploaded"
                except HttpError as failure:
                    document = self._get(path)
                    if document is None:
                        raise failure
            _, existing = self._change(document, str(change.change_id))
            if existing != raw:
                raise IdentityCollision("A sandbox Change ID already has different content; it was not overwritten.")
            return "already-uploaded"
        except HttpError as exc:
            raise _network_error(exc) from None

    def pull(self):
        changes = []
        page = None
        pages = set()
        try:
            while True:
                params = {"pageSize": 1}
                if page:
                    params["pageToken"] = page
                result = self._request(f"{self.url}/{self._collection()}?" + urllib.parse.urlencode(params))
                documents = result.get("documents", [])
                if not isinstance(documents, list):
                    raise TransportError("The sandbox mailbox response is malformed.")
                for document in documents:
                    name = document.get("name", "")
                    if not name.startswith(f"{self.root}/{self._collection()}/"):
                        raise TransportError("A sandbox mailbox document has an invalid path.")
                    change, _ = self._change(document, name.rsplit("/", 1)[-1])
                    changes.append(change)
                page = result.get("nextPageToken")
                if not page:
                    break
                if not isinstance(page, str):
                    raise TransportError("The sandbox mailbox page token is malformed.")
                if page in pages:
                    raise TransportError("The sandbox mailbox pagination is invalid.")
                pages.add(page)
            return SyncBatch(tuple(changes))
        except HttpError as exc:
            raise _network_error(exc) from None


def _network_error(error):
    if error.status == 401:
        return TransportError("Sandbox sign-in expired. Sign in again.", kind="auth")
    if error.status == 403:
        return TransportError("Firebase denied the sandbox request. Local entries remain available.", kind="permission")
    return TransportError("The sandbox service is unavailable. Local entries remain available; retry later.", kind="offline")
