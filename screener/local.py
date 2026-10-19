#!/usr/bin/env python3
"""Run the whole Solana launch screener from your own computer, with no Claude and no usage credits.

  python3 local.py run   [FOLDER]   fetch, screen, update and save everything (default folder: ./screener_data)
  python3 local.py page  [FOLDER]   rewrite FOLDER/dashboard.html (done automatically by run)
  python3 local.py watch [MINUTES] [FOLDER]   scan every MINUTES (default 10, minimum 5) with no browser; keeps a Mac awake;
                                    Telegram warning after 3 failed scans in a row (Ctrl+C to stop)
  python3 local.py serve [FOLDER]   open the dashboard in your browser with a "Run scan now" button and a way to save
                                    your limits; leave the window open while you use it (Ctrl+C to stop)

Needs only Python 3.8+ and internet. It uses screen.py (same folder) for all the screening rules, so the results are
the same as the hourly Claude task, and the same strategy limits apply (FOLDER/params.json, copied from
../dashboard/params.json the first time).

FOLDER holds
  dashboard.html        open this in a browser; it reads current/data.js, so "Reload prices" shows the newest run
  current/              the newest tokens.json, alerts.json, profiles.json and data.js
  history/<time>/       tokens.json and alerts.json as they stood after each run (thumbnails left out to save space)
  raw/<time>/           the raw answers from DEX Screener and RugCheck for each run, as downloaded
  log/runs.csv          one line per run
  log/alerts_log.csv    one line per tracked alert per run: market cap, change since first alert, drawdown, and more
  log/tokens_log.csv    one line per screened token per run
  log/new_alerts.txt    every new alert ever raised, with the time
Nothing is ever deleted by the program.
"""
import sys, os, re, json, html, subprocess, csv, time, shutil, ssl, datetime as dt, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import screen as S  # noqa: E402

# python.org's Python on a Mac ships without its security certificates until "Install Certificates.command" is run, which
# makes every download fail with CERTIFICATE_VERIFY_FAILED. Use the certifi bundle when it is installed. Certificate
# checking is never switched off.
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
    ssl._create_default_https_context = lambda *a, **k: _CTX
except Exception:
    pass

SSL_HELP = ("Your Python cannot verify website certificates (a common first-time problem with Python from python.org on a Mac). "
            "Fix, once: in Finder open Applications > Python 3.x and double-click \"Install Certificates.command\", "
            "or run:  python3 -m pip install certifi   then run this again.")

UA = {"User-Agent": "Mozilla/5.0 (launch screener, local run)", "Accept": "application/json"}
PROFILES = ["https://api.dexscreener.com/token-profiles/latest/v1", "https://api.dexscreener.com/token-boosts/latest/v1",
            "https://api.dexscreener.com/token-boosts/top/v1"]
REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/%s/report"
USER_URL = "https://frontend-api-v3.pump.fun/users/%s"
PAUSE = 0.35   # seconds between requests to the same site


UNREACHABLE = set()   # sites this computer could not even find (no internet, DNS or VPN trouble) during this run
DNS_WORDS = ("nodename nor servname", "Name or service not known", "getaddrinfo", "Temporary failure in name resolution", "Network is unreachable")
RUGCHECK_FAILED = set()   # tokens whose RugCheck report could not be loaded this run
REFUSED = {}   # site -> number of answers this run that said "too many requests" (429) or "refused" (403)


def get_json(url, tries=3):
    last = None
    host = url.split("/")[2]
    if host in UNREACHABLE:
        raise RuntimeError("%s: cannot reach this site from this computer" % host)
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as f:
                return json.load(f)
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code in (400, 404):
                break          # nothing there; asking again will not help
            if e.code in (429, 403):
                REFUSED[host] = REFUSED.get(host, 0) + 1
                wait = e.headers.get("Retry-After", "") if e.headers else ""
                time.sleep(min(int(wait), 30) if wait.isdigit() else 3 * (i + 1))   # the site asked us to slow down
        except Exception as e:
            last = str(getattr(e, "reason", e))
            if any(w in last for w in DNS_WORDS):
                UNREACHABLE.add(host)
                break           # the name will not resolve; asking again will not help
        time.sleep(1.5 * (i + 1))
    if last and "CERTIFICATE_VERIFY_FAILED" in last:
        raise SystemExit(SSL_HELP)
    raise RuntimeError("%s: %s" % (url.split("?")[0], last))


def telegram_settings(folder):
    """Bot token and chat id, from screener_data/telegram.json or the TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID variables."""
    c = S.read_json(os.path.join(folder, "telegram.json"), {})
    c = c if isinstance(c, dict) else {}
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or c.get("bot_token") or "").strip(), str(os.environ.get("TELEGRAM_CHAT_ID") or c.get("chat_id") or "").strip()


def telegram_send(token, chat, text):
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token,
                                 data=json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as f:
        f.read()


def money(v):
    """$68.2K style; None becomes n/a."""
    if not isinstance(v, (int, float)):
        return "n/a"
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= lim:
            return "$%.1f%s" % (v / lim, suf)
    return "$%d" % v


def pct(v):
    return "%+.0f%%" % v if isinstance(v, (int, float)) else "n/a"


def alert_message(a):
    """Telegram HTML message for one new alert. Names and links come from strangers, so everything is escaped or checked."""
    esc = lambda x: html.escape(str(x), quote=True)
    snap = a.get("first_alert_snapshot") or {}
    g = lambda k: snap.get(k) if snap.get(k) is not None else a.get(k)
    mint = str(a.get("mint") or "")
    plat = "pump.fun" if mint.endswith("pump") or "pump" in str(a.get("venue") or "").lower() else str(a.get("venue") or "")
    lock = g("locked_lp_pct")
    lp = money(g("liquidity_usd")) + (" 🔒%.0f%% locked" % lock if isinstance(lock, (int, float)) and lock > 0 else "")
    rows = [("MC at alert", money(a.get("first_alert_cap_usd") or g("market_cap_usd"))),
            ("Vol", money(g("volume_24h_usd")) + " (24h)"),
            ("LP", lp),
            ("Vol/LP", "%sx" % g("vol_to_liq") if g("vol_to_liq") is not None else "n/a"),
            ("1H", pct(g("change_1h_pct"))),
            ("24H", "%s  🅑%s Ⓢ%s" % (pct(g("change_24h_pct")), g("buys_24h") if g("buys_24h") is not None else "?", g("sells_24h") if g("sells_24h") is not None else "?")),
            ("Hold", "{:,}".format(g("holders")) if isinstance(g("holders"), (int, float)) else "n/a"),
            ("Top10", "%s%% · max %s%%" % (g("top10_wallets_pct"), g("largest_wallet_pct"))),
            ("Age", str(a.get("first_alert_age") or a.get("age") or "n/a"))]
    rug = [("Flags", str(g("flags") or "none")[:120]),
           ("Linked", "%s wallets" % g("linked_wallets") if g("linked_wallets") is not None else "n/a")]
    stats = "\n".join("%s %s: %s" % ("└" if i == len(rows) - 1 else "├", k, v) for i, (k, v) in enumerate(rows))
    rugtxt = "\n".join("%s %s: %s" % ("└" if i == len(rug) - 1 else "├", k, v) for i, (k, v) in enumerate(rug))
    soc = []
    x = a.get("x_url")
    if isinstance(x, str) and x.startswith(("https://x.com/", "https://twitter.com/")):
        soc.append('<a href="%s">𝕏</a>' % esc(x))
    pf = a.get("pumpfun_url")
    if isinstance(pf, str) and pf.startswith("https://pump.fun/"):
        soc.append('<a href="%s">pump.fun</a>' % esc(pf))
    out = ["🚨 <b>New alert: %s</b>" % esc(str(a.get("symbol") or "?")[:20]), esc(str(a.get("name") or "")[:40]) + (" · " + esc(plat) if plat else ""),
           "", "📊 <b>Stats</b>", esc(stats), "", "🛡 <b>RugCheck</b>", esc(rugtxt)]
    if soc:
        out += ["", "🔗 Socials: " + " • ".join(soc)]
    out += ["", '📈 <a href="https://dexscreener.com/solana/%s">DEX Screener</a> • <a href="https://gmgn.ai/sol/token/%s">GMGN</a>' % (mint, mint),
            "<code>%s</code>" % mint]
    return "\n".join(out)


def notify_new_alerts(folder, prev_alerts, alerts):
    """Telegram message for each alert whose token was never in the alert history before this run. Plain text, never stops a run."""
    token, chat = telegram_settings(folder)
    if not token or not chat:
        return
    seen = {a.get("mint") for a in prev_alerts}
    new = [a for a in alerts if a.get("mint") not in seen and not a.get("removed_at")]
    sent = 0
    try:
        for a in new[:10]:
            mint = str(a.get("mint") or "")
            if not re.fullmatch(S.B58, mint):
                continue
            telegram_send(token, chat, alert_message(a))
            sent += 1
        if len(new) > 10:
            telegram_send(token, chat, "...and %d more new alerts. Open the dashboard." % (len(new) - 10))
    except Exception as e:
        print("Telegram message failed (the scan itself is fine):", getattr(e, "reason", e))
    if sent:
        print("Sent %d Telegram alert(s)." % sent)


def rate_notice(folder, ok):
    """Write current/status.json (the dashboard banner reads it), shout in the terminal, and send one Telegram message when it starts."""
    path = os.path.join(folder, "current", "status.json")
    was = bool((S.read_json(path, {}) or {}).get("limited"))
    limited = dict(REFUSED)
    cut = sorted(UNREACHABLE)
    save(path, {"at": S.iso(time.time()), "limited": limited, "unreachable": cut, "scan_ok": ok})
    if cut:
        print("\n" + "!" * 70 + "\n  CANNOT REACH: %s\n  This computer could not look these sites up (internet down, Wi-Fi switched, VPN or DNS blocking).\n  Nothing can be screened until it is back. Check the connection, then try again.\n" % ", ".join(cut) + "!" * 70)
    if limited:
        sites = ", ".join("%s (%d)" % kv for kv in sorted(limited.items()))
        print("\n" + "!" * 70 + "\n  RATE LIMITED: %s told the screener to slow down or refused requests.\n  Alerts may be missing or late until it stops. Try a longer wait between scans (watch 15).\n" % sites + "!" * 70)
        if not was:
            token, chat = telegram_settings(folder)
            try:
                if token and chat:
                    telegram_send(token, chat, "⚠️ Screener is being rate limited by %s. Scans may miss alerts until it clears." % html.escape(sites))
            except Exception:
                pass


def notify_unverified(folder, p, prev_alerts, tokens):
    """RugCheck could not be loaded, so these tokens cannot be checked against the holder, lock and wallet limits. Tell Blake once per token,
    clearly marked as unverified. They are not saved as alerts; when RugCheck works again a token that passes becomes a normal alert."""
    token, chat = telegram_settings(folder)
    if not token or not chat or not RUGCHECK_FAILED:
        return
    path = os.path.join(folder, "current", "unverified_sent.json")
    sent = set(S.read_json(path, []) or [])
    seen = {a.get("mint") for a in prev_alerts}
    todo = [t for t in tokens if t.get("mint") in RUGCHECK_FAILED and t["mint"] not in sent and t["mint"] not in seen
            and re.fullmatch(S.B58, str(t["mint"])) and S.fails(t, p) == ["holders not checked"]]
    esc = lambda x: html.escape(str(x), quote=True)
    try:
        for t in todo[:5]:
            m = t["mint"]
            lines = ["⚠️ <b>Possible alert (unverified): %s</b>" % esc(str(t.get("symbol") or "?")[:20]),
                     esc(str(t.get("name") or "")[:40]), "",
                     "🛑 <b>RugCheck API failed to load.</b> Holders, liquidity lock and wallet checks were NOT done, so this has not been checked against all your limits.", "",
                     "📊 <b>Stats</b>",
                     "├ MC: %s" % money(t.get("market_cap_usd")), "├ Vol: %s (24h)" % money(t.get("volume_24h_usd")),
                     "├ LP: %s" % money(t.get("liquidity_usd")), "├ Vol/LP: %s" % ("%sx" % t["vol_to_liq"] if t.get("vol_to_liq") is not None else "n/a"),
                     "├ 1H: %s" % pct(t.get("change_1h_pct")), "└ Age: %s" % esc(t.get("age") or "n/a"), "",
                     '📈 <a href="https://dexscreener.com/solana/%s">DEX Screener</a> • <a href="https://gmgn.ai/sol/token/%s">GMGN</a>' % (m, m),
                     "<code>%s</code>" % m]
            telegram_send(token, chat, "\n".join(lines))
            sent.add(m)
        if todo:
            save(path, sorted(sent))
            print("Sent %d unverified Telegram alert(s) (RugCheck failed)." % min(len(todo), 5))
    except Exception as e:
        print("Telegram message failed (the scan itself is fine):", getattr(e, "reason", e))


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


def compact_pair(o):
    """DEX Screener's raw pair -> the compact object screen.py reads (the keys the Claude task used)."""
    t = o.get("txns") or {}
    info = o.get("info") or {}
    x = next((s.get("url") for s in (info.get("socials") or []) if s.get("type") == "twitter" and s.get("url")), None)
    return dict(mint=(o.get("baseToken") or {}).get("address"), sym=(o.get("baseToken") or {}).get("symbol"),
                name=(o.get("baseToken") or {}).get("name"), dex=o.get("dexId"), pair=o.get("pairAddress"),
                quote=(o.get("quoteToken") or {}).get("symbol"), liq=(o.get("liquidity") or {}).get("usd"),
                mc=o.get("marketCap"), v24=(o.get("volume") or {}).get("h24"), v1=(o.get("volume") or {}).get("h1"),
                b24=(t.get("h24") or {}).get("buys"), s24=(t.get("h24") or {}).get("sells"),
                b1=(t.get("h1") or {}).get("buys"), s1=(t.get("h1") or {}).get("sells"),
                pc24=(o.get("priceChange") or {}).get("h24"), pc1=(o.get("priceChange") or {}).get("h1"),
                created=o.get("pairCreatedAt"), ts=None, boosts=(o.get("boosts") or {}).get("active") or 0, x=x)


def compact_report(r):
    known = r.get("knownAccounts") or {}
    tok = r.get("token") or {}
    return dict(mint=r.get("mint"), totalHolders=r.get("totalHolders"), totalMarketLiquidity=r.get("totalMarketLiquidity"),
                score_normalised=r.get("score_normalised"), rugged=r.get("rugged"), mintAuthority=r.get("mintAuthority") or tok.get("mintAuthority"),
                freezeAuthority=r.get("freezeAuthority") or tok.get("freezeAuthority"), supply=tok.get("supply"),
                creator=r.get("creator"), creatorBalance=r.get("creatorBalance"), transferFeePct=(r.get("transferFee") or {}).get("pct"),
                detectedAt=r.get("detectedAt"), graphInsidersDetected=r.get("graphInsidersDetected"),
                risks=[[x.get("name"), x.get("value"), x.get("level")] for x in (r.get("risks") or [])],
                topHolders=[[h.get("owner"), h.get("pct"), (known.get(h.get("owner")) or {}).get("name"),
                             (known.get(h.get("owner")) or {}).get("type")] for h in (r.get("topHolders") or [])[:12]],
                markets=[[m.get("marketType"), m.get("pubkey"), (m.get("lp") or {}).get("lpLockedPct"), (m.get("lp") or {}).get("lpLockedUSD")]
                         for m in (r.get("markets") or [])[:6]])


def fetch_inputs(run, as_of, p, prev_alerts, raw):
    """Does what the Claude task does with WebFetch, but directly. Returns a list of problems to report."""
    inp = os.path.join(run, "in")
    os.makedirs(inp, exist_ok=True)
    problems, lists = [], {}
    for name, url in zip(("profiles", "boosts_latest", "boosts_top"), PROFILES):
        try:
            data = get_json(url)
            lists[name] = data
            lines = [x["tokenAddress"] for x in (data if isinstance(data, list) else [data]) if x.get("chainId") == "solana"]
            open(os.path.join(inp, "candidates_%s.txt" % name), "w", encoding="utf-8").write("\n".join(lines) + "\n")
        except Exception as e:
            problems.append("candidate list %s failed (%s)" % (name, e))
        time.sleep(PAUSE)
    save(os.path.join(raw, "candidate_lists.json"), lists)
    cands = S.candidates(run)
    mints = list(cands) + [m for m in S.tracked(prev_alerts, as_of) if m not in cands]
    pairs, n = [], 0
    for i in range(0, len(mints), 30):
        try:
            got = get_json(S.PRICE_URL % ",".join(mints[i:i + 30]))
        except Exception as e:
            problems.append("market data request failed (%s)" % e)
            continue
        pairs += got if isinstance(got, list) else []
        time.sleep(PAUSE)
    pairs = [o for o in pairs if o.get("chainId") == "solana"]
    save(os.path.join(raw, "market_pairs.json"), pairs)
    save(os.path.join(inp, "market_01.json"), [compact_pair(o) for o in pairs])
    mk = S.market(run, as_of)
    need = sorted([m for m, o in mk.items() if S.passes_market(o, p, as_of)], key=lambda m: -(S.num(mk[m].get("liq")) or 0))[:S.MAX_REPORTS]
    reports = {}
    for m in need:
        try:
            r = get_json(REPORT_URL % m)
            reports[m] = r
            n += 1
            save(os.path.join(inp, "report_%02d.json" % n), compact_report(r))
        except Exception as e:
            problems.append("RugCheck report for %s failed (%s)" % (m, e))
            RUGCHECK_FAILED.add(m)
        time.sleep(PAUSE)
    save(os.path.join(raw, "rugcheck_reports.json"), reports)
    return problems, len(cands), len(mints), len(pairs), n


def fetch_profiles(run, as_of, wallets):
    inp = os.path.join(run, "in")
    for k, w in enumerate(wallets[:S.MAX_PROFILE_LOOKUPS], 1):
        try:
            d = get_json(USER_URL % w, tries=1)
            rec = {"address": w, "username": d.get("username"), "followers": d.get("followers")}
        except Exception:
            rec = {"address": w, "username": None, "followers": None}   # no profile: remembered so it is not asked again
        save(os.path.join(inp, "profile_%02d.json" % k), rec)
        time.sleep(PAUSE)


def run_cmd(folder):
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(folder, exist_ok=True)
    cur = os.path.join(folder, "current")
    os.makedirs(cur, exist_ok=True)
    pj = os.path.join(folder, "params.json")
    if not os.path.exists(pj):
        shutil.copy(os.path.join(HERE, "..", "dashboard", "params.json"), pj)
    seed = os.path.join(HERE, "..", "data", "alerts.json")
    if not os.path.exists(os.path.join(cur, "alerts.json")) and os.path.exists(seed):
        shutil.copy(seed, os.path.join(cur, "alerts.json"))   # first run: carry on from the alert history saved in this repository
        print("First run: starting from the saved alert history in data/alerts.json (%d alerts)." % len(json.load(open(seed, encoding="utf-8"))))
    # a scratch run folder in the format screen.py expects, rebuilt every time from the saved state
    run = os.path.join(folder, "work")
    shutil.rmtree(run, ignore_errors=True)
    os.makedirs(os.path.join(run, "in"))
    shutil.copy(pj, os.path.join(run, "params.json"))
    for f, dest in (("alerts.json", "prev_alerts.json"), ("profiles_doc.json", "prev_profiles.json")):
        if os.path.exists(os.path.join(cur, f)):
            shutil.copy(os.path.join(cur, f), os.path.join(run, dest))
    open(os.path.join(run, "as_of.txt"), "w", encoding="utf-8").write(S.iso(now.timestamp()))
    as_of = now.timestamp()
    p = S.get_params(run)
    prev_alerts = S.read_json(os.path.join(run, "prev_alerts.json"), [])
    raw = os.path.join(folder, "raw", stamp)

    REFUSED.clear()
    RUGCHECK_FAILED.clear()
    UNREACHABLE.clear()
    problems, ncand, ntrack, npairs, nrep = fetch_inputs(run, as_of, p, prev_alerts, raw)
    rate_notice(folder, bool(npairs))
    if not npairs:
        write_data_js(folder)
        raise SystemExit("No market data came back (%s). Nothing was changed." % "; ".join(problems or ["no reason given"]))

    def screen_cmd(*args):
        sys.argv = ["screen.py"] + list(args)
        S.main()

    # wallet profile names for big holders of alerted coins, then build again so they show up
    want = re.findall(r"users/(%s)\?z=" % S.B58, _capture(lambda: screen_cmd("build", run)))
    if want:
        fetch_profiles(run, as_of, want)
        screen_cmd("build", run)
    screen_cmd("images", run)
    screen_cmd("reprice", run)

    out = os.path.join(run, "out")
    for f in ("tokens.json", "alerts.json", "profiles_doc.json"):
        shutil.copy(os.path.join(out, f), os.path.join(cur, f))
    tokens, alerts = json.load(open(os.path.join(cur, "tokens.json"), encoding="utf-8")), json.load(open(os.path.join(cur, "alerts.json"), encoding="utf-8"))
    summary = open(os.path.join(out, "summary.txt"), encoding="utf-8").read()
    # history: what the data looked like after this run (thumbnails left out, they are big and never change)
    h = os.path.join(folder, "history", stamp)
    save(os.path.join(h, "tokens.json"), tokens)
    save(os.path.join(h, "alerts.json"), [{k: v for k, v in a.items() if k != "image"} for a in alerts])
    shutil.copytree(os.path.join(run, "in"), os.path.join(raw, "compact_inputs"), dirs_exist_ok=True)
    write_logs(folder, stamp, tokens, alerts, summary, now, (ncand, ntrack, npairs, nrep), problems)
    write_data_js(folder)
    write_page(folder)
    notify_new_alerts(folder, prev_alerts, alerts)
    notify_unverified(folder, p, prev_alerts, tokens)
    shutil.rmtree(run, ignore_errors=True)
    print("Fetched %d candidates (+%d earlier alerts), %d market pairs, %d RugCheck reports." % (ncand, ntrack - ncand, npairs, nrep))
    for x in problems[:3]:
        print("Problem:", x[:200])
    if len(problems) > 3:
        print("...and %d more problems like these (all of them are in log/runs.csv)." % (len(problems) - 3))
    print("Saved to", os.path.abspath(folder), "- open dashboard.html in a browser.")


def reprice_cmd(folder):
    """Refresh only the market caps of tracked alerts (what the dashboard's Reload prices button does when served)."""
    now = dt.datetime.now(dt.timezone.utc)
    cur = os.path.join(folder, "current")
    if not os.path.exists(os.path.join(cur, "alerts.json")):
        raise RuntimeError("No saved scan yet. Press Run scan now first.")
    run = os.path.join(folder, "work_reprice")
    shutil.rmtree(run, ignore_errors=True)
    os.makedirs(os.path.join(run, "out"))
    try:
        shutil.copy(os.path.join(folder, "params.json"), os.path.join(run, "params.json"))
        shutil.copy(os.path.join(cur, "alerts.json"), os.path.join(run, "out", "alerts.json"))
        open(os.path.join(run, "as_of.txt"), "w", encoding="utf-8").write(S.iso(now.timestamp()))
        sys.argv = ["screen.py", "reprice", run]
        msg = _capture(S.main)
        shutil.copy(os.path.join(run, "out", "alerts.json"), os.path.join(cur, "alerts.json"))
        write_data_js(folder)
        return msg
    finally:
        shutil.rmtree(run, ignore_errors=True)


def _capture(fn):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


LOG_ALERT = ["run_at", "mint", "symbol", "first_alert_at", "first_alert_cap_usd", "latest_cap_usd", "change_since_first_pct", "peak_cap_usd",
             "trough_cap_usd", "max_drawdown_pct", "ath_cap_usd", "ath_gain_pct", "peak_gain_pct", "qualified_runs", "removed_at",
             "removed_reason", "top_traders"]
LOG_TOKEN = ["run_at", "mint", "symbol", "name", "venue", "quote", "age_hours", "market_cap_usd", "liquidity_usd", "volume_24h_usd",
             "vol_to_liq", "buy_share_pct", "change_1h_pct", "change_24h_pct", "holder_check", "holders", "top10_wallets_pct",
             "largest_wallet_pct", "creator_pct", "locked_lp_pct", "linked_wallets", "rugcheck_score", "flags", "labels"]


def append_csv(path, header, rows):
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new: w.writerow(header)
        for r in rows:
            w.writerow(["; ".join(v) if isinstance(v, list) else ("" if v is None else v) for v in r])


def write_logs(folder, stamp, tokens, alerts, summary, now, counts, problems):
    at = S.iso(now.timestamp())
    log = os.path.join(folder, "log")
    append_csv(os.path.join(log, "alerts_log.csv"), LOG_ALERT, [[at] + [a.get(k) for k in LOG_ALERT[1:]] for a in alerts])
    append_csv(os.path.join(log, "tokens_log.csv"), LOG_TOKEN, [[at] + [t.get(k) for k in LOG_TOKEN[1:]] for t in tokens])
    new = [l for l in summary.splitlines() if l.startswith("NEW ALERT")]
    if new:
        with open(os.path.join(log, "new_alerts.txt"), "a", encoding="utf-8") as f:
            f.write("".join("%s  %s\n" % (at, l) for l in new))
    append_csv(os.path.join(log, "runs.csv"), ["run_at", "candidates", "tracked", "market_pairs", "rugcheck_reports", "tokens_saved",
                                              "alerts_total", "new_alerts", "problems"],
               [[at, counts[0], counts[1], counts[2], counts[3], len(tokens), len(alerts), len(new), " | ".join(problems)[:400]]])


def write_data_js(folder):
    cur = os.path.join(folder, "current")
    d = {k: S.read_json(os.path.join(cur, k + ".json"), []) for k in ("tokens", "alerts")}   # a missing file (no finished run yet) shows as empty
    d["params"] = S.read_json(os.path.join(folder, "params.json"), {})
    d["written_at"] = S.iso(time.time())
    d["notice"] = S.read_json(os.path.join(cur, "status.json"), {})
    tmp = os.path.join(cur, "data.js.tmp")
    open(tmp, "w", encoding="utf-8").write("window.__SCREENER_DATA = " + json.dumps(d, ensure_ascii=False) + ";\n")
    os.replace(tmp, os.path.join(cur, "data.js"))


def write_page(folder):
    page = open(os.path.join(HERE, "..", "dashboard", "page.html"), encoding="utf-8").read()
    shell = open(os.path.join(HERE, "local_shell.html"), encoding="utf-8").read()
    d3 = open(os.path.join(HERE, "d3.min.js"), encoding="utf-8").read()
    i = page.rindex("<script>")
    html = shell.replace("/*D3*/", d3.replace("</script", "<\\/script")).replace("<!--PAGE-HTML-->", page[:i]).replace("/*PAGE-JS*/", page[i + 8:page.rindex("</script>")])
    open(os.path.join(folder, "dashboard.html"), "w", encoding="utf-8").write(html)


# ---------------------------------------------------------------- serve: dashboard with a Run button and saved limits
LIMIT_KEYS = {"max_age_days": (0, 365), "min_liq": (0, 1e12), "min_vol": (0, 1e12), "min_holders": (0, 1e9),
              "max_top10": (0, 100), "max_wallet": (0, 100), "min_locked": (0, 100)}


def clean_limits(o):
    """Check limits sent by the page. Returns (dict, error)."""
    out = {}
    if not isinstance(o, dict):
        return None, "Expected a set of limits."
    for k, (lo, hi) in LIMIT_KEYS.items():
        if k not in o:
            continue
        v = o[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
            return None, "%s must be a number between %g and %g." % (k, lo, hi)
        out[k] = int(v) if float(v).is_integer() else v
    if "no_danger" in o:
        if not isinstance(o["no_danger"], bool):
            return None, "no_danger must be on or off."
        out["no_danger"] = o["no_danger"]
    return out, None


def serve_cmd(folder, port=8765):
    import threading, subprocess, webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    folder = os.path.abspath(folder)
    os.makedirs(os.path.join(folder, "current"), exist_ok=True)
    pj = os.path.join(folder, "params.json")
    if not os.path.exists(pj):
        shutil.copy(os.path.join(HERE, "..", "dashboard", "params.json"), pj)
    write_data_js(folder)
    write_page(folder)
    lock = threading.Lock()
    reprice_lock = threading.Lock()
    st = {"running": False, "started_at": None, "finished_at": None, "ok": None, "tail": []}

    def do_run():
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", folder], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=env, encoding="utf-8", errors="replace")
            for line in proc.stdout:
                with lock:
                    st["tail"] = (st["tail"] + [line.rstrip()])[-12:]
            ok = proc.wait() == 0
        except Exception as e:
            ok = False
            with lock:
                st["tail"].append("Could not start the scan: %s" % e)
        with lock:
            st.update(running=False, ok=ok, finished_at=S.iso(time.time()))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            b = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _json(self, code, o):
            self._send(code, json.dumps(o))

        def _host_ok(self):   # only this computer's own page may talk to the server (blocks other websites from clicking Run)
            return self.headers.get("Host", "") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "Wrong host."})
            path = self.path.split("?")[0]
            if path in ("/", "/dashboard.html"):
                return self._send(200, open(os.path.join(folder, "dashboard.html"), "rb").read(), "text/html; charset=utf-8")
            if path == "/current/data.js":
                f = os.path.join(folder, "current", "data.js")
                if not os.path.exists(f):
                    return self._send(404, "", "text/plain")
                return self._send(200, open(f, "rb").read(), "text/javascript; charset=utf-8")
            if path == "/api/status":
                with lock:
                    return self._json(200, dict(st))
            if path == "/api/params":
                return self._json(200, S.read_json(pj, {}))
            self._send(404, "Not found", "text/plain")

        def do_POST(self):
            if not self._host_ok() or self.headers.get("Origin") not in (None, "http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
                return self._json(403, {"error": "Not allowed."})
            path = self.path.split("?")[0]
            n = int(self.headers.get("Content-Length") or 0)
            if n > 10000:
                return self._json(413, {"error": "Too large."})
            raw = self.rfile.read(n)
            if path == "/api/run":
                with lock:
                    if st["running"]:
                        return self._json(409, {"error": "A scan is already running."})
                    st.update(running=True, started_at=S.iso(time.time()), finished_at=None, ok=None, tail=[])
                threading.Thread(target=do_run, daemon=True).start()
                return self._json(200, {"started": True})
            if path == "/api/reprice":
                try:
                    with lock:
                        busy = st["running"]
                    if busy:
                        return self._json(409, {"ok": False, "message": "A scan is running; prices will update when it finishes."})
                    with reprice_lock:
                        msg = reprice_cmd(folder)
                    return self._json(200, {"ok": True, "message": msg})
                except Exception as e:
                    return self._json(500, {"ok": False, "message": str(e)})
            if path == "/api/params":
                try:
                    new, err = clean_limits(json.loads(raw.decode("utf-8")))
                except ValueError:
                    new, err = None, "Could not read the limits."
                if err:
                    return self._json(400, {"error": err})
                cur = S.read_json(pj, {})
                cur.update(new)
                tmp = pj + ".tmp"
                open(tmp, "w", encoding="utf-8").write(json.dumps(cur, indent=1, sort_keys=True) + "\n")
                os.replace(tmp, pj)
                write_data_js(folder)
                return self._json(200, cur)
            self._json(404, {"error": "Not found"})

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        raise SystemExit("Port %d is already in use. Is the screener already open in another window? Close it, or visit http://127.0.0.1:%d" % (port, port))
    url = "http://127.0.0.1:%d/" % port
    print("Screener dashboard is running at", url)
    print("Leave this window open while you use the page. Press Ctrl+C here to stop.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


def watch_cmd(folder, minutes):
    """Scan again and again, no browser needed. A failed scan is reported and the loop carries on."""
    minutes = max(5, minutes)
    if sys.platform == "darwin":   # Mac: stop idle sleep for as long as this window is running
        try: subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except Exception: pass
    print("Scanning every %d minutes. Leave this window open; press Ctrl+C to stop." % minutes)
    fails, warned = 0, False
    while True:
        print("\n--- scan at %s ---" % dt.datetime.now().strftime("%a %d %b %H:%M"))
        try:
            run_cmd(folder)
            fails, warned = 0, False
        except (Exception, SystemExit) as e:   # SystemExit: run_cmd uses it for "no market data" and certificate problems
            fails += 1
            print("Scan failed:", e)
            if fails >= 3 and not warned:
                warned = True
                token, chat = telegram_settings(folder)
                try:
                    if token and chat:
                        telegram_send(token, chat, "⚠️ The screener has failed %d scans in a row. Last error: %s" % (fails, html.escape(str(e))[:200]))
                except Exception:
                    pass
        time.sleep(minutes * 60)


def main():
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to a narrow encoding that chokes on token names
        try: stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass
    if len(sys.argv) < 2 or sys.argv[1] not in ("run", "page", "serve", "watch"):
        raise SystemExit(__doc__)
    if sys.argv[1] == "watch":   # watch [minutes] [folder]
        mins = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 10
        rest = [a for a in sys.argv[2:] if not a.isdigit()]
        return watch_cmd(rest[0] if rest else os.path.join(HERE, "..", "screener_data"), mins)
    # default: a screener_data folder next to the screener folder, wherever the command is typed
    folder = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "..", "screener_data")
    if sys.argv[1] == "page":
        write_data_js(folder) if os.path.exists(os.path.join(folder, "current", "alerts.json")) else None
        write_page(folder)
        print("Wrote", os.path.join(folder, "dashboard.html"))
    elif sys.argv[1] == "serve":
        serve_cmd(folder)
    else:
        run_cmd(folder)


if __name__ == "__main__":
    main()
