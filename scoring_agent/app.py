"""Scoring Agent - the desktop app.

The window is Chrome in `--app` mode (a real app window: no tabs, no address bar) pointed at a
small local server, which is how the page and the backend talk. It used to be an embedded
WebView2 window driven by pywebview; that deadlocked solid on roughly one launch in ten - the
window blocked with 0% CPU and never recovered - and no amount of patching removed it.

The UI still only shows the job list + progress; all the portal automation runs in the backend
(Engine) on a worker thread, and events are streamed to the page.
"""
from __future__ import annotations
import json
import os
import sys
import threading

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

    One file means one request: the window is up as soon as the server answers.
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
        self.server = None          # set by main(); how events reach the page
        self.engine = None
        self._thread = None
        self._log_fh = None

    # ---- helpers to talk to the page ----
    def _emit(self, name, payload):
        srv = getattr(self, "server", None)
        if srv is None:
            return
        try:
            srv.emit(name, payload)
        except Exception:
            pass

    # ---- exposed to JS ----
    def pick_folder(self):
        """Windows' own folder dialog. None means cancelled - or that it could not be shown,
        in which case the page offers a box to paste a path into instead."""
        from .folderdialog import ask_folder
        return ask_folder()

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
    """True if we are the only instance, otherwise focus the running one and return False."""
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


def _no_browser_message():
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        None,
        "Scoring Agent needs Google Chrome, which is also what it uses to drive the "
        "portal." + chr(10) + chr(10) +
        "Install Chrome and start Scoring Agent again.",
        APP_NAME, 0x10)


def _window_failed_message():
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        None,
        "Scoring Agent could not open its window." + chr(10) + chr(10) +
        "Close any Scoring Agent window that is already open and try again.",
        APP_NAME, 0x10)


def main():
    if not _claim_single_instance():
        return

    from . import shell
    from .server import UiServer

    if shell.find_browser() is None:
        _no_browser_message()
        return

    api = Api()
    server = UiServer(api, _ui_html()).start()
    api.server = server

    proc = shell.open_window(server.url)
    if proc is None:
        _no_browser_message()
        return

    # Do NOT treat the launcher exiting as "the window closed". Chrome often hands the command
    # line to an instance that already owns this profile and the launcher then exits at once
    # (exit code 21) while the window is perfectly fine. What actually tells us the window is
    # there - and later gone - is whether it is connected to our event stream.
    import time
    deadline = time.time() + 90
    while time.time() < deadline and not server.ever_connected:
        time.sleep(0.2)
    if not server.ever_connected:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        _window_failed_message()
        return

    try:
        while True:
            time.sleep(0.5)
            if server.clients:
                continue
            # a reload or a hiccup drops the stream for a moment - only leave once it stays gone
            if time.time() - server.last_disconnect > 6:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
