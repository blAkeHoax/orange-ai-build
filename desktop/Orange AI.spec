# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all('customtkinter')
dp_datas, dp_binaries, dp_hiddenimports = collect_all('dateparser')

datas = [('assets', 'assets')] + ctk_datas + dp_datas
binaries = [] + ctk_binaries + dp_binaries
hiddenimports = [
    'customtkinter',
    'dateparser',
    'PIL',
    'PIL.Image',
    'PIL.ImageTk',
    'sqlite3',
    'dotenv',
    'urllib.request',
    'urllib.error',
    'json',
] + ctk_hiddenimports + dp_hiddenimports

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Orange AI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='assets/app_icon.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Orange AI can run without elevation. Requiring it prevents a normal
    # console launch and hides the useful startup traceback behind UAC.
    uac_admin=False,
)
