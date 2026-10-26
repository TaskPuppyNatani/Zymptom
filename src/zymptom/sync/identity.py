"""Installation identity, independent of journal files and restored snapshots."""

import fcntl
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from zymptom.dictation.config import configuration_path
from zymptom.domain.validation import parse_id
from zymptom.persistence.backups import sync_directory


def parse_device_id(value: UUID | str) -> UUID:
    identity = parse_id(value)
    if identity.int == 0:
        raise ValueError("An installation device ID cannot use the reserved zero UUID")
    return identity


class DeviceIdentity:
    def __init__(self, path: Path | None = None):
        self.path = (Path(path) if path is not None else configuration_path().with_name("device.json")).resolve()

    def get(self) -> UUID:
        return self._load_or_reset(reset=False)

    def reset(self) -> UUID:
        """Explicit installation reset; existing revision origins are unchanged."""
        return self._load_or_reset(reset=True)

    def _load_or_reset(self, *, reset: bool) -> UUID:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists() and not reset:
                try:
                    value = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(value, dict) or not isinstance(value.get("device_id"), str):
                        raise ValueError()
                    identity = parse_device_id(value["device_id"])
                    if type(value.get("version")) is not int or value["version"] != 1:
                        raise ValueError()
                    return identity
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError("The installation device identity is invalid. Reset it explicitly instead of silently replacing it.") from exc
            identity = uuid4()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({"version": 1, "device_id": str(identity)}, stream, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                sync_directory(self.path.parent)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return identity
