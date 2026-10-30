# Add-ons

An add-on is a program a person adds to `qbd` on the QuickBooks PC when they want it, as one adds an extension to a
code editor. `qbd` itself stays a command line for AI agents; an add-on gives people something more on top of it.
The first is the [Activity Monitor](setup.md#7-watch-and-approve-in-the-activity-monitor), a window that shows what
the assistants do in QuickBooks and lets a person approve their changes.

- [Installing one](#installing-one)
- [What is checked](#what-is-checked)
- [Where files go](#where-files-go)
- [For administrators](#for-administrators)
- [Making an add-on](#making-an-add-on)

## Installing one

With the Windows download, double-click `add-ons.cmd` in its folder. It lists the add-ons in qbd's catalog and the
ones installed; type an add-on's number and Enter, then `y` to install it. An installed add-on's number offers to
start it, update it when a newer version is out, or remove it.

The same from a command line:

```
qbd addons
qbd addons install activity-monitor
qbd addons install activity-monitor --yes
qbd addons run activity-monitor
qbd addons remove activity-monitor --yes
```

`qbd addons` lists the catalog, what is installed here, and the administrator's policy, if any. Without `--yes`,
`install` and `remove` change nothing: they say what they would do (the version, the size, the address and SHA-256 of
the download, the folder). An add-on with a window gets an entry in the Start menu, under **QBD Agent Kit**. To update
one, install it again when `qbd addons` says a newer version is out. An add-on that is running must be quit first (in
its tray icon's menu: **Quit**); removing it also removes its Start menu entry and its start at sign-in.

### On a PC without internet

Download the add-on's zip on another PC, from the kit's release page that `qbd addons install <name>` names, copy it
to the QuickBooks PC, and give its path:

```
qbd addons install D:\activity-monitor-0.3.0-cp315-win_amd64.zip --yes
```

qbd checks the zip's SHA-256 against its catalog, as it checks a download.

## What is checked

- **Only add-ons from qbd's catalog install.** The catalog ships inside `qbd` and names each add-on's download by its
  address (https only), its size and its SHA-256. A download that is larger, or whose SHA-256 differs, is refused and
  nothing is installed. A zip whose SHA-256 matches no add-on in the catalog installs only where an administrator
  allows it (below).
- **The zip is read before anything is unpacked.** Every path in it must stay inside the add-on's folder, with no
  links; at most 5,000 files and 400 MB unpacked; an `addon.json` at its root. It must be made for this qbd's add-on
  API and for this Python: the same Python version and system (`cp315-win_amd64` for the Windows download's Python),
  or `any` for an add-on without compiled files.
- **Installing runs none of the add-on's code.** The new version is unpacked beside the old one and the two folders
  are swapped only once it is whole, so a download or an unpacking that fails leaves the old version in place.
- **Each start checks again** that add-ons are still allowed, that the add-on is installed whole and made for this
  qbd, and, for one from outside the catalog, that the policy still allows its SHA-256.
- **Who did it.** Like every `qbd` command, each install and removal is kept in the
  [call log](safety.md#call-log-and-the-activity-page), with the Windows user.

An add-on runs with the rights of the person who starts it, as any program does, and starts with the kit's own
Python, from the folder the kit is installed in.

## Where files go

- `%LOCALAPPDATA%\qbd-agent-kit\addons\<add-on>`: the add-on, for the signed-in Windows user alone (each Windows
  account installs its own), also when `QBD_STATE_DIR` puts qbd's other files in a shared folder. Beside its files,
  `.qbd-install.json` keeps qbd's note of the install: the version, the SHA-256, where it came from, when, and the
  Windows user.
- `%APPDATA%\Microsoft\Windows\Start Menu\Programs\QBD Agent Kit`: the Start menu entry of an add-on with a window.
- `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`: a value named after the add-on, only while
  **Start with Windows** is ticked in it.

## For administrators

Two values under `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\qbd-agent-kit`, where only an administrator can write (Group
Policy Preferences can set them on many PCs), decide what may be installed. On other systems, the same names in
`/etc/qbd-agent-kit/policy.json`.

| Value | Type | What it does |
| --- | --- | --- |
| `DisableAddons` | `REG_DWORD` `1` | No add-on installs or starts; `qbd addons` says an administrator turned them off |
| `AllowedAddons` | `REG_SZ` or `REG_MULTI_SZ` | The SHA-256 of add-on zips allowed besides the catalog's, separated by spaces, commas or semicolons; `*` allows any add-on |

With neither, add-ons from the catalog install and no others. A policy that cannot be read turns add-ons off. For
example, to allow one zip after checking it (`qbd addons install` prints its SHA-256 when it refuses it):

```
reg add HKLM\SOFTWARE\Policies\qbd-agent-kit /v AllowedAddons /t REG_SZ /d <the zip's SHA-256>
```

An add-on allowed this way stops starting when its SHA-256 is taken out of the policy. For SOC 2, this is the kit's
part of CC6.8 (unauthorised software); see [Safety](safety.md#soc-2).

## Making an add-on

An add-on is a zip with:

- `addon.json` at its root:

  | Field | Example | What it is |
  | --- | --- | --- |
  | `id` | `activity-monitor` | Lowercase letters, digits and hyphens: its folder's name and its name in `qbd addons` |
  | `name` | `Activity Monitor` | The name people see, also the Start menu entry's |
  | `version` | `0.3.0` | Compared number by number to tell an update |
  | `publisher` | `qbd-agent-kit` | Who made it |
  | `description` | | A sentence or two for the list |
  | `api` | `1` | The version of `qbd_agent_kit.api` it was made for |
  | `module` | `qbd_activity_monitor` | The package qbd runs, as `python -m` runs it |
  | `window` | `true` | A window app: started with `pythonw.exe`, so no console window opens, and listed in the Start menu |
  | `build` | `cp315-win_amd64` | The Python and system its compiled files are for; `any` when it has none |
  | `icon` | `icon.ico` | Optional: an `.ico` file in the zip, for the Start menu |

- its Python package, and under `vendor\` the libraries it needs. Both folders come first on `sys.path` when it
  starts.

qbd starts it as `python -m qbd_agent_kit.addons <id> [arguments]`, in the kit's own Python. From qbd, an add-on uses
only `qbd_agent_kit.api`: the names there keep their meaning for every qbd that serves the same API version, and the
files qbd keeps in its state folder are part of that API. To run a `qbd` command, it runs `api.qbd_command()` in the
folder `api.KIT_HOME`.

`python scripts/build-addon.py <add-on folder> [<wheel> ...]` builds the zip from the add-on's folder, taking from
each wheel only the files the add-on needs, and writes the zip's address, SHA-256 and size into qbd's catalog
(`src/qbd_agent_kit/addons/catalog.json`). The same files make the same zip, byte for byte. Add-ons in the catalog are
published with qbd's own releases.
