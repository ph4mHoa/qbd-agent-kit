"""The Activity Monitor: a Windows tray app that shows what AI assistants do in QuickBooks through qbd, and lets a
person answer their drafts, undo a change or stop a command. It reads qbd's state folders and never talks to
QuickBooks itself. It is an add-on of the kit: `qbd addons install activity-monitor`, then start it from the Start menu
or with `python -m qbd_agent_kit.addons activity-monitor`. Its screens are drawn with wxPython, which the add-on
carries in its vendor folder; the rest uses only the kit's API (qbd_agent_kit.api)."""
