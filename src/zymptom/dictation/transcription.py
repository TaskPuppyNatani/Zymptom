"""Local whisper.cpp CLI adapter. No shell, HTTP client, or model downloads."""

import os
import shutil
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer

from .contracts import LocalTranscriber


class WhisperCppTranscriber(LocalTranscriber):
    def __init__(self, parent=None):
        super().__init__(parent)
        command = os.environ.get("ZYMPTOM_WHISPER_COMMAND", "whisper-cli")
        self.command = shutil.which(command)
        model = os.environ.get("ZYMPTOM_WHISPER_MODEL", "")
        self.model = Path(model).expanduser().resolve() if model else None
        self._process = None
        self._output = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(300_000)
        self._timeout.timeout.connect(lambda: self._fail("Local transcription timed out. Try a shorter recording."))

    def unavailable_reason(self) -> str | None:
        if not self.command or not self.model or not self.model.is_file():
            return (
                "Local speech transcription is unavailable. Install whisper.cpp's whisper-cli "
                "and set ZYMPTOM_WHISPER_MODEL to an existing local model file. "
                "ZYMPTOM_WHISPER_COMMAND may select the executable. Nothing is downloaded."
            )
        return None

    def start(self, audio: Path) -> None:
        self.cancel()
        reason = self.unavailable_reason()
        if reason:
            raise RuntimeError(reason)
        self._output = audio.parent / "transcript.txt"
        process = QProcess(self)
        self._process = process
        process.setProgram(self.command)
        process.setArguments([
            "-m", str(self.model), "-f", str(audio), "-l", "auto",
            "-nt", "-otxt", "-of", str(self._output.with_suffix("")),
        ])
        # Avoid retaining or displaying process diagnostics that may contain speech.
        process.setStandardOutputFile(QProcess.nullDevice())
        process.setStandardErrorFile(QProcess.nullDevice())
        process.finished.connect(self._finished)
        process.errorOccurred.connect(lambda _: self._fail("Local transcription could not run. Check the executable and model."))
        self._timeout.start()
        process.start()

    def _finished(self, code, status) -> None:
        if self._process is None:
            return
        if code != 0 or status != QProcess.ExitStatus.NormalExit:
            self._fail("Local transcription failed. Check the model and try again.")
            return
        try:
            text = self._output.read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError("No speech")
        except (OSError, UnicodeError, ValueError):
            self._fail("No transcript was produced. Check the microphone and local model, then try again.")
            return
        self.cancel()
        self.completed.emit(text)

    def _fail(self, message) -> None:
        if self._process is not None:
            self.cancel()
            self.failed.emit(message)

    def cancel(self) -> None:
        self._timeout.stop()
        process, self._process = self._process, None
        if process is not None:
            process.finished.disconnect()
            process.errorOccurred.disconnect()
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(3000)
            process.deleteLater()
