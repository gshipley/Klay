# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ["../../_build/klay/klay"],
    pathex=[],
    binaries=[],
    datas=[("../../_build/data/klay.gresource", "Resources")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={
        "gi": {
            "module-versions": {
                "Gtk": "4.0",
            },
        },
    },
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
    name="Klay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Klay",
)
app = BUNDLE(
    coll,
    name="Klay.app",
    icon="./icon.icns",
    bundle_identifier="org.kde.Klay",
    info_plist={
        "LSApplicationCategoryType": "public.app-category.games",
    },
)
