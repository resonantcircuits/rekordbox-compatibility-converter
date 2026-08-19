"""Main engine orchestrating scan, plan, parallel conversion, dotfile cleanup, and database synchronization."""

import hashlib
import os
import platform
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Tuple

from .anlz_manager import ANLZManager
from .audio_converter import AudioConverter
from .dlp_manager import (
    DLPManager,
    ONELIBRARY_ONLY_MESSAGE,
    ONELIBRARY_PRESENT_MESSAGE,
    ONELIBRARY_REBUILD_REQUIRED_MESSAGE,
)
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

    @staticmethod
    def _is_within(root: Path, candidate: Path) -> bool:
        """Returns whether candidate resolves inside root, including symlinks."""
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve(strict=False)
        return resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents

    @staticmethod
    def _path_key(path: Path) -> str:
        """Uses a conservative case-insensitive key suitable for common USB filesystems."""
        return str(path.resolve(strict=False)).casefold()

    @staticmethod
    def _restore_file_bytes(path: Path, data: bytes) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.restore.tmp")
        try:
            with open(temp, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name != "posix":
            return
        try:
            directory_fd = os.open(str(path), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

    @classmethod
    def _backup_file(cls, source: Path) -> Path:
        destination = source.with_suffix(source.suffix + ".bak")
        temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temp)
            os.replace(temp, destination)
            cls._sync_directory(destination.parent)
        finally:
            temp.unlink(missing_ok=True)
        return destination

    def scan(
        self,
        usb_root: Path,
        profile: Optional[HardwareProfile] = None,
        forced_target_format: Optional[TargetFormat] = None,
        forced_sample_rate: Optional[int] = None,
        forced_sample_depth: Optional[int] = None,
        allow_onelibrary_bridge: bool = False,
    ) -> ScanSummary:
        """Scans a Rekordbox USB drive and builds an actionable conversion plan."""
        usb_root = Path(usb_root).resolve()
        profile = profile or get_profile()

        pdb_path = usb_root / "PIONEER" / "rekordbox" / "export.pdb"
        ext_pdb_path = usb_root / "PIONEER" / "rekordbox" / "exportExt.pdb"
        dlp_path = usb_root / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db"
        if not dlp_path.is_file():
            dlp_path = usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db"

        unsafe_pdb = pdb_path.is_file() and not self._is_within(usb_root, pdb_path)
        has_pdb = pdb_path.is_file() and not unsafe_pdb
        has_ext_pdb = ext_pdb_path.is_file()
        has_dlp = dlp_path.is_file()

        summary = ScanSummary(
            usb_root=usb_root,
            has_export_pdb=has_pdb,
            has_export_ext_pdb=has_ext_pdb,
            has_dlp=has_dlp,
        )
        if unsafe_pdb:
            summary.unsupported_reason = "export.pdb resolves outside the selected USB root."
        elif has_dlp:
            if not has_pdb:
                summary.unsupported_reason = ONELIBRARY_ONLY_MESSAGE
            elif allow_onelibrary_bridge:
                summary.onelibrary_bridge_mode = True
            else:
                summary.unsupported_reason = ONELIBRARY_PRESENT_MESSAGE

        try:
            stat = shutil.disk_usage(usb_root)
            summary.free_space_bytes = stat.free
        except Exception:
            summary.free_space_bytes = 0

        if not has_pdb or (has_dlp and not summary.onelibrary_bridge_mode):
            return summary

        pdb_manager = PDBManager(pdb_path)
        summary.pdb_sha256 = hashlib.sha256(pdb_manager.data).hexdigest()
        summary.total_tracks = len(pdb_manager.tracks)

        for track in pdb_manager.tracks:
            ext = track.extension.lower()
            summary.format_counts[ext] = summary.format_counts.get(ext, 0) + 1

            rel_usb_path = track.file_path.lstrip("/")
            source_abs = usb_root / rel_usb_path
            if (
                ext in {"m4a", "mp4"}
                and self._is_within(usb_root, source_abs)
                and source_abs.is_file()
            ):
                probe = self.audio_converter.probe(source_abs)
                if not probe.get("probe_error"):
                    track.codec_name = str(probe.get("codec_name") or "")
                    track.channels = int(probe.get("channels") or 2)

            check = profile.evaluate(track)
            if check.is_compatible:
                summary.compatible_tracks += 1
            else:
                summary.incompatible_tracks += 1

                target_fmt = forced_target_format or check.suggested_target_format
                target_sr = forced_sample_rate or check.suggested_sample_rate
                target_sd = forced_sample_depth or check.suggested_sample_depth
                if target_fmt == TargetFormat.MP3:
                    target_sd = 16

                filename_path = PurePosixPath(track.filename)
                stem = filename_path.stem if filename_path.suffix else filename_path.name
                file_path = PurePosixPath(track.file_path)

                # `.aif` and `.aiff` contain the same AIFF data. Prefer the
                # familiar `.aiff`, but fall back to `.aif` when the DeviceSQL
                # row has only enough in-place string space for a 3-letter
                # extension (for example, `.wav` or `.m4a` -> `.aif`).
                target_extensions = [target_fmt.value]
                if target_fmt == TargetFormat.AIFF:
                    target_extensions.append("aif")

                new_filename = f"{stem}.{target_extensions[0]}"
                new_usb_path = str(file_path.parent / new_filename)
                for candidate_ext in target_extensions:
                    candidate_filename = f"{stem}.{candidate_ext}"
                    candidate_usb_path = str(file_path.parent / candidate_filename)
                    if pdb_manager.can_fit_strings(
                        track,
                        candidate_filename,
                        candidate_usb_path,
                    ):
                        new_filename = candidate_filename
                        new_usb_path = candidate_usb_path
                        break

                target_abs = usb_root / new_usb_path.lstrip("/")

                anlz_dat = None
                anlz_ext = None
                if track.analyze_path:
                    anlz_rel = track.analyze_path.lstrip("/")
                    anlz_dat_candidate = usb_root / anlz_rel
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
                if track.duration > 0:
                    if target_fmt == TargetFormat.MP3:
                        estimate = int(track.duration * 320000 / 8)
                    else:
                        estimate = int(track.duration * target_sr * 2 * target_sd / 8)
                    summary.estimated_extra_bytes += estimate

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
            if search_dir.is_dir() and self._is_within(usb_root, search_dir):
                for root, _dirs, files in os.walk(search_dir):
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

        if not pdb_bak.is_file():
            return False, "No export.pdb.bak found to restore."
        if not self._is_within(usb_root, pdb_bak) or not self._is_within(usb_root, pdb_dest):
            return False, "Database backup or destination resolves outside the selected USB root."

        try:
            backup_manager = PDBManager(pdb_bak)
            missing_or_changed = []
            for track in backup_manager.tracks:
                audio_path = usb_root / track.file_path.lstrip("/")
                if not self._is_within(usb_root, audio_path):
                    return False, f"Backup contains an unsafe audio path: {track.file_path}"
                if not audio_path.is_file():
                    missing_or_changed.append(track.file_path)
                elif track.file_size and audio_path.stat().st_size != track.file_size:
                    missing_or_changed.append(track.file_path)

            if missing_or_changed:
                sample = ", ".join(missing_or_changed[:3])
                return False, (
                    "Backup was not restored because its original audio files are missing or changed: "
                    f"{sample}. Database-only restore would create broken track references."
                )

            # Only restore sidecar backups referenced by this database backup.
            # Unrelated .bak files from older runs must never be applied.
            restore_entries: List[Tuple[Path, bytes]] = []
            seen_sidecars: Dict[Path, str] = {}
            for track in backup_manager.tracks:
                if not track.analyze_path:
                    continue
                dat_path = usb_root / track.analyze_path.lstrip("/")
                if not self._is_within(usb_root, dat_path):
                    return False, f"Backup contains an unsafe ANLZ path: {track.analyze_path}"
                for sidecar in (dat_path, dat_path.with_suffix(".EXT")):
                    expected_path = seen_sidecars.get(sidecar)
                    if sidecar in seen_sidecars and expected_path != track.file_path:
                        return False, f"Backup assigns conflicting tracks to ANLZ file {sidecar}."
                    if sidecar in seen_sidecars:
                        continue
                    seen_sidecars[sidecar] = track.file_path
                    current_path = ANLZManager.read_path(sidecar)
                    if current_path == track.file_path:
                        continue
                    sidecar_backup = sidecar.with_suffix(sidecar.suffix + ".bak")
                    if sidecar_backup.is_file() and ANLZManager.read_path(sidecar_backup) == track.file_path:
                        restore_entries.append((sidecar, sidecar_backup.read_bytes()))
                    elif sidecar.is_file() or sidecar_backup.is_file():
                        return False, (
                            f"Cannot safely restore {sidecar}: no matching backup references "
                            f"{track.file_path}."
                        )

            # Apply sidecars first and the database last. If anything fails,
            # restore every destination to its pre-restore bytes.
            restore_entries.append((pdb_dest, pdb_bak.read_bytes()))
            snapshots: Dict[Path, Optional[bytes]] = {
                destination: destination.read_bytes() if destination.is_file() else None
                for destination, _ in restore_entries
            }
            applied: List[Path] = []
            try:
                for destination, restored_data in restore_entries:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._restore_file_bytes(destination, restored_data)
                    self._sync_directory(destination.parent)
                    applied.append(destination)
            except Exception as restore_exc:
                rollback_errors = []
                for destination in reversed(applied):
                    try:
                        previous_data = snapshots[destination]
                        if previous_data is None:
                            destination.unlink(missing_ok=True)
                        else:
                            self._restore_file_bytes(destination, previous_data)
                        self._sync_directory(destination.parent)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"{destination}: {rollback_exc}")
                detail = (
                    " Rollback also failed for: " + "; ".join(rollback_errors)
                    if rollback_errors
                    else ""
                )
                return False, f"Restore failed and prior files were rolled back: {restore_exc}.{detail}"

            return True, "Database and matching analysis files restored; referenced audio files were verified first."
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
                return False, "On Windows, use 'Safely Remove Hardware' from the system tray."
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
        allow_onelibrary_bridge: bool = False,
    ) -> dict:
        """Executes staged conversions and commits each track before deleting its source."""
        pdb_path = summary.usb_root / "PIONEER" / "rekordbox" / "export.pdb"
        if not pdb_path.is_file():
            return {"success": False, "error": "export.pdb not found."}
        if not self._is_within(summary.usb_root, pdb_path):
            return {"success": False, "error": "export.pdb resolves outside the selected USB root."}

        if summary.has_dlp and not (
            allow_onelibrary_bridge and summary.onelibrary_bridge_mode
        ):
            dlp_paths = [
                summary.usb_root / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db",
                summary.usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db",
            ]
            dlp_path = next((path for path in dlp_paths if path.exists()), dlp_paths[0])
            _, message = DLPManager(dlp_path).check_status()
            return {
                "success": False,
                "error": message,
                "total": len(summary.tasks),
                "completed": 0,
                "failed": len(summary.tasks),
            }

        if summary.onelibrary_bridge_mode:
            if delete_original:
                return {
                    "success": False,
                    "error": (
                        "The experimental OneLibrary bridge requires retaining all original "
                        "audio files until Rekordbox has rebuilt and verified OneLibrary."
                    ),
                    "total": len(summary.tasks),
                    "completed": 0,
                    "failed": len(summary.tasks),
                }
            if not backup:
                return {
                    "success": False,
                    "error": "The experimental OneLibrary bridge requires database backups.",
                    "total": len(summary.tasks),
                    "completed": 0,
                    "failed": len(summary.tasks),
                }

        try:
            pdb_manager = PDBManager(pdb_path)
        except Exception as exc:
            return {"success": False, "error": f"Failed to parse export.pdb: {exc}"}

        current_pdb_sha256 = hashlib.sha256(pdb_manager.data).hexdigest()
        if not summary.pdb_sha256 or current_pdb_sha256 != summary.pdb_sha256:
            return {
                "success": False,
                "error": "export.pdb changed after the scan. Scan the drive again before converting.",
                "total": len(summary.tasks),
                "completed": 0,
                "failed": len(summary.tasks),
            }

        total_tasks = len(summary.tasks)
        preflight_errors: List[str] = []
        target_owners: Dict[str, ConversionTask] = {}

        for task in summary.tasks:
            task.status = "pending"
            task.error = None
            source = task.source_abs_path
            target = task.target_abs_path
            source_key = self._path_key(source)
            target_key = self._path_key(target)

            if not self._is_within(summary.usb_root, source):
                task.error = f"Unsafe source path escapes the USB root: {task.track.file_path}"
            elif not self._is_within(summary.usb_root, target):
                task.error = f"Unsafe target path escapes the USB root: {task.target_usb_path}"
            elif task.anlz_dat_path and not self._is_within(summary.usb_root, task.anlz_dat_path):
                task.error = f"Unsafe ANLZ path escapes the USB root: {task.track.analyze_path}"
            elif task.anlz_ext_path and not self._is_within(summary.usb_root, task.anlz_ext_path):
                task.error = f"Unsafe ANLZ path escapes the USB root: {task.track.analyze_path}"
            elif not source.is_file():
                task.error = f"Source file not found or not a regular file: {source}"
            elif PurePosixPath(task.track.file_path).name != task.track.filename:
                task.error = (
                    "Database filename and file path disagree: "
                    f"'{task.track.filename}' vs '{task.track.file_path}'"
                )
            elif task.track.file_size and source.stat().st_size != task.track.file_size:
                task.error = (
                    f"Source file changed after export or scan: {source} "
                    f"(database {task.track.file_size} bytes, disk {source.stat().st_size} bytes)"
                )
            elif task.target_sample_rate <= 0:
                task.error = f"Invalid target sample rate: {task.target_sample_rate}"
            elif task.target_format != TargetFormat.MP3 and task.target_sample_depth not in {16, 24}:
                task.error = f"Unsupported target PCM depth: {task.target_sample_depth}-bit"
            elif source_key == target_key and not delete_original:
                task.error = (
                    "Cannot keep the original when conversion must replace the same WAV/AIFF path. "
                    "Choose a different target format or allow replacement."
                )
            elif source_key != target_key and target.exists():
                task.error = f"Refusing to overwrite existing target: {target}"
            elif target_key in target_owners:
                other = target_owners[target_key]
                task.error = (
                    f"Target collision with track {other.track.id}: both map to {task.target_usb_path}"
                )
                if not other.error:
                    other.error = task.error
                    preflight_errors.append(other.error)
            elif not pdb_manager.can_fit_strings(task.track, task.target_filename, task.target_usb_path):
                task.error = (
                    f"Cannot patch export.pdb: '{task.target_filename}' does not fit the existing "
                    "DeviceSQL string allocation. Re-export from Rekordbox or choose a shorter extension."
                )
            elif (
                task.track.analyze_path
                and task.track.file_path != task.target_usb_path
                and (not task.anlz_dat_path or not task.anlz_dat_path.is_file())
            ):
                task.error = f"Required ANLZ .DAT file is missing: {task.track.analyze_path}"
            elif (
                task.track.file_path != task.target_usb_path
                and task.anlz_dat_path
                and ANLZManager.read_path(task.anlz_dat_path) != task.track.file_path
            ):
                task.error = (
                    f"ANLZ .DAT does not reference this track's current path: "
                    f"{task.track.analyze_path}"
                )
            elif (
                task.track.file_path != task.target_usb_path
                and task.anlz_ext_path
                and ANLZManager.read_path(task.anlz_ext_path) != task.track.file_path
            ):
                task.error = f"ANLZ .EXT does not reference this track's current path: {task.anlz_ext_path}"

            target_owners[target_key] = task
            if task.error:
                task.status = "failed"
                preflight_errors.append(task.error)

        if preflight_errors:
            return {
                "success": False,
                "error": "Preflight checks failed; no files were converted.",
                "preflight_errors": preflight_errors,
                "total": total_tasks,
                "completed": 0,
                "failed": len([task for task in summary.tasks if task.error]),
                "cleaned_dotfiles": 0,
            }

        # Parallel workers can have every converted output staged at once, so
        # deletion of originals cannot be counted as space available up front.
        required_space = 0
        for task in summary.tasks:
            if task.track.duration > 0:
                if task.target_format == TargetFormat.MP3:
                    output_estimate = int(task.track.duration * 320000 / 8)
                else:
                    output_estimate = int(
                        task.track.duration
                        * task.target_sample_rate
                        * max(1, task.track.channels)
                        * task.target_sample_depth
                        / 8
                    )
            else:
                # A conservative fallback for missing duration metadata.
                output_estimate = max(task.source_abs_path.stat().st_size * 3, 1024 * 1024)
            required_space += int(output_estimate * 1.05) + 1024 * 1024

        sidecar_bytes = sum(
            path.stat().st_size
            for path in {
                sidecar
                for task in summary.tasks
                for sidecar in (task.anlz_dat_path, task.anlz_ext_path)
                if sidecar and sidecar.is_file()
            }
        )
        # Atomic database and sidecar rewrites need temporary copies even when
        # persistent .bak creation is disabled.
        required_space += pdb_path.stat().st_size + sidecar_bytes
        if backup:
            required_space += pdb_path.stat().st_size + sidecar_bytes

        try:
            free_space = shutil.disk_usage(summary.usb_root).free
        except OSError:
            free_space = None
        if free_space is not None and required_space > free_space:
            return {
                "success": False,
                "error": (
                    "Insufficient free space for safe staging: approximately "
                    f"{required_space / (1024 ** 2):.1f} MiB required, "
                    f"{free_space / (1024 ** 2):.1f} MiB available."
                ),
                "total": total_tasks,
                "completed": 0,
                "failed": total_tasks,
                "cleaned_dotfiles": 0,
            }

        # Initial backup
        if backup:
            try:
                pdb_manager.save(backup=True)
                backed_up_paths = set()
                for task in summary.tasks:
                    if task.track.file_path == task.target_usb_path:
                        continue
                    for anlz_path in (task.anlz_dat_path, task.anlz_ext_path):
                        if anlz_path and anlz_path not in backed_up_paths:
                            self._backup_file(anlz_path)
                            backed_up_paths.add(anlz_path)
            except Exception as exc:
                return {"success": False, "error": f"Could not create required backups: {exc}"}

        completed = 0
        failed = 0
        anlz_updated = 0
        warnings: List[str] = []
        staged_paths: Dict[int, Path] = {}

        def convert_to_stage(task: ConversionTask):
            task.status = "converting"
            stage = task.target_abs_path.with_name(
                f".{task.target_abs_path.stem}.rbconvert-{uuid.uuid4().hex}{task.target_abs_path.suffix}"
            )
            staged_paths[id(task)] = stage
            success, new_size, err = self.audio_converter.convert(
                source_path=task.source_abs_path,
                target_path=stage,
                target_format=task.target_format,
                sample_rate=task.target_sample_rate,
                sample_depth=task.target_sample_depth,
            )
            if not success:
                task.status = "failed"
                task.error = err
                return False
            task.new_file_size = new_size
            task.output_probe = self.audio_converter.probe(stage)
            if task.output_probe.get("probe_error"):
                task.status = "failed"
                task.error = f"Staged output failed verification: {task.output_probe['probe_error']}"
                stage.unlink(missing_ok=True)
                return False
            return True

        def commit_task(task: ConversionTask) -> bool:
            nonlocal anlz_updated
            stage = staged_paths[id(task)]
            source = task.source_abs_path
            target = task.target_abs_path
            same_path = self._path_key(source) == self._path_key(target)
            source_backup: Optional[Path] = None
            pdb_snapshot = bytes(pdb_manager.data)
            seq_snapshot = pdb_manager.seq_db
            track_snapshot = {
                "filename": task.track.filename,
                "file_path": task.track.file_path,
                "file_size": task.track.file_size,
                "sample_rate": task.track.sample_rate,
                "sample_depth": task.track.sample_depth,
                "bitrate": task.track.bitrate,
            }
            anlz_snapshots: Dict[Path, bytes] = {}
            updated_anlz_paths = 0
            database_committed = False

            try:
                if pdb_path.read_bytes() != bytes(pdb_manager.data):
                    raise RuntimeError(
                        "export.pdb changed while conversion was running; the track was not committed"
                    )
                if not self._is_within(summary.usb_root, source) or not self._is_within(
                    summary.usb_root, target
                ):
                    raise RuntimeError("A source or target path became unsafe during conversion")
                if not same_path and target.exists():
                    raise RuntimeError(f"Target appeared during conversion; refusing to overwrite: {target}")
                if track_snapshot["file_path"] != task.target_usb_path:
                    for anlz_path in (task.anlz_dat_path, task.anlz_ext_path):
                        if anlz_path and ANLZManager.read_path(anlz_path) != track_snapshot["file_path"]:
                            raise RuntimeError(
                                f"ANLZ file changed while conversion was running: {anlz_path}"
                            )
                target.parent.mkdir(parents=True, exist_ok=True)
                if same_path:
                    source_backup = source.with_name(
                        f".{source.name}.rbconvert-original-{uuid.uuid4().hex}"
                    )
                    os.replace(source, source_backup)
                os.replace(stage, target)
                self._sync_directory(target.parent)

                actual_rate = int(task.output_probe.get("sample_rate") or task.target_sample_rate)
                if task.target_format == TargetFormat.MP3:
                    actual_depth = 16
                    new_bitrate = 320000
                else:
                    actual_depth = int(
                        task.output_probe.get("bits_per_sample") or task.target_sample_depth
                    )
                    channels = int(task.output_probe.get("channels") or 2)
                    new_bitrate = actual_rate * channels * actual_depth

                if not pdb_manager.update_track(
                    track=task.track,
                    new_filename=task.target_filename,
                    new_filepath=task.target_usb_path,
                    new_filesize=task.new_file_size,
                    new_sample_rate=actual_rate,
                    new_sample_depth=actual_depth,
                    new_bitrate=new_bitrate,
                ):
                    raise RuntimeError("export.pdb patch rejected the replacement strings")

                # The database is made durable while the original path still
                # exists. Only after all required sidecars are synchronized is
                # the original removed.
                pdb_manager.save(backup=False)
                database_committed = True

                if track_snapshot["file_path"] != task.target_usb_path:
                    for anlz_path in (task.anlz_dat_path, task.anlz_ext_path):
                        if not anlz_path:
                            continue
                        if not anlz_path.is_file():
                            raise RuntimeError(f"ANLZ file disappeared during conversion: {anlz_path}")
                        anlz_snapshots[anlz_path] = anlz_path.read_bytes()
                        if not ANLZManager.update_path(
                            anlz_path, task.target_usb_path, backup=False
                        ):
                            raise RuntimeError(f"Could not update PPTH path in {anlz_path}")
                        updated_anlz_paths += 1

                if delete_original:
                    obsolete = source_backup if same_path else source
                    if obsolete:
                        try:
                            obsolete.unlink(missing_ok=True)
                            self._sync_directory(obsolete.parent)
                        except OSError as exc:
                            warning = f"Converted track {task.track.id}, but could not remove {obsolete}: {exc}"
                            task.warnings.append(warning)
                            warnings.append(warning)
                elif source_backup:
                    raise RuntimeError("Internal error: same-path conversion cannot preserve its original")

                task.status = "completed"
                anlz_updated += updated_anlz_paths
                return True
            except Exception as exc:
                task.status = "failed"
                task.error = str(exc)

                for anlz_path, original_data in anlz_snapshots.items():
                    try:
                        self._restore_file_bytes(anlz_path, original_data)
                    except Exception as rollback_exc:
                        warnings.append(f"Could not roll back {anlz_path}: {rollback_exc}")

                pdb_manager.data = bytearray(pdb_snapshot)
                pdb_manager.seq_db = seq_snapshot
                for field_name, value in track_snapshot.items():
                    setattr(task.track, field_name, value)

                rollback_saved = not database_committed
                if database_committed:
                    try:
                        pdb_manager.save(backup=False)
                        rollback_saved = True
                    except Exception as rollback_exc:
                        warnings.append(f"Could not roll back export.pdb: {rollback_exc}")

                if rollback_saved:
                    try:
                        if same_path and source_backup and source_backup.exists():
                            target.unlink(missing_ok=True)
                            os.replace(source_backup, source)
                        elif not same_path:
                            target.unlink(missing_ok=True)
                    except Exception as rollback_exc:
                        warnings.append(f"Could not remove staged target during rollback: {rollback_exc}")
                return False
            finally:
                try:
                    stage.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    warnings.append(f"Could not remove staging file {stage}: {cleanup_exc}")

        max_workers = max(1, min(threads, total_tasks if total_tasks > 0 else 1))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {
                executor.submit(convert_to_stage, task): task for task in summary.tasks
            }

            done_count = 0
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    converted = future.result()
                    is_ok = converted and commit_task(task)
                except Exception as exc:
                    task.status = "failed"
                    task.error = f"Unexpected conversion error: {exc}"
                    is_ok = False
                if is_ok:
                    completed += 1
                else:
                    failed += 1

                done_count += 1
                if progress_callback:
                    try:
                        progress_callback(task, done_count, total_tasks)
                    except Exception as exc:
                        warnings.append(f"Progress callback failed: {exc}")

        # Clean AppleDouble ghost files
        cleaned_files = 0
        if clean_dotfiles:
            cleaned_files = self.clean_dotfiles(summary.usb_root)

        if summary.onelibrary_bridge_mode and completed:
            warnings.insert(0, ONELIBRARY_REBUILD_REQUIRED_MESSAGE)

        return {
            "success": failed == 0,
            "total": total_tasks,
            "completed": completed,
            "failed": failed,
            "anlz_updated": anlz_updated,
            "cleaned_dotfiles": cleaned_files,
            "warnings": warnings,
            "onelibrary_sync_required": bool(
                summary.onelibrary_bridge_mode and completed
            ),
        }
