"""Atomic export-file replacement."""

import os
import tempfile
from pathlib import Path


class ExportWriteError(OSError):
    """An export could not be written without disturbing an existing file."""


def write_atomically(path: str | os.PathLike[str], contents: str) -> None:
    destination = Path(path)
    descriptor = None
    temporary_path = None

    def cleanup() -> None:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        descriptor = None  # The stream now owns the descriptor.
        with stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
    except OSError as exc:
        cleanup()
        raise ExportWriteError(f"Could not write export file: {exc}") from exc
    except BaseException:
        cleanup()
        raise
