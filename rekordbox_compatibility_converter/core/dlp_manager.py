"""Detection and safety policy for Device Library Plus exports."""

from pathlib import Path
from typing import Tuple


class DLPManager:
    """Detects Device Library Plus and prevents unsupported mutation."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.is_present = self.db_path.exists()
        self.is_unlocked = False

    def check_status(self) -> Tuple[bool, str]:
        """Checks if exportLibrary.db exists and whether it can be accessed."""
        if not self.is_present:
            return False, "Not present"
        return False, (
            "Device Library Plus is present, but safe exportLibrary.db updates "
            "are not implemented in this version."
        )

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
        """Refuses to claim an update until the encrypted schema is supported."""
        return False
