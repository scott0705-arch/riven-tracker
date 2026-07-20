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
DEFAULT_BRANCH = "main"            # code + config
DATA_BRANCH = "data"               # bot-maintained price data

APP_TITLE = "Warframe Riven Tracker"

# Auto-refresh cadence. 90s = 40 polls/hour, safely under GitHub's
# 60-requests/hour unauthenticated API limit even before ETag savings.
AUTO_REFRESH_SECONDS = 120         # recent.json only; data lands ~5-minutely
CONFIG_EVERY_N_POLLS = 10          # weapon list rarely changes; check it less

# The collector runs every ~5 minutes -> ~288 samples per day. Used to turn
# the user's "lookback (days)" into a sample count for the analysis.
SAMPLES_PER_DAY = 288
MAX_LOOKBACK_DAYS = 30             # matches the server's data retention cap

# Buy-panel range: the good-buy price is your ceiling; open negotiations
# this % below it. 20 -> a 400p good buy shows as "320-400".
BUY_RANGE_PCT = 20.0

CLIENT_ANALYSIS_DEFAULTS = {
    "lookback_days": 7,            # analysis window, in days
    "sell_percentile": 0.5,        # 0-1, where in the recent range you sell
    "desired_profit": 50,          # platinum profit wanted per flip
    "safety_margin_pct": 10.0,     # % haircut on the projected sell price
    "min_roi_pct": 15.0,           # minimum return-on-capital, in %
    "buy_range_pct": 20.0,         # opening-offer discount below the good buy
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
    "chat_fore": "WTB rivens ",        # text before the [weapon] list
    "chat_after": " pm me!",           # text after it
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
    if "min_roi_pct" in a:
        out["min_roi_pct"] = a["min_roi_pct"]
    if "buy_range_pct" in a:
        out["buy_range_pct"] = a["buy_range_pct"]
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
        "min_roi": float(a.get("min_roi_pct", 0.0)) / 100.0,
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

    def _get(self, filename, conditional=True, branch=None):
        repo, cfg_branch = self._repo_branch()
        use = branch or cfg_branch
        etag = self.etags.get(filename) if conditional else None
        data, new_etag = http_get_json(api_url(repo, use, filename), etag)
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
        rec = self._get("recent.json", conditional, branch=DATA_BRANCH)
        if rec is not None:
            snaps = sorted(rec.get("snapshots", []),
                           key=lambda s: s["timestamp"])
            self.recent = snaps
            core.atomic_write_json(CACHE_RECENT, {"snapshots": snaps})
            changed = True
        if include_archive:
            arch = self._get("archive.json", conditional,
                             branch=DATA_BRANCH)
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
    weapon_tab = ttk.Frame(nb, padding=10)
    samples_tab = ttk.Frame(nb, padding=10)
    settings_tab = ttk.Frame(nb, padding=10)
    nb.add(dash_tab, text="  Dashboard  ")
    nb.add(weapon_tab, text="  Weapon Data  ")
    nb.add(samples_tab, text="  Samples  ")
    nb.add(settings_tab, text="  Settings  ")

    status_var = tk.StringVar()
    status_bar = ttk.Frame(root)
    status_bar.pack(fill="x", padx=10, pady=(0, 6))
    ttk.Label(status_bar, textvariable=status_var,
              foreground="#555").pack(side="left")
    ttk.Button(status_bar, text="Refresh now",
               command=lambda: do_refresh(manual=True)).pack(side="right")

    def set_status(msg):
        status_var.set(msg)

    # ======================= Dashboard tab =================================
    # --- trade chat composer (180-char in-game limit) ----------------------
    CHAT_LIMIT = 180
    chat_frame = ttk.LabelFrame(dash_tab, text="Trade chat message",
                                padding=(6, 4))
    chat_frame.pack(fill="x", pady=(0, 6))
    chat_state = {"profitable": []}          # ordered names, buy price desc

    crow1 = ttk.Frame(chat_frame)
    crow1.pack(fill="x")
    ttk.Label(crow1, text="Before:").pack(side="left")
    fore_e = ttk.Entry(crow1, width=28)
    fore_e.insert(0, settings.get("chat_fore", ""))
    fore_e.pack(side="left", padx=(4, 12))
    ttk.Label(crow1, text="After:").pack(side="left")
    after_e = ttk.Entry(crow1, width=28)
    after_e.insert(0, settings.get("chat_after", ""))
    after_e.pack(side="left", padx=(4, 12))
    chat_count = ttk.Label(crow1, text="", foreground="#777")
    chat_count.pack(side="left")

    crow2 = ttk.Frame(chat_frame)
    crow2.pack(fill="x", pady=(4, 0))
    copy_var = tk.StringVar()
    copy_entry = ttk.Entry(crow2, textvariable=copy_var, state="readonly")
    copy_entry.pack(side="left", fill="x", expand=True)

    def copy_profitable():
        root.clipboard_clear()
        root.clipboard_append(copy_var.get())
        set_status("Trade chat message copied to clipboard")

    ttk.Button(crow1, text="Copy",
               command=copy_profitable).pack(side="right")

    def update_chat_message(_event=None):
        """Compose fore + [weapons] + after within CHAT_LIMIT. First-fit
        with skip: walk the profitable list in order; when one doesn't fit,
        try the next, all the way to the end (short names can still slot in
        after a long one fails)."""
        fore = fore_e.get()
        after = after_e.get()
        if fore != settings.get("chat_fore") or \
                after != settings.get("chat_after"):
            settings["chat_fore"] = fore
            settings["chat_after"] = after
            save_settings(settings)
        budget = CHAT_LIMIT - len(fore) - len(after)
        parts, used = [], 0
        skipped = 0
        for name in chat_state["profitable"]:
            b = f"[{name}]"
            if len(b) <= budget - used:
                parts.append(b)
                used += len(b)
            else:
                skipped += 1
        msg = fore + "".join(parts) + after
        copy_var.set(msg)
        total = len(chat_state["profitable"])
        note = f"{len(msg)}/{CHAT_LIMIT} chars - " \
               f"{len(parts)}/{total} weapon(s)"
        if skipped:
            note += f" ({skipped} skipped, no room)"
        over = len(msg) > CHAT_LIMIT           # only possible via fore/after
        chat_count.config(text=note, foreground="#b00" if over else "#777")

    fore_e.bind("<KeyRelease>", update_chat_message)
    after_e.bind("<KeyRelease>", update_chat_message)

    dash_body = ttk.Frame(dash_tab)
    dash_body.pack(fill="both", expand=True)

    # --- left: at-a-glance buy recommender (sorted, highest first) ---------
    buy_frame = ttk.LabelFrame(dash_body, text="Buy at", padding=(4, 4))
    buy_frame.pack(side="left", fill="y", padx=(0, 8))
    buy_tv = ttk.Treeview(buy_frame, show="headings",
                          columns=("weapon", "buy", "sell"), height=8)
    buy_tv.heading("weapon", text="Weapon")
    buy_tv.heading("buy", text="Buy range")
    buy_tv.heading("sell", text="Proj. sell")
    buy_tv.column("weapon", width=130, anchor="w", stretch=False)
    buy_tv.column("buy", width=92, anchor="e", stretch=False)
    buy_tv.column("sell", width=70, anchor="e", stretch=False)
    buy_tv.pack(fill="y", expand=True)

    def refresh_buy_panel(rows):
        """rows = compute_analysis_series output, sorted by good-buy price
        (index 7) desc; also rebuilds the copyable profitable string."""
        priced = [r for r in rows if isinstance(r[7], (int, float))]
        unpriced = [r for r in rows if not isinstance(r[7], (int, float))]
        priced.sort(key=lambda r: -r[7])
        buy_tv.delete(*buy_tv.get_children())
        rng = float(settings["analysis"].get("buy_range_pct", 20.0)) / 100.0
        for r in priced:
            upper = r[7]
            lower = round(upper * (1 - rng))
            label = f"{lower}-{upper}" if rng > 0 and lower < upper else upper
            buy_tv.insert("", "end", values=(r[0], label, r[6]))
        for r in unpriced:
            buy_tv.insert("", "end", values=(r[0], "-", "-"))
        buy_tv.configure(height=max(8, len(rows)))
        chat_state["profitable"] = [r[0] for r in priced
                                    if r[7] >= UNPROFITABLE_BELOW]
        update_chat_message()

    def ensure_window_fits():
        """Grow the window height (never shrink) so the buy panel fits -
        capped to the screen. Width is left to the user; the chat message
        line scrolls horizontally when longer than the window."""
        root.update_idletasks()
        need_h = root.winfo_reqheight()
        cur_h = root.winfo_height()
        if cur_h >= need_h or cur_h <= 1:
            return
        cap = root.winfo_screenheight() - 90
        root.geometry(f"{root.winfo_width()}x{min(need_h, cap)}")

    # --- right: price graph (window = analysis lookback) -------------------
    graph_frame = ttk.LabelFrame(dash_body, text="Price graph", padding=(6, 4))
    graph_frame.pack(side="left", fill="both", expand=True)
    gtop = ttk.Frame(graph_frame)
    gtop.pack(fill="x")
    ttk.Label(gtop, text="Weapon:").pack(side="left")
    graph_sel = ttk.Combobox(gtop, state="readonly", width=18)
    graph_sel.pack(side="left", padx=(6, 10))
    graph_info = ttk.Label(gtop, text="", foreground="#777")
    graph_info.pack(side="left")
    canvas = tk.Canvas(graph_frame, background="white", highlightthickness=0)
    canvas.pack(fill="both", expand=True, pady=(6, 0))
    graph_pts = []                     # [(cx, cy, epoch, price)] for hover
    GM = {"L": 52, "R": 12, "T": 12, "B": 30}      # plot margins

    def redraw_graph(_event=None):
        canvas.delete("all")
        graph_pts.clear()
        name = graph_sel.get()
        pts = remote.series_map().get(name, [])
        lookback = int(round(settings["analysis"]["lookback_days"]
                             * SAMPLES_PER_DAY))
        pts = pts[-lookback:]
        num = [(e, p) for e, p in pts if isinstance(p, (int, float))]
        graph_info.config(
            text=f"last {settings['analysis']['lookback_days']} day(s) - "
                 f"{len(num)} sample(s)")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 100 or h < 70:
            return
        if len(num) < 2:
            canvas.create_text(w // 2, h // 2, text="Not enough data yet",
                               fill="#999")
            return
        L, R, T, B = GM["L"], GM["R"], GM["T"], GM["B"]
        xs = [e for e, _ in num]
        ys = [p for _, p in num]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if y1 == y0:
            y0, y1 = y0 - 1, y1 + 1
        pad = (y1 - y0) * 0.08
        y0, y1 = y0 - pad, y1 + pad
        if x1 == x0:
            x1 = x0 + 1

        def X(e):
            return L + (w - L - R) * (e - x0) / (x1 - x0)

        def Y(p):
            return T + (h - T - B) * (1 - (p - y0) / (y1 - y0))

        canvas.create_line(L, T, L, h - B, fill="#bbb")
        canvas.create_line(L, h - B, w - R, h - B, fill="#bbb")
        for i in range(5):                          # y grid + labels
            v = y0 + (y1 - y0) * i / 4
            yy = Y(v)
            canvas.create_line(L, yy, w - R, yy, fill="#eee")
            canvas.create_text(L - 6, yy, text=f"{v:.0f}", anchor="e",
                               fill="#666", font=("Segoe UI", 8))
        for i in range(4):                          # x labels
            e = x0 + (x1 - x0) * i / 3
            lbl = datetime.fromtimestamp(e).strftime("%d %b %H:%M")
            # edge labels anchor inward so they never clip off the canvas
            anchor = "nw" if i == 0 else ("ne" if i == 3 else "n")
            canvas.create_text(X(e), h - B + 4, text=lbl, anchor=anchor,
                               fill="#666", font=("Segoe UI", 8))
        coords = []
        for e, p in num:
            cx, cy = X(e), Y(p)
            coords += [cx, cy]
            graph_pts.append((cx, cy, e, p))
        canvas.create_line(*coords, fill="#534AB7", width=2)

    def on_graph_hover(ev):
        canvas.delete("hover")
        if not graph_pts:
            return
        cx, cy, e, p = min(graph_pts, key=lambda t: abs(t[0] - ev.x))
        w, h = canvas.winfo_width(), canvas.winfo_height()
        canvas.create_line(cx, GM["T"], cx, h - GM["B"], fill="#ddd",
                           tags="hover")
        canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill="#534AB7",
                           outline="", tags="hover")
        txt = f"{p:.0f}p - {datetime.fromtimestamp(e).strftime('%d %b %H:%M')}"
        tx = min(max(cx + 10, GM["L"] + 8), w - 150)
        canvas.create_rectangle(tx - 4, 14, tx + 140, 32, fill="#ffffe0",
                                outline="#ccc", tags="hover")
        canvas.create_text(tx, 23, text=txt, anchor="w", fill="#333",
                           font=("Segoe UI", 8), tags="hover")

    canvas.bind("<Configure>", redraw_graph)
    canvas.bind("<Motion>", on_graph_hover)
    canvas.bind("<Leave>", lambda e: canvas.delete("hover"))
    graph_sel.bind("<<ComboboxSelected>>", redraw_graph)

    def refresh_graph_choices():
        names = selected_names()
        cur = graph_sel.get()
        graph_sel["values"] = names
        if cur not in names:
            graph_sel.set(names[0] if names else "")
        redraw_graph()

    # ======================= Weapon Data tab ===============================
    wd_top = ttk.Frame(weapon_tab)
    wd_top.pack(fill="x", pady=(0, 6))
    ttk.Label(wd_top,
              text="Good buy price = projected sell x (1 - safety margin) - desired profit",
              foreground="#777").pack(side="left")
    ttk.Button(wd_top, text="Refresh now",
               command=lambda: do_refresh(manual=True)).pack(side="right")

    ana_wrap = ttk.Frame(weapon_tab)
    ana_wrap.pack(fill="both", expand=True)
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
        refresh_graph_choices()
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

    # --- my weapons: pick which tracked weapons YOU see --------------------
    wf = ttk.LabelFrame(frm, text="My weapons - tick what you want on your "
                                  "dashboard (data is collected for all of "
                                  "them regardless; changes apply instantly)",
                        padding=8)
    wf.pack(fill="x", pady=(0, 8))
    checks_frame = ttk.Frame(wf)
    checks_frame.pack(side="left", fill="both", expand=True)
    check_vars = {}                                  # url_name -> BooleanVar
    PICKER_COLS = 3

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
    UNPROFITABLE_BELOW = 10        # good-buy upper bound under this = untick

    def untick_unprofitable():
        """Untick every weapon whose suggested buy price (the upper end of
        the buy range) is below UNPROFITABLE_BELOW - it can't meaningfully
        meet your profit target at current prices. Weapons with no data yet
        are left ticked (unknown, not unprofitable)."""
        rows = core.compute_analysis_series(
            remote.series_map(), engine_analysis(settings["analysis"]))
        bad = {r[0] for r in rows
               if isinstance(r[7], (int, float)) and r[7] < UNPROFITABLE_BELOW}
        slug_by_name = {w["name"]: w["url_name"]
                        for w in remote.server_cfg.get("weapons", [])}
        n = 0
        for name in bad:
            var = check_vars.get(slug_by_name.get(name))
            if var and var.get():
                var.set(False)
                n += 1
        on_toggle()
        set_status(f"Unticked {n} unprofitable weapon(s)" if n else
                   "No ticked weapons are unprofitable right now")

    ttk.Button(wbtns, text="All",
               command=lambda: set_all(True)).pack(fill="x", pady=(0, 3))
    ttk.Button(wbtns, text="Untick unprofitable",
               command=untick_unprofitable).pack(fill="x", pady=(0, 3))
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
    set_frame = ttk.LabelFrame(frm, text="Profit settings", padding=8)
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
        ("Safety margin (%)", "safety_margin_pct",
         "float", 0.0, 100.0,
         "Haircut on the projected sell price, as a percentage. 10% = assume "
         "you actually sell 10% below the projection, to be safe."),
        ("Desired profit (plat)", "desired_profit",
         "float", 0, 100000, "Platinum profit you want per flip."),
        ("Buy range (%)", "buy_range_pct",
         "float", 0.0, 90.0,
         "Negotiation room on the dashboard's Buy at panel. 20% turns a good "
         "buy of 400 into a 320-400 range: open at the low end, never pay "
         "past the top."),
        ("Minimum ROI (%)", "min_roi_pct",
         "float", 0.0, 500.0,
         "Minimum return on the plat you tie up. Desired profit is the floor; "
         "on expensive rivens the buy price drops further until profit is at "
         "least this % of the buy. 0 = off. Whichever rule demands the lower "
         "buy price wins."),
    ]
    entries = {}
    tip_labels = []
    for i, (label, key, _t, _lo, _hi, tip) in enumerate(fields):
        ttk.Label(set_frame, text=label).grid(row=i, column=0, sticky="nw", pady=2)
        e = ttk.Entry(set_frame, width=10)
        e.insert(0, str(settings["analysis"][key]))
        e.grid(row=i, column=1, sticky="nw", padx=(8, 12), pady=2)
        tip_lbl = ttk.Label(set_frame, text=tip, foreground="#777",
                            wraplength=380, justify="left")
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
                msg = str(e)
                gui_log(msg)
                root.after(0, lambda m=msg: set_status(
                    f"{m} - showing cached data, auto-refresh paused"))
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
    # width: sane cap - wide tabs (checkbox grid, tip text) must not blow the
    # window up; the dynamic tip re-wrap and column fitting handle narrowness
    w = min(max(900, root.winfo_reqwidth()), 1000,
            root.winfo_screenwidth() - 80)
    h = min(max(560, root.winfo_reqheight()), root.winfo_screenheight() - 90)
    root.geometry(f"{w}x{h}")

    # kick off: one full refresh shortly after launch, then the poll loop
    root.after(300, lambda: do_refresh(manual=True))
    root.after(1500 + AUTO_REFRESH_SECONDS * 1000, auto_poll)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
