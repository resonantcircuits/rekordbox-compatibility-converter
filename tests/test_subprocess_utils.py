"""Tests for safe external-process launching."""

from types import SimpleNamespace

from rekordbox_compatibility_converter.core import subprocess_utils


def test_macos_external_process_is_posix_spawn_eligible(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess_utils.sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess_utils.shutil, "which", lambda _name: "/usr/bin/ffprobe"
    )
    monkeypatch.setattr(subprocess_utils.subprocess, "run", fake_run)

    subprocess_utils.run_external(["ffprobe", "-version"], text=True)

    command, kwargs = calls[0]
    assert command[0] == "/usr/bin/ffprobe"
    assert kwargs["close_fds"] is False


def test_non_macos_external_process_keeps_subprocess_defaults(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess_utils.sys, "platform", "linux")
    monkeypatch.setattr(subprocess_utils.subprocess, "run", fake_run)

    subprocess_utils.run_external(["ffprobe", "-version"], text=True)

    command, kwargs = calls[0]
    assert command[0] == "ffprobe"
    assert "close_fds" not in kwargs
