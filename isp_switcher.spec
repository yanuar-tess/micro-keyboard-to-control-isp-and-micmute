# PyInstaller build (used by run.ps1):
#   uv run --group build pyinstaller isp_switcher.spec --noconfirm --clean --distpath dist --workpath build
#
# One analysis, two launchers in dist\isp_switcher\ sharing _internal\:
#   isp_switcher.exe     console: interactive runs (run.ps1)
#   isp_switcher_bg.exe  no window: the logon task (install-task.ps1); output goes to the log file
# --onedir style on purpose: no self-extracting stub, which antivirus flags far less.

a = Analysis(
    ['isp_switcher.py'],
    pathex=[],
    # pynput picks its backend at runtime, so its Windows modules must be named.
    hiddenimports=['pynput.keyboard._win32', 'pynput.mouse._win32'],
    excludes=['tkinter'],
)
pyz = PYZ(a.pure)

common = dict(exclude_binaries=True, debug=False, strip=False, upx=False)
exe_console = EXE(pyz, a.scripts, [], name='isp_switcher', console=True, **common)
exe_bg = EXE(pyz, a.scripts, [], name='isp_switcher_bg', console=False, **common)

coll = COLLECT(exe_console, exe_bg, a.binaries, a.datas, strip=False, upx=False,
               name='isp_switcher')
