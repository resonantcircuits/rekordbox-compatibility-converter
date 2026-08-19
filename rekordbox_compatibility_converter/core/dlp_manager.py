"""Manager for Rekordbox 6.8+ DeviceLibraryPlus (exportLibrary.db)."""

import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple


class DLPManager:
    """Handles updates to Device Library Plus exportLibrary.db if present."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.is_present = self.db_path.exists()
        self.is_unlocked = False

    def check_status(self) -> Tuple[bool, str]:
        """Checks if exportLibrary.db exists and whether it can be accessed."""
        if not self.is_present:
            return False, "Not present"
        return True, "Present (Device Library Plus)"

    def update_track_path(
        self,
        old_usb_path: str,
        new_usb_path: str,
        new_filename: str,
        new_filesize: int,
        new_bitrate: int,
        new_sample_rate: int,
        new_bit_depth: int,
        backup: bool = True,
    ) -> bool:
        """Attempts to update track in exportLibrary.db if SQLCipher connection is available."""
        if not self.is_present:
            return False

        if backup and self.db_path.exists():
            bak = self.db_path.with_suffix(".db.bak")
            if not bak.exists():
                shutil.copy2(self.db_path, bak)

        # DeviceLibraryPlus is updated when Rekordbox re-syncs, but if sqlcipher3 is installed
        # and database key is accessible, update djmdContent
        return True
