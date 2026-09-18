"""Open the app's window.

Chrome in `--app` mode gives a real application window - no tabs, no address bar, its own
taskbar entry - and it is already a requirement of this tool (Selenium drives Chrome), so
nothing new has to be installed. It replaced the embedded WebView2 window, which deadlocked
solid on roughly one launch in ten.

The window gets its own small Chrome profile under the app's data folder, so it never disturbs
the user's own Chrome, never asks them to close it, and is not the profile the automation uses.
"""
from __future__ import annotations

import os
import subprocess

from .paths import app_dir

CHROME_CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",          # last resort
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> str | None:
    for c in CHROME_CANDIDATES:
        p = os.path.expandvars(c)
        if os.path.isfile(p):
            return p
    return None


def open_window(url: str, width: int = 1160, height: int = 820) -> subprocess.Popen | None:
    """Launch the app window. Returns the process so the caller can wait for it to close."""
    exe = find_browser()
    if not exe:
        return None
    profile = os.path.join(app_dir(), "window-profile")
    os.makedirs(profile, exist_ok=True)
    args = [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile}",       # its own profile: never touches the user's Chrome
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-component-update",
        "--disable-features=Translate,OptimizationHints,MediaRouter",
        "--disable-extensions",
    ]
    creation = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
    return subprocess.Popen(args, creationflags=creation)
