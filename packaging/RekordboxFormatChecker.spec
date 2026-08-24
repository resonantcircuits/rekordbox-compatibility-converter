"""Cross-platform PyInstaller definition for the desktop application."""

import os
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


PROJECT_ROOT = Path(SPEC).resolve().parent.parent
APP_NAME = "RekordboxFormatChecker"
APP_DISPLAY_NAME = "Rekordbox Format Checker"
APP_IDENTIFIER = "com.resonantcircuits.rekordbox-format-checker"


def project_version():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in pyproject.splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("Could not determine project version from pyproject.toml")


def required_tool(name):
    configured = os.environ.get(f"RBCONVERT_{name.upper()}_PATH")
    resolved = configured or shutil.which(name)
    if not resolved or not Path(resolved).is_file():
        raise RuntimeError(
            f"{name} was not found; set RBCONVERT_{name.upper()}_PATH before building"
        )
    return str(Path(resolved).resolve())


def required_file(environment_name):
    configured = os.environ.get(environment_name)
    if not configured or not Path(configured).is_file():
        raise RuntimeError(f"Set {environment_name} to an existing file before building")
    return str(Path(configured).resolve())


VERSION = project_version()
VERSION_PARTS = tuple(int(part) for part in VERSION.split("."))
FILE_VERSION = (VERSION_PARTS + (0, 0, 0, 0))[:4]

ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all("customtkinter")
runtime_binaries = ctk_binaries + [
    (required_tool("ffmpeg"), "."),
    (required_tool("ffprobe"), "."),
]
runtime_data = ctk_datas + [
    (str(PROJECT_ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt"), "."),
    (required_file("RBCONVERT_FFMPEG_LICENSE_PATH"), "licenses/ffmpeg"),
    (required_file("RBCONVERT_FFMPEG_BUILD_INFO_PATH"), "licenses/ffmpeg"),
]

a = Analysis(
    [str(PROJECT_ROOT / "rekordbox_compatibility_converter" / "gui" / "app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=runtime_binaries,
    datas=runtime_data,
    hiddenimports=ctk_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name=APP_NAME,
    )
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier=APP_IDENTIFIER,
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": APP_DISPLAY_NAME,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
        },
    )
elif sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=FILE_VERSION,
            prodvers=FILE_VERSION,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "Resonant Circuits"),
                            StringStruct("FileDescription", APP_DISPLAY_NAME),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("InternalName", APP_NAME),
                            StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                            StringStruct("ProductName", APP_DISPLAY_NAME),
                            StringStruct("ProductVersion", VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        version=version_info,
    )
else:
    raise RuntimeError(f"Desktop packaging is not configured for {sys.platform}")
