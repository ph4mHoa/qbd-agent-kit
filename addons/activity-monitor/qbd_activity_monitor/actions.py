"""What a person does in the Activity Monitor (release 2): approve or reject a draft, undo a saved change, stop a
command after its current step, resume a paused session, and dismiss a draft that expired. Each runs as the
signed-in Windows user and leaves its mark in qbd's logs. Approving and undoing run qbd itself, with this PC's
settings, so qbd's own checks apply; nothing here talks to QuickBooks. An approval names the draft's fingerprint as
shown, so a draft changed after it was shown is not saved."""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from qbd_agent_kit import api

from .feed import Folder, pc_env
from .model import Approval, ChangeRow, LiveCmd, is_sample

ADDON = "activity-monitor"  # its name among qbd's add-ons
MONITOR = "Activity Monitor"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env(folder: Folder) -> dict[str, str]:
    """This PC's settings, the folder the draft lives in, and the monitor as the one who asks."""
    return {**pc_env(), "QBD_STATE_DIR": folder.path, "QBD_AGENT": MONITOR, "QBD_SESSION": "", "QBD_SESSION_TITLE": "",
            "PYTHONIOENCODING": "utf-8"}


def run(args: list[str], folder: Folder, timeout: float = 600) -> dict[str, Any]:
    """Runs qbd and returns its JSON answer; {"error": {...}} when it failed."""
    try:
        p = subprocess.run(api.qbd_command() + args, env=_env(folder), cwd=api.KIT_HOME, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": {"code": "monitor", "message": f"qbd could not be started: {e}"}}
    lines = [x for x in p.stdout.strip().splitlines() if x.strip()]
    try:
        answer = json.loads(lines[-1]) if lines else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["no answer"]
        return {"error": {"code": "monitor", "message": f"qbd gave no answer ({tail[0][:200]})"}}
    return answer


def _sample(path: Any) -> list[str]:
    return ["--demo"] if is_sample(path) else []


def approve(a: Approval) -> dict[str, Any]:
    """Saves exactly the previewed change, once (G6): qbd commit, marked as approved in the monitor, with the
    fingerprint of what the person saw."""
    if not a.seen:
        return {"error": {"code": "monitor", "message": "The draft could not be read just now, so it was not saved. Try again."}}
    return run(_sample(a.d.get("company_file")) + ["commit", a.token, "--approved-in", "monitor", "--seen", a.seen], a.folder)


def reject(a: Approval) -> dict[str, Any]:
    """Discards the draft. The write log and the draft's answer say who rejected it, here."""
    config = api.load_config({**pc_env(), "QBD_STATE_DIR": a.folder.path}, demo=is_sample(a.d.get("company_file")))
    try:
        api.reject(config, api.Drafts(os.path.join(a.folder.path, "drafts")), a.token)
    except api.ToolError as e:
        return {"error": {"code": "refused", "message": str(e)}}
    return {"rejected": True}


def undo(c: ChangeRow) -> dict[str, Any]:
    """Drafts the reverse of a saved change; the draft comes back under Approvals."""
    return run(_sample(c.rec.e.get("companyFile")) + ["undo", str(c.rec.e.get("token"))], _folder_of(c.rec.folder))


def dismiss(a: Approval) -> None:
    """An expired draft leaves the list of waiting ones for the answered ones, as qbd does with it later anyway."""
    if not api.TOKEN.match(a.token):
        return
    drafts = api.Drafts(os.path.join(a.folder.path, "drafts"))
    d = drafts._load(a.token)
    if d is not None and d.expires < time.time():
        drafts.retire(d)


def stop(cmd: LiveCmd) -> None:
    """Stop after this step: the command ends after its current QuickBooks request, and its session waits until
    someone clicks Resume (BR 6)."""
    by = api.who_am_i()
    info = cmd.info
    api.ask_stop(cmd.folder.path, cmd.pid, by)
    api.pause(cmd.folder.path, api.who(str(info.get("agent") or ""), str(info.get("session") or ""), str(info.get("program") or "")),
              by, name=cmd.who.name, title=str(info.get("session_title") or ""), command=str(info.get("command") or ""),
              request=cmd.title, step=len(cmd.done) + (1 if cmd.step else 0))


def resume(folder: Folder, pause: dict[str, Any]) -> None:
    """Lets a paused session run again, and says so in the call log."""
    key = api.who(str(pause.get("agent") or ""), str(pause.get("session") or ""), str(pause.get("program") or ""))
    api.resume(folder.path, key)
    config = api.load_config({**pc_env(), "QBD_STATE_DIR": folder.path})
    if config.call_log:
        try:
            api.write_call(config.call_log, {"agent": MONITOR, "command": "resume",
                                         "title": "Resume a paused session", "args": {"assistant": pause.get("name") or key["agent"] or
                                                                                      key["program"] or "unknown", **({"session": key["session"]} if key["session"] else {})},
                                         "outcome": "ok", "ms": 0, "items": 0, "output_bytes": 0, "requests": []})
        except OSError:
            pass


def _folder_of(path: str) -> Folder:
    return Folder(path, True)


# ---------------------------------------------------------------- start with Windows


def starts_with_windows() -> bool | None:
    """None where it cannot be told (not Windows)."""
    return api.starts_at_sign_in(ADDON)


def set_start_with_windows(on: bool) -> None:
    api.set_start_at_sign_in(ADDON, on, "--tray")
