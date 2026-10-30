"""python -m qbd_agent_kit.addons <add-on> [arguments]: runs an installed add-on in this process, as qbd addons run,
the Start menu and Windows' sign-in start it. With nothing after it: a short menu to install, start, update or remove
add-ons, for people who double-click add-ons.cmd in the Windows download."""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
import textwrap
import traceback

from .. import __version__
from . import AddonError, addons_dir, check, listing


def tell(title: str, text: str) -> None:
    """A message for the person: in the console, or in a message box when the add-on runs without one (pythonw)."""
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    elif sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR


def run(addon_id: str, args: list[str]) -> int:
    try:
        m, _ = check(addon_id)
    except AddonError as e:
        tell("qbd add-ons", str(e))
        return 2
    folder = os.path.join(addons_dir(), addon_id)
    sys.path[:0] = [folder, os.path.join(folder, "vendor")]
    sys.argv = [addon_id, *args]
    try:
        runpy.run_module(m.module, run_name="__main__", alter_sys=True)
    except Exception as e:  # noqa: BLE001 - without a console, the person would not see why it stopped
        last = traceback.format_exception_only(type(e), e)[-1].strip()
        hint = ("\n\nWindows may be missing the Microsoft Visual C++ Redistributable (x64): "
                "https://aka.ms/vs/17/release/vc_redist.x64.exe" if "DLL load failed" in last else "")
        tell(m.name, f"{m.name} stopped: {last}{hint}")
        return 1
    return 0


def ask(question: str) -> str:
    return input(question).strip().lower()


def qbd(*args: str) -> dict:
    """qbd addons, run as the command line runs it, so the call log keeps a line for each install, removal and start
    made from this menu too."""
    from ..cli import main as qbd_main

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = qbd_main(["addons", *args])
    answer = json.loads(out.getvalue())
    if code:
        raise AddonError(answer["error"]["message"])
    return answer


def menu() -> int:
    """What can be added, and a number to type to install, start, update or remove it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    while True:
        found = listing()
        rows = found["addons"]
        print(f"\nAdd-ons for qbd {__version__}. {found['policy']}\n")
        for n, r in enumerate(rows, 1):
            if r["installed"]:
                state = f"installed (version {r['installed']}" + (f"; {r['latest']} is out)" if r["update"] else ")")
            else:
                state = "not installed" + ("" if r["latest"] else f" ({r.get('note', 'not in the catalog')})")
            print(f"  {n}. {r['name']}, by {r['publisher']}: {state}")
            print(textwrap.fill(r["description"], 100, initial_indent="     ", subsequent_indent="     "))
        if not rows:
            print("  None yet.")
        choice = ask("\nType an add-on's number and Enter, or just Enter to close: ")
        if not choice:
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
            continue
        r = rows[int(choice) - 1]
        try:
            if r["installed"]:
                todo = ask(f"{r['name']}: S to start it, " + ("U to update it, " if r["update"] else "") + "R to remove it, or Enter: ")
                if todo == "s":
                    qbd("run", r["id"])
                    print("Started.")
                elif todo == "r" and ask(f"Remove {r['name']}? Type y and Enter: ") == "y":
                    qbd("remove", r["id"], "--yes")
                    print("Removed.")
                elif todo == "u" and r["update"]:
                    qbd("install", r["id"], "--yes")
                    print("Updated.")
            elif r["latest"]:
                plan = qbd("install", r["id"])["plan"]
                if ask(f"Install {r['name']} {plan['version']} ({plan['size_mb']} MB, from {plan['from']})? Type y and Enter: ") == "y":
                    done = qbd("install", r["id"], "--yes")["installed"]
                    print("Installed" + (", in the Start menu under QBD Agent Kit." if done["start_menu"] else "."))
                    if ask("Start it now? Type y and Enter: ") == "y":
                        qbd("run", r["id"])
        except AddonError as e:
            print(f"\n{e}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return run(argv[0], argv[1:]) if argv else menu()


if __name__ == "__main__":
    sys.exit(main())
