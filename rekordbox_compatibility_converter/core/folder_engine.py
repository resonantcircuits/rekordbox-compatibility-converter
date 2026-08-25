"""Safe standalone audio-folder scanning and conversion."""

import hashlib
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .audio_converter import AudioConverter
from .models import TargetFormat
from .profiles import HardwareProfile


SUPPORTED_INPUT_EXTENSIONS = {
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".wav",
    ".wave",
}


@dataclass
class FolderAudioFile:
    """Audio properties used by compatibility profiles outside DeviceSQL."""

    source_path: Path
    relative_path: Path
    sample_rate: int
    sample_depth: int
    channels: int
    codec_name: str
    duration: float
    file_size: int
    tags: Dict = field(default_factory=dict)
    has_artwork: bool = False

    @property
    def filename(self) -> str:
        return self.source_path.name

    @property
    def extension(self) -> str:
        return self.source_path.suffix.lstrip(".").lower()


@dataclass
class FolderConversionTask:
    audio: FolderAudioFile
    target_path: Path
    action: str  # "convert" or "copy"
    target_format: TargetFormat
    target_sample_rate: int
    target_sample_depth: int
    reasons: List[str] = field(default_factory=list)
    source_size_at_scan: int = 0
    source_mtime_ns_at_scan: int = 0
    status: str = "pending"
    error: Optional[str] = None
    output_size: int = 0


@dataclass
class FolderScanSummary:
    source_root: Path
    destination_root: Path
    total_files: int = 0
    compatible_files: int = 0
    conversion_files: int = 0
    copy_files: int = 0
    unreadable_files: int = 0
    format_counts: Dict[str, int] = field(default_factory=dict)
    tasks: List[FolderConversionTask] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    required_space_bytes: int = 0
    free_space_bytes: int = 0
    normalize_all: bool = False
    recursive: bool = True


class FolderConversionEngine:
    """Converts ordinary folders without reading or creating Rekordbox databases."""

    def __init__(self, audio_converter: Optional[AudioConverter] = None):
        self.audio_converter = audio_converter or AudioConverter()

    @staticmethod
    def _is_within(root: Path, candidate: Path) -> bool:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve(strict=False)
        return (
            resolved_candidate == resolved_root
            or resolved_root in resolved_candidate.parents
        )

    @staticmethod
    def _target_rate(
        audio: FolderAudioFile,
        profile: HardwareProfile,
        target_format: TargetFormat,
    ) -> int:
        if profile.target_sample_rate:
            return profile.target_sample_rate
        allowed = profile.allowed_sample_rates.get(
            target_format.value, {44100, 48000}
        )
        if audio.sample_rate in allowed:
            return audio.sample_rate
        if audio.sample_rate % 44100 == 0 and 44100 in allowed:
            return 44100
        if audio.sample_rate % 48000 == 0 and 48000 in allowed:
            return 48000
        return min(allowed, key=lambda rate: abs(rate - audio.sample_rate))

    @staticmethod
    def _target_depth(
        audio: FolderAudioFile,
        profile: HardwareProfile,
        target_format: TargetFormat,
        enforce_pcm_16_bit: bool,
    ) -> int:
        if target_format == TargetFormat.MP3 or enforce_pcm_16_bit:
            return 16
        if profile.target_sample_depth:
            return profile.target_sample_depth
        if audio.sample_depth in profile.allowed_sample_depths:
            return audio.sample_depth
        return 24 if 24 in profile.allowed_sample_depths else 16

    @staticmethod
    def _estimate_task_size(task: FolderConversionTask) -> int:
        if task.action == "copy":
            return task.audio.file_size
        if task.audio.duration > 0:
            if task.target_format == TargetFormat.MP3:
                estimate = int(task.audio.duration * 320000 / 8)
            else:
                estimate = int(
                    task.audio.duration
                    * task.target_sample_rate
                    * max(1, task.audio.channels)
                    * task.target_sample_depth
                    / 8
                )
        else:
            estimate = max(task.audio.file_size * 3, 1024 * 1024)
        return int(estimate * 1.05) + 1024 * 1024

    def scan(
        self,
        source_root: Path,
        destination_root: Path,
        profile: HardwareProfile,
        target_format: TargetFormat = TargetFormat.AIFF,
        enforce_pcm_16_bit: bool = False,
        recursive: bool = True,
        normalize_all: bool = False,
        copy_compatible: bool = True,
    ) -> FolderScanSummary:
        source_root = Path(source_root).expanduser().resolve()
        destination_root = Path(destination_root).expanduser().resolve(strict=False)
        summary = FolderScanSummary(
            source_root=source_root,
            destination_root=destination_root,
            normalize_all=normalize_all,
            recursive=recursive,
        )
        if not source_root.is_dir():
            summary.issues.append(f"Source folder does not exist: {source_root}")
            return summary
        if source_root == destination_root:
            summary.issues.append(
                "Source and destination must be different folders; originals are never overwritten."
            )
            return summary

        iterator = source_root.rglob("*") if recursive else source_root.glob("*")
        candidates = []
        destination_inside_source = self._is_within(source_root, destination_root)
        for path in iterator:
            if destination_inside_source and self._is_within(destination_root, path):
                continue
            if path.name.startswith("._") or path.name == ".DS_Store":
                continue
            if path.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
                continue
            if path.is_symlink():
                summary.warnings.append(f"Symbolic link skipped: {path}")
                continue
            if path.is_file() and self._is_within(source_root, path):
                candidates.append(path)

        target_owners: Dict[str, Path] = {}
        for source_path in sorted(candidates, key=lambda item: str(item).casefold()):
            relative = source_path.relative_to(source_root)
            summary.total_files += 1
            extension = source_path.suffix.lstrip(".").lower()
            summary.format_counts[extension] = summary.format_counts.get(extension, 0) + 1
            try:
                source_stat_before = source_path.stat()
            except OSError as exc:
                summary.unreadable_files += 1
                summary.warnings.append(f"Cannot read {relative}: {exc}")
                continue
            probe = self.audio_converter.probe(source_path)
            if probe.get("probe_error") or not probe:
                summary.unreadable_files += 1
                summary.warnings.append(
                    f"Cannot read {relative}: {probe.get('probe_error') or 'no audio stream'}"
                )
                continue
            audio = FolderAudioFile(
                source_path=source_path,
                relative_path=relative,
                sample_rate=int(probe.get("sample_rate") or 0),
                sample_depth=int(probe.get("bits_per_sample") or 0),
                channels=int(probe.get("channels") or 0),
                codec_name=str(probe.get("codec_name") or ""),
                duration=float(probe.get("duration") or 0),
                file_size=int(probe.get("size") or source_stat_before.st_size),
                tags=dict(probe.get("tags") or {}),
                has_artwork=bool(probe.get("has_artwork")),
            )
            check = profile.evaluate(audio)
            lossless = audio.extension in {
                "aif", "aiff", "wav", "wave", "flac", "fla"
            } or audio.codec_name.lower() == "alac"
            if enforce_pcm_16_bit and lossless and audio.sample_depth != 16:
                check.is_compatible = False
                check.reasons.append(
                    "The selected 16-bit policy requires lossless audio to be 16-bit."
                )

            if check.is_compatible:
                summary.compatible_files += 1
            action = "convert" if normalize_all or not check.is_compatible else "copy"
            if action == "copy" and not copy_compatible:
                continue
            target_rate = self._target_rate(audio, profile, target_format)
            target_depth = self._target_depth(
                audio, profile, target_format, enforce_pcm_16_bit
            )
            target_relative = (
                relative.with_suffix(f".{target_format.value}")
                if action == "convert"
                else relative
            )
            target_path = destination_root / target_relative
            if not self._is_within(destination_root, target_path):
                summary.issues.append(
                    f"Unsafe output path escapes the destination: {target_relative}"
                )
                continue
            target_key = str(target_path.resolve(strict=False)).casefold()
            previous_source = target_owners.get(target_key)
            if previous_source is not None:
                summary.issues.append(
                    f"Output collision: {previous_source.relative_to(source_root)} and "
                    f"{relative} both map to {target_relative}"
                )
                continue
            target_owners[target_key] = source_path
            if target_path.exists():
                summary.issues.append(f"Output already exists: {target_relative}")
                continue
            try:
                source_stat = source_path.stat()
            except OSError as exc:
                summary.issues.append(
                    f"Source became unavailable while scanning: {relative}: {exc}"
                )
                continue
            if (source_stat.st_size, source_stat.st_mtime_ns) != (
                source_stat_before.st_size,
                source_stat_before.st_mtime_ns,
            ):
                summary.issues.append(f"Source changed while scanning: {relative}")
                continue
            task = FolderConversionTask(
                audio=audio,
                target_path=target_path,
                action=action,
                target_format=target_format,
                target_sample_rate=target_rate,
                target_sample_depth=target_depth,
                reasons=list(check.reasons),
                source_size_at_scan=source_stat.st_size,
                source_mtime_ns_at_scan=source_stat.st_mtime_ns,
            )
            summary.tasks.append(task)
            if (
                action == "convert"
                and target_format == TargetFormat.WAV
                and audio.has_artwork
            ):
                summary.warnings.append(
                    f"{relative}: embedded artwork cannot be represented in WAV output"
                )
            if action == "convert":
                summary.conversion_files += 1
            else:
                summary.copy_files += 1
            summary.required_space_bytes += self._estimate_task_size(task)

        try:
            if destination_root.exists() and not destination_root.is_dir():
                raise NotADirectoryError(
                    f"Destination is not a folder: {destination_root}"
                )
            capacity_path = destination_root
            while not capacity_path.exists() and capacity_path != capacity_path.parent:
                capacity_path = capacity_path.parent
            summary.free_space_bytes = shutil.disk_usage(capacity_path).free
        except OSError as exc:
            summary.issues.append(f"Destination folder is unavailable: {exc}")
        if summary.required_space_bytes > summary.free_space_bytes:
            summary.issues.append(
                "Destination does not have enough free space for the planned output."
            )
        return summary

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _copy_task(self, task: FolderConversionTask) -> bool:
        target = task.target_path
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(task.audio.source_path, temp)
            if self._sha256(temp) != self._sha256(task.audio.source_path):
                raise OSError("Copied file failed SHA-256 verification")
            with open(temp, "r+b") as copied_file:
                os.fsync(copied_file.fileno())
            if target.exists():
                raise FileExistsError(f"Output appeared during copy: {target}")
            os.replace(temp, target)
            if os.name == "posix":
                try:
                    directory_fd = os.open(str(target.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            task.output_size = target.stat().st_size
            return True
        except Exception as exc:
            temp.unlink(missing_ok=True)
            task.error = str(exc)
            return False

    def execute(
        self,
        summary: FolderScanSummary,
        threads: int = 2,
        progress_callback: Optional[
            Callable[[FolderConversionTask, int, int], None]
        ] = None,
    ) -> dict:
        """Execute a previously scanned plan without modifying source files."""
        if summary.issues:
            return {
                "success": False,
                "completed": 0,
                "converted": 0,
                "copied": 0,
                "failed": len(summary.issues),
                "errors": list(summary.issues),
            }
        if not summary.tasks:
            return {
                "success": True,
                "completed": 0,
                "converted": 0,
                "copied": 0,
                "failed": 0,
                "errors": [],
            }

        preflight_errors = []
        for task in summary.tasks:
            source = task.audio.source_path
            try:
                source_stat = source.stat()
            except OSError as exc:
                preflight_errors.append(f"Source unavailable: {source}: {exc}")
                continue
            if source.is_symlink() or not source.is_file():
                preflight_errors.append(f"Source is not a regular file: {source}")
            elif not self._is_within(summary.source_root, source):
                preflight_errors.append(f"Source escapes the selected folder: {source}")
            elif (source_stat.st_size, source_stat.st_mtime_ns) != (
                task.source_size_at_scan,
                task.source_mtime_ns_at_scan,
            ):
                preflight_errors.append(f"Source changed after scanning: {source}")
            elif task.target_path.exists():
                preflight_errors.append(f"Output already exists: {task.target_path}")
            elif not self._is_within(summary.destination_root, task.target_path):
                preflight_errors.append(
                    f"Output escapes the selected destination: {task.target_path}"
                )
        try:
            summary.destination_root.mkdir(parents=True, exist_ok=True)
            free_space = shutil.disk_usage(summary.destination_root).free
            if summary.required_space_bytes > free_space:
                preflight_errors.append("Destination no longer has enough free space.")
        except OSError as exc:
            preflight_errors.append(f"Destination is unavailable: {exc}")
        if preflight_errors:
            return {
                "success": False,
                "completed": 0,
                "converted": 0,
                "copied": 0,
                "failed": len(preflight_errors),
                "errors": preflight_errors,
            }

        completed = 0
        converted = 0
        copied = 0
        failed = 0

        def run_task(task: FolderConversionTask) -> bool:
            task.status = "processing"
            if task.action == "copy":
                success = self._copy_task(task)
            else:
                success, task.output_size, task.error = self.audio_converter.convert(
                    task.audio.source_path,
                    task.target_path,
                    target_format=task.target_format,
                    sample_rate=task.target_sample_rate,
                    sample_depth=task.target_sample_depth,
                )
            if success:
                try:
                    source_stat = task.audio.source_path.stat()
                    source_unchanged = (
                        source_stat.st_size,
                        source_stat.st_mtime_ns,
                    ) == (
                        task.source_size_at_scan,
                        task.source_mtime_ns_at_scan,
                    )
                except OSError:
                    source_unchanged = False
                if not source_unchanged:
                    task.target_path.unlink(missing_ok=True)
                    task.error = "Source changed while output was being created"
                    success = False
            task.status = "completed" if success else "failed"
            return success

        with ThreadPoolExecutor(max_workers=max(1, threads)) as executor:
            futures = {executor.submit(run_task, task): task for task in summary.tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    success = future.result()
                except Exception as exc:
                    task.status = "failed"
                    task.error = str(exc)
                    success = False
                if success:
                    completed += 1
                    if task.action == "copy":
                        copied += 1
                    else:
                        converted += 1
                else:
                    failed += 1
                if progress_callback:
                    try:
                        progress_callback(task, completed + failed, len(summary.tasks))
                    except Exception:
                        pass

        return {
            "success": failed == 0,
            "completed": completed,
            "converted": converted,
            "copied": copied,
            "failed": failed,
            "errors": [task.error for task in summary.tasks if task.error],
            "destination": str(summary.destination_root),
        }
