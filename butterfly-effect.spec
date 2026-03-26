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

block_cipher = None
SRC = Path(SPECPATH)   # directory containing this .spec file

a = Analysis(
    [str(SRC / 'main.py')],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        # Web UI
        (str(SRC / 'templates'),        'templates'),
        (str(SRC / 'static'),           'static'),
        (str(SRC / 'startup.html'),     '.'),
        # Config example & version
        (str(SRC / 'config.yaml.example'), '.'),
        (str(SRC / 'VERSION'),          '.'),
    ],
    hiddenimports=[
        # Flask stack
        'flask',
        'jinja2',
        'jinja2.ext',
        'werkzeug',
        'werkzeug.routing',
        'werkzeug.serving',
        # Playwright — async branch used by monarch_client.py
        'playwright',
        'playwright.async_api',
        'playwright._impl._async_base',
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
    noarchive=False,
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
    icon=str(SRC / 'static' / 'favicon.svg') if sys.platform == 'darwin' else None,
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
        icon=None,   # replace with an .icns file once one exists
        bundle_identifier='com.vendaface.butterfly-effect',
        info_plist={
            'CFBundleShortVersionString': Path(SPECPATH + '/VERSION').read_text().strip(),
            'CFBundleVersion':            Path(SPECPATH + '/VERSION').read_text().strip(),
            'NSHumanReadableCopyright':   'MIT License',
            'NSHighResolutionCapable':    True,
            'LSMinimumSystemVersion':     '13.0',
        },
    )
