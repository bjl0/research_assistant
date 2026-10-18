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

4. Open the dashboard. For live prices, start the little local server instead of double-clicking the file:

       python3 screener/local.py serve

   It opens `http://localhost:8765/dashboard.html`. Leave that terminal window open; **Reload prices** now fetches fresh market caps for your alerts from DEX Screener (a few seconds). Press Ctrl+C in the terminal to stop it. If you just double-click `screener_data/dashboard.html`, the button can only re-read the last run's saved data.

## Easier: a dashboard with a "Run scan now" button

Instead of steps 3 and 4, on Windows just double-click **`start_screener.bat`** in the main folder (or type `py screener\local.py serve`). A black window opens and stays open, and your browser opens the dashboard. Keep the black window open while you use the page; close it to stop.

- **Run scan now** (top of the page) does the same as `local.py run`, takes about a minute, and updates the page when it finishes.
- **Your limits:** change the numbers in the filter area, then press **Save these limits for scans**. They are written to `screener_data/params.json`, so every later scan (button, terminal or scheduled) uses them. Without pressing Save, the numbers only change what you see on screen.
- The page only works while that window is open. If you open `dashboard.html` as a plain file instead, the Run and Save buttons do not appear.

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


## Automatic scans while the dashboard is open

At the top of the served dashboard, tick **Auto scan every N minutes** and type the number of minutes you want (5 or more). The page counts down and starts a scan by itself, then refreshes. Your choice is remembered. It only works while the dashboard page and the screener window (where you ran `serve`) both stay open and the computer is awake.

## Telegram alerts (optional)

Each scan can message you on Telegram for every token that becomes an alert for the first time.

1. In Telegram, message **@BotFather**, send `/newbot`, follow the steps, and copy the **token** it gives you (keep it private).
2. Open a chat with your new bot and send it any message (for example "hi").
3. Open `https://api.telegram.org/bot<YOUR TOKEN>/getUpdates` in a browser and find `"chat":{"id":123456789` - that number is your **chat id**.
4. Create a file `screener_data/telegram.json` containing: `{"bot_token": "YOUR TOKEN", "chat_id": "123456789"}`

Messages only go out from scans (terminal, serve button or auto scan), never from Reload prices. If Telegram can't be reached, the scan still finishes. Delete the file to switch alerts off.
