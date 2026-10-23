import json
import shutil

from zymptom.dictation.config import (
    configuration_path,
    load_whisper_configuration,
    save_whisper_configuration,
)
from zymptom.dictation.transcription import WhisperCppTranscriber


def make_executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_xdg_configuration_loads_persisted_paths_with_spaces(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config with spaces"))
    command = make_executable(tmp_path / "whisper tools" / "whisper-cli")
    model = tmp_path / "models" / "small model.bin"
    model.parent.mkdir()
    model.touch()

    path = save_whisper_configuration(str(command), str(model))

    assert path == configuration_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_whisper_configuration() == (str(command), str(model))
    backend = WhisperCppTranscriber()
    assert backend.command == shutil.which(str(command))
    assert backend.model == model.resolve()
    assert backend.unavailable_reason() is None


def test_environment_overrides_each_saved_setting(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    saved_command = make_executable(tmp_path / "saved" / "whisper-cli")
    saved_model = tmp_path / "saved model.bin"
    saved_model.touch()
    save_whisper_configuration(str(saved_command), str(saved_model), path=config)

    override_command = make_executable(tmp_path / "override" / "whisper-cli")
    override_model = tmp_path / "override model.bin"
    override_model.touch()
    monkeypatch.setenv("ZYMPTOM_WHISPER_COMMAND", str(override_command))
    monkeypatch.setenv("ZYMPTOM_WHISPER_MODEL", str(override_model))

    assert load_whisper_configuration(config_path=config) == (
        str(override_command), str(override_model),
    )
    backend = WhisperCppTranscriber()
    assert backend.command == shutil.which(str(override_command))
    assert backend.model == override_model.resolve()


def test_missing_and_invalid_configuration_fail_gracefully(tmp_path, monkeypatch):
    config = tmp_path / "zymptom" / "config.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ZYMPTOM_WHISPER_COMMAND", raising=False)
    monkeypatch.delenv("ZYMPTOM_WHISPER_MODEL", raising=False)

    backend = WhisperCppTranscriber()
    assert backend.unavailable_reason()

    config.parent.mkdir()
    config.write_text("{invalid json", encoding="utf-8")
    backend = WhisperCppTranscriber()
    assert backend.unavailable_reason()

    config.write_text(json.dumps({
        "whisper_command": "invalid\u0000command",
        "whisper_model": "invalid\u0000model",
        "unrelated_state": "ignored",
    }), encoding="utf-8")
    backend = WhisperCppTranscriber()
    assert backend.unavailable_reason()
    assert backend.command is None and backend.model is None
    assert backend._process is None
