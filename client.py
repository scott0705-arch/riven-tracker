"""
client.py - Riven Tracker desktop client
========================================
The app you and your friends run. It does NOT collect prices itself - the
cloud collector (GitHub Actions) does that every ~5 minutes. This client:

  1. Downloads config.json + price_history.json from the tracker's GitHub
     repo via the GitHub API (fresh, no CDN caching)
  2. Auto-refreshes in the background (ETag conditional requests, so
     "nothing changed" checks are nearly free and rate-limit friendly)
  3. Caches data locally so it still opens offline
  4. Lets each user set their OWN profit settings (desired profit, safety
     margin, sell percentile, lookback) - stored locally, per user
  5. Shows the Prices and Analysis views and can export the Excel workbook

Run from source:   python client.py
Build an exe:      see build_client.bat (PyInstaller)
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import tracker_core as core

# ---------------------------------------------------------------------------
# where the hosted data lives - set your repo here so friends never have to.
# Format: "githubusername/reponame"
# ---------------------------------------------------------------------------
DEFAULT_REPO = "scott0705-arch/riven-tracker"
DEFAULT_BRANCH = "main"

APP_TITLE = "Warframe Riven Tracker"

# Auto-refresh cadence. 90s = 40 polls/hour, safely under GitHub's
# 60-requests/hour unauthenticated API limit even before ETag savings.
# (The collector only adds data every ~5 min, so faster polling gains nothing.)
AUTO_REFRESH_SECONDS = 90
CONFIG_EVERY_N_POLLS = 10          # weapon list rarely changes; check it less


def app_dir():
    """Directory of the exe (frozen) or the script - all local files go here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_FILE = os.path.join(app_dir(), "client_settings.json")
CACHE_HISTORY = os.path.join(app_dir(), "cached_history.json")
CACHE_CONFIG = os.path.join(app_dir(), "cached_config.json")

DEFAULT_SETTINGS = {
    "repo": DEFAULT_REPO,
    "branch": DEFAULT_BRANCH,
    "analysis": dict(core.ANALYSIS_DEFAULTS),
    "excel_file": "riven_prices.xlsx",
}


def load_settings():
    s = core.load_json(SETTINGS_FILE, {}) or {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(s)
    merged["analysis"] = {**core.ANALYSIS_DEFAULTS, **(s.get("analysis") or {})}
    if not merged.get("repo"):
        merged["repo"] = DEFAULT_REPO          # heal older empty settings files
    return merged


def save_settings(s):
    core.atomic_write_json(SETTINGS_FILE, s)


# ---------------------------------------------------------------------------
# remote data (GitHub contents API - not raw.githubusercontent, which caches)
# ---------------------------------------------------------------------------
class RateLimited(RuntimeError):
    def __init__(self, reset_epoch):
        self.reset_epoch = reset_epoch
        when = datetime.fromtimestamp(reset_epoch).strftime("%H:%M") \
            if reset_epoch else "later"
        super().__init__(f"GitHub API rate limit reached; resets at {when}")


def api_url(repo, branch, filename):
    return (f"https://api.github.com/repos/{repo}/contents/"
            f"{filename}?ref={branch}")


def http_get_json(url, etag=None):
    """GET a GitHub contents URL as raw JSON.
    Returns (data, new_etag) on 200, (None, etag) on 304 Not Modified.
    Raises RateLimited when the API quota is exhausted."""
    headers = {
        "User-Agent": core.USER_AGENT,
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data, resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            try:
                reset = int(e.headers.get("X-RateLimit-Reset", "0"))
            except ValueError:
                reset = 0
            raise RateLimited(reset) from e
        raise


class RemoteData:
    """Owns the downloaded config/history, their ETags, and the local caches.
    Thread-safe enough for our single background worker at a time."""

    def __init__(self, settings):
        self.settings = settings
        self.server_cfg = core.load_json(CACHE_CONFIG, {}) or {}
        self.history = core.load_history_file(CACHE_HISTORY)
        self.etags = {}                    # filename -> etag
        self.rate_limited_until = 0        # epoch; skip polls before this

    def _repo_branch(self):
        repo = self.settings["repo"].strip()
        branch = self.settings["branch"].strip() or "main"
        if not repo or "/" not in repo:
            raise RuntimeError("No data source set. Enter the GitHub repo as "
                               "'username/reponame' in Setup and click Save.")
        return repo, branch

    def _get(self, filename, conditional=True):
        repo, branch = self._repo_branch()
        etag = self.etags.get(filename) if conditional else None
        data, new_etag = http_get_json(api_url(repo, branch, filename), etag)
        if new_etag:
            self.etags[filename] = new_etag
        return data                        # None means "unchanged"

    def refresh(self, include_config=True, conditional=True):
        """Fetch remote files. Returns True if anything changed.
        Raises on network errors / RateLimited."""
        changed = False
        if include_config:
            cfg = self._get("config.json", conditional)
            if cfg is not None:
                self.server_cfg = cfg
                core.atomic_write_json(CACHE_CONFIG, cfg)
                changed = True
        hist = self._get("price_history.json", conditional)
        if hist is not None:
            snaps = sorted(hist.get("snapshots", []),
                           key=lambda s: s["timestamp"])
            self.history = snaps
            core.atomic_write_json(CACHE_HISTORY, {"snapshots": snaps})
            changed = True
        return changed


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    import webbrowser

    settings = load_settings()
    remote = RemoteData(settings)

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("880x640")
    root.minsize(720, 540)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=6, pady=6)

    setup_tab = ttk.Frame(nb, padding=10)
    prices_tab = ttk.Frame(nb, padding=10)
    analysis_tab = ttk.Frame(nb, padding=10)
    nb.add(setup_tab, text="  Setup  ")
    nb.add(prices_tab, text="  Prices  ")
    nb.add(analysis_tab, text="  Analysis  ")

    status_var = tk.StringVar()
    ttk.Label(root, textvariable=status_var, foreground="#555").pack(
        anchor="w", padx=10, pady=(0, 6))

    def set_status(msg):
        status_var.set(msg)

    # ======================= Setup tab =====================================
    frm = setup_tab

    # --- data source -------------------------------------------------------
    src = ttk.LabelFrame(frm, text="Data source (the shared tracker on GitHub)",
                         padding=8)
    src.pack(fill="x", pady=(0, 8))
    ttk.Label(src, text="Repo (username/reponame)").grid(row=0, column=0, sticky="w")
    repo_e = ttk.Entry(src, width=34)
    repo_e.insert(0, settings["repo"])
    repo_e.grid(row=0, column=1, sticky="w", padx=(8, 12))
    ttk.Label(src, text="Branch").grid(row=0, column=2, sticky="w")
    branch_e = ttk.Entry(src, width=10)
    branch_e.insert(0, settings["branch"])
    branch_e.grid(row=0, column=3, sticky="w", padx=(8, 12))

    def save_source():
        settings["repo"] = repo_e.get().strip()
        settings["branch"] = branch_e.get().strip() or "main"
        save_settings(settings)
        remote.etags.clear()               # force full re-download of new source
        set_status("Data source saved")

    ttk.Button(src, text="Save", command=save_source).grid(row=0, column=4)
    src.columnconfigure(4, weight=1)

    # --- tracked weapons (read-only; managed on GitHub) --------------------
    wf = ttk.LabelFrame(frm, text="Tracked weapons (managed on GitHub by the "
                                  "tracker owner)", padding=8)
    wf.pack(fill="both", expand=False, pady=(0, 8))
    lb = tk.Listbox(wf, height=6)
    lb.pack(side="left", fill="both", expand=True)
    wsb = ttk.Scrollbar(wf, command=lb.yview)
    wsb.pack(side="left", fill="y")
    lb.config(yscrollcommand=wsb.set)

    def open_repo_config():
        repo = settings["repo"].strip()
        if repo:
            webbrowser.open(f"https://github.com/{repo}/edit/"
                            f"{settings['branch'] or 'main'}/config.json")
        else:
            messagebox.showinfo("No repo set", "Set the data source first.")

    ttk.Button(wf, text="Edit list on GitHub...",
               command=open_repo_config).pack(side="left", padx=(8, 0), anchor="n")

    def refresh_weapon_list():
        lb.delete(0, "end")
        for w in remote.server_cfg.get("weapons", []):
            lb.insert("end", w["name"])

    # --- profit settings (LOCAL - each user has their own) -----------------
    set_frame = ttk.LabelFrame(frm, text="Profit settings (yours only - saved "
                                         "on this PC)", padding=8)
    set_frame.pack(fill="x", pady=(0, 8))

    fields = [
        ("Lookback window (samples)", "lookback",
         "int", 1, 100000, "Recent samples used by the analysis (288 = 1 day at 5-min data)"),
        ("Projected sell percentile", "sell_percentile",
         "float", 0.0, 1.0, "Where in the recent price range you expect to sell (0.5 = median)"),
        ("Desired profit (plat)", "desired_profit",
         "float", 0, 100000, "Platinum profit you want per flip"),
        ("Safety margin", "safety_margin",
         "float", 0.0, 1.0, "Haircut on the projected sell price (0.1 = 10% below)"),
    ]
    entries = {}
    for i, (label, key, _t, _lo, _hi, tip) in enumerate(fields):
        ttk.Label(set_frame, text=label).grid(row=i, column=0, sticky="w", pady=1)
        e = ttk.Entry(set_frame, width=10)
        e.insert(0, str(settings["analysis"][key]))
        e.grid(row=i, column=1, sticky="w", padx=(8, 12), pady=1)
        ttk.Label(set_frame, text=tip, foreground="#777").grid(row=i, column=2, sticky="w")
        entries[key] = e
    ttk.Label(set_frame,
              text="Good buy price = projected sell x (1 - safety margin) - desired profit",
              foreground="#555").grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 2))

    def apply_settings():
        new = {}
        for label, key, typ, lo, hi, _tip in fields:
            raw = entries[key].get().strip()
            try:
                v = int(raw) if typ == "int" else float(raw)
            except ValueError:
                messagebox.showwarning("Invalid value", f"'{raw}' is not a number ({label}).")
                return
            if not (lo <= v <= hi):
                messagebox.showwarning("Invalid value",
                                       f"{label} must be between {lo} and {hi}.")
                return
            new[key] = v
        settings["analysis"] = new
        save_settings(settings)
        refresh_analysis_view()
        set_status("Profit settings saved - Analysis updated")

    ttk.Button(set_frame, text="Apply", command=apply_settings).grid(
        row=0, column=3, rowspan=2, padx=(16, 0))
    set_frame.columnconfigure(2, weight=1)

    # --- actions + log -----------------------------------------------------
    btn_frame = ttk.Frame(frm)
    btn_frame.pack(fill="x", pady=(0, 8))

    logbox = tk.Text(frm, height=6, state="disabled", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True, pady=(0, 6))

    def gui_log(msg):
        def _do():
            logbox.config(state="normal")
            logbox.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
            logbox.see("end")
            logbox.config(state="disabled")
        root.after(0, _do)

    busy = {"flag": False}
    poll_count = {"n": 0}

    def latest_str():
        return (core.ts_local_str(remote.history[-1]["timestamp"])
                if remote.history else "never")

    def do_refresh(manual):
        """Runs in a worker thread. Manual = full unconditional refresh;
        auto = conditional, history-focused poll."""
        if busy["flag"]:
            return
        if not manual and time.time() < remote.rate_limited_until:
            return                                     # waiting out rate limit
        busy["flag"] = True
        root.after(0, lambda: refresh_btn.config(state="disabled"))

        def worker():
            try:
                if manual:
                    changed = remote.refresh(include_config=True,
                                             conditional=False)
                    gui_log(f"refreshed - {len(remote.history)} snapshot(s), "
                            f"latest {latest_str()}")
                else:
                    poll_count["n"] += 1
                    include_cfg = poll_count["n"] % CONFIG_EVERY_N_POLLS == 0
                    changed = remote.refresh(include_config=include_cfg,
                                             conditional=True)
                    if changed:
                        gui_log(f"new data - latest {latest_str()}")
                if changed or manual:
                    root.after(0, refresh_all_views)
                root.after(0, lambda: set_status(
                    f"Up to date - latest snapshot {latest_str()} - "
                    f"auto-checking every {AUTO_REFRESH_SECONDS}s"))
            except RateLimited as e:
                remote.rate_limited_until = e.reset_epoch or (time.time() + 900)
                gui_log(str(e))
                root.after(0, lambda: set_status(
                    f"{e} - showing cached data, auto-refresh paused"))
            except Exception as e:
                if manual:
                    gui_log(f"could not refresh: {e}")
                root.after(0, lambda: set_status(
                    "Offline - showing cached data" if remote.history
                    else "No data - set the data source and click Refresh"))
            finally:
                busy["flag"] = False
                root.after(0, lambda: refresh_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def auto_poll():
        do_refresh(manual=False)
        root.after(AUTO_REFRESH_SECONDS * 1000, auto_poll)

    def export_excel():
        if not remote.history:
            messagebox.showinfo("No data", "Refresh data first.")
            return
        path = os.path.join(app_dir(), settings["excel_file"])
        ok = core.export_workbook(path, remote.server_cfg.get("weapons", []),
                                  remote.history, settings["analysis"])
        if ok:
            set_status(f"Workbook exported: {path}")
        else:
            messagebox.showwarning("File busy",
                                   "The workbook is open in Excel - close it "
                                   "and export again.")

    def open_sheet():
        path = os.path.join(app_dir(), settings["excel_file"])
        if not os.path.exists(path):
            messagebox.showinfo("Not yet created", "Click 'Export Excel' first.")
            return
        try:
            os.startfile(path)
        except AttributeError:                      # non-Windows
            import subprocess
            subprocess.Popen(["xdg-open", path])

    refresh_btn = ttk.Button(btn_frame, text="Refresh now",
                             command=lambda: do_refresh(manual=True))
    refresh_btn.pack(side="left", padx=(0, 4))
    ttk.Button(btn_frame, text="Export Excel", command=export_excel).pack(
        side="left", padx=(0, 4))
    ttk.Button(btn_frame, text="Open spreadsheet", command=open_sheet).pack(side="left")

    # ======================= Prices tab ====================================
    MAX_ROWS_SHOWN = 500

    prices_top = ttk.Frame(prices_tab)
    prices_top.pack(fill="x", pady=(0, 6))
    prices_info = ttk.Label(prices_top, text="", foreground="#777")
    prices_info.pack(side="left")
    ttk.Button(prices_top, text="Refresh",
               command=lambda: do_refresh(manual=True)).pack(side="right")

    prices_wrap = ttk.Frame(prices_tab)
    prices_wrap.pack(fill="both", expand=True)
    prices_tv = ttk.Treeview(prices_wrap, show="headings")
    pvs = ttk.Scrollbar(prices_wrap, orient="vertical", command=prices_tv.yview)
    phs = ttk.Scrollbar(prices_wrap, orient="horizontal", command=prices_tv.xview)
    prices_tv.configure(yscrollcommand=pvs.set, xscrollcommand=phs.set)
    prices_tv.grid(row=0, column=0, sticky="nsew")
    pvs.grid(row=0, column=1, sticky="ns")
    phs.grid(row=1, column=0, sticky="ew")
    prices_wrap.rowconfigure(0, weight=1)
    prices_wrap.columnconfigure(0, weight=1)

    def refresh_prices_view():
        history = remote.history
        names = core.all_weapon_names(remote.server_cfg.get("weapons", []), history)
        cols = ["time"] + names
        prices_tv.delete(*prices_tv.get_children())
        prices_tv["columns"] = cols
        prices_tv.heading("time", text="Time")
        prices_tv.column("time", width=130, anchor="w", stretch=False)
        for n in names:
            prices_tv.heading(n, text=n)
            prices_tv.column(n, width=110, anchor="center", stretch=False)
        shown = history[-MAX_ROWS_SHOWN:]
        for snap in reversed(shown):                     # newest first
            ts = core.ts_local_str(snap["timestamp"])
            vals = [ts] + [snap["prices"].get(n, "") if
                           isinstance(snap["prices"].get(n), (int, float)) else "-"
                           for n in names]
            prices_tv.insert("", "end", values=vals)
        extra = f" (showing last {MAX_ROWS_SHOWN})" if len(history) > MAX_ROWS_SHOWN else ""
        prices_info.config(
            text=f"{len(history)} snapshot(s){extra} - '-' = no in-game listing "
                 f"- times shown in your local timezone")

    # ======================= Analysis tab ==================================
    ana_top = ttk.Frame(analysis_tab)
    ana_top.pack(fill="x", pady=(0, 6))
    ttk.Label(ana_top,
              text="Good buy price = projected sell x (1 - safety margin) - desired profit",
              foreground="#777").pack(side="left")
    ttk.Button(ana_top, text="Refresh",
               command=lambda: do_refresh(manual=True)).pack(side="right")

    ana_wrap = ttk.Frame(analysis_tab)
    ana_wrap.pack(fill="both", expand=True)
    ana_tv = ttk.Treeview(ana_wrap, show="headings")
    avs = ttk.Scrollbar(ana_wrap, orient="vertical", command=ana_tv.yview)
    ana_tv.configure(yscrollcommand=avs.set)
    ana_tv.grid(row=0, column=0, sticky="nsew")
    avs.grid(row=0, column=1, sticky="ns")
    ana_wrap.rowconfigure(0, weight=1)
    ana_wrap.columnconfigure(0, weight=1)

    ana_cols = [h.lower().replace(" ", "_") for h in core.ANALYSIS_HEADERS]
    ana_tv["columns"] = ana_cols
    for cid, h in zip(ana_cols, core.ANALYSIS_HEADERS):
        ana_tv.heading(cid, text=h)
        ana_tv.column(cid, width=120 if cid == "weapon" else 100,
                      anchor="w" if cid == "weapon" else "center")

    def refresh_analysis_view():
        ana_tv.delete(*ana_tv.get_children())
        for row in core.compute_analysis(remote.server_cfg.get("weapons", []),
                                         remote.history, settings["analysis"]):
            ana_tv.insert("", "end", values=row)

    def refresh_all_views():
        refresh_weapon_list()
        refresh_prices_view()
        refresh_analysis_view()

    refresh_all_views()

    # kick off: one full refresh shortly after launch, then the poll loop
    root.after(300, lambda: do_refresh(manual=True))
    root.after(1500 + AUTO_REFRESH_SECONDS * 1000, auto_poll)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
