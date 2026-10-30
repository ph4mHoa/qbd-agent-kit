"""Colours and type from the design's Component sheet, light and dark (Windows 11 Fluent). Light and dark follow
Windows. Status colours always come with an icon and a word."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache

import wx

LIGHT = {
    "accent": "#005FB8", "accent_hover": "#0067C0", "accent_pressed": "#1975C5", "on_accent": "#FFFFFF",
    "info": "#005FB8", "info_bg": "#E6F0FA", "ok": "#0F7B0F", "ok_bg": "#DFF6DD", "warn": "#9D5D00", "warn_bg": "#FFF4CE",
    "bad": "#C42B1C", "bad_bg": "#FDE7E9", "neutral": "#424242", "neutral_bg": "#F0F0F0",
    "mica": "#F3F3F3", "content": "#F9F9F9", "card": "#FFFFFF", "card_alt": "#FBFBFB", "stroke": "#E5E5E5", "divider": "#EBEBEB",
    "control": "#FFFFFF", "control_border": "#D1D1D1", "control_hover": "#F6F6F6", "hover": "#EAEAEA", "subtle": "#F6F6F6",
    "selected": "#EAEAEA", "text": "#1B1B1B", "secondary": "#5F5F5F", "disabled": "#A0A0A0", "disabled_border": "#C8C8C8",
    "track": "#E0E0E0", "code_bg": "#F6F6F6", "warn_stroke": "#F1D18A", "bad_stroke": "#F3B5B8", "focus": "#1B1B1B",
    "quiet": "#FBFBFB", "portal": "#F9F9F9", "portal_foot": "#F3F3F3", "portal_stroke": "#E5E5E5",
}
DARK = {
    "accent": "#60CDFF", "accent_hover": "#5AB8E5", "accent_pressed": "#4FA3CC", "on_accent": "#000000",
    "info": "#60CDFF", "info_bg": "#1A3447", "ok": "#6CCB5F", "ok_bg": "#393D1B", "warn": "#FCE100", "warn_bg": "#433519",
    "bad": "#FF99A4", "bad_bg": "#442726", "neutral": "#D6D6D6", "neutral_bg": "#3A3A3A",
    "mica": "#202020", "content": "#272727", "card": "#2E2E2E", "card_alt": "#383838", "stroke": "#3A3A3A", "divider": "#383838",
    "control": "#363636", "control_border": "#4A4A4A", "control_hover": "#3D3D3D", "hover": "#2D2D2D", "subtle": "#333333",
    "selected": "#383838", "text": "#FFFFFF", "secondary": "#C8C8C8", "disabled": "#787878", "disabled_border": "#4A4A4A",
    "track": "#4A4A4A", "code_bg": "#252525", "warn_stroke": "#6B5A1E", "bad_stroke": "#6E3A3D", "focus": "#FFFFFF",
    "quiet": "#3A3A3A", "portal": "#2B2B2B", "portal_foot": "#242424", "portal_stroke": "#3D3D3D",
}
# Six assistant colours (text on tint), none of them a status or accent colour; grey for an unknown caller.
ASSISTANTS_LIGHT = [("#5B3CC4", "#EEE9FB"), ("#00695E", "#E0F4F1"), ("#A3195B", "#FBE7F1"), ("#6D4C41", "#F2EBE8"),
                    ("#455A64", "#E7EDF0"), ("#7A1F7A", "#F5E6F5")]
ASSISTANTS_DARK = [("#C9B8FF", "#3A2F5C"), ("#7FE0D2", "#163F3A"), ("#FF9CCB", "#4A1F33"), ("#E0C0B4", "#3E2E28"),
                   ("#B9CCD6", "#2C3A41"), ("#EBA9EB", "#422442")]
UNKNOWN = {"light": ("#5F5F5F", "#F0F0F0"), "dark": ("#C8C8C8", "#3A3A3A")}

FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "DejaVu Sans", "Liberation Sans"]
MONO = ["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Liberation Mono"]
# (size, weight, line height) in pixels: Title 28/36, Subtitle 20/28, Body strong 14/20, Body 14/20, Caption 12/16,
# and the few sizes the screens use beside the ramp
TYPE = {"title": (28, 600, 36), "subtitle": (20, 600, 28), "strong": (14, 600, 20), "body": (14, 400, 20), "caption": (12, 400, 16),
        "caption_strong": (12, 600, 16), "big": (28, 600, 36), "amount": (28, 600, 36),
        "lead": (16, 400, 24), "lead_strong": (16, 600, 22), "mono": (12, 400, 18),
        # an assistant's initials: one letter, two letters, and on the 32 and 40 px avatars
        "initials": (12, 700, 16), "initials_small": (11, 700, 16), "initials_big": (14, 700, 20), "initials_large": (18, 700, 24)}


@dataclass
class Theme:
    dark: bool

    @property
    def c(self) -> dict[str, str]:
        return DARK if self.dark else LIGHT

    def assistant(self, colour: int) -> tuple[str, str]:
        if colour < 0:
            return UNKNOWN["dark" if self.dark else "light"]
        return (ASSISTANTS_DARK if self.dark else ASSISTANTS_LIGHT)[colour % 6]

    def tone(self, tone: str) -> tuple[str, str]:
        """(text, fill) for a status tone: ok, warn, bad, info, neutral, and new (the solid tag on a new problem)."""
        c = self.c
        return {"ok": (c["ok"], c["ok_bg"]), "warn": (c["warn"], c["warn_bg"]), "bad": (c["bad"], c["bad_bg"]),
                "info": (c["info"], c["info_bg"]), "running": (c["info"], c["info_bg"]), "new": (c["on_accent"], c["accent"])}.get(tone, (c["neutral"], c["neutral_bg"]))


@lru_cache(maxsize=256)
def colour(hex_: str) -> wx.Colour:
    return wx.Colour(hex_)


@lru_cache(maxsize=8)
def family(mono: bool) -> str:
    """The first of the design's typefaces this PC has."""
    names = MONO if mono else FAMILIES
    found = wx.FontEnumerator()
    return next((n for n in names if found.IsValidFacename(n)), "")


@lru_cache(maxsize=1)
def _ppi() -> int:
    return wx.ScreenDC().GetPPI()[1] or 96


@lru_cache(maxsize=128)
def font(role: str, scale: float) -> wx.Font:
    """A role of the type ramp at a pixel size, in the design's typeface. The size is given in points: a size in
    pixels means the letters' height on Windows but the whole line's on Linux."""
    size, weight, _ = TYPE.get(role, TYPE["body"])
    info = wx.FontInfo(size * scale * 72 / _ppi()).Weight(weight)
    face = family(role == "mono")
    if face:
        info = info.FaceName(face)
    elif role == "mono":
        info = info.Family(wx.FONTFAMILY_TELETYPE)
    return wx.Font(info)


def windows_dark() -> bool | None:
    """Whether Windows' apps use the dark mode; None where that cannot be told."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return None
