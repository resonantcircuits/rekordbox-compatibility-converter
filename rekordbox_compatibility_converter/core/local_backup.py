"""Verified local recovery archives for space-constrained USB conversion."""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .models import ConversionTask


class LocalBackupSession:
    """Creates and journals a recoverable archive outside the selected USB."""

    MANIFEST_NAME = "manifest.json"

    def __init__(self, session_dir: Path, manifest: Dict):
        self.session_dir = Path(session_dir).resolve()
        self.manifest = manifest
        self.usb_root = Path(str(manifest["usb_root"])).resolve()
        self._lock = threading.RLock()

    @staticmethod
    def _sha256(
        path: Path,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                if progress_callback:
                    progress_callback(len(chunk))
        return digest.hexdigest()

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

    @staticmethod
    def _safe_join(root: Path, relative_value: str, label: str) -> Path:
        """Join a manifest path without allowing symlinks to escape its root."""
        resolved_root = Path(root).resolve()
        candidate = resolved_root / relative_value
        resolved_candidate = candidate.resolve(strict=False)
        if not (
            resolved_candidate == resolved_root
            or resolved_root in resolved_candidate.parents
        ):
            raise ValueError(f"Unsafe {label} escapes its selected root: {relative_value}")
        return candidate

    @classmethod
    def validate_destination(cls, base_dir: Path, usb_root: Path) -> Path:
        """Require a writable local archive directory outside the selected USB."""
        base_dir = Path(base_dir).expanduser().resolve(strict=False)
        usb_root = Path(usb_root).resolve()
        if base_dir == usb_root or usb_root in base_dir.parents:
            raise ValueError("Local original backup folder cannot be located on the selected USB.")
        base_dir.mkdir(parents=True, exist_ok=True)
        if not base_dir.is_dir():
            raise ValueError(f"Local backup destination is not a directory: {base_dir}")
        probe = base_dir / f".rbconvert-write-test-{uuid.uuid4().hex}"
        try:
            with open(probe, "xb") as output:
                output.write(b"rbconvert")
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            raise ValueError(f"Local backup destination is not writable: {exc}") from exc
        finally:
            probe.unlink(missing_ok=True)
        return base_dir

    @classmethod
    def create(cls, base_dir: Path, usb_root: Path, pdb_sha256: str) -> "LocalBackupSession":
        base_dir = cls.validate_destination(base_dir, usb_root)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_name = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in Path(usb_root).name
        ) or "USB"
        session_dir = base_dir / f"RekordboxBackup-{safe_name}-{timestamp}-{uuid.uuid4().hex[:8]}"
        session_dir.mkdir(parents=False, exist_ok=False)
        manifest = {
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "usb_root": str(Path(usb_root).resolve()),
            "usb_name": Path(usb_root).name,
            "pre_conversion_pdb_sha256": pdb_sha256,
            "status": "preparing",
            "originals": [],
            "preexisting_targets": [],
            "metadata": [],
        }
        session = cls(session_dir, manifest)
        session._save_manifest()
        return session

    @classmethod
    def load(cls, session_dir: Path) -> "LocalBackupSession":
        session_dir = Path(session_dir).expanduser().resolve()
        manifest_path = session_dir / cls.MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Could not read local backup manifest: {exc}") from exc
        if (
            manifest.get("schema_version") != 1
            or not isinstance(manifest.get("originals"), list)
            or not isinstance(manifest.get("metadata"), list)
        ):
            raise ValueError("Unsupported or malformed local backup manifest.")
        manifest.setdefault("preexisting_targets", [])
        if not isinstance(manifest["preexisting_targets"], list):
            raise ValueError("Unsupported or malformed local backup manifest.")
        for collection in (
            manifest["originals"],
            manifest["preexisting_targets"],
            manifest["metadata"],
        ):
            for entry in collection:
                if not isinstance(entry, dict):
                    raise ValueError("Malformed local backup manifest entry.")
                for field in ("usb_path", "archive_path"):
                    value = Path(str(entry.get(field, "")))
                    if not str(value) or value.is_absolute() or ".." in value.parts:
                        raise ValueError(f"Unsafe {field} in local backup manifest.")
                converted_value = entry.get("converted_path")
                if converted_value:
                    converted = Path(str(converted_value))
                    if converted.is_absolute() or ".." in converted.parts:
                        raise ValueError("Unsafe converted_path in local backup manifest.")
        return cls(session_dir, manifest)

    def _save_manifest(self) -> None:
        with self._lock:
            destination = self.session_dir / self.MANIFEST_NAME
            temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                with open(temp, "w", encoding="utf-8") as output:
                    json.dump(self.manifest, output, indent=2, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, destination)
                self._sync_directory(destination.parent)
            finally:
                temp.unlink(missing_ok=True)

    def _relative_usb_path(self, source: Path) -> Path:
        source = Path(source).resolve()
        try:
            return source.relative_to(self.usb_root)
        except ValueError as exc:
            raise ValueError(f"Refusing to archive path outside selected USB: {source}") from exc

    def _copy_verified(
        self,
        source: Path,
        destination: Path,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Tuple[int, str]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            source_stat = source.stat()
            source_digest = hashlib.sha256()
            with open(source, "rb") as input_file, open(temp, "xb") as copied:
                for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                    copied.write(chunk)
                    source_digest.update(chunk)
                    if progress_callback:
                        progress_callback(len(chunk))
                copied.flush()
                os.fsync(copied.fileno())
            shutil.copystat(source, temp)
            current_source_stat = source.stat()
            if (
                current_source_stat.st_size != source_stat.st_size
                or current_source_stat.st_mtime_ns != source_stat.st_mtime_ns
            ):
                raise OSError(f"Source changed while archiving {source}")
            source_sha256 = source_digest.hexdigest()
            if (
                temp.stat().st_size != source_stat.st_size
                or self._sha256(temp, progress_callback) != source_sha256
            ):
                raise OSError(f"Verification failed while archiving {source}")
            os.replace(temp, destination)
            self._sync_directory(destination.parent)
            return source_stat.st_size, source_sha256
        finally:
            temp.unlink(missing_ok=True)

    def archive(
        self,
        tasks: Iterable[ConversionTask],
        metadata_paths: Iterable[Path],
        progress_callback: Optional[Callable[[int, int, Path], None]] = None,
    ) -> None:
        """Copy and verify every source and metadata file before USB mutation."""
        tasks = list(tasks)
        metadata_paths = list(metadata_paths)
        unique_originals = {
            str(task.source_abs_path.resolve()).casefold(): task.source_abs_path.resolve()
            for task in tasks
            if not task.adopt_existing_target
        }
        unique_targets = {
            str(task.target_abs_path.resolve()).casefold(): task.target_abs_path.resolve()
            for task in tasks
            if str(task.target_abs_path.resolve()).casefold()
            != str(task.source_abs_path.resolve()).casefold()
            and not task.reuse_existing_target
            and task.target_abs_path.is_file()
        }
        unique_metadata = {
            str(Path(path).resolve()).casefold(): Path(path).resolve()
            for path in metadata_paths
        }
        all_paths = (
            list(unique_originals.values())
            + list(unique_targets.values())
            + list(unique_metadata.values())
        )
        total_bytes = 2 * sum(path.stat().st_size for path in all_paths)
        completed_bytes = 0
        last_reported_bytes = 0

        def advance(amount: int, path: Path, force: bool = False) -> None:
            nonlocal completed_bytes, last_reported_bytes
            completed_bytes += amount
            if progress_callback and (
                force
                or completed_bytes == total_bytes
                or completed_bytes - last_reported_bytes >= 8 * 1024 * 1024
            ):
                progress_callback(completed_bytes, total_bytes, path)
                last_reported_bytes = completed_bytes

        originals_by_path: Dict[str, Dict] = {}
        for task in tasks:
            if task.adopt_existing_target:
                continue
            source = task.source_abs_path.resolve()
            key = str(source).casefold()
            if key not in originals_by_path:
                relative = self._relative_usb_path(source)
                archive_relative = Path("originals") / relative
                size, digest = self._copy_verified(
                    source,
                    self.session_dir / archive_relative,
                    progress_callback=lambda amount, path=source: advance(amount, path),
                )
                originals_by_path[key] = {
                    "usb_path": relative.as_posix(),
                    "archive_path": archive_relative.as_posix(),
                    "size": size,
                    "mtime_ns": source.stat().st_mtime_ns,
                    "sha256": digest,
                    "status": "archived",
                    "target_paths": [],
                }
                advance(0, source, force=True)
            target_path = task.target_usb_path.lstrip("/")
            if target_path not in originals_by_path[key]["target_paths"]:
                originals_by_path[key]["target_paths"].append(target_path)

        preexisting_targets: List[Dict] = []
        seen_targets = set()
        for task in tasks:
            target = task.target_abs_path.resolve()
            key = str(target).casefold()
            if (
                key in seen_targets
                or key == str(task.source_abs_path.resolve()).casefold()
                or task.reuse_existing_target
                or not target.is_file()
            ):
                continue
            seen_targets.add(key)
            relative = self._relative_usb_path(target)
            archive_relative = Path("preexisting_targets") / relative
            size, digest = self._copy_verified(
                target,
                self.session_dir / archive_relative,
                progress_callback=lambda amount, path=target: advance(amount, path),
            )
            preexisting_targets.append(
                {
                    "usb_path": relative.as_posix(),
                    "archive_path": archive_relative.as_posix(),
                    "size": size,
                    "mtime_ns": target.stat().st_mtime_ns,
                    "sha256": digest,
                    "status": "archived",
                }
            )
            advance(0, target, force=True)

        metadata_entries: List[Dict] = []
        seen_metadata = set()
        for source in metadata_paths:
            source = Path(source).resolve()
            key = str(source).casefold()
            if key in seen_metadata:
                continue
            seen_metadata.add(key)
            relative = self._relative_usb_path(source)
            archive_relative = Path("metadata") / relative
            size, digest = self._copy_verified(
                source,
                self.session_dir / archive_relative,
                progress_callback=lambda amount, path=source: advance(amount, path),
            )
            metadata_entries.append(
                {
                    "usb_path": relative.as_posix(),
                    "archive_path": archive_relative.as_posix(),
                    "size": size,
                    "sha256": digest,
                }
            )
            advance(0, source, force=True)

        self.manifest["originals"] = list(originals_by_path.values())
        self.manifest["preexisting_targets"] = preexisting_targets
        self.manifest["metadata"] = metadata_entries
        self.manifest["status"] = "archived"
        self._save_manifest()

    def archived_path(self, source: Path) -> Path:
        relative = self._relative_usb_path(source).as_posix()
        entry = next(
            (item for item in self.manifest["originals"] if item["usb_path"] == relative),
            None,
        )
        if not entry:
            raise ValueError(f"Source is missing from local backup manifest: {source}")
        archive = self._safe_join(
            self.session_dir, entry["archive_path"], "archive_path"
        )
        if not archive.is_file() or archive.stat().st_size != entry["size"]:
            raise ValueError(f"Archived original is missing or changed: {archive}")
        return archive

    def remove_originals_from_usb(
        self,
        progress_callback: Optional[Callable[[int, int, Path], None]] = None,
    ) -> None:
        removed: List[Dict] = []
        entries = self.manifest["originals"] + self.manifest["preexisting_targets"]
        total_bytes = 2 * sum(int(entry["size"]) for entry in entries)
        completed_bytes = 0
        last_reported_bytes = 0

        def advance(amount: int, path: Path, force: bool = False) -> None:
            nonlocal completed_bytes, last_reported_bytes
            completed_bytes += amount
            if progress_callback and (
                force
                or completed_bytes == total_bytes
                or completed_bytes - last_reported_bytes >= 8 * 1024 * 1024
            ):
                progress_callback(completed_bytes, total_bytes, path)
                last_reported_bytes = completed_bytes

        try:
            for entry in entries:
                source = self._safe_join(self.usb_root, entry["usb_path"], "usb_path")
                archive = self._safe_join(
                    self.session_dir, entry["archive_path"], "archive_path"
                )
                if self._sha256(
                    archive,
                    lambda amount, path=source: advance(amount, path),
                ) != entry["sha256"]:
                    raise OSError(f"Archived original failed verification: {archive}")
                if self._sha256(
                    source,
                    lambda amount, path=source: advance(amount, path),
                ) != entry["sha256"]:
                    raise OSError(f"USB original changed after archiving: {source}")
                source.unlink()
                self._sync_directory(source.parent)
                entry["status"] = "removed_from_usb"
                removed.append(entry)
                self._save_manifest()
                advance(0, source, force=True)
        except Exception:
            for entry in reversed(removed):
                try:
                    self._restore_entry(entry)
                except Exception:
                    pass
            self.manifest["status"] = "archive_failed"
            self._save_manifest()
            raise
        self.manifest["status"] = "converting"
        self._save_manifest()

    def _restore_entry(self, entry: Dict) -> None:
        source = self._safe_join(self.usb_root, entry["usb_path"], "usb_path")
        archive = self._safe_join(
            self.session_dir, entry["archive_path"], "archive_path"
        )
        if source.exists():
            if self._sha256(source) != entry["sha256"]:
                raise OSError(f"Refusing to overwrite changed USB original: {source}")
        else:
            size, digest = self._copy_verified(archive, source)
            if size != entry["size"] or digest != entry["sha256"]:
                raise OSError(f"Restored original failed verification: {source}")
        entry["status"] = "restored_after_failure"
        self._save_manifest()

    def restore_source_after_failure(self, source: Path) -> None:
        with self._lock:
            relative = self._relative_usb_path(source).as_posix()
            entry = next(
                item for item in self.manifest["originals"] if item["usb_path"] == relative
            )
            self._restore_entry(entry)

    def restore_task_after_failure(self, task: ConversionTask) -> None:
        """Restore a task's source and any target that existed before this session."""
        with self._lock:
            self.restore_source_after_failure(task.source_abs_path)
            relative_target = self._relative_usb_path(task.target_abs_path).as_posix()
            target_entry = next(
                (
                    item
                    for item in self.manifest["preexisting_targets"]
                    if item["usb_path"] == relative_target
                ),
                None,
            )
            if target_entry:
                self._restore_entry(target_entry)

    def mark_converted(self, task: ConversionTask) -> None:
        with self._lock:
            relative = self._relative_usb_path(task.source_abs_path).as_posix()
            entry = next(
                item for item in self.manifest["originals"] if item["usb_path"] == relative
            )
            target = task.target_abs_path
            entry["status"] = "converted"
            entry["converted_path"] = task.target_usb_path.lstrip("/")
            entry["converted_size"] = target.stat().st_size
            entry["converted_sha256"] = self._sha256(target)
            entry["converted_target_preexisting"] = task.reuse_existing_target
            self._save_manifest()

    def finish(self, success: bool) -> None:
        with self._lock:
            for entry in self.manifest["metadata"]:
                current = self._safe_join(
                    self.usb_root, entry["usb_path"], "usb_path"
                )
                if current.is_file():
                    entry["post_conversion_size"] = current.stat().st_size
                    entry["post_conversion_sha256"] = self._sha256(current)
            self.manifest["status"] = "complete" if success else "complete_with_errors"
            self._save_manifest()

    def restore_to_usb(self, usb_root: Optional[Path] = None) -> Tuple[bool, str]:
        """Restore archived audio and metadata after verifying current session outputs."""
        destination_root = Path(usb_root or self.usb_root).expanduser().resolve()
        if not destination_root.is_dir():
            return False, f"Selected USB root does not exist: {destination_root}"

        try:
            converted_by_path = {
                entry["converted_path"]: entry
                for entry in self.manifest["originals"]
                if entry.get("converted_path")
            }
            for entry in self.manifest["originals"]:
                archive = self._safe_join(
                    self.session_dir, entry["archive_path"], "archive_path"
                )
                if not archive.is_file() or self._sha256(archive) != entry["sha256"]:
                    raise ValueError(f"Archived original is missing or changed: {archive}")
                converted_path = entry.get("converted_path")
                if converted_path:
                    converted = self._safe_join(
                        destination_root, converted_path, "converted_path"
                    )
                    if converted.is_file() and self._sha256(converted) != entry.get(
                        "converted_sha256"
                    ):
                        raise ValueError(
                            f"Converted file changed after this backup was created: {converted}"
                        )
                original = self._safe_join(
                    destination_root, entry["usb_path"], "usb_path"
                )
                if (
                    original.is_file()
                    and entry.get("converted_path") != entry["usb_path"]
                    and self._sha256(original) != entry["sha256"]
                ):
                    raise ValueError(f"Original path contains an unrelated changed file: {original}")

            for entry in self.manifest["preexisting_targets"]:
                archive = self._safe_join(
                    self.session_dir, entry["archive_path"], "archive_path"
                )
                if not archive.is_file() or self._sha256(archive) != entry["sha256"]:
                    raise ValueError(f"Archived pre-existing target is missing or changed: {archive}")
                current = self._safe_join(
                    destination_root, entry["usb_path"], "usb_path"
                )
                if current.is_file():
                    current_digest = self._sha256(current)
                    converted_entry = converted_by_path.get(entry["usb_path"])
                    allowed_digests = {entry["sha256"]}
                    if converted_entry and converted_entry.get("converted_sha256"):
                        allowed_digests.add(converted_entry["converted_sha256"])
                    if current_digest not in allowed_digests:
                        raise ValueError(
                            f"Target changed after this backup was created: {current}"
                        )

            for entry in self.manifest["metadata"]:
                archive = self._safe_join(
                    self.session_dir, entry["archive_path"], "archive_path"
                )
                current = self._safe_join(
                    destination_root, entry["usb_path"], "usb_path"
                )
                if not archive.is_file() or self._sha256(archive) != entry["sha256"]:
                    raise ValueError(f"Archived metadata is missing or changed: {archive}")
                expected_current = entry.get("post_conversion_sha256")
                current_digest = self._sha256(current) if current.is_file() else ""
                if expected_current and current_digest not in {
                    expected_current,
                    entry["sha256"],
                }:
                    raise ValueError(
                        f"USB metadata changed after conversion; refusing stale restore: {current}"
                    )

            bytes_to_restore = sum(
                entry["size"]
                for entry in self.manifest["originals"]
                if not self._safe_join(
                    destination_root, entry["usb_path"], "usb_path"
                ).is_file()
                or entry.get("converted_path") == entry["usb_path"]
            )
            reclaimable = sum(
                self._safe_join(
                    destination_root, entry["converted_path"], "converted_path"
                ).stat().st_size
                for entry in self.manifest["originals"]
                if entry.get("converted_path")
                and not entry.get("converted_target_preexisting")
                and self._safe_join(
                    destination_root, entry["converted_path"], "converted_path"
                ).is_file()
            )
            free_space = shutil.disk_usage(destination_root).free
            if bytes_to_restore > free_space + reclaimable:
                raise ValueError(
                    "The USB does not have enough recoverable space to restore this archive."
                )

            affected_paths = set()
            for entry in self.manifest["originals"]:
                affected_paths.add(
                    self._safe_join(destination_root, entry["usb_path"], "usb_path")
                )
                converted_path = entry.get("converted_path")
                if converted_path and not entry.get("converted_target_preexisting"):
                    affected_paths.add(
                        self._safe_join(
                            destination_root, converted_path, "converted_path"
                        )
                    )
            affected_paths.update(
                self._safe_join(destination_root, entry["usb_path"], "usb_path")
                for entry in self.manifest["preexisting_targets"]
            )
            affected_paths.update(
                self._safe_join(destination_root, entry["usb_path"], "usb_path")
                for entry in self.manifest["metadata"]
            )
            manifest_before_restore = json.loads(json.dumps(self.manifest))

            # Keep rollback bytes off the USB so converted files may still be
            # reclaimed before originals are copied back on space-tight drives.
            with tempfile.TemporaryDirectory(
                prefix=".restore-rollback-", dir=self.session_dir
            ) as rollback_dir_name:
                rollback_dir = Path(rollback_dir_name)
                snapshots: Dict[Path, Optional[Path]] = {}
                for index, current in enumerate(sorted(affected_paths, key=str)):
                    if current.is_file():
                        snapshot = rollback_dir / f"{index:08d}.bin"
                        self._copy_verified(current, snapshot)
                        snapshots[current] = snapshot
                    else:
                        snapshots[current] = None

                try:
                    for entry in self.manifest["originals"]:
                        converted_path = entry.get("converted_path")
                        if converted_path and not entry.get("converted_target_preexisting"):
                            converted = self._safe_join(
                                destination_root, converted_path, "converted_path"
                            )
                            converted.unlink(missing_ok=True)
                            self._sync_directory(converted.parent)

                    for entry in self.manifest["originals"]:
                        original = self._safe_join(
                            destination_root, entry["usb_path"], "usb_path"
                        )
                        archive = self._safe_join(
                            self.session_dir, entry["archive_path"], "archive_path"
                        )
                        if not original.is_file():
                            size, digest = self._copy_verified(archive, original)
                            if size != entry["size"] or digest != entry["sha256"]:
                                raise OSError(f"Restored original failed verification: {original}")
                        entry["status"] = "restored"

                    for entry in self.manifest["preexisting_targets"]:
                        target = self._safe_join(
                            destination_root, entry["usb_path"], "usb_path"
                        )
                        archive = self._safe_join(
                            self.session_dir, entry["archive_path"], "archive_path"
                        )
                        size, digest = self._copy_verified(archive, target)
                        if size != entry["size"] or digest != entry["sha256"]:
                            raise OSError(
                                f"Restored pre-existing target failed verification: {target}"
                            )
                        entry["status"] = "restored"

                    # Restore the catalog last so it never durably points at
                    # audio or analysis files that have not yet been restored.
                    metadata_entries = sorted(
                        self.manifest["metadata"],
                        key=lambda entry: Path(entry["usb_path"]).name == "export.pdb",
                    )
                    for entry in metadata_entries:
                        archive = self._safe_join(
                            self.session_dir, entry["archive_path"], "archive_path"
                        )
                        current = self._safe_join(
                            destination_root, entry["usb_path"], "usb_path"
                        )
                        size, digest = self._copy_verified(archive, current)
                        if size != entry["size"] or digest != entry["sha256"]:
                            raise OSError(f"Restored metadata failed verification: {current}")

                    self.manifest["status"] = "restored"
                    self.manifest["restored_utc"] = datetime.now(timezone.utc).isoformat()
                    self._save_manifest()
                except Exception as restore_exc:
                    rollback_errors = []
                    self.manifest = manifest_before_restore
                    for current, snapshot in reversed(list(snapshots.items())):
                        try:
                            if snapshot is None:
                                current.unlink(missing_ok=True)
                                self._sync_directory(current.parent)
                            else:
                                self._copy_verified(snapshot, current)
                        except Exception as rollback_exc:
                            rollback_errors.append(f"{current}: {rollback_exc}")
                    if rollback_errors:
                        raise RuntimeError(
                            f"Restore failed: {restore_exc}; rollback also failed for "
                            + "; ".join(rollback_errors)
                        ) from restore_exc
                    raise RuntimeError(
                        f"Restore failed and the prior USB state was restored: {restore_exc}"
                    ) from restore_exc
            return True, f"Restored USB from local backup session: {self.session_dir}"
        except Exception as exc:
            return False, f"Local backup restore refused or failed: {exc}"

    @property
    def path(self) -> Path:
        return self.session_dir
