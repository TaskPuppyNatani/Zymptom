"""Install or remove Zymptom's user-level Linux desktop launcher."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOGO_RELATIVE_PATH = Path("src/zymptom/ui/assets/taskpuppy_kreations_logo.png")


@dataclass(frozen=True)
class IntegrationPaths:
    desktop_file: Path
    launcher: Path
    icon: Path
    config_file: Path


def user_data_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home()).expanduser()
    data_home = Path(env.get("XDG_DATA_HOME", "")).expanduser()
    if not str(data_home) or not data_home.is_absolute():
        data_home = home / ".local" / "share"
    return data_home


def integration_paths(environ: Mapping[str, str] | None = None) -> IntegrationPaths:
    from zymptom.dictation.config import configuration_path

    data_home = user_data_home(environ)
    app_dir = data_home / "zymptom"
    return IntegrationPaths(
        desktop_file=data_home / "applications" / "zymptom.desktop",
        launcher=app_dir / "zymptom-launch",
        icon=app_dir / "zymptom.png",
        config_file=configuration_path(environ),
    )


def _escape_desktop_exec_argument(value: str | Path) -> str:
    escaped = str(value).replace("\\", "\\\\")
    for character in ('"', "`", "$"):
        escaped = escaped.replace(character, "\\" + character)
    return f'"{escaped}"'


def _escape_desktop_value(value: str | Path) -> str:
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\t", "\\t").replace("\r", "\\r"))


def render_desktop_entry(launcher: str | Path, icon: str | Path) -> str:
    """Render a desktop entry whose executable is safe when paths contain spaces."""
    return "\n".join((
        "[Desktop Entry]",
        "Version=1.0",
        "Type=Application",
        "Name=Zymptom",
        "Comment=Local functional journal",
        f"Exec={_escape_desktop_exec_argument(launcher)}",
        f"Icon={_escape_desktop_value(icon)}",
        "Terminal=false",
        "Categories=Office;",
        "StartupNotify=true",
        "",
    ))


def render_launcher_script(repo_root: str | Path, python_executable: str | Path) -> str:
    root = shlex.quote(str(Path(repo_root).resolve()))
    # Keep the venv's interpreter path intact: resolving python may follow its
    # symlink into the system interpreter and skip the venv site-packages.
    python = shlex.quote(str(Path(python_executable).absolute()))
    return f'''#!/bin/sh
APP_ROOT={root}
PYTHON={python}

show_failure() {{
    status=$1
    output=$2
    if [ -s "$output" ] && command -v zenity >/dev/null 2>&1; then
        zenity --text-info --title="Zymptom could not start" --filename="$output" --width=800 --height=500
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send --urgency=critical "Zymptom could not start" "The application exited with status $status."
    fi
}}

output=$(mktemp "${{TMPDIR:-/tmp}}/zymptom-launch.XXXXXX") || exit 1
trap 'rm -f "$output"' EXIT
if ! cd "$APP_ROOT"; then
    printf '%s\\n' "The Zymptom source checkout is unavailable: $APP_ROOT" >"$output"
    show_failure 1 "$output"
    exit 1
fi
if [ ! -x "$PYTHON" ]; then
    printf '%s\\n' "The Zymptom virtual environment is unavailable: $PYTHON" >"$output"
    show_failure 1 "$output"
    exit 1
fi
"$PYTHON" -m zymptom "$@" >"$output" 2>&1
status=$?
if [ "$status" -ne 0 ]; then
    show_failure "$status" "$output"
fi
exit "$status"
'''


def _initial_whisper_settings(environ: Mapping[str, str], home: Path) -> tuple[str | None, str | None]:
    command = environ.get("ZYMPTOM_WHISPER_COMMAND")
    model = environ.get("ZYMPTOM_WHISPER_MODEL")
    if command or model:
        return command, model

    default_command = home / "Projects" / "whisper.cpp" / "build" / "bin" / "whisper-cli"
    default_model = home / "Projects" / "whisper.cpp" / "models" / "ggml-small.en.bin"
    if default_command.is_file() and os.access(default_command, os.X_OK) and default_model.is_file():
        return str(default_command), str(default_model)
    return None, None


def install_desktop(
    repo_root: str | Path = REPOSITORY_ROOT,
    *,
    environ: Mapping[str, str] | None = None,
    whisper_command: str | None = None,
    whisper_model: str | None = None,
) -> IntegrationPaths:
    env = os.environ if environ is None else environ
    root = Path(repo_root).resolve()
    python = root / ".venv" / "bin" / "python"
    logo = root / LOGO_RELATIVE_PATH
    if not python.is_file() or not os.access(python, os.X_OK):
        raise FileNotFoundError(f"Zymptom's virtual environment was not found at {python}; install dependencies first.")
    if not logo.is_file():
        raise FileNotFoundError(f"Zymptom's packaged branding image was not found at {logo}.")

    paths = integration_paths(env)
    from zymptom.dictation.config import read_saved_configuration, save_whisper_configuration

    command = whisper_command
    model = whisper_model
    if command is None and model is None:
        command = env.get("ZYMPTOM_WHISPER_COMMAND") or None
        model = env.get("ZYMPTOM_WHISPER_MODEL") or None
    if command is not None or model is not None:
        save_whisper_configuration(command, model, path=paths.config_file)
    elif not paths.config_file.exists() and not read_saved_configuration(paths.config_file):
        command, model = _initial_whisper_settings(env, Path(env.get("HOME") or Path.home()).expanduser())
        if command or model:
            save_whisper_configuration(command, model, path=paths.config_file)

    paths.desktop_file.parent.mkdir(parents=True, exist_ok=True)
    paths.launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(logo, paths.icon)
    os.chmod(paths.icon, 0o644)
    paths.launcher.write_text(render_launcher_script(root, python), encoding="utf-8")
    os.chmod(paths.launcher, 0o755)
    paths.desktop_file.write_text(render_desktop_entry(paths.launcher, paths.icon), encoding="utf-8")
    os.chmod(paths.desktop_file, 0o644)
    return paths


def uninstall_desktop(environ: Mapping[str, str] | None = None) -> IntegrationPaths:
    """Remove only launcher artifacts; settings, journal, and records stay intact."""
    paths = integration_paths(environ)
    paths.desktop_file.unlink(missing_ok=True)
    paths.launcher.unlink(missing_ok=True)
    paths.icon.unlink(missing_ok=True)
    for directory in (paths.desktop_file.parent, paths.launcher.parent):
        try:
            directory.rmdir()
        except OSError:
            pass
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Zymptom's user-level Linux desktop integration.")
    commands = parser.add_subparsers(dest="action", required=True)
    install = commands.add_parser("install", help="Install or update the application-menu launcher.")
    install.add_argument("--whisper-command", help="Persist a local whisper.cpp executable path or command name.")
    install.add_argument("--whisper-model", help="Persist a local whisper.cpp model file path.")
    commands.add_parser("uninstall", help="Remove the launcher without deleting settings or journal data.")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            paths = install_desktop(
                whisper_command=args.whisper_command,
                whisper_model=args.whisper_model,
            )
            print(f"Installed Zymptom in the application menu: {paths.desktop_file}")
            if paths.config_file.exists():
                print(f"Local dictation settings: {paths.config_file}")
        else:
            paths = uninstall_desktop()
            print(f"Removed Zymptom desktop integration: {paths.desktop_file}")
            print("Local settings and journal data were preserved.")
    except (OSError, ValueError) as error:
        print(f"Desktop integration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
    raise SystemExit(main())
