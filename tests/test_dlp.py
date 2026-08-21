"""Tests for actionable OneLibrary safety guidance."""

from rekordbox_compatibility_converter.core.dlp_manager import (
    ONELIBRARY_BRIDGE_CONFIRM_MESSAGE,
    ONELIBRARY_BRIDGE_PROMPT,
    ONELIBRARY_REBUILD_REQUIRED_MESSAGE,
)


def test_onelibrary_prompt_explains_both_user_stages():
    assert "STEP 1" in ONELIBRARY_BRIDGE_PROMPT
    assert "This app does the conversion" in ONELIBRARY_BRIDGE_PROMPT
    assert "STEP 2" in ONELIBRARY_BRIDGE_PROMPT
    assert "You finish the update in Rekordbox" in ONELIBRARY_BRIDGE_PROMPT
    assert "left sidebar" in ONELIBRARY_BRIDGE_PROMPT
    assert "Convert from Device Library" in ONELIBRARY_BRIDGE_PROMPT
    assert "do not use this USB" in ONELIBRARY_BRIDGE_PROMPT


def test_onelibrary_confirmation_says_rekordbox_work_remains():
    assert "Start Step 1" in ONELIBRARY_BRIDGE_CONFIRM_MESSAGE
    assert "does not update OneLibrary" in ONELIBRARY_BRIDGE_CONFIRM_MESSAGE
    assert "complete Step 2" in ONELIBRARY_BRIDGE_CONFIRM_MESSAGE


def test_onelibrary_completion_lists_exact_rekordbox_actions():
    assert "STEP 1 OF 2 IS COMPLETE" in ONELIBRARY_REBUILD_REQUIRED_MESSAGE
    assert "WHAT YOU NEED TO DO NOW" in ONELIBRARY_REBUILD_REQUIRED_MESSAGE
    assert "expand Devices" in ONELIBRARY_REBUILD_REQUIRED_MESSAGE
    assert "Right-click OneLibrary" in ONELIBRARY_REBUILD_REQUIRED_MESSAGE
    assert "Click Convert from Device Library" in ONELIBRARY_REBUILD_REQUIRED_MESSAGE
    assert "Remove Retained Originals" in ONELIBRARY_REBUILD_REQUIRED_MESSAGE
    assert "Never delete every FLAC file manually" in ONELIBRARY_REBUILD_REQUIRED_MESSAGE
    assert "Eject the USB safely" in ONELIBRARY_REBUILD_REQUIRED_MESSAGE
