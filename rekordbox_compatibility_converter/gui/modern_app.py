"""Modern, sleek Dark/Light mode GUI using CustomTkinter with dynamic theme styling."""

import os
import threading
from pathlib import Path
from typing import Dict, Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..core.engine import ConversionEngine
from ..core.models import CompatibilityProfileType, ScanSummary, TargetFormat
from ..core.usb_detector import USBDetector
from ..core.profiles import get_profile

PROFILE_DESCRIPTIONS = {
    "standard": "Standard Club — converts FLAC/ALAC to AIFF and downsamples >48 kHz. For CDJ-2000NXS, CDJ-900NXS, XDJ-1000/700/RX/RX2.",
    "maximum": "Maximum Compatibility — enforces 16-bit 44.1 kHz AIFF for vintage gear (CDJ-2000 original, CDJ-850, CDJ-350, XDJ-AERO).",
    "modern": "Modern Flagship — allows FLAC, ALAC and high-res audio up to 24-bit 96 kHz for CDJ-3000, CDJ-2000NXS2, XDJ-XZ, XDJ-RX3, OPUS-QUAD.",
}

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
        self.is_scanning = False
        self.drive_map: Dict[str, Path] = {}

        self._build_ui()
        self._apply_treeview_theme("dark")
        self._refresh_drives()

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
            selected_bg = "#0A84FF"
        else:
            bg_color = "#FFFFFF"
            stripe_color = "#F4F4F6"
            fg_color = "#1C1C1E"
            hdr_bg = "#E8E8ED"
            hdr_fg = "#1C1C1E"
            selected_bg = "#007AFF"

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
            text="Prepare USB exports for club CDJs — convert audio, sync database and waveforms",
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
        btn_browse = ctk.CTkButton(grid, text="Browse...", width=100, height=32, command=self._browse_folder)
        btn_browse.grid(row=1, column=1, padx=(10, 0), pady=(4, 0))
        btn_refresh = ctk.CTkButton(
            grid,
            text="Refresh",
            width=90,
            height=32,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            command=self._refresh_drives,
        )
        btn_refresh.grid(row=1, column=2, padx=(8, 0), pady=(4, 0))

        # Settings row: three labeled groups on one aligned grid
        settings = ctk.CTkFrame(config_card, fg_color="transparent")
        settings.pack(fill="x", padx=18, pady=(0, 4))

        self._field_label(settings, "Target Profile").grid(row=0, column=0, sticky="w")
        self._field_label(settings, "Target Format").grid(row=0, column=1, sticky="w", padx=(24, 0))
        self._field_label(settings, "Parallel Threads").grid(row=0, column=2, sticky="w", padx=(24, 0))

        self.profile_var = ctk.StringVar(value="standard")
        profile_menu = ctk.CTkOptionMenu(
            settings,
            values=["standard", "maximum", "modern"],
            variable=self.profile_var,
            width=150,
            height=30,
            command=self._on_profile_changed,
        )
        profile_menu.grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.format_var = ctk.StringVar(value="aiff")
        format_menu = ctk.CTkOptionMenu(
            settings,
            values=["aiff", "wav", "mp3"],
            variable=self.format_var,
            width=110,
            height=30,
        )
        format_menu.grid(row=1, column=1, sticky="w", padx=(24, 0), pady=(4, 0))

        threads_box = ctk.CTkFrame(settings, fg_color="transparent")
        threads_box.grid(row=1, column=2, sticky="w", padx=(24, 0), pady=(4, 0))
        self.threads_slider = ctk.CTkSlider(threads_box, from_=1, to=16, number_of_steps=15, width=140)
        self.threads_slider.set(min(8, os.cpu_count() or 4))
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

        # Divider
        ctk.CTkFrame(config_card, height=1, fg_color=CARD_BORDER).pack(fill="x", padx=18)

        # Switches row
        switches_row = ctk.CTkFrame(config_card, fg_color="transparent")
        switches_row.pack(fill="x", padx=18, pady=(10, 14))

        self.del_switch = ctk.CTkSwitch(switches_row, text="Delete originals after conversion", font=ctk.CTkFont(size=12))
        self.del_switch.select()
        self.del_switch.pack(side="left", padx=(0, 22))

        self.dotfiles_switch = ctk.CTkSwitch(switches_row, text="Clean macOS ghost files (._*, .DS_Store)", font=ctk.CTkFont(size=12))
        self.dotfiles_switch.select()
        self.dotfiles_switch.pack(side="left", padx=(0, 22))

        self.backup_switch = ctk.CTkSwitch(switches_row, text="Create database backups (.bak)", font=ctk.CTkFont(size=12))
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
            fg_color=("#007AFF", "#0A84FF"),
            hover_color=("#0062CC", "#0071E3"),
            text_color="#FFFFFF",
            height=40,
            corner_radius=8,
            command=self._start_scan,
        )
        self.btn_scan.pack(side="left", padx=(0, 10))

        self.btn_convert = ctk.CTkButton(
            action_bar,
            text="Convert Incompatible Tracks",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=("#28A745", "#2EA043"),
            hover_color=("#218838", "#3FB950"),
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

        # 6. Track Table (takes all remaining vertical space)
        table_card = ctk.CTkFrame(self, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        table_card.pack(side="top", fill="both", expand=True, padx=24, pady=(4, 0))

        table_header = ctk.CTkFrame(table_card, fg_color="transparent")
        table_header.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(
            table_header,
            text="Tracks Requiring Conversion",
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
        self.lbl_profile_desc.configure(text=PROFILE_DESCRIPTIONS.get(choice, ""))
        if self.summary and not self.is_converting and not self.is_scanning:
            self._start_scan()

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

    def _on_drive_selected(self, choice: str):
        self.btn_convert.configure(state="disabled")
        self.card_total.val_label.configure(text="--")
        self.card_compat.val_label.configure(text="--")
        self.card_incompat.val_label.configure(text="--")
        self.tree.delete(*self.tree.get_children())
        self.lbl_track_count.configure(text="")
        self.lbl_status.configure(text="Ready to scan.")

    def _get_selected_path(self) -> Path:
        selected = self.drive_menu.get()
        if selected in self.drive_map:
            return self.drive_map[selected]
        return Path(selected)

    # ------------------------------------------------------------------ scan

    def _start_scan(self):
        usb_path = self._get_selected_path()
        if not usb_path.exists():
            messagebox.showerror("Error", f"Path does not exist: {usb_path}")
            return

        self.is_scanning = True
        self.lbl_status.configure(text="Scanning database...")
        self.tree.delete(*self.tree.get_children())
        self.btn_scan.configure(state="disabled")
        self.btn_convert.configure(state="disabled")

        def run():
            try:
                profile_type = CompatibilityProfileType(self.profile_var.get())
                hw_profile = get_profile(profile_type)
                target_fmt = TargetFormat(self.format_var.get())

                summary = self.engine.scan(
                    usb_root=usb_path,
                    profile=hw_profile,
                    forced_target_format=target_fmt,
                )
            except Exception as e:
                self.after(0, lambda: self._on_scan_error(str(e)))
                return
            self.summary = summary
            self.after(0, self._render_scan)

        threading.Thread(target=run, daemon=True).start()

    def _on_scan_error(self, msg: str):
        self.is_scanning = False
        self.btn_scan.configure(state="normal")
        self.lbl_status.configure(text="Scan failed.")
        messagebox.showerror("Scan Failed", f"Could not read the Rekordbox database:\n{msg}")

    def _render_scan(self):
        self.is_scanning = False
        self.btn_scan.configure(state="normal")
        if not self.summary or not self.summary.has_export_pdb:
            messagebox.showerror("Error", "No PIONEER/rekordbox/export.pdb found on this drive.")
            self.lbl_status.configure(text="Scan failed: export.pdb missing.")
            return

        for i, task in enumerate(self.summary.tasks):
            t = task.track
            spec = f"{t.sample_rate / 1000:g} kHz / {t.sample_depth}-bit"
            self.tree.insert(
                "",
                "end",
                values=(t.id, t.title or t.filename, t.extension.upper(), spec, task.target_format.value.upper()),
                tags=("odd" if i % 2 else "even",),
            )

        total = self.summary.total_tracks
        incompat = self.summary.incompatible_tracks
        compat = self.summary.compatible_tracks

        self.card_total.val_label.configure(text=str(total))
        self.card_compat.val_label.configure(text=str(compat))
        self.card_incompat.val_label.configure(text=str(incompat))
        self.lbl_track_count.configure(text=f"{incompat} of {total} tracks")

        self.lbl_status.configure(text=f"Scan complete: {incompat} tracks require conversion to {self.format_var.get().upper()}.")

        if incompat > 0:
            self.btn_convert.configure(state="normal")
        else:
            self.btn_convert.configure(state="disabled")
            messagebox.showinfo("All Compatible", "All tracks are compatible with the selected profile.")

    # ------------------------------------------------------------------ convert

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
        self.btn_restore.configure(state="disabled")
        self.progress_bar.set(0)

        def run():
            try:
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
            except Exception as e:
                self.after(0, lambda: self._on_conversion_error(str(e)))
                return
            self.after(0, lambda: self._on_finish(result))

        threading.Thread(target=run, daemon=True).start()

    def _update_prog(self, pct: float, name: str):
        self.progress_bar.set(pct)
        self.lbl_status.configure(text=f"Converting: {name[:45]}...")

    def _on_conversion_error(self, msg: str):
        self.is_converting = False
        self.btn_scan.configure(state="normal")
        self.btn_restore.configure(state="normal")
        self.lbl_status.configure(text="Conversion failed.")
        messagebox.showerror("Conversion Failed", f"An unexpected error occurred:\n{msg}")

    def _on_finish(self, result: dict):
        self.is_converting = False
        self.btn_scan.configure(state="normal")
        self.btn_restore.configure(state="normal")
        self.progress_bar.set(1.0)

        if result.get("success"):
            self.lbl_status.configure(text=f"Conversion complete: {result.get('completed', 0)} tracks converted.")
            cleaned = result.get("cleaned_dotfiles", 0)
            dot_msg = f"\n- Cleaned {cleaned} macOS ghost files (._*)" if cleaned else ""
            messagebox.showinfo(
                "Conversion Complete",
                f"Successfully converted {result['completed']} tracks.\n\n"
                f"- Database export.pdb patched and synced.\n"
                f"- Waveforms and beatgrids intact.{dot_msg}\n"
                f"- USB is ready for Pioneer CDJ/XDJ booth.",
            )
        else:
            completed = result.get("completed", 0)
            failed = result.get("failed", 0)
            self.lbl_status.configure(text=f"Conversion finished with errors: {completed} converted, {failed} failed.")
            errors = [t.error for t in (self.summary.tasks if self.summary else []) if t.error]
            detail = f"\n\nFirst error:\n{errors[0]}" if errors else ""
            messagebox.showwarning(
                "Completed with Errors",
                f"Converted: {completed}\nFailed: {failed}{detail}",
            )
        self._start_scan()

    # ------------------------------------------------------------------ restore

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
