"""Tests for the desktop GUI entrypoint."""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from rekordbox_compatibility_converter.gui import app
from rekordbox_compatibility_converter.gui import modern_app
from rekordbox_compatibility_converter.gui.modern_app import ModernRekordboxGUI
from rekordbox_compatibility_converter.core.models import OriginalCleanupPlan


class _FakeWidget:
    def __init__(self):
        self.config = {}
        self.value = None
        self.started = False

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def set(self, value):
        self.value = value

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


def _fake_modern_app(launches):
    module = ModuleType("rekordbox_compatibility_converter.gui.modern_app")

    def fake_main():
        launches.append(True)

    module.main = fake_main
    return module


def test_gui_smoke_mode_imports_without_launching(monkeypatch):
    launches = []
    monkeypatch.setitem(
        sys.modules,
        "rekordbox_compatibility_converter.gui.modern_app",
        _fake_modern_app(launches),
    )
    monkeypatch.setenv("RBCONVERT_SMOKE_TEST", "1")

    app.main()

    assert launches == []


def test_gui_entrypoint_launches_normally(monkeypatch):
    launches = []
    monkeypatch.setitem(
        sys.modules,
        "rekordbox_compatibility_converter.gui.modern_app",
        _fake_modern_app(launches),
    )
    monkeypatch.delenv("RBCONVERT_SMOKE_TEST", raising=False)

    app.main()

    assert launches == [True]


def test_conversion_start_is_immediately_visible():
    progress = _FakeWidget()
    button = _FakeWidget()
    status = _FakeWidget()
    idle_updates = []
    gui = SimpleNamespace(
        progress_bar=progress,
        btn_convert=button,
        lbl_status=status,
        update_idletasks=lambda: idle_updates.append(True),
    )

    ModernRekordboxGUI._show_conversion_started(gui, 45)

    assert button.config == {"state": "disabled", "text": "Converting 0 of 45..."}
    assert progress.config["mode"] == "indeterminate"
    assert progress.started is True
    assert "Conversion started" in status.config["text"]
    assert "do not eject the USB" in status.config["text"]
    assert idle_updates == [True]


def test_conversion_progress_shows_track_count_and_latest_file():
    progress = _FakeWidget()
    button = _FakeWidget()
    status = _FakeWidget()
    gui = SimpleNamespace(
        progress_bar=progress,
        btn_convert=button,
        lbl_status=status,
    )

    ModernRekordboxGUI._update_prog(gui, 0.25, "Example.flac", 2, 8)

    assert progress.config["mode"] == "determinate"
    assert progress.value == 0.25
    assert button.config["text"] == "Converting 2 of 8..."
    assert "2 of 8 tracks processed" in status.config["text"]
    assert "Example.flac" in status.config["text"]


def test_cleanup_confirmation_requires_rekordbox_verification(monkeypatch):
    progress = _FakeWidget()
    status = _FakeWidget()
    busy_states = []
    prompt = {}
    gui = SimpleNamespace(
        progress_bar=progress,
        lbl_status=status,
        _set_cleanup_busy=lambda value: busy_states.append(value),
    )
    plan = OriginalCleanupPlan(
        usb_root=Path("/Volumes/USB"),
        candidates=[SimpleNamespace()],
        total_bytes=2 * 1024 ** 3,
        warnings=["OneLibrary contents require visual verification."],
    )

    def decline(title, message, default):
        prompt.update(title=title, message=message, default=default)
        return False

    monkeypatch.setattr(modern_app.messagebox, "askyesno", decline)

    ModernRekordboxGUI._confirm_original_cleanup(gui, plan)

    assert "OneLibrary > Convert from Device Library" in prompt["message"]
    assert "verified the converted tracks" in prompt["message"]
    assert "cannot be undone" in prompt["message"]
    assert "2.00 GiB" in prompt["message"]
    assert busy_states == [False]
    assert "canceled" in status.config["text"]
