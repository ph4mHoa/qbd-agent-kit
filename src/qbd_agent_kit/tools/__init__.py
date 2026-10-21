"""The tools an agent can call, each run by a qbd command."""
from __future__ import annotations

from typing import Any

from ..config import Config
from ..errors import InvalidArguments, QbxmlInputError
from ..safety.privacy import mask_personal_data
from .args import parse_args
from .common import Ctx, Tool
from .files import FILE_TOOLS
from .insights import INSIGHT_TOOLS
from .read import READ_TOOLS
from .write import WRITE_TOOLS


def tools_for(config: Config) -> list[Tool]:
    return [*READ_TOOLS, *INSIGHT_TOOLS, *FILE_TOOLS, *(WRITE_TOOLS if config.allow_writes else [])]


def run_tool(ctx: Ctx, tool: Tool, args: Any) -> Any:
    """Runs a tool with checked arguments. Personal data is masked unless the owner turned that off."""
    result = tool.run(ctx, parse_args(tool.input, args))
    return mask_personal_data(result) if ctx.config.mask_personal_data else result


def error_text(e: BaseException) -> str:
    if isinstance(e, InvalidArguments):
        return f"Invalid arguments: {e}"
    if isinstance(e, QbxmlInputError):
        return f"The request does not fit the qbXML schema: {e}"
    return str(e)
