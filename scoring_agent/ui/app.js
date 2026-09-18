/* Scoring Agent UI logic. Calls the backend over the app's own local HTTP API and receives
   its events through window.__on(name, payload). */
let state = { path: null, mode: "save", jobs: [], rowByModule: {}, statusByModule: {},
               counts: {done:0,total:0,live:0,saved:0,fail:0}, running:false };

function $(id){ return document.getElementById(id); }

/* ---------- talking to the backend ----------
   The window is a Chrome app window pointed at a small local server, so a call is just a POST
   and events arrive on a Server-Sent Events stream. (This replaced pywebview's in-process
   bridge, which deadlocked the window on roughly one launch in ten.) The token in the address
   is what proves this page is the app's own window; every request carries it. */
const TOKEN = new URLSearchParams(location.search).get("t") || "";

async function call(method, ...args){
  const r = await fetch(`/api/${method}?t=${encodeURIComponent(TOKEN)}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(args),
  });
  const data = await r.json().catch(() => ({}));
  if (data && data.error) throw new Error(data.error);
  return data ? data.result : undefined;
}

/* Same shape the rest of the UI expects: API.preview(path), API.start_run(...), and so on. */
const API = new Proxy({}, { get: (_, name) => (...args) => call(String(name), ...args) });

function connectEvents(){
  const es = new EventSource(`/events?t=${encodeURIComponent(TOKEN)}`);
  es.onmessage = e => {
    try {
      const m = JSON.parse(e.data);
      window.__on(m.name, m.payload);
    } catch (err) {}
  };
  es.onerror = () => {};          // EventSource reconnects on its own
}

/* Any backend call made at startup gets a deadline - a slow backend must not freeze the UI. */
function callWithin(fn, ms, fallback){
  return Promise.race([
    Promise.resolve().then(fn).catch(() => fallback),
    new Promise(res => setTimeout(() => res(fallback), ms)),
  ]);
}

/* ---------- backend -> UI ---------- */
window.__on = function(name, p){
  if (name === "log"){ const el=$("log"); el.textContent += p + "\n"; el.scrollTop = el.scrollHeight; }
  else if (name === "login"){ showBusy(false); showLogin(true); }
  else if (name === "login_done"){ showBusy(false); showLogin(false); refreshSession(); }
  else if (name === "session"){ refreshSession(); }
  else if (name === "session_status"){ renderSession(p); }
  else if (name === "update_progress"){ $("updFill").style.width = p + "%"; }
  else if (name === "progress"){ onProgress(p); }
  else if (name === "done"){ onDone(p); }
  else if (name === "error"){ onError(p.error || "Unknown error"); }
};

function badge(status){
  const m = {
    Pending:['b-pending','<span class="dot"></span>Pending'],
    Running:['b-run','<span class="spin"></span>Running'],
    Live:['b-live','<span class="dot"></span>Live'],
    InProgress:['b-prog','<span class="dot"></span>InProgress'],
    Saved:['b-prog','<span class="dot"></span>Saved'],
    Failed:['b-fail','<span class="dot"></span>Failed'],
  }[status] || ['b-pending', status];
  return `<span class="badge ${m[0]}">${m[1]}</span>`;
}

function showLogin(on){ $("loginBanner").classList.toggle("show", !!on); }
function openLog(on){ $("logBox").open = !!on; }
function showBusy(on, html, spin){
  if (html) $("busyText").innerHTML = html;
  $("busySpin").style.display = (spin === false) ? "none" : "";
  $("busyBanner").classList.toggle("show", !!on);
}
function showErr(on, msg){
  if (msg) $("errText").textContent = msg;
  $("errBanner").classList.toggle("show", !!on);
}

function onProgress(p){
  showBusy(false);
  showLogin(false);
  const tr = state.rowByModule[p.module];
  if (!tr) return;
  const st = p.status;
  state.statusByModule[p.module] = st;
  tr.querySelector(".st").innerHTML = badge(st);
  if (p.vm) tr.querySelector(".vm").textContent = p.vm;
  renderLiveActions();
  if (st === "Live" || st === "InProgress" || st === "Saved"){
    state.counts.done++; if (st==="Live") state.counts.live++; else state.counts.saved++;
  } else if (st === "Failed"){ state.counts.done++; state.counts.fail++; }
  renderCounts();
}

function renderCounts(){
  const c = state.counts;
  $("c-done").textContent = c.done; $("c-total").textContent = c.total;
  $("c-live").textContent = c.live; $("c-saved").textContent = c.saved; $("c-fail").textContent = c.fail;
  const pct = c.total ? Math.round(100*c.done/c.total) : 0;
  $("pfill").style.width = pct + "%";
}

function onDone(summary){
  state.running = false;
  $("cancel").style.display = "none";
  $("start").disabled = false; $("start").textContent = "Start";
  showBusy(false); showLogin(false); showErr(false);
  const d = $("done"); d.classList.add("show");
  $("doneTitle").textContent = summary.failed ? "Run finished with issues" : "Run complete";
  $("doneSub").textContent = `${summary.live} live · ${summary.ok} ok · ${summary.failed} failed — report saved`;
  $("openReport").onclick = () => API.open_path(summary.report);
  $("openFolder").onclick = () => API.open_path(summary.report.replace(/[\\/][^\\/]+$/, ""));
  if (summary.log_file) $("logHint").textContent = "— saved to " + summary.log_file.split(/[\\/]/).pop();
  const pending = inProgressModules();
  const next = $("doneNext");
  if (pending.length){
    next.style.display = "";
    next.textContent = `${pending.length} module(s) are InProgress — use “Make all live”, or the Make live button on a row.`;
  } else { next.style.display = "none"; }
  renderLiveActions();
  refreshSession();
}

/* Turn a raw Selenium/portal message into something a user can act on. */
function friendlyError(msg){
  const m = (msg || "").toLowerCase();
  if (m.includes("message from renderer") || m.includes("timed out receiving"))
    return "Chrome stopped responding while loading the portal. It's usually a slow sign-in page — press Start again.";
  if (m.includes("could not start chrome") || m.includes("chromedriver") || m.includes("session not created"))
    return "Chrome couldn't be started. Make sure Google Chrome is installed and up to date, then press Start again.";
  if (m.includes("window was closed"))
    return "The Chrome window was closed, so the run stopped. Press Start again and leave the browser open.";
  if (m.includes("timed out waiting for the manual sign-in") || m.includes("manual login"))
    return "Sign-in wasn't completed in time. Press Start again and log in in the Chrome window that opens.";
  if (m.includes("refusing to act"))
    return msg;   // host-lock message is already clear
  return msg;
}

function onError(msg){
  state.running = false;
  $("cancel").style.display = "none";
  $("start").disabled = false; $("start").textContent = "Start";
  showBusy(false);
  showLogin(false);                 // the browser is gone - never leave "Log in required" up
  showErr(true, friendlyError(msg));
  $("errBanner").scrollIntoView({behavior:"smooth", block:"nearest"});
  refreshSession();
}

/* ---------- UI -> backend ---------- */
async function pick(){
  let p = await API.pick_folder().catch(() => null);
  if (!p){
    // the dialog was cancelled, or Windows would not show it - offer the manual way
    p = prompt("Paste the full path of the course folder:", "");
    if (!p) return;
    p = p.trim().replace(/^"|"$/g, "");
    if (!p) return;
  }
  state.path = p; $("path").textContent = p;
  $("path").style.color = "#12232f";
  const r = await API.preview(p);
  if (!r.ok){ alert(r.error); return; }
  const d = r.data;
  $("stats").style.display = "flex";
  $("s-mod").textContent = d.module_count;
  $("s-job").textContent = d.jobs.length;
  $("s-miss").textContent = d.missing.length;
  $("excel").innerHTML = d.excel ? ("Excel: <b>"+ d.excel.split(/[\\/]/).pop() +"</b>" + (d.missing.length? " — "+d.missing.length+" module(s) have no .ps1 and will be skipped":"")) : "";
  buildJobs(d.jobs);
  $("cardLogin").style.display = "";
  $("card2").style.display = ""; $("card3").style.display = "";
  $("start").disabled = d.jobs.length === 0;
  $("jobsub").textContent = d.jobs.length + " module(s) ready";
  refreshSession();        // safe now: the page has been up for a while
}

function buildJobs(jobs){
  state.jobs = jobs; state.rowByModule = {};
  state.counts = {done:0,total:jobs.length,live:0,saved:0,fail:0}; renderCounts();
  const tb = $("jobs"); tb.innerHTML = "";
  jobs.forEach((j,i)=>{
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${i+1}</td>
      <td class="mod">${escapeHtml(j.module)}</td>
      <td><span class="vm">${escapeHtml(j.vm||'?')}</span></td>
      <td class="kind">${j.kind}</td>
      <td class="st">${badge(j.ready ? 'Pending' : 'Failed')}</td>
      <td class="act"></td>`;
    tb.appendChild(tr);
    state.rowByModule[j.module] = tr;
    state.statusByModule[j.module] = j.ready ? 'Pending' : 'Failed';
  });
  renderLiveActions();
}

/* Any module sitting at InProgress can be published later - on its own or all together. */
function inProgressModules(){
  return Object.keys(state.statusByModule).filter(m => state.statusByModule[m] === 'InProgress');
}

function renderLiveActions(){
  const pending = inProgressModules();
  Object.keys(state.rowByModule).forEach(mod => {
    const cell = state.rowByModule[mod].querySelector('.act');
    if (!cell) return;
    if (state.statusByModule[mod] === 'InProgress'){
      if (!cell.firstChild){
        const b = document.createElement('button');
        b.className = 'mini'; b.textContent = 'Make live';
        b.onclick = () => makeLive([mod]);
        cell.appendChild(b);
      }
      cell.firstChild.disabled = state.running;
    } else {
      cell.innerHTML = '';
    }
  });
  const all = $("makeAllLive");
  all.style.display = pending.length ? "" : "none";
  all.disabled = state.running;
  all.textContent = `Make all live (${pending.length})`;
  all.onclick = () => makeLive(inProgressModules());
}

/* Publish already-saved modules - no script is uploaded, just InProgress -> Live. */
async function makeLive(modules){
  if (state.running || !modules.length || !state.path) return;
  state.running = true;
  $("start").disabled = true;
  $("cancel").style.display = "";
  $("done").classList.remove("show");
  showErr(false);
  openLog(true);
  showBusy(true, `<b>Publishing…</b> taking ${modules.length} module(s) from InProgress to Live.`);
  state.counts = {done:0, total:modules.length, live:0, saved:0, fail:0};
  renderCounts();
  modules.forEach(m => {
    const tr = state.rowByModule[m];
    if (tr) tr.querySelector('.st').innerHTML = badge('Running');
  });
  renderLiveActions();
  const r = await API.start_make_live(state.path, modules, null, creds());
  if (!r.ok){ onError(r.error); }
}

function escapeHtml(s){ return (s||"").replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

/* The sign-in details, straight from the form. Nothing here is stored or remembered - they are
   read at the moment a run starts and handed to the backend for that run only. */
function creds(){
  const u = $("pUser").value.trim(), p = $("pPass").value;
  return (u && p) ? {username: u, password: p} : null;
}

async function start(){
  if (state.running) return;
  if (!state.path) return;
  state.running = true;
  $("start").disabled = true; $("start").textContent = "Running…";
  $("cancel").style.display = "";
  $("done").classList.remove("show");
  showErr(false);
  openLog(true);                 // the user should see what the agent is doing
  showBusy(true, "<b>Starting…</b> opening Chrome. The very first run can take a minute while the browser driver is prepared.");
  // reset statuses
  buildJobs(state.jobs);
  const r = await API.start_run(state.path, state.mode, null, creds());
  if (!r.ok){ onError(r.error); }
}

/* ---------- reset ---------- */
/* Put the screen back to how it looks on a fresh launch (the folder selection is kept). */
function resetUi(){
  state.running = false;
  state.path = null; state.jobs = []; state.rowByModule = {}; state.statusByModule = {};
  state.counts = {done:0,total:0,live:0,saved:0,fail:0};
  showBusy(false); showLogin(false); showErr(false);
  $("done").classList.remove("show");
  $("doneNext").style.display = "none";
  $("log").textContent = ""; $("logHint").textContent = ""; openLog(false);
  $("cancel").style.display = "none"; $("cancel").textContent = "Stop";
  $("start").textContent = "Start"; $("start").disabled = true;
  // back to a blank first screen: no folder, no stats, no job list
  $("path").textContent = "No folder selected — pick a folder that has the Module Excel and the .ps1 scripts.";
  $("path").style.color = "";
  $("stats").style.display = "none";
  $("excel").innerHTML = "";
  $("jobs").innerHTML = "";
  $("jobsub").textContent = "";
  $("makeAllLive").style.display = "none";
  $("pUser").value = ""; $("pPass").value = "";     // never leave credentials on screen
  $("cardLogin").style.display = "none";
  $("card2").style.display = "none"; $("card3").style.display = "none";
  renderCounts();
}

async function openReset(){
  const r = await API.is_running().catch(()=>({running:false}));
  $("resetWarn").style.display = r.running ? "" : "none";
  $("resetModal").classList.add("show");
}

async function doReset(){
  const btn = $("resetYes");
  btn.disabled = true; btn.textContent = "Resetting…";
  try{
    const r = await API.reset_session();
    resetUi();
    $("log").textContent = "Session reset — browser closed and the saved login cleared.\n";
    await refreshSession();
    $("resetModal").classList.remove("show");
    if (r && r.leftover){
      showErr(true, "Session cleared, but some browser files were still locked. Close any leftover Chrome window and reset again if the login sticks.");
    } else {
      showBusy(true, "<b>Session reset.</b> Browser closed and the saved login cleared — press Start when you're ready.", false);
      setTimeout(()=>showBusy(false), 6000);
    }
  }catch(e){
    showErr(true, "Could not reset the session: " + e);
  }finally{
    btn.disabled = false; btn.textContent = "Reset session";
  }
}

/* ---------- wiring ---------- */
/* Draw the header chip from a session-status object (pushed by the backend, or fetched). */
function renderSession(s){
  const el = $("sess");
  if (s && s.authenticated){
    const leftMin = Math.max(0, Math.round(s.max_age_hours*60 - (s.age_seconds||0)/60));
    const pretty = leftMin >= 60 ? `${Math.floor(leftMin/60)}h ${leftMin%60}m` : `${leftMin}m`;
    el.textContent = `Signed in · re-auth in ${pretty}`;
    $("signout").style.display = "";
  } else {
    el.textContent = "Not signed in";
    $("signout").style.display = "none";
  }
}

/* Ask the backend for the session state. Only ever called AFTER the page is up (a finished
   run, Sign out, Reset) - never while it is loading, which used to deadlock the window. */
async function refreshSession(){
  try{
    renderSession(await callWithin(() => API.session_status(), 8000, null));
  }catch(e){ renderSession(null); }
}

/* Everything here is synchronous on purpose: the buttons must work even if the Python
   bridge is slow to attach, so the app can never come up dead. */
function wireUi(){
  $("pick").onclick = pick;
  $("start").onclick = start;
  $("cancel").onclick = async () => { await API.cancel(); $("cancel").textContent = "Stopping…"; };
  $("signout").onclick = async () => {
    const r = await API.sign_out();
    if (r && r.ok === false){ showErr(true, r.error); return; }
    await refreshSession();
    showErr(false);
    showBusy(true, "<b>Signed out.</b> The saved login has been cleared — you'll sign in again on the next run.", false);
    setTimeout(()=>showBusy(false), 6000);
  };
  $("reset").onclick = openReset;
  $("resetNo").onclick = () => $("resetModal").classList.remove("show");
  $("resetYes").onclick = doReset;
  $("updCheck").onclick = () => checkUpdates(false);
  $("updInstall").onclick = installUpdate;
  document.querySelectorAll("#seg button").forEach(b => b.onclick = () => {
    document.querySelectorAll("#seg button").forEach(x=>x.classList.remove("on"));
    b.classList.add("on"); state.mode = b.dataset.mode;
    $("modehint").innerHTML = state.mode === "live"
      ? "Uploads each script and takes it all the way to <b>Live</b> for its module."
      : "Uploads each script and leaves it <b>InProgress</b> — you make them live yourself later.";
  });
}

document.addEventListener("DOMContentLoaded", () => {
  wireUi();
  connectEvents();
  refreshSession();
  scheduleUpdateCheck();
});

/* ---------- updates ---------- */
let updLatest = null;                 // the release we found, if any

function openUpdates(){
  $("updates").classList.add("show");
  $("updBanner").classList.remove("show");
  API.app_version().then(v => { $("updCurrent").textContent = v.version; }).catch(()=>{});
  API.update_settings().then(r => {
    if (r && r.ok) $("updRepo").value = r.repo || "";
    if (r && r.ok && r.repo) checkUpdates();      // a repo is set, so look straight away
  }).catch(()=>{});
}
function hideUpdates(){ $("updates").classList.remove("show"); }

function updSay(html){ $("updResult").innerHTML = html; }

async function checkUpdates(quiet){
  const repo = $("updRepo").value.trim();
  const saved = await API.update_settings(repo).catch(()=>null);
  if (saved && saved.ok === false){ updSay(`<span style="color:#b02020">${escapeHtml(saved.error)}</span>`); return null; }

  if (!quiet) updSay("Checking…");
  const r = await API.check_update(repo).catch(()=>null);
  updLatest = null;
  $("updInstall").style.display = "none";
  if (!r) { if (!quiet) updSay("Could not check just now."); return null; }

  if (!r.ok){
    const why = {
      no_repo:  "Add your release repository above, then press <b>Check now</b>.",
      bad_repo: "That repository name does not look right. Use <b>owner/repo</b>.",
      not_found:"No releases found there yet — or the repository is private.",
      offline:  "Could not reach GitHub. Check your connection and try again.",
    }[r.reason] || ("Could not check (" + escapeHtml(r.reason || "unknown") + ").");
    if (!quiet) updSay(why);
    return null;
  }

  if (!r.newer){
    if (!quiet) updSay(`<b>You are up to date.</b> Latest published is ${escapeHtml(r.latest)}.`);
    return null;
  }
  if (!r.asset_url){
    if (!quiet) updSay(`<b>Version ${escapeHtml(r.latest)} is out</b>, but that release has no
      installer attached, so it cannot be installed from here.`);
    return null;
  }

  updLatest = r;
  const mb = r.asset_size ? ` · ${(r.asset_size/1048576).toFixed(0)} MB` : "";
  updSay(`<b>Version ${escapeHtml(r.latest)} is available</b>
     <span class="muted">(you have ${escapeHtml(r.current)}${mb})</span>
     ${r.notes ? `<div style="margin-top:8px;max-height:130px;overflow:auto;white-space:pre-wrap;
        font-size:12.5px;color:var(--muted)">${escapeHtml(r.notes)}</div>` : ""}`);
  $("updInstall").style.display = "";
  return r;
}

async function installUpdate(){
  if (!updLatest) return;
  const b = $("updInstall");
  b.disabled = true; b.textContent = "Downloading…";
  $("updBar").style.display = ""; $("updFill").style.width = "0%";
  const r = await API.install_update(updLatest.asset_url, updLatest.asset_size).catch(e => ({ok:false, error:String(e)}));
  if (!r || !r.ok){
    b.disabled = false; b.textContent = "Download & install";
    $("updBar").style.display = "none";
    updSay(`<span style="color:#b02020">${escapeHtml((r && r.error) || "The download failed.")}</span>`);
    return;
  }
  b.textContent = "Starting the installer…";
  updSay(`<b>Downloaded.</b> The installer is opening — follow it, and Scoring Agent will close
     so the new version can replace it. Your saved login and settings are kept.`);
}

/* A quiet look for updates, well after the page has settled - never during load. */
function scheduleUpdateCheck(){
  setTimeout(async () => {
    const r = await API.update_settings().catch(()=>null);
    if (!r || !r.ok || !r.repo) return;           // nothing configured, stay quiet
    const found = await checkUpdates(true);
    if (found){
      $("updBannerText").textContent =
        `Version ${found.latest} is ready to install (you have ${found.current}).`;
      $("updBanner").classList.add("show");
    }
  }, 25000);
}

/* about */
function showAbout(){ $("about").classList.add("show"); }
function hideAbout(){ $("about").classList.remove("show"); }
