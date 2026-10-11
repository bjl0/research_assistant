"""Build Solana wallet labels from the screener's tokens.

Usage: python3 labels/build_labels.py data/tokens.json labels/labels.json

Besides the full list it writes labels/chunks/auto-<n>.json (about 250 rows
each, under the 256 KiB document limit) and labels/chunks/meta.json. Those
files are the documents for the wallet label page's `auto` collection;
labels people add on the page live in its `labels` collection and are never
touched by this script.

The list holds individual trader wallets only (Blake's request, 10 October
2026). For each token mint it downloads the RugCheck report and collects
large holders RugCheck marks as insiders. Left out entirely:
  - every account RugCheck names itself (pools of any venue, bonding
    curves, liquidity lockers, order-book markets, ...), the token's market
    accounts and its liquidity locker owners
  - token creator wallets, even if they also hold a token or have a
    pump.fun profile
It also adds every wallet on pump.fun's own profit leaderboard
(frontend-api-v3.pump.fun/pnl-leaderboard: daily, weekly and monthly, each
sorted by combined and by realized profit, top 100 per list), with the
pump.fun username, X handle and best placement.

X (Twitter) accounts: every listed wallet gets the X account linked to its
pump.fun profile (`x_username`), when there is one.

Then it looks up the pump.fun profile behind every
holder wallet with at least 1% of a token, and keeps those that have a
username. Names and profile text are written by strangers: they are stored
as plain text and never followed.

SOL balances: every labelled wallet's native SOL balance is read from a
Solana RPC node (getMultipleAccounts, 100 wallets per call). The node is
https://api.mainnet-beta.solana.com unless SOLANA_RPC_URL is set. If the node
cannot be reached the labels are still written, without balances, and the
page hides its SOL balance option. Only native SOL is counted: wrapped SOL
held in token accounts (as liquidity pools hold it) is not included.

Confidence is fixed per kind of evidence, not guessed per wallet:
  90 on pump.fun's profit leaderboard (pump.fun's own account data),
  80 pump.fun profile (self-chosen name), 70 RugCheck insider heuristic,
  50 pump.fun profile whose name looks auto-generated (three joined words,
  e.g. "PewterLemonAct"; these are flagged "auto-name").
"""
import json, os, re, sys, time, urllib.request, datetime

UA = {"User-Agent": "solana-label-db/1.0", "Accept": "application/json"}
CONF = {"LEADERBOARD": 90, "PROFILE": 80, "INSIDER": 70, "AUTONAME": 50}
X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
PERIODS = {"daily": "24h", "weekly": "7d", "monthly": "30d"}
AUTO_NAME = re.compile(r"^(?:[A-Z][a-z]+){3}$")


def get(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.load(r)
        except Exception as e:
            if getattr(e, "code", None) == 404:
                return None
            time.sleep(2 * (attempt + 1))
    return None


def sol_balances(addresses):
    """Return {address: SOL} for the addresses, or None if the RPC node fails."""
    url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    out = {}
    for start in range(0, len(addresses), 100):
        batch = addresses[start:start + 100]
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                           "params": [batch, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]}).encode()
        req = urllib.request.Request(url, data=body, headers={**UA, "Content-Type": "application/json"})
        res = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.load(r)
                break
            except Exception as e:
                print(f"RPC error: {e}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
        if not res or "result" not in res:
            return None
        for addr, acct in zip(batch, res["result"]["value"]):
            out[addr] = round((acct or {}).get("lamports", 0) / 1e9, 4)
        time.sleep(0.3)
    return out


def clean(s, n=60):
    s = "" if s is None else str(s)
    return "".join(c for c in s if c.isprintable())[:n].strip()


def main(tokens_path, out_path):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tokens = json.load(open(tokens_path))
    rows = {}
    big_holders = set()
    holdings = {}  # owner -> {"usd": float, "tokens": [symbols]}
    excluded = set()  # creators, pools, lockers, markets: not trader wallets

    def row(addr):
        return rows.setdefault(addr, {
            "address": addr, "chain": "SOL", "label": "", "sublabel": "",
            "category": "", "confidence": 0, "evidence": [], "tokens": [],
            "flags": [], "holdings_usd": 0.0, "followers": None,
            "source": "auto", "updated_at": now,
        })

    def claim(r, category, label, sublabel, kind):
        if CONF[kind] >= r["confidence"]:
            r.update(category=category, label=label, sublabel=sublabel, confidence=CONF[kind])
        if kind not in r["evidence"]:
            r["evidence"].append(kind)

    def seen(r, sym):
        if sym and sym not in r["tokens"]:
            r["tokens"].append(sym)

    for i, t in enumerate(tokens):
        mint, sym = t["mint"], clean(t.get("symbol"), 16)
        rep = get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        time.sleep(0.4)
        if not rep:
            print(f"[{i+1}/{len(tokens)}] {sym}: no report", file=sys.stderr)
            continue
        price = rep.get("price") or 0
        excluded.update(rep.get("knownAccounts") or {})
        excluded.update(m["pubkey"] for m in rep.get("markets") or [] if m.get("pubkey"))
        if isinstance(rep.get("lockerOwners"), dict):
            excluded.update(rep["lockerOwners"])
        if rep.get("creator"):
            excluded.add(rep["creator"])
        for h in rep.get("topHolders") or []:
            owner = h.get("owner")
            if not owner:
                continue
            pct = h.get("pct") or 0
            if pct >= 1:
                big_holders.add(owner)
            hd = holdings.setdefault(owner, {"usd": 0.0, "tokens": []})
            hd["usd"] += (h.get("uiAmount") or 0) * price
            if sym and sym not in hd["tokens"]:
                hd["tokens"].append(sym)
            if h.get("insider"):
                r = row(owner)
                if not r["confidence"]:
                    claim(r, "INSIDER", "Insider wallet", sym, "INSIDER")
                elif "INSIDER" not in r["evidence"]:
                    r["evidence"].append("INSIDER")
                if "insider" not in r["flags"]:
                    r["flags"].append("insider")
                seen(r, sym)
        print(f"[{i+1}/{len(tokens)}] {sym}: ok", file=sys.stderr)

    for addr in excluded:
        rows.pop(addr, None)
    checked = set()  # wallets whose pump.fun profile has been looked up

    def note_x(r, p):
        x = clean((p or {}).get("x_username"), 20)
        if x and X_HANDLE.match(x):
            r["x_username"] = x

    for j, w in enumerate(sorted(big_holders - excluded)):
        p = get(f"https://frontend-api-v3.pump.fun/users/{w}")
        checked.add(w)
        time.sleep(0.25)
        name = clean((p or {}).get("username"), 40)
        if not name or name == w[:6]:
            continue  # no profile, or pump.fun's default (first 6 characters)
        auto = bool(AUTO_NAME.match(name))
        r = row(w)
        if auto and "auto-name" not in r["flags"]:
            r["flags"].append("auto-name")
        if p.get("is_banned") and "banned" not in r["flags"]:
            r["flags"].append("banned")
        r["followers"] = p.get("followers")
        note_x(r, p)
        if r["category"] in ("", "INSIDER"):
            claim(r, "PROFILE", name, "PUMP.FUN", "AUTONAME" if auto else "PROFILE")
        else:
            r["sublabel"] = f"@{name}"
            r["evidence"].append("PROFILE")
    for period, short in PERIODS.items():
        for sort in ("combined", "realized"):
            lb = get(f"https://frontend-api-v3.pump.fun/pnl-leaderboard?period={period}&sort={sort}&limit=100")
            time.sleep(0.3)
            entries = (lb or {}).get("entries") or []
            print(f"leaderboard {period}/{sort}: {len(entries)} wallets", file=sys.stderr)
            for e in entries:
                w = e.get("walletAddress")
                if not w or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", w) or w in excluded:
                    continue
                r = row(w)
                rank = int(e.get("rank") or 0)
                place = {"period": short, "sort": sort, "rank": rank,
                         "pnl_usd": round(float(e.get("pnlUsd") or 0), 2),
                         "realized_usd": round(float(e.get("realizedPnlUsd") or 0), 2)}
                r.setdefault("leaderboard", []).append(place)
                best = min(r["leaderboard"], key=lambda x: x["rank"])
                name = clean(e.get("username"), 40)
                if not name or name == w[:6]:
                    name = "pump.fun top trader"
                claim(r, "TOP TRADER", name, f"PF #{best['rank']} {best['period']}", "LEADERBOARD")
                if r["category"] == "TOP TRADER":
                    r["sublabel"] = f"PF #{best['rank']} {best['period']}"
                x = clean(e.get("xUsername"), 20)
                if x and X_HANDLE.match(x):
                    r["x_username"] = x
                if e.get("isVerified") and "verified" not in r["flags"]:
                    r["flags"].append("verified")
                r["best_pnl_usd"] = max(r.get("best_pnl_usd") or 0, place["pnl_usd"])
    # X accounts linked on pump.fun, for listed wallets not looked up yet
    # (mostly leaderboard wallets whose board entry carried no X handle).
    for w in [a for a, r in rows.items() if not r.get("x_username") and a not in checked]:
        p = get(f"https://frontend-api-v3.pump.fun/users/{w}")
        checked.add(w)
        time.sleep(0.25)
        if p:
            note_x(rows[w], p)
            if rows[w].get("followers") is None:
                rows[w]["followers"] = p.get("followers")
    for addr, hd in holdings.items():
        if addr in rows:
            rows[addr]["holdings_usd"] = hd["usd"]
            for sym in hd["tokens"]:
                if sym not in rows[addr]["tokens"]:
                    rows[addr]["tokens"].append(sym)
    balances = sol_balances(list(rows))
    if balances is None:
        print("SOL balances skipped: RPC node unreachable", file=sys.stderr)
    else:
        for addr, sol in balances.items():
            rows[addr]["sol_balance"] = sol
    out = sorted(rows.values(), key=lambda r: (-r["confidence"], r["label"]))
    for r in out:
        r["holdings_usd"] = round(r["holdings_usd"], 2)
    json.dump(out, open(out_path, "w"), indent=1)
    cdir = os.path.join(os.path.dirname(out_path) or ".", "chunks")
    os.makedirs(cdir, exist_ok=True)
    for old in os.listdir(cdir):
        if old.startswith("auto-"):
            os.remove(os.path.join(cdir, old))
    size = 250
    n = 0
    for n, start in enumerate(range(0, len(out), size)):
        json.dump({"rows": out[start:start + size]}, open(os.path.join(cdir, f"auto-{n}.json"), "w"),
                  separators=(",", ":"))
    json.dump({"updated_at": now, "tokens_scanned": len(tokens), "labels": len(out), "chunks": n + 1,
               "sol_checked_at": now if balances is not None else None},
              open(os.path.join(cdir, "meta.json"), "w"))
    print(f"{len(out)} labels written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:3])
