"""
Builds a Windows MSI installer for the Galcon GL6100 control tools.

The MSI bundles a private copy of Python plus all dependencies, so end users
do not need Python installed. It installs both the GUI and the CLI, and
creates Start Menu and Desktop shortcuts. Windows startup for the MQTT tray
bridge is controlled from the tray menu for the current user.

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
from cx_Freeze.command.bdist_msi import Binary, PyDialog, add_data, bdist_msi

sys.path.insert(0, str(Path(__file__).parent))
from control_galcon import APP_VERSION  # noqa: E402

VERSION = APP_VERSION
PRODUCT_NAME = "Galcon GL6100 Control"
SHORTCUT_NAME = "Galcon GL6100 Control"

# Stable across releases so future MSIs upgrade in place instead of
# installing side-by-side. Do not change this.
UPGRADE_CODE = "{6F3A1C24-9B5E-4D77-9E31-2A8C4B0D77E1}"

ROOT = Path(__file__).parent


class GalconBdistMsi(bdist_msi):
    def add_config(self):
        super().add_config()
        add_data(
            self.db,
            "Binary",
            [("StopGalconAppsScript", Binary(str(ROOT / "stop_galcon_apps.vbs")))],
        )
        add_data(
            self.db,
            "CustomAction",
            [("StopGalconApps", 6, "StopGalconAppsScript",
              "StopGalconApps")],
        )
        add_data(
            self.db,
            "InstallUISequence",
            [("StopGalconApps", None, 1299)],
        )

    def add_exit_dialog(self):
        dialog = PyDialog(
            self.db,
            "ExitDialog",
            x=self.x,
            y=self.y,
            w=self.width,
            h=self.height,
            attr=self.modal,
            title=self.title,
            first="Finish",
            default="Finish",
            cancel="Finish",
        )
        dialog.title("Completing the [ProductName] installer")
        add_data(
            self.db,
            "ControlCondition",
            [
                ("ExitDialog", "LaunchMqttOnFinish", "Hide",
                 'MaintenanceForm_Action="Remove"'),
                ("ExitDialog", "LaunchOnFinish", "Hide",
                 'MaintenanceForm_Action="Remove"'),
            ],
        )
        dialog.checkbox(
            "LaunchMqttOnFinish",
            15,
            180,
            300,
            20,
            3,
            "LAUNCHMQTT",
            "Run Galcon MQTT Bridge in the notification area",
            "LaunchOnFinish",
        )
        dialog.checkbox(
            "LaunchOnFinish",
            15,
            200,
            300,
            20,
            3,
            "LAUNCHAPP",
            "Launch the Galcon control GUI on finish",
            "Finish",
        )
        add_data(
            self.db,
            "ControlEvent",
            [
                ("ExitDialog", "Finish", "DoAction", "VSDCA_Launch",
                 "LAUNCHAPP=1", 1),
                ("ExitDialog", "Finish", "DoAction", "VSDCA_LaunchMqtt",
                 "LAUNCHMQTT=1", 2),
            ],
        )
        add_data(
            self.db,
            "CustomAction",
            [
                ("VSDCA_Launch", 226, "TARGETDIR",
                 "[TARGETDIR]GalconControlGUI.exe"),
                ("VSDCA_LaunchMqtt", 226, "TARGETDIR",
                 "[TARGETDIR]GalconMqttTray.exe"),
            ],
        )
        dialog.backbutton("< Back", "LaunchMqttOnFinish", active=False)
        dialog.cancelbutton("Cancel", "Back", active=False)
        dialog.text(
            "Description",
            15,
            235,
            320,
            20,
            0x30003,
            "Click the Finish button to exit the installer.",
        )
        button = dialog.nextbutton("Finish", "Cancel", name="Finish")
        button.event("EndDialog", "Return", "1", 3)

ARCH_TAG = {
    "AMD64": "x64",
    "ARM64": "arm64",
    "x86": "x86",
}.get(platform.machine(), platform.machine().lower())

include_files = [
    (str(ROOT / "README.md"), "README.md"),
    (str(ROOT / "LICENSE"), "LICENSE"),
    (str(ROOT / "galcon_device.example.json"), "galcon_device.example.json"),
    (str(ROOT / "galcon.ico"), "galcon.ico"),
]

build_exe_options = {
    "packages": ["asyncio", "bleak", "paho", "pystray", "PIL", "tkinter",
                 "queue", "threading", "json"],
    "includes": [
        "control_galcon",
        "http",
        "http.client",
        "PIL.Image",
        "PIL.ImageTk",
        "pystray._win32",
        "urllib.error",
        "urllib.request",
    ],
    "include_files": include_files,
    "excludes": ["test", "unittest", "pydoc_data"],
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
    (
        "StartMenuTrayShortcut",
        "ProgramMenuFolder",
        "Galcon MQTT Bridge",
        "TARGETDIR",
        "[TARGETDIR]GalconMqttTray.exe",
        None,
        "Run the Galcon MQTT bridge in the notification area",
        None,
        "[TARGETDIR]galcon.ico",
        0,
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
    "product_name": PRODUCT_NAME,
    "license_file": str(ROOT / "LICENSE.rtf"),
    "launch_on_finish": True,
    "all_users": True,
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
        icon=str(ROOT / "galcon.ico"),
        copyright="Copyright (C) 2026",
    ),
    Executable(
        script=str(ROOT / "control_galcon.py"),
        base=None,
        target_name="galcon-cli.exe",
        copyright="Copyright (C) 2026",
    ),
    Executable(
        script=str(ROOT / "galcon_mqtt.py"),
        base=None,
        target_name="galcon-mqtt.exe",
        copyright="Copyright (C) 2026",
    ),
    Executable(
        script=str(ROOT / "galcon_mqtt_tray.py"),
        base="Win32GUI",
        target_name="GalconMqttTray.exe",
        icon=str(ROOT / "galcon.ico"),
        copyright="Copyright (C) 2026",
    ),
]

setup(
    name=PRODUCT_NAME,
    version=VERSION,
    description="Galcon MQTT Bridge",
    options={
        "build_exe": build_exe_options,
        "bdist_msi": bdist_msi_options,
    },
    cmdclass={"bdist_msi": GalconBdistMsi},
    executables=executables,
)
