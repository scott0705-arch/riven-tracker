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
     margin, sell percentile, lookback days) - stored locally, per user
  5. Tabs: Dashboard (analysis), Samples (raw price log), Settings

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
AUTO_REFRESH_SECONDS = 120         # recent.json only; data lands ~5-minutely
CONFIG_EVERY_N_POLLS = 10          # weapon list rarely changes; check it less

# The collector runs every ~5 minutes -> ~288 samples per day. Used to turn
# the user's "lookback (days)" into a sample count for the analysis.
SAMPLES_PER_DAY = 288
MAX_LOOKBACK_DAYS = 30             # matches the server's data retention cap

CLIENT_ANALYSIS_DEFAULTS = {
    "lookback_days": 7,            # analysis window, in days
    "sell_percentile": 0.5,        # 0-1, where in the recent range you sell
    "desired_profit": 50,          # platinum profit wanted per flip
    "safety_margin_pct": 10.0,     # % haircut on the projected sell price
}


def app_dir():
    """Directory of the exe (frozen) or the script - all local files go here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_FILE = os.path.join(app_dir(), "client_settings.json")
CACHE_RECENT = os.path.join(app_dir(), "cached_recent.json")
CACHE_ARCHIVE = os.path.join(app_dir(), "cached_archive.json")
CACHE_CONFIG = os.path.join(app_dir(), "cached_config.json")

DEFAULT_SETTINGS = {
    "repo": DEFAULT_REPO,
    "branch": DEFAULT_BRANCH,
    "analysis": dict(CLIENT_ANALYSIS_DEFAULTS),
    "excel_file": "riven_prices.xlsx",
    "selected_weapons": [],            # url_names; empty = all weapons
}


def _migrate_analysis(a):
    """Accept old-format analysis settings (lookback in samples, safety
    margin as a 0-1 decimal) and convert to the new friendly units."""
    out = dict(CLIENT_ANALYSIS_DEFAULTS)
    if not a:
        return out
    if "lookback_days" in a:
        out["lookback_days"] = a["lookback_days"]
    elif "lookback" in a:                       # old: sample count
        out["lookback_days"] = max(1, min(MAX_LOOKBACK_DAYS,
                                          round(a["lookback"] / SAMPLES_PER_DAY)))
    if "sell_percentile" in a:
        out["sell_percentile"] = a["sell_percentile"]
    if "desired_profit" in a:
        out["desired_profit"] = a["desired_profit"]
    if "safety_margin_pct" in a:
        out["safety_margin_pct"] = a["safety_margin_pct"]
    elif "safety_margin" in a:                  # old: 0-1 decimal
        out["safety_margin_pct"] = float(a["safety_margin"]) * 100.0
    return out


def load_settings():
    s = core.load_json(SETTINGS_FILE, {}) or {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(s)
    merged["analysis"] = _migrate_analysis(s.get("analysis"))
    if not merged.get("repo"):
        merged["repo"] = DEFAULT_REPO          # heal older empty settings files
    return merged


def save_settings(s):
    core.atomic_write_json(SETTINGS_FILE, s)


def engine_analysis(a):
    """Convert the client's friendly settings into what the analysis engine
    (tracker_core.compute_analysis) expects: samples + 0-1 decimal margin."""
    return {
        "lookback": int(round(a["lookback_days"] * SAMPLES_PER_DAY)),
        "sell_percentile": float(a["sell_percentile"]),
        "desired_profit": float(a["desired_profit"]),
        "safety_margin": float(a["safety_margin_pct"]) / 100.0,
    }


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
    """Owns the downloaded config/history, their ETags, and the local caches."""

    def __init__(self, settings):
        self.settings = settings
        self.server_cfg = core.load_json(CACHE_CONFIG, {}) or {}
        self.recent = core.load_history_file(CACHE_RECENT)
        self.archive = core.load_archive(CACHE_ARCHIVE)
        self.etags = {}                    # filename -> etag
        self.rate_limited_until = 0        # epoch; skip polls before this

    def series_map(self):
        return core.build_series_map(self.server_cfg.get("weapons", []),
                                     self.archive, self.recent)

    def _repo_branch(self):
        repo = self.settings["repo"].strip()
        branch = self.settings["branch"].strip() or "main"
        if not repo or "/" not in repo:
            raise RuntimeError("No data source set. Enter the GitHub repo as "
                               "'username/reponame' in Settings and click Save.")
        return repo, branch

    def _get(self, filename, conditional=True):
        repo, branch = self._repo_branch()
        etag = self.etags.get(filename) if conditional else None
        data, new_etag = http_get_json(api_url(repo, branch, filename), etag)
        if new_etag:
            self.etags[filename] = new_etag
        return data                        # None means "unchanged"

    def refresh(self, include_config=True, include_archive=False,
                conditional=True):
        """Fetch remote files. Polls fetch recent.json only; launch and
        manual refreshes also pull config + the 30-day archive.
        Returns True if anything changed."""
        changed = False
        if include_config:
            cfg = self._get("config.json", conditional)
            if cfg is not None:
                self.server_cfg = cfg
                core.atomic_write_json(CACHE_CONFIG, cfg)
                changed = True
        rec = self._get("recent.json", conditional)
        if rec is not None:
            snaps = sorted(rec.get("snapshots", []),
                           key=lambda s: s["timestamp"])
            self.recent = snaps
            core.atomic_write_json(CACHE_RECENT, {"snapshots": snaps})
            changed = True
        if include_archive:
            arch = self._get("archive.json", conditional)
            if arch is not None:
                self.archive = arch.get("weapons", {})
                core.atomic_write_json(CACHE_ARCHIVE, arch)
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

    def selected_slugs():
        sel = settings.get("selected_weapons") or []
        all_slugs = [w["url_name"] for w in remote.server_cfg.get("weapons", [])]
        return [s for s in sel if s in all_slugs] or all_slugs

    def selected_names():
        slugs = set(selected_slugs())
        return [w["name"] for w in remote.server_cfg.get("weapons", [])
                if w["url_name"] in slugs]

    def filtered_series_map():
        names = set(selected_names())
        return {n: pts for n, pts in remote.series_map().items()
                if n in names}

    root = tk.Tk()
    root.title(APP_TITLE)
    root.minsize(740, 540)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=6, pady=6)

    dash_tab = ttk.Frame(nb, padding=10)
    samples_tab = ttk.Frame(nb, padding=10)
    settings_tab = ttk.Frame(nb, padding=10)
    nb.add(dash_tab, text="  Dashboard  ")
    nb.add(samples_tab, text="  Samples  ")
    nb.add(settings_tab, text="  Settings  ")

    status_var = tk.StringVar()
    ttk.Label(root, textvariable=status_var, foreground="#555").pack(
        anchor="w", padx=10, pady=(0, 6))

    def set_status(msg):
        status_var.set(msg)

    # ======================= Dashboard tab =================================
    # For now: the analysis table. More widgets (price graphs etc.) later.
    dash_top = ttk.Frame(dash_tab)
    dash_top.pack(fill="x", pady=(0, 6))
    ttk.Label(dash_top,
              text="Good buy price = projected sell x (1 - safety margin) - desired profit",
              foreground="#777").pack(side="left")
    ttk.Button(dash_top, text="Refresh now",
               command=lambda: do_refresh(manual=True)).pack(side="right")

    dash_body = ttk.Frame(dash_tab)
    dash_body.pack(fill="both", expand=True)

    # --- left: at-a-glance buy recommender (sorted, highest first) ---------
    buy_frame = ttk.LabelFrame(dash_body, text="Buy at", padding=(4, 4))
    buy_frame.pack(side="left", fill="y", padx=(0, 8))
    buy_tv = ttk.Treeview(buy_frame, show="headings",
                          columns=("weapon", "buy"), height=8)
    buy_tv.heading("weapon", text="Weapon")
    buy_tv.heading("buy", text="Buy \u2264")
    buy_tv.column("weapon", width=130, anchor="w", stretch=False)
    buy_tv.column("buy", width=62, anchor="e", stretch=False)
    buy_tv.pack(fill="y", expand=True)

    def refresh_buy_panel(rows):
        """rows = compute_analysis_series output. Sort by good-buy price
        (index 7), highest first; rows without a price go to the bottom."""
        priced = [r for r in rows if isinstance(r[7], (int, float))]
        unpriced = [r for r in rows if not isinstance(r[7], (int, float))]
        priced.sort(key=lambda r: -r[7])
        buy_tv.delete(*buy_tv.get_children())
        for r in priced:
            buy_tv.insert("", "end", values=(r[0], r[7]))
        for r in unpriced:
            buy_tv.insert("", "end", values=(r[0], "-"))
        buy_tv.configure(height=max(8, len(rows)))

    def ensure_window_fits():
        """Grow the window (never shrink it) so the buy panel's full height
        is visible - capped to the screen."""
        root.update_idletasks()
        need_h = root.winfo_reqheight()
        cur_h = root.winfo_height()
        if cur_h >= need_h or cur_h <= 1:
            return
        cap = root.winfo_screenheight() - 90
        root.geometry(f"{root.winfo_width()}x{min(need_h, cap)}")

    ana_wrap = ttk.Frame(dash_body)
    ana_wrap.pack(side="left", fill="both", expand=True)
    ana_tv = ttk.Treeview(ana_wrap, show="headings")
    avs = ttk.Scrollbar(ana_wrap, orient="vertical", command=ana_tv.yview)
    ana_tv.configure(yscrollcommand=avs.set)
    ana_tv.grid(row=0, column=0, sticky="nsew")
    avs.grid(row=0, column=1, sticky="ns")
    ana_wrap.rowconfigure(0, weight=1)
    ana_wrap.columnconfigure(0, weight=1)

    ana_cols = [h.lower().replace(" ", "_") for h in core.ANALYSIS_HEADERS]
    # compact headers so they survive narrow columns (Excel export keeps
    # the full names from core.ANALYSIS_HEADERS)
    ANA_DISPLAY = ["Weapon", "Latest", "Samples", "Min", "Median", "Avg",
                   "Proj. sell", "Good buy", "Profit"]
    ana_tv["columns"] = ana_cols
    for cid, h in zip(ana_cols, ANA_DISPLAY):
        ana_tv.heading(cid, text=h)
        # stretch=False: widths are managed entirely by _fit_ana_columns
        ana_tv.column(cid, width=100, minwidth=60, stretch=False,
                      anchor="w" if cid == "weapon" else "center")

    def _fit_ana_columns(event=None):
        """Distribute the available width across the columns so the table
        always fits exactly - on first draw and on every resize."""
        outer = event.width if event is not None else ana_tv.winfo_width()
        usable = outer - 24            # theme borders/padding safety inset
        n_other = len(ana_cols) - 1
        if usable < 60 * len(ana_cols):
            return                     # window too small to bother
        other_w = max(60, int(usable * 0.84) // n_other)
        weapon_w = usable - other_w * n_other      # sums to usable exactly
        ana_tv.column("weapon", width=weapon_w)
        for cid in ana_cols[1:]:
            ana_tv.column(cid, width=other_w)

    ana_tv.bind("<Configure>", _fit_ana_columns)

    def refresh_analysis_view():
        rows = core.compute_analysis_series(
            filtered_series_map(), engine_analysis(settings["analysis"]))
        ana_tv.delete(*ana_tv.get_children())
        for row in rows:
            ana_tv.insert("", "end", values=row)
        refresh_buy_panel(rows)
        ensure_window_fits()

    # ======================= Samples tab ===================================
    MAX_ROWS_SHOWN = 500

    samples_top = ttk.Frame(samples_tab)
    samples_top.pack(fill="x", pady=(0, 6))
    samples_info = ttk.Label(samples_top, text="", foreground="#777")
    samples_info.pack(side="left")
    ttk.Button(samples_top, text="Refresh now",
               command=lambda: do_refresh(manual=True)).pack(side="right")

    samples_wrap = ttk.Frame(samples_tab)
    samples_wrap.pack(fill="both", expand=True)
    samples_tv = ttk.Treeview(samples_wrap, show="headings")
    svs = ttk.Scrollbar(samples_wrap, orient="vertical", command=samples_tv.yview)
    shs = ttk.Scrollbar(samples_wrap, orient="horizontal", command=samples_tv.xview)
    samples_tv.configure(yscrollcommand=svs.set, xscrollcommand=shs.set)
    samples_tv.grid(row=0, column=0, sticky="nsew")
    svs.grid(row=0, column=1, sticky="ns")
    shs.grid(row=1, column=0, sticky="ew")
    samples_wrap.rowconfigure(0, weight=1)
    samples_wrap.columnconfigure(0, weight=1)

    def refresh_samples_view():
        history = remote.recent
        names = selected_names()
        cols = ["time"] + names
        samples_tv.delete(*samples_tv.get_children())
        samples_tv["columns"] = cols
        samples_tv.heading("time", text="Time")
        samples_tv.column("time", width=130, anchor="w", stretch=False)
        for n in names:
            samples_tv.heading(n, text=n)
            samples_tv.column(n, width=110, anchor="center", stretch=False)
        shown = history[-MAX_ROWS_SHOWN:]
        for snap in reversed(shown):                     # newest first
            ts = core.ts_local_str(snap["timestamp"])
            vals = [ts] + [snap["prices"].get(n, "") if
                           isinstance(snap["prices"].get(n), (int, float)) else "-"
                           for n in names]
            samples_tv.insert("", "end", values=vals)
        extra = f" (showing last {MAX_ROWS_SHOWN})" if len(history) > MAX_ROWS_SHOWN else ""
        samples_info.config(
            text=f"{len(history)} sample(s) from the last 48h{extra} - "
                 f"'-' = no in-game listing - times in your local timezone")

    # ======================= Settings tab ==================================
    frm = settings_tab

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

    # --- my weapons: pick which tracked weapons YOU see --------------------
    wf = ttk.LabelFrame(frm, text="My weapons - tick what you want on your "
                                  "dashboard (data is collected for all of "
                                  "them regardless; changes apply instantly)",
                        padding=8)
    wf.pack(fill="x", pady=(0, 8))
    checks_frame = ttk.Frame(wf)
    checks_frame.pack(side="left", fill="both", expand=True)
    check_vars = {}                                  # url_name -> BooleanVar
    PICKER_COLS = 4

    def on_toggle():
        picked = [slug for slug, v in check_vars.items() if v.get()]
        all_slugs = [w["url_name"]
                     for w in remote.server_cfg.get("weapons", [])]
        if not picked:                               # none ticked = all
            settings["selected_weapons"] = []
            for v in check_vars.values():
                v.set(True)
            set_status("Nothing ticked - showing all weapons")
        else:
            settings["selected_weapons"] = \
                [] if len(picked) == len(all_slugs) else picked
            set_status(f"Watching {len(picked)} weapon(s)")
        save_settings(settings)
        refresh_all_views()

    def set_all(state):
        for v in check_vars.values():
            v.set(state)
        on_toggle()

    def open_repo_config():
        repo = settings["repo"].strip()
        if repo:
            webbrowser.open(f"https://github.com/{repo}/edit/"
                            f"{settings['branch'] or 'main'}/config.json")
        else:
            messagebox.showinfo("No repo set", "Set the data source first.")

    wbtns = ttk.Frame(wf)
    wbtns.pack(side="left", padx=(8, 0), anchor="n")
    ttk.Button(wbtns, text="All",
               command=lambda: set_all(True)).pack(fill="x", pady=(0, 3))
    ttk.Button(wbtns, text="Edit shared list on GitHub...",
               command=open_repo_config).pack(fill="x")

    def refresh_weapon_list():
        weapons = remote.server_cfg.get("weapons", [])
        for child in checks_frame.winfo_children():
            child.destroy()
        check_vars.clear()
        sel = set(settings.get("selected_weapons") or [])
        for i, w in enumerate(weapons):
            var = tk.BooleanVar(value=(not sel or w["url_name"] in sel))
            check_vars[w["url_name"]] = var
            cb = ttk.Checkbutton(checks_frame, text=w["name"], variable=var,
                                 command=on_toggle)
            cb.grid(row=i // PICKER_COLS, column=i % PICKER_COLS,
                    sticky="w", padx=(0, 12), pady=1)

    # --- profit settings (LOCAL - each user has their own) -----------------
    set_frame = ttk.LabelFrame(frm, text="Profit settings (yours only - saved "
                                         "on this PC)", padding=8)
    set_frame.pack(fill="x", pady=(0, 8))

    PCTL_TIP = ("How optimistic your projected sell price is, from 0 to 1. "
                "The analysis looks at all the prices seen over your lookback "
                "window and picks a point in that range: 0.5 = the typical "
                "(middle) price - a realistic sale. Lower (e.g. 0.25) = "
                "price at the cheap end to sell quickly. Higher (e.g. 0.75) = "
                "hold out for the expensive end, slower to sell.")

    fields = [
        ("Lookback window (days)", "lookback_days",
         "int", 1, MAX_LOOKBACK_DAYS,
         f"How many days of price data the analysis uses (1 day = "
         f"{SAMPLES_PER_DAY} samples, worked out automatically). "
         f"Max {MAX_LOOKBACK_DAYS} days."),
        ("Projected sell percentile", "sell_percentile",
         "float", 0.0, 1.0, PCTL_TIP),
        ("Desired profit (plat)", "desired_profit",
         "float", 0, 100000, "Platinum profit you want per flip."),
        ("Safety margin (%)", "safety_margin_pct",
         "float", 0.0, 100.0,
         "Haircut on the projected sell price, as a percentage. 10% = assume "
         "you actually sell 10% below the projection, to be safe."),
    ]
    entries = {}
    tip_labels = []
    for i, (label, key, _t, _lo, _hi, tip) in enumerate(fields):
        ttk.Label(set_frame, text=label).grid(row=i, column=0, sticky="nw", pady=2)
        e = ttk.Entry(set_frame, width=10)
        e.insert(0, str(settings["analysis"][key]))
        e.grid(row=i, column=1, sticky="nw", padx=(8, 12), pady=2)
        tip_lbl = ttk.Label(set_frame, text=tip, foreground="#777",
                            wraplength=520, justify="left")
        tip_lbl.grid(row=i, column=2, sticky="w", pady=2)
        tip_labels.append(tip_lbl)
        entries[key] = e
    ttk.Label(set_frame,
              text="Good buy price = projected sell x (1 - safety margin) - desired profit",
              foreground="#555").grid(row=len(fields), column=0, columnspan=3,
                                      sticky="w", pady=(6, 2))

    def apply_settings():
        new = {}
        for label, key, typ, lo, hi, _tip in fields:
            raw = entries[key].get().strip().rstrip("%")
            try:
                v = int(raw) if typ == "int" else float(raw)
            except ValueError:
                messagebox.showwarning("Invalid value",
                                       f"'{raw}' is not a number ({label}).")
                return
            if not (lo <= v <= hi):
                messagebox.showwarning("Invalid value",
                                       f"{label} must be between {lo} and {hi}.")
                return
            new[key] = v
        settings["analysis"] = new
        save_settings(settings)
        refresh_analysis_view()
        set_status("Profit settings saved - Dashboard updated")

    ttk.Button(set_frame, text="Apply", command=apply_settings).grid(
        row=0, column=3, rowspan=2, padx=(16, 0), sticky="n")
    set_frame.columnconfigure(2, weight=1)

    def _rewrap_tips(event):
        """Keep the grey explanation text wrapped to the space actually
        available (labels + entry + Apply button take ~320px)."""
        avail = max(220, event.width - 320)
        for lab in tip_labels:
            lab.configure(wraplength=avail)

    set_frame.bind("<Configure>", _rewrap_tips)

    # --- actions + log -----------------------------------------------------
    btn_frame = ttk.Frame(frm)
    btn_frame.pack(fill="x", pady=(0, 8))

    logbox = tk.Text(frm, height=5, state="disabled", font=("Consolas", 9))
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
        return (core.ts_local_str(remote.recent[-1]["timestamp"])
                if remote.recent else "never")

    def do_refresh(manual):
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
                                             include_archive=True,
                                             conditional=False)
                    gui_log(f"refreshed (incl. 30-day archive) - "
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
                    f"Up to date - latest sample {latest_str()} - "
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
        if not remote.recent:
            messagebox.showinfo("No data", "Refresh data first.")
            return
        path = os.path.join(app_dir(), settings["excel_file"])
        ok = core.export_workbook(path, remote.server_cfg.get("weapons", []),
                                  remote.recent, filtered_series_map(),
                                  engine_analysis(settings["analysis"]))
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

    # ======================= shared refresh ================================
    def refresh_all_views():
        refresh_weapon_list()
        refresh_samples_view()
        refresh_analysis_view()

    refresh_all_views()

    # size the window so every tab fits on first launch (the notebook's
    # requested size is that of its tallest/widest tab - the Settings tab)
    root.update_idletasks()
    w = max(900, root.winfo_reqwidth())
    h = max(560, root.winfo_reqheight())
    root.geometry(f"{w}x{h}")

    # kick off: one full refresh shortly after launch, then the poll loop
    root.after(300, lambda: do_refresh(manual=True))
    root.after(1500 + AUTO_REFRESH_SECONDS * 1000, auto_poll)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
