"""Tests for the desktop GUI entrypoint."""

import json
import queue
import sys
import threading
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


class _FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _fake_modern_app(launches, threading_smokes=None):
    module = ModuleType("rekordbox_compatibility_converter.gui.modern_app")

    def fake_main():
        launches.append(True)

    module.main = fake_main
    module.run_threading_smoke_test = lambda: (
        threading_smokes.append(True) if threading_smokes is not None else None
    )
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


def test_gui_conversion_smoke_mode_runs_packaged_self_test(monkeypatch):
    runs = []
    smoke_module = ModuleType("rekordbox_compatibility_converter.packaging_smoke")
    smoke_module.run_frozen_conversion_smoke_test = lambda: runs.append(True)
    monkeypatch.setitem(
        sys.modules,
        "rekordbox_compatibility_converter.packaging_smoke",
        smoke_module,
    )
    monkeypatch.setenv("RBCONVERT_SMOKE_TEST", "conversion")

    app.main()

    assert runs == [True]


def test_gui_threading_smoke_mode_runs_tk_smoke_test(monkeypatch):
    launches = []
    threading_smokes = []
    monkeypatch.setitem(
        sys.modules,
        "rekordbox_compatibility_converter.gui.modern_app",
        _fake_modern_app(launches, threading_smokes),
    )
    monkeypatch.setenv("RBCONVERT_SMOKE_TEST", "gui-threading")

    app.main()

    assert launches == []
    assert threading_smokes == [True]


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


def test_worker_ui_posts_do_not_call_tk_from_worker_thread():
    main_thread = threading.get_ident()
    callback_threads = []
    scheduled_polls = []
    gui = SimpleNamespace(
        _ui_queue=queue.Queue(),
        after=lambda delay, callback: scheduled_polls.append((delay, callback)),
    )
    gui._drain_ui_queue = lambda: ModernRekordboxGUI._drain_ui_queue(gui)

    worker = threading.Thread(
        target=lambda: ModernRekordboxGUI._post_to_ui(
            gui,
            lambda: callback_threads.append(threading.get_ident()),
        )
    )
    worker.start()
    worker.join()

    assert callback_threads == []
    assert scheduled_polls == []

    ModernRekordboxGUI._drain_ui_queue(gui)

    assert callback_threads == [main_thread]
    assert scheduled_polls[0][0] == 25


def test_guidance_preference_is_persisted(tmp_path, monkeypatch):
    preferences = tmp_path / "preferences.json"
    monkeypatch.setattr(modern_app, "PREFERENCES_PATH", preferences)
    gui = SimpleNamespace(
        preferences={},
        show_guidance_var=SimpleNamespace(get=lambda: False),
    )

    ModernRekordboxGUI._save_preferences(gui)

    assert json.loads(preferences.read_text(encoding="utf-8")) == {
        "show_guidance_dialogs": False
    }
    assert modern_app.GUIDANCE_SETTING_LABEL == "Show guidance popups"


def test_help_text_keeps_compatibility_and_onelibrary_workflow_accessible():
    expected_fields = {
        "name",
        "best_for",
        "keeps",
        "pcm_limits",
        "converts",
        "models",
        "note",
    }
    assert len(modern_app.PROFILE_GUIDE) == 3
    assert all(set(profile) == expected_fields for profile in modern_app.PROFILE_GUIDE)
    assert "CDJ-350" in modern_app.COVERED_BASELINE_MODELS
    assert "CDJ-3000X" in modern_app.COVERED_BASELINE_MODELS
    assert "XDJ-AZ" in modern_app.COVERED_BASELINE_MODELS
    assert "OMNIS-DUO" in modern_app.COVERED_BASELINE_MODELS
    assert "XDJ-1000MK2" in modern_app.MODERN_LOSSLESS_MODELS
    assert "XDJ-RX3" not in modern_app.MODERN_LOSSLESS_MODELS
    assert "Convert from Device Library" in modern_app.WORKFLOW_HELP


def test_track_table_reserves_space_for_complete_target_specification():
    columns = {
        column_id: options
        for column_id, _heading, options in modern_app.TRACK_TABLE_COLUMNS
    }

    assert columns["target"]["width"] >= 200
    assert columns["target"]["minwidth"] >= 190
    assert columns["target"]["stretch"] is False
    assert columns["title"]["stretch"] is True
    assert columns["title"]["width"] < 380


def test_about_documents_expose_application_and_third_party_terms(monkeypatch):
    monkeypatch.setattr(modern_app.sys, "frozen", False, raising=False)

    documents = ModernRekordboxGUI._legal_documents()

    assert "Third-Party Notices" in documents
    assert "Application MIT Licence" in documents
    assert "Corresponding Source" in documents
    assert "FFmpeg" in documents["Third-Party Notices"]
    assert "MIT License" in documents["Application MIT Licence"]
    assert "exact, unmodified upstream source" in documents["Corresponding Source"]


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


def test_backup_folder_picker_refreshes_result_without_starting_another_scan(
    tmp_path, monkeypatch
):
    renders = []
    backup_var = _FakeVar()
    gui = SimpleNamespace(
        local_backup_var=backup_var,
        summary=SimpleNamespace(tasks=[SimpleNamespace()]),
        is_scanning=False,
        _render_scan=lambda: renders.append(True),
    )
    monkeypatch.setattr(
        modern_app.filedialog,
        "askdirectory",
        lambda **_kwargs: str(tmp_path),
    )

    selected = ModernRekordboxGUI._browse_local_backup_folder(gui)

    assert selected is True
    assert backup_var.get() == str(tmp_path)
    assert renders == [True]


def test_required_backup_folder_prompt_explains_location_and_recovery(
    tmp_path, monkeypatch
):
    prompts = []
    backup_var = _FakeVar()
    gui = SimpleNamespace(
        local_backup_var=backup_var,
        lbl_status=_FakeWidget(),
    )

    def choose_folder(refresh_summary=True):
        assert refresh_summary is False
        backup_var.set(str(tmp_path))
        return True

    gui._browse_local_backup_folder = choose_folder
    monkeypatch.setattr(
        modern_app.messagebox,
        "showinfo",
        lambda title, message: prompts.append((title, message)),
    )

    selected = ModernRekordboxGUI._ensure_local_backup_folder(
        gui, "Existing converted files must be archived."
    )

    assert selected == tmp_path
    assert prompts[0][0] == "Recovery Folder Required"
    assert "on this computer, not a folder on the USB" in prompts[0][1]
    assert "restore the USB later" in prompts[0][1]


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


def test_backup_phase_shows_file_count_and_latest_file():
    progress = _FakeWidget()
    button = _FakeWidget()
    status = _FakeWidget()
    gui = SimpleNamespace(
        progress_bar=progress,
        btn_convert=button,
        lbl_status=status,
    )

    ModernRekordboxGUI._update_phase(
        gui,
        "backup",
        37 * 1024 * 1024,
        120 * 1024 * 1024,
        "Long Track Name.flac",
    )

    assert progress.config["mode"] == "determinate"
    assert progress.value == 37 / 120
    assert button.config["text"] == "Backing up 31%"
    assert "Backing up and verifying files" in status.config["text"]
    assert "37.0 MiB of 120.0 MiB" in status.config["text"]
    assert "Long Track Name.flac" in status.config["text"]


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
