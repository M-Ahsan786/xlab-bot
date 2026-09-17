"""Run orchestration for Scoring Agent.

Drives the whole batch: scan -> (login) -> per module: Add + fill + Save, and in Live mode also
Make Live -> writes an Excel summary report. Reports progress through callbacks so the UI only
has to render a job list and a progress line.

- mode "save": create the InProgress draft only (the user makes it live later).
- mode "live": go all the way to Live.
- Keeps the machine awake for the whole run.
- Every per-module step is retried a couple of times so a slow/again-loading list or a transient
  glitch doesn't fail a big batch.
"""
from __future__ import annotations
import datetime as dt
import os
import time
from contextlib import contextmanager

from .config import Config
from .jobs import scan, Job
from .keepawake import KeepAwake
# Writable-data locations live in paths.py so the UI can reach them without
# importing this module (and with it openpyxl + selenium).
from .paths import app_dir as _default_app_dir, reports_dir as _default_reports_dir
from .report import write_report
from .session import SessionManager


class RunState:
    def __init__(self):
        self.cancel = False


class Engine:
    def __init__(self, cfg: Config | None = None, on_event=None, on_progress=None,
                 on_login_wait=None, on_login_done=None, on_session=None):
        self.cfg = cfg or Config()
        self.on_event = on_event or (lambda msg: None)      # log line
        self.on_progress = on_progress or (lambda p: None)  # dict per module update
        self.on_login_wait = on_login_wait or (lambda: None)
        self.on_login_done = on_login_done or (lambda: None)
        self.on_session = on_session or (lambda: None)   # session state changed (UI chip)
        self.state = RunState()
        self.portal = None          # the live Portal while a run is going (for Reset/Stop)

    def shutdown_browser(self):
        """Close the automation browser right now (used by Stop / Reset session)."""
        p, self.portal = self.portal, None
        if p is not None:
            try:
                p.stop()
            except Exception:
                pass
            return True
        return False

    def log(self, msg):
        line = f"{dt.datetime.now():%H:%M:%S} {msg}"
        self.on_event(line)

    # ---- scanning (offline, for the UI to show the job list) ----
    def preview(self, path: str, dc: str | None = None) -> dict:
        res = scan(path)
        items = []
        for j in res.jobs:
            vm, reason = j.meta.vm_to_select(dc)
            items.append({
                "module": j.module_name,
                "script": os.path.basename(j.script_path),
                "vm": vm or "",
                "vm_reason": reason,
                "kind": "multi-VM" if j.meta.is_multi_vm else ("cloud" if j.meta.is_cloud else "single-VM"),
                "ready": vm is not None,
                "status": "Pending",
            })
        return {
            "excel": res.excel_path,
            "module_count": res.module_count,
            "jobs": items,
            "missing": res.modules_without_script,
        }

    # ---- shared browser + sign-in + session lifecycle ----
    @contextmanager
    def _portal_session(self, profile_dir=None, headless=False, creds=None):
        from .portal import Portal
        session = SessionManager(_default_app_dir())
        profile_dir = profile_dir or session.profile_dir
        # SECURITY: if the agent has been idle longer than the allowed window (or was never
        # signed in), wipe the saved session so the user must re-authenticate.
        if session.enforce():
            if session.profile_left_behind():
                raise RuntimeError(
                    "The saved browser session had to be cleared for security (the agent was "
                    "idle too long) but some Chrome files are still locked. Close any open "
                    "Chrome window started by the agent and press Start again.")
            self.log("Security: saved session was cleared - sign-in required for verification.")
        with KeepAwake():
            with Portal(self.cfg, profile_dir=profile_dir, headless=headless, log=self.log,
                        cancel_check=lambda: self.state.cancel) as portal:
                self.portal = portal               # so Stop/Reset can close it immediately
                # The UI sign-in banner is raised ONLY if a manual sign-in is really needed,
                # and lowered the moment the session is good (or the attempt ends).
                try:
                    portal.ensure_logged_in(wait_seconds=600,
                                            on_wait=self.on_login_wait,
                                            on_ok=self.on_login_done,
                                            creds=creds)
                    portal.assert_expected_host()  # SECURITY: right host only
                    session.mark_authenticated()
                    self.on_session()
                except Exception:
                    self.on_login_done()           # never leave the banner up
                    raise
                try:
                    yield portal
                finally:
                    session.touch()                # idle clock starts when the agent goes quiet
                    self.on_session()
                    self.portal = None

    def _report_path(self, path: str, given: str | None = None) -> str:
        """Reports go in the folder the user picked, so they always know where to find them."""
        if given:
            return given
        name = f"Scoring-Agent-Report-{dt.datetime.now():%Y%m%d-%H%M%S}.xlsx"
        try:
            if os.path.isdir(path) and os.access(path, os.W_OK):
                return os.path.join(path, name)
        except Exception:
            pass
        fallback = _default_reports_dir()          # read-only input folder: keep the report safe
        os.makedirs(fallback, exist_ok=True)
        return os.path.join(fallback, name)

    def _save_report(self, report_path: str, results: list, mode: str) -> str:
        try:
            write_report(report_path, results, mode)
        except PermissionError:
            # The previous report is open in Excel, or the folder isn't writable after all.
            # Try a fresh name first, then a folder we know we can write to.
            try:
                report_path = report_path.replace(".xlsx", f"-{int(time.time())}.xlsx")
                write_report(report_path, results, mode)
            except OSError:
                fallback = _default_reports_dir()
                os.makedirs(fallback, exist_ok=True)
                report_path = os.path.join(fallback, os.path.basename(report_path))
                write_report(report_path, results, mode)
                self.log("   (the selected folder was not writable - report saved here instead)")
        self.log(f"Report: {report_path}")
        return report_path

    def _vm_map(self, path: str, dc: str | None = None) -> dict:
        try:
            return {j.module_name: (j.meta.vm_to_select(dc)[0] or "") for j in scan(path).jobs}
        except Exception:
            return {}

    # ---- the actual run ----
    def run(self, path: str, mode: str = "save", dc: str | None = None,
            profile_dir: str | None = None, headless: bool = False,
            report_path: str | None = None, creds: dict | None = None) -> dict:
        from .portal import PortalError  # lazy import (Selenium)

        res = scan(path)
        jobs = res.jobs
        do_live = (mode == "live")
        report_path = self._report_path(path, report_path)

        self.log(f"Input     : {path}")
        self.log(f"Excel     : {res.excel_path}")
        self.log(f"Modules   : {res.module_count}   with script: {len(jobs)}   missing: {len(res.modules_without_script)}")
        self.log(f"Mode      : {'MAKE LIVE (Save + Make Live)' if do_live else 'SAVE ONLY (InProgress)'}")

        results = []
        plan = []
        for j in jobs:
            vm, _ = j.meta.vm_to_select(dc)
            plan.append((j, vm))
        for m in res.modules_without_script:
            results.append({"module": m, "status": "Failed", "version": "",
                            "vm": "", "note": "no matching .ps1 script found"})
            self.on_progress({"module": m, "status": "Failed", "note": "no .ps1"})

        ok = fail = live = 0
        done_modules: set[str] = set()
        with self._portal_session(profile_dir, headless, creds) as portal:
            try:
                for idx, (job, vm) in enumerate(plan, 1):
                    if self.state.cancel:
                        results.append({"module": job.module_name, "status": "Failed",
                                        "version": "", "vm": vm or "", "note": "cancelled"})
                        done_modules.add(job.module_name)
                        continue
                    self.on_progress({"module": job.module_name, "status": "Running",
                                      "index": idx, "total": len(plan)})
                    self.log(f"[{idx}/{len(plan)}] {job.module_name}")
                    try:
                        if vm is None:
                            raise PortalError("could not decide the VM (multi-VM needs a DC name)")
                        status, version, note = self._process_one(portal, job, vm, do_live)
                        results.append({"module": job.module_name, "status": status,
                                        "version": version, "vm": vm, "note": note})
                        self.on_progress({"module": job.module_name, "status": status,
                                          "version": version, "vm": vm})
                        ok += 1
                        live += 1 if status == "Live" else 0
                        self.log(f"    OK: {status} (v{version}) on {vm}")
                    except Exception as e:
                        msg = str(e).splitlines()[0][:200] if str(e) else e.__class__.__name__
                        results.append({"module": job.module_name, "status": "Failed",
                                        "version": "", "vm": vm or "", "note": msg})
                        self.on_progress({"module": job.module_name, "status": "Failed", "note": msg})
                        fail += 1
                        self.log(f"    FAILED: {msg}")
                    done_modules.add(job.module_name)
            except Exception as fatal:
                # The browser was closed / crashed mid-batch. Don't lose the run: mark what is
                # left, then fall through and still write the report.
                msg = str(fatal).splitlines()[0][:200] if str(fatal) else fatal.__class__.__name__
                self.log(f"Run stopped: {msg}")
                for job, vm in plan:
                    if job.module_name in done_modules:
                        continue
                    results.append({"module": job.module_name, "status": "Failed",
                                    "version": "", "vm": vm or "", "note": f"not processed - {msg}"})
                    self.on_progress({"module": job.module_name, "status": "Failed",
                                      "note": "not processed"})
                    fail += 1

        report_path = self._save_report(report_path, results,
                                        "Make Live" if do_live else "Save only")
        self.log(f"Done. ok={ok} live={live} failed={fail} total={len(results)}")
        return {"ok": ok, "failed": fail, "live": live, "total": len(results),
                "report": report_path, "results": results, "mode": mode}

    # ---- publish modules that are already saved as InProgress ----
    def make_live(self, path: str, modules: list | None = None, dc: str | None = None,
                  profile_dir: str | None = None, headless: bool = False,
                  report_path: str | None = None, creds: dict | None = None) -> dict:
        """Take already-saved modules from InProgress to Live. Nothing is uploaded.

        This is exactly the manual workflow: open the Scripts list, search the module, and use
        its Make Live action - only for the modules the user asked for.
        """
        names = [str(m).strip() for m in (modules or []) if str(m).strip()]
        if not names:
            names = [j.module_name for j in scan(path).jobs]
        vms = self._vm_map(path, dc)
        report_path = self._report_path(path, report_path)

        self.log(f"Input     : {path}")
        self.log(f"Mode      : MAKE LIVE ONLY - {len(names)} module(s) already saved")

        results, ok, fail, live = [], 0, 0, 0
        done: set[str] = set()
        with self._portal_session(profile_dir, headless, creds) as portal:
            try:
                for idx, name in enumerate(names, 1):
                    if self.state.cancel:
                        results.append({"module": name, "status": "Failed", "version": "",
                                        "vm": vms.get(name, ""), "note": "cancelled"})
                        done.add(name)
                        continue
                    self.on_progress({"module": name, "status": "Running",
                                      "index": idx, "total": len(names)})
                    self.log(f"[{idx}/{len(names)}] {name}")
                    try:
                        version = self._make_live_one(portal, name)
                        results.append({"module": name, "status": "Live", "version": version,
                                        "vm": vms.get(name, ""), "note": "made live"})
                        self.on_progress({"module": name, "status": "Live", "version": version})
                        ok += 1
                        live += 1
                        self.log(f"    OK: Live (v{version})")
                    except Exception as e:
                        msg = str(e).splitlines()[0][:200] if str(e) else e.__class__.__name__
                        results.append({"module": name, "status": "Failed", "version": "",
                                        "vm": vms.get(name, ""), "note": msg})
                        self.on_progress({"module": name, "status": "Failed", "note": msg})
                        fail += 1
                        self.log(f"    FAILED: {msg}")
                    done.add(name)
            except Exception as fatal:
                msg = str(fatal).splitlines()[0][:200] if str(fatal) else fatal.__class__.__name__
                self.log(f"Run stopped: {msg}")
                for name in names:
                    if name in done:
                        continue
                    results.append({"module": name, "status": "Failed", "version": "",
                                    "vm": vms.get(name, ""), "note": f"not processed - {msg}"})
                    self.on_progress({"module": name, "status": "Failed", "note": "not processed"})
                    fail += 1

        report_path = self._save_report(report_path, results, "Make Live (already saved)")
        self.log(f"Done. live={live} failed={fail} total={len(results)}")
        return {"ok": ok, "failed": fail, "live": live, "total": len(results),
                "report": report_path, "results": results, "mode": "live_only"}

    def _make_live_one(self, portal, name: str, attempts: int = 3):
        last = None
        for attempt in range(1, attempts + 1):
            try:
                portal.ensure_list_ready(force=(attempt > 1))
                return portal.make_live_exact(name)
            except Exception as e:
                last = e
                if not portal.browser_alive():
                    raise RuntimeError("the Chrome window was closed - run stopped") from e
                self.log(f"    attempt {attempt}/{attempts} failed: {str(e).splitlines()[0][:120]}")
                if self.state.cancel:
                    break
                time.sleep(3)
        raise last

    def _process_one(self, portal, job: Job, vm: str, do_live: bool, attempts: int = 3):
        """Upload one module's script and (in live mode) publish it.

        The portal's "Processing..." dialog sometimes spins for minutes after a Save that in fact
        succeeded. So nothing here trusts the dialog: after Save we reload the Scripts list and
        VERIFY by searching for the module, and that verification is also what decides success.
        """
        from .portal import PortalError, AlreadyExistsError
        with open(job.script_path, encoding="utf-8-sig") as fh:
            body = fh.read()
        name = job.module_name
        want = str(self.cfg.version)
        last = None
        for attempt in range(1, attempts + 1):
            try:
                # Reuse the list that's already on screen; only reload it on a retry, where a
                # clean page is worth the wait. (Reloading per module cost ~a minute each.)
                portal.ensure_list_ready(force=(attempt > 1))

                # LOOK BEFORE YOU ADD. The portal refuses a second script at the same version
                # ("There is already a script for this lab with version 1"), so adding blind
                # only opens a dialog we then have to fight with - and a retry would repeat it.
                same = [r for r in portal.find_module_rows(name)
                        if str(r.version).strip() == want]
                live = [r for r in same if r.status == "Live"]
                prog = [r for r in same if r.status == "InProgress"]

                if live:
                    self.log(f"    already Live on the portal (v{live[0].version}) - leaving it alone.")
                    return ("Live", live[0].version, "already live on the portal - not re-added")
                if prog:
                    self.log(f"    already saved as InProgress (v{prog[0].version}) - not adding it again.")
                else:
                    portal.open_add_modal()
                    portal.fill_add_form(name, body, vm)
                    if not portal.click_save():
                        # Dialog stuck. Clear it, reload the list and let the search below tell
                        # us the truth about whether the save landed.
                        portal.dismiss_modal()
                        portal.goto_list()

                rows = self._verify_saved(portal, name)
                version = rows[0].version
                if not do_live:
                    note = "already saved as InProgress - not re-added" if prog else "saved as InProgress"
                    return ("InProgress", version, note)
                return ("Live", portal.make_live_exact(name), "made live")
            except AlreadyExistsError as e:
                # The portal says this version exists. Retrying can never change that - read the
                # row that is there, report it, and move on instead of looping on the dialog.
                self.log("    the portal already has this version - reading its current status.")
                portal.dismiss_modal()
                rows = portal.find_module_rows(name)
                match = [r for r in rows if str(r.version).strip() == want] or rows
                if not match:
                    raise PortalError(str(e))
                r = match[0]
                if r.status == "Live":
                    return ("Live", r.version, "already live on the portal - not re-added")
                if r.status == "InProgress" and do_live:
                    return ("Live", portal.make_live_exact(name), "made live (was already saved)")
                return (r.status, r.version, "already on the portal - not re-added")
            except Exception as e:
                last = e
                if not portal.browser_alive():
                    raise RuntimeError("the Chrome window was closed - run stopped") from e
                self.log(f"    attempt {attempt}/{attempts} failed: {str(e).splitlines()[0][:120]}")
                if self.state.cancel:
                    break
                time.sleep(3)
        raise last

    def _verify_saved(self, portal, module_name: str, tries: int = 3):
        """Search the list for the module's InProgress row, reloading if it isn't there yet."""
        from .portal import PortalError
        for i in range(1, tries + 1):
            rows = portal.find_module_rows(module_name, status="InProgress")
            if len(rows) > 1:
                self.log(f"    note: {len(rows)} InProgress rows for this module - "
                         "using the highest version.")
                rows.sort(key=lambda r: (len(r.version), r.version), reverse=True)
            if rows:
                if i > 1:
                    self.log("    verified on the reloaded list - the save had gone through.")
                return rows
            if i < tries:
                self.log(f"    not on the list yet - reloading and checking again ({i}/{tries - 1})")
                portal.dismiss_modal()
                portal.goto_list()
                time.sleep(2)
        raise PortalError(
            "Saved, but no InProgress row for this module appeared on the Scripts list.")
