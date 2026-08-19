"""Tests for the desktop GUI entrypoint."""

import sys
from types import ModuleType

from rekordbox_compatibility_converter.gui import app


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
