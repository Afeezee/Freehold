# PyInstaller build for the single-file Windows executable.
#
#   pip install pyinstaller
#   pyinstaller packaging/Freehold.spec --noconfirm
#
# Produces dist/Freehold.exe - no Python installation required on the
# machine that runs it, and no credentials of any kind baked in.

import os

block_cipher = None

a = Analysis(
    ["entry.py"],
    pathex=[os.path.abspath(os.path.join(SPECPATH, ".."))],
    binaries=[],
    # The dashboard's page is loaded from disk at runtime, so it has to
    # travel inside the executable.
    datas=[("../freehold/dashboard/static", "freehold/dashboard/static")],
    hiddenimports=[
        "freehold.drivers.local",
        "freehold.drivers.github",
        "freehold.drivers.s3",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "numpy", "PIL"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Freehold",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # A console window: this prints the dashboard URL and the warning
    # about the encryption key, both of which the user must see.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="../brand/Freehold.ico",
)
