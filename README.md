# Warframe Riven Price Tracker (cloud edition)

Tracks the lowest **in-game, direct-sale (buyout)** unrolled-riven price on
warframe.market for a shared list of weapons, every hour, **in the cloud** -
free, using GitHub Actions. A small desktop client (shareable as a single
`.exe`) pulls the shared data and computes **per-user** buy/sell
recommendations from each user's own profit settings.

```
GitHub repo (this folder)
├── collect.py + tracker_core.py     the collector (runs hourly via Actions)
├── config.json                      SHARED: tracked weapons, platform, max rerolls
├── price_history.json               SHARED: all snapshots (committed hourly by the bot)
└── client.py                        the desktop app  ->  build_client.bat -> RivenTracker.exe
```

- **Server-side (shared):** which weapons are tracked, the price history.
- **Client-side (per user):** desired profit, safety margin, sell percentile,
  lookback window - so two friends can look at the same data with different
  risk settings.

## One-time setup (tracker owner)

1. Create a **public** GitHub repository (public = unlimited free Actions
   minutes) and push everything in this folder to it. Example with
   [GitHub Desktop](https://desktop.github.com) or the command line:

   ```
   git init
   git add .
   git commit -m "riven tracker"
   git branch -M main
   git remote add origin https://github.com/YOURNAME/riven-tracker.git
   git push -u origin main
   ```

2. On github.com open the repo -> **Settings -> Actions -> General** and make
   sure "Workflow permissions" is set to **Read and write permissions**
   (needed so the hourly job can commit the updated history).

3. Open the **Actions** tab, select **Collect riven prices**, and click
   **Run workflow** once to test. Within a minute or two you should see a new
   commit "price snapshot ..." touching `price_history.json`. From now on it
   runs itself every hour.

4. Edit `client.py` and set `DEFAULT_REPO = "YOURNAME/riven-tracker"`, then
   run `build_client.bat` (Windows) to produce `dist\RivenTracker.exe`.
   Share that single file with friends - nothing else needed. (Python users
   can instead run `python client.py` with `client.py` + `tracker_core.py`.)

## Managing the weapon list

The tracked weapons live in `config.json` in the repo. To add/remove one,
edit that file on github.com (the client has an "Edit list on GitHub..."
button that jumps straight there). Entries look like:

```json
{ "name": "Torid", "url_name": "torid" }
```

`url_name` is the weapon's warframe.market slug - usually just the name in
lowercase with spaces as underscores (check the URL on warframe.market if
unsure). The change takes effect on the next hourly run.

## Using the client

- **Refresh data** downloads the latest shared history (it also refreshes
  automatically on launch, and falls back to the cached copy when offline).
- **Profit settings** are yours alone, saved next to the exe in
  `client_settings.json`.
- **Analysis**: Good buy price = projected sell x (1 - safety margin) -
  desired profit, computed over your lookback window.
- **Export Excel** rebuilds `riven_prices.xlsx` locally, same three sheets as
  before (Data / Analysis / Settings).

## Good to know

- **Timing jitter:** GitHub's cron is best-effort - the "hourly" run can start
  up to ~15-45 min late during busy periods. Data is timestamped (UTC on the
  server, shown in your local time in the client), so this is harmless.
- **Raw-file caching:** raw.githubusercontent.com caches for ~5 minutes, so a
  brand-new snapshot can take a few minutes to appear in the client.
- **Auto-disable:** GitHub pauses scheduled workflows in repos with no
  activity for 60 days - the hourly bot commits keep it alive by themselves,
  but if you ever see it stopped, one click on "Enable workflow" restores it.
- **Public repo:** anyone can see the weapon list and price history (it's just
  public market data - no accounts, no tokens involved).
- **History size:** hourly JSON snapshots are tiny (~a few MB per year for a
  handful of weapons); no pruning needed for a long time.

## Migrating from the old local version

Your old `price_history.json` was copied in as the starting history, so
nothing is lost. You can uninstall the old hourly Windows task with:
`schtasks /Delete /F /TN WarframeRivenTracker`
