"""What an add-on may use of qbd. The names here keep their meaning within one version of this API, so an add-on made
for version 1 runs on every later qbd that serves version 1; the rest of qbd_agent_kit may change in any release. The
files qbd keeps in its state folder (the call log, the write log, drafts, live/ and paused/) are part of the API as
qbd 0.3 writes them. An add-on names the version it was made for in its addon.json ("api": 1)."""
from __future__ import annotations

from . import __version__ as QBD_VERSION
from .addons import KIT_HOME, qbd_command, set_start_at_sign_in, starts_at_sign_in
from .cli import COMMANDS
from .config import Config, load_config, state_dir
from .demo.transport import COMPANY_FILE as SAMPLE_COMPANY_FILE
from .errors import HINTS, STATUS_HINTS, ToolError
from .jsutil import MISSING, dumps
from .safety.audit import _windows_processes as windows_processes
from .safety.audit import archives, read_log, who_am_i, write_call
from .safety.live import LIVE, PAUSED, alive, ask_stop, pause, read_json, resume, who, write_json
from .tools.common import TOKEN, Drafts, batch_tokens, file_digest, fingerprint
from .tools.write import reject

API_VERSION = 1

__all__ = [
    "API_VERSION", "QBD_VERSION",
    # running qbd, and starting an add-on when Windows starts
    "KIT_HOME", "qbd_command", "starts_at_sign_in", "set_start_at_sign_in",
    # qbd's commands (name -> its title, description and arguments) and settings
    "COMMANDS", "Config", "load_config", "state_dir", "SAMPLE_COMPANY_FILE",
    # errors and what to do about them
    "HINTS", "STATUS_HINTS", "ToolError", "MISSING", "dumps",
    # the logs
    "archives", "read_log", "who_am_i", "write_call", "windows_processes",
    # what runs now, and stopping or pausing it
    "LIVE", "PAUSED", "alive", "ask_stop", "pause", "resume", "who", "read_json", "write_json",
    # drafts waiting for a person's answer
    "TOKEN", "Drafts", "batch_tokens", "file_digest", "fingerprint", "reject",
]
