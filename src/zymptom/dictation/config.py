"""Persistent, user-local configuration for the whisper.cpp adapter."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


def configuration_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the XDG config file location, honoring an absolute XDG override."""
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home()).expanduser()
    config_home = Path(env.get("XDG_CONFIG_HOME", "")).expanduser()
    if not str(config_home) or not config_home.is_absolute():
        config_home = home / ".config"
    return config_home / "zymptom" / "config.json"


def read_saved_configuration(path: str | Path | None = None) -> dict[str, str]:
    """Read only the two supported local transcription settings.

    A missing, unreadable, malformed, or old config file is treated as an
    unconfigured installation; the dictation adapter reports missing paths in
    its existing UI instead of failing application startup.
    """
    config_file = Path(path) if path is not None else configuration_path()
    try:
        value = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: item.strip()
        for key in ("whisper_command", "whisper_model")
        if isinstance((item := value.get(key)), str) and item.strip()
    }


def load_whisper_configuration(
    environ: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> tuple[str, str | None]:
    """Return command/model settings, with environment variables taking priority."""
    env = os.environ if environ is None else environ
    saved = read_saved_configuration(config_path)
    command = env["ZYMPTOM_WHISPER_COMMAND"] if "ZYMPTOM_WHISPER_COMMAND" in env else saved.get("whisper_command", "whisper-cli")
    model = env["ZYMPTOM_WHISPER_MODEL"] if "ZYMPTOM_WHISPER_MODEL" in env else saved.get("whisper_model")
    model = model or None
    return command, model


def save_whisper_configuration(
    command: str | None = None,
    model: str | None = None,
    *,
    path: str | Path | None = None,
) -> Path:
    """Save supplied paths without discarding the other existing setting."""
    config_file = Path(path) if path is not None else configuration_path()
    values = read_saved_configuration(config_file)
    if command and command.strip():
        values["whisper_command"] = command.strip()
    if model and model.strip():
        values["whisper_model"] = model.strip()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_file.parent,
            prefix=f".{config_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            json.dump(values, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
        os.replace(temporary_path, config_file)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return config_file
