"""Detection and validation of Rekordbox USB drives and export directories."""

import os
import platform
from pathlib import Path
from typing import List, Optional, Tuple


class USBDetector:
    """Detects connected USB drives containing Rekordbox export structures."""

    @staticmethod
    def is_rekordbox_export_dir(path: Path) -> bool:
        """Determines if a directory contains a Rekordbox USB export structure."""
        path = Path(path)
        if not path.is_dir():
            return False

        has_pioneer = (path / "PIONEER").is_dir()
        has_pdb = (path / "PIONEER" / "rekordbox" / "export.pdb").is_file()
        has_dlp = (path / "PIONEER" / "DeviceLibraryPlus" / "exportLibrary.db").is_file() or (
            path / "PIONEER" / "rekordbox" / "exportLibrary.db"
        ).is_file()
        has_contents = (path / "Contents").is_dir()

        return (has_pioneer and (has_pdb or has_dlp)) or (has_pdb or has_contents)

    @classmethod
    def list_rekordbox_drives(cls) -> List[Tuple[Path, str]]:
        """Scans system mount points for Rekordbox exported drives.

        Returns list of (drive_path, label_or_name).
        """
        drives: List[Tuple[Path, str]] = []
        os_type = platform.system()

        if os_type == "Darwin":
            # macOS: /Volumes/*
            volumes_dir = Path("/Volumes")
            if volumes_dir.exists():
                for item in volumes_dir.iterdir():
                    if item.is_dir() and cls.is_rekordbox_export_dir(item):
                        drives.append((item, item.name))

        elif os_type == "Windows":
            # Windows: Drive letters A-Z
            import string
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:\\")
                if drive.exists() and cls.is_rekordbox_export_dir(drive):
                    drives.append((drive, f"Drive {letter}:"))

        else:
            # Linux: /media/*, /mnt/*, /run/media/$USER/*
            search_paths = [Path("/media"), Path("/mnt")]
            user = os.environ.get("USER")
            if user:
                search_paths.append(Path(f"/run/media/{user}"))

            for base in search_paths:
                if base.exists():
                    for item in base.glob("*/*"):
                        if item.is_dir() and cls.is_rekordbox_export_dir(item):
                            drives.append((item, item.name))
                    for item in base.glob("*"):
                        if item.is_dir() and cls.is_rekordbox_export_dir(item):
                            drives.append((item, item.name))

        return drives
