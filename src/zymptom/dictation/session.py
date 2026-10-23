"""Own one cancellable dictation attempt and its temporary data."""

from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from .capture import QtAudioCapture
from .transcription import WhisperCppTranscriber


class DictationSession(QObject):
    changed = Signal(str, str)
    transcript_ready = Signal(str)

    def __init__(self, parent=None, *, capture=None, transcriber=None):
        super().__init__(parent)
        self.capture = capture if capture is not None else QtAudioCapture(self)
        self.transcriber = transcriber if transcriber is not None else WhisperCppTranscriber(self)
        self.state = "idle"
        self._temporary = None
        self.capture.failed.connect(self._fail)
        self.transcriber.failed.connect(self._fail)
        self.transcriber.completed.connect(self._completed)
        self._limit = QTimer(self)
        self._limit.setSingleShot(True)
        self._limit.setInterval(120_000)
        self._limit.timeout.connect(self.stop)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel)

    def _set_state(self, state, message):
        self.state = state
        self.changed.emit(state, message)

    def start(self):
        self.cancel()
        if self._temporary is not None:
            return  # Do not start again if cleanup still needs a retry.
        try:
            reason = self.transcriber.unavailable_reason()
            if reason:
                self._set_state("error", reason)
                return
            self._temporary = TemporaryDirectory(prefix="zymptom-dictation-")
            self.audio = Path(self._temporary.name) / "recording.wav"
            self._set_state("listening", "Listening… Speak, then choose Stop. Recording stops after 2 minutes.")
            self.capture.start(self.audio)
            if self.state == "listening":
                self._limit.start()
        except (OSError, RuntimeError, ImportError) as exc:
            self._set_state("error", str(exc) or "Could not start local dictation.")
            self._cleanup()

    def stop(self):
        if self.state != "listening":
            return
        self._limit.stop()
        try:
            self.capture.stop()
            if self.state != "listening":  # Capture can signal failure while draining.
                return
            self._set_state("transcribing", "Transcribing… Audio stays on this computer.")
            self.transcriber.start(self.audio)
        except (OSError, RuntimeError) as exc:
            self._fail(str(exc) or "Local transcription failed.")

    def _completed(self, text):
        if self.state != "transcribing":
            return
        if not text.strip():
            self._fail("No speech was transcribed. Try again.")
            return
        if not self._cleanup():
            return
        self._set_state("review", "Review and edit the transcript, then choose Insert text.")
        self.transcript_ready.emit(text)

    def _fail(self, message):
        if self.state not in ("listening", "transcribing"):
            return
        self._set_state("error", message)
        self._cleanup()

    def _cleanup(self):
        self._limit.stop()
        clean = True
        for release in (self.capture.cancel, self.transcriber.cancel):
            try:
                release()
            except (OSError, RuntimeError):
                clean = False
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
                self._temporary = None
            except OSError:
                clean = False
        if not clean:
            self._set_state("error", "Could not fully clean up local dictation. Close dictation and try again.")
        return clean

    def cancel(self):
        # Change state first: late callbacks cannot revive a cancelled attempt.
        self._set_state("idle", "Ready for local dictation.")
        self._cleanup()
