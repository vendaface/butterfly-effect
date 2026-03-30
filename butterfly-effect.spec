# butterfly-effect.spec
# PyInstaller spec for Butterfly Effect
#
# Build:
#   pip install pyinstaller
#   pyinstaller butterfly-effect.spec
#
# Output: dist/butterfly-effect   (Linux/Mac binary)
#         dist/Butterfly Effect.app  (Mac, with --windowed)

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

block_cipher = None
SRC = Path(SPECPATH)   # directory containing this .spec file

# Flask and Werkzeug are discovered automatically through server.py's imports.
# collect_all() for those packages registers submodules in the wrong order and
# causes flask.__init__'s circular `from . import json` to fail. Don't use it.
#
# Playwright is NOT found through static analysis (monarch_client.py uses async
# APIs that PyInstaller can't trace), so collect_all is correct here.
playwright_datas, playwright_bins, playwright_hidden = collect_all('playwright')
webview_datas,    webview_bins,    webview_hidden    = collect_all('webview')

a = Analysis(
    [str(SRC / 'main.py')],
    pathex=[str(SRC)],
    binaries=[*playwright_bins, *webview_bins],
    datas=[
        # Web UI
        (str(SRC / 'templates'),        'templates'),
        (str(SRC / 'static'),           'static'),
        # Config example & version
        (str(SRC / 'config.yaml.example'), '.'),
        (str(SRC / 'VERSION'),          '.'),
        # Demo data for screenshots (demo_mode: true in config.yaml)
        (str(SRC / 'demo'),             'demo'),
        *playwright_datas,
        *webview_datas,
    ],
    hiddenimports=[
        *playwright_hidden,
        *webview_hidden,
        'webview.platforms.cocoa',   # macOS platform — not auto-detected
        # AI providers
        'anthropic',
        'openai',
        'google.genai',
        # Other deps
        'icalendar',
        'yaml',
        'dotenv',
        'requests',
        'websockets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep bundle lean — test frameworks not needed at runtime
        'pytest', 'unittest', 'doctest',
        'tkinter', 'matplotlib', 'numpy',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=True,   # store modules as files, not zip — fixes Flask relative imports
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='butterfly-effect',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,     # keep console so users can see startup errors
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(SRC / 'static' / 'icon.icns') if sys.platform == 'darwin' else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='butterfly-effect',
)

# ── macOS .app bundle (built when running on macOS) ──────────────────────────
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Butterfly Effect.app',
        icon=str(SRC / 'static' / 'icon.icns'),
        bundle_identifier='com.vendaface.butterfly-effect',
        info_plist={
            'CFBundleShortVersionString': Path(SPECPATH + '/VERSION').read_text().strip(),
            'CFBundleVersion':            Path(SPECPATH + '/VERSION').read_text().strip(),
            'NSHumanReadableCopyright':   'MIT License',
            'NSHighResolutionCapable':    True,
            'LSMinimumSystemVersion':     '13.0',
        },
    )
