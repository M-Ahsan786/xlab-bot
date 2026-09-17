"""Scoring Agent - desktop app (pywebview shell around a modern web UI).

The UI only shows the job list + progress; all the portal automation runs in the backend
(Engine), on a worker thread, pushing events/progress to the page.
"""
from __future__ import annotations
import json
import os
import sys
import threading

import webview

# NOTE: Engine (and through it openpyxl/selenium) is imported lazily - see _engine().
# Importing it here delayed the window by many seconds, so Windows painted the app
# greyed out with "Not Responding" while it loaded.

APP_NAME = "Scoring Agent"

_INSTANCE_MUTEX = None


def _engine_cls():
    """Import the engine on first use, not at startup."""
    from .engine import Engine
    return Engine


def _here() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # bundled resources
    return os.path.dirname(os.path.abspath(__file__))


def _ui_file() -> str:
    return os.path.join(_here(), "ui", "index.html")


def _ui_html() -> str:
    """The whole UI as one HTML string, with app.js inlined.

    Handing pywebview `html=` skips its bundled HTTP server. That server runs on a Python
    thread, and while the GUI thread holds the GIL during WebView2 start-up it cannot answer -
    so the page took ~23s to load and Windows painted the window "(Not Responding)" meanwhile.
    """
    ui = os.path.join(_here(), "ui")
    with open(os.path.join(ui, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    with open(os.path.join(ui, "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    return html.replace('<script src="app.js"></script>',
                        '<script>' + chr(10) + js + chr(10) + '</script>')


class Api:
    def __init__(self):
        self.window = None
        self.engine = None
        self._thread = None
        self._log_fh = None

    # ---- helpers to talk to the page ----
    def _emit(self, name, payload):
        if not self.window:
            return
        try:
            self.window.evaluate_js(f"window.__on(({json.dumps(name)}),({json.dumps(payload)}))")
        except Exception:
            pass

    # ---- exposed to JS ----
    def pick_folder(self):
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if res:
            return res[0] if isinstance(res, (list, tuple)) else res
        return None

    def preview(self, path):
        try:
            eng = _engine_cls()()
            data = eng.preview(path)
            return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _open_log_file(self, path):
        """Keep a plain-text copy of the activity log next to the user's folder."""
        try:
            import datetime as _dt
            d = path if os.path.isdir(path) else os.path.dirname(path or "") or "."
            f = os.path.join(d, f"Scoring-Agent-Log-{_dt.datetime.now():%Y%m%d-%H%M%S}.txt")
            self._log_fh = open(f, "w", encoding="utf-8", buffering=1)
            return f
        except Exception:
            self._log_fh = None
            return None

    def _log(self, msg):
        """Send a log line to the page AND to the run's log file."""
        self._emit("log", msg)
        fh = getattr(self, "_log_fh", None)
        if fh:
            try:
                fh.write(msg + "\n")
            except Exception:
                pass

    def _close_log_file(self):
        fh = getattr(self, "_log_fh", None)
        self._log_fh = None
        if fh:
            try:
                fh.close()
            except Exception:
                pass

    def _new_engine(self):
        return _engine_cls()(
            on_event=self._log,
            on_progress=lambda p: self._emit("progress", p),
            on_login_wait=lambda: self._emit("login", {}),
            on_login_done=lambda: self._emit("login_done", {}),
            on_session=lambda: self._emit("session", {}),
        )

    def _start(self, path, job):
        """Run `job(engine)` on a worker thread and report the outcome to the page."""
        self.engine = self._new_engine()
        log_file = self._open_log_file(path)
        if log_file:
            self._log(f"Activity log: {log_file}")

        def worker():
            try:
                summary = job(self.engine)
                summary["log_file"] = log_file
                self._emit("done", summary)
            except Exception as e:
                import traceback
                msg = str(e).strip() or e.__class__.__name__
                self._log(f"ERROR: {msg}")
                fh = getattr(self, "_log_fh", None)
                if fh:                       # full detail in the file, short message on screen
                    try:
                        fh.write(traceback.format_exc() + "\n")
                    except Exception:
                        pass
                self._emit("login_done", {})   # never leave the "log in" banner up
                self._emit("error", {"error": msg})
            finally:
                self._close_log_file()

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        return {"ok": True}

    @staticmethod
    def _creds(creds):
        """Take the sign-in details straight from the page into a throwaway dict.

        They are never stored, never logged and never leave this process - the only thing done
        with them is typing them into the portal's own sign-in page.
        """
        if not creds:
            return None
        u = str(creds.get("username") or "").strip()
        p = str(creds.get("password") or "")
        return {"username": u, "password": p} if u and p else None

    def start_run(self, path, mode, dc, creds=None):
        if self._thread and self._thread.is_alive():
            return {"ok": False, "error": "A run is already in progress."}
        dc = (dc or "").strip() or None
        c = self._creds(creds)
        return self._start(path, lambda eng: eng.run(path, mode=mode, dc=dc, creds=c))

    def start_make_live(self, path, modules, dc=None, creds=None):
        """Publish modules that are already saved as InProgress (nothing is uploaded)."""
        if self._thread and self._thread.is_alive():
            return {"ok": False, "error": "A run is already in progress."}
        mods = [str(m) for m in (modules or []) if str(m).strip()]
        if not mods:
            return {"ok": False, "error": "No modules were selected to make live."}
        dc = (dc or "").strip() or None
        c = self._creds(creds)
        return self._start(path, lambda eng: eng.make_live(path, modules=mods, dc=dc, creds=c))

    def cancel(self):
        if self.engine:
            self.engine.state.cancel = True
        return {"ok": True}

    def is_running(self):
        return {"running": bool(self._thread and self._thread.is_alive())}

    def reset_session(self):
        """Full reset without restarting the app.

        Stops anything in flight, closes the automation browser, then clears the saved login
        (Chrome profile + auth marker) so the next Start begins from a clean, signed-out state.
        """
        import time
        was_running = bool(self._thread and self._thread.is_alive())
        if self.engine:
            self.engine.state.cancel = True
            self.engine.shutdown_browser()      # close Chrome now, don't wait for the loop
        if was_running:
            self._thread.join(timeout=20)       # let the worker finish/report cleanly
        still_running = bool(self._thread and self._thread.is_alive())
        if still_running:
            # Leave the worker's own teardown alone - clearing self.engine / the log handle
            # underneath it would orphan it and swallow its last messages.
            self._log("Reset: the previous run is taking a moment to stop; "
                      "its browser is closed and the session will be cleared.")
        else:
            self.engine = None
            self._close_log_file()

        from .paths import app_dir as _default_app_dir
        from .session import SessionManager
        sm = SessionManager(_default_app_dir())
        time.sleep(0.6)                          # give Chrome a moment to release the profile
        cleared = sm.clear()
        self._emit("log", "Session reset - saved login cleared; sign in again on the next run.")
        return {"ok": True, "cleared": cleared, "was_running": was_running,
                "still_running": still_running, "leftover": sm.profile_left_behind()}

    def open_path(self, p):
        try:
            if p and os.path.exists(p):
                os.startfile(p)  # noqa
                return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "not found"}

    def push_session(self):
        """Send the session state TO the page.

        The page used to ask for this while it was still loading, and that call occasionally
        deadlocked the window. Pushing costs nothing and cannot wedge the UI.
        """
        try:
            self._emit("session_status", self.session_status())
        except Exception:
            pass

    # ---- updates ----
    def app_version(self):
        from .version import __version__
        return {"version": __version__}

    def update_settings(self, repo=None):
        """Read, or set, the GitHub repo the team publishes releases to."""
        from . import update as up
        try:
            if repo is not None:
                return {"ok": True, "repo": up.set_repo(repo)}
            return {"ok": True, "repo": up.get_repo()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def check_update(self, repo=None):
        from . import update as up
        try:
            return up.check(repo)
        except Exception as e:
            return {"ok": False, "reason": "error", "error": str(e)}

    def install_update(self, url, size=0):
        """Download this release's installer, then start it and close the app.

        Nothing happens without the user pressing the button - this is that press.
        """
        from . import update as up
        if self._thread and self._thread.is_alive():
            return {"ok": False, "error": "A run is in progress. Let it finish, then update."}
        try:
            path = up.download(url, int(size or 0),
                               on_progress=lambda pct: self._emit("update_progress", pct))
        except Exception as e:
            return {"ok": False, "error": str(e)}

        def go():
            import time
            time.sleep(1.0)              # let the page show "starting the installer"
            try:
                up.launch_installer(path)
            except Exception:
                return
            time.sleep(1.5)              # then get out of the way so files aren't locked
            try:
                if self.window:
                    self.window.destroy()
            except Exception:
                os._exit(0)

        threading.Thread(target=go, daemon=True).start()
        return {"ok": True, "path": path}

    # ---- security / session ----
    def session_status(self):
        from .paths import app_dir as _default_app_dir
        from .session import SessionManager
        return SessionManager(_default_app_dir()).status()

    def sign_out(self):
        if self._thread and self._thread.is_alive():
            return {"ok": False,
                    "error": "A run is in progress. Use “Reset session” to stop it and sign out."}
        from .paths import app_dir as _default_app_dir
        from .session import SessionManager
        sm = SessionManager(_default_app_dir())
        removed = sm.sign_out()
        if sm.profile_left_behind():
            return {"ok": False,
                    "error": "The saved session could not be fully removed - some browser files "
                             "are still locked. Close any Chrome window the agent opened and try again."}
        return {"ok": True, "cleared": removed}


def _claim_single_instance() -> bool:
    """True if we are the only instance. Otherwise focus the running one and return False.

    Without this, double-clicking the icon twice leaves two windows fighting over the same
    saved session, and the user cannot tell which one is theirs.
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        # session-scoped name (no "Global\\" prefix, which needs a privilege we may not have);
        # the handle is kept in a module global so the OS holds it for our whole lifetime.
        global _INSTANCE_MUTEX
        _INSTANCE_MUTEX = k32.CreateMutexW(None, wintypes.BOOL(True),
                                           "ScoringAgent.SingleInstance.Mutex")
        if k32.GetLastError() != 183:            # 183 = ERROR_ALREADY_EXISTS
            return True
        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, APP_NAME)   # bring the existing window forward
        if hwnd:
            u32.ShowWindow(hwnd, 9)              # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
        return False
    except Exception:
        return True                              # never block startup over this


# WebView2 reads this before it starts. Without it the embedded browser does the same
# start-up chatter a full browser does (component update, optimisation hints, telemetry), and
# on a slow or filtered connection that blocks its initialisation for ~20s - during which the
# GUI thread cannot pump messages and Windows paints the app "(Not Responding)".
os.environ.setdefault(
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "--disable-background-networking --disable-component-update --no-first-run "
    "--disable-sync --disable-features=OptimizationHints,Translate,MediaRouter "
    "--disable-breakpad --no-pings")


def main():
    if not _claim_single_instance():
        return
    api = Api()
    window = webview.create_window(
        APP_NAME, html=_ui_html(), js_api=api,
        width=1120, height=760, min_size=(920, 620),
        background_color="#0B2A4A",
    )
    api.window = window
    # Give WebView2 ONE persistent profile instead of letting pywebview build a fresh
    # private one on every launch - creating a browser profile from scratch is what makes
    # start-up take tens of seconds on some launches.
    store = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         APP_NAME, "webview")
    try:
        os.makedirs(store, exist_ok=True)
    except Exception:
        store = None
    # NOTHING is sent to or asked of the page while it loads. Measured over dozens of
    # launches: any bridge traffic in that window occasionally deadlocks WebView2 and the
    # app locks up for good. The chip starts at "Not signed in" and is refreshed the first
    # time the user actually does something.
    if store:
        webview.start(private_mode=False, storage_path=store)
    else:
        webview.start()


if __name__ == "__main__":
    main()
