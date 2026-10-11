#!/usr/bin/env python3
"""Solana launch screener: turns saved lookups into the dashboard's data files.

  screen.py urls-market  RUN   after the three candidate lists are saved: prints the market-data URLs to fetch
  screen.py urls-reports RUN   after market data is saved: prints the RugCheck report URLs to fetch
  screen.py build        RUN   after reports are saved: writes RUN/out/*.json, prints profile URLs still needed
                               and the alert summary. Safe to run again after saving profiles.
  screen.py images       RUN   after the last build: downloads token thumbnails for the alerts into RUN/out/alerts.json.
                               The one command that goes online itself; if the image site cannot be reached it
                               keeps the thumbnails earlier runs saved and says so.
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
import sys, os, re, json, glob, random, string, base64, urllib.request, urllib.error, datetime as dt

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
PRICE_URL = "https://api.dexscreener.com/tokens/v1/solana/%s"   # up to 30 mints, comma separated
IMG_URL = "https://dd.dexscreener.com/ds-data/tokens/solana/%s.png"   # redirects to a 64 px thumbnail on cdn.dexscreener.com
IMG_MAX_BYTES = 12000             # larger answers are skipped, not shrunk
IMG_PER_RUN = 40                  # downloads per run
IMG_RETRY_HOURS = 6               # wait this long before asking again for a token that had no image
RAYDIUM_AUTH = {"GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL": "Raydium CPMM pool",
                "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium pool"}
VENUES = {"pumpfun": "Pump.fun bonding curve", "pumpswap": "PumpSwap", "raydium": "Raydium",
          "meteoradbc": "Meteora bonding curve", "meteora": "Meteora", "orca": "Orca"}
DEFAULTS = dict(max_age_days=7, min_liq=10000, min_vol=10000, min_holders=500, max_top10=30,
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
        open(p, "w").write(iso(dt.datetime.now(dt.timezone.utc).timestamp()))
    return epoch(open(p).read())


def get_params(run):
    p = dict(DEFAULTS)
    p.update({k: v for k, v in read_json(os.path.join(run, "params.json"), {}).items() if k in DEFAULTS})
    return p


def candidates(run):
    out = {}
    for name, via in (("profiles", "New profile"), ("boosts_latest", "Paid boost"), ("boosts_top", "Paid boost")):
        path = os.path.join(run, "in", "candidates_%s.txt" % name)
        if not os.path.exists(path):
            print("WARNING: missing", path)
            continue
        for m in re.finditer(r"(?m)^\s*(%s)\b" % B58, open(path, encoding="utf-8").read()):
            out.setdefault(m.group(1), via)
    return out


def tracked(prev_alerts, as_of):
    return [a["mint"] for a in prev_alerts if as_of - epoch(a["first_alert_at"]) <= TRACK_DAYS * 86400]


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
    for mint in list(cands) + [m for m in track if m not in cands]:
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
            if ins >= 20: flags.append("%d linked wallets (RugCheck)" % ins)
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
        rows.append(r)
    return rows, lookups, dropped


def update_alerts(prev, rows, p, as_of, dropped=()):
    """One row per token that has ever met the strategy, minus coins dropped by the same-amount rule."""
    gone = {d["mint"] for d in dropped}
    by = {a["mint"]: dict(a) for a in prev if a["mint"] not in gone}
    now = iso(as_of)
    new = []
    seen = {r["mint"] for r in rows}
    for a in by.values():
        if a["mint"] not in seen:
            a["now"] = "Not re-priced in the last run"
    for r in rows:
        f = fails(r, p)
        a = by.get(r["mint"])
        if not f and a is None and r["market_cap_usd"]:
            a = by[r["mint"]] = dict(mint=r["mint"], first_alert_at=now, first_alert_cap_usd=r["market_cap_usd"],
                                      first_alert_age=r["age"], triggers=0, latest_alert_at=now,
                                      peak_cap_usd=r["market_cap_usd"])
            new.append(r)
        if a is None:
            continue
        a.update(symbol=r["symbol"], name=r["name"], venue=r["venue"],
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
        a["is_new"] = a["first_alert_at"] == now
        for k in ("holders", "top10_wallets_pct", "largest_wallet_pct", "locked_lp_pct", "liquidity_usd", "volume_24h_usd"):
            a.setdefault(k, None)
        for k in ("flags", "identified_holders", "identity_checks", "now", "age", "dexscreener_url", "pumpfun_url", "x_url", "x_kind",
                  "image", "image_checked_at"):
            a.setdefault(k, "")
        a["as_of"] = now
    out = sorted(by.values(), key=lambda a: a["first_alert_at"], reverse=True)[:200]
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
    json.dump(alerts, open(path, "w"), ensure_ascii=False, indent=0)
    have = sum(1 for a in alerts if a.get("image"))
    print("Images: %d downloaded, %d tokens have none, %d alerts now show a thumbnail out of %d." % (got, none, have, len(alerts)))
    if errors:
        print("Images: %d download(s) failed (%s). Thumbnails saved by earlier runs are kept; this is not an error for the run." % (
            len(errors), errors[0]))


def fetch_pairs(mints):
    req = urllib.request.Request(PRICE_URL % ",".join(mints), headers={"User-Agent": "Mozilla/5.0 (launch screener)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as f:
        return json.load(f)


def reprice(run, as_of):
    """Latest market cap for alerts still tracked, from the deepest pool of each token."""
    path = os.path.join(run, "out", "alerts.json")
    if not os.path.exists(path):
        raise SystemExit("out/alerts.json is missing")
    alerts = json.load(open(path, encoding="utf-8"))
    mints = [a["mint"] for a in alerts if as_of - epoch(a["first_alert_at"]) <= TRACK_DAYS * 86400]
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
    json.dump(alerts, open(path, "w"), ensure_ascii=False, indent=0)
    print("Prices: %d of %d tracked alerts re-priced." % (len(best), len(mints)))


def money(v):
    return "n/a" if v is None else ("$%.2fM" % (v / 1e6) if v >= 1e6 else "$%.1fK" % (v / 1e3))


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("urls-market", "urls-reports", "build", "images", "reprice"):
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
    json.dump(rows, open(os.path.join(out, "tokens.json"), "w"), ensure_ascii=False, indent=0)
    json.dump(alerts, open(os.path.join(out, "alerts.json"), "w"), ensure_ascii=False, indent=0)
    keep = sorted(profiles.values(), key=lambda x: x.get("checked_at") or "", reverse=True)[:1500]
    json.dump({"list": keep}, open(os.path.join(out, "profiles_doc.json"), "w"), ensure_ascii=False)
    meets = [r for r in rows if not fails(r, p)]
    lines = ["Run at %s: %d tokens screened, %d holder-checked, %d meet the strategy, %d new." % (
        iso(as_of), len(rows), sum(1 for r in rows if r["holder_check"] == "Checked"), len(meets), len(new))]
    for r in new:
        lines.append("NEW ALERT %s (%s): cap %s, liquidity %s, %s old, %s holders, top-10 wallets %s%%, largest wallet %s%%%s" % (
            r["symbol"], r["name"], money(r["market_cap_usd"]), money(r["liquidity_usd"]), r["age"], r["holders"],
            r["top10_wallets_pct"], r["largest_wallet_pct"], ("; flags: " + r["flags"]) if r["flags"] else ""))
    if not new:
        lines.append("No new matches this run.")
    if dropped:
        lines.append("Filtered out %d coin(s) where %d or more top wallets hold the same amount: %s" % (
            len(dropped), SAME_AMOUNT_DROP, ", ".join("%s (%d wallets)" % (d["symbol"], d["wallets"]) for d in dropped)))
    open(os.path.join(out, "summary.txt"), "w").write("\n".join(lines) + "\n")
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
