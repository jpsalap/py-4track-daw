# -*- mode: python ; coding: utf-8 -*-
import os
import sys

_portaudio_candidates = [
    os.path.join(sys.prefix, "lib", "libportaudio.dylib"),
    "/opt/homebrew/opt/portaudio/lib/libportaudio.dylib",
    "/usr/local/opt/portaudio/lib/libportaudio.dylib",
]
extra_binaries = []
for _candidate in _portaudio_candidates:
    if os.path.exists(_candidate):
        extra_binaries.append((_candidate, "."))
        break

_dsp_lib = os.path.join(SPECPATH, "libminidaw_dsp.dylib")
if os.path.exists(_dsp_lib):
    extra_binaries.append((_dsp_lib, "."))

a = Analysis(
    ["window.py"],
    pathex=[],
    binaries=extra_binaries,
    datas=[],
    hiddenimports=[
        "pyaudio",
        "dsp_accel",
        "wav_io",
        "MasterLimiter",
        "themes",
        "Delay",
        "Tremolo",
        "Chorus",
        "Compressor",
        "fx_dialog_base",
        "ui_widgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "_tkinter",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtCharts",
        "PySide6.QtDBus",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtNetwork",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPositioning",
        "PySide6.QtPrintSupport",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtSensors",
        "PySide6.QtSql",
        "PySide6.QtSvg",
        "PySide6.QtTest",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets",
        "PySide6.QtXml",
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
    name="Mini-DAW",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="Mini-DAW.icns",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Mini-DAW",
)

app = BUNDLE(
    coll,
    name="Mini-DAW.app",
    icon="Mini-DAW.icns",
    bundle_identifier="com.juansalazar.minidaw",
    info_plist={
        "CFBundleName": "Mini-DAW",
        "CFBundleDisplayName": "Mini-DAW",
        "CFBundleShortVersionString": "2.0.0",
        "CFBundleVersion": "2.0.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Mini-DAW needs microphone access to record audio tracks.",
    },
)
