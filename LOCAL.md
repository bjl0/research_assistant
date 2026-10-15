# Running the screener on your own computer (no Claude, no usage credits)

You need Python 3 (free, from python.org; on Windows tick "Add Python to PATH" when installing) and an internet connection.

1. Download this repository (green **Code** button on GitHub, then **Download ZIP**) and unzip it.
2. Open a terminal in the unzipped folder.
   - Windows: open the folder, click the address bar, type `cmd`, press Enter.
   - Mac: right-click the folder, **New Terminal at Folder**.
3. Run one refresh (takes about a minute):

       python3 screener/local.py run          (on Windows: py screener\local.py run)

   Type it from the main folder (the one that contains `screener`, `dashboard` and `data`). If you are inside the `screener` folder it also works as `python3 local.py run`; the results always go to `screener_data` in the main folder.

   If it says it cannot verify certificates (common with Python from python.org on a Mac), open Applications > Python 3.x in Finder, double-click **Install Certificates.command**, and run it again.

4. Open `screener_data/dashboard.html` in your browser (double-click it). After each later run, press **Reload prices** on the page, or refresh the browser tab.

Run it as often as you like. To make it automatic, schedule that same command every hour or two: Task Scheduler on Windows, `crontab -e` on Mac/Linux (for example `24 * * * * cd /path/to/research_assistant && python3 screener/local.py run`). The computer has to be on and online at that time.

## What gets saved (in `screener_data/`, nothing is ever deleted)

| Where | What |
| --- | --- |
| `current/` | the newest screened tokens and alerts |
| `history/<time>/` | tokens and alerts exactly as they stood after every run |
| `raw/<time>/` | the unedited answers from DEX Screener and RugCheck for every run |
| `log/alerts_log.csv` | one line per alert per run: market cap, change since first alert, peak, lowest point, max drawdown, all-time high. Open it in Excel or Google Sheets to see how each alert moved over time |
| `log/tokens_log.csv` | one line per screened token per run: liquidity, volume, holders, wallet shares, flags |
| `log/new_alerts.txt` | every new alert ever raised, with the time |
| `log/runs.csv` | one line per run, with any download problems |
| `params.json` | your saved limits (edit it to change them for local runs) |

The first run carries on from the alert history saved in `data/alerts.json`, so nothing starts from zero. Back up the `screener_data` folder if you want to keep the history safe.

## Differences from the Claude dashboard

- Same page and same screening rules. The data is only as new as your last run.
- "Acted on" marks and any limits you change on the page are saved in your browser only.
- The green top-trader marks are not available locally yet (they need the wallet label database that lives in Claude).
- The claude.ai dashboard is not updated by local runs. The scheduled Claude tasks are separate and do not run when they are switched off.
