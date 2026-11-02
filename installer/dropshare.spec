# PyInstaller spec for the Windows client: one-folder mode (packaging.md, FR-UPD-01).
# Run by scripts/build_client.ps1, which sets DROPSHARE_VERSION and DROPSHARE_BUILD_DIR (holding
# build_info.json and DropShare.ico) first. Windows only: PyInstaller can't cross-compile.

import os
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH).parent  # noqa: F821 - defined by PyInstaller
CLIENT = ROOT / "client" / "src" / "dropshare_client"
BUILD = Path(os.environ["DROPSHARE_BUILD_DIR"])
VERSION = os.environ["DROPSHARE_VERSION"]
PARTS = tuple(int(part) for part in VERSION.split(".")) + (0,)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=PARTS, prodvers=PARTS),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "StateLife"),
                        StringStruct("FileDescription", "StateLife DropShare"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", "DropShare"),
                        StringStruct("OriginalFilename", "DropShare.exe"),
                        StringStruct("ProductName", "StateLife DropShare"),
                        StringStruct("ProductVersion", VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(  # noqa: F821
    [str(CLIENT / "__main__.py")],
    datas=[
        # The server address and update publisher (FR-CLIENT-05, FR-UPD-03), beside the module.
        (str(BUILD / "build_info.json"), "dropshare_client"),
        (str(CLIENT / "resources"), "dropshare_client/resources"),
        # importlib.metadata.version("dropshare-client") is the version the app reports.
        *copy_metadata("dropshare-client"),
    ],
    hiddenimports=["keyring.backends.Windows"],  # chosen at runtime, so not found by analysis
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DropShare",
    console=False,
    icon=str(BUILD / "DropShare.ico"),
    version=version_info,
    upx=False,  # compressed executables trip antivirus heuristics
)
coll = COLLECT(exe, a.binaries, a.datas, name="DropShare", upx=False)  # noqa: F821
