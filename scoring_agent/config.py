"""Central configuration for the Scoring Script Uploader.

Only the values that describe the portal live here. Selectors are best-effort and can be
overridden from a `portal.json` sitting next to the executable / project root, so the tool
can be re-pointed without touching code (see `load_overrides`).
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field


BASE_URL = "https://labs.xtremelabs.io"
SCRIPTLIST_PATH = "/Admin/Scoring/ScriptList"
SCORING_PATH = "/Admin/Scoring"
MAKELIVE_PATH = "/Admin/Scoring/MakeLive/{id}"

# Default agent shown for every row in the portal (video confirmed "Legacy Agent").
DEFAULT_AGENT = "Legacy Agent"

# Default script version for a brand-new module.
DEFAULT_VERSION = "1"


@dataclass
class Selectors:
    """CSS/XPath hints for the portal DOM.

    Confirmed against the live portal (labs.xtremelabs.io/Admin/Scoring) on 2026-09-15.
    Each field is a list of candidates tried in order; the first that exists wins. If the
    markup ever changes, run `scoring-uploader inspect` and adjust here or in portal.json.
    """
    # The "Add" opens the modal (it is an <a href="/Admin/Scoring/AddScript">).
    add_button: list = field(default_factory=lambda: [
        "a[href='/Admin/Scoring/AddScript']",
        "//a[normalize-space()='Add']",
        "//button[normalize-space()='Add']",
    ])
    # The add/edit modal.
    modal: list = field(default_factory=lambda: [
        "#script-modal", "div.modal.in", "div.modal.show", "div.modal",
    ])
    version_input: list = field(default_factory=lambda: [
        "#Version", "input[name='Version']",
    ])
    # The Lab picker is a Bootstrap search-dropdown: you TYPE into the searchbox, a menu of
    # <a data-id title="..."> items appears, and clicking one sets the hidden LabId.
    lab_input: list = field(default_factory=lambda: [
        "#LabId-searchbox", "input[placeholder='Search...']",
    ])
    lab_hidden: list = field(default_factory=lambda: [
        "input[name='LabId']",
    ])
    lab_suggestion: list = field(default_factory=lambda: [
        "#LabId-dropdown ul.dropdown-menu li a",
    ])
    agent_select: list = field(default_factory=lambda: [
        "#AgentId", "select[name='AgentId']",
    ])
    vm_select: list = field(default_factory=lambda: [
        "#VirtualMachineId", "select[name='VirtualMachineId']",
    ])
    # Script Body: a #ScriptData textarea enhanced by CodeMirror (paste_script handles both).
    script_textarea: list = field(default_factory=lambda: [
        "#ScriptData", "textarea[name='ScriptData']", "#script-modal textarea",
    ])
    save_button: list = field(default_factory=lambda: [
        "#script-modal input[type='submit'].btn-success",
        "#script-modal input[type='submit']",
        "//div[@id='script-modal']//input[@type='submit']",
    ])
    # DataTables search box on the list page (filters #script-table).
    search_input: list = field(default_factory=lambda: [
        "input[aria-controls='script-table']",
        "#script-table_filter input",
        "div.dataTables_filter input",
    ])
    table_row: list = field(default_factory=lambda: [
        "#script-table tbody tr", "table.dataTable tbody tr",
    ])
    makelive_link: list = field(default_factory=lambda: [
        "a[href*='/MakeLive/']",
        "//a[normalize-space()='Make Live']",
    ])
    # ---- the portal's own sign-in page (ASP.NET MVC names, with generic fallbacks) ----
    login_user: list = field(default_factory=lambda: [
        "#UserName", "input[name='UserName']", "input[name='Email']",
        "input[type='email']", "form input[type='text']",
    ])
    login_pass: list = field(default_factory=lambda: [
        "#Password", "input[name='Password']", "input[type='password']",
    ])
    login_remember: list = field(default_factory=lambda: [
        "#RememberMe", "input[name='RememberMe']",
        "input[type='checkbox'][name*='emember']",
    ])
    login_submit: list = field(default_factory=lambda: [
        "form input[type='submit']", "form button[type='submit']",
        "//input[@type='submit']", "//button[normalize-space()='Log On']",
        "//button[normalize-space()='Log in']", "//button[normalize-space()='Sign in']",
    ])
    # A CAPTCHA the agent must never try to solve - it hands back to the user.
    captcha: list = field(default_factory=lambda: [
        "iframe[src*='recaptcha']", ".g-recaptcha", "#g-recaptcha",
        "iframe[src*='hcaptcha']", ".h-captcha",
    ])
    # Only present when logged in. NOTE: do NOT use "Welcome" - the LogOn page also says
    # "Welcome". "Log off" appears only on authenticated pages.
    logged_in_marker: list = field(default_factory=lambda: [
        "//a[contains(text(),'Log off')]",
        "//*[contains(text(),'Log off')]",
    ])


@dataclass
class Config:
    base_url: str = BASE_URL
    scriptlist_path: str = SCRIPTLIST_PATH
    scoring_path: str = SCORING_PATH
    makelive_path: str = MAKELIVE_PATH
    agent: str = DEFAULT_AGENT
    version: str = DEFAULT_VERSION
    selectors: Selectors = field(default_factory=Selectors)

    @property
    def scriptlist_url(self) -> str:
        return self.base_url.rstrip("/") + self.scriptlist_path

    @property
    def scoring_url(self) -> str:
        return self.base_url.rstrip("/") + self.scoring_path
