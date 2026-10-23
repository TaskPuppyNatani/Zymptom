"""Small asynchronous ports for capture and local speech engines."""

from pathlib import Path

from PySide6.QtCore import QObject, Signal


class AudioCapture(QObject):
    failed = Signal(str)

    def start(self, destination: Path) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        """Finish the recording and release the microphone."""
        raise NotImplementedError

    def cancel(self) -> None:
        """Release all resources, including after a partially failed start."""
        raise NotImplementedError


class LocalTranscriber(QObject):
    completed = Signal(str)
    failed = Signal(str)

    def unavailable_reason(self) -> str | None:
        raise NotImplementedError

    def start(self, audio: Path) -> None:
        raise NotImplementedError

    def cancel(self) -> None:
        """Stop processing before returning, so temporary files can be removed."""
        raise NotImplementedError
