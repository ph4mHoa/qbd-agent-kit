"""Builds the Windows download: one folder with Python, pywin32 and the kit, so qbd runs on the QuickBooks PC with
nothing to install. QuickBooks accepts the bundled python.exe like any other python.org build: QuickBooks
recognizes a program by its publisher, and python.exe is signed by the Python Software Foundation.

usage: python scripts/build-windows-zip.py <python-X.Y.Z-embed-amd64.zip> <pywin32-NNN-cpXY-cpXY-win_amd64.whl> [out dir]

Download both first, for the same Python version and bitness: the embeddable package from
https://www.python.org/downloads/windows/ and the pywin32 wheel from https://pypi.org/project/pywin32/#files.
QuickBooks 2022 and later are 64-bit (amd64); older versions need the 32-bit (win32) files. Writes
<out dir>/qbd-agent-kit-<version>-windows-<bitness>.zip (out dir: dist).
"""
from __future__ import annotations

import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qbd_agent_kit import __version__  # noqa: E402

WHEEL_PLATFORM = {"amd64": "win_amd64", "win32": "win32"}
BITNESS = {"amd64": "x64", "win32": "x86"}

README = """qbd-agent-kit {version} for Windows, with Python {python} ({bitness}) and pywin32 inside.

QuickBooks Desktop for AI agents: a command line that runs on this PC and prints JSON.
Guide: https://github.com/ph4mHoa/qbd-agent-kit/blob/main/docs/setup.md

1. Unzip to a short path without spaces, for example C:\\qbd.
2. Try it without QuickBooks:   C:\\qbd\\qbd.cmd daily-brief --demo --pretty
3. Open QuickBooks with the company file, log in as Admin, and run:   C:\\qbd\\qbd.cmd authorize
   Allow "QBD Agent Kit" in the window QuickBooks opens (it may be behind other windows).
4. Check the connection:   C:\\qbd\\qbd.cmd doctor
5. So an agent can type just qbd, add C:\\qbd to your Path: Start > "Edit environment variables for your
   account" > Path > Edit > New. qbd.cmd serves Command Prompt and PowerShell, qbd serves Git Bash.

To update, unzip the new version over this folder; QuickBooks keeps its permission.
"""


def build(python_zip: Path, wheel: Path, out: Path) -> Path:
    py = re.fullmatch(r"python-(\d+)\.(\d+)\.(\d+)-embed-(amd64|win32)\.zip", python_zip.name)
    whl = re.fullmatch(r"pywin32-\d+-cp(\d+)-cp\d+-(\w+)\.whl", wheel.name)
    if not py or not whl:
        sys.exit(__doc__)
    arch = py[4]
    if whl[1] != py[1] + py[2] or whl[2] != WHEEL_PLATFORM[arch]:
        sys.exit(f"{wheel.name} does not fit {python_zip.name}: use the cp{py[1]}{py[2]} {WHEEL_PLATFORM[arch]} wheel.")
    name = f"qbd-agent-kit-{__version__}-windows-{BITNESS[arch]}"
    stage = out / name
    shutil.rmtree(stage, ignore_errors=True)
    home = stage / "python"
    with zipfile.ZipFile(python_zip) as z:
        z.extractall(home)
    # The embeddable package ignores PYTHONPATH and site-packages until its ._pth file names them.
    pth = next(home.glob("python*._pth"))
    lines = [x for x in pth.read_text(encoding="utf-8").splitlines() if x.strip() not in ("#import site", "import site")]
    pth.write_bytes("\r\n".join([*lines, "Lib\\site-packages", "import site", ""]).encode("utf-8"))
    site = home / "Lib" / "site-packages"
    with zipfile.ZipFile(wheel) as z:
        z.extractall(site)
    shutil.copytree(ROOT / "src" / "qbd_agent_kit", site / "qbd_agent_kit", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (stage / "qbd.cmd").write_bytes(b'@"%~dp0python\\python.exe" -m qbd_agent_kit %*\r\n')
    (stage / "qbd").write_bytes(b'#!/bin/sh\nexec "$(dirname "$0")/python/python.exe" -m qbd_agent_kit "$@"\n')
    shutil.copy(ROOT / "LICENSE", stage / "LICENSE.txt")
    readme = README.format(version=__version__, python=".".join(py.groups()[:3]), bitness=BITNESS[arch])
    (stage / "README.txt").write_bytes(readme.replace("\n", "\r\n").encode("utf-8"))
    path = out / f"{name}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p for p in stage.rglob("*") if p.is_file()):
            # At the zip's root, so unzipping to C:\qbd gives C:\qbd\qbd.cmd as README.txt says.
            z.write(f, f.relative_to(stage))
    return path


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    path = build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3] if len(sys.argv) == 4 else ROOT / "dist"))
    print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")
