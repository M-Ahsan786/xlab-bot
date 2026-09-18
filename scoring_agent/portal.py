"""Selenium driver for the XtremeLabs Scoring portal.

Design notes
------------
- Uses the teammate's own installed Chrome with a PERSISTENT profile directory, so they log
  in ONCE (manually) and the session is reused on every later run. The tool never types or
  stores credentials.
- Every element is located by trying a list of candidate selectors from config.Selectors, so
  the portal markup can shift without a code change (adjust config / portal.json instead).
- The Script Body is a code editor; `paste_script` tries CodeMirror, then ACE, then a plain
  textarea, and verifies the text landed.
- Safety: exact module matching for the Lab autocomplete, and Make Live only ever acts on a
  single InProgress row for the exact module - otherwise it refuses and reports.
"""
from __future__ import annotations
import time
from dataclasses import dataclass

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.common.exceptions import (TimeoutException,
                                        WebDriverException, NoSuchWindowException,
                                        InvalidSessionIdException,
                                        ElementClickInterceptedException,
                                        ElementNotInteractableException)

from .config import Config

# Substrings that really do mean the browser/session is gone (anything else is just a hiccup).
_DEAD_BROWSER_MARKERS = (
    "no such window", "invalid session id", "target window already closed",
    "web view not found", "browser has closed", "chrome not reachable",
    "disconnected: not connected to devtools", "session deleted",
    "failed to check if window was closed",
)


class PortalError(RuntimeError):
    pass


class AlreadyExistsError(PortalError):
    """The portal refuses the upload because this lab already has a script at this version.

    Retrying can never help - the caller reports the row that is already there instead.
    """


@dataclass
class RowInfo:
    lab: str
    status: str
    version: str
    agent: str
    makelive_href: str | None


def _is_xpath(sel: str) -> bool:
    return sel.strip().startswith(("/", "(", "./"))


class Portal:
    def __init__(self, cfg: Config, profile_dir: str, headless: bool = False,
                 log=print, cancel_check=None):
        self.cfg = cfg
        self.sel = cfg.selectors
        self.profile_dir = profile_dir
        self.headless = headless
        self.log = log
        self.cancel_check = cancel_check or (lambda: False)
        self.driver = None

    # ---------- lifecycle ----------
    def start(self):
        opts = Options()
        opts.add_argument(f"--user-data-dir={self.profile_dir}")
        opts.add_argument("--profile-directory=Default")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        # Trim the work Chrome does at startup so the window appears quickly.
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-sync")
        opts.add_argument("--disable-component-update")
        opts.add_argument("--disable-features=Translate,OptimizationHints,MediaRouter")
        opts.add_argument("--start-maximized")
        # Keep working when the user minimises the window or switches to another app. Without
        # these, Chrome throttles timers and pauses background renderers, which stalls the
        # portal's AJAX mid-run.
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-backgrounding-occluded-windows")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument("--disable-ipc-flooding-protection")
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        # SECURITY: do not let Chrome cache the user's password/credentials into the profile.
        opts.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "autofill.profile_enabled": False,
        })
        # The sign-in page carries a reCAPTCHA whose sub-resources can keep loading for a long
        # time. Waiting for a *complete* load makes Chrome look hung and produces
        # "timed out receiving message from renderer". 'eager' returns at DOMContentLoaded,
        # which is all we need to read/click the page.
        opts.page_load_strategy = "eager"
        # Selenium Manager (built into Selenium 4.6+) auto-resolves chromedriver. The very first
        # run on a machine downloads it, which can take a minute - say so instead of looking hung.
        self.log("[browser] Starting Chrome... (the first run can take a minute while the "
                 "browser driver is prepared)")
        t0 = time.time()
        try:
            self.driver = webdriver.Chrome(options=opts)
        except Exception as e:
            raise PortalError(
                "Could not start Chrome. Make sure Google Chrome is installed and closed, "
                f"then try again.\nDetails: {str(e).splitlines()[0][:200]}")
        # The portal's script list (2000+ rows) can take a minute, so be generous. 'eager' above
        # is what stops the sign-in page's reCAPTCHA from hanging the load, not a short timeout.
        self.driver.set_page_load_timeout(150)
        self.driver.set_script_timeout(120)
        self.log(f"[browser] Chrome ready in {time.time() - t0:.0f}s.")
        return self

    def _safe_get(self, url: str):
        """Navigate, tolerating a slow/never-finishing page (reCAPTCHA, ads, etc.)."""
        try:
            self.driver.get(url)
        except TimeoutException:
            self.log("   [warn] page load timed out - continuing with whatever rendered.")
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass
        except WebDriverException as e:
            # renderer hiccup: stop the load and carry on; the polling below still works
            self.log(f"   [warn] navigation issue ({str(e).splitlines()[0][:80]}) - continuing.")
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass

    # ---------- security ----------
    def expected_host(self) -> str:
        from urllib.parse import urlparse
        return (urlparse(self.cfg.base_url).hostname or "").lower()

    def assert_expected_host(self):
        """Never operate on anything but the configured portal host."""
        from urllib.parse import urlparse
        exp = self.expected_host()
        url, last = None, None
        for _ in range(3):                       # a busy renderer can delay the URL read
            try:
                url = self.driver.current_url or ""
                break
            except Exception as e:
                last = e
                time.sleep(1.5)
        if url is None:
            raise PortalError(f"Could not read the browser's address to verify the portal host ({last}).")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        # Fail CLOSED: about:blank / file:// / chrome-error:// have no hostname, and those are
        # exactly the states a crash or a hijacked navigation lands on.
        if not host:
            raise PortalError(
                f"Refusing to act: the browser is not on the portal (address: {url[:80] or 'blank'}).")
        if host != exp:
            raise PortalError(f"Refusing to act: browser is on '{host}', expected '{exp}'.")
        if parsed.scheme and parsed.scheme not in ("https",):
            raise PortalError(f"Refusing to act: the portal was reached over '{parsed.scheme}', not https.")

    def stop(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---------- helpers ----------
    def _find(self, candidates, timeout=10, root=None, required=True):
        root = root or self.driver
        end = time.time() + timeout
        last = None
        while time.time() < end:
            for sel in candidates:
                try:
                    by = By.XPATH if _is_xpath(sel) else By.CSS_SELECTOR
                    els = root.find_elements(by, sel)
                    for el in els:
                        if el.is_displayed():
                            return el
                    if els:
                        last = els[0]
                except Exception:
                    continue
            time.sleep(0.25)
        if last is not None:
            return last
        if required:
            raise PortalError(f"None of these selectors matched: {candidates}")
        return None

    def _find_all(self, candidates, root=None):
        root = root or self.driver
        for sel in candidates:
            by = By.XPATH if _is_xpath(sel) else By.CSS_SELECTOR
            els = root.find_elements(by, sel)
            if els:
                return els
        return []

    def browser_alive(self) -> bool:
        """False ONLY when the Chrome window/session is really gone.

        This must never guess. A busy renderer (the portal's script list is huge and takes ~a
        minute) makes a WebDriver command time out - that is a *busy* browser, not a dead one.
        Treating a timeout as "closed" used to abort the run and quit Chrome right after login.
        """
        if self.driver is None:
            return False
        try:
            return len(self.driver.window_handles) > 0
        except (NoSuchWindowException, InvalidSessionIdException):
            return False
        except TimeoutException:
            return True                      # just busy
        except WebDriverException as e:
            msg = (str(e) or "").lower()
            return not any(k in msg for k in _DEAD_BROWSER_MARKERS)
        except Exception:
            return True                      # unknown hiccup - assume alive, don't kill the run

    def bring_to_front(self):
        """Raise the automation browser so the user can see which window is being driven."""
        try:
            self.driver.execute_cdp_cmd("Page.bringToFront", {})
            return True
        except Exception:
            pass
        try:
            self.driver.maximize_window()    # older Chrome / no CDP: still raises it
            return True
        except Exception:
            return False

    def _where(self) -> str:
        """Current URL, for the log - never raises."""
        try:
            return (self.driver.current_url or "?")[:90]
        except Exception:
            return "page busy"

    # ---------- auth ----------
    def captcha_present(self) -> bool:
        return self._find(self.sel.captcha, timeout=1, required=False) is not None

    def try_auto_login(self, username: str, password: str) -> str:
        """Fill the portal's sign-in page with the user's own credentials.

        Returns what happened: "submitted", "captcha" (filled, waiting for the user to solve
        it and press the button), or "unavailable" (no form found - sign in by hand).
        The password is used here and nowhere else: never logged, never written down.
        """
        user_el = self._find(self.sel.login_user, timeout=8, required=False)
        pass_el = self._find(self.sel.login_pass, timeout=5, required=False)
        if user_el is None or pass_el is None:
            self.log("   [auth] no sign-in form on this page - please sign in manually.")
            return "unavailable"

        self._type(user_el, username, "username")
        self._type(pass_el, password, "password")      # value is never echoed anywhere

        # "Keep me signed in", so the saved session survives between runs
        box = self._find(self.sel.login_remember, timeout=3, required=False)
        if box is not None:
            try:
                if not box.is_selected():
                    self._click(box, "keep me signed in")
            except Exception:
                pass

        if self.captcha_present():
            # Say so, but still press the button - Ahsan solves the CAPTCHA himself and presses
            # it again. Refusing to click just means he has to do the typing as well.
            self.log("   [auth] the page has a CAPTCHA - if sign-in does not go through, "
                     "solve it and press SIGN IN again.")

        if not self.click_signin():
            self.log("   [auth] could not find the sign-in button - press it yourself.")
            return "captcha"
        self.log("   [auth] signed in with the details you entered; waiting for the portal...")
        return "submitted"

    def click_signin(self) -> bool:
        """Press the sign-in button on the portal's own login page.

        Found from the PASSWORD field's own form, because the real page's button is just
        `<button>SIGN IN -></button>`: no type attribute (so `button[type=submit]` misses it)
        and the text carries an arrow (so an exact-text match misses it too). Starting from the
        form also keeps us away from the "continue with Google/LinkedIn/Microsoft" buttons.
        """
        js = r"""
        const pw = document.querySelector("input[type='password']");
        const scope = (pw && pw.form) || document;
        let b = scope.querySelector("button[type='submit'], input[type='submit']");
        if (!b) {
          const social = /google|linkedin|microsoft|facebook|apple|github|sso|continue with/i;
          const wanted = /sign\s*in|log\s*in|log\s*on|submit|continue/i;
          b = [...scope.querySelectorAll('button, input[type=button]')].find(x => {
            const t = (x.textContent || x.value || '') + ' ' + (x.getAttribute('aria-label') || '');
            return wanted.test(t) && !social.test(t);
          });
        }
        if (!b) return null;
        b.scrollIntoView({block:'center'});
        b.click();
        return (b.textContent || b.value || '').trim().slice(0, 40);
        """
        try:
            what = self.driver.execute_script(js)
        except Exception:
            what = None
        if what is None:
            # last resort: whatever the configured selectors can find
            el = self._find(self.sel.login_submit, timeout=3, required=False)
            if el is None:
                return False
            self._click(el, "sign-in button")
            return True
        self.log(f"   [auth] pressed '{what}'.")
        return True

    def login_page_message(self) -> str:
        """Whatever the sign-in page is complaining about (wrong password, locked out, ...)."""
        try:
            return (self.driver.execute_script(
                "const e = document.querySelector("
                "'.validation-summary-errors, .text-danger, .alert-danger, .field-validation-error');"
                "return e ? e.innerText.trim() : '';") or "").strip()
        except Exception:
            return ""

    def ensure_logged_in(self, wait_seconds=600, on_wait=None, on_ok=None, creds=None) -> bool:
        """Open the scoring page; if not logged in, wait for the user to log in by hand.

        `on_wait()` fires only when a manual sign-in is actually needed, and `on_ok()` once the
        session is good - so the UI's "log in" banner is never shown/left up by guesswork.
        """
        self._safe_get(self.cfg.scoring_url)
        self.bring_to_front()          # so it is obvious which browser the agent is driving
        # Give the first check a longer grace period: with the 'eager' strategy the page can
        # still be settling, and we don't want to flash "log in required" on a good session.
        if self._logged_in(timeout=15):
            self.log("[auth] existing session detected - already logged in.")
            if on_ok:
                on_ok()
            return True
        if creds and creds.get("username") and creds.get("password"):
            # Use the details the user typed into the app, then fall through to the same wait
            # below - which is what catches a CAPTCHA, a typo, or a slow portal.
            try:
                outcome = self.try_auto_login(creds["username"], creds["password"])
            except Exception as e:
                outcome = "unavailable"
                self.log(f"   [auth] could not fill the sign-in form ({e.__class__.__name__}) "
                         "- please sign in manually.")
            finally:
                creds["password"] = ""          # done with it - do not keep it around
            if outcome == "submitted":
                end = time.time() + 45          # give the portal a moment to come back
                while time.time() < end:
                    if self._logged_in(timeout=2):
                        self.log("[auth] Signed in.")
                        if on_ok:
                            on_ok()
                        return True
                    msg = self.login_page_message()
                    if msg:
                        self.log(f"   [auth] the portal said: {msg}")
                        break
                    time.sleep(1.5)

        if on_wait:
            on_wait()
        self.log("[auth] Not logged in. A browser window is open - please LOG IN there.")
        self.log(f"[auth] Waiting up to {int(wait_seconds/60)} min for login to complete...")
        end = time.time() + wait_seconds
        next_note = time.time() + 30
        dead_reads = 0
        while time.time() < end:
            if self.cancel_check():
                raise PortalError("Stopped before sign-in finished.")
            # Only give up after two readings in a row say the window is gone - one is never
            # enough, because the portal keeps the renderer busy for a long time after login.
            if self.browser_alive():
                dead_reads = 0
            else:
                dead_reads += 1
                if dead_reads >= 2:
                    raise PortalError(
                        "The Chrome window was closed before sign-in finished. "
                        "Press Start again and log in when the browser opens.")
            if self._logged_in():
                self.log("[auth] Login detected. Continuing.")
                if on_ok:
                    on_ok()
                return True
            if time.time() >= next_note:
                left = int(end - time.time())
                self.log(f"   [auth] still waiting for sign-in... {left}s left  (at {self._where()})")
                next_note = time.time() + 30
            time.sleep(1.5)
        raise PortalError(
            "Timed out waiting for the manual sign-in. Press Start again and log in "
            "in the Chrome window that opens.")

    def _logged_in(self, timeout: int = 3) -> bool:
        # On the login page even a "Welcome" heading shows, so first rule out the LogOn URL.
        try:
            url = (self.driver.current_url or "").lower()
        except Exception:
            return False
        if "/account/logon" in url or "/account/login" in url:
            return False
        # Cheap DOM check first: it doesn't care whether the header is scrolled out of view or
        # the page is still filling in, which the visibility-based lookup below does.
        try:
            if self.driver.execute_script(
                    "return [...document.querySelectorAll('a')].some("
                    "a => /log\\s*off|sign\\s*out/i.test(a.textContent || ''))"
                    " || !!document.querySelector(\"a[href*='LogOff' i]\");"):
                return True
        except Exception:
            pass
        try:
            return self._find(self.sel.logged_in_marker, timeout=timeout, required=False) is not None
        except Exception:
            return False


    # ---------- interaction (overlay- and minimise-proof) ----------
    def modal_open(self) -> bool:
        """Is a dialog actually on screen?

        Bootstrap leaves the modal in the DOM and just hides it, and `_find` falls back to a
        hidden element, so a `_find(modal) is None` test reads "still open" forever. Ask the
        page what is really visible instead.
        """
        try:
            return bool(self.driver.execute_script(
                "return [...document.querySelectorAll('div.modal')].some(m => {"
                "  const cs = getComputedStyle(m);"
                "  if (cs.display === 'none' || cs.visibility === 'hidden') return false;"
                "  return m.classList.contains('in') || m.classList.contains('show')"
                "         || cs.display === 'block';"
                "});"))
        except Exception:
            return False

    def modal_message(self) -> str:
        """Any validation text the open dialog is showing (the portal puts it in red at the top)."""
        try:
            return (self.driver.execute_script(
                "const m = [...document.querySelectorAll('div.modal')].find(x => {"
                "  const cs = getComputedStyle(x);"
                "  return cs.display !== 'none' && cs.visibility !== 'hidden';"
                "});"
                "if (!m) return '';"
                "const e = m.querySelector('.text-danger, .validation-summary-errors, .alert-danger');"
                "return e ? e.innerText.trim() : '';") or "").strip()
        except Exception:
            return ""

    def _mask_visible(self) -> bool:
        """The portal drops a full-screen loading mask over the page during every AJAX call."""
        try:
            return bool(self.driver.execute_script(
                "const m = document.querySelector('#full-screen-loading-mask');"
                "if (!m) return false;"
                "const cs = getComputedStyle(m);"
                "return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';"))
        except Exception:
            return False

    def wait_mask_gone(self, timeout: int = 120) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if not self._mask_visible():
                return True
            time.sleep(0.3)
        return False

    def _click(self, el, what: str = "element"):
        """Click reliably: wait out the loading mask, scroll into view, then native click -
        falling back to a JS click, which also works while the window is minimised."""
        self.wait_mask_gone(60)
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', behavior:'instant'});", el)
        except Exception:
            pass
        try:
            el.click()
            return
        except (ElementClickInterceptedException, ElementNotInteractableException,
                WebDriverException) as e:
            self.log(f"   [click] native click on {what} blocked ({e.__class__.__name__}) - using JS click.")
        self.driver.execute_script("arguments[0].click();", el)

    def _type(self, el, text: str, what: str = "field"):
        """Type real key events; if the element can't take them (minimised/overlaid), set the
        value in JS and fire the events the portal's own handlers listen for."""
        try:
            el.clear()
            el.send_keys(text)
            return
        except (ElementNotInteractableException, ElementClickInterceptedException,
                WebDriverException) as e:
            self.log(f"   [type] send_keys to {what} failed ({e.__class__.__name__}) - using JS input.")
        self.driver.execute_script(
            "const el = arguments[0], v = arguments[1];"
            "el.focus(); el.value = v;"
            "el.dispatchEvent(new Event('input', {bubbles:true}));"
            "el.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:'a'}));"
            "el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, key:'a'}));"
            "el.dispatchEvent(new Event('change', {bubbles:true}));", el, text)

    # ---------- list page ----------
    def dismiss_modal(self):
        """Close a leftover dialog so it can't sit over the page."""
        try:
            self.driver.execute_script(
                "document.querySelectorAll('div.modal').forEach(m => {"
                "  m.classList.remove('in','show'); m.style.display='none';"
                "});"
                "document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());"
                "document.body.classList.remove('modal-open');")
        except Exception:
            pass

    def goto_list(self):
        # /ScriptList is an AJAX partial (404 as a full page). Load the Scoring page and, if the
        # Scripts DataTable isn't already showing, click the Scripts tab (an AJAX tab). A JS
        # click is used so an overlay/loading-mask or a not-yet-"displayed" state can't block it.
        self._safe_get(self.cfg.scoring_url)
        self.assert_expected_host()  # SECURITY: only ever act on the configured portal
        try:
            WebDriverWait(self.driver, 20).until(
                lambda d: d.execute_script("return document.readyState") in ("interactive", "complete"))
        except Exception:
            pass
        if self._find(self.sel.table_row, timeout=4, required=False) is not None:
            return
        clicked = self.driver.execute_script(
            "const a = document.querySelector(\"a[href='/Admin/Scoring/ScriptList']\")"
            " || [...document.querySelectorAll('a')].find(x => x.textContent.trim()==='Scripts');"
            "if (a) { a.click(); return true; } return false;")
        if not clicked:
            info = self.driver.execute_script(
                "return {url:location.href, title:document.title};")
            self.log(f"   [debug] url={info.get('url')} title={info.get('title')}")
            raise PortalError("Could not find the Scripts tab on the Scoring page.")
        # The tab content (Add button + DataTable) loads by AJAX behind a loading mask.
        # Wait for the Add button, which is the reliable "tab content is up" signal.
        add = self._find(self.sel.add_button, timeout=120, required=False)
        if add is None:
            diag = self.driver.execute_script(
                "const tc=document.querySelector('#tab-content');"
                "const mask=document.querySelector('#full-screen-loading-mask');"
                "return {url:location.href,"
                " tabContentLen: tc? tc.innerHTML.length : -1,"
                " maskShown: mask? (mask.classList.contains('in')||mask.style.display==='block') : false,"
                " addScriptAnchors: [...document.querySelectorAll(\"a[href*='AddScript']\")].length,"
                " jquery: (typeof window.jQuery!=='undefined')};")
            self.log(f"   [debug] {diag}")
            raise PortalError("Scripts tab clicked but the Add button never appeared.")
        time.sleep(0.8)

    def list_is_ready(self) -> bool:
        """True when the Scripts tab content (Add button + search box) is already on screen."""
        try:
            return (self._find(self.sel.add_button, timeout=1, required=False) is not None
                    and self._find(self.sel.search_input, timeout=1, required=False) is not None)
        except Exception:
            return False

    def ensure_list_ready(self, force: bool = False):
        """Make the Scripts list usable - WITHOUT reloading the page if it already is.

        Saving a script drops you straight back on the list with Add and the search box right
        there, so reloading /Admin/Scoring for every module meant waiting for 2000+ rows to come
        back each time for nothing. Only reload when the list really isn't there (or on a retry,
        where a clean page is worth the wait).
        """
        if not force:
            self.wait_mask_gone(120)
            # A modal left open would sit over the Add button, so only reuse a clean list.
            if self.modal_open():
                self.log("   [list] a dialog is still open - reloading the list.")
            elif self.list_is_ready():
                self.assert_expected_host()      # SECURITY: still only ever the portal host
                self.log("   [list] reusing the open Scripts list (no reload).")
                return
        self.goto_list()

    def search_rows(self, module_name: str) -> list:
        """Type into the DataTables search box and return the visible rows as RowInfo."""
        box = self._find(self.sel.search_input, timeout=15)
        self._type(box, module_name, "list search box")
        time.sleep(1.2)  # let DataTables filter
        self.wait_mask_gone(60)
        rows = []
        for tr in self._find_all(self.sel.table_row):
            try:
                if not tr.is_displayed():
                    continue
                tds = tr.find_elements(By.TAG_NAME, "td")
                if len(tds) < 5:
                    continue
                # fixed columns: 0=actions, 1=Lab, 2=Status, 3=Version, 4=Agent
                lab = tds[1].text.strip()
                status = tds[2].text.strip()
                version = tds[3].text.strip()
                agent = tds[4].text.strip()
                href = None
                links = tr.find_elements(By.CSS_SELECTOR, "a[href*='/MakeLive/']")
                for a in links:
                    h = a.get_attribute("href")
                    if h and "/MakeLive/" in h:   # disabled Make Live has no real href
                        href = h
                        break
                rows.append(RowInfo(lab=lab, status=status, version=version,
                                    agent=agent, makelive_href=href))
            except Exception:
                continue
        return rows

    # ---------- add a script ----------
    def open_add_modal(self):
        # The portal's full-screen loading mask sits over this button while the list refreshes;
        # clicking through it is what produced "element click intercepted".
        self.wait_mask_gone(120)
        self._click(self._find(self.sel.add_button, timeout=60), "Add button")
        end = time.time() + 20
        while time.time() < end and not self.modal_open():
            time.sleep(0.3)
        if not self.modal_open():
            raise PortalError("Add was clicked but the dialog never opened.")
        # The modal body loads by AJAX (a "Processing..." spinner shows first). Wait for the
        # real fields to exist before touching them.
        self._find(self.sel.lab_input, timeout=20)
        time.sleep(0.4)

    def fill_add_form(self, module_name: str, script_body: str, vm_name: str,
                      agent: str | None = None, version: str | None = None):
        modal = self._find(self.sel.modal, timeout=10)
        agent = agent or self.cfg.agent
        version = version or self.cfg.version

        # Version
        vin = self._find(self.sel.version_input, timeout=8, root=modal, required=False)
        if vin is not None:
            try:
                self._type(vin, version, "Version")
            except Exception:
                pass

        # Lab autocomplete -> require an EXACT match
        self._pick_lab(modal, module_name)

        # Agent + VM populate after the Lab is chosen
        time.sleep(0.8)
        self._select_by_text(self.sel.agent_select, agent, modal, "Agent")
        self._select_by_text(self.sel.vm_select, vm_name, modal, "Virtual Machine")

        # Script body
        self.paste_script(script_body)

    @staticmethod
    def _strip_suffix(text: str) -> str:
        """Portal suggestions carry a trailing ' (MILT-2016)'-style suffix that the module
        name does not. Drop one trailing parenthetical for comparison."""
        import re
        return re.sub(r"\s*\([^)]*\)\s*$", "", (text or "").strip())

    def _pick_lab(self, modal, module_name: str):
        lab = self._find(self.sel.lab_input, timeout=10, root=modal)
        # send_keys types real key events, which triggers the portal's keyup search; _type
        # falls back to a JS input + keyup if the window is minimised or something overlays it.
        self._type(lab, module_name, "Lab search box")

        target = module_name.strip().lower()
        end = time.time() + 12
        matches = []
        while time.time() < end:
            items = self._find_all(self.sel.lab_suggestion)
            # ignore the placeholder rows ("Type to begin searching" / "No matches found"):
            # those are plain <li>, real results are <a> with a title attribute.
            cands = [it for it in items if (it.get_attribute("title") or it.text or "").strip()
                     and (it.get_attribute("title") or "").strip() not in
                     ("Type to begin searching", "No matches found")]
            matches = []
            for it in cands:
                full = (it.get_attribute("title") or it.text or "").strip()
                if full.lower() == target or self._strip_suffix(full).lower() == target:
                    matches.append((it, full))
            # results have arrived and settled if we see any <a> candidates
            if cands and matches:
                break
            time.sleep(0.4)

        if len(matches) == 1:
            self._click(matches[0][0], "Lab suggestion")
            time.sleep(0.6)
            hidden = self._find(self.sel.lab_hidden, timeout=5, required=False)
            if hidden is not None and not (hidden.get_attribute("value") or "").strip():
                raise PortalError("Clicked the Lab suggestion but LabId stayed empty.")
            return

        if len(matches) > 1:
            raise PortalError(
                f"Module '{module_name}' matched {len(matches)} labs "
                f"({[m[1] for m in matches]}). Refusing to guess which one.")
        raise PortalError(
            f"No EXACT Lab match for module:\n  {module_name}\n"
            "Refusing to guess (a wrong pick would set the script live on the wrong module).")

    def _select_by_text(self, candidates, text, modal, label):
        el = self._find(candidates, timeout=8, root=modal, required=False)
        if el is None:
            raise PortalError(f"Could not find the {label} dropdown.")
        if el.tag_name.lower() == "select":
            sel = Select(el)
            # exact visible text first, then a contains fallback
            for opt in sel.options:
                if opt.text.strip().lower() == text.strip().lower():
                    sel.select_by_visible_text(opt.text)
                    return
            for opt in sel.options:
                if text.strip().lower() in opt.text.strip().lower():
                    sel.select_by_visible_text(opt.text)
                    return
            raise PortalError(f"{label} has no option matching '{text}'. "
                              f"Options: {[o.text for o in sel.options]}")
        else:
            # custom dropdown: click, then click the matching item
            self._click(el, label)
            time.sleep(0.5)
            for item in self._find_all(self.sel.lab_suggestion + ["li", "option", "a"]):
                if (item.text or "").strip().lower() == text.strip().lower():
                    self._click(item, label)
                    return
            raise PortalError(f"{label} custom dropdown: no item '{text}'.")

    def paste_script(self, script_body: str) -> str:
        """Set the Script Body text across CodeMirror / ACE / plain textarea. Returns which."""
        js = r"""
        const body = arguments[0];
        // 1) CodeMirror
        const cm = document.querySelector('.CodeMirror');
        if (cm && cm.CodeMirror) { cm.CodeMirror.setValue(body); return 'codemirror'; }
        // 2) ACE
        if (window.ace) {
          const aceEl = document.querySelector('.ace_editor');
          if (aceEl && window.ace.edit) { window.ace.edit(aceEl).setValue(body, -1); return 'ace'; }
        }
        // 3) plain textarea inside the modal
        const modal = document.querySelector('div.modal.show, div.modal.in, div.modal[style*="display: block"], div.modal');
        const ta = (modal || document).querySelector('textarea');
        if (ta) {
          ta.value = body;
          ta.dispatchEvent(new Event('input', {bubbles:true}));
          ta.dispatchEvent(new Event('change', {bubbles:true}));
          return 'textarea';
        }
        return 'none';
        """
        kind = self.driver.execute_script(js, script_body)
        if kind == "none":
            raise PortalError("Could not find the Script Body editor to paste into.")
        return kind

    def click_save(self, settle: int = 60) -> bool:
        """Press Save. Returns True if the dialog closed by itself.

        The portal sometimes leaves its "Processing..." bar spinning for many minutes even though
        the script HAS been saved. So we give it a sensible wait and then hand back False - the
        caller reloads the list and verifies by searching, instead of sitting here.
        """
        self._click(self._find(self.sel.save_button, timeout=15), "Save button")
        end = time.time() + settle
        while time.time() < end:
            if not self.modal_open():
                # the list refreshes by AJAX behind the mask - let it settle
                self.wait_mask_gone(120)
                return True
            msg = self.modal_message()
            if msg:
                # the portal rejected it (e.g. "There is already a script for this lab with
                # version 1"). Retrying would just re-open the same dialog for ever.
                self.dismiss_modal()
                if "already a script" in msg.lower() or "already exists" in msg.lower():
                    raise AlreadyExistsError(msg)
                raise PortalError(f"The portal refused the script: {msg}")
            time.sleep(0.5)
        self.log(f"   [save] the portal's dialog is still showing after {settle}s - "
                 "will reload the list and check whether it saved anyway.")
        return False

    # ---------- status ----------
    def matches_module(self, lab_text: str, module_name: str) -> bool:
        """EXACT match only, tolerating the portal's trailing ' (MILT-2016)'-style suffix.

        A substring test is not good enough: the lab "Configure DNS and DHCP" contains the
        module "Configure DNS", and matching it would report - or publish - the wrong lab.
        """
        lab = (lab_text or "").strip().lower()
        m = (module_name or "").strip().lower()
        return lab == m or self._strip_suffix(lab).lower() == m

    def find_module_rows(self, module_name: str, status: str | None = None) -> list:
        """Rows for the exact module (suffix-tolerant), optionally filtered by status."""
        out = []
        for r in self.search_rows(module_name):
            if self.matches_module(r.lab, module_name):
                if status is None or r.status == status:
                    out.append(r)
        return out

    # ---------- make live ----------
    def make_live_exact(self, module_name: str) -> tuple:
        """Find the single InProgress row for the exact module and set it Live via the two-step
        confirm (row Make Live link -> confirm modal -> Make Live submit). Returns (version)."""
        matches = self.find_module_rows(module_name, status="InProgress")
        if len(matches) != 1:
            raise PortalError(
                f"Expected exactly 1 InProgress row for '{module_name}', found {len(matches)}. "
                "Refusing to Make Live (won't guess which version).")
        version = matches[0].version

        # Click the row's Make Live link to open the confirm modal.
        link = self._find_makelive_link_for(module_name)
        if link is None:
            raise PortalError("InProgress row found but its Make Live link was not clickable.")
        self.driver.execute_script("arguments[0].click();", link)

        # Confirm modal -> click its "Make Live" submit.
        submit = self._find([
            "//div[contains(@class,'modal')]//input[@type='submit'][@value='Make Live']",
            "//div[contains(@class,'modal')]//button[normalize-space()='Make Live']",
            "div.modal input[type='submit'].btn-success",
        ], timeout=15)
        self.driver.execute_script("arguments[0].click();", submit)

        self.assert_expected_host()   # SECURITY: the confirm POST navigated - re-check
        # Wait for the row to become Live (list refreshes after the POST).
        end = time.time() + 90
        while time.time() < end:
            live = self.find_module_rows(module_name, status="Live")
            if any(r.version == version for r in live) or (live and not
                   self.find_module_rows(module_name, status="InProgress")):
                return version
            time.sleep(2)
        raise PortalError("Made Live submitted but the row did not reach 'Live' in time.")

    def _find_makelive_link_for(self, module_name: str):
        for tr in self._find_all(self.sel.table_row):
            try:
                tds = tr.find_elements(By.TAG_NAME, "td")
                if len(tds) < 3:
                    continue
                # Same exact rule as find_module_rows - the row we click must be the row we
                # verified, never a near-miss chosen by a looser test.
                if self.matches_module(tds[1].text, module_name) and tds[2].text.strip() == "InProgress":
                    links = tr.find_elements(By.CSS_SELECTOR, "a[href*='/MakeLive/']")
                    for a in links:
                        if a.get_attribute("href") and "/MakeLive/" in a.get_attribute("href"):
                            return a
            except Exception:
                continue
        return None
