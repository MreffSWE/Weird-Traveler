"""
build.py — build Weird Traveler as a single self-contained exe.

Usage:
    python build.py

Output:
    dist/Weird Traveler.exe

The exe acts as both the browser and the viewer:
    "Weird Traveler.exe"               -> opens the file browser
    "Weird Traveler.exe"  C:\\Photos   -> browser starting in that folder
    "Weird Traveler.exe"  --viewer img -> opens the image viewer

Requirements:
    pip install pyinstaller
"""

import subprocess, sys, shutil
from pathlib import Path

HERE = Path(__file__).parent
DIST = HERE / "dist"
WORK = HERE / "build"
ICON = HERE / "wif_icon.ico"

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    f"--distpath={DIST}",
    f"--workpath={WORK}",
    f"--specpath={WORK}",
    "--onefile",
    "--windowed",
    f"--icon={ICON}",
    "--name=Weird Traveler",
    "--hidden-import=PIL._tkinter_finder",
    "--collect-submodules=PIL",
    str(HERE / "wif_main.py"),
]

print("Building Weird Traveler.exe …")
result = subprocess.run(cmd)
if result.returncode != 0:
    print("Build failed.")
    sys.exit(result.returncode)

exe = DIST / "Weird Traveler.exe"
print(f"\nDone.  {exe}  ({exe.stat().st_size / 1024**2:.1f} MB)")
