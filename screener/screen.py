#!/usr/bin/env python3
"""Solana launch screener: turns saved lookups into the dashboard's data files.

  screen.py urls-market  RUN   after the three candidate lists are saved: prints the market-data URLs to fetch
  screen.py urls-reports RUN   after market data is saved: prints the RugCheck report URLs to fetch
  screen.py build        RUN   after reports are saved: writes RUN/out/*.json, prints profile URLs still needed
                               and the alert summary. Safe to run again after saving profiles.
  screen.py images       RUN   after the last build: downloads token thumbnails for the alerts into RUN/out/alerts.json.
                               The one command that goes online itself; if the image site cannot be reached it
                               keeps the thumbnails earlier runs saved and says so.
  screen.py traders      RUN   marks tokens and alerts in RUN/out held by wallets labelled TOP TRADER in the wallet label
                               database (RUN/labels/auto/*.json and RUN/labels/labels/*.json, as ArtifactData saves them),
                               from pump.fun's top-50 holder list per coin (goes online itself). Used by the :45 top-up.
  screen.py reprice      RUN   updates the market cap of every tracked alert in RUN/out/alerts.json straight from
                               DEX Screener (also goes online itself). Used by the :45 top-up between hourly runs.

RUN/ holds
  params.json          the saved strategy (the dashboard's dash/params document)
  as_of.txt            ISO UTC time the market data was pulled (written by urls-market if missing)
  prev_alerts.json     the dashboard's current alerts file (optional)
  prev_profiles.json   the dashboard's screener/profiles document (optional)
  in/candidates_profiles.txt, in/candidates_boosts_latest.txt, in/candidates_boosts_top.txt   lines: <address> <timestamp>
  in/market_*.json     compact arrays, one object per pair (key x: the X link DEX Screener lists for the token, if any)
  in/report_*.json     one compact object per token
  in/profile_*.json    one compact object per wallet
"""
import sys, os, re, json, glob, random, string, base64, time, urllib.request, urllib.error, datetime as dt

B58 = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
X_URL = re.compile(r"https://(?:x|twitter)\.com/[A-Za-z0-9_./?=&%+\-]+")   # only plain X links are kept


def x_kind(url):
    if not url: return ""
    if "/status/" in url: return "X post"
    if "/i/communities/" in url: return "X community"
    return "X account"
HOLDER_CHECK_MIN_LIQ = 10000      # no holder checks below this much pooled liquidity
TRACK_DAYS = 14                   # keep re-pricing an alerted token for this long
MAX_REPORTS = 25                  # holder reports per run, deepest pools first
MAX_PROFILE_LOOKUPS = 20          # new wallet lookups per run
MIN_LOOKUP_PCT = 1.0              # only look up wallets holding at least this share of supply
SAME_AMOUNT_DROP = 5              # Blake's rule (11 Oct 2026): if this many top wallets hold the same amount
                                  # (within 3% of each other), the coin is dropped from tokens and alerts entirely
COIN_URL = "https://frontend-api-v3.pump.fun/coins-v2/%s"   # carries the coin's all-time-high market cap (ath_market_cap, USD)
CANDLES_URL = "https://swap-api.pump.fun/v2/coins/%s/candles?interval=%s&limit=1000&currency=USD&createdTs=0"   # newest 1000 candles
CANDLE_SUPPLY = 1e9               # USD price x 1 billion = market cap (checked against the caps the screener recorded)
HOLDERS_URL = "https://frontend-api-v3.pump.fun/coins/top-holders/%s"   # a coin's top 50 holders
TRADER_CATEGORY = "TOP TRADER"
PRICE_URL = "https://api.dexscreener.com/tokens/v1/solana/%s"   # up to 30 mints, comma separated
IMG_URL = "https://dd.dexscreener.com/ds-data/tokens/solana/%s.png"   # redirects to a 64 px thumbnail on cdn.dexscreener.com
IMG_MAX_BYTES = 12000             # larger answers are skipped, not shrunk
IMG_PER_RUN = 40                  # downloads per run
IMG_RETRY_HOURS = 6               # wait this long before asking again for a token that had no image
RAYDIUM_AUTH = {"GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL": "Raydium CPMM pool",
                "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium pool"}
VENUES = {"pumpfun": "Pump.fun bonding curve", "pumpswap": "PumpSwap", "raydium": "Raydium",
          "meteoradbc": "Meteora bonding curve", "meteora": "Meteora", "orca": "Orca"}
DEFAULTS = dict(max_age_days=7, min_liq=10000, min_vol=10000, min_holders=100, max_top10=30,
                max_wallet=10, min_locked=90, no_danger=True)


def loose_json(path):
    """Read JSON that may be wrapped in a code fence or followed by stray text."""
    s = open(path, encoding="utf-8").read()
    s = re.sub(r"```(?:json)?", "", s).strip()
    starts = [i for i in (s.find("["), s.find("{")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in " + path)
    return json.JSONDecoder().raw_decode(s[min(starts):])[0]


def read_json(path, default):
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else default


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(s):
    s = re.sub(r"\.\d+", "", s.strip()).replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s).timestamp()


def num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def get_as_of(run, create=False):
    p = os.path.join(run, "as_of.txt")
    if not os.path.exists(p):
        if not create:
            raise SystemExit("as_of.txt is missing: run urls-market first")
        open(p, "w", encoding="utf-8").write(iso(dt.datetime.now(dt.timezone.utc).timestamp()))
    return epoch(open(p, encoding="utf-8").read())


def get_params(run):
    p = dict(DEFAULTS)
    p.update({k: v for k, v in read_json(os.path.join(run, "params.json"), {}).items() if k in DEFAULTS})
    return p


def candidates(run):
    out = {}
    for name, via in (("profiles", "New profile"), ("boosts_latest", "Paid boost"), ("boosts_top", "Paid boost"), ("stonkfun", "StonkFun")):
        path = os.path.join(run, "in", "candidates_%s.txt" % name)
        if not os.path.exists(path):
            if name != "stonkfun":      # only local.py fetches the StonkFun list; the Claude tasks never do
                print("WARNING: missing", path)
            continue
        for m in re.finditer(r"(?m)^\s*(%s)\b" % B58, open(path, encoding="utf-8").read()):
            out.setdefault(m.group(1), via)
    return out


DOWN_REMOVE_PCT = -90             # Blake's rules (11 Oct 2026): a token leaves the screener when it is down 90% or more
MIN_CAP_USD = 10000               # since its first alert or from its all-time high, or its market cap falls under $10K


VOL_LIQ_MAX = 20                  # Blake's trial labels (11 Oct 2026), shown but never used to hide or block:
BUY_SHARE_BAND = (45, 60)         # 24h volume over 20x liquidity, buys outside 45-60% of trades, first trigger only
SNAPSHOT_FIELDS = ("age_hours", "market_cap_usd", "liquidity_usd", "volume_24h_usd", "volume_1h_usd", "vol_to_liq",
                   "buys_24h", "sells_24h", "buy_share_pct", "change_1h_pct", "change_24h_pct", "holders", "top10_pct",
                   "top10_wallets_pct", "largest_wallet_pct", "creator_pct", "locked_lp_pct", "linked_wallets",
                   "lookalike_wallets", "rugcheck_score", "flags", "found_via", "boosts", "venue", "x_kind")


def trial_labels(r):
    """Warning labels from one set of measurements (a token row or a first-alert snapshot)."""
    out = []
    vl, bs = r.get("vol_to_liq"), r.get("buy_share_pct")
    if vl is not None and vl > VOL_LIQ_MAX:
        out.append("Volume %gx liquidity" % round(vl))
    if bs is not None and not BUY_SHARE_BAND[0] <= bs <= BUY_SHARE_BAND[1]:
        out.append("Buys %g%% of trades" % round(bs))
    return out


UP_SAVE_PCT = 5                   # Blake's rule (12 Oct 2026): a token that has ever been this far above its first-alert cap is
MAX_ALERT_ROWS = 200              # saved for good: never dropped by the row cap or the same-amount rule, kept even when removed


def peak_gain(a):
    """Highest gain over the first-alert market cap, in percent, or None."""
    f, p = a.get("first_alert_cap_usd"), a.get("peak_cap_usd")
    return None if not f or not p else round(100.0 * (p / f - 1), 1)


def kept(a):
    """Saved for good: noticed up 5%+ before, or its recorded peak is. A peak never falls, so this never reverses."""
    return bool(a.get("went_up_5_at")) or (peak_gain(a) or 0) >= UP_SAVE_PCT


def mark_winner(a, now):
    """Records the peak gain and, the first time it reaches UP_SAVE_PCT, when the screener noticed. Never unset."""
    if a["mint"].endswith("pump") and a.get("ath_at") and a.get("ath_cap_usd") and a["ath_at"] >= a["first_alert_at"]:
        # pump.fun measures its coins the same way DEX Screener does, so a high set after the alert is a real peak, even
        # when it fell between two of the screener's own price checks (it can be 2-3x what the :24/:45 checks saw)
        a["peak_cap_usd"] = round(max(a.get("peak_cap_usd") or 0, a["ath_cap_usd"]), 2)
    g = peak_gain(a)
    if g is None:
        return
    a["peak_gain_pct"] = g
    if g >= UP_SAVE_PCT and not a.get("went_up_5_at"):
        a["went_up_5_at"] = now


def derive_extremes(a):
    """Lowest cap since the alert (so the max drawdown from the alert) and, when the all-time high came after the alert,
    its gain over the first-alert cap. Blake, 12 Oct: show both beside the cap since first alert."""
    f = a.get("first_alert_cap_usd")
    if not f:
        return
    seen = [x for x in (a.get("trough_cap_usd"), a.get("latest_cap_usd"), f) if x]
    a["trough_cap_usd"] = round(min(seen), 2)                    # never above the alert cap, so the drawdown is 0 or negative
    a["max_drawdown_pct"] = round(100.0 * (a["trough_cap_usd"] / f - 1), 1)
    ath, peak = a.get("ath_cap_usd"), a.get("peak_cap_usd") or 0
    if ath:
        ath = max(ath, peak)
        a["ath_cap_usd"] = round(ath, 2)
        came_after = bool(a.get("ath_at")) and a["ath_at"] >= a["first_alert_at"]
        # the high was set before the alert when pump.fun dates it earlier and nothing since the alert came close
        a["ath_gain_pct"] = round(100.0 * (ath / f - 1), 1) if (came_after or peak >= 0.98 * ath) else None


def removal_reason(a):
    """Why an alert has left the screener, or '' if it has not. Once removed it is never tracked again."""
    if a.get("removed_at"):
        return a.get("removed_reason") or "removed earlier"
    ch, cap, ath = a.get("change_since_first_pct"), a.get("latest_cap_usd"), a.get("ath_cap_usd")
    if ch is not None and ch <= DOWN_REMOVE_PCT:
        return "down 90% or more since first alert"
    if cap is not None and cap < MIN_CAP_USD:
        return "market cap under $10K"
    if ath and cap is not None and cap <= ath * (1 + DOWN_REMOVE_PCT / 100.0):
        return "down 90% or more from all-time high"
    return ""


def removed(a):
    return bool(removal_reason(a))


def tracked(prev_alerts, as_of):
    return [a["mint"] for a in prev_alerts if as_of - epoch(a["first_alert_at"]) <= TRACK_DAYS * 86400 and not removed(a)]


def buster():
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))


def market(run, as_of):
    """Best pair per mint from every in/market_*.json."""
    best, stale, xs = {}, 0, {}
    for path in sorted(glob.glob(os.path.join(run, "in", "market_*.json"))):
        for o in loose_json(path):
            if not isinstance(o, dict) or not re.fullmatch(B58, str(o.get("mint", ""))):
                continue
            if isinstance(o.get("x"), str) and X_URL.fullmatch(o["x"].strip()):
                xs.setdefault(o["mint"], o["x"].strip())
            if o.get("mc") is None and o.get("liq") is None and o.get("created") is None:
                continue  # carries a link only, not market data
            if o.get("ts") and as_of - float(o["ts"]) / 1000 > 2700:
                stale += 1
            key = (num(o.get("liq")) or -1, num(o.get("v24")) or 0)
            if o["mint"] not in best or key > best[o["mint"]][0]:
                best[o["mint"]] = (key, o)
    if stale:
        print("WARNING: %d pair(s) carry a timestamp more than 45 minutes before as_of: the fetch served an old copy. "
              "Re-fetch those URLs with a new ?z= value." % stale)
    out = {k: v[1] for k, v in best.items()}
    for k, o in out.items():
        o["x"] = xs.get(k)
    return out


def passes_market(o, p, as_of):
    liq, v24 = num(o.get("liq")), num(o.get("v24")) or 0
    age_h = (as_of - float(o["created"]) / 1000) / 3600 if o.get("created") else 0
    return (liq is not None and liq >= max(HOLDER_CHECK_MIN_LIQ, p["min_liq"])
            and v24 >= p["min_vol"] and age_h <= p["max_age_days"] * 24)


def fails(r, p):
    f = []
    if r["age_hours"] > p["max_age_days"] * 24: f.append("age")
    if r["liquidity_usd"] is None or r["liquidity_usd"] < p["min_liq"]: f.append("liquidity")
    if (r["volume_24h_usd"] or 0) < p["min_vol"]: f.append("24h volume")
    if r["holder_check"] != "Checked":
        return f + ["holders not checked"]
    if r["holders"] < p["min_holders"]: f.append("holder count")
    if r["top10_wallets_pct"] > p["max_top10"]: f.append("top-10 wallets")
    if r["largest_wallet_pct"] > p["max_wallet"]: f.append("largest wallet")
    if r["locked_lp_pct"] is None or r["locked_lp_pct"] < p["min_locked"]: f.append("locked liquidity")
    if p["no_danger"] and r["danger"]: f.append("danger flag")
    if r.get("market_cap_usd") is not None and r["market_cap_usd"] < MIN_CAP_USD: f.append("market cap under $10K")
    if r.get("identity_checks") == "No wallet holds 1% or more": f.append("no wallet at 1%")   # Blake's rule, 11 Oct 2026
    return f


def lookalikes(vals):
    vals, best = sorted((v for v in vals if v > 0), reverse=True), 1
    for i in range(len(vals)):
        j = i
        while j + 1 < len(vals) and vals[i] / vals[j + 1] <= 1.03:
            j += 1
        best = max(best, j - i + 1)
    return best if vals else 0


def holders_of(rep, pair):
    """[(owner, pct, kind, label)] for the report's top holders, largest first."""
    out = []
    creator = rep.get("creator")
    for h in rep.get("topHolders") or []:
        owner, pct = h[0], num(h[1]) or 0.0
        name = h[2] if len(h) > 2 and isinstance(h[2], str) else None
        typ = (h[3] if len(h) > 3 and isinstance(h[3], str) else "") or ""
        low = (name or "").lower()
        if owner == pair:
            kind, label = "pool", name or "Liquidity pool"
        elif owner in RAYDIUM_AUTH:
            kind, label = "pool", name or RAYDIUM_AUTH[owner]
        elif typ.upper() in ("AMM", "CLOB") or any(w in low for w in ("amm", " pool", "position", "market")) and "stake" not in low:
            kind, label = "pool", name
        elif typ.upper() == "LOCKER" or any(w in low for w in ("vault", "locker", "stake pool")):
            kind, label = "locker", name
        elif typ.upper() == "CREATOR" or (creator and owner == creator):
            kind, label = "creator", "Creator"
        else:
            kind, label = None, None
        out.append((owner, pct, kind, label))
    return sorted(out, key=lambda t: -t[1])


def build_rows(run, p, as_of, prev_alerts, profiles):
    cands = candidates(run)
    mk = market(run, as_of)
    reports = {}
    for path in sorted(glob.glob(os.path.join(run, "in", "report_*.json"))):
        try:
            o = loose_json(path)
        except ValueError as e:
            print("WARNING:", e)
            continue
        if isinstance(o, dict) and o.get("mint") and o.get("topHolders"):
            reports[o["mint"]] = o
    track = set(tracked(prev_alerts, as_of))
    rows, lookups, dropped = [], [], []
    gone = {a["mint"] for a in prev_alerts if removed(a)}
    for mint in [m for m in cands if m not in gone] + [m for m in track if m not in cands]:
        o = mk.get(mint)
        if not o:
            continue
        liq, mc, v24, v1 = num(o.get("liq")), num(o.get("mc")), num(o.get("v24")) or 0, num(o.get("v1")) or 0
        b24, s24 = int(num(o.get("b24")) or 0), int(num(o.get("s24")) or 0)
        first_seen = float(o["created"]) / 1000 if o.get("created") else as_of
        rep = reports.get(mint)
        if rep and rep.get("detectedAt"):
            try:
                first_seen = min(first_seen, epoch(rep["detectedAt"]))
            except ValueError:
                pass
        age_h = max(0.0, (as_of - first_seen) / 3600)
        r = dict(
            mint=mint, symbol=str(o.get("sym") or "?"), name=str(o.get("name") or ""),
            venue=VENUES.get(o.get("dex"), str(o.get("dex") or "")), quote=str(o.get("quote") or ""),
            found_via=cands.get(mint, "Earlier alert"), boosts=int(num(o.get("boosts")) or 0),
            age=("%.1f h" % age_h) if age_h < 48 else ("%.1f d" % (age_h / 24)), age_hours=round(age_h, 1),
            liquidity_usd=liq, market_cap_usd=mc, volume_24h_usd=v24, volume_1h_usd=v1,
            vol_to_liq=round(v24 / liq, 1) if liq else None, buys_24h=b24, sells_24h=s24,
            buy_share_pct=round(100.0 * b24 / (b24 + s24), 1) if (b24 + s24) else None,
            change_24h_pct=num(o.get("pc24")), change_1h_pct=num(o.get("pc1")),
            holder_check="Checked" if rep else "Not checked",
            holders=None, top10_pct=None, top10_wallets_pct=None, largest_wallet_pct=None, creator_pct=None,
            locked_lp_pct=None, linked_wallets=None, lookalike_wallets=None, rugcheck_score=None, danger=None,
            flags="", identified_holders="", identity_checks="",
            dexscreener_url="https://dexscreener.com/solana/" + mint,
            pumpfun_url=("https://pump.fun/coin/" + mint) if (mint.endswith("pump") or o.get("dex") in ("pumpfun", "pumpswap")) else "",
            x_url=o.get("x") or "", x_kind=x_kind(o.get("x")), as_of=iso(as_of),
        )
        if rep:
            hs = holders_of(rep, o.get("pair"))
            top10 = hs[:10]
            wallets10 = [t for t in top10 if t[2] not in ("pool", "locker")]
            nonpool = [t for t in hs if t[2] not in ("pool", "locker")]
            sup, cb = num(rep.get("supply")), num(rep.get("creatorBalance")) or 0
            creator_pct = 100.0 * cb / sup if sup else 0.0
            look = lookalikes([t[1] for t in nonpool])
            if look >= SAME_AMOUNT_DROP:
                dropped.append(dict(mint=mint, symbol=r["symbol"], wallets=look))
                continue
            risks = rep.get("risks") or []
            mint_auth, freeze_auth = rep.get("mintAuthority"), rep.get("freezeAuthority")
            fee = num(rep.get("transferFeePct")) or 0
            danger = bool(rep.get("rugged")) or bool(mint_auth) or bool(freeze_auth) or any(
                len(x) > 2 and str(x[2]).lower() == "danger" for x in risks)
            flags = [(str(x[0]) + (" " + str(x[1]) if len(x) > 1 and x[1] else "")).strip() for x in risks]
            if mint_auth: flags.append("Mint authority still active")
            if freeze_auth: flags.append("Freeze authority still active")
            if fee > 0: flags.append("Transfer fee %g%%" % fee)
            if rep.get("rugged"): flags.append("Marked as rugged")
            if creator_pct >= 5: flags.append("Creator holds %.1f%%" % creator_pct)
            pools = [t for t in hs if t[2] == "pool"]
            main = next((t for t in pools if t[0] == o.get("pair") or t[0] in RAYDIUM_AUTH), pools[0] if pools else None)
            for t in pools:
                if t is not main and t[1] >= 5:
                    flags.append("%.1f%% of supply sits in a %s" % (t[1], t[3] or "second pool"))
            ins = int(num(rep.get("graphInsidersDetected")) or 0)
            if ins >= 5: flags.append("%d linked wallets (RugCheck)" % ins)   # Blake: shown in red from 5
            tml = num(rep.get("totalMarketLiquidity"))
            locked = None
            if tml:
                locked = min(100.0, 100.0 * sum(num(m[3]) or 0 for m in (rep.get("markets") or []) if len(m) > 3) / tml)
            ident, wallets, checked = [], [t for t in nonpool if t[1] >= MIN_LOOKUP_PCT][:5], 0
            for owner, pct, kind, label in hs:
                prof = profiles.get(owner)
                if kind in ("pool", "locker"):
                    ident.append("%s %.1f%%" % (label or kind.title(), pct))
                elif kind == "creator":
                    nm = prof.get("username") if prof else None
                    ident.append("Creator%s %.1f%%" % ((" (" + nm + ")") if nm else "", pct))
                elif prof and prof.get("username"):
                    ident.append("%s %.1f%%" % (prof["username"], pct))
            for owner, pct, kind, label in wallets:
                if owner in profiles: checked += 1
                else: lookups.append((mint, owner, pct))
            r.update(
                holders=int(num(rep.get("totalHolders")) or 0),
                top10_pct=round(sum(t[1] for t in top10), 1),
                top10_wallets_pct=round(sum(t[1] for t in wallets10), 1),
                largest_wallet_pct=round(max([t[1] for t in nonpool] or [0]), 1),
                creator_pct=round(creator_pct, 1), locked_lp_pct=None if locked is None else round(locked, 1),
                linked_wallets=ins, lookalike_wallets=look, rugcheck_score=num(rep.get("score_normalised")),
                danger=danger, flags="; ".join(flags), identified_holders=" · ".join(ident[:8]),
                identity_checks=("%d of %d top wallets looked up" % (checked, len(wallets))) if wallets else "No wallet holds 1% or more",
            )
        r["labels"] = trial_labels(r)
        rows.append(r)
    return rows, lookups, dropped


def update_alerts(prev, rows, p, as_of, dropped=()):
    """One row per token that has ever met the strategy, minus coins dropped by the same-amount rule."""
    gone = {d["mint"] for d in dropped}
    by = {a["mint"]: dict(a) for a in prev if a["mint"] not in gone or kept(a)}
    now = iso(as_of)
    new = []
    seen = {r["mint"] for r in rows}
    for a in by.values():
        if a["mint"] not in seen:
            a["now"] = "Not re-priced in the last run"
    for r in rows:
        f = fails(r, p)
        a = by.get(r["mint"])
        if a is not None and a.get("removed_at"):
            continue
        if not f and a is None and r["market_cap_usd"]:
            a = by[r["mint"]] = dict(mint=r["mint"], first_alert_at=now, first_alert_cap_usd=r["market_cap_usd"],
                                      first_alert_age=r["age"], triggers=0, latest_alert_at=now,
                                      peak_cap_usd=r["market_cap_usd"],
                                      first_alert_snapshot={k: r.get(k) for k in SNAPSHOT_FIELDS},
                                      first_alert_labels=trial_labels(r))
            new.append(r)
        if a is None:
            continue
        a.update(symbol=r["symbol"], name=r["name"], venue=r["venue"], quote=r.get("quote", ""),
                 dexscreener_url=r.get("dexscreener_url", ""), pumpfun_url=r.get("pumpfun_url", ""))
        if r.get("x_url"):
            a.update(x_url=r["x_url"], x_kind=r["x_kind"])
        if r["market_cap_usd"]:
            a.update(latest_cap_usd=r["market_cap_usd"], latest_cap_at=now,
                     peak_cap_usd=max(a.get("peak_cap_usd") or 0, r["market_cap_usd"]),
                     change_since_first_pct=round(100.0 * (r["market_cap_usd"] / a["first_alert_cap_usd"] - 1), 1))
        a.update(liquidity_usd=r["liquidity_usd"], volume_24h_usd=r["volume_24h_usd"], age=r["age"])
        if r["holder_check"] == "Checked":
            a.update(holders=r["holders"], top10_wallets_pct=r["top10_wallets_pct"], largest_wallet_pct=r["largest_wallet_pct"],
                     locked_lp_pct=r["locked_lp_pct"], flags=r["flags"], identified_holders=r["identified_holders"],
                     identity_checks=r["identity_checks"])
        if not f:
            a["triggers"] = int(a.get("triggers") or 0) + 1
            a["latest_alert_at"] = now
        a["now"] = "Meets strategy" if not f else "Misses: " + ", ".join(f)
    for a in by.values():
        if a["mint"] in gone and not a.get("removed_at"):
            a["removed_at"], a["removed_reason"] = now, "5 or more top wallets hold the same amount"
        mark_winner(a, now)
        derive_extremes(a)
        why = removal_reason(a)
        if why:
            a.setdefault("removed_reason", why)
            a.setdefault("removed_at", now)
            a["now"] = "Removed: " + a["removed_reason"]
        a["is_new"] = a["first_alert_at"] == now
        for k in ("holders", "top10_wallets_pct", "largest_wallet_pct", "locked_lp_pct", "liquidity_usd", "volume_24h_usd"):
            a.setdefault(k, None)
        for k in ("flags", "identified_holders", "identity_checks", "now", "age", "dexscreener_url", "pumpfun_url", "x_url", "x_kind",
                  "image", "image_checked_at"):
            a.setdefault(k, "")
        a["as_of"] = now
    newest = sorted(by.values(), key=lambda a: a["first_alert_at"], reverse=True)
    saved = [a for a in newest if kept(a)]
    rest = [a for a in newest if not kept(a)][:max(0, MAX_ALERT_ROWS - len(saved))]
    out = sorted(saved + rest, key=lambda a: a["first_alert_at"], reverse=True)
    return out, new


def image_kind(b):
    """Image type from the file's first bytes, or None. SVG is never accepted."""
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "webp"
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "png"
    if b[:3] == b"\xff\xd8\xff": return "jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"): return "gif"
    return None


def fetch_image(mint):
    """('ok', data URL) | ('none', None) when the token has no usable image | ('error', reason) when the site was not reached."""
    req = urllib.request.Request(IMG_URL % mint, headers={"User-Agent": "Mozilla/5.0 (launch screener)", "Accept": "image/webp,image/*"})
    try:
        with urllib.request.urlopen(req, timeout=10) as f:
            b = f.read(IMG_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return ("none", None) if e.code == 404 else ("error", "HTTP %d" % e.code)
    except Exception as e:
        return "error", str(getattr(e, "reason", e))
    kind = image_kind(b)
    if not kind or len(b) > IMG_MAX_BYTES:
        return "none", None
    return "ok", "data:image/%s;base64,%s" % (kind, base64.b64encode(b).decode("ascii"))


def add_images(run, as_of):
    path = os.path.join(run, "out", "alerts.json")
    if not os.path.exists(path):
        raise SystemExit("out/alerts.json is missing: run build first")
    alerts = json.load(open(path, encoding="utf-8"))
    todo = [a for a in alerts if not a.get("image") and (not a.get("image_checked_at")
            or as_of - epoch(a["image_checked_at"]) >= IMG_RETRY_HOURS * 3600)][:IMG_PER_RUN]
    got = none = 0
    errors = []
    for a in todo:
        status, val = fetch_image(a["mint"])
        if status == "error":
            errors.append(val)
            if len(errors) >= 3 and not got and not none:
                break  # the site cannot be reached from here: stop rather than wait on every token
            continue
        a["image"], a["image_checked_at"] = (val if status == "ok" else ""), iso(as_of)
        got += status == "ok"
        none += status == "none"
    json.dump(alerts, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    have = sum(1 for a in alerts if a.get("image"))
    print("Images: %d downloaded, %d tokens have none, %d alerts now show a thumbnail out of %d." % (got, none, have, len(alerts)))
    if errors:
        print("Images: %d download(s) failed (%s). Thumbnails saved by earlier runs are kept; this is not an error for the run." % (
            len(errors), errors[0]))


def fetch_pairs(mints):
    req = urllib.request.Request(PRICE_URL % ",".join(mints), headers={"User-Agent": "Mozilla/5.0 (launch screener)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as f:
        return json.load(f)


def candle_extremes(a, as_of):
    """(highest cap, lowest cap) since the alert from pump.fun's price candles, or None. A wrong-sized supply would distort
    everything, so the candles must agree with the caps the screener recorded, or the coin is skipped."""
    t0 = epoch(a["first_alert_at"])
    age = as_of - t0
    interval, secs = ("5m", 300) if age <= 80 * 3600 else ("15m", 900) if age <= 240 * 3600 else ("1h", 3600)
    req = urllib.request.Request(CANDLES_URL % (a["mint"], interval), headers={"User-Agent": "Mozilla/5.0 (launch screener)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as f:
            candles = json.load(f)
        rows = [(c["timestamp"] / 1000.0, float(c["high"]) * CANDLE_SUPPLY, float(c["low"]) * CANDLE_SUPPLY, float(c["close"]) * CANDLE_SUPPLY) for c in candles]
    except Exception:
        return None
    if not rows:
        return None
    cont = [r for r in rows if r[0] <= t0 < r[0] + secs]
    first, latest = a.get("first_alert_cap_usd"), a.get("latest_cap_usd")
    agrees = bool(cont and first and cont[0][2] * 0.5 <= first <= cont[0][1] * 1.5) or bool(latest and 0.5 <= rows[-1][3] / latest <= 2)
    after = [r for r in rows if r[0] >= t0]
    if not agrees or not after:
        return None
    return max(r[1] for r in after), min(r[2] for r in after)


def reprice(run, as_of):
    """Latest market cap for alerts still tracked, from the deepest pool of each token."""
    path = os.path.join(run, "out", "alerts.json")
    if not os.path.exists(path):
        raise SystemExit("out/alerts.json is missing")
    alerts = json.load(open(path, encoding="utf-8"))
    mints = [a["mint"] for a in alerts if as_of - epoch(a["first_alert_at"]) <= TRACK_DAYS * 86400 and not removed(a)]
    best, failed = {}, 0
    for i in range(0, len(mints), 30):
        try:
            pairs = fetch_pairs(mints[i:i + 30])
        except Exception as e:
            failed += 1
            print("Prices: a DEX Screener request failed (%s); those tokens keep their last cap." % getattr(e, "reason", e))
            continue
        for o in pairs if isinstance(pairs, list) else []:
            mint = (o.get("baseToken") or {}).get("address")
            mc, liq = num(o.get("marketCap")), num((o.get("liquidity") or {}).get("usd")) or 0
            if o.get("chainId") == "solana" and mint in mints and mc and (mint not in best or liq > best[mint][0]):
                best[mint] = (liq, mc)
    now = iso(as_of)
    for a in alerts:
        if a["mint"] not in best:
            continue
        mc = best[a["mint"]][1]
        a.update(latest_cap_usd=mc, latest_cap_at=now, peak_cap_usd=max(a.get("peak_cap_usd") or 0, mc),
                 change_since_first_pct=round(100.0 * (mc / a["first_alert_cap_usd"] - 1), 1) if a.get("first_alert_cap_usd") else None)
    aths = 0
    for a in alerts:
        if a["mint"] not in mints and a.get("ath_cap_usd"):
            continue   # no longer tracked; rows that never got an all-time high are filled in once
        req = urllib.request.Request(COIN_URL % a["mint"], headers={"User-Agent": "Mozilla/5.0 (launch screener)", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as f:
                coin = json.load(f)
        except Exception:
            continue   # keeps the last ATH
        ath, ts = num(coin.get("ath_market_cap")), num(coin.get("ath_market_cap_timestamp"))
        if ath:
            # never below a cap the screener itself has seen
            a["ath_cap_usd"] = round(max(ath, a.get("peak_cap_usd") or 0, a.get("latest_cap_usd") or 0), 2)
            a["ath_at"] = iso(ts / 1000) if ts else ""
            aths += 1
    candled = 0
    for a in alerts:
        if a["mint"] not in mints and a.get("candles_at"):
            continue   # no longer tracked and already measured from its candles once
        ext = candle_extremes(a, as_of)
        time.sleep(0.1)
        if ext:
            hi, lo = ext
            a["peak_cap_usd"] = round(max(a.get("peak_cap_usd") or 0, hi), 2)
            a["trough_cap_usd"] = round(min(x for x in (a.get("trough_cap_usd"), lo) if x), 2)
            a["candles_at"] = now
            candled += 1
    for a in alerts:
        mark_winner(a, now)
        derive_extremes(a)
    json.dump(alerts, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print("Prices: %d of %d tracked alerts re-priced; all-time high found for %d; highs and lows from candles for %d; %d saved as up %d%%+ since alert." % (
        len(best), len(mints), aths, candled, sum(1 for a in alerts if kept(a)), UP_SAVE_PCT))


def load_labels(run):
    """Wallet -> label row, the page's own labels (labels/) overriding the automatic ones (auto/)."""
    by = {}
    for sub in ("auto", "labels"):
        for path in sorted(glob.glob(os.path.join(run, "labels", sub, "*.json"))):
            d = json.load(open(path, encoding="utf-8"))
            d = d.get("data", d) if isinstance(d, dict) else d
            for r in (d.get("rows") if isinstance(d, dict) and "rows" in d else [d]):
                if isinstance(r, dict) and re.fullmatch(B58, str(r.get("address", ""))):
                    by[r["address"]] = r
    return by


def mark_traders(run, as_of):
    traders = {a: r for a, r in load_labels(run).items() if r.get("category") == TRADER_CATEGORY}
    files = {k: os.path.join(run, "out", k + ".json") for k in ("tokens", "alerts")}
    data = {k: json.load(open(p, encoding="utf-8")) for k, p in files.items() if os.path.exists(p)}
    mints = list(dict.fromkeys(r["mint"] for rows in data.values() for r in rows))
    found, failed = {}, 0
    for mint in mints:
        req = urllib.request.Request(HOLDERS_URL % mint, headers={"User-Agent": "Mozilla/5.0 (launch screener)", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as f:
                holders = json.load(f).get("topHolders") or []
        except Exception:
            failed += 1
            continue
        hits = []
        for h in holders:
            t = traders.get(h.get("address"))
            if not t:
                continue
            name = str(t.get("label") or h["address"][:6]).strip()[:40]
            # pump.fun coins all have a supply of 1 billion; other coins show the name only
            pct = 100.0 * (num(h.get("amount")) or 0) / 1e9 if mint.endswith("pump") else None
            share = "" if pct is None else (" <0.1%" if pct < 0.05 else " %.1f%%" % pct)
            hits.append((pct or 0, name + share))
        found[mint] = " · ".join(t for _, t in sorted(hits, key=lambda x: -x[0]))
    now = iso(as_of)
    for k, rows in data.items():
        for r in rows:
            if r["mint"] in found:   # a failed lookup keeps what the row had
                r["top_traders"], r["top_traders_checked_at"] = found[r["mint"]], now
        json.dump(rows, open(files[k], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print("Traders: %d labelled top traders; %d of %d coins checked, %d held by at least one%s." % (
        len(traders), len(found), len(mints), sum(1 for v in found.values() if v),
        (", %d lookups failed" % failed) if failed else ""))


def money(v):
    return "n/a" if v is None else ("$%.2fM" % (v / 1e6) if v >= 1e6 else "$%.1fK" % (v / 1e3))


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("urls-market", "urls-reports", "build", "images", "reprice", "traders"):
        raise SystemExit(__doc__)
    cmd, run = sys.argv[1], sys.argv[2]
    p = get_params(run)
    prev_alerts = read_json(os.path.join(run, "prev_alerts.json"), [])
    profiles = read_json(os.path.join(run, "prev_profiles.json"), {})
    if isinstance(profiles, dict) and isinstance(profiles.get("list"), list):
        profiles = profiles["list"]
    if isinstance(profiles, list):
        profiles = {x["wallet"]: x for x in profiles if isinstance(x, dict) and x.get("wallet")}

    if cmd == "urls-market":
        as_of = get_as_of(run, create=True)
        cands = candidates(run)
        mints = list(cands) + [m for m in tracked(prev_alerts, as_of) if m not in cands]
        print("%d tokens (%d candidates, %d earlier alerts). Fetch each URL and save the answer as in/market_N.json:" % (
            len(mints), len(cands), len(mints) - len(cands)))
        for i in range(0, len(mints), 4):
            print("https://api.dexscreener.com/tokens/v1/solana/%s?z=%s" % (",".join(mints[i:i + 4]), buster()))
        return

    as_of = get_as_of(run)
    if cmd == "images":
        add_images(run, as_of)
        return
    if cmd == "reprice":
        reprice(run, as_of)
        return
    if cmd == "traders":
        mark_traders(run, as_of)
        return
    if cmd == "urls-reports":
        mk = market(run, as_of)
        need = [m for m, o in mk.items() if passes_market(o, p, as_of)]
        need = sorted(need, key=lambda m: -(num(mk[m].get("liq")) or 0))[:MAX_REPORTS]
        print("%d of %d tokens pass the saved age, liquidity and volume limits. Fetch each URL and save the answer as in/report_N.json:" % (
            len(need), len(mk)))
        for m in need:
            print("https://api.rugcheck.xyz/v1/tokens/%s/report?z=%s" % (m, buster()))
        return

    # build
    for path in sorted(glob.glob(os.path.join(run, "in", "profile_*.json"))):
        try:
            o = loose_json(path)
        except ValueError:
            continue
        if isinstance(o, dict) and re.fullmatch(B58, str(o.get("address", ""))):
            nm = o.get("username")
            profiles[o["address"]] = dict(wallet=o["address"], username=nm if isinstance(nm, str) and nm.strip() else None,
                                          followers=num(o.get("followers")), checked_at=iso(as_of))
    rows, lookups, dropped = build_rows(run, p, as_of, prev_alerts, profiles)
    alerts, new = update_alerts(prev_alerts, rows, p, as_of, dropped)
    alerted = {a["mint"] for a in alerts}
    want, seen = [], set()
    for mint, owner, pct in sorted(lookups, key=lambda t: -t[2]):
        if mint in alerted and owner not in seen:
            seen.add(owner); want.append(owner)
    out = os.path.join(run, "out")
    os.makedirs(out, exist_ok=True)
    gone = {a["mint"] for a in alerts if removed(a)}
    shown = [r for r in rows if r["mint"] not in gone and not (r["market_cap_usd"] is not None and r["market_cap_usd"] < MIN_CAP_USD)]
    json.dump(shown, open(os.path.join(out, "tokens.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    json.dump(alerts, open(os.path.join(out, "alerts.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    keep = sorted(profiles.values(), key=lambda x: x.get("checked_at") or "", reverse=True)[:1500]
    json.dump({"list": keep}, open(os.path.join(out, "profiles_doc.json"), "w", encoding="utf-8"), ensure_ascii=False)
    meets = [r for r in rows if not fails(r, p)]
    lines = ["Run at %s: %d tokens screened, %d holder-checked, %d meet the strategy, %d new." % (
        iso(as_of), len(rows), sum(1 for r in rows if r["holder_check"] == "Checked"), len(meets), len(new))]
    for r in new:
        lines.append("NEW ALERT %s (%s): cap %s, liquidity %s, %s old, %s holders, top-10 wallets %s%%, largest wallet %s%%%s" % (
            r["symbol"], r["name"], money(r["market_cap_usd"]), money(r["liquidity_usd"]), r["age"], r["holders"],
            r["top10_wallets_pct"], r["largest_wallet_pct"], ("; flags: " + r["flags"]) if r["flags"] else "")
            + (("; check: " + ", ".join(r["labels"])) if r.get("labels") else ""))
    if not new:
        lines.append("No new matches this run.")
    if dropped:
        lines.append("Filtered out %d coin(s) where %d or more top wallets hold the same amount: %s" % (
            len(dropped), SAME_AMOUNT_DROP, ", ".join("%s (%d wallets)" % (d["symbol"], d["wallets"]) for d in dropped)))
    open(os.path.join(out, "summary.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    if want:
        print("\nProfile lookups still to do (%d, doing the first %d). Fetch each URL, save the answer as in/profile_N.json, then run build again:" % (
            len(want), min(len(want), MAX_PROFILE_LOOKUPS)))
        for w in want[:MAX_PROFILE_LOOKUPS]:
            print("https://frontend-api-v3.pump.fun/users/%s?z=%s" % (w, buster()))
    else:
        print("\nNo profile lookups left. out/ is ready to upload.")


if __name__ == "__main__":
    main()
