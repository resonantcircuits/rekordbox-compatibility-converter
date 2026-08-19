"""Main engine orchestrating scan, plan, parallel conversion, dotfile cleanup, and database synchronization."""

import os
import platform
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .anlz_manager import ANLZManager
from .audio_converter import AudioConverter
from .dlp_manager import DLPManager
from .models import (
    ConversionTask,
    ScanSummary,
    TargetFormat,
    TrackInfo,
)
from .pdb_manager import PDBManager
from .profiles import HardwareProfile, get_profile


class ConversionEngine:
    """Coordinates USB scanning, parallel audio conversion, ANLZ patching, and database updates."""

    def __init__(self, audio_converter: Optional[AudioConverter] = None):
        self.audio_converter = audio_converter or AudioConverter()

    def scan(
        self,
        usb_root: Path,
        profile: Optional[HardwareProfile] = None,
        forced_target_format: Optional[TargetFormat] = None,
        forced_sample_rate: Optional[int] = None,
        forced_sample_depth: Optional[int] = None,
    ) -> ScanSummary:
        """Scans a Rekordbox USB drive and builds an actionable conversion plan."""
        usb_root = Path(usb_root).resolve()
        profile = profile or get_profile()

        pdb_path = usb_root / "PIONEER" / "rekordbox" / "export.pdb"
        ext_pdb_path = usb_root / "PIONEER" / "rekordbox" / "exportExt.pdb"
        dlp_path = usb_root / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
        if not dlp_path.exists():
            dlp_path = usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db"

        has_pdb = pdb_path.exists()
        has_ext_pdb = ext_pdb_path.exists()
        has_dlp = dlp_path.exists()

        summary = ScanSummary(
            usb_root=usb_root,
            has_export_pdb=has_pdb,
            has_export_ext_pdb=has_ext_pdb,
            has_dlp=has_dlp,
        )

        try:
            stat = shutil.disk_usage(usb_root)
            summary.free_space_bytes = stat.free
        except Exception:
            summary.free_space_bytes = 0

        if not has_pdb:
            return summary

        pdb_manager = PDBManager(pdb_path)
        summary.total_tracks = len(pdb_manager.tracks)

        for track in pdb_manager.tracks:
            ext = track.extension.lower()
            summary.format_counts[ext] = summary.format_counts.get(ext, 0) + 1

            check = profile.evaluate(track)
            if check.is_compatible:
                summary.compatible_tracks += 1
            else:
                summary.incompatible_tracks += 1

                target_fmt = forced_target_format or check.suggested_target_format
                target_sr = forced_sample_rate or check.suggested_sample_rate
                target_sd = forced_sample_depth or check.suggested_sample_depth

                rel_usb_path = track.file_path.lstrip("/")
                source_abs = usb_root / rel_usb_path

                new_ext = target_fmt.value
                if "." in track.filename:
                    new_filename = track.filename.rsplit(".", 1)[0] + f".{new_ext}"
                else:
                    new_filename = f"{track.filename}.{new_ext}"

                if "." in track.file_path:
                    new_usb_path = track.file_path.rsplit(".", 1)[0] + f".{new_ext}"
                else:
                    new_usb_path = f"{track.file_path}.{new_ext}"

                target_abs = usb_root / new_usb_path.lstrip("/")

                anlz_dat = None
                anlz_ext = None
                if track.analyze_path:
                    anlz_rel = track.analyze_path.lstrip("/")
                    anlz_dat_candidate = usb_root / anlz_rel
                    if anlz_dat_candidate.exists():
                        anlz_dat = anlz_dat_candidate
                        anlz_ext_candidate = anlz_dat_candidate.with_suffix(".EXT")
                        if anlz_ext_candidate.exists():
                            anlz_ext = anlz_ext_candidate

                task = ConversionTask(
                    track=track,
                    source_abs_path=source_abs,
                    target_abs_path=target_abs,
                    target_usb_path=new_usb_path,
                    target_filename=new_filename,
                    target_format=target_fmt,
                    target_sample_rate=target_sr,
                    target_sample_depth=target_sd,
                    anlz_dat_path=anlz_dat,
                    anlz_ext_path=anlz_ext,
                )
                summary.tasks.append(task)

        return summary

    def clean_dotfiles(self, usb_root: Path) -> int:
        """Removes hidden AppleDouble (._*) and .DS_Store ghost files from the USB drive.

        Returns the number of files cleaned.
        """
        usb_root = Path(usb_root).resolve()
        count = 0

        # Run macOS dot_clean if on Darwin
        if platform.system() == "Darwin" and shutil.which("dot_clean"):
            try:
                subprocess.run(["dot_clean", "-m", str(usb_root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

        # Manually remove any remaining ._* and .DS_Store files across Contents and PIONEER
        for search_dir in [usb_root / "Contents", usb_root / "PIONEER"]:
            if search_dir.exists():
                for root, dirs, files in os.walk(search_dir):
                    for f in files:
                        if f.startswith("._") or f == ".DS_Store":
                            p = Path(root) / f
                            try:
                                p.unlink(missing_ok=True)
                                count += 1
                            except Exception:
                                pass
        return count

    def restore_backup(self, usb_root: Path) -> Tuple[bool, str]:
        """Restores export.pdb and ANLZ files from their .bak backups."""
        usb_root = Path(usb_root).resolve()
        pdb_bak = usb_root / "PIONEER" / "rekordbox" / "export.pdb.bak"
        pdb_dest = usb_root / "PIONEER" / "rekordbox" / "export.pdb"

        if not pdb_bak.exists():
            return False, "No export.pdb.bak found to restore."

        try:
            shutil.copy2(pdb_bak, pdb_dest)

            # Restore ANLZ files
            anlz_base = usb_root / "PIONEER" / "USBANLZ"
            if anlz_base.exists():
                for root, _, files in os.walk(anlz_base):
                    for f in files:
                        if f.endswith(".bak"):
                            bak_file = Path(root) / f
                            orig_file = bak_file.with_suffix("")
                            shutil.copy2(bak_file, orig_file)

            return True, "Database and analysis files restored successfully from backup."
        except Exception as e:
            return False, f"Restore failed: {e}"

    def eject_drive(self, usb_root: Path) -> Tuple[bool, str]:
        """Safely unmounts and ejects the USB drive."""
        usb_root = Path(usb_root).resolve()
        os_type = platform.system()

        try:
            if os_type == "Darwin":
                res = subprocess.run(["diskutil", "unmountDisk", str(usb_root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode == 0:
                    return True, f"Drive {usb_root.name} safely unmounted."
                return False, res.stderr.strip()
            elif os_type == "Windows":
                return True, "On Windows, please use 'Safely Remove Hardware' from the system tray."
            else:
                res = subprocess.run(["umount", str(usb_root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode == 0:
                    return True, f"Drive {usb_root.name} safely unmounted."
                return False, res.stderr.strip()
        except Exception as e:
            return False, f"Eject failed: {e}"

    def execute(
        self,
        summary: ScanSummary,
        delete_original: bool = True,
        backup: bool = True,
        threads: int = 4,
        clean_dotfiles: bool = True,
        progress_callback: Optional[Callable[[ConversionTask, int, int], None]] = None,
    ) -> dict:
        """Executes the conversion plan on the USB drive with multi-threaded audio conversion."""
        pdb_path = summary.usb_root / "PIONEER" / "rekordbox" / "export.pdb"
        if not pdb_path.exists():
            return {"success": False, "error": "export.pdb not found."}

        pdb_manager = PDBManager(pdb_path)
        dlp_manager = DLPManager(summary.usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db")

        # Initial backup
        if backup:
            pdb_manager.save(backup=True)

        total_tasks = len(summary.tasks)
        completed = 0
        failed = 0
        db_lock = threading.Lock()

        def process_task(task_item: Tuple[int, ConversionTask]):
            idx, task = task_item
            task.status = "converting"

            if not task.source_abs_path.exists():
                task.status = "failed"
                task.error = f"Source file not found: {task.source_abs_path}"
                return False

            # Step 1: Audio conversion via FFmpeg
            success, new_size, err = self.audio_converter.convert(
                source_path=task.source_abs_path,
                target_path=task.target_abs_path,
                target_format=task.target_format,
                sample_rate=task.target_sample_rate,
                sample_depth=task.target_sample_depth,
            )

            if not success:
                task.status = "failed"
                task.error = err
                return False

            task.new_file_size = new_size

            # Step 2: Update ANLZ files
            if task.anlz_dat_path and task.anlz_dat_path.exists():
                ANLZManager.update_path(task.anlz_dat_path, task.target_usb_path, backup=backup)
            if task.anlz_ext_path and task.anlz_ext_path.exists():
                ANLZManager.update_path(task.anlz_ext_path, task.target_usb_path, backup=backup)

            # Step 3: Thread-safe database synchronization
            if task.target_format in (TargetFormat.AIFF, TargetFormat.WAV):
                new_bitrate = task.target_sample_rate * 2 * task.target_sample_depth
            else:
                new_bitrate = 320000

            with db_lock:
                pdb_manager.update_track(
                    track=task.track,
                    new_filename=task.target_filename,
                    new_filepath=task.target_usb_path,
                    new_filesize=new_size,
                    new_sample_rate=task.target_sample_rate,
                    new_sample_depth=task.target_sample_depth,
                    new_bitrate=new_bitrate,
                )

                if summary.has_dlp:
                    dlp_manager.update_track_path(
                        old_usb_path=task.track.file_path,
                        new_usb_path=task.target_usb_path,
                        new_filename=task.target_filename,
                        new_filesize=new_size,
                        new_bitrate=new_bitrate,
                        new_sample_rate=task.target_sample_rate,
                        new_bit_depth=task.target_sample_depth,
                        backup=backup,
                    )

            # Step 4: Delete original file (Option A)
            if delete_original and task.source_abs_path != task.target_abs_path:
                try:
                    task.source_abs_path.unlink(missing_ok=True)
                except Exception:
                    pass

            task.status = "completed"
            return True

        # Execute tasks in parallel
        max_workers = max(1, min(threads, total_tasks if total_tasks > 0 else 1))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {
                executor.submit(process_task, (i, task)): (i, task)
                for i, task in enumerate(summary.tasks)
            }

            done_count = 0
            for future in as_completed(future_to_task):
                idx, task = future_to_task[future]
                is_ok = future.result()
                if is_ok:
                    completed += 1
                else:
                    failed += 1

                done_count += 1
                if progress_callback:
                    progress_callback(task, done_count, total_tasks)

                # Periodic save
                if done_count % 25 == 0:
                    with db_lock:
                        pdb_manager.save(backup=False)

        # Final commit
        pdb_manager.save(backup=False)

        # Clean AppleDouble ghost files
        cleaned_files = 0
        if clean_dotfiles:
            cleaned_files = self.clean_dotfiles(summary.usb_root)

        return {
            "success": failed == 0,
            "total": total_tasks,
            "completed": completed,
            "failed": failed,
            "cleaned_dotfiles": cleaned_files,
        }
