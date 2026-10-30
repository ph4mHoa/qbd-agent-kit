"""qbd addons: the programs a person can add to qbd on this PC (qbd_agent_kit.addons)."""
from __future__ import annotations

from ..addons import AddonError, install, listing, remove, start
from .args import Arg
from .common import Ctx, tool


@tool(
    "qb_addons",
    "Add-ons: list, install, start or remove one",
    "Programs a person can add to qbd on this PC, such as the Activity Monitor: a window that shows what assistants do "
    "in QuickBooks and lets the person approve their changes. list shows qbd's catalog and what is installed. install "
    "takes a name from the catalog, downloaded and checked against the SHA-256 the catalog names, or the path of a "
    "downloaded .zip; add-ons outside the catalog install only where an administrator allows them. Without --yes, "
    "install and remove only say what they would do. Install or remove an add-on only when the person asks.",
    {
        "action": Arg("enum", "What to do", default="list", choices=("list", "install", "run", "remove")),
        "addon": Arg("str", "The add-on: its name in the catalog, or the path of a downloaded .zip to install"),
        "yes": Arg("bool", "Install or remove it; without this, only say what would happen"),
    },
)
def addons(ctx: Ctx, a: dict) -> dict:
    action, name = a["action"], a["addon"]
    if action == "list":
        return listing()
    if not name:
        raise AddonError(f"qbd addons {action} needs the add-on's name, as qbd addons list shows it.")
    if action == "install":
        return install(name, bool(a["yes"]))
    if action == "remove":
        return remove(name, bool(a["yes"]))
    return {"started": name, "pid": start(name)}
