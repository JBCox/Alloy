# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Alloy
Builds a standalone executable with all dependencies bundled.
"""

import os
import sys
from pathlib import Path

# Get the directory containing this spec file
ALLOY_DIR = os.path.dirname(os.path.abspath(SPECPATH))

# Analysis: Gather all dependencies
a = Analysis(
    [os.path.join(ALLOY_DIR, 'main.py')],
    pathex=[ALLOY_DIR],
    binaries=[],
    datas=[
        # Include config file as template
        (os.path.join(ALLOY_DIR, 'config.yaml'), '.'),
        # Include all GUI files
        (os.path.join(ALLOY_DIR, 'gui'), 'gui'),
    ],
    hiddenimports=[
        # Tkinter
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        # Rich console
        'rich',
        'rich.console',
        'rich.panel',
        'rich.table',
        'rich.live',
        'rich.progress',
        'rich.markdown',
        'rich.syntax',
        # Prompt toolkit
        'prompt_toolkit',
        'prompt_toolkit.completion',
        'prompt_toolkit.history',
        'prompt_toolkit.styles',
        'prompt_toolkit.key_binding',
        # YAML
        'yaml',
        # Standard library
        'concurrent.futures',
        'threading',
        'queue',
        'subprocess',
        'json',
        'pathlib',
        'dataclasses',
        'abc',
        'enum',
        're',
        'datetime',
        'platform',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary modules to reduce size
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'cv2',
        'tensorflow',
        'torch',
        'pytest',
        'unittest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# Create the PYZ archive
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# Create the executable
exe = EXE(
    pyz,
    a.scripts,
    [],  # Don't include binaries in exe for onedir mode
    exclude_binaries=True,
    name='Alloy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # Compress with UPX if available
    console=False,  # Windowed mode (no console window)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon path here if you have one: icon='alloy.ico'
)

# Collect all files into a directory
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Alloy',
)
