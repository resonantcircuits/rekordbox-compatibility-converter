"""Modern, sleek Dark/Light mode GUI using CustomTkinter."""

import os
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox, ttk
    import tkinter as tk
except ImportError:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    ctk = None

from ..core.audio_converter import AudioConverter
from ..core.engine import ConversionEngine
from ..core.models import CompatibilityProfileType, ScanSummary, TargetFormat
from ..core.profiles import PROFILES, get_profile
from ..core.usb_detector import USBDetector


class ModernRekordboxGUI(ctk.CTk if ctk else tk.Tk):
    """Modern DJ-styled cross-platform GUI for Rekordbox format conversion."""

    def __init__(self):
        super().__init__()

        if ctk:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")

        self.title("Rekordbox Format Checker & CDJ Converter")
        self.geometry("960x760")
        self.minsize(800, 600)

        self.engine = ConversionEngine()
        self.summary: Optional[ScanSummary] = None
        self.is_converting = False
        self.drive_map: Dict[str, Path] = {}

        self._build_ui()
        self._refresh_drives()

    def _build_ui(self):
        # 1. Header Banner
        header_frame = ctk.CTkFrame(self, corner_radius=10)
        header_frame.pack(fill="x", padx=16, pady=(16, 8))

        title_lbl = ctk.CTkLabel(
            header_frame,
            text="🎧 Rekordbox CDJ Format Converter",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title_lbl.pack(side="left", padx=16, pady=12)

        theme_switch = ctk.CTkOptionMenu(
            header_frame,
            values=["Dark", "Light", "System"],
            command=self._change_theme,
            width=100,
        )
        theme_switch.set("Dark")
        theme_switch.pack(side="right", padx=16, pady=12)

        # 2. Main Config Card
        config_card = ctk.CTkFrame(self, corner_radius=10)
        config_card.pack(fill="x", padx=16, pady=8)

        # Drive Row
        drive_row = ctk.CTkFrame(config_card, fg_color="transparent")
        drive_row.pack(fill="x", padx=16, pady=(12, 6))

        ctk.CTkLabel(drive_row, text="Rekordbox USB Drive:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0, 10))
        self.drive_menu = ctk.CTkOptionMenu(drive_row, values=["Scanning..."], width=400, command=self._on_drive_selected)
        self.drive_menu.pack(side="left", fill="x", expand=True, padx=(0, 10))

        btn_browse = ctk.CTkButton(drive_row, text="📁 Browse...", width=100, command=self._browse_folder)
        btn_browse.pack(side="left", padx=(0, 6))

        btn_refresh = ctk.CTkButton(drive_row, text="⟳ Refresh", width=90, fg_color="#3A3A3C", hover_color="#48484A", command=self._refresh_drives)
        btn_refresh.pack(side="left")

        # Profile & Format Row
        settings_row = ctk.CTkFrame(config_card, fg_color="transparent")
        settings_row.pack(fill="x", padx=16, pady=6)

        ctk.CTkLabel(settings_row, text="Target Profile:").pack(side="left", padx=(0, 6))
        self.profile_var = ctk.StringVar(value="standard")
        profile_menu = ctk.CTkOptionMenu(settings_row, values=["standard", "maximum", "modern"], variable=self.profile_var, width=130)
        profile_menu.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(settings_row, text="Target Format:").pack(side="left", padx=(0, 6))
        self.format_var = ctk.StringVar(value="aiff")
        format_menu = ctk.CTkOptionMenu(settings_row, values=["aiff", "wav", "mp3"], variable=self.format_var, width=100)
        format_menu.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(settings_row, text="Threads:").pack(side="left", padx=(0, 6))
        self.threads_slider = ctk.CTkSlider(settings_row, from_=1, to=16, number_of_steps=15, width=120)
        self.threads_slider.set(min(8, os.cpu_count() or 4))
        self.threads_slider.pack(side="left", padx=(0, 6))

        self.threads_lbl = ctk.CTkLabel(settings_row, text=f"{int(self.threads_slider.get())}")
        self.threads_lbl.pack(side="left")
        self.threads_slider.configure(command=lambda val: self.threads_lbl.configure(text=f"{int(val)}"))

        # Switches Row
        switches_row = ctk.CTkFrame(config_card, fg_color="transparent")
        switches_row.pack(fill="x", padx=16, pady=(4, 12))

        self.del_switch = ctk.CTkSwitch(switches_row, text="Delete originals after conversion (Option A)")
        self.del_switch.select()
        self.del_switch.pack(side="left", padx=(0, 16))

        self.dotfiles_switch = ctk.CTkSwitch(switches_row, text="Clean macOS ghost files (._*)")
        self.dotfiles_switch.select()
        self.dotfiles_switch.pack(side="left", padx=(0, 16))

        self.backup_switch = ctk.CTkSwitch(switches_row, text="Create .bak backups")
        self.backup_switch.select()
        self.backup_switch.pack(side="left")

        # 3. Stats Dashboard Cards
        stats_frame = ctk.CTkFrame(self, fg_color="transparent")
        stats_frame.pack(fill="x", padx=16, pady=4)

        self.card_total = self._create_stat_card(stats_frame, "TOTAL TRACKS", "--", "#2C2C2E")
        self.card_total.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.card_compat = self._create_stat_card(stats_frame, "COMPATIBLE", "--", "#1E382B")
        self.card_compat.pack(side="left", fill="x", expand=True, padx=4)

        self.card_incompat = self._create_stat_card(stats_frame, "INCOMPATIBLE", "--", "#3D2424")
        self.card_incompat.pack(side="left", fill="x", expand=True, padx=(8, 0))

        # 4. Action Buttons Bar
        action_bar = ctk.CTkFrame(self, fg_color="transparent")
        action_bar.pack(fill="x", padx=16, pady=8)

        self.btn_scan = ctk.CTkButton(
            action_bar,
            text="🔍 Scan USB Drive",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=38,
            command=self._start_scan,
        )
        self.btn_scan.pack(side="left", padx=(0, 10))

        self.btn_convert = ctk.CTkButton(
            action_bar,
            text="⚡ Convert Incompatible Tracks",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#30D158",
            hover_color="#28CD41",
            text_color="#000000",
            height=38,
            state="disabled",
            command=self._start_conversion,
        )
        self.btn_convert.pack(side="left", padx=(0, 10))

        self.btn_restore = ctk.CTkButton(
            action_bar,
            text="↺ Restore Backup",
            fg_color="#3A3A3C",
            hover_color="#48484A",
            height=38,
            command=self._restore_backup,
        )
        self.btn_restore.pack(side="left")

        # 5. Track Table View
        table_card = ctk.CTkFrame(self, corner_radius=10)
        table_card.pack(fill="both", expand=True, padx=16, pady=6)

        table_header = ctk.CTkFrame(table_card, fg_color="transparent")
        table_header.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(table_header, text="Tracks Requiring Conversion", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")

        # Treeview styled inside CTkFrame
        tree_container = tk.Frame(table_card, bg="#1C1C1E")
        tree_container.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        columns = ("id", "title", "format", "specs", "target")
        self.tree = ttk.Treeview(tree_container, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("id", text="ID")
        self.tree.heading("title", text="Track Title")
        self.tree.heading("format", text="Format")
        self.tree.heading("specs", text="Specs")
        self.tree.heading("target", text="Target Format")

        self.tree.column("id", width=60, anchor="center")
        self.tree.column("title", width=340)
        self.tree.column("format", width=90, anchor="center")
        self.tree.column("specs", width=140, anchor="center")
        self.tree.column("target", width=140, anchor="center")

        tree_scroll = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        # 6. Bottom Progress & Status Bar
        bottom_frame = ctk.CTkFrame(self, corner_radius=10)
        bottom_frame.pack(fill="x", padx=16, pady=(4, 16))

        self.progress_bar = ctk.CTkProgressBar(bottom_frame)
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", padx=14, pady=(10, 4))

        self.lbl_status = ctk.CTkLabel(bottom_frame, text="Ready. Select a drive and click 'Scan USB Drive'.", font=ctk.CTkFont(size=12))
        self.lbl_status.pack(side="left", padx=14, pady=(0, 8))

    def _create_stat_card(self, parent, title: str, initial_val: str, bg_color: str):
        card = ctk.CTkFrame(parent, corner_radius=8, fg_color=bg_color)
        lbl_title = ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=10, weight="bold"), text_color="#AEAEB2")
        lbl_title.pack(pady=(8, 0))
        lbl_val = ctk.CTkLabel(card, text=initial_val, font=ctk.CTkFont(size=22, weight="bold"))
        lbl_val.pack(pady=(0, 8))
        card.val_label = lbl_val
        return card

    def _change_theme(self, mode: str):
        if ctk:
            ctk.set_appearance_mode(mode.lower())

    def _refresh_drives(self):
        detected = USBDetector.list_rekordbox_drives()
        self.drive_map = {}
        values = []
        for path, label in detected:
            display = f"{label} ({path})"
            self.drive_map[display] = path
            values.append(display)

        if values:
            self.drive_menu.configure(values=values)
            self.drive_menu.set(values[0])
            self._on_drive_selected(values[0])
        else:
            self.drive_menu.configure(values=["No Rekordbox USB detected"])
            self.drive_menu.set("No Rekordbox USB detected")

    def _browse_folder(self):
        path = filedialog.askdirectory(title="Select Rekordbox USB Export Directory")
        if path:
            p = Path(path)
            display = f"Custom: {p.name} ({p})"
            self.drive_map[display] = p
            vals = list(self.drive_menu.cget("values"))
            vals.append(display)
            self.drive_menu.configure(values=vals)
            self.drive_menu.set(display)
            self._on_drive_selected(display)

    def _on_drive_selected(self, choice: str):
        self.btn_convert.configure(state="disabled")
        self.card_total.val_label.configure(text="--")
        self.card_compat.val_label.configure(text="--")
        self.card_incompat.val_label.configure(text="--")
        self.lbl_status.configure(text="Ready to scan.")

    def _get_selected_path(self) -> Path:
        selected = self.drive_menu.get()
        if selected in self.drive_map:
            return self.drive_map[selected]
        return Path(selected)

    def _start_scan(self):
        usb_path = self._get_selected_path()
        if not usb_path.exists():
            messagebox.showerror("Error", f"Path does not exist: {usb_path}")
            return

        self.lbl_status.configure(text="Scanning database...")
        self.tree.delete(*self.tree.get_children())
        self.btn_scan.configure(state="disabled")

        def run():
            profile_type = CompatibilityProfileType(self.profile_var.get())
            hw_profile = get_profile(profile_type)
            target_fmt = TargetFormat(self.format_var.get())

            self.summary = self.engine.scan(
                usb_root=usb_path,
                profile=hw_profile,
                forced_target_format=target_fmt,
            )
            self.after(0, self._render_scan)

        threading.Thread(target=run, daemon=True).start()

    def _render_scan(self):
        self.btn_scan.configure(state="normal")
        if not self.summary or not self.summary.has_export_pdb:
            messagebox.showerror("Error", "No PIONEER/rekordbox/export.pdb found on this drive.")
            self.lbl_status.configure(text="Scan failed: export.pdb missing.")
            return

        for task in self.summary.tasks:
            t = task.track
            spec = f"{t.sample_rate}Hz {t.sample_depth}b"
            self.tree.insert("", "end", values=(t.id, t.title or t.filename, t.extension.upper(), spec, task.target_format.value.upper()))

        total = self.summary.total_tracks
        incompat = self.summary.incompatible_tracks
        compat = self.summary.compatible_tracks

        self.card_total.val_label.configure(text=str(total))
        self.card_compat.val_label.configure(text=str(compat))
        self.card_incompat.val_label.configure(text=str(incompat))

        self.lbl_status.configure(text=f"Scan complete: {incompat} tracks require conversion to {self.format_var.get().upper()}.")

        if incompat > 0:
            self.btn_convert.configure(state="normal")
        else:
            self.btn_convert.configure(state="disabled")
            messagebox.showinfo("All Compatible", "All tracks are 100% compatible with the selected profile!")

    def _start_conversion(self):
        if not self.summary or not self.summary.tasks:
            return

        threads = int(self.threads_slider.get())
        confirm = messagebox.askyesno(
            "Confirm Conversion",
            f"Convert {len(self.summary.tasks)} tracks in parallel ({threads} threads) to {self.format_var.get().upper()}?\n\n"
            f"export.pdb and ANLZ waveforms will be synchronized automatically.",
        )
        if not confirm:
            return

        self.is_converting = True
        self.btn_scan.configure(state="disabled")
        self.btn_convert.configure(state="disabled")
        self.progress_bar.set(0)

        def run():
            tot = len(self.summary.tasks)

            def on_prog(task, cur, total_count):
                pct = cur / total_count
                self.after(0, lambda: self._update_prog(pct, task.track.filename))

            result = self.engine.execute(
                summary=self.summary,
                delete_original=self.del_switch.get() == 1,
                backup=self.backup_switch.get() == 1,
                threads=threads,
                clean_dotfiles=self.dotfiles_switch.get() == 1,
                progress_callback=on_prog,
            )
            self.after(0, lambda: self._on_finish(result))

        threading.Thread(target=run, daemon=True).start()

    def _update_prog(self, pct: float, name: str):
        self.progress_bar.set(pct)
        self.lbl_status.configure(text=f"Converting: {name[:45]}...")

    def _on_finish(self, result: dict):
        self.is_converting = False
        self.btn_scan.configure(state="normal")
        self.progress_bar.set(1.0)
        self.lbl_status.configure(text="Conversion completed successfully!")

        if result.get("success"):
            cleaned = result.get("cleaned_dotfiles", 0)
            dot_msg = f"\n• Cleaned {cleaned} macOS ghost files (._*)" if cleaned else ""
            messagebox.showinfo(
                "Conversion Complete",
                f"Successfully converted {result['completed']} tracks in parallel!\n\n"
                f"• Database export.pdb patched and synced.\n"
                f"• Waveforms and beatgrids intact.{dot_msg}\n"
                f"• USB is ready for Pioneer CDJ/XDJ booth.",
            )
        else:
            messagebox.showwarning(
                "Completed with Warnings",
                f"Converted: {result.get('completed', 0)}\nFailed: {result.get('failed', 0)}",
            )
        self._start_scan()

    def _restore_backup(self):
        usb_path = self._get_selected_path()
        if not usb_path.exists():
            return
        confirm = messagebox.askyesno("Confirm Restore", f"Restore {usb_path} from .bak backup?")
        if not confirm:
            return
        success, msg = self.engine.restore_backup(usb_path)
        if success:
            messagebox.showinfo("Restored", msg)
            self._start_scan()
        else:
            messagebox.showerror("Error", msg)


def main():
    app = ModernRekordboxGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
