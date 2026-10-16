#!/usr/bin/env python3
"""Run the whole Solana launch screener from your own computer, with no Claude and no usage credits.

  python3 local.py run   [FOLDER]   fetch, screen, update and save everything (default folder: ./screener_data)
  python3 local.py page  [FOLDER]   rewrite FOLDER/dashboard.html (done automatically by run)

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
import sys, os, re, json, csv, time, shutil, ssl, datetime as dt, urllib.request, urllib.error

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


def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as f:
                return json.load(f)
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code in (400, 404):
                break          # nothing there; asking again will not help
        except Exception as e:
            last = str(getattr(e, "reason", e))
        time.sleep(1.5 * (i + 1))
    if last and "CERTIFICATE_VERIFY_FAILED" in last:
        raise SystemExit(SSL_HELP)
    raise RuntimeError("%s: %s" % (url.split("?")[0], last))


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

    problems, ncand, ntrack, npairs, nrep = fetch_inputs(run, as_of, p, prev_alerts, raw)
    if not npairs:
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
    shutil.rmtree(run, ignore_errors=True)
    print("Fetched %d candidates (+%d earlier alerts), %d market pairs, %d RugCheck reports." % (ncand, ntrack - ncand, npairs, nrep))
    for x in problems:
        print("Problem:", x)
    print("Saved to", os.path.abspath(folder), "- open dashboard.html in a browser.")


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
               [[at, counts[0], counts[1], counts[2], counts[3], len(tokens), len(alerts), len(new), " | ".join(problems)]])


def write_data_js(folder):
    cur = os.path.join(folder, "current")
    d = {k: json.load(open(os.path.join(cur, k + ".json"), encoding="utf-8")) for k in ("tokens", "alerts")}
    d["params"] = S.read_json(os.path.join(folder, "params.json"), {})
    d["written_at"] = S.iso(time.time())
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


def main():
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to a narrow encoding that chokes on token names
        try: stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass
    if len(sys.argv) < 2 or sys.argv[1] not in ("run", "page"):
        raise SystemExit(__doc__)
    # default: a screener_data folder next to the screener folder, wherever the command is typed
    folder = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "..", "screener_data")
    if sys.argv[1] == "page":
        write_data_js(folder) if os.path.exists(os.path.join(folder, "current", "alerts.json")) else None
        write_page(folder)
        print("Wrote", os.path.join(folder, "dashboard.html"))
    else:
        run_cmd(folder)


if __name__ == "__main__":
    main()
