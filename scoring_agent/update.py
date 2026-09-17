"""Check GitHub Releases for a newer Scoring Agent and fetch its installer.

How it hangs together
---------------------
- The team's repo is set once in the app's Updates dialog and kept in `settings.json`
  (`owner/repo` - not a secret, so it is fine on disk next to the other app data).
- A check reads `https://api.github.com/repos/<owner>/<repo>/releases/latest` and compares the
  release tag (`v1.2.3`) with `version.__version__`.
- The download only ever comes from that release's own assets, over HTTPS, from a github.com /
  githubusercontent.com host - anything else is refused.
- Nothing installs itself: the user presses the button, the setup is downloaded, its size is
  checked against what the release advertised, and only then is the installer launched.

The installer does the rest: same AppId (so it upgrades in place), `CloseApplications=yes`, and
an `[InstallDelete]` that wipes the old `_internal` first, so a new version can never end up
mixed with the previous one.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request

from .paths import app_dir
from .version import __version__ as CURRENT

GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
TIMEOUT = 20


class UpdateError(RuntimeError):
    pass


# --------------------------------------------------------------------- settings
def _settings_path() -> str:
    return os.path.join(app_dir(), "settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_settings(data: dict):
    p = _settings_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def get_repo() -> str:
    return str(load_settings().get("update_repo") or "").strip()


def set_repo(repo: str) -> str:
    repo = (repo or "").strip().rstrip("/")
    # tolerate someone pasting the whole URL
    m = re.search(r"github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", repo)
    if m:
        repo = m.group(1)
    if repo and not REPO_RE.match(repo):
        raise UpdateError("That does not look like a GitHub repository. Use owner/repo, "
                          "for example xtremelabs/scoring-agent.")
    s = load_settings()
    s["update_repo"] = repo
    save_settings(s)
    return repo


# --------------------------------------------------------------------- versions
def parse_version(text: str) -> tuple:
    """'v1.2.10' -> (1, 2, 10). Anything unparseable sorts lowest."""
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(candidate: str, current: str = CURRENT) -> bool:
    return parse_version(candidate) > parse_version(current)


# --------------------------------------------------------------------- the check
def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ScoringAgent/" + CURRENT,
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def check(repo: str | None = None) -> dict:
    """What the Updates dialog shows. Never raises for the ordinary 'no update' cases."""
    repo = (repo or get_repo()).strip()
    if not repo:
        return {"ok": False, "reason": "no_repo", "current": CURRENT}
    if not REPO_RE.match(repo):
        return {"ok": False, "reason": "bad_repo", "current": CURRENT}

    try:
        rel = _get_json(GITHUB_API.format(repo=repo))
    except urllib.error.HTTPError as e:
        reason = "not_found" if e.code == 404 else f"http_{e.code}"
        return {"ok": False, "reason": reason, "current": CURRENT}
    except Exception:
        return {"ok": False, "reason": "offline", "current": CURRENT}

    tag = rel.get("tag_name") or rel.get("name") or ""
    asset = None
    for a in rel.get("assets") or []:
        name = a.get("name") or ""
        if name.lower().endswith(".exe"):
            asset = a
            break

    return {
        "ok": True,
        "current": CURRENT,
        "latest": str(tag).lstrip("vV") or "?",
        "newer": is_newer(tag),
        "notes": (rel.get("body") or "").strip()[:1500],
        "published": (rel.get("published_at") or "")[:10],
        "asset_name": (asset or {}).get("name"),
        "asset_url": (asset or {}).get("browser_download_url"),
        "asset_size": (asset or {}).get("size") or 0,
        "page": rel.get("html_url"),
        "repo": repo,
    }


# --------------------------------------------------------------------- the download
def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()


def download(url: str, expected_size: int = 0, on_progress=None) -> str:
    """Fetch the release's installer to a temp folder and hand back the path."""
    from urllib.parse import urlparse
    if urlparse(url).scheme != "https":
        raise UpdateError("Refusing to download an update over anything but HTTPS.")
    host = _host_of(url)
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise UpdateError(f"Refusing to download an update from '{host}'.")

    dest_dir = os.path.join(app_dir(), "updates")
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(urlparse(url).path) or "ScoringAgent-Setup.exe"
    if not name.lower().endswith(".exe"):
        raise UpdateError("The release asset is not an installer (.exe).")
    dest = os.path.join(dest_dir, name)

    req = urllib.request.Request(url, headers={"User-Agent": "ScoringAgent/" + CURRENT})
    ctx = ssl.create_default_context()
    got = 0
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        # the redirect GitHub sends must stay on an allowed host too
        final = _host_of(r.geturl())
        if not any(final == h or final.endswith("." + h) for h in ALLOWED_HOSTS):
            raise UpdateError(f"The download was redirected to '{final}' - refusing it.")
        total = int(r.headers.get("Content-Length") or expected_size or 0)
        with open(dest, "wb") as fh:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if on_progress and total:
                    on_progress(int(got * 100 / total))

    if expected_size and got != expected_size:
        os.remove(dest)
        raise UpdateError("The download did not match the size the release advertised - "
                          "it was discarded. Try again.")
    if got < 1024 * 1024:
        os.remove(dest)
        raise UpdateError("The downloaded installer is too small to be real - discarded.")
    if on_progress:
        on_progress(100)
    return dest


def launch_installer(path: str):
    """Start the downloaded setup. The app should close right after so nothing is locked."""
    if not (path and os.path.isfile(path) and path.lower().endswith(".exe")):
        raise UpdateError("The downloaded installer has gone missing.")
    os.startfile(path)  # noqa - the user asked for this
