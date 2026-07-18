"""
collect.py - cloud collector entry point
========================================
Run hourly by GitHub Actions (see .github/workflows/collect.yml).

Reads config.json (weapons / platform / max_rerolls), takes one price
snapshot from warframe.market, and appends it to price_history.json.
The workflow then commits the updated files back to the repository, which is
what makes the data visible to every client.

Can also be run locally:  python collect.py
"""

import os
import sys

import tracker_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
HISTORY_FILE = os.path.join(HERE, "price_history.json")
ITEMS_CACHE_FILE = os.path.join(HERE, "riven_items_cache.json")


def main():
    cfg = core.load_json(CONFIG_FILE, {}) or {}
    weapons = cfg.get("weapons", [])
    if not weapons:
        print("no weapons configured in config.json; nothing to collect")
        return 0

    platform = cfg.get("platform", "pc")
    max_rerolls = cfg.get("max_rerolls", 0)

    print(f"collecting {len(weapons)} weapon(s), platform={platform}, "
          f"max_rerolls={max_rerolls}")
    snap = core.collect_snapshot(weapons, platform, max_rerolls, progress=print)
    core.append_history_file(HISTORY_FILE, snap)
    history = core.prune_history_file(HISTORY_FILE)
    print(f"snapshot {snap['timestamp']} saved "
          f"({len(history)} total after 14-day prune)")

    # refresh the weapon-name cache occasionally so clients can pull it too
    core.get_riven_items(ITEMS_CACHE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
