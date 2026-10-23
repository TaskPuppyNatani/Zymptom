"""Qt desktop microphone capture to a private, session-owned PCM WAV file."""

import wave
from pathlib import Path

from .contracts import AudioCapture


class QtAudioCapture(AudioCapture):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = None
        self._device = None
        self._wav = None
        self._connected = False

    def start(self, destination: Path) -> None:
        # Multimedia is optional at runtime; failure must not prevent app launch.
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices, QtAudio

        self.cancel()
        microphone = QMediaDevices.defaultAudioInput()
        if microphone.isNull():
            raise RuntimeError("No microphone is available. Check your audio input and permissions.")
        format = QAudioFormat()
        format.setSampleRate(16000)
        format.setChannelCount(1)
        format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not microphone.isFormatSupported(format):
            raise RuntimeError("The microphone does not support 16 kHz mono PCM recording.")
        self._wav = wave.open(str(destination), "wb")
        self._wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        self._source = QAudioSource(microphone, format, self)
        self._device = self._source.start()
        if self._device is None or self._source.error() != QtAudio.Error.NoError:
            raise RuntimeError("Could not start the microphone. Check your audio input and permissions.")
        self._device.readyRead.connect(self._read)
        self._source.stateChanged.connect(self._state_changed)
        self._connected = True

    def _read(self) -> None:
        try:
            if self._device is not None and self._wav is not None:
                self._wav.writeframesraw(bytes(self._device.readAll()))
        except (OSError, wave.Error):
            self.failed.emit("Microphone recording failed. Check available disk space and try again.")

    def _state_changed(self) -> None:
        from PySide6.QtMultimedia import QtAudio

        # PySide6 6.12 advertises QAudio::State on the signal but cannot convert
        # it into a Python slot argument. Discard that argument and query the
        # source instead, keeping this callback's Qt meta-signature enum-free.
        source = self._source
        if source is not None and source.state() == QtAudio.State.StoppedState and source.error() != QtAudio.Error.NoError:
            self.failed.emit("Microphone recording failed. Check your audio input and try again.")

    def stop(self) -> None:
        if self._source is not None:
            self._read()
        self.cancel()

    def cancel(self) -> None:
        source, self._source = self._source, None
        device, self._device = self._device, None
        recording, self._wav = self._wav, None
        try:
            if source is not None:
                # Disconnect before stopping to avoid reentrant error handling.
                if self._connected:
                    device.readyRead.disconnect(self._read)
                    source.stateChanged.disconnect(self._state_changed)
                self._connected = False
                source.stop()
                source.deleteLater()
        finally:
            if recording is not None:
                try:
                    recording.close()
                except wave.Error as exc:
                    raise RuntimeError("Could not finish microphone recording.") from exc
