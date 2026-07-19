"""
collect.py - cloud collector entry point (v2: recent + archive storage)
=======================================================================
Run every ~5 minutes by GitHub Actions / cron-job.org dispatch.

  1. One-time migration: splits legacy price_history.json into the new files
  2. Collects one snapshot for every weapon in config.json
     (hardened pacing: 2s between weapons, cooldown-and-retry on 429s)
  3. Appends to recent.json  (last 48h - what clients poll)
     and archive.json        (30-day compact per-weapon series)

Can also be run locally:  python collect.py
"""

import os
import sys
import time

import tracker_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
# Data files live on their own git branch, checked out into DATA_DIR by the
# workflow, so price commits never touch main. Local runs default to HERE.
DATA_DIR = os.environ.get("DATA_DIR") or HERE
CONFIG_FILE = os.path.join(HERE, "config.json")
LEGACY_HISTORY = os.path.join(DATA_DIR, "price_history.json")
RECENT_FILE = os.path.join(DATA_DIR, "recent.json")
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.json")
ITEMS_CACHE_FILE = os.path.join(DATA_DIR, "riven_items_cache.json")

GAP_SECONDS = 2.0            # polite spacing between weapon requests
RATE_LIMIT_COOLDOWN = 30     # pause after a 429, then retry same weapon
MAX_COOLDOWNS = 3            # after this many, record None and move on


def migrate_legacy(weapons):
    """Split price_history.json into recent.json + archive.json, once."""
    if not os.path.exists(LEGACY_HISTORY):
        return
    print("migrating legacy price_history.json -> recent.json + archive.json")
    snaps = core.load_history_file(LEGACY_HISTORY)
    for snap in snaps:
        core.append_history_file(RECENT_FILE, snap)
        core.update_archive_file(ARCHIVE_FILE, weapons, snap)
    core.prune_recent_file(RECENT_FILE)
    os.unlink(LEGACY_HISTORY)
    print(f"  migrated {len(snaps)} snapshot(s); legacy file removed")


def collect_hardened(weapons, platform, max_rerolls):
    """One snapshot with 429-resilient pacing."""
    prices = {}
    cooldowns = 0
    for i, w in enumerate(weapons):
        price = None
        fetched = False
        while not fetched:
            try:
                price = core.lowest_ingame_price(w["url_name"], platform,
                                                 max_rerolls)
                fetched = True
            except Exception as e:
                if "429" in str(e) and cooldowns < MAX_COOLDOWNS:
                    cooldowns += 1
                    print(f"  rate limited at {w['name']} - cooling "
                          f"{RATE_LIMIT_COOLDOWN}s "
                          f"({cooldowns}/{MAX_COOLDOWNS})")
                    time.sleep(RATE_LIMIT_COOLDOWN)
                    continue
                print(f"  ! {w['name']}: fetch failed ({e}); recording no data")
                fetched = True                     # price stays None
        prices[w["name"]] = price
        print(f"  {w['name']}: {price if price is not None else '-'}"
              f"  ({i + 1}/{len(weapons)})")
        if i < len(weapons) - 1:
            time.sleep(GAP_SECONDS)
    return {"timestamp": core.utcnow_iso(), "prices": prices}


def main():
    cfg = core.load_json(CONFIG_FILE, {}) or {}
    weapons = cfg.get("weapons", [])
    if not weapons:
        print("no weapons configured; nothing to collect")
        return 0
    platform = cfg.get("platform", "pc")
    max_rerolls = cfg.get("max_rerolls", 0)

    migrate_legacy(weapons)

    print(f"collecting {len(weapons)} weapon(s), platform={platform}, "
          f"max_rerolls={max_rerolls}")
    t0 = time.time()
    snap = collect_hardened(weapons, platform, max_rerolls)
    core.append_history_file(RECENT_FILE, snap)
    recent = core.prune_recent_file(RECENT_FILE)
    core.update_archive_file(ARCHIVE_FILE, weapons, snap)
    print(f"snapshot {snap['timestamp']} saved in "
          f"{time.time() - t0:.0f}s ({len(recent)} recent snapshots)")

    core.get_riven_items(ITEMS_CACHE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
