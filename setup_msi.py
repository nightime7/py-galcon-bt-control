"""
Builds a Windows MSI installer for the Galcon GL6100 control tools.

The MSI bundles a private copy of Python plus all dependencies, so end users
do not need Python installed. It installs both the GUI and the CLI, and
creates Start Menu and Desktop shortcuts for the GUI.

cx_Freeze builds for the architecture of the interpreter running it - an x64
Python produces an x64 MSI, an ARM64 Python produces an ARM64 MSI. There is
no cross-compilation, because the bundled interpreter and the native winrt
extension modules that bleak depends on are architecture-specific.

Usage
-----
    python setup_msi.py bdist_msi
"""

import platform
import sys
from pathlib import Path

from cx_Freeze import Executable, setup

VERSION = "1.1.0"
PRODUCT_NAME = "Galcon GL6100 Control"
SHORTCUT_NAME = "Galcon GL6100 Control"

# Stable across releases so future MSIs upgrade in place instead of
# installing side-by-side. Do not change this.
UPGRADE_CODE = "{6F3A1C24-9B5E-4D77-9E31-2A8C4B0D77E1}"

ROOT = Path(__file__).parent

ARCH_TAG = {
    "AMD64": "x64",
    "ARM64": "arm64",
    "x86": "x86",
}.get(platform.machine(), platform.machine().lower())

include_files = [
    (str(ROOT / "README.md"), "README.md"),
    (str(ROOT / "LICENSE"), "LICENSE"),
    (str(ROOT / "galcon_device.example.json"), "galcon_device.example.json"),
]

build_exe_options = {
    "packages": ["asyncio", "bleak", "tkinter", "queue", "threading", "json"],
    "includes": ["control_galcon"],
    "include_files": include_files,
    "excludes": ["test", "unittest", "pydoc_data", "email", "http", "xml"],
    "include_msvcr": True,
}

shortcut_table = [
    (
        "StartMenuShortcut",          # Shortcut
        "ProgramMenuFolder",          # Directory_
        SHORTCUT_NAME,                # Name
        "TARGETDIR",                  # Component_
        "[TARGETDIR]GalconControlGUI.exe",  # Target
        None,                         # Arguments
        PRODUCT_NAME,                 # Description
        None,                         # Hotkey
        None,                         # Icon
        None,                         # IconIndex
        None,                         # ShowCmd
        "TARGETDIR",                  # WkDir
    ),
    (
        "DesktopShortcut",
        "DesktopFolder",
        SHORTCUT_NAME,
        "TARGETDIR",
        "[TARGETDIR]GalconControlGUI.exe",
        None,
        PRODUCT_NAME,
        None,
        None,
        None,
        None,
        "TARGETDIR",
    ),
]

bdist_msi_options = {
    "upgrade_code": UPGRADE_CODE,
    "add_to_path": False,
    "initial_target_dir": rf"[ProgramFiles64Folder]\{PRODUCT_NAME}"
    if ARCH_TAG != "x86"
    else rf"[ProgramFilesFolder]\{PRODUCT_NAME}",
    "output_name": f"GalconGL6100Control-{VERSION}-{ARCH_TAG}.msi",
    "data": {"Shortcut": shortcut_table},
    "summary_data": {
        "author": "nightime7",
        "comments": PRODUCT_NAME,
    },
}

executables = [
    Executable(
        script=str(ROOT / "galcon_gui.py"),
        base="Win32GUI",
        target_name="GalconControlGUI.exe",
        copyright="Copyright (C) 2026",
    ),
    Executable(
        script=str(ROOT / "control_galcon.py"),
        base=None,
        target_name="galcon-cli.exe",
        copyright="Copyright (C) 2026",
    ),
]

setup(
    name=PRODUCT_NAME,
    version=VERSION,
    description="Unofficial control tools for Galcon GL6100 BLE irrigation valves",
    options={
        "build_exe": build_exe_options,
        "bdist_msi": bdist_msi_options,
    },
    executables=executables,
)
