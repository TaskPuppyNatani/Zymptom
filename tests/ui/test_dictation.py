"""Dictation integration with fake microphones/engines and real temporary data."""

import os
import sys
import wave

import pytest
from PySide6.QtCore import QBuffer, QIODevice, QObject, QProcess, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from zymptom.dictation.capture import QtAudioCapture
from zymptom.dictation.contracts import AudioCapture, LocalTranscriber
from zymptom.dictation.session import DictationSession
from zymptom.dictation.transcription import WhisperCppTranscriber
from zymptom.domain import EpisodeOutcome
from zymptom.ui.dictation import DictationDialog, append_transcript


class FakeCapture(AudioCapture):
    def __init__(self):
        super().__init__()
        self.active = False
        self.paths = []
        self.error = None

    def start(self, destination):
        self.active = True
        self.paths.append(destination)
        destination.write_bytes(b"fake audio")
        if self.error:
            raise RuntimeError(self.error)

    def stop(self):
        self.active = False

    def cancel(self):
        self.active = False


class FakeTranscriber(LocalTranscriber):
    def __init__(self):
        super().__init__()
        self.reason = None
        self.active = False
        self.paths = []
        self.error = None

    def unavailable_reason(self):
        return self.reason

    def start(self, audio):
        assert audio.exists()
        self.active = True
        self.paths.append(audio)
        if self.error:
            raise RuntimeError(self.error)

    def cancel(self):
        self.active = False


@pytest.fixture
def rig(qapp):
    capture, transcriber = FakeCapture(), FakeTranscriber()
    sessions = []

    def session_factory(parent):
        session = DictationSession(parent, capture=capture, transcriber=transcriber)
        sessions.append(session)
        return session

    def dialog_factory(name, parent):
        return DictationDialog(name, parent, session_factory=session_factory)

    yield capture, transcriber, dialog_factory
    for session in sessions:
        session.cancel()


def assert_clean(capture, transcriber):
    assert not capture.active
    assert not transcriber.active
    assert all(not path.parent.exists() for path in capture.paths)


def open_dictation(make_editor, rig, field="activity"):
    capture, transcriber, factory = rig
    editor = make_editor(dictation_factory=factory)
    if field != "activity":
        editor.more_button.setChecked(True)
    editor.dictation_buttons[field].click()
    return editor, editor.dictation, capture, transcriber


def test_controls_are_accessible_and_only_offer_existing_free_text_fields(make_editor, rig):
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig)
    assert list(editor.dictation_buttons) == [
        "activity", "symptom_notes", "help_notes", "notes",
    ]
    assert not hasattr(editor, "dictation_target")
    assert not hasattr(editor, "dictate_button")
    names = set()
    for field, button in editor.dictation_buttons.items():
        assert "Dictate into" in button.accessibleName()
        assert button.toolTip()
        assert not button.icon().isNull()
        names.add(button.accessibleName())
    assert len(names) == 4
    assert dialog.session.state == "listening"
    assert "Listening…" in dialog.status.text()
    assert capture.active
    assert dialog.stop_button.isEnabled()
    assert not dialog.insert_button.isEnabled()
    assert "Review transcript" in dialog.transcript.accessibleName()
    assert os.stat(capture.paths[-1].parent).st_mode & 0o777 == 0o700


@pytest.mark.parametrize("field,existing,expected", [
    ("activity", "", "Walk outside"),
    ("activity", "Tried", "Tried Walk outside"),
    ("activity", "Walk 🐾", "Walk 🐾 Walk outside"),
    ("symptom_notes", "", "Walk\noutside"),
    ("symptom_notes", "Already recorded", "Already recorded\nWalk\noutside"),
    ("help_notes", "Helped.\n", "Helped.\nWalk\noutside"),
    ("notes", "Note: ", "Note: Walk\noutside"),
])
def test_review_edit_and_explicit_insertion_preserve_text_without_saving(
    make_editor, rig, service, field, existing, expected,
):
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig, field)
    widget = getattr(editor, field)
    if field == "activity":
        widget.setText(existing)
    else:
        widget.setPlainText(existing)
    untouched = {name: control.text() if name == "activity" else control.toPlainText()
                 for name, control in ((name, getattr(editor, name)) for name in editor.dictation_buttons)
                 if name != field}
    assert dialog.windowTitle() == f"Dictate — {widget.accessibleName()}"
    editor.outcome.setCurrentIndex(editor.outcome.findData(EpisodeOutcome.STOPPED_EARLY))
    before_structured = (editor.duration.text(), editor.outcome.currentData(), editor.help_level.currentData(),
                         editor.recovery_minutes.text(), editor.resume_ability.currentData(),
                         editor.tags["functional_demands"].values())
    dialog.stop_button.click()
    assert dialog.session.state == "transcribing"
    assert not capture.active and transcriber.active
    assert "Transcribing…" in dialog.status.text()
    editor.save()  # An editor shortcut cannot save while dictation is open.
    assert service.list() == []
    transcriber.completed.emit("unreviewed transcript")
    assert dialog.session.state == "review"
    assert_clean(capture, transcriber)
    assert (widget.text() if field == "activity" else widget.toPlainText()) == existing
    assert dialog.insert_button.isEnabled()
    dialog.transcript.setPlainText("  Walk\noutside  ")
    dialog.insert_button.click()
    assert editor.dictation is None
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert (widget.text() if field == "activity" else widget.toPlainText()) == expected
    assert service.list() == [] and editor.saved_episode is None
    assert editor.isVisible()
    assert {name: getattr(editor, name).text() if name == "activity" else getattr(editor, name).toPlainText()
            for name in untouched} == untouched
    assert (editor.duration.text(), editor.outcome.currentData(), editor.help_level.currentData(),
            editor.recovery_minutes.text(), editor.resume_ability.currentData(),
            editor.tags["functional_demands"].values()) == before_structured
    if field != "activity":
        assert editor.details.isVisible()
    widget.undo()
    assert (widget.text() if field == "activity" else widget.toPlainText()) == existing
    assert dialog.transcript.toPlainText() == ""


@pytest.mark.parametrize("stage", ["listening", "transcribing", "review"])
@pytest.mark.parametrize("close_editor", [False, True])
def test_cancel_or_editor_close_leaves_text_unchanged_and_cleans_up(
    make_editor, rig, service, stage, close_editor,
):
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig)
    editor.activity.setText("Original")
    if stage != "listening":
        dialog.stop_button.click()
    if stage == "review":
        transcriber.completed.emit("Discard me")
    if close_editor:
        editor.close()
    else:
        dialog.cancel_button.click()
    assert editor.activity.text() == "Original"
    assert editor.dictation is None
    assert_clean(capture, transcriber)
    transcriber.completed.emit("late result")
    assert editor.activity.text() == "Original"
    assert service.list() == []


def test_missing_backend_does_not_start_microphone_or_prevent_logging(make_editor, rig, service):
    capture, transcriber, factory = rig
    transcriber.reason = "Local speech transcription is unavailable."
    editor = make_editor(dictation_factory=factory)
    editor.dictation_buttons["activity"].click()
    assert editor.dictation.session.state == "error"
    assert "unavailable" in editor.dictation.status.text()
    assert capture.paths == []
    editor.dictation.cancel_button.click()
    editor.activity.setText("Typed normally")
    editor.outcome.setCurrentIndex(editor.outcome.findData(EpisodeOutcome.COMPLETED_NORMALLY))
    editor.save_button.click()
    assert service.list()[0].activity == "Typed normally"


@pytest.mark.parametrize("failure", ["microphone_start", "microphone_async", "transcription_start", "transcription_async", "empty"])
def test_failures_clean_temporary_audio_and_allow_retry(make_editor, rig, failure):
    capture, transcriber, factory = rig
    if failure == "microphone_start":
        capture.error = "No microphone is available."
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig)
    editor.activity.setText("Keep this")
    if failure == "microphone_async":
        capture.failed.emit("Microphone recording failed.")
    elif failure.startswith("transcription") or failure == "empty":
        if failure == "transcription_start":
            transcriber.error = "Local transcription failed."
        dialog.stop_button.click()
        if failure == "transcription_async":
            transcriber.failed.emit("Local transcription failed.")
        elif failure == "empty":
            transcriber.completed.emit("  ")
    assert dialog.session.state == "error"
    assert dialog.status.property("role") == "error"
    assert not dialog.insert_button.isEnabled()
    assert_clean(capture, transcriber)
    assert editor.activity.text() == "Keep this"
    capture.error = transcriber.error = None
    dialog.start_button.click()
    assert dialog.session.state == "listening"
    dialog.stop_button.click()
    transcriber.completed.emit("Now works")
    dialog.insert_button.click()
    assert editor.activity.text() == "Keep this Now works"
    assert_clean(capture, transcriber)


def test_repeated_dialogs_and_recording_limit(make_editor, rig):
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig)
    first = capture.paths[-1]
    dialog.session._limit.timeout.emit()
    assert dialog.session.state == "transcribing"
    transcriber.completed.emit("First")
    dialog.insert_button.click()
    editor.dictation_buttons["activity"].click()
    second = capture.paths[-1]
    assert first.parent != second.parent
    editor.dictation.stop_button.click()
    transcriber.completed.emit("Second")
    editor.dictation.insert_button.click()
    assert editor.activity.text() == "First Second"
    assert_clean(capture, transcriber)


def test_different_field_microphones_keep_their_own_targets(make_editor, rig, service):
    capture, transcriber, factory = rig
    editor = make_editor(dictation_factory=factory)
    editor.more_button.setChecked(True)
    for field in ("symptom_notes", "activity", "notes", "help_notes"):
        button = editor.dictation_buttons[field]
        widget = getattr(editor, field)
        assert button.isVisible()
        widget.setFocus()
        QTest.keyClick(widget, Qt.Key.Key_Tab)
        assert editor.focusWidget() is button
        QTest.keyClick(button, Qt.Key.Key_Space)
        dialog = editor.dictation
        assert dialog is not None
        assert dialog.windowTitle() == f"Dictate — {widget.accessibleName()}"
        dialog.stop_button.click()
        transcriber.completed.emit(f"Text for {field}")
        dialog.insert_button.click()
        assert_clean(capture, transcriber)
    assert editor.activity.text() == "Text for activity"
    assert editor.symptom_notes.toPlainText() == "Text for symptom_notes"
    assert editor.notes.toPlainText() == "Text for notes"
    assert editor.help_notes.toPlainText() == "Text for help_notes"
    assert service.list() == []


def test_normal_application_shutdown_cancels_capture(make_editor, rig, qapp):
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig)
    qapp.aboutToQuit.emit()
    assert dialog.session.state == "idle"
    assert_clean(capture, transcriber)


def test_existing_episode_stays_unchanged_until_explicit_editor_save(make_editor, rig, service, full_episode):
    service.repository.create(full_episode)
    capture, transcriber, factory = rig
    editor = make_editor(full_episode, dictation_factory=factory)
    editor.dictation_buttons["activity"].click()
    dialog = editor.dictation
    dialog.stop_button.click()
    transcriber.completed.emit("Additional words")
    dialog.insert_button.click()
    assert service.get(full_episode.id) == full_episode
    editor.save_button.click()
    assert service.get(full_episode.id).activity == full_episode.activity + " Additional words"


def test_escape_cancels_and_ctrl_s_cannot_save_during_dictation(make_editor, rig, service):
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig)
    editor.activity.setText("Typed words")
    editor.outcome.setCurrentIndex(editor.outcome.findData(EpisodeOutcome.COMPLETED_NORMALLY))
    QTest.keyClick(dialog, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert service.list() == []
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert editor.dictation is None
    assert editor.activity.text() == "Typed words"
    assert_clean(capture, transcriber)


def test_temporary_directory_failure_is_reported(make_editor, rig, monkeypatch):
    import zymptom.dictation.session as session_module

    def unavailable_storage(**kwargs):
        raise OSError("Temporary storage is unavailable.")

    monkeypatch.setattr(session_module, "TemporaryDirectory", unavailable_storage)
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig)
    assert dialog.session.state == "error"
    assert "Temporary storage" in dialog.status.text()
    assert capture.paths == []


def test_real_adapter_unavailable_keeps_editor_usable(make_editor, monkeypatch):
    monkeypatch.setenv("ZYMPTOM_WHISPER_COMMAND", "/missing-zymptom-test/whisper-cli")
    monkeypatch.delenv("ZYMPTOM_WHISPER_MODEL", raising=False)
    editor = make_editor()
    editor.dictation_buttons["activity"].click()
    assert editor.dictation.session.state == "error"
    assert "Local speech transcription is unavailable" in editor.dictation.status.text()
    editor.dictation.reject()
    assert editor.isVisible()


def test_blank_review_cannot_insert(make_editor, rig):
    editor, dialog, capture, transcriber = open_dictation(make_editor, rig)
    dialog.stop_button.click()
    transcriber.completed.emit("Words")
    dialog.transcript.setPlainText("  \n")
    dialog.accept()
    assert editor.dictation is dialog
    assert not dialog.insert_button.isEnabled()
    assert append_transcript("Original", "  ", multiline=True) == "Original"


def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "Timed out waiting for local test process"


@pytest.mark.parametrize("mode", ["success", "failed", "missing", "empty", "cancel", "timeout", "crash"])
def test_local_command_adapter_and_session_cleanup(qapp, tmp_path, monkeypatch, mode):
    command = tmp_path / "local speech command"
    command.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time, os, resource\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        "args = sys.argv[1:]\n"
        "assert '-m' in args and '-f' in args and '-otxt' in args\n"
        "assert pathlib.Path(args[args.index('-m') + 1]).is_file()\n"
        "assert pathlib.Path(args[args.index('-f') + 1]).is_file()\n"
        + {
            "success": "pathlib.Path(args[args.index('-of') + 1] + '.txt').write_text('Local words', encoding='utf-8')\n",
            "failed": "sys.exit(2)\n",
            "missing": "sys.exit(0)\n",
            "empty": "pathlib.Path(args[args.index('-of') + 1] + '.txt').write_text('  ')\n",
            "cancel": "time.sleep(30)\n",
            "timeout": "time.sleep(30)\n",
            "crash": "os.abort()\n",
        }[mode], encoding="utf-8",
    )
    command.chmod(0o700)
    model = tmp_path / "local model.bin"
    model.touch()
    monkeypatch.setenv("ZYMPTOM_WHISPER_COMMAND", str(command))
    monkeypatch.setenv("ZYMPTOM_WHISPER_MODEL", str(model))
    backend = WhisperCppTranscriber()
    capture = FakeCapture()
    session = DictationSession(capture=capture, transcriber=backend)
    results = []
    session.transcript_ready.connect(results.append)
    try:
        session.start()
        session.stop()
        process = backend._process
        states = [process.state()]
        process.stateChanged.connect(states.append)
        if mode == "cancel":
            wait_until(lambda: process.state() == QProcess.ProcessState.Running)
            session.cancel()
        elif mode == "timeout":
            backend._timeout.timeout.emit()
        else:
            wait_until(lambda: session.state in ("review", "error"))
        assert results == (["Local words"] if mode == "success" else [])
        assert session.state == ("review" if mode == "success" else "idle" if mode == "cancel" else "error")
        assert states[-1] == QProcess.ProcessState.NotRunning
        assert backend._process is None
        assert all(not path.parent.exists() for path in capture.paths)
    finally:
        session.cancel()


def test_backend_configuration_and_failed_process_start(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ZYMPTOM_WHISPER_COMMAND", str(tmp_path / "missing"))
    monkeypatch.delenv("ZYMPTOM_WHISPER_MODEL", raising=False)
    assert "unavailable" in WhisperCppTranscriber().unavailable_reason()
    executable = tmp_path / "removed-executable"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    model = tmp_path / "model.bin"
    model.touch()
    monkeypatch.setenv("ZYMPTOM_WHISPER_COMMAND", str(executable))
    monkeypatch.setenv("ZYMPTOM_WHISPER_MODEL", str(model))
    backend = WhisperCppTranscriber()
    assert backend.unavailable_reason() is None
    executable.unlink()
    capture = FakeCapture()
    session = DictationSession(capture=capture, transcriber=backend)
    session.start()
    session.stop()
    wait_until(lambda: session.state == "error")
    assert all(not path.parent.exists() for path in capture.paths)
    session.cancel()


@pytest.mark.parametrize("mode", ["success", "missing", "unsupported", "start_failure", "runtime_failure"])
def test_qt_capture_with_fake_audio_device(qapp, tmp_path, monkeypatch, mode):
    from PySide6 import QtMultimedia
    from PySide6.QtMultimedia import QtAudio

    class Microphone:
        def isNull(self):
            return mode == "missing"

        def isFormatSupported(self, format):
            return mode != "unsupported"

    class Devices:
        defaultAudioInput = staticmethod(Microphone)

    class Source(QObject):
        stateChanged = Signal(object)

        def __init__(self, microphone, format, parent):
            super().__init__(parent)
            self.stopped = False
            self.buffer = QBuffer(self)
            self.buffer.setData(b"\x00\x00" * 160)
            self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)

        def start(self):
            return self.buffer

        def error(self):
            return QtAudio.Error.OpenError if mode == "start_failure" else QtAudio.Error.NoError

        def state(self):
            return QtAudio.State.StoppedState if mode == "runtime_failure" else QtAudio.State.ActiveState

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(QtMultimedia, "QMediaDevices", Devices)
    monkeypatch.setattr(QtMultimedia, "QAudioSource", Source)
    capture = QtAudioCapture()
    audio = tmp_path / "capture.wav"
    failures = []
    capture.failed.connect(failures.append)
    try:
        if mode in ("missing", "unsupported", "start_failure"):
            with pytest.raises(RuntimeError):
                capture.start(audio)
        else:
            capture.start(audio)
            source = capture._source
            if mode == "runtime_failure":
                source.error = lambda: QtAudio.Error.IOError
                source.stateChanged.emit(QtAudio.State.StoppedState)
                assert failures == ["Microphone recording failed. Check your audio input and try again."]
            capture.stop()
            assert source.stopped
            with wave.open(str(audio)) as recording:
                assert (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) == (1, 2, 16000)
                assert recording.getnframes() == 160
    finally:
        capture.cancel()
    assert capture._source is None and capture._wav is None


def test_native_audio_signal_connects_without_enum_meta_slot(qapp, tmp_path, monkeypatch):
    """Exercise the installed signal's meta-object without opening a microphone.

    PySide6 6.12 advertises QAudio::State but cannot marshal that enum into a
    Python slot. No-argument slots may safely read the current state instead.
    """
    from PySide6 import QtMultimedia
    from PySide6.QtMultimedia import QAudioDevice, QAudioFormat, QAudioSource, QtAudio

    source = QAudioSource(QAudioDevice(), QAudioFormat())
    buffer = QBuffer(source)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    source.start = lambda: buffer
    source.state = lambda: QtAudio.State.StoppedState
    # Allow capture startup; the error will be introduced after connection.
    source.error = lambda: QtAudio.Error.NoError

    class Microphone:
        isNull = lambda self: False
        isFormatSupported = lambda self, format: True

    class Devices:
        defaultAudioInput = staticmethod(Microphone)

    monkeypatch.setattr(QtMultimedia, "QMediaDevices", Devices)
    monkeypatch.setattr(QtMultimedia, "QAudioSource", lambda *args: source)
    capture = QtAudioCapture()
    failures = []
    capture.failed.connect(failures.append)
    try:
        capture.start(tmp_path / "test.wav")
        meta = capture.metaObject()
        handlers = [meta.method(i) for i in range(meta.methodCount())
                    if meta.method(i).name().data() == b"_state_changed"]
        assert len(handlers) == 1
        assert handlers[0].parameterCount() == 0
        # Invoke through Qt's C++ meta-call path, just as a native signal does.
        source.error = lambda: QtAudio.Error.IOError
        assert handlers[0].invoke(capture, Qt.ConnectionType.DirectConnection)
        assert failures == ["Microphone recording failed. Check your audio input and try again."]
    finally:
        capture.cancel()
