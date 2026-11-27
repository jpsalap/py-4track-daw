# -*- mode: python ; coding: utf-8 -*-
import os
import sys

portaudio_candidates = [
    os.path.join(sys.prefix, "lib", "libportaudio.dylib"),
    "/opt/homebrew/lib/libportaudio.dylib",
    "/opt/homebrew/opt/portaudio/lib/libportaudio.dylib",
    "/usr/local/lib/libportaudio.dylib",
    "/usr/local/opt/portaudio/lib/libportaudio.dylib",
]
portaudio_lib = next((p for p in portaudio_candidates if os.path.exists(p)), None)
extra_binaries = [(portaudio_lib, ".")] if portaudio_lib else []

a = Analysis(
    ["window.py"],
    pathex=[],
    binaries=extra_binaries,
    datas=[],
    hiddenimports=["pyaudio", "Delay", "Tremolo", "Chorus", "Compressor"],
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
        "CFBundleShortVersionString": "1.1.0",
        "CFBundleVersion": "1.1.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Mini-DAW needs microphone access to record audio tracks.",
    },
)
