"""Builds an add-on's zip for qbd's catalog: its addon.json, its icon, its package and, under vendor/, the part of each
wheel it needs. Then writes the zip's address, SHA-256 and size into qbd's catalog
(src/qbd_agent_kit/addons/catalog.json), where `qbd addons install <id>` finds what to download and how to check it.

usage: python scripts/build-addon.py <add-on folder> [<wheel> ...] [out dir]
  e.g. python scripts/build-addon.py addons/activity-monitor wxpython-4.3.2-cp315-cp315-win_amd64.whl

The Activity Monitor needs the wxPython wheel for the Windows download's Python (cp315, win_amd64), from
https://pypi.org/project/wxPython/#files. Writes <out dir>/<id>-<version>-<build>.zip (out dir: dist). The same files
make the same zip, byte for byte, so a rebuild keeps the SHA-256 in the catalog. Upload the zip to the kit's GitHub
release v<qbd's version>, the address the catalog names.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qbd_agent_kit import __version__  # noqa: E402
from qbd_agent_kit.addons import MANIFEST, manifest, read_zip  # noqa: E402

CATALOG = ROOT / "src" / "qbd_agent_kit" / "addons" / "catalog.json"
RELEASE = "https://github.com/ph4mHoa/qbd-agent-kit/releases/download/v{version}/{name}"
WHEEL = re.compile(r"([A-Za-z0-9_.]+?)-([\d.]+)-(cp\d+)-(?:cp\d+|abi3)-(\w+)\.whl")
# What an add-on takes from each wheel. From wxPython: wx.core and wx.adv, the wxWidgets DLLs they link (objdump -p
# lists each file's DLLs; in wxWidgets 3.3 the adv DLL is empty, its classes are in core) and the licence. Not the
# other modules, translations and type stubs, which are most of its 60 MB.
KEEP = {
    "wxpython": re.compile(r"wx/(?:__init__|__version__|core|adv)\.py|wx/(?:_core|_adv|siplib)\.cp\d+-\w+\.pyd"
                           r"|wx/wxbase\d+u_(?:net_)?vc\d+_\w+\.dll|wx/wxmsw\d+u_core_vc\d+_\w+\.dll"
                           r"|wxpython-[\d.]+\.dist-info/(?:METADATA|licenses/LICENSE\.txt)"),
}
STAMP = (1980, 1, 1, 0, 0, 0)  # every entry's date in the zip, so the same files make the same zip


def build(folder: Path, wheels: list[Path], out: Path) -> Path:
    m = manifest(json.loads((folder / MANIFEST).read_text(encoding="utf-8")))
    builds = set()
    for w in wheels:
        found = WHEEL.fullmatch(w.name)
        if not found or found[1].lower() not in KEEP:
            sys.exit(f"{w.name}: not a wheel this script knows what to take from ({', '.join(KEEP)}).")
        builds.add(f"{found[3]}-{found[4]}")
    if len(builds) > 1:
        sys.exit(f"The wheels are for different Pythons or systems: {', '.join(sorted(builds))}.")
    tag = builds.pop() if builds else "any"
    files: dict[str, bytes] = {MANIFEST: (json.dumps({**json.loads((folder / MANIFEST).read_text(encoding="utf-8")), "build": tag},
                                                     indent=2, ensure_ascii=False) + "\n").encode("utf-8")}
    if m.icon:
        files[m.icon] = (folder / m.icon).read_bytes()
    package = folder / m.module.split(".")[0]
    for p in package.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            files[p.relative_to(folder).as_posix()] = p.read_bytes()
    for w in wheels:
        keep = KEEP[WHEEL.fullmatch(w.name)[1].lower()]
        with zipfile.ZipFile(w) as z:
            for name in z.namelist():
                if keep.fullmatch(name):
                    files[f"vendor/{name}"] = z.read(name)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{m.id}-{m.version}-{tag}.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name], compresslevel=9)
    read_zip(str(path))  # what qbd checks before it installs one
    data = path.read_bytes()
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    entry = catalog.get(m.id, {})
    known = entry.get("builds", {}) if entry.get("version") == m.version else {}
    known[tag] = {"url": RELEASE.format(version=__version__, name=path.name), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    catalog[m.id] = {"name": m.name, "publisher": m.publisher, "description": m.description, "version": m.version, "builds": known}
    CATALOG.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]]
    if not args or not (args[0] / MANIFEST).is_file():
        sys.exit(__doc__)
    wheels = [a for a in args[1:] if a.suffix == ".whl"]
    rest = [a for a in args[1:] if a.suffix != ".whl"]
    if len(rest) > 1:
        sys.exit(__doc__)
    path = build(args[0], wheels, rest[0] if rest else ROOT / "dist")
    print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB) and its line in {CATALOG.relative_to(ROOT)}")
