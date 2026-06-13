# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

# ── Kaynak klasör ──────────────────────────────────────────────────────────────
SRC = Path(r'C:\Users\rafet\Downloads\claud\LetterFormer_Alpha182_solid\alpha182')

# ── Programa dahil edilecek veri dosyaları ─────────────────────────────────────
datas = [
    (str(SRC / 'butonların tamamı.svg'),          '.'),
    (str(SRC / 'former ve prepare barı.svg'),      '.'),
    (str(SRC / 'F butonunun içi.svg'),             '.'),
    (str(SRC / 'fill des butonu.svg'),             '.'),
    (str(SRC / 'color buton yeni.svg'),            '.'),
    (str(SRC / 'mod 1 butonu.svg'),                '.'),
    (str(SRC / 'mod 2 butonu.svg'),                '.'),
    (str(SRC / 'only wall.svg'),                   '.'),
    (str(SRC / 'honeycomb fill.svg'),              '.'),
    (str(SRC / 'logo.png'),                        '.'),
    (str(SRC / 'logo yeni.png'),                   '.'),
    (str(SRC / 'exe, açılış ve kısayol için geçerli .svg'), '.'),
    (str(SRC / 'tüm logolar.svg'),                        '.'),
    (str(SRC / 'OrcaSlicer_Windows_V2.3.2_portable'), 'OrcaSlicer_Windows_V2.3.2_portable'),
]

hiddenimports = [
    'PySide6.QtSvg',
    'PySide6.QtOpenGL',
    'PySide6.QtOpenGLWidgets',
    'pyqtgraph.opengl',
    'OpenGL',
    'OpenGL.GL',
    'shapely',
    'shapely.geometry',
    'manifold3d',
    'mapbox_earcut',
    'ezdxf',
    'numpy',
    'lang',
    'orca_integration',
]

a = Analysis(
    [str(SRC / 'main.py')],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Splash — Analysis'ten sonra tanımlanmalı
splash = Splash(
    str(SRC / 'logo yeni.png'),
    binaries=a.binaries,
    datas=a.datas,
    text_pos=(10, 280),
    text_size=11,
    text_color='#dddddd',
    text_default='Harfex yükleniyor…',
    minify_script=True,
    always_on_top=True,
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    splash.binaries,
    [],
    exclude_binaries=True,
    name='Harfex',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(SRC / 'harfex.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Harfex',
)
