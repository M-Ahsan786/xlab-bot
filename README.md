# Scoring Agent

A modern desktop app that automates uploading and publishing XtremeLabs scoring scripts.
The user only picks a folder and presses **Start** — everything else runs in the background,
and a formatted **Excel report** is produced at the end.

Developed by **Hafiz Muhammad Ahsan**.

## What the user does

1. Launch **Scoring Agent**.
2. **Browse** to the course folder. It must contain:
   - one **Excel** file with the module/lab names in a column headed **Module**, **Modules**,
     **Lab**, **Labs** (or *Module Name* / *Lab Name*) — capitalisation and spacing don't
     matter, and the heading doesn't have to be on the first row, and
   - the scoring scripts as **`.ps1`** files whose name matches the module name (anywhere under
     the folder, e.g. a `Scoring Scripts` subfolder).
3. The app shows the **job list** (module, VM, type) and how many modules have a script.
4. Choose the mode:
   - **Save as InProgress** — uploads each script and leaves it InProgress (you publish later).
   - **Make Live** — uploads and takes each one all the way to **Live**.
5. *(optional)* Fill in **Portal sign-in** — your XtremeLabs username and password. The agent
   then fills the portal's own sign-in page for you and ticks **Keep me signed in**. If the page
   shows a **CAPTCHA** it fills what it can and waits for you to finish that bit by hand. Leave
   the fields blank to sign in yourself in the browser, as before.
6. Press **Start**. A Chrome window opens (for the sign-in, and for the work). The run then
   proceeds on its own — even if the laptop would otherwise sleep.
   *The very first run on a machine can take a minute while the browser driver is prepared —
   the app says so on screen.*
7. When it finishes, open the **Excel report**: Module Name, Status, Version, VM, Notes. It is
   written **into the folder you picked** (with a matching `Scoring-Agent-Log-*.txt`), so it sits
   next to the scripts it describes. If that folder isn't writable it falls back to
   **Documents\Scoring Agent Reports**; the **Folder** button opens wherever it landed.
8. Modules left at **InProgress** can be published later without re-uploading: press
   **Make live** on a row, or **Make all live** in the Job list header.

## Security

- **No credentials are ever stored.** If you use the optional sign-in form, what you type is
  held in memory for that run only, typed into the portal's own sign-in page, and cleared the
  moment sign-in is attempted — it is never written to disk, the activity log, the `.txt` log
  file or the report. Only the session cookie persists, in a local Chrome profile, and the
  browser is launched with Chrome's password manager and autofill **disabled** so your password
  is never cached into that profile either.
- **CAPTCHAs are never automated.** If the sign-in page shows one, the agent stops and hands it
  to you.
- **Re-authentication after inactivity.** If the agent has not been used for **3 hours** (or
  was never signed in), the saved session is wiped before a run and you must sign in again — a
  stale, unattended session can't be reused. The clock restarts when a run finishes, so a long
  batch never expires half-way.
- **Sign out** (header button) clears the saved session/cache on demand.
- **Reset session** (header button) does the lot without restarting the app: stops anything
  running, closes the automation browser, clears the saved login, and puts the screen back to a
  fresh state. If a run was in progress it is stopped and the report is still written.
- **Host lock.** The tool refuses to act unless the browser is on the configured portal host
  (`labs.xtremelabs.io`); it never operates on any other site.
- **No blind publishing.** A row counts as the module only on an **exact** name match (the
  portal's trailing `(…)` suffix is tolerated) — a longer lab that merely *contains* the name is
  never touched. Make Live acts only when exactly one InProgress row matches; anything uncertain
  is reported as Failed, never guessed.

## How VM selection works

- **Single-VM** script → the VM named by `$RemoteServer` inside the script (auto).
- **Multi-VM** script → the **DC** (auto-detected by name; the run flags any it can't decide).

## Robustness

- The portal's script list is large and slow to load; the app waits generously (up to ~2 min per
  page) so a big batch (e.g. 100 modules) completes without timing out.
- The sign-in page carries a reCAPTCHA that can keep loading forever, so pages are loaded with
  the **eager** strategy and a stalled load is stopped and carried on from — no more
  *"timed out receiving message from renderer"* aborting a run.
- Each module is retried a few times on a transient glitch. If the Chrome window is closed
  mid-batch the run stops cleanly and the report is **still written**, with the modules that
  never ran marked as such.
- The machine is kept awake for the whole run.
- The app opens in about 1-2 seconds and only one copy can run at a time (a second launch just
  focuses the window that is already open).
- **The window is Chrome in `--app` mode** (no tabs, no address bar) talking to a small local
  server. It used to be an embedded WebView2 window, which locked up solid on roughly one launch
  in ten - blocked with 0% CPU and never recovering. Measured after the change: 20 launches,
  zero stalls. Chrome is already required, since the automation drives it.
- Exact module matching (with the portal's `(…)` suffix tolerated) — it never publishes to the
  wrong module or version; anything uncertain is reported as Failed, not guessed.

## For developers — build & package

```
# 1) Build the app (needs Google Chrome on the target machine; WebView2 ships with Win 11)
powershell -NoProfile -ExecutionPolicy Bypass -File build_app.ps1 -Clean
#    -> dist\ScoringAgent\ScoringAgent.exe   (onedir - see the note in build_app.ps1:
#       the old onefile build unpacked ~48 MB per launch and looked frozen for ~25 s)

# 2) Build the Windows installer (needs Inno Setup 6: winget install JRSoftware.InnoSetup)
powershell -NoProfile -ExecutionPolicy Bypass -File build_installer.ps1
#    -> installer\Output\ScoringAgent-Setup-<version>.exe  (with EULA page + developer info)
```

Run from source (no build): `python -m scoring_agent`

## Layout

```
scoring_agent/
  app.py         the app: the API the window calls, and start-up
  server.py      the local HTTP/SSE server the window talks to (127.0.0.1, token-guarded)
  shell.py       finds Chrome and opens the app window
  folderdialog.py  Windows' own "pick a folder" dialog
  update.py      checks GitHub Releases and fetches the installer
  version.py     the one place the version number lives
  ui/            modern web UI (index.html, app.js) - job list + progress + report
  engine.py      run orchestration: modes, keep-awake, retries, report
  jobs.py        read the Module Excel + match .ps1 scripts -> job list
  portal.py      Selenium automation (login, add, save, make live) - calibrated to the portal
  scriptmeta.py  read a .ps1 to decide the VM (single vs multi/DC)
  report.py      the formatted Excel summary report
  keepawake.py   stops Windows sleeping during a run
  paths.py       where writable data lives (kept engine-free so the UI starts instantly)
  config.py      portal URLs + DOM selectors
assets/          app icon
installer/        EULA.txt + Inno Setup script
build_app.ps1 / build_installer.ps1
```
