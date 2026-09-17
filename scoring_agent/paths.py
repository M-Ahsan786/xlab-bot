"""Where the app keeps its writable data.

Kept in its own tiny module so the UI can ask about the session without importing the engine
(which pulls in openpyxl and selenium and would stall the window at startup).
"""
from __future__ import annotations
import os
import sys

APP_FOLDER = "Scoring Agent"


def app_dir() -> str:
    r"""Writable data (Chrome profile, session marker).

    Installed builds live under C:\Program Files, which a normal user cannot write to, so the
    frozen app uses %LOCALAPPDATA%\Scoring Agent. Running from source keeps everything in the
    project folder.
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, APP_FOLDER)
        os.makedirs(d, exist_ok=True)
        return d
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def reports_dir() -> str:
    """Reports go somewhere the user can actually find them."""
    if getattr(sys, "frozen", False):
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        if os.path.isdir(docs):
            return os.path.join(docs, APP_FOLDER + " Reports")
    return os.path.join(app_dir(), "reports")
