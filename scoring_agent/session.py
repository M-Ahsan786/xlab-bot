"""Session & credential-cache security for Scoring Agent.

The tool never stores a username or password. The only thing that persists a login is the
Chrome profile's session cookie. To keep that safe:

- **Re-auth after inactivity:** if the agent has not been used for more than MAX_AGE (3 hours),
  the saved session is wiped so the user must sign in again for verification. This stops a
  stale, unattended session from being reused (a breach risk). The clock is the LAST USE, not
  the login: `touch()` is called as a run finishes, so a long batch never expires mid-way and
  the 3 hours start counting from when the agent actually went idle.
- **Proper cache cleanup:** wiping the session removes the whole Chrome profile directory, so
  no cookies / cached credentials are left behind. `sign_out()` does the same on demand.
- The browser is also launched with the password manager disabled (see portal.py) so the
  user's password is never cached into the tool's profile.
"""
from __future__ import annotations
import json
import os
import shutil
import time

MAX_AGE_SECONDS = 3 * 60 * 60  # re-authenticate after 3 hours of not using the agent


class SessionManager:
    def __init__(self, app_dir: str, max_age: int = MAX_AGE_SECONDS):
        self.app_dir = app_dir
        self.max_age = max_age
        self.profile_dir = os.path.join(app_dir, "chrome-profile")
        self.meta_path = os.path.join(app_dir, "session", "meta.json")

    # ---- state ----
    def _read(self) -> dict:
        try:
            with open(self.meta_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _write(self, data: dict):
        os.makedirs(os.path.dirname(self.meta_path), exist_ok=True)
        with open(self.meta_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def last_auth(self) -> float | None:
        v = self._read().get("last_auth")
        return float(v) if v else None

    def age_seconds(self):
        la = self.last_auth()
        return None if la is None else max(0, time.time() - la)

    def needs_reauth(self) -> bool:
        la = self.last_auth()
        if la is None:
            return True
        return (time.time() - la) > self.max_age

    # ---- actions ----
    def clear(self) -> bool:
        """Remove the saved session + cached data (Chrome profile) and the auth marker.

        Chrome can hold the profile files open for a moment after it quits, so the delete is
        retried briefly instead of silently leaving cookies behind.
        """
        if os.path.isdir(self.profile_dir):
            for _ in range(6):
                shutil.rmtree(self.profile_dir, ignore_errors=True)
                if not os.path.isdir(self.profile_dir):
                    break
                time.sleep(0.5)
        try:
            if os.path.isfile(self.meta_path):
                os.remove(self.meta_path)
        except Exception:
            pass
        # Tell the truth: if Chrome still holds the profile, the saved cookie is NOT gone.
        return not os.path.isdir(self.profile_dir)

    def profile_left_behind(self) -> bool:
        """True if the Chrome profile could not be fully deleted (browser still holding it)."""
        return os.path.isdir(self.profile_dir)

    def enforce(self) -> bool:
        """Before a run: if the session is stale, wipe it so the user re-authenticates.
        Returns True if a re-auth is now required."""
        if self.needs_reauth():
            self.clear()
            return True
        return False

    def mark_authenticated(self):
        self._write({"last_auth": time.time()})

    def touch(self):
        """Restart the idle clock (called when a run finishes) - only if already signed in."""
        if self.last_auth() is not None:
            self.mark_authenticated()

    def sign_out(self) -> bool:
        return self.clear()

    def status(self) -> dict:
        age = self.age_seconds()
        return {
            "authenticated": (age is not None) and (age <= self.max_age),
            "age_seconds": age,
            "needs_reauth": self.needs_reauth(),
            "max_age_hours": round(self.max_age / 3600, 1),
        }
