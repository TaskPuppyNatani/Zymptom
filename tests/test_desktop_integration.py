import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.desktop_integration import (
    install_desktop,
    render_desktop_entry,
    render_launcher_script,
    uninstall_desktop,
)
from zymptom.dictation.config import read_saved_configuration


def source_checkout(root: Path, *, python_body: str | None = None) -> Path:
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    if python_body is None:
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    else:
        python.write_text("#!/bin/sh\n" + python_body, encoding="utf-8")
    python.chmod(0o700)
    logo = root / "src" / "zymptom" / "ui" / "assets" / "taskpuppy_kreations_logo.png"
    logo.parent.mkdir(parents=True)
    logo.write_bytes(b"test-branding")
    return python


def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / "user home with spaces"
    data_home = home / ".local" / "share"
    config_home = home / ".config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("ZYMPTOM_WHISPER_COMMAND", raising=False)
    monkeypatch.delenv("ZYMPTOM_WHISPER_MODEL", raising=False)
    return home, data_home, config_home


def test_generated_desktop_entry_quotes_space_containing_paths(tmp_path):
    launcher = tmp_path / "user data" / "Zymptom Launcher"
    icon = tmp_path / "user data" / "Zymptom Icon.png"

    entry = render_desktop_entry(launcher, icon)

    assert "Name=Zymptom" in entry
    assert f'Exec="{launcher}"' in entry
    assert f"Icon={icon}" in entry
    assert "Terminal=false" in entry
    validator = shutil.which("desktop-file-validate")
    if validator:
        desktop_file = tmp_path / "zymptom.desktop"
        desktop_file.write_text(entry, encoding="utf-8")
        result = subprocess.run([validator, str(desktop_file)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_committed_launcher_templates_do_not_contain_developer_home_path():
    repository = Path(__file__).resolve().parents[1]
    for path in (
        repository / "scripts" / "desktop_integration.py",
        repository / "scripts" / "install-desktop.sh",
        repository / "scripts" / "uninstall-desktop.sh",
    ):
        assert "/home/natani" not in path.read_text(encoding="utf-8")


def test_launcher_preserves_the_virtualenv_python_symlink(tmp_path):
    root = tmp_path / "source checkout"
    venv_python = root / ".venv" / "bin" / "python"
    system_python = tmp_path / "system python"
    system_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(system_python)

    launcher = render_launcher_script(root, venv_python)

    assert f"PYTHON='{venv_python.absolute()}'" in launcher
    assert f"PYTHON='{system_python}'" not in launcher


def test_launcher_changes_to_checkout_and_uses_venv_from_any_directory(tmp_path, monkeypatch):
    root = tmp_path / "source checkout with spaces"
    capture_cwd = tmp_path / "captured working directory.txt"
    capture_args = tmp_path / "captured command args.txt"
    source_checkout(
        root,
        python_body=(
            'printf "%s" "$PWD" > "$CAPTURE_CWD"\n'
            'printf "%s\\n" "$@" > "$CAPTURE_ARGS"\n'
        ),
    )
    output_home = tmp_path / "user data with spaces"
    paths_env = {
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(output_home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    paths = install_desktop(root, environ=paths_env)
    run_env = os.environ.copy()
    run_env.update(paths_env)
    run_env["CAPTURE_CWD"] = str(capture_cwd)
    run_env["CAPTURE_ARGS"] = str(capture_args)

    result = subprocess.run(
        [str(paths.launcher)], cwd=tmp_path, env=run_env, capture_output=True, text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture_cwd.read_text(encoding="utf-8") == str(root.resolve())
    assert capture_args.read_text(encoding="utf-8").splitlines() == ["-m", "zymptom"]
    assert paths.desktop_file.exists() and paths.icon.read_bytes() == b"test-branding"
    assert paths.launcher.stat().st_mode & 0o777 == 0o755


def test_launcher_surfaces_startup_errors_in_a_desktop_dialog(tmp_path):
    root = tmp_path / "source"
    source_checkout(root, python_body="printf 'local startup failure' >&2\nexit 4\n")
    paths = install_desktop(root, environ={
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    })
    bin_dir = tmp_path / "desktop tools"
    bin_dir.mkdir()
    dialog_capture = tmp_path / "dialog content.txt"
    fake_zenity = bin_dir / "zenity"
    fake_zenity.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in --filename=*) cat \"${1#*=}\" > \"$DIALOG_CAPTURE\" ;; esac\n"
        "  shift\n"
        "done\n",
        encoding="utf-8",
    )
    fake_zenity.chmod(0o700)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["DIALOG_CAPTURE"] = str(dialog_capture)

    result = subprocess.run([str(paths.launcher)], cwd=tmp_path, env=env, capture_output=True)

    assert result.returncode == 4
    assert "local startup failure" in dialog_capture.read_text(encoding="utf-8")


def test_install_detects_environment_and_local_default_whisper_paths(tmp_path, monkeypatch):
    home, _, _ = isolated_environment(tmp_path, monkeypatch)
    root = tmp_path / "source"
    source_checkout(root)
    command = home / "Projects" / "whisper.cpp" / "build" / "bin" / "whisper-cli"
    model = home / "Projects" / "whisper.cpp" / "models" / "ggml-small.en.bin"
    make_executable = command
    make_executable.parent.mkdir(parents=True)
    make_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    make_executable.chmod(0o700)
    model.parent.mkdir(parents=True)
    model.touch()

    paths = install_desktop(root, environ=os.environ)

    assert read_saved_configuration(paths.config_file) == {
        "whisper_command": str(command),
        "whisper_model": str(model),
    }


def test_install_persists_exported_whisper_values(tmp_path, monkeypatch):
    isolated_environment(tmp_path, monkeypatch)
    root = tmp_path / "source"
    source_checkout(root)
    command = tmp_path / "local whisper-cli"
    model = tmp_path / "local model.bin"
    monkeypatch.setenv("ZYMPTOM_WHISPER_COMMAND", str(command))
    monkeypatch.setenv("ZYMPTOM_WHISPER_MODEL", str(model))

    paths = install_desktop(root, environ=os.environ)

    assert read_saved_configuration(paths.config_file) == {
        "whisper_command": str(command),
        "whisper_model": str(model),
    }


def test_uninstall_removes_only_integration_and_preserves_settings_and_journal(
    tmp_path, monkeypatch,
):
    _, data_home, config_home = isolated_environment(tmp_path, monkeypatch)
    root = tmp_path / "source"
    source_checkout(root)
    paths = install_desktop(root, environ=os.environ)
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text('{"whisper_model":"keep"}\n', encoding="utf-8")
    database = data_home / "Zymptom" / "Zymptom" / "zymptom.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"journal data")

    removed = uninstall_desktop(os.environ)

    assert not removed.desktop_file.exists()
    assert not removed.launcher.exists()
    assert not removed.icon.exists()
    assert removed.config_file.read_text(encoding="utf-8") == '{"whisper_model":"keep"}\n'
    assert database.read_bytes() == b"journal data"
    assert config_home.exists()
