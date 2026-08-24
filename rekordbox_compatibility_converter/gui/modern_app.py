"""Modern, sleek Dark/Light mode GUI using CustomTkinter with dynamic theme styling."""

import json
import os
import queue
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Dict, Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..core.dlp_manager import (
    ONELIBRARY_BRIDGE_CONFIRM_MESSAGE,
    ONELIBRARY_BRIDGE_PROMPT,
    ONELIBRARY_REBUILD_REQUIRED_MESSAGE,
)
from ..core.engine import DEFAULT_CONVERSION_THREADS, ConversionEngine
from ..core.models import (
    CompatibilityProfileType,
    OriginalCleanupPlan,
    ScanSummary,
    TargetFormat,
)
from ..core.usb_detector import USBDetector
from ..core.profiles import get_profile

try:
    APP_VERSION = version("rekordbox-compatibility-converter")
except PackageNotFoundError:
    APP_VERSION = "development"

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PROFILE_DESCRIPTIONS = {
    "standard": "Standard Club — MP3, AAC, WAV and AIFF through 48 kHz. Converts FLAC/ALAC for broad Rekordbox-player coverage.",
    "maximum": "Conservative 16-bit — normalizes to a deliberately strict 44.1 kHz AIFF/WAV/MP3 set and avoids AAC.",
    "modern": "Modern Lossless — allows both FLAC and ALAC through 48 kHz for the explicitly listed models in the ? reference.",
}
PROFILE_VALUES = {
    "Standard Club": "standard",
    "Conservative 16-bit": "maximum",
    "Modern Lossless": "modern",
}

COVERED_BASELINE_MODELS = (
    "CDJ-350",
    "CDJ-850",
    "CDJ-900",
    "CDJ-900NXS",
    "CDJ-2000",
    "CDJ-2000NXS",
    "CDJ-2000NXS2",
    "CDJ-TOUR1",
    "CDJ-3000",
    "CDJ-3000X",
    "XDJ-AERO",
    "XDJ-R1",
    "XDJ-RR",
    "XDJ-RX",
    "XDJ-RX2",
    "XDJ-RX3",
    "XDJ-RZ",
    "XDJ-XZ",
    "XDJ-700",
    "XDJ-1000",
    "XDJ-1000MK2",
    "XDJ-AN",
    "XDJ-AZ",
    "OPUS-QUAD",
    "OMNIS-DUO",
)

MODERN_LOSSLESS_MODELS = (
    "CDJ-2000NXS2",
    "CDJ-TOUR1",
    "CDJ-3000",
    "CDJ-3000X",
    "XDJ-1000MK2",
    "XDJ-AN",
    "XDJ-AZ",
    "OPUS-QUAD",
    "OMNIS-DUO",
)

PROFILE_GUIDE = (
    {
        "name": "Standard Club",
        "best_for": "Unknown venue hardware and broad cross-generation playback.",
        "keeps": "MP3, AAC, WAV and AIFF",
        "pcm_limits": "16/24-bit at 44.1 or 48 kHz",
        "converts": "FLAC, ALAC and unsupported sample rates to the selected format.",
        "models": COVERED_BASELINE_MODELS,
        "note": "Recommended default. Newer players also support this conservative common set.",
    },
    {
        "name": "Conservative 16-bit",
        "best_for": "A deliberately uniform fallback library.",
        "keeps": "MP3, WAV and AIFF",
        "pcm_limits": "16-bit at 44.1 kHz",
        "converts": "AAC, FLAC, ALAC, 24-bit lossless audio and other sample rates.",
        "models": COVERED_BASELINE_MODELS,
        "note": "Stricter than the published limits of many listed players; choose it for consistency, not because all older players require it.",
    },
    {
        "name": "Modern Lossless",
        "best_for": "Players explicitly supporting both FLAC and ALAC.",
        "keeps": "MP3, AAC, WAV, AIFF, FLAC and ALAC",
        "pcm_limits": "16/24-bit at 44.1 or 48 kHz",
        "converts": "Unsupported formats and audio above the shared 48 kHz ceiling.",
        "models": MODERN_LOSSLESS_MODELS,
        "note": "XDJ-XZ and XDJ-RX3 are omitted because their published USB format sets include FLAC but not ALAC; use Standard Club for a shared guarantee.",
    },
)

WORKFLOW_HELP = """ONELIBRARY TWO-STEP WORKFLOW
1. Scan the Rekordbox USB and choose a local recovery folder on the computer.
2. Convert the planned tracks. Originals and Rekordbox metadata are verified locally before USB removal.
3. Do not use the USB on players yet: OneLibrary still contains the old paths.
4. Open the USB in Rekordbox. Under Devices, right-click OneLibrary (Device Library Plus in Rekordbox 6) and choose Convert from Device Library.
5. Inspect playlists and tracks, then test the USB on the intended player.
6. Keep the local recovery folder until testing is complete.

The Restore Backup button can restore a local recovery session if the USB has not changed independently."""

PREFERENCES_PATH = Path.home() / ".rekordbox-format-checker.json"
GUIDANCE_SETTING_LABEL = "Show guidance popups"

# (light, dark) color pairs, WCAG-checked in both modes
MUTED_TEXT = ("gray35", "gray65")
CARD_BORDER = ("gray80", "gray25")


class ModernRekordboxGUI(ctk.CTk):
    """Modern DJ-styled cross-platform GUI for Rekordbox format conversion."""

    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Rekordbox Compatibility Converter")
        self.geometry("1020x800")
        self.minsize(860, 640)

        self.engine = ConversionEngine()
        self.summary: Optional[ScanSummary] = None
        self.is_converting = False
        self.is_cleanup_running = False
        self.cleanup_previous_convert_state = "disabled"
        self.is_scanning = False
        self.scan_generation = 0
        self.drive_map: Dict[str, Path] = {}
        self.preferences = self._load_preferences()
        self.show_guidance_var = tk.BooleanVar(
            value=bool(self.preferences.get("show_guidance_dialogs", True))
        )

        self._build_menu()
        self._build_ui()
        self._apply_treeview_theme("dark")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._ui_queue = queue.Queue()
        self._ui_poll_id = self.after(25, self._drain_ui_queue)
        self._refresh_drives()

    def _post_to_ui(self, callback: Callable, *args) -> None:
        """Queue work for the Tk-owning thread without calling Tcl here."""
        self._ui_queue.put((callback, args))

    def _drain_ui_queue(self) -> None:
        """Run queued worker results from the Tk event-loop thread."""
        try:
            while True:
                callback, args = self._ui_queue.get_nowait()
                callback(*args)
        except queue.Empty:
            pass
        finally:
            self._ui_poll_id = self.after(25, self._drain_ui_queue)

    @staticmethod
    def _load_preferences() -> Dict[str, bool]:
        try:
            loaded = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_preferences(self) -> None:
        self.preferences["show_guidance_dialogs"] = bool(
            self.show_guidance_var.get()
        )
        try:
            PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp = PREFERENCES_PATH.with_name(
                f".{PREFERENCES_PATH.name}.{os.getpid()}.tmp"
            )
            temp.write_text(
                json.dumps(self.preferences, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temp, PREFERENCES_PATH)
        except OSError as exc:
            messagebox.showerror("Could Not Save Preference", str(exc))

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        settings_menu = tk.Menu(menu, tearoff=False)
        settings_menu.add_checkbutton(
            label=GUIDANCE_SETTING_LABEL,
            variable=self.show_guidance_var,
            command=self._save_preferences,
        )
        menu.add_cascade(label="Settings", menu=settings_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(
            label="Compatibility profiles...", command=self._show_profile_help
        )
        help_menu.add_command(
            label="OneLibrary workflow...", command=self._show_workflow_help
        )
        help_menu.add_separator()
        help_menu.add_command(
            label="About & Open Source Licences...", command=self._show_about
        )
        menu.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menu)

    @staticmethod
    def _legal_documents() -> Dict[str, str]:
        if getattr(sys, "frozen", False):
            root = Path(getattr(sys, "_MEIPASS", ""))
            paths = {
                "Third-Party Notices": root / "THIRD_PARTY_NOTICES.txt",
                "Application MIT Licence": root / "licenses" / "rekordbox" / "LICENSE",
                "FFmpeg LGPL Licence": root / "licenses" / "ffmpeg" / "COPYING.LGPLv2.1",
                "LAME LGPL Licence": root / "licenses" / "lame" / "COPYING",
                "Python Runtime Licence": root / "licenses" / "python" / "LICENSE.txt",
                "Tcl Runtime Licence": (
                    root / "licenses" / "tcl-tk" / "tcl" / "license.terms"
                ),
                "Tk Runtime Licence": (
                    root / "licenses" / "tcl-tk" / "tk" / "license.terms"
                ),
                "CustomTkinter Licence": (
                    root / "licenses" / "python-packages" / "customtkinter" / "LICENSE"
                ),
                "darkdetect Licence": (
                    root / "licenses" / "python-packages" / "darkdetect" / "LICENSE"
                ),
                "packaging Licences": (
                    root / "licenses" / "python-packages" / "packaging" / "LICENSE"
                ),
                "FFmpeg Build Information": root / "licenses" / "ffmpeg" / "FFMPEG_BUILD_INFO.txt",
                "Corresponding Source": root / "SOURCE_OFFER.txt",
            }
        else:
            paths = {
                "Third-Party Notices": PROJECT_ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt",
                "Application MIT Licence": PROJECT_ROOT / "LICENSE",
                "Tcl Runtime Licence": (
                    PROJECT_ROOT / "packaging" / "licenses" / "tcl" / "license.terms"
                ),
                "Tk Runtime Licence": (
                    PROJECT_ROOT / "packaging" / "licenses" / "tk" / "license.terms"
                ),
                "Corresponding Source": (
                    PROJECT_ROOT
                    / "packaging"
                    / "ffmpeg"
                    / "CORRESPONDING_SOURCE_README.md"
                ),
            }

        documents = {}
        for label, path in paths.items():
            try:
                documents[label] = path.read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                )
            except OSError:
                documents[label] = f"This document is missing from the application:\n{path}"
        return documents

    def _show_about(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("About & Open Source Licences")
        window.geometry("880x680")
        window.minsize(700, 520)
        window.transient(self)

        ctk.CTkLabel(
            window,
            text=f"Rekordbox Format Checker {APP_VERSION}",
            font=ctk.CTkFont(size=20, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=22, pady=(20, 3))
        ctk.CTkLabel(
            window,
            text=(
                "Copyright 2026 Resonant Circuits. The application is MIT-licensed. "
                "Packaged releases bundle the separate FFmpeg and ffprobe tools under "
                "LGPL 2.1 or later and LAME under LGPL 2.0 or later. The projects are "
                "not affiliated with or endorsed by Resonant Circuits. Additional "
                "runtime and GUI dependency terms are listed below."
            ),
            font=ctk.CTkFont(size=12),
            text_color=MUTED_TEXT,
            anchor="w",
            justify="left",
            wraplength=830,
        ).pack(fill="x", padx=22, pady=(0, 14))

        documents = self._legal_documents()
        selector = ctk.CTkOptionMenu(window, values=list(documents))
        selector.pack(fill="x", padx=22, pady=(0, 10))
        text_box = ctk.CTkTextbox(window, wrap="word", font=("Courier", 11))
        text_box.pack(fill="both", expand=True, padx=22, pady=(0, 14))

        def show_document(label: str) -> None:
            text_box.configure(state="normal")
            text_box.delete("1.0", "end")
            text_box.insert("1.0", documents[label])
            text_box.configure(state="disabled")

        selector.configure(command=show_document)
        first_document = next(iter(documents))
        selector.set(first_document)
        show_document(first_document)

        ctk.CTkButton(window, text="Close", command=window.destroy, width=100).pack(
            pady=(0, 18)
        )

    def _show_profile_help(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("Compatibility Profiles and Supported Models")
        window.geometry("900x700")
        window.minsize(720, 520)
        window.transient(self)

        ctk.CTkLabel(
            window,
            text="Compatibility Profiles",
            font=ctk.CTkFont(size=20, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=22, pady=(20, 2))
        ctk.CTkLabel(
            window,
            text=(
                "Each list names the Rekordbox USB players intentionally covered by this app. "
                "Exact firmware and library-format support can still differ."
            ),
            font=ctk.CTkFont(size=12),
            text_color=MUTED_TEXT,
            anchor="w",
            justify="left",
            wraplength=840,
        ).pack(fill="x", padx=22, pady=(0, 12))

        scroll = ctk.CTkScrollableFrame(window, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        fields = (
            ("Best for", "best_for"),
            ("Keeps", "keeps"),
            ("PCM limits", "pcm_limits"),
            ("Converts", "converts"),
            ("Supported models", "models"),
            ("Important", "note"),
        )
        for profile in PROFILE_GUIDE:
            card = ctk.CTkFrame(
                scroll, corner_radius=10, border_width=1, border_color=CARD_BORDER
            )
            card.pack(fill="x", padx=2, pady=(0, 12))
            ctk.CTkLabel(
                card,
                text=profile["name"],
                font=ctk.CTkFont(size=15, weight="bold"),
                anchor="w",
            ).pack(fill="x", padx=16, pady=(13, 7))
            for label, key in fields:
                row = ctk.CTkFrame(card, fg_color="transparent")
                row.pack(fill="x", padx=16, pady=2)
                ctk.CTkLabel(
                    row,
                    text=f"{label}:",
                    width=125,
                    font=ctk.CTkFont(size=12, weight="bold"),
                    anchor="nw",
                ).pack(side="left")
                value = profile[key]
                if key == "models":
                    value = ", ".join(value)
                ctk.CTkLabel(
                    row,
                    text=value,
                    font=ctk.CTkFont(size=12),
                    anchor="nw",
                    justify="left",
                    wraplength=675,
                ).pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(card, text="").pack(pady=1)

        policy_card = ctk.CTkFrame(
            scroll, corner_radius=10, border_width=1, border_color=CARD_BORDER
        )
        policy_card.pack(fill="x", padx=2, pady=(0, 12))
        ctk.CTkLabel(
            policy_card,
            text="Independent option: Enforce 16-bit",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(13, 5))
        ctk.CTkLabel(
            policy_card,
            text=(
                "Applies on top of any profile. It converts otherwise-compatible 24-bit "
                "WAV, AIFF, FLAC and ALAC to dithered 16-bit output. MP3 and AAC are not "
                "reconverted merely because this option is enabled."
            ),
            font=ctk.CTkFont(size=12),
            anchor="w",
            justify="left",
            wraplength=800,
        ).pack(fill="x", padx=16, pady=(0, 14))

        ctk.CTkButton(window, text="Close", width=100, command=window.destroy).pack(
            pady=(0, 16)
        )
        window.grab_set()

    def _show_workflow_help(self) -> None:
        messagebox.showinfo("OneLibrary Workflow", WORKFLOW_HELP)

    # ------------------------------------------------------------------ theming

    def _apply_treeview_theme(self, mode: str):
        """Applies seamless Dark/Light styling to the ttk.Treeview table."""
        style = ttk.Style()
        style.theme_use("clam")

        if mode == "dark":
            bg_color = "#1B1B1D"
            stripe_color = "#232326"
            fg_color = "#F2F2F7"
            hdr_bg = "#2C2C2E"
            hdr_fg = "#FFFFFF"
            selected_bg = "#0057A8"
        else:
            bg_color = "#FFFFFF"
            stripe_color = "#F4F4F6"
            fg_color = "#1C1C1E"
            hdr_bg = "#E8E8ED"
            hdr_fg = "#1C1C1E"
            selected_bg = "#0057A8"

        style.configure(
            "Treeview",
            background=bg_color,
            foreground=fg_color,
            fieldbackground=bg_color,
            rowheight=30,
            font=("Helvetica", 12),
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=hdr_bg,
            foreground=hdr_fg,
            relief="flat",
            font=("Helvetica", 11, "bold"),
            padding=(8, 6),
        )
        style.map(
            "Treeview",
            background=[("selected", selected_bg)],
            foreground=[("selected", "#FFFFFF")],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", hdr_bg)],
        )

        if hasattr(self, "tree"):
            self.tree.tag_configure("even", background=bg_color, foreground=fg_color)
            self.tree.tag_configure("odd", background=stripe_color, foreground=fg_color)
        if hasattr(self, "tree_container"):
            self.tree_container.configure(bg=bg_color)

    def _change_theme(self, mode: str):
        ctk.set_appearance_mode(mode.lower())
        current = ctk.get_appearance_mode().lower()
        self._apply_treeview_theme(current)

    # ------------------------------------------------------------------ layout

    def _section_label(self, parent, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text=text.upper(),
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=MUTED_TEXT,
            anchor="w",
        )

    def _field_label(self, parent, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        )

    def _build_ui(self):
        # 1. Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(side="top", fill="x", padx=24, pady=(20, 10))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")
        ctk.CTkLabel(
            title_box,
            text="Rekordbox Compatibility Converter",
            font=ctk.CTkFont(size=21, weight="bold"),
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box,
            text="Check a Rekordbox USB, convert unsupported audio, and keep its library paths in sync",
            font=ctk.CTkFont(size=12),
            text_color=MUTED_TEXT,
            anchor="w",
        ).pack(anchor="w", pady=(1, 0))

        theme_box = ctk.CTkFrame(header, fg_color="transparent")
        theme_box.pack(side="right")
        ctk.CTkLabel(theme_box, text="Appearance", font=ctk.CTkFont(size=11), text_color=MUTED_TEXT).pack(anchor="e")
        theme_switch = ctk.CTkOptionMenu(
            theme_box,
            values=["Dark", "Light", "System"],
            command=self._change_theme,
            width=110,
            height=26,
        )
        theme_switch.set("Dark")
        theme_switch.pack(anchor="e", pady=(2, 0))
        self.guidance_switch = ctk.CTkSwitch(
            theme_box,
            text=GUIDANCE_SETTING_LABEL,
            variable=self.show_guidance_var,
            command=self._save_preferences,
            font=ctk.CTkFont(size=11),
        )
        self.guidance_switch.pack(anchor="e", pady=(8, 0))

        # 2. Bottom Progress & Status Bar (packed first on bottom so it is never clipped)
        bottom_frame = ctk.CTkFrame(self, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        bottom_frame.pack(side="bottom", fill="x", padx=24, pady=(8, 20))

        self.progress_bar = ctk.CTkProgressBar(bottom_frame, height=10)
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", padx=16, pady=(14, 8))

        self.lbl_status = ctk.CTkLabel(
            bottom_frame,
            text="Ready. Select a drive and click 'Scan USB Drive'.",
            font=ctk.CTkFont(size=12),
            text_color=MUTED_TEXT,
            anchor="w",
        )
        self.lbl_status.pack(fill="x", padx=16, pady=(0, 12))

        # 3. Config Card
        config_card = ctk.CTkFrame(self, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        config_card.pack(side="top", fill="x", padx=24, pady=8)

        grid = ctk.CTkFrame(config_card, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(14, 16))
        grid.grid_columnconfigure(0, weight=1)

        # Drive row
        self._field_label(grid, "Rekordbox USB Drive").grid(row=0, column=0, columnspan=3, sticky="w")

        self.drive_menu = ctk.CTkOptionMenu(grid, values=["Scanning..."], command=self._on_drive_selected, height=32)
        self.drive_menu.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.btn_browse = ctk.CTkButton(grid, text="Browse...", width=100, height=32, command=self._browse_folder)
        self.btn_browse.grid(row=1, column=1, padx=(10, 0), pady=(4, 0))
        self.btn_refresh = ctk.CTkButton(
            grid,
            text="Refresh",
            width=90,
            height=32,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            command=self._refresh_drives,
        )
        self.btn_refresh.grid(row=1, column=2, padx=(8, 0), pady=(4, 0))

        # Settings row: three labeled groups on one aligned grid
        settings = ctk.CTkFrame(config_card, fg_color="transparent")
        settings.pack(fill="x", padx=18, pady=(0, 4))

        profile_heading = ctk.CTkFrame(settings, fg_color="transparent")
        profile_heading.grid(row=0, column=0, sticky="w")
        self._field_label(profile_heading, "Compatibility Profile").pack(side="left")
        self.btn_profile_info = ctk.CTkButton(
            profile_heading,
            text="?",
            width=24,
            height=22,
            corner_radius=11,
            command=self._show_profile_help,
        )
        self.btn_profile_info.pack(side="left", padx=(6, 0))
        self._field_label(settings, "Conversion Format").grid(row=0, column=1, sticky="w", padx=(20, 0))
        self._field_label(settings, "Lossless Bit Depth").grid(row=0, column=2, sticky="w", padx=(20, 0))
        self._field_label(settings, "Conversion Workers").grid(row=0, column=3, sticky="w", padx=(20, 0))

        self.profile_var = ctk.StringVar(value="Standard Club")
        self.profile_menu = ctk.CTkOptionMenu(
            settings,
            values=list(PROFILE_VALUES),
            variable=self.profile_var,
            width=145,
            height=30,
            command=self._on_profile_changed,
        )
        self.profile_menu.grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.format_var = ctk.StringVar(value="AIFF")
        self.format_menu = ctk.CTkOptionMenu(
            settings,
            values=["AIFF", "WAV", "MP3"],
            variable=self.format_var,
            width=100,
            height=30,
            command=self._on_format_changed,
        )
        self.format_menu.grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(4, 0))

        self.bit_depth_var = ctk.StringVar(value="Profile default")
        self.bit_depth_menu = ctk.CTkOptionMenu(
            settings,
            values=["Profile default", "Enforce 16-bit"],
            variable=self.bit_depth_var,
            width=140,
            height=30,
            command=self._on_bit_depth_changed,
        )
        self.bit_depth_menu.grid(row=1, column=2, sticky="w", padx=(20, 0), pady=(4, 0))

        threads_box = ctk.CTkFrame(settings, fg_color="transparent")
        threads_box.grid(row=1, column=3, sticky="w", padx=(20, 0), pady=(4, 0))
        self.threads_slider = ctk.CTkSlider(threads_box, from_=1, to=16, number_of_steps=15, width=120)
        self.threads_slider.set(DEFAULT_CONVERSION_THREADS)
        self.threads_slider.pack(side="left")
        self.threads_lbl = ctk.CTkLabel(
            threads_box,
            text=f"{int(self.threads_slider.get())}",
            font=ctk.CTkFont(size=12, weight="bold"),
            width=24,
        )
        self.threads_lbl.pack(side="left", padx=(8, 0))
        self.threads_slider.configure(command=lambda val: self.threads_lbl.configure(text=f"{int(val)}"))

        # Profile description
        self.lbl_profile_desc = ctk.CTkLabel(
            config_card,
            text=PROFILE_DESCRIPTIONS["standard"],
            font=ctk.CTkFont(size=11),
            text_color=MUTED_TEXT,
            anchor="w",
            justify="left",
        )
        self.lbl_profile_desc.pack(fill="x", padx=18, pady=(6, 10))

        archive_row = ctk.CTkFrame(config_card, fg_color="transparent")
        archive_row.pack(fill="x", padx=18, pady=(0, 10))
        self.local_backup_var = ctk.StringVar(value="")
        self.local_backup_entry = ctk.CTkEntry(
            archive_row,
            textvariable=self.local_backup_var,
            placeholder_text="Recovery folder on this computer (required for the space-saving OneLibrary workflow)",
            height=30,
        )
        self.local_backup_entry.pack(side="left", fill="x", expand=True)
        self.btn_backup_browse = ctk.CTkButton(
            archive_row,
            text="Choose Backup Folder...",
            width=155,
            height=30,
            command=self._browse_local_backup_folder,
        )
        self.btn_backup_browse.pack(side="left", padx=(10, 0))

        # Divider
        ctk.CTkFrame(config_card, height=1, fg_color=CARD_BORDER).pack(fill="x", padx=18)

        # Switches row
        switches_row = ctk.CTkFrame(config_card, fg_color="transparent")
        switches_row.pack(fill="x", padx=18, pady=(10, 14))

        self.del_switch = ctk.CTkSwitch(switches_row, text="Remove originals after safe commit", font=ctk.CTkFont(size=12))
        self.del_switch.select()
        self.del_switch.pack(side="left", padx=(0, 22))

        self.dotfiles_switch = ctk.CTkSwitch(switches_row, text="Clean macOS ghost files (._*, .DS_Store)", font=ctk.CTkFont(size=12))
        self.dotfiles_switch.select()
        self.dotfiles_switch.pack(side="left", padx=(0, 22))

        self.backup_switch = ctk.CTkSwitch(switches_row, text="Keep USB metadata recovery copies", font=ctk.CTkFont(size=12))
        self.backup_switch.select()
        self.backup_switch.pack(side="left")

        # 4. Stats Cards
        stats_frame = ctk.CTkFrame(self, fg_color="transparent")
        stats_frame.pack(side="top", fill="x", padx=24, pady=6)

        self.card_total = self._create_stat_card(
            stats_frame,
            title="Total Tracks",
            fg_color=("#EDEDF0", "#232326"),
            text_color=("#1C1C1E", "#FFFFFF"),
        )
        self.card_total.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.card_compat = self._create_stat_card(
            stats_frame,
            title="Compatible",
            fg_color=("#DEF2E4", "#17301F"),
            text_color=("#136A2E", "#4ADE80"),
        )
        self.card_compat.pack(side="left", fill="x", expand=True, padx=5)

        self.card_incompat = self._create_stat_card(
            stats_frame,
            title="Needs Conversion",
            fg_color=("#FBE3E4", "#331B1B"),
            text_color=("#A61B29", "#FF6B60"),
        )
        self.card_incompat.pack(side="left", fill="x", expand=True, padx=(10, 0))

        # 5. Action Buttons
        action_bar = ctk.CTkFrame(self, fg_color="transparent")
        action_bar.pack(side="top", fill="x", padx=24, pady=(8, 6))

        self.btn_scan = ctk.CTkButton(
            action_bar,
            text="Scan USB Drive",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=("#0067C5", "#0062B8"),
            hover_color=("#0062CC", "#0057A8"),
            text_color="#FFFFFF",
            height=40,
            corner_radius=8,
            command=self._start_scan,
        )
        self.btn_scan.pack(side="left", padx=(0, 10))

        self.btn_convert = ctk.CTkButton(
            action_bar,
            text="Convert Planned Tracks",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=("#187434", "#176B31"),
            hover_color=("#218838", "#238636"),
            text_color="#FFFFFF",
            text_color_disabled=("#8C8C91", "#76767A"),
            height=40,
            corner_radius=8,
            state="disabled",
            command=self._start_conversion,
        )
        self.btn_convert.pack(side="left", padx=(0, 10))

        self.btn_restore = ctk.CTkButton(
            action_bar,
            text="Restore Backup",
            font=ctk.CTkFont(size=13),
            fg_color="transparent",
            border_width=1,
            border_color=("gray60", "gray40"),
            hover_color=("gray85", "#3A3A3C"),
            text_color=("#1C1C1E", "#FFFFFF"),
            height=40,
            corner_radius=8,
            command=self._restore_backup,
        )
        self.btn_restore.pack(side="right")

        self.btn_cleanup = ctk.CTkButton(
            action_bar,
            text="Remove Retained Originals",
            font=ctk.CTkFont(size=13),
            fg_color="transparent",
            border_width=1,
            border_color=("#9A6700", "#B78103"),
            hover_color=("#F3E8C8", "#3B2F12"),
            text_color=("#6B4700", "#F0C36A"),
            height=40,
            corner_radius=8,
            command=self._start_original_cleanup,
        )
        self.btn_cleanup.pack(side="right", padx=(0, 10))

        # 6. Track Table (takes all remaining vertical space)
        table_card = ctk.CTkFrame(self, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        table_card.pack(side="top", fill="both", expand=True, padx=24, pady=(4, 0))

        table_header = ctk.CTkFrame(table_card, fg_color="transparent")
        table_header.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(
            table_header,
            text="Planned Conversions",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")
        self.lbl_track_count = ctk.CTkLabel(
            table_header,
            text="",
            font=ctk.CTkFont(size=12),
            text_color=MUTED_TEXT,
        )
        self.lbl_track_count.pack(side="right")

        self.tree_container = tk.Frame(table_card, bg="#1B1B1D")
        self.tree_container.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        columns = ("id", "title", "format", "specs", "target")
        self.tree = ttk.Treeview(self.tree_container, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("id", text="ID")
        self.tree.heading("title", text="Track Title")
        self.tree.heading("format", text="Format")
        self.tree.heading("specs", text="Current Spec")
        self.tree.heading("target", text="Converts To")

        self.tree.column("id", width=60, anchor="center", stretch=False)
        self.tree.column("title", width=380)
        self.tree.column("format", width=90, anchor="center", stretch=False)
        self.tree.column("specs", width=150, anchor="center", stretch=False)
        self.tree.column("target", width=120, anchor="center", stretch=False)

        tree_scroll = ttk.Scrollbar(self.tree_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

    def _create_stat_card(self, parent, title: str, fg_color: tuple, text_color: tuple):
        card = ctk.CTkFrame(parent, corner_radius=10, fg_color=fg_color)
        lbl_val = ctk.CTkLabel(card, text="--", font=ctk.CTkFont(size=28, weight="bold"), text_color=text_color)
        lbl_val.pack(pady=(14, 0))
        lbl_title = ctk.CTkLabel(card, text=title.upper(), font=ctk.CTkFont(size=11, weight="bold"), text_color=text_color)
        lbl_title.pack(pady=(0, 12))
        card.val_label = lbl_val
        return card

    # ------------------------------------------------------------------ handlers

    def _on_profile_changed(self, choice: str):
        self.lbl_profile_desc.configure(
            text=PROFILE_DESCRIPTIONS.get(PROFILE_VALUES.get(choice, choice), "")
        )
        if (
            not self.is_converting
            and not self.is_cleanup_running
            and (self.summary or self.is_scanning)
        ):
            self._start_scan()

    def _on_format_changed(self, _choice: str):
        if (
            not self.is_converting
            and not self.is_cleanup_running
            and (self.summary or self.is_scanning)
        ):
            self._start_scan()

    def _on_bit_depth_changed(self, _choice: str):
        if (
            not self.is_converting
            and not self.is_cleanup_running
            and (self.summary or self.is_scanning)
        ):
            self._start_scan(
                allow_onelibrary_bridge=bool(
                    self.summary and self.summary.onelibrary_bridge_mode
                )
            )

    def _set_conversion_controls(self, enabled: bool):
        """Prevents drive or conversion settings from changing during a commit."""
        state = "normal" if enabled else "disabled"
        for widget in (
            self.drive_menu,
            self.btn_browse,
            self.btn_refresh,
            self.profile_menu,
            self.format_menu,
            self.bit_depth_menu,
            self.threads_slider,
            self.del_switch,
            self.dotfiles_switch,
            self.backup_switch,
            self.local_backup_entry,
            self.btn_backup_browse,
        ):
            widget.configure(state=state)
        if enabled and self.summary and self.summary.onelibrary_bridge_mode:
            self.del_switch.select()
            self.del_switch.configure(state="disabled")
            self.backup_switch.select()
            self.backup_switch.configure(state="disabled")

    def _on_close(self):
        if self.is_converting or self.is_cleanup_running:
            messagebox.showwarning(
                "USB Operation in Progress",
                "A USB operation is still running. Keep this window open and do not eject the "
                "USB until the completion message appears.",
            )
            return
        self.after_cancel(self._ui_poll_id)
        self.destroy()

    def _refresh_drives(self):
        detected = USBDetector.list_rekordbox_drives()
        self.drive_map = {}
        values = []
        for path, label in detected:
            display = f"{label}  ({path})"
            self.drive_map[display] = path
            values.append(display)

        if values:
            self.drive_menu.configure(values=values)
            self.drive_menu.set(values[0])
            self._on_drive_selected(values[0])
        else:
            self.drive_menu.configure(values=["No Rekordbox USB detected"])
            self.drive_menu.set("No Rekordbox USB detected")
            self._on_drive_selected("No Rekordbox USB detected")

    def _browse_folder(self):
        path = filedialog.askdirectory(title="Select Rekordbox USB Export Directory")
        if path:
            p = Path(path)
            display = f"Custom: {p.name}  ({p})"
            self.drive_map[display] = p
            vals = [v for v in self.drive_menu.cget("values") if v != display]
            vals.append(display)
            self.drive_menu.configure(values=vals)
            self.drive_menu.set(display)
            self._on_drive_selected(display)

    def _browse_local_backup_folder(self):
        path = filedialog.askdirectory(title="Select Local Original Backup Folder")
        if path:
            self.local_backup_var.set(path)
            if self.summary and self.summary.tasks and not self.is_scanning:
                self._start_scan(
                    allow_onelibrary_bridge=self.summary.onelibrary_bridge_mode
                )

    def _on_drive_selected(self, choice: str):
        self.scan_generation += 1
        self.is_scanning = False
        self.summary = None
        self.btn_scan.configure(state="normal")
        self.btn_cleanup.configure(state="normal")
        self.btn_convert.configure(
            state="disabled",
            text="Convert Planned Tracks",
        )
        self.card_total.val_label.configure(text="--")
        self.card_compat.val_label.configure(text="--")
        self.card_incompat.val_label.configure(text="--")
        self.tree.delete(*self.tree.get_children())
        self.lbl_track_count.configure(text="")
        self.lbl_status.configure(text="Ready to scan.")
        self.del_switch.configure(state="normal")
        self.backup_switch.configure(state="normal")

    def _get_selected_path(self) -> Path:
        selected = self.drive_menu.get()
        if selected in self.drive_map:
            return self.drive_map[selected]
        return Path(selected)

    # ------------------------------------------------------------------ scan

    def _start_scan(self, allow_onelibrary_bridge: bool = False):
        usb_path = self._get_selected_path()
        if not usb_path.exists():
            messagebox.showerror("Error", f"Path does not exist: {usb_path}")
            return

        self.scan_generation += 1
        scan_generation = self.scan_generation
        profile_type = CompatibilityProfileType(
            PROFILE_VALUES.get(self.profile_var.get(), self.profile_var.get())
        )
        target_fmt = TargetFormat(self.format_var.get().lower())
        enforce_16_bit = self.bit_depth_var.get() == "Enforce 16-bit"
        self.is_scanning = True
        self.lbl_status.configure(text="Scanning database...")
        self.tree.delete(*self.tree.get_children())
        self.btn_scan.configure(state="disabled")
        self.btn_cleanup.configure(state="disabled")
        self.btn_convert.configure(
            state="disabled",
            text=(
                "Convert Tracks (Step 1 of 2)"
                if allow_onelibrary_bridge
                else "Convert Planned Tracks"
            ),
        )

        def run():
            try:
                hw_profile = get_profile(profile_type)
                summary = self.engine.scan(
                    usb_root=usb_path,
                    profile=hw_profile,
                    forced_target_format=target_fmt,
                    enforce_pcm_16_bit=enforce_16_bit,
                    allow_onelibrary_bridge=allow_onelibrary_bridge,
                )
            except Exception as e:
                msg = str(e)
                self._post_to_ui(self._on_scan_error, msg, scan_generation)
                return
            self._post_to_ui(
                self._accept_scan_result,
                scan_generation,
                usb_path,
                summary,
            )

        threading.Thread(target=run, daemon=True).start()

    def _accept_scan_result(self, generation: int, usb_path: Path, summary: ScanSummary):
        if generation != self.scan_generation or usb_path != self._get_selected_path():
            return
        self.summary = summary
        self._render_scan()

    def _on_scan_error(self, msg: str, generation: int):
        if generation != self.scan_generation:
            return
        self.is_scanning = False
        self.btn_scan.configure(state="normal")
        self.btn_cleanup.configure(state="normal")
        self.lbl_status.configure(text="Scan failed.")
        messagebox.showerror("Scan Failed", f"Could not read the Rekordbox database:\n{msg}")

    def _render_scan(self):
        self.is_scanning = False
        self.btn_scan.configure(state="normal")
        self.btn_cleanup.configure(state="normal")
        if not self.summary:
            return
        if self.summary.has_dlp and not self.summary.onelibrary_bridge_mode:
            if not self.summary.has_export_pdb:
                messagebox.showerror(
                    "OneLibrary-only Export",
                    self.summary.unsupported_reason
                    or "This USB has no traditional Device Library to convert.",
                )
                self.lbl_status.configure(text="No files changed: Device Library is missing.")
                self.btn_convert.configure(state="disabled")
                return
            proceed = not self.show_guidance_var.get() or messagebox.askyesno(
                "Two-Step Rekordbox Update Required",
                ONELIBRARY_BRIDGE_PROMPT,
                default=messagebox.NO,
            )
            self.btn_convert.configure(state="disabled")
            if proceed:
                self._start_scan(allow_onelibrary_bridge=True)
            else:
                self.lbl_status.configure(text="No files changed: two-step workflow declined.")
            return
        if not self.summary.has_export_pdb:
            messagebox.showerror(
                "Error",
                self.summary.unsupported_reason
                or "No PIONEER/rekordbox/export.pdb found on this drive.",
            )
            self.lbl_status.configure(text="Scan failed: export is missing or unsafe.")
            return

        for i, task in enumerate(self.summary.tasks):
            t = task.track
            spec = f"{t.sample_rate / 1000:g} kHz / {t.sample_depth}-bit"
            target_spec = (
                f"{task.target_format.value.upper()} "
                f"{task.target_sample_rate / 1000:g} kHz / {task.target_sample_depth}-bit"
            )
            self.tree.insert(
                "",
                "end",
                values=(t.id, t.title or t.filename, t.extension.upper(), spec, target_spec),
                tags=("odd" if i % 2 else "even",),
            )

        total = self.summary.total_tracks
        incompat = self.summary.incompatible_tracks
        compat = self.summary.compatible_tracks

        self.card_total.val_label.configure(text=str(total))
        self.card_compat.val_label.configure(text=str(compat))
        self.card_incompat.val_label.configure(text=str(incompat))
        self.lbl_track_count.configure(text=f"{incompat} of {total} tracks")

        if self.summary.onelibrary_bridge_mode:
            self.del_switch.select()
            self.del_switch.configure(state="disabled")
            self.backup_switch.select()
            self.backup_switch.configure(state="disabled")
            self.btn_convert.configure(text="Convert Tracks (Step 1 of 2)")
            self.lbl_status.configure(
                text=(
                    f"Step 1 of 2 ready: {incompat} tracks need conversion. "
                    "You will finish Step 2 in Rekordbox."
                )
            )
        else:
            self.del_switch.configure(state="normal")
            self.backup_switch.configure(state="normal")
            self.btn_convert.configure(text="Convert Planned Tracks")
            self.lbl_status.configure(
                text=(
                    f"Scan complete: {incompat} tracks require conversion to "
                    f"{self.format_var.get().upper()}."
                )
            )

        use_local_backup = bool(self.local_backup_var.get().strip()) or (
            self.summary.onelibrary_bridge_mode
        )
        required_usb_space = (
            self.summary.required_space_with_local_backup_bytes
            if use_local_backup
            else self.summary.required_space_bytes
        )
        insufficient_space = (
            incompat > 0
            and required_usb_space > self.summary.free_space_bytes
        )
        if insufficient_space:
            mib = 1024 * 1024
            required_mib = (required_usb_space + mib - 1) // mib
            free_mib = self.summary.free_space_bytes // mib
            recommended_mib = ((required_mib + 99) // 100) * 100
            self.btn_convert.configure(state="disabled")
            self.lbl_status.configure(
                text=(
                    f"Not enough USB space: about {required_mib} MiB required, "
                    f"{free_mib} MiB free."
                )
            )
            messagebox.showerror(
                "Not Enough Space on the USB",
                f"This conversion needs approximately {required_mib} MiB of free space on the "
                f"USB, but only {free_mib} MiB is available.\n\n"
                "This estimate already credits the originals that can be moved to a verified "
                "local backup. The final converted library still has to fit on the USB.\n\n"
                "What to do:\n"
                f"1. Make at least {recommended_mib} MiB free on the USB, preferably by removing "
                "exported content through Rekordbox rather than deleting referenced files in "
                "Finder or Explorer.\n"
                "2. Alternatively, re-export the library to a larger USB.\n"
                "3. Scan the USB again.",
            )
        elif incompat > 0:
            self.btn_convert.configure(state="normal")
            if use_local_backup:
                local_mib = (
                    self.summary.local_backup_required_space_bytes + 1024 * 1024 - 1
                ) // (1024 * 1024)
                self.lbl_status.configure(
                    text=(
                        f"Ready: local recovery needs about {local_mib} MiB; the USB needs "
                        f"about {(required_usb_space + 1024 * 1024 - 1) // (1024 * 1024)} MiB free."
                    )
                )
        else:
            self.btn_convert.configure(state="disabled")
            self.lbl_status.configure(
                text="Scan complete: no conversion is needed for the selected settings."
            )
            if self.show_guidance_var.get():
                messagebox.showinfo(
                    "No Conversion Needed",
                    "All tracks already match the selected compatibility and bit-depth settings.",
                )

    # ------------------------------------------------------------------ convert

    def _show_conversion_started(self, total: int):
        """Make background conversion activity visible before work begins."""
        self.btn_convert.configure(
            state="disabled",
            text=f"Converting 0 of {total}...",
        )
        self.progress_bar.stop()
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.set(0)
        self.progress_bar.start()
        self.lbl_status.configure(
            text=(
                f"Conversion started — 0 of {total} tracks complete. "
                "Validating files and preparing backups; do not eject the USB."
            )
        )
        self.update_idletasks()

    def _start_conversion(self):
        if not self.summary or not self.summary.tasks:
            return

        tools_ok, tools_message = self.engine.audio_converter.check_tools()
        if not tools_ok:
            messagebox.showerror("FFmpeg Not Found", tools_message)
            return

        threads = int(self.threads_slider.get())
        conversion_summary = self.summary
        bridge_mode = conversion_summary.onelibrary_bridge_mode
        backup_text = self.local_backup_var.get().strip()
        if bridge_mode and not backup_text:
            self._browse_local_backup_folder()
            backup_text = self.local_backup_var.get().strip()
            if not backup_text:
                messagebox.showerror(
                    "Local Backup Folder Required",
                    "Choose a local folder before starting the OneLibrary conversion.",
                )
                return
        local_backup_dir = Path(backup_text).expanduser() if backup_text else None
        delete_original = True if bridge_mode else self.del_switch.get() == 1
        if local_backup_dir and not delete_original:
            messagebox.showerror(
                "Conflicting Original Settings",
                "Enable original removal when using a local original backup folder.",
            )
            return
        create_backup = True if bridge_mode else self.backup_switch.get() == 1
        clean_dotfiles = self.dotfiles_switch.get() == 1
        if bridge_mode:
            bridge_detail = (
                ONELIBRARY_BRIDGE_CONFIRM_MESSAGE
                if self.show_guidance_var.get()
                else (
                    "This changes Device Library only. You must rebuild OneLibrary from Device "
                    "Library in Rekordbox before using the USB."
                )
            )
            confirm = messagebox.askyesno(
                "Start Step 1 of 2",
                f"{len(self.summary.tasks)} tracks will be converted to "
                f"{self.format_var.get().upper()} using {threads} threads.\n\n"
                f"{bridge_detail}",
                default=messagebox.NO,
            )
        else:
            confirm = messagebox.askyesno(
                "Confirm Conversion",
                f"Convert {len(self.summary.tasks)} tracks in parallel ({threads} threads) to "
                f"{self.format_var.get().upper()}?\n\n"
                "export.pdb and ANLZ waveforms will be synchronized automatically.",
            )
        if not confirm:
            return

        existing_targets = [
            task
            for task in conversion_summary.tasks
            if task.target_abs_path.is_file()
            and self.engine._path_key(task.target_abs_path)
            != self.engine._path_key(task.source_abs_path)
        ]
        replace_existing_targets = False
        if existing_targets:
            if not local_backup_dir:
                messagebox.showerror(
                    "Local Backup Required",
                    "Existing conversion targets were found. Choose a local backup folder "
                    "so they can be archived before replacement.",
                )
                return
            referenced_count = sum(
                task.existing_target_track_id is not None for task in existing_targets
            )
            unreferenced_count = len(existing_targets) - referenced_count
            handling = []
            if referenced_count:
                handling.append(
                    f"{referenced_count} Rekordbox-referenced target(s) will be reused only "
                    "after a decoded-audio hash comparison"
                )
            if unreferenced_count:
                handling.append(
                    f"{unreferenced_count} unreferenced target(s) will be archived, removed, "
                    "and regenerated"
                )
            replace_existing_targets = messagebox.askyesno(
                "Resolve Existing Targets?",
                f"Found {len(existing_targets)} existing target file(s).\n\n"
                + ".\n".join(handling)
                + ".\n\nOriginal FLACs and replaced targets remain recoverable from the "
                "local archive. Proceed?",
                default=messagebox.NO,
            )
            if not replace_existing_targets:
                return

        self.is_converting = True
        self.btn_scan.configure(state="disabled")
        self.btn_restore.configure(state="disabled")
        self.btn_cleanup.configure(state="disabled")
        self._set_conversion_controls(False)
        self._show_conversion_started(len(conversion_summary.tasks))

        def run():
            try:
                def on_prog(task, cur, total_count):
                    pct = cur / total_count
                    filename = task.track.filename
                    self._post_to_ui(
                        self._update_prog,
                        pct,
                        filename,
                        cur,
                        total_count,
                    )

                result = self.engine.execute(
                    summary=conversion_summary,
                    delete_original=delete_original,
                    backup=create_backup,
                    threads=threads,
                    clean_dotfiles=clean_dotfiles,
                    progress_callback=on_prog,
                    allow_onelibrary_bridge=bridge_mode,
                    local_original_backup_dir=local_backup_dir,
                    replace_existing_targets=replace_existing_targets,
                )
            except Exception as e:
                msg = str(e)
                self._post_to_ui(self._on_conversion_error, msg)
                return
            self._post_to_ui(
                self._on_finish,
                result,
                conversion_summary,
                delete_original,
            )

        threading.Thread(target=run, daemon=True).start()

    def _update_prog(self, pct: float, name: str, current: int, total: int):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(pct)
        self.btn_convert.configure(text=f"Converting {current} of {total}...")
        self.lbl_status.configure(
            text=(
                f"Conversion in progress — {current} of {total} tracks processed. "
                f"Latest: {name[:45]}. Do not eject the USB."
            )
        )

    def _on_conversion_error(self, msg: str):
        self.is_converting = False
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self._set_conversion_controls(True)
        self.btn_scan.configure(state="normal")
        self.btn_restore.configure(state="normal")
        self.btn_cleanup.configure(state="normal")
        self.lbl_status.configure(text="Conversion failed.")
        messagebox.showerror("Conversion Failed", f"An unexpected error occurred:\n{msg}")

    def _on_finish(
        self, result: dict, conversion_summary: ScanSummary, delete_original: bool
    ):
        self.is_converting = False
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self._set_conversion_controls(True)
        self.btn_scan.configure(state="normal")
        self.btn_restore.configure(state="normal")
        self.btn_cleanup.configure(state="normal")
        self.progress_bar.set(1.0)

        if result.get("success"):
            if result.get("onelibrary_sync_required"):
                self.lbl_status.configure(
                    text="Step 1 of 2 complete. Complete Step 2 in Rekordbox before using this USB."
                )
                if self.show_guidance_var.get():
                    messagebox.showwarning(
                        "Step 1 Complete — Finish in Rekordbox",
                        f"Converted {result['completed']} tracks.\n\n"
                        f"Local recovery archive:\n{result.get('local_backup_session')}\n\n"
                        f"{ONELIBRARY_REBUILD_REQUIRED_MESSAGE}",
                    )
                self.summary = None
                self.btn_convert.configure(
                    state="disabled",
                    text="Finish Step 2 in Rekordbox",
                )
                self.del_switch.configure(state="normal")
                self.backup_switch.configure(state="normal")
                return
            self.lbl_status.configure(text=f"Conversion complete: {result.get('completed', 0)} tracks converted.")
            cleaned = result.get("cleaned_dotfiles", 0)
            dot_msg = f"\n- Cleaned {cleaned} macOS ghost files" if cleaned else ""
            original_msg = (
                "- Original removal was attempted only after each durable commit."
                if delete_original
                else "- Original audio files were retained."
            )
            warnings = result.get("warnings") or []
            warning_msg = f"\n\nWarning:\n{warnings[0]}" if warnings else ""
            if warnings or self.show_guidance_var.get():
                show_result = messagebox.showwarning if warnings else messagebox.showinfo
                show_result(
                    "Conversion Complete with Warnings" if warnings else "Conversion Complete",
                    f"Successfully converted {result['completed']} tracks.\n\n"
                    f"- Rekordbox Device Library paths were updated.\n"
                    f"- Updated {result.get('anlz_updated', 0)} waveform-analysis path files.{dot_msg}\n"
                    f"{original_msg}{warning_msg}",
                )
        else:
            completed = result.get("completed", 0)
            failed = result.get("failed", 0)
            self.lbl_status.configure(text=f"Conversion finished with errors: {completed} converted, {failed} failed.")
            errors = list(result.get("preflight_errors") or [])
            errors.extend(t.error for t in conversion_summary.tasks if t.error)
            unique_errors = list(dict.fromkeys(error for error in errors if error))
            if unique_errors:
                displayed_errors = "\n".join(
                    f"- {error}" for error in unique_errors[:3]
                )
                remaining = len(unique_errors) - 3
                if remaining > 0:
                    displayed_errors += f"\n- {remaining} additional error types"
                detail = f"\n\nWhy conversion stopped:\n{displayed_errors}"
            elif result.get("error"):
                detail = f"\n\nWhy conversion stopped:\n{result['error']}"
            else:
                detail = ""
            messagebox.showwarning(
                "Completed with Errors",
                f"Converted: {completed}\nFailed: {failed}{detail}"
                + (
                    "\n\nOneLibrary was not updated. Keep the local recovery archive and either "
                    "restore it or resolve the failed tracks before rebuilding OneLibrary."
                    if conversion_summary.onelibrary_bridge_mode and completed
                    else ""
                ),
            )
        if conversion_summary.onelibrary_bridge_mode:
            self.summary = None
            self.btn_convert.configure(
                state="disabled",
                text="Scan Again Before Retrying",
            )
            self.del_switch.configure(state="normal")
            self.backup_switch.configure(state="normal")
            return
        self._start_scan()

    # ----------------------------------------------------- retained originals

    def _set_cleanup_busy(self, busy: bool):
        self.is_cleanup_running = busy
        state = "disabled" if busy else "normal"
        self.btn_scan.configure(state=state)
        self.btn_restore.configure(state=state)
        self.btn_cleanup.configure(state=state)
        self._set_conversion_controls(not busy)
        if busy:
            self.btn_convert.configure(state="disabled")
        else:
            self.btn_convert.configure(
                state=(
                    self.cleanup_previous_convert_state
                    if self.summary is not None
                    else "disabled"
                )
            )

    def _start_original_cleanup(self):
        usb_path = self._get_selected_path()
        if not usb_path.exists():
            messagebox.showerror("Cleanup Unavailable", f"Path does not exist: {usb_path}")
            return
        if self.is_converting or self.is_scanning or self.is_cleanup_running:
            return

        self.cleanup_previous_convert_state = str(self.btn_convert.cget("state"))
        self._set_cleanup_busy(True)
        self.progress_bar.stop()
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.set(0)
        self.progress_bar.start()
        self.lbl_status.configure(
            text="Checking retained originals and converted replacements; do not eject the USB."
        )

        def run():
            try:
                plan = self.engine.plan_retained_original_cleanup(usb_path)
            except Exception as exc:
                self._post_to_ui(self._on_cleanup_error, str(exc))
                return
            self._post_to_ui(self._confirm_original_cleanup, plan)

        threading.Thread(target=run, daemon=True).start()

    def _confirm_original_cleanup(self, plan: OriginalCleanupPlan):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(0)
        if plan.errors:
            self._set_cleanup_busy(False)
            details = "\n".join(f"- {error}" for error in plan.errors[:4])
            self.lbl_status.configure(text="Retained-original cleanup was not started.")
            messagebox.showerror(
                "Cleanup Safety Check Failed",
                f"No files were removed.\n\n{details}",
            )
            return

        gib = plan.total_bytes / (1024 ** 3)
        mib = plan.total_bytes / (1024 ** 2)
        space_text = f"{gib:.2f} GiB" if gib >= 1 else f"{mib:.1f} MiB"
        confirmation = (
            f"Permanently remove {len(plan.candidates)} retained original files and reclaim "
            f"approximately {space_text}?\n\n"
            "Continue only after all of these are true:\n"
            "1. In Rekordbox, you ran OneLibrary > Convert from Device Library.\n"
            "2. You opened OneLibrary on this USB and verified the converted tracks.\n"
            "3. You have another copy of the original audio.\n\n"
            "The app verified each replacement against Device Library and its ANLZ path. "
            "This deletion cannot be undone, and database backups do not contain audio files."
        )
        if plan.warnings:
            confirmation += f"\n\nImportant: {plan.warnings[0]}"
        if not messagebox.askyesno(
            "Remove Retained Originals",
            confirmation,
            default=messagebox.NO,
        ):
            self._set_cleanup_busy(False)
            self.lbl_status.configure(text="Retained-original cleanup canceled; no files removed.")
            return

        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.lbl_status.configure(
            text=f"Removing {len(plan.candidates)} verified originals; do not eject the USB."
        )

        def run():
            try:
                result = self.engine.cleanup_retained_originals(plan)
            except Exception as exc:
                self._post_to_ui(self._on_cleanup_error, str(exc))
                return
            self._post_to_ui(self._on_cleanup_finish, result)

        threading.Thread(target=run, daemon=True).start()

    def _on_cleanup_error(self, message: str):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(0)
        self._set_cleanup_busy(False)
        self.lbl_status.configure(text="Retained-original cleanup failed.")
        messagebox.showerror("Cleanup Failed", f"No further files were removed.\n\n{message}")

    def _on_cleanup_finish(self, result: dict):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0 if result.get("success") else 0)
        if result.get("success"):
            self.summary = None
        self._set_cleanup_busy(False)

        removed = int(result.get("removed", 0))
        freed_bytes = int(result.get("freed_bytes", 0))
        freed_gib = freed_bytes / (1024 ** 3)
        freed_mib = freed_bytes / (1024 ** 2)
        freed_text = f"{freed_gib:.2f} GiB" if freed_gib >= 1 else f"{freed_mib:.1f} MiB"
        if result.get("success"):
            self.lbl_status.configure(
                text=f"Cleanup complete: removed {removed} originals and reclaimed {freed_text}."
            )
            messagebox.showinfo(
                "Retained Originals Removed",
                f"Removed {removed} verified original files and reclaimed approximately "
                f"{freed_text}.\n\nThe converted files, Device Library, OneLibrary, and ANLZ "
                "files were left in place. Safely eject the USB before using it.",
            )
            return

        details = "\n".join(f"- {error}" for error in result.get("errors", [])[:4])
        self.lbl_status.configure(
            text=f"Cleanup incomplete: removed {removed}; {result.get('failed', 0)} failed."
        )
        messagebox.showerror(
            "Cleanup Incomplete",
            f"Removed: {removed}\nFailed: {result.get('failed', 0)}\n\n{details}",
        )

    # ------------------------------------------------------------------ restore

    def _restore_backup(self):
        usb_path = self._get_selected_path()
        if not usb_path.exists():
            return
        restore_local = messagebox.askyesnocancel(
            "Choose Backup Type",
            "Restore from a local original backup session?\n\n"
            "Yes: restore audio and all Rekordbox metadata from a local session.\n"
            "No: restore the legacy .bak files stored on the USB.",
            default=messagebox.YES,
        )
        if restore_local is None:
            return
        if restore_local:
            session_path = filedialog.askdirectory(
                title="Select RekordboxBackup Session Folder"
            )
            if not session_path:
                return
            confirm = messagebox.askyesno(
                "Confirm Full Local Restore",
                "Restore original audio and all archived Rekordbox metadata? Verified converted "
                "replacements will be removed.",
                default=messagebox.NO,
            )
            if not confirm:
                return
            success, msg = self.engine.restore_local_backup(Path(session_path), usb_path)
            if success:
                messagebox.showinfo("Restored", msg)
                self._start_scan()
            else:
                messagebox.showerror("Restore Refused", msg)
            return
        dlp_paths = (
            usb_path / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db",
            usb_path / "PIONEER" / "rekordbox" / "exportLibrary.db",
        )
        restore_message = f"Restore {usb_path} Device Library and ANLZ files from .bak backup?"
        if any(path.is_file() for path in dlp_paths):
            restore_message += (
                "\n\nOneLibrary will not be restored. If it has already been rebuilt, you must "
                "run OneLibrary > Convert from Device Library in Rekordbox again after this "
                "restore."
            )
        confirm = messagebox.askyesno(
            "Confirm Restore",
            restore_message,
            default=messagebox.NO,
        )
        if not confirm:
            return
        success, msg = self.engine.restore_backup(usb_path)
        if success:
            messagebox.showinfo("Restored", msg)
            self._start_scan()
        else:
            messagebox.showerror("Error", msg)


def run_threading_smoke_test() -> None:
    """Exercise worker-to-main-thread delivery against the bundled Tcl/Tk."""
    app = ModernRekordboxGUI()
    app.withdraw()
    expected_callbacks = 2000
    callback_threads = []
    worker_thread_id = []
    timed_out = []
    deadline = time.monotonic() + 10

    def record_callback() -> None:
        callback_threads.append(threading.get_ident())

    def post_callbacks() -> None:
        worker_thread_id.append(threading.get_ident())
        for _ in range(expected_callbacks):
            app._post_to_ui(record_callback)

    worker = threading.Thread(target=post_callbacks, daemon=True)
    worker.start()

    def check_completion() -> None:
        if len(callback_threads) == expected_callbacks and not worker.is_alive():
            app._on_close()
            return
        if time.monotonic() >= deadline:
            timed_out.append(True)
            app._on_close()
            return
        app.after(10, check_completion)

    app.after(10, check_completion)
    app.mainloop()
    worker.join(timeout=1)

    if timed_out or len(callback_threads) != expected_callbacks:
        raise RuntimeError(
            "Tk worker-delivery smoke test timed out: "
            f"received {len(callback_threads)} of {expected_callbacks} callbacks"
        )
    if not worker_thread_id or any(
        thread_id == worker_thread_id[0] for thread_id in callback_threads
    ):
        raise RuntimeError("A Tk callback ran on the posting worker thread")


def main():
    app = ModernRekordboxGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
