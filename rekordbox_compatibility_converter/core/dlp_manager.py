"""Detection and safety policy for OneLibrary exports."""

from pathlib import Path
from typing import Tuple


ONELIBRARY_PRESENT_MESSAGE = (
    "This USB contains OneLibrary (formerly Device Library Plus) alongside the traditional "
    "Device Library. No files were changed.\n\n"
    "Why conversion is disabled: this version can "
    "update only Device Library; OneLibrary would retain the old audio paths, so equipment "
    "that reads OneLibrary could show missing tracks.\n\n"
    "What to do: do not manually delete exportLibrary.db from a working USB. If Rekordbox "
    "offers OneLibrary conversion, you may decline it for a separate USB intended only for "
    "legacy Device Library players, then verify that the USB has PIONEER/rekordbox/export.pdb "
    "and no exportLibrary.db before scanning again. To test the Rekordbox rebuild workflow on "
    "a complete USB copy, explicitly choose the experimental OneLibrary bridge. Simply "
    "re-exporting may create OneLibrary again."
)

ONELIBRARY_ONLY_MESSAGE = (
    "This USB contains only OneLibrary (formerly Device Library Plus), not "
    "PIONEER/rekordbox/export.pdb. No files were changed.\n\nThis version cannot synchronize "
    "OneLibrary. Newer OneLibrary-only equipment will not reliably fall back to the traditional "
    "Device Library, so removing exportLibrary.db is not a safe workaround. Use a separately "
    "exported legacy Device Library-only USB only with equipment that supports Device Library."
)

ONELIBRARY_BRIDGE_PROMPT = (
    "Rekordbox has stored two separate track catalogs on this USB. Older players read Device "
    "Library; newer players read OneLibrary. This app can update only the older catalog, so "
    "finishing this USB requires two steps.\n\n"
    "STEP 1 — This app does the conversion\n"
    "Click Yes below. After the scan finishes, click Convert Tracks (Step 1 of 2). The app will "
    "copy every affected original to a verified local backup, remove those originals from the "
    "USB, convert the audio, and update Device Library.\n\n"
    "STEP 2 — You finish the update in Rekordbox\n"
    "After this app reports that Step 1 is complete:\n"
    "1. Keep the USB connected and open the latest Rekordbox.\n"
    "2. Do not export or synchronize anything to the USB first.\n"
    "3. In Rekordbox's left sidebar, expand Devices and find this USB.\n"
    "4. Right-click OneLibrary (Device Library Plus in Rekordbox 6).\n"
    "5. Click Convert from Device Library and approve the overwrite warnings.\n"
    "6. Open OneLibrary under the USB and verify the converted tracks.\n\n"
    "AFTER STEP 2 — Keep the recovery archive\n"
    "Verify the rebuilt OneLibrary and test the USB on the intended players before removing the "
    "local backup session.\n\n"
    "Until Step 2 is complete, do not use this USB on a OneLibrary player. Rekordbox may remove "
    "playlists or histories stored only in OneLibrary when it overwrites that catalog. Test on "
    "a complete copy of the USB.\n\n"
    "Start the Step 1 scan now?"
)

ONELIBRARY_BRIDGE_CONFIRM_MESSAGE = (
    "Start Step 1 of the two-step update?\n\n"
    "This app will convert the selected tracks and update Device Library and the waveform files. "
    "It will first verify every original in the selected local recovery folder, then remove the "
    "USB originals to make room. Device Library and waveform backups are stored in that session.\n\n"
    "This step does not update OneLibrary. When it finishes, follow the on-screen Rekordbox "
    "instructions to complete Step 2. Continue only on a complete copy of the USB."
)

ONELIBRARY_REBUILD_REQUIRED_MESSAGE = (
    "STEP 1 OF 2 IS COMPLETE\n\n"
    "The converted files and the older Device Library catalog are ready, but OneLibrary still "
    "contains the old track links. Do not use this USB on a OneLibrary player yet.\n\n"
    "WHAT YOU NEED TO DO NOW\n"
    "1. Keep the USB connected and open the latest Rekordbox.\n"
    "2. Do not export or synchronize anything to the USB first.\n"
    "3. In Rekordbox's left sidebar, expand Devices and find this USB.\n"
    "4. Right-click OneLibrary (Device Library Plus in Rekordbox 6).\n"
    "5. Click Convert from Device Library.\n"
    "6. Approve Rekordbox's overwrite warnings.\n"
    "7. Open OneLibrary under the USB and verify that the converted tracks appear.\n"
    "8. Eject the USB safely before testing it on equipment.\n\n"
    "Original audio is preserved in the local recovery archive shown by this app, not on the USB. "
    "Keep that folder until Rekordbox and hardware testing are complete. Rekordbox may remove "
    "playlists or histories stored only in OneLibrary when it overwrites that catalog."
)


class DLPManager:
    """Detects OneLibrary and prevents unsupported mutation."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.is_present = self.db_path.exists()
        self.is_unlocked = False

    def check_status(self) -> Tuple[bool, str]:
        """Checks if exportLibrary.db exists and whether it can be accessed."""
        if not self.is_present:
            return False, "Not present"
        return False, ONELIBRARY_PRESENT_MESSAGE

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
