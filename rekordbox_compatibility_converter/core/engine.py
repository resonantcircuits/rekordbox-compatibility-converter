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
from .local_backup import LocalBackupSession
from .models import (
    AnalysisPathRepair,
    BitrateMetadataRepair,
    ConversionTask,
    OriginalCleanupCandidate,
    OriginalCleanupPlan,
    REKORDBOX_FILE_TYPE_BY_EXTENSION,
    REKORDBOX_FILE_TYPE_BY_TARGET,
    ScanSummary,
    TargetFormat,
    TrackInfo,
)
from .pdb_manager import PDBManager, device_sql_bitrate_kbps
from .profiles import HardwareProfile, get_profile
from .subprocess_utils import run_external

DEFAULT_CONVERSION_THREADS = 2


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
    def _probed_device_sql_bitrate(probe: dict) -> int:
        """Return a probe's expected DeviceSQL bitrate, or zero if unsupported."""
        codec_name = str(probe.get("codec_name") or "")
        if codec_name.startswith("pcm_"):
            rate = int(probe.get("sample_rate") or 0)
            depth = int(probe.get("bits_per_sample") or 0)
            channels = int(probe.get("channels") or 0)
            if rate and depth and channels:
                return device_sql_bitrate_kbps(rate * depth * channels)
        if codec_name == "mp3":
            return device_sql_bitrate_kbps(int(probe.get("bit_rate") or 0))
        return 0

    @staticmethod
    def _existing_owner_matches_task(
        task: ConversionTask, owner: Optional[TrackInfo]
    ) -> Optional[str]:
        """Validate that a referenced target is the same logical Rekordbox track."""
        if owner is None:
            return "the existing target has no Rekordbox database owner"
        expected_type = REKORDBOX_FILE_TYPE_BY_TARGET[task.target_format]
        checks = (
            (owner.id != task.track.id, "the database owner is the same stale row"),
            (owner.file_path == task.target_usb_path, "the owner path differs"),
            (owner.filename == task.target_filename, "the owner filename differs"),
            (
                bool(owner.analyze_path)
                and owner.analyze_path == task.track.analyze_path,
                "the analysis path differs",
            ),
            (owner.title == task.track.title, "the track title differs"),
            (owner.duration == task.track.duration, "the track duration differs"),
            (owner.file_type == expected_type, "the target format metadata differs"),
            (
                owner.sample_rate == task.target_sample_rate,
                "the target sample rate metadata differs",
            ),
            (
                owner.sample_depth == task.target_sample_depth,
                "the target bit depth metadata differs",
            ),
        )
        return next((message for matches, message in checks if not matches), None)

    @staticmethod
    def _existing_target_probe_error(task: ConversionTask, probe: Dict) -> Optional[str]:
        if probe.get("probe_error"):
            return f"the existing target failed audio inspection: {probe['probe_error']}"
        if int(probe.get("sample_rate") or 0) != task.target_sample_rate:
            return "the existing target has the wrong sample rate"
        codec = str(probe.get("codec_name") or "")
        expected_codec = {
            TargetFormat.MP3: "mp3",
            TargetFormat.AIFF: "pcm_s16be" if task.target_sample_depth == 16 else "pcm_s24be",
            TargetFormat.WAV: "pcm_s16le" if task.target_sample_depth == 16 else "pcm_s24le",
        }[task.target_format]
        if codec != expected_codec:
            return f"the existing target codec is {codec or 'unknown'}, not {expected_codec}"
        if task.target_format != TargetFormat.MP3 and int(
            probe.get("bits_per_sample") or 0
        ) != task.target_sample_depth:
            return "the existing target has the wrong PCM bit depth"
        if abs(float(probe.get("duration") or 0) - task.track.duration) > 2:
            return "the existing target duration differs from the database row"
        return None

    @classmethod
    def _plan_analysis_path_repair(
        cls, usb_root: Path, track: TrackInfo, audio_path: Path
    ) -> Optional[AnalysisPathRepair]:
        """Plan an extension-only stale PPTH repair for an existing audio file."""
        if not track.analyze_path or not audio_path.is_file():
            return None
        dat_path = usb_root / track.analyze_path.lstrip("/")
        if not cls._is_within(usb_root, dat_path) or not dat_path.is_file():
            return None
        sidecars = [dat_path]
        sidecars.extend(
            candidate
            for candidate in (dat_path.with_suffix(".EXT"), dat_path.with_suffix(".2EX"))
            if candidate.is_file() and cls._is_within(usb_root, candidate)
        )
        stored_paths = [ANLZManager.read_path(path) for path in sidecars]
        if any(path is None for path in stored_paths):
            return None
        old_paths = {str(path) for path in stored_paths}
        if old_paths == {track.file_path} or len(old_paths) != 1:
            return None
        old_path = next(iter(old_paths))
        old_posix = PurePosixPath(old_path)
        new_posix = PurePosixPath(track.file_path)
        if (
            old_posix.parent != new_posix.parent
            or old_posix.stem != new_posix.stem
            or old_posix.suffix.lower() == new_posix.suffix.lower()
        ):
            return None
        old_absolute = usb_root / old_path.lstrip("/")
        if not cls._is_within(usb_root, old_absolute) or old_absolute.exists():
            return None
        return AnalysisPathRepair(
            track=track,
            old_audio_path=old_path,
            new_audio_path=track.file_path,
            sidecar_paths=sidecars,
        )

    @staticmethod
    def _estimated_output_size(task: ConversionTask) -> int:
        """Return a conservative staged-output estimate for one task."""
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
        elif task.source_abs_path.is_file():
            output_estimate = max(
                task.source_abs_path.stat().st_size * 3,
                1024 * 1024,
            )
        else:
            output_estimate = 1024 * 1024
        return int(output_estimate * 1.05) + 1024 * 1024

    @classmethod
    def estimate_required_space(
        cls,
        summary: ScanSummary,
        backup: bool = True,
        local_original_backup: bool = False,
    ) -> int:
        """Estimates peak USB space for staged outputs and atomic metadata updates."""
        output_bytes = sum(cls._estimated_output_size(task) for task in summary.tasks)
        source_paths = {
            task.source_abs_path.resolve()
            for task in summary.tasks
            if task.source_abs_path.is_file()
        }
        source_bytes = sum(path.stat().st_size for path in source_paths)
        preexisting_target_paths = {
            task.target_abs_path.resolve()
            for task in summary.tasks
            if task.target_abs_path.is_file()
            and task.existing_target_track_id is None
            and cls._path_key(task.target_abs_path) != cls._path_key(task.source_abs_path)
        }
        preexisting_target_bytes = sum(
            path.stat().st_size for path in preexisting_target_paths
        )
        required_space = (
            max(0, output_bytes - source_bytes - preexisting_target_bytes)
            if local_original_backup
            else output_bytes
        )

        sidecar_bytes = sum(
            path.stat().st_size
            for path in {
                sidecar
                for task in summary.tasks
                for sidecar in (
                    task.anlz_dat_path,
                    task.anlz_ext_path,
                    task.anlz_2ex_path,
                )
                if sidecar and sidecar.is_file()
            }
            | {
                sidecar
                for repair in summary.analysis_repairs
                for sidecar in repair.sidecar_paths
                if sidecar.is_file()
            }
        )
        pdb_path = summary.usb_root / "PIONEER" / "rekordbox" / "export.pdb"
        pdb_bytes = pdb_path.stat().st_size if pdb_path.is_file() else 0

        # Atomic rewrites need temporary copies even without persistent .bak files.
        required_space += pdb_bytes + sidecar_bytes
        if backup and not local_original_backup:
            required_space += pdb_bytes + sidecar_bytes
        return required_space

    @staticmethod
    def estimate_local_backup_space(summary: ScanSummary) -> int:
        """Estimate local capacity for originals, metadata snapshots, and a margin."""
        source_paths = {
            task.source_abs_path.resolve()
            for task in summary.tasks
            if task.source_abs_path.is_file()
        }
        source_bytes = sum(path.stat().st_size for path in source_paths)
        preexisting_target_paths = {
            task.target_abs_path.resolve()
            for task in summary.tasks
            if task.target_abs_path.is_file()
            and task.existing_target_track_id is None
            and ConversionEngine._path_key(task.target_abs_path)
            != ConversionEngine._path_key(task.source_abs_path)
        }
        preexisting_target_bytes = sum(
            path.stat().st_size for path in preexisting_target_paths
        )
        metadata_paths = {
            path.resolve()
            for task in summary.tasks
            for path in (task.anlz_dat_path, task.anlz_ext_path, task.anlz_2ex_path)
            if path and path.is_file()
        }
        metadata_paths.update(
            sidecar.resolve()
            for repair in summary.analysis_repairs
            for sidecar in repair.sidecar_paths
            if sidecar.is_file()
        )
        pdb_path = summary.usb_root / "PIONEER" / "rekordbox" / "export.pdb"
        if pdb_path.is_file():
            metadata_paths.add(pdb_path.resolve())
        for dlp_path in (
            summary.usb_root / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db",
            summary.usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db",
        ):
            if dlp_path.is_file():
                metadata_paths.add(dlp_path.resolve())
        metadata_bytes = sum(path.stat().st_size for path in metadata_paths)
        return int(
            (source_bytes + preexisting_target_bytes + metadata_bytes) * 1.02
        ) + 1024 * 1024

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
        enforce_pcm_16_bit: bool = False,
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
        repair_analyze_paths = set()

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
            lossless_depth_format = track.extension in {
                "aif",
                "aiff",
                "wav",
                "wave",
                "flac",
                "fla",
            } or track.codec_name.lower() == "alac"
            if (
                enforce_pcm_16_bit
                and lossless_depth_format
                and track.sample_depth != 16
            ):
                check.is_compatible = False
                check.reasons.append(
                    "The selected 16-bit policy requires all lossless audio to be 16-bit."
                )
            if check.is_compatible:
                summary.compatible_tracks += 1
                if (
                    ext in {"aif", "aiff", "wav", "wave", "mp3"}
                    and self._is_within(usb_root, source_abs)
                    and source_abs.is_file()
                ):
                    probe = self.audio_converter.probe(source_abs)
                    if not probe.get("probe_error"):
                        expected_bitrate = self._probed_device_sql_bitrate(probe)
                        if (
                            expected_bitrate
                            and track.bitrate >= 100000
                            and track.bitrate // 1000 == expected_bitrate
                        ):
                            summary.bitrate_repairs.append(
                                BitrateMetadataRepair(
                                    track=track,
                                    old_bitrate=track.bitrate,
                                    new_bitrate=expected_bitrate,
                                )
                            )
                repair = self._plan_analysis_path_repair(
                    usb_root, track, source_abs
                )
                if repair and track.analyze_path not in repair_analyze_paths:
                    summary.analysis_repairs.append(repair)
                    repair_analyze_paths.add(track.analyze_path)
            else:
                summary.incompatible_tracks += 1

                target_fmt = forced_target_format or check.suggested_target_format
                target_sr = forced_sample_rate or check.suggested_sample_rate
                target_sd = (
                    16
                    if enforce_pcm_16_bit
                    else forced_sample_depth or check.suggested_sample_depth
                )
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
                anlz_2ex = None
                if track.analyze_path:
                    anlz_rel = track.analyze_path.lstrip("/")
                    anlz_dat_candidate = usb_root / anlz_rel
                    anlz_dat = anlz_dat_candidate
                    anlz_ext_candidate = anlz_dat_candidate.with_suffix(".EXT")
                    if anlz_ext_candidate.exists():
                        anlz_ext = anlz_ext_candidate
                    anlz_2ex_candidate = anlz_dat_candidate.with_suffix(".2EX")
                    if anlz_2ex_candidate.exists():
                        anlz_2ex = anlz_2ex_candidate

                source_size_at_scan = None
                source_mtime_ns_at_scan = None
                try:
                    if source_abs.is_file():
                        source_stat = source_abs.stat()
                        source_size_at_scan = source_stat.st_size
                        source_mtime_ns_at_scan = source_stat.st_mtime_ns
                except OSError:
                    # Preflight reports a missing or unreadable source before
                    # any conversion work begins.
                    pass

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
                    anlz_2ex_path=anlz_2ex,
                    source_size_at_scan=source_size_at_scan,
                    source_mtime_ns_at_scan=source_mtime_ns_at_scan,
                )
                summary.tasks.append(task)
                if track.duration > 0:
                    if target_fmt == TargetFormat.MP3:
                        estimate = int(track.duration * 320000 / 8)
                    else:
                        estimate = int(track.duration * target_sr * 2 * target_sd / 8)
                    summary.estimated_extra_bytes += estimate

        database_path_owners = {
            self._path_key(usb_root / track.file_path.lstrip("/")): track.id
            for track in pdb_manager.tracks
            if track.file_path
        }
        for task in summary.tasks:
            if task.target_abs_path.is_file():
                task.existing_target_track_id = database_path_owners.get(
                    self._path_key(task.target_abs_path)
                )

        summary.required_space_bytes = self.estimate_required_space(summary, backup=True)
        summary.required_space_with_local_backup_bytes = self.estimate_required_space(
            summary,
            backup=True,
            local_original_backup=True,
        )
        summary.local_backup_required_space_bytes = self.estimate_local_backup_space(summary)
        return summary

    def plan_retained_original_cleanup(self, usb_root: Path) -> OriginalCleanupPlan:
        """Build a verified plan for removing originals retained by bridge conversion."""
        usb_root = Path(usb_root).resolve()
        plan = OriginalCleanupPlan(usb_root=usb_root)
        pdb_path = usb_root / "PIONEER" / "rekordbox" / "export.pdb"
        backup_path = usb_root / "PIONEER" / "rekordbox" / "export.pdb.bak"
        dlp_paths = (
            usb_root / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db",
            usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db",
        )
        plan.onelibrary_path = next((path for path in dlp_paths if path.is_file()), None)
        plan.has_onelibrary = plan.onelibrary_path is not None
        if not plan.onelibrary_path:
            plan.errors.append(
                "OneLibrary was not found. This cleanup is only available after the guarded "
                "OneLibrary bridge workflow."
            )
        elif not self._is_within(usb_root, plan.onelibrary_path):
            plan.errors.append("OneLibrary resolves outside the selected USB root.")

        for label, path in (
            ("current export.pdb", pdb_path),
            ("pre-conversion export.pdb backup", backup_path),
        ):
            if not self._is_within(usb_root, path):
                plan.errors.append(f"Unsafe {label} path resolves outside the selected USB.")
            elif not path.is_file():
                plan.errors.append(f"Missing {label}: {path}")
        if plan.errors:
            return plan

        try:
            current_manager = PDBManager(pdb_path)
            backup_manager = PDBManager(backup_path)
        except Exception as exc:
            plan.errors.append(f"Could not read Device Library databases: {exc}")
            return plan

        plan.current_pdb_sha256 = hashlib.sha256(current_manager.data).hexdigest()
        plan.backup_pdb_sha256 = hashlib.sha256(backup_manager.data).hexdigest()
        current_by_id = {track.id: track for track in current_manager.tracks}
        current_paths = {
            PurePosixPath(track.file_path).as_posix().casefold()
            for track in current_manager.tracks
        }
        seen_originals = set()
        latest_replacement_mtime_ns = 0

        for original_track in backup_manager.tracks:
            replacement_track = current_by_id.get(original_track.id)
            if not replacement_track or replacement_track.file_path == original_track.file_path:
                continue

            original_key = PurePosixPath(original_track.file_path).as_posix().casefold()
            if original_key in seen_originals:
                continue
            seen_originals.add(original_key)

            original = usb_root / original_track.file_path.lstrip("/")
            replacement = usb_root / replacement_track.file_path.lstrip("/")
            problem_prefix = f"Track {original_track.id} ({original_track.title or original_track.filename})"
            original_path = PurePosixPath(original_track.file_path)
            replacement_path = PurePosixPath(replacement_track.file_path)

            if original_key in current_paths:
                plan.errors.append(
                    f"{problem_prefix}: retained original is still referenced by the current Device Library: "
                    f"{original_track.file_path}"
                )
                continue
            if (
                original_path.parent != replacement_path.parent
                or original_path.stem != replacement_path.stem
                or replacement_track.extension not in {"aif", "aiff", "wav", "mp3"}
            ):
                plan.errors.append(
                    f"{problem_prefix}: database paths are not an extension-only converter "
                    "replacement; refusing to infer that the original is safe to remove."
                )
                continue
            if not self._is_within(usb_root, original):
                plan.errors.append(f"{problem_prefix}: original path escapes the USB root.")
                continue
            if not self._is_within(usb_root, replacement):
                plan.errors.append(f"{problem_prefix}: replacement path escapes the USB root.")
                continue
            if not original.is_file():
                continue
            if PurePosixPath(replacement_track.file_path).name != replacement_track.filename:
                plan.errors.append(
                    f"{problem_prefix}: replacement filename and database path disagree."
                )
                continue
            if not replacement.is_file():
                plan.errors.append(
                    f"{problem_prefix}: converted replacement is missing: {replacement_track.file_path}"
                )
                continue

            replacement_stat = replacement.stat()
            if replacement_track.file_size and replacement_stat.st_size != replacement_track.file_size:
                plan.errors.append(
                    f"{problem_prefix}: converted replacement size does not match Device Library "
                    f"({replacement_track.file_size} bytes in database, {replacement_stat.st_size} on disk)."
                )
                continue

            probe = self.audio_converter.probe(replacement)
            if probe.get("probe_error"):
                plan.errors.append(
                    f"{problem_prefix}: converted replacement cannot be decoded: {probe['probe_error']}"
                )
                continue
            expected_codecs = {
                "aif": {"pcm_s16be", "pcm_s24be"},
                "aiff": {"pcm_s16be", "pcm_s24be"},
                "wav": {"pcm_s16le", "pcm_s24le"},
                "mp3": {"mp3"},
            }
            actual_codec = str(probe.get("codec_name") or "").lower()
            if actual_codec not in expected_codecs[replacement_track.extension]:
                plan.errors.append(
                    f"{problem_prefix}: replacement codec '{actual_codec or 'unknown'}' does not "
                    f"match its .{replacement_track.extension} extension."
                )
                continue
            expected_file_type = REKORDBOX_FILE_TYPE_BY_EXTENSION[
                replacement_track.extension
            ]
            if replacement_track.file_type != expected_file_type:
                plan.errors.append(
                    f"{problem_prefix}: Device Library file type does not match the "
                    f".{replacement_track.extension} replacement "
                    f"(expected 0x{expected_file_type:02x}, found "
                    f"0x{replacement_track.file_type:02x})."
                )
                continue
            actual_rate = int(probe.get("sample_rate") or 0)
            if actual_rate and actual_rate != replacement_track.sample_rate:
                plan.errors.append(
                    f"{problem_prefix}: replacement sample rate does not match Device Library "
                    f"({replacement_track.sample_rate} Hz in database, {actual_rate} Hz in file)."
                )
                continue
            if replacement_track.extension != "mp3":
                actual_depth = int(probe.get("bits_per_sample") or 0)
                if actual_depth and actual_depth != replacement_track.sample_depth:
                    plan.errors.append(
                        f"{problem_prefix}: replacement bit depth does not match Device Library "
                        f"({replacement_track.sample_depth}-bit in database, "
                        f"{actual_depth}-bit in file)."
                    )
                    continue

            if replacement_track.analyze_path:
                anlz_dat = usb_root / replacement_track.analyze_path.lstrip("/")
                if not self._is_within(usb_root, anlz_dat) or not anlz_dat.is_file():
                    plan.errors.append(
                        f"{problem_prefix}: required ANLZ .DAT file is missing or unsafe."
                    )
                    continue
                if ANLZManager.read_path(anlz_dat) != replacement_track.file_path:
                    plan.errors.append(
                        f"{problem_prefix}: ANLZ .DAT still references the old audio path."
                    )
                    continue
                anlz_ext = anlz_dat.with_suffix(".EXT")
                if (
                    anlz_ext.is_file()
                    and ANLZManager.read_path(anlz_ext) != replacement_track.file_path
                ):
                    plan.errors.append(
                        f"{problem_prefix}: ANLZ .EXT still references the old audio path."
                    )
                    continue
                anlz_2ex = anlz_dat.with_suffix(".2EX")
                if (
                    anlz_2ex.is_file()
                    and ANLZManager.read_path(anlz_2ex) != replacement_track.file_path
                ):
                    plan.errors.append(
                        f"{problem_prefix}: ANLZ .2EX still references the old audio path."
                    )
                    continue

            original_stat = original.stat()
            plan.candidates.append(
                OriginalCleanupCandidate(
                    track_id=original_track.id,
                    track_title=original_track.title or original_track.filename,
                    original_usb_path=original_track.file_path,
                    original_abs_path=original,
                    replacement_usb_path=replacement_track.file_path,
                    replacement_abs_path=replacement,
                    original_size=original_stat.st_size,
                    original_mtime_ns=original_stat.st_mtime_ns,
                    replacement_size=replacement_stat.st_size,
                    replacement_mtime_ns=replacement_stat.st_mtime_ns,
                )
            )
            plan.total_bytes += original_stat.st_size
            latest_replacement_mtime_ns = max(
                latest_replacement_mtime_ns,
                replacement_stat.st_mtime_ns,
            )

        if not plan.candidates and not plan.errors:
            plan.errors.append(
                "No retained originals with verified converted replacements were found."
            )

        if plan.onelibrary_path:
            # A strictly newer OneLibrary file is evidence that Rekordbox
            # performed Step 2. Equal timestamps are intentionally rejected:
            # FAT timestamp rounding must not make a pre-conversion database
            # look rebuilt. The opaque contents still require explicit visual
            # verification by the user.
            onelibrary_data = plan.onelibrary_path.read_bytes()
            plan.onelibrary_sha256 = hashlib.sha256(onelibrary_data).hexdigest()
            plan.onelibrary_mtime_ns = plan.onelibrary_path.stat().st_mtime_ns
            plan.onelibrary_rebuild_observed = (
                plan.onelibrary_mtime_ns > latest_replacement_mtime_ns
            )
            if not plan.onelibrary_rebuild_observed:
                plan.errors.append(
                    "OneLibrary does not appear to have been rebuilt after conversion. In Rekordbox, "
                    "run OneLibrary > Convert from Device Library, verify the converted tracks, then "
                    "prepare cleanup again."
                )
            else:
                plan.warnings.append(
                    "OneLibrary was modified after conversion, but its opaque contents cannot be "
                    "verified by this app. Confirm the converted tracks in Rekordbox before deleting originals."
                )

        return plan

    def cleanup_retained_originals(self, plan: OriginalCleanupPlan) -> dict:
        """Permanently remove every verified original in a fresh cleanup plan."""
        if plan.errors:
            return {
                "success": False,
                "error": "Cleanup plan contains errors; no originals were removed.",
                "errors": list(plan.errors),
                "removed": 0,
                "failed": len(plan.candidates),
                "freed_bytes": 0,
            }

        fresh = self.plan_retained_original_cleanup(plan.usb_root)
        expected_candidates = {
            (
                candidate.track_id,
                candidate.original_usb_path,
                candidate.replacement_usb_path,
                candidate.original_size,
                candidate.original_mtime_ns,
                candidate.replacement_size,
                candidate.replacement_mtime_ns,
            )
            for candidate in plan.candidates
        }
        fresh_candidates = {
            (
                candidate.track_id,
                candidate.original_usb_path,
                candidate.replacement_usb_path,
                candidate.original_size,
                candidate.original_mtime_ns,
                candidate.replacement_size,
                candidate.replacement_mtime_ns,
            )
            for candidate in fresh.candidates
        }
        if (
            fresh.errors
            or fresh.current_pdb_sha256 != plan.current_pdb_sha256
            or fresh.backup_pdb_sha256 != plan.backup_pdb_sha256
            or fresh.onelibrary_sha256 != plan.onelibrary_sha256
            or fresh.onelibrary_mtime_ns != plan.onelibrary_mtime_ns
            or fresh_candidates != expected_candidates
        ):
            errors = list(fresh.errors)
            if not errors:
                errors.append("The USB changed after cleanup was prepared. Prepare cleanup again.")
            return {
                "success": False,
                "error": "Cleanup preflight changed; no originals were removed.",
                "errors": errors,
                "removed": 0,
                "failed": len(plan.candidates),
                "freed_bytes": 0,
            }

        removed = 0
        freed_bytes = 0
        errors = []
        for candidate in plan.candidates:
            try:
                candidate.original_abs_path.unlink()
                self._sync_directory(candidate.original_abs_path.parent)
                removed += 1
                freed_bytes += candidate.original_size
            except OSError as exc:
                errors.append(f"Could not remove {candidate.original_usb_path}: {exc}")

        return {
            "success": not errors,
            "error": "Some retained originals could not be removed." if errors else "",
            "errors": errors,
            "removed": removed,
            "failed": len(errors),
            "freed_bytes": freed_bytes,
        }

    def clean_dotfiles(self, usb_root: Path) -> int:
        """Removes hidden AppleDouble (._*) and .DS_Store ghost files from the USB drive.

        Returns the number of files cleaned.
        """
        usb_root = Path(usb_root).resolve()
        count = 0

        # Run macOS dot_clean if on Darwin
        if platform.system() == "Darwin" and shutil.which("dot_clean"):
            try:
                run_external(
                    ["dot_clean", "-m", str(usb_root)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
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
                for sidecar in (
                    dat_path,
                    dat_path.with_suffix(".EXT"),
                    dat_path.with_suffix(".2EX"),
                ):
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
                res = run_external(
                    ["diskutil", "unmountDisk", str(usb_root)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if res.returncode == 0:
                    return True, f"Drive {usb_root.name} safely unmounted."
                return False, res.stderr.strip()
            elif os_type == "Windows":
                return False, "On Windows, use 'Safely Remove Hardware' from the system tray."
            else:
                res = run_external(
                    ["umount", str(usb_root)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
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
        threads: int = DEFAULT_CONVERSION_THREADS,
        clean_dotfiles: bool = True,
        progress_callback: Optional[Callable[[ConversionTask, int, int], None]] = None,
        phase_callback: Optional[Callable[[str, int, int, str], None]] = None,
        allow_onelibrary_bridge: bool = False,
        local_original_backup_dir: Optional[Path] = None,
        replace_existing_targets: bool = False,
    ) -> dict:
        """Execute conversion, optionally archiving originals off the USB first."""
        total_tasks = len(summary.tasks)
        total_repairs = len(summary.analysis_repairs)
        total_bitrate_repairs = len(summary.bitrate_repairs)
        total_work = total_tasks + total_repairs + total_bitrate_repairs

        def report_phase(phase: str, current: int, total: int, detail: str = "") -> None:
            if not phase_callback:
                return
            try:
                phase_callback(phase, current, total, detail)
            except Exception:
                # Status reporting must never interrupt a recoverable conversion.
                pass

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
                "total": total_work,
                "completed": 0,
                "failed": total_work,
            }

        if summary.onelibrary_bridge_mode:
            if local_original_backup_dir and not delete_original:
                return {
                    "success": False,
                    "error": "The OneLibrary local-backup workflow must remove archived originals from the USB.",
                    "total": total_work,
                    "completed": 0,
                    "failed": total_work,
                }
            if not local_original_backup_dir and delete_original:
                return {
                    "success": False,
                    "error": (
                        "The experimental OneLibrary bridge requires retaining all original "
                        "audio files unless a verified local original backup folder is provided."
                    ),
                    "total": total_work,
                    "completed": 0,
                    "failed": total_work,
                }
            if not backup:
                return {
                    "success": False,
                    "error": "The experimental OneLibrary bridge requires database backups.",
                    "total": total_work,
                    "completed": 0,
                    "failed": total_work,
                }
        elif local_original_backup_dir and not delete_original:
            return {
                "success": False,
                "error": "A local original backup can only be used when originals are removed from the USB.",
                "total": total_work,
                "completed": 0,
                "failed": total_work,
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
                "total": total_work,
                "completed": 0,
                "failed": total_work,
            }

        preflight_errors: List[str] = []
        target_owners: Dict[str, ConversionTask] = {}
        use_local_original_backup = local_original_backup_dir is not None
        database_path_owners = {
            self._path_key(summary.usb_root / track.file_path.lstrip("/")): track.id
            for track in pdb_manager.tracks
            if track.file_path
        }
        tracks_by_id = {track.id: track for track in pdb_manager.tracks}
        adopted_target_stats: Dict[int, Tuple[int, int]] = {}

        report_phase("preflight", 0, total_tasks, "Checking conversion plan")
        for task_number, task in enumerate(summary.tasks, start=1):
            task.status = "pending"
            task.error = None
            task.reuse_existing_target = False
            task.adopt_existing_target = False
            source = task.source_abs_path
            target = task.target_abs_path
            source_key = self._path_key(source)
            target_key = self._path_key(target)
            referenced_track_id = (
                database_path_owners.get(target_key)
                if source_key != target_key and target.is_file()
                else None
            )
            task.existing_target_track_id = referenced_track_id

            adoption_error: Optional[str] = None
            if not source.is_file() and referenced_track_id is not None:
                adoption_error = self._existing_owner_matches_task(
                    task, tracks_by_id.get(referenced_track_id)
                )
                owner = tracks_by_id.get(referenced_track_id)
                if (
                    adoption_error is None
                    and owner is not None
                    and owner.file_size
                    and target.stat().st_size != owner.file_size
                ):
                    adoption_error = "the existing target size differs from its database row"
                if adoption_error is None:
                    task.output_probe = self.audio_converter.probe(target)
                    adoption_error = self._existing_target_probe_error(
                        task, task.output_probe
                    )
                if (
                    adoption_error is None
                    and use_local_original_backup
                    and replace_existing_targets
                ):
                    target_stat = target.stat()
                    task.reuse_existing_target = True
                    task.adopt_existing_target = True
                    task.new_file_size = target_stat.st_size
                    adopted_target_stats[id(task)] = (
                        target_stat.st_size,
                        target_stat.st_mtime_ns,
                    )

            if not self._is_within(summary.usb_root, source):
                task.error = f"Unsafe source path escapes the USB root: {task.track.file_path}"
            elif not self._is_within(summary.usb_root, target):
                task.error = f"Unsafe target path escapes the USB root: {task.target_usb_path}"
            elif task.anlz_dat_path and not self._is_within(
                summary.usb_root, task.anlz_dat_path
            ):
                task.error = f"Unsafe ANLZ path escapes the USB root: {task.track.analyze_path}"
            elif task.anlz_ext_path and not self._is_within(
                summary.usb_root, task.anlz_ext_path
            ):
                task.error = f"Unsafe ANLZ path escapes the USB root: {task.track.analyze_path}"
            elif task.anlz_2ex_path and not self._is_within(
                summary.usb_root, task.anlz_2ex_path
            ):
                task.error = f"Unsafe ANLZ path escapes the USB root: {task.track.analyze_path}"
            elif not source.is_file() and not task.adopt_existing_target:
                if adoption_error:
                    task.error = (
                        f"Source is missing and the existing target cannot be safely adopted: "
                        f"{adoption_error}: {target}"
                    )
                else:
                    task.error = f"Source file not found or not a regular file: {source}"
            elif PurePosixPath(task.track.file_path).name != task.track.filename:
                task.error = (
                    "Database filename and file path disagree: "
                    f"'{task.track.filename}' vs '{task.track.file_path}'"
                )
            elif (
                source.is_file()
                and task.source_size_at_scan is not None
                and source.stat().st_size != task.source_size_at_scan
            ):
                task.error = (
                    f"Source file changed since this scan: {source} "
                    f"(scanned {task.source_size_at_scan} bytes, "
                    f"now {source.stat().st_size} bytes). Scan the drive again before converting."
                )
            elif (
                source.is_file()
                and task.source_mtime_ns_at_scan is not None
                and source.stat().st_mtime_ns != task.source_mtime_ns_at_scan
            ):
                task.error = (
                    f"Source file changed since this scan: {source} "
                    "(modification time changed). Scan the drive again before converting."
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
            elif target_key in target_owners:
                other = target_owners[target_key]
                task.error = (
                    f"Target collision with track {other.track.id}: both map to {task.target_usb_path}"
                )
                if not other.error:
                    other.error = task.error
                    preflight_errors.append(other.error)
            elif source_key != target_key and target.exists():
                if not use_local_original_backup:
                    task.error = (
                        "An existing target can only be resolved with a verified local "
                        f"backup enabled: {target}"
                    )
                elif not replace_existing_targets:
                    task.error = (
                        "Existing target requires explicit reuse or replacement "
                        f"confirmation: {target}"
                    )
                elif referenced_track_id is not None:
                    task.reuse_existing_target = True
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
                task.error = (
                    "ANLZ .EXT does not reference this track's current path: "
                    f"{task.anlz_ext_path}"
                )
            elif (
                task.track.file_path != task.target_usb_path
                and task.anlz_2ex_path
                and ANLZManager.read_path(task.anlz_2ex_path) != task.track.file_path
            ):
                task.error = (
                    "ANLZ .2EX does not reference this track's current path: "
                    f"{task.anlz_2ex_path}"
                )

            target_owners[target_key] = task
            if task.error:
                task.status = "failed"
                preflight_errors.append(task.error)
            report_phase("preflight", task_number, total_tasks, task.track.filename)

        current_tracks = {track.id: track for track in pdb_manager.tracks}
        for repair in summary.bitrate_repairs:
            current_track = current_tracks.get(repair.track.id)
            if (
                current_track is None
                or current_track.file_path != repair.track.file_path
                or current_track.bitrate != repair.old_bitrate
            ):
                preflight_errors.append(
                    f"Track {repair.track.id} bitrate metadata changed after the scan; "
                    "scan the drive again."
                )
        for repair in summary.analysis_repairs:
            current_track = current_tracks.get(repair.track.id)
            if (
                current_track is None
                or current_track.file_path != repair.new_audio_path
                or current_track.analyze_path != repair.track.analyze_path
            ):
                preflight_errors.append(
                    f"Track {repair.track.id} changed after the scan; scan the drive again."
                )
                continue
            audio_path = summary.usb_root / repair.new_audio_path.lstrip("/")
            if not self._is_within(summary.usb_root, audio_path) or not audio_path.is_file():
                preflight_errors.append(
                    f"Waveform repair target audio is missing or unsafe: {audio_path}"
                )
                continue
            for sidecar in repair.sidecar_paths:
                if (
                    not self._is_within(summary.usb_root, sidecar)
                    or not sidecar.is_file()
                    or ANLZManager.read_path(sidecar) != repair.old_audio_path
                ):
                    preflight_errors.append(
                        f"Analysis file changed after the scan: {sidecar}"
                    )
                    break

        if preflight_errors:
            return {
                "success": False,
                "error": "Preflight checks failed; no changes were made.",
                "preflight_errors": preflight_errors,
                "total": total_work,
                "completed": 0,
                "failed": len(preflight_errors),
                "cleaned_dotfiles": 0,
            }

        local_backup_base: Optional[Path] = None
        if local_original_backup_dir is not None:
            try:
                local_backup_base = LocalBackupSession.validate_destination(
                    local_original_backup_dir,
                    summary.usb_root,
                )
                local_required = self.estimate_local_backup_space(summary)
                local_free = shutil.disk_usage(local_backup_base).free
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"Local original backup is unavailable: {exc}",
                    "total": total_work,
                    "completed": 0,
                    "failed": total_work,
                    "cleaned_dotfiles": 0,
                }
            if local_required > local_free:
                return {
                    "success": False,
                    "error": (
                        "Insufficient local backup space: approximately "
                        f"{local_required / (1024 ** 2):.1f} MiB required, "
                        f"{local_free / (1024 ** 2):.1f} MiB available."
                    ),
                    "total": total_work,
                    "completed": 0,
                    "failed": total_work,
                    "cleaned_dotfiles": 0,
                }

        required_space = self.estimate_required_space(
            summary,
            backup=backup,
            local_original_backup=use_local_original_backup,
        )

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
                "total": total_work,
                "completed": 0,
                "failed": total_work,
                "cleaned_dotfiles": 0,
            }

        local_session: Optional[LocalBackupSession] = None
        if local_backup_base is not None:
            metadata_paths = {pdb_path}
            metadata_paths.update(
                path
                for task in summary.tasks
                for path in (task.anlz_dat_path, task.anlz_ext_path, task.anlz_2ex_path)
                if path and path.is_file()
            )
            metadata_paths.update(
                sidecar
                for repair in summary.analysis_repairs
                for sidecar in repair.sidecar_paths
                if sidecar.is_file()
            )
            for dlp_path in (
                summary.usb_root / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db",
                summary.usb_root / "PIONEER" / "rekordbox" / "exportLibrary.db",
            ):
                if dlp_path.is_file():
                    metadata_paths.add(dlp_path)
            try:
                report_phase("backup", 0, 0, "Creating local recovery archive")
                local_session = LocalBackupSession.create(
                    local_backup_base,
                    summary.usb_root,
                    summary.pdb_sha256,
                )
                local_session.archive(
                    summary.tasks,
                    metadata_paths,
                    progress_callback=lambda current, total, path: report_phase(
                        "backup", current, total, path.name
                    ),
                )
                report_phase(
                    "backup_verification", 0, 0, "Checking archived originals"
                )
                local_session.remove_originals_from_usb(
                    progress_callback=lambda current, total, path: report_phase(
                        "backup_verification", current, total, path.name
                    )
                )
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"Could not create verified local original backup: {exc}",
                    "total": total_work,
                    "completed": 0,
                    "failed": total_work,
                    "cleaned_dotfiles": 0,
                    "local_backup_session": str(local_session.path) if local_session else "",
                }

        # USB-side metadata backups remain the compatibility path when no
        # complete local recovery archive was requested.
        if backup and local_session is None:
            try:
                report_phase("metadata_backup", 0, total_tasks, "Protecting Rekordbox metadata")
                pdb_manager.save(backup=True)
                backed_up_paths = set()
                for task in summary.tasks:
                    if task.track.file_path == task.target_usb_path:
                        continue
                    for anlz_path in (
                        task.anlz_dat_path,
                        task.anlz_ext_path,
                        task.anlz_2ex_path,
                    ):
                        if anlz_path and anlz_path not in backed_up_paths:
                            self._backup_file(anlz_path)
                            backed_up_paths.add(anlz_path)
                for repair in summary.analysis_repairs:
                    for sidecar in repair.sidecar_paths:
                        if sidecar not in backed_up_paths:
                            self._backup_file(sidecar)
                            backed_up_paths.add(sidecar)
            except Exception as exc:
                return {"success": False, "error": f"Could not create required backups: {exc}"}

        completed = 0
        failed = 0
        anlz_updated = 0
        warnings: List[str] = []
        staged_paths: Dict[int, Path] = {}

        def convert_to_stage(task: ConversionTask):
            task.status = "converting"
            if task.adopt_existing_target:
                expected_stat = adopted_target_stats[id(task)]
                current_stat = task.target_abs_path.stat()
                if (current_stat.st_size, current_stat.st_mtime_ns) != expected_stat:
                    task.status = "failed"
                    task.error = (
                        "Existing target changed after preflight; scan the drive again"
                    )
                    return False
                return True
            stage = task.target_abs_path.with_name(
                f".{task.target_abs_path.stem}.rbconvert-{uuid.uuid4().hex}{task.target_abs_path.suffix}"
            )
            staged_paths[id(task)] = stage
            success, new_size, err = self.audio_converter.convert(
                source_path=(
                    local_session.archived_path(task.source_abs_path)
                    if local_session
                    else task.source_abs_path
                ),
                target_path=stage,
                target_format=task.target_format,
                sample_rate=task.target_sample_rate,
                sample_depth=task.target_sample_depth,
            )
            if not success:
                task.status = "failed"
                task.error = err
                if local_session:
                    try:
                        local_session.restore_task_after_failure(task)
                    except Exception as restore_exc:
                        task.warnings.append(
                            f"Could not restore task files from local backup: {restore_exc}"
                        )
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
            stage = staged_paths.get(id(task))
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
                "file_type": task.track.file_type,
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
                if task.reuse_existing_target and not target.is_file():
                    raise RuntimeError(
                        f"Referenced target disappeared during conversion: {target}"
                    )
                if not task.reuse_existing_target and not same_path and target.exists():
                    raise RuntimeError(f"Target appeared during conversion; refusing to overwrite: {target}")
                if task.adopt_existing_target:
                    expected_stat = adopted_target_stats[id(task)]
                    target_stat = target.stat()
                    if (target_stat.st_size, target_stat.st_mtime_ns) != expected_stat:
                        raise RuntimeError(
                            "Existing target changed while conversion was running"
                        )
                    task.output_probe = self.audio_converter.probe(target)
                    probe_error = self._existing_target_probe_error(
                        task, task.output_probe
                    )
                    if probe_error:
                        raise RuntimeError(
                            f"Existing target is no longer safe to adopt: {probe_error}"
                        )
                    task.new_file_size = target_stat.st_size
                elif task.reuse_existing_target:
                    if stage is None:
                        raise RuntimeError("Internal error: converted staging file is missing")
                    staged_hash = self.audio_converter.decoded_audio_sha256(stage)
                    existing_hash = self.audio_converter.decoded_audio_sha256(target)
                    if staged_hash != existing_hash:
                        raise RuntimeError(
                            "Existing referenced target is not audio-identical to the requested "
                            f"conversion; refusing reuse: {target}"
                        )
                    task.new_file_size = target.stat().st_size
                    task.output_probe = self.audio_converter.probe(target)
                    if task.output_probe.get("probe_error"):
                        raise RuntimeError(
                            f"Existing target failed verification: {task.output_probe['probe_error']}"
                        )
                if track_snapshot["file_path"] != task.target_usb_path:
                    for anlz_path in (
                        task.anlz_dat_path,
                        task.anlz_ext_path,
                        task.anlz_2ex_path,
                    ):
                        if anlz_path and ANLZManager.read_path(anlz_path) not in {
                            track_snapshot["file_path"],
                            task.target_usb_path if task.adopt_existing_target else "",
                        }:
                            raise RuntimeError(
                                f"ANLZ file changed while conversion was running: {anlz_path}"
                            )
                target.parent.mkdir(parents=True, exist_ok=True)
                if same_path and local_session is None:
                    source_backup = source.with_name(
                        f".{source.name}.rbconvert-original-{uuid.uuid4().hex}"
                    )
                    os.replace(source, source_backup)
                if not task.reuse_existing_target:
                    if stage is None:
                        raise RuntimeError("Internal error: converted staging file is missing")
                    os.replace(stage, target)
                    self._sync_directory(target.parent)

                actual_rate = int(task.output_probe.get("sample_rate") or task.target_sample_rate)
                if task.target_format == TargetFormat.MP3:
                    actual_depth = 16
                    bitrate_bits_per_second = 320000
                else:
                    actual_depth = int(
                        task.output_probe.get("bits_per_sample") or task.target_sample_depth
                    )
                    channels = int(task.output_probe.get("channels") or 2)
                    bitrate_bits_per_second = actual_rate * channels * actual_depth
                new_bitrate = device_sql_bitrate_kbps(bitrate_bits_per_second)

                if not pdb_manager.update_track(
                    track=task.track,
                    new_filename=task.target_filename,
                    new_filepath=task.target_usb_path,
                    new_filesize=task.new_file_size,
                    new_sample_rate=actual_rate,
                    new_sample_depth=actual_depth,
                    new_bitrate=new_bitrate,
                    new_file_type=REKORDBOX_FILE_TYPE_BY_TARGET[task.target_format],
                ):
                    raise RuntimeError("export.pdb patch rejected the replacement strings")

                # The database is made durable while the original path still
                # exists. Only after all required sidecars are synchronized is
                # the original removed.
                pdb_manager.save(backup=False)
                database_committed = True

                if track_snapshot["file_path"] != task.target_usb_path:
                    for anlz_path in (
                        task.anlz_dat_path,
                        task.anlz_ext_path,
                        task.anlz_2ex_path,
                    ):
                        if not anlz_path:
                            continue
                        if not anlz_path.is_file():
                            raise RuntimeError(f"ANLZ file disappeared during conversion: {anlz_path}")
                        if ANLZManager.read_path(anlz_path) != task.target_usb_path:
                            anlz_snapshots[anlz_path] = anlz_path.read_bytes()
                            if not ANLZManager.update_path(
                                anlz_path, task.target_usb_path, backup=False
                            ):
                                raise RuntimeError(f"Could not update PPTH path in {anlz_path}")
                            updated_anlz_paths += 1

                if local_session and not task.adopt_existing_target:
                    local_session.mark_converted(task)
                elif delete_original and not task.adopt_existing_target:
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
                        if local_session:
                            if not task.reuse_existing_target:
                                target.unlink(missing_ok=True)
                            if not task.adopt_existing_target:
                                local_session.restore_task_after_failure(task)
                        elif same_path and source_backup and source_backup.exists():
                            target.unlink(missing_ok=True)
                            os.replace(source_backup, source)
                        elif not same_path:
                            target.unlink(missing_ok=True)
                    except Exception as rollback_exc:
                        warnings.append(f"Could not remove staged target during rollback: {rollback_exc}")
                return False
            finally:
                if stage is not None:
                    try:
                        stage.unlink(missing_ok=True)
                    except OSError as cleanup_exc:
                        warnings.append(f"Could not remove staging file {stage}: {cleanup_exc}")

        max_workers = max(1, min(threads, total_tasks if total_tasks > 0 else 1))
        report_phase("conversion", 0, total_tasks, "Starting audio conversion")
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
                    if local_session and not task.adopt_existing_target:
                        try:
                            local_session.restore_source_after_failure(task.source_abs_path)
                        except Exception as restore_exc:
                            warnings.append(
                                f"Could not restore {task.source_abs_path} from local backup: {restore_exc}"
                            )
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

        analysis_paths_repaired = 0
        analysis_repair_error = ""
        if summary.analysis_repairs:
            report_phase(
                "waveform_repair", 0, total_repairs, "Synchronizing waveform paths"
            )
            snapshots = {
                sidecar: sidecar.read_bytes()
                for repair in summary.analysis_repairs
                for sidecar in repair.sidecar_paths
            }
            try:
                for current, repair in enumerate(summary.analysis_repairs, start=1):
                    for sidecar in repair.sidecar_paths:
                        if ANLZManager.read_path(sidecar) != repair.old_audio_path:
                            raise RuntimeError(
                                f"Analysis path changed while repair was running: {sidecar}"
                            )
                        if not ANLZManager.update_path(
                            sidecar, repair.new_audio_path, backup=False
                        ):
                            raise RuntimeError(f"Could not update waveform path in {sidecar}")
                    analysis_paths_repaired += 1
                    report_phase(
                        "waveform_repair",
                        current,
                        total_repairs,
                        repair.track.title or repair.track.filename,
                    )
            except Exception as exc:
                rollback_errors = []
                for sidecar, data in snapshots.items():
                    try:
                        self._restore_file_bytes(sidecar, data)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"{sidecar}: {rollback_exc}")
                analysis_paths_repaired = 0
                analysis_repair_error = f"Could not repair waveform paths: {exc}"
                if rollback_errors:
                    analysis_repair_error += "; rollback failed for " + "; ".join(
                        rollback_errors
                    )
                warnings.append(analysis_repair_error)

        bitrate_metadata_repaired = 0
        bitrate_repair_error = ""
        if summary.bitrate_repairs:
            report_phase(
                "metadata_repair",
                0,
                total_bitrate_repairs,
                "Correcting Device Library bitrate units",
            )
            pdb_snapshot = bytes(pdb_manager.data)
            try:
                for current, repair in enumerate(summary.bitrate_repairs, start=1):
                    track = next(
                        item for item in pdb_manager.tracks if item.id == repair.track.id
                    )
                    if track.bitrate != repair.old_bitrate:
                        raise RuntimeError(
                            f"Track {track.id} bitrate changed while repair was running"
                        )
                    if not pdb_manager.update_track_bitrate(
                        track, repair.new_bitrate
                    ):
                        raise RuntimeError(
                            f"Device Library rejected bitrate repair for track {track.id}"
                        )
                    bitrate_metadata_repaired += 1
                    report_phase(
                        "metadata_repair",
                        current,
                        total_bitrate_repairs,
                        track.title or track.filename,
                    )
                pdb_manager.save(backup=False)
            except Exception as exc:
                bitrate_metadata_repaired = 0
                bitrate_repair_error = f"Could not repair bitrate metadata: {exc}"
                try:
                    self._restore_file_bytes(pdb_path, pdb_snapshot)
                    pdb_manager = PDBManager(pdb_path)
                except Exception as rollback_exc:
                    bitrate_repair_error += f"; rollback failed: {rollback_exc}"
                warnings.append(bitrate_repair_error)

        # Clean AppleDouble ghost files
        cleaned_files = 0
        if clean_dotfiles:
            report_phase("cleanup", 0, 1, "Removing macOS ghost files")
            cleaned_files = self.clean_dotfiles(summary.usb_root)

        if summary.onelibrary_bridge_mode and (
            completed or analysis_paths_repaired or bitrate_metadata_repaired
        ):
            warnings.insert(0, ONELIBRARY_REBUILD_REQUIRED_MESSAGE)

        local_session_error = ""
        if local_session:
            try:
                report_phase("finalizing", 0, 1, "Finalizing recovery archive")
                local_session.finish(
                    failed == 0
                    and not analysis_repair_error
                    and not bitrate_repair_error
                )
            except Exception as exc:
                local_session_error = f"Could not finalize local recovery manifest: {exc}"
                warnings.append(local_session_error)

        return {
            "success": (
                failed == 0
                and not local_session_error
                and not analysis_repair_error
                and not bitrate_repair_error
            ),
            "error": local_session_error or analysis_repair_error or bitrate_repair_error,
            "total": total_work,
            "completed": completed,
            "failed": (
                failed
                + (total_repairs if analysis_repair_error else 0)
                + (total_bitrate_repairs if bitrate_repair_error else 0)
            ),
            "adopted_existing_targets": len(
                [
                    task
                    for task in summary.tasks
                    if task.status == "completed" and task.adopt_existing_target
                ]
            ),
            "anlz_updated": anlz_updated,
            "analysis_paths_repaired": analysis_paths_repaired,
            "bitrate_metadata_repaired": bitrate_metadata_repaired,
            "cleaned_dotfiles": cleaned_files,
            "warnings": warnings,
            "onelibrary_sync_required": bool(
                summary.onelibrary_bridge_mode
                and (completed or analysis_paths_repaired or bitrate_metadata_repaired)
            ),
            "local_backup_session": str(local_session.path) if local_session else "",
        }

    def restore_local_backup(
        self, session_dir: Path, usb_root: Optional[Path] = None
    ) -> Tuple[bool, str]:
        """Restore audio, Device Library, OneLibrary, and ANLZ from a local archive."""
        try:
            session = LocalBackupSession.load(session_dir)
        except Exception as exc:
            return False, f"Could not load local backup session: {exc}"
        return session.restore_to_usb(usb_root)
