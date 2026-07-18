"""
client.py - Riven Tracker desktop client
========================================
The app you and your friends run. It does NOT collect prices itself - the
cloud collector (GitHub Actions) does that hourly. This client:

  1. Downloads config.json + price_history.json from the tracker's GitHub repo
  2. Caches them locally so it still opens offline
  3. Lets each user set their OWN profit settings (desired profit, safety
     margin, sell percentile, lookback) - stored locally, per user
  4. Shows the Prices and Analysis views and can export the Excel workbook

Run from source:   python client.py
Build an exe:      see build_client.bat (PyInstaller)
"""

import json
import os
import sys
import threading
import urllib.request
from datetime import datetime

import tracker_core as core

# ---------------------------------------------------------------------------
# where the hosted data lives - set your repo here so friends never have to.
# Format: "githubusername/reponame"
# ---------------------------------------------------------------------------
DEFAULT_REPO = ""          # e.g. "yourname/riven-tracker"
DEFAULT_BRANCH = "main"

APP_TITLE = "Warframe Riven Tracker"


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
    return merged


def save_settings(s):
    core.atomic_write_json(SETTINGS_FILE, s)


# ---------------------------------------------------------------------------
# remote data
# ---------------------------------------------------------------------------
def raw_url(repo, branch, filename):
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}"


def http_get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": core.USER_AGENT,
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_remote(settings):
    """Download config + history from the repo, update local caches.
    Returns (server_cfg, history). Raises on network failure."""
    repo, branch = settings["repo"].strip(), settings["branch"].strip() or "main"
    if not repo or "/" not in repo:
        raise RuntimeError("No data source set. Enter the GitHub repo as "
                           "'username/reponame' in Setup and click Save.")
    server_cfg = http_get_json(raw_url(repo, branch, "config.json"))
    hist_data = http_get_json(raw_url(repo, branch, "price_history.json"))
    snaps = sorted(hist_data.get("snapshots", []), key=lambda s: s["timestamp"])
    core.atomic_write_json(CACHE_CONFIG, server_cfg)
    core.atomic_write_json(CACHE_HISTORY, {"snapshots": snaps})
    return server_cfg, snaps


def load_cached():
    server_cfg = core.load_json(CACHE_CONFIG, {}) or {}
    snaps = core.load_history_file(CACHE_HISTORY)
    return server_cfg, snaps


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    import webbrowser

    settings = load_settings()
    server_cfg, history = load_cached()
    state = {"server_cfg": server_cfg, "history": history}

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
        for w in state["server_cfg"].get("weapons", []):
            lb.insert("end", w["name"])

    # --- profit settings (LOCAL - each user has their own) -----------------
    set_frame = ttk.LabelFrame(frm, text="Profit settings (yours only - saved "
                                         "on this PC)", padding=8)
    set_frame.pack(fill="x", pady=(0, 8))

    fields = [
        ("Lookback window (samples)", "lookback",
         "int", 1, 10000, "How many recent hourly samples the analysis uses (168 = 1 week)"),
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
            logbox.insert("end", msg + "\n")
            logbox.see("end")
            logbox.config(state="disabled")
        root.after(0, _do)

    busy = {"flag": False}

    def refresh_data(silent=False):
        if busy["flag"]:
            return
        busy["flag"] = True
        refresh_btn.config(state="disabled")
        if not silent:
            gui_log(f"--- refreshing data {datetime.now():%H:%M:%S} ---")

        def worker():
            try:
                cfg, hist = fetch_remote(settings)
                state["server_cfg"], state["history"] = cfg, hist
                last = core.ts_local_str(hist[-1]["timestamp"]) if hist else "never"
                gui_log(f"downloaded {len(hist)} snapshot(s); latest: {last}")
                root.after(0, refresh_all_views)
                root.after(0, lambda: set_status(
                    f"Data updated - latest snapshot {last} "
                    f"(collector runs hourly; new data can lag a few minutes)"))
            except Exception as e:
                gui_log(f"could not refresh: {e}")
                root.after(0, lambda: set_status(
                    "Offline - showing cached data" if state["history"]
                    else "No data - set the data source and click Refresh"))
            finally:
                busy["flag"] = False
                root.after(0, lambda: refresh_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def export_excel():
        if not state["history"]:
            messagebox.showinfo("No data", "Refresh data first.")
            return
        path = os.path.join(app_dir(), settings["excel_file"])
        ok = core.export_workbook(path, state["server_cfg"].get("weapons", []),
                                  state["history"], settings["analysis"])
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

    refresh_btn = ttk.Button(btn_frame, text="Refresh data", command=refresh_data)
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
               command=lambda: refresh_data()).pack(side="right")

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
        history = state["history"]
        names = core.all_weapon_names(state["server_cfg"].get("weapons", []), history)
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
               command=lambda: refresh_data()).pack(side="right")

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
        for row in core.compute_analysis(state["server_cfg"].get("weapons", []),
                                         state["history"], settings["analysis"]):
            ana_tv.insert("", "end", values=row)

    def refresh_all_views():
        refresh_weapon_list()
        refresh_prices_view()
        refresh_analysis_view()

    refresh_all_views()

    # auto-refresh on launch (non-blocking; falls back to cache if offline)
    root.after(300, lambda: refresh_data(silent=True))
    root.mainloop()


if __name__ == "__main__":
    run_gui()
