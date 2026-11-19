# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all

# Dynamically resolve paths relative to SPECPATH (location of this spec file)
# In repo structure: SPECPATH is <repo_root>/tauri/scripts
tauri_dir = os.path.abspath(os.path.join(SPECPATH, ".."))
workspace_root = os.path.abspath(os.path.join(tauri_dir, ".."))

# Resolve desktop directory supporting both repo layout ('desktop') and local workspace ('Orange AI Desktop')
desktop_candidates = [
    os.path.join(workspace_root, "desktop"),
    os.path.join(workspace_root, "Orange AI Desktop"),
]
desktop_dir = next((d for d in desktop_candidates if os.path.exists(d)), desktop_candidates[0])
runtime_dir = os.path.join(tauri_dir, "runtime")

dp_datas, dp_binaries, dp_hiddenimports = collect_all('dateparser')

datas = [
    (os.path.join(desktop_dir, 'assets'), 'assets'),
] + dp_datas

binaries = [] + dp_binaries

hiddenimports = [
    'dateparser',
    'sqlite3',
    'dotenv',
    'urllib.request',
    'urllib.error',
    'json',
    'pydantic',
    'pydantic_core',
    'cryptography',
    'requests',
    'psutil',
    'PIL',
    'PIL.Image',
    'brain',
    'brain.personality',
    'brain.router',
    'brain.intents',
    'brain.dispatcher',
    'brain.logger',
    'brain.api_client',
    'brain.memory_classifier',
    'brain.memory',
    'brain.scheduler',
    'brain.mood',
    'brain.planner',
    'brain.agent',
    'brain.agent_models',
    'brain.agent_context',
    'brain.conflict_resolver',
    'brain.context_budget',
    'brain.database',
    'brain.paths',
    'brain.reminders',
    'brain.reminder_parser',
    'brain.web_search',
    'brain.event_store',
    'brain.event_readers',
    'brain.daily_briefing',
    'brain.recommendations',
    'brain.health',
    'brain.briefing',
    'brain.briefing.models',
    'brain.briefing.collector',
    'brain.briefing.synthesizer',
    'brain.briefing.settings',
    'brain.briefing.recommendations',
    'brain.briefing.reconciliation',
    'brain.briefing.project_health',
    'brain.briefing.event_delivery',
    'brain.briefing.event_readers',
    'brain.briefing.event_store',
    'gui.settings',
    'tools',
    'tools.registry',
    'tools.permissions',
    'tools.system',
    'tools.apps',
    'tools.command',
    'tools.files',
    'tools.processes',
    'tools.todo',
    'main',
] + dp_hiddenimports

a = Analysis(
    [os.path.join(runtime_dir, 'runtime_adapter.py')],
    pathex=[desktop_dir, runtime_dir],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'customtkinter',
        'tkinter',
        '_tkinter',
        'Tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='runtime_adapter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='runtime',
)
