# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['cli.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['core.adapters.claude_code', 'core.adapters.qwen_code', 'core.adapters.codewhale_tui', 'core.adapters.opencode'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Don't bundle base system libraries built by the (bleeding-edge) build
# machine's toolchain — e.g. libz.so.1 built here requires GLIBC_ABI_DT_RELR,
# which older-glibc target servers don't have. Every mainstream Linux system
# already ships these, so exclude them and let the binary resolve them from
# the target system's own library path at runtime instead.
_SYSTEM_LIB_EXCLUDES = ("libz.so", "libbz2.so", "liblzma.so", "libcrypt.so", "libreadline.so", "libncurses")
a.binaries = [b for b in a.binaries if not b[0].startswith(_SYSTEM_LIB_EXCLUDES)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='chatlistctl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
