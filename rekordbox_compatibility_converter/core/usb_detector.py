"""Detection and validation of Rekordbox USB drives and export directories."""

import os
import platform
from pathlib import Path
from typing import List, Tuple


class USBDetector:
    """Detects connected USB drives containing Rekordbox export structures."""

    @staticmethod
    def is_rekordbox_export_dir(path: Path) -> bool:
        """Determines if a directory contains a Rekordbox USB export structure."""
        path = Path(path)
        if not path.is_dir():
            return False

        has_pdb = (path / "PIONEER" / "rekordbox" / "export.pdb").is_file()
        has_dlp = (path / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db").is_file() or (
            path / "PIONEER" / "rekordbox" / "exportLibrary.db"
        ).is_file()
        return has_pdb or has_dlp

    @classmethod
    def list_rekordbox_drives(cls) -> List[Tuple[Path, str]]:
        """Scans system mount points for Rekordbox exported drives.

        Returns list of (drive_path, label_or_name).
        """
        drives: List[Tuple[Path, str]] = []
        seen = set()
        os_type = platform.system()

        def add_drive(path: Path, label: str) -> None:
            try:
                key = str(path.resolve()).casefold()
                if key not in seen and cls.is_rekordbox_export_dir(path):
                    seen.add(key)
                    drives.append((path, label))
            except OSError:
                return

        if os_type == "Darwin":
            # macOS: /Volumes/*
            volumes_dir = Path("/Volumes")
            if volumes_dir.exists():
                try:
                    for item in volumes_dir.iterdir():
                        if item.is_dir():
                            add_drive(item, item.name)
                except OSError:
                    pass

        elif os_type == "Windows":
            # Windows: Drive letters A-Z
            import string
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:\\")
                if drive.exists():
                    add_drive(drive, f"Drive {letter}:")

        else:
            # Linux: /media/*, /mnt/*, /run/media/$USER/*
            search_paths = [Path("/media"), Path("/mnt")]
            user = os.environ.get("USER")
            if user:
                search_paths.append(Path(f"/run/media/{user}"))

            for base in search_paths:
                if base.exists():
                    try:
                        for item in list(base.glob("*")) + list(base.glob("*/*")):
                            if item.is_dir():
                                add_drive(item, item.name)
                    except OSError:
                        continue

        return drives
