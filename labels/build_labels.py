"""Build Solana wallet labels from the screener's tokens.

Usage: python3 labels/build_labels.py data/tokens.json labels/labels.json

Besides the full list it writes labels/chunks/auto-<n>.json (about 250 rows
each, under the 256 KiB document limit) and labels/chunks/meta.json. Those
files are the documents for the wallet label page's `auto` collection;
labels people add on the page live in its `labels` collection and are never
touched by this script.

For each token mint it downloads the RugCheck report and collects:
  - accounts RugCheck names itself (AMM pools, lockers, exchanges ...)
  - the token's creator wallet
  - large holders RugCheck marks as insiders
Then it looks up the pump.fun profile behind every creator and every
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
  99 creator (on-chain record), 95 RugCheck named account,
  80 pump.fun profile (self-chosen name), 70 RugCheck insider heuristic,
  50 pump.fun profile whose name looks auto-generated (three joined words,
  e.g. "PewterLemonAct"; these are flagged "auto-name").
"""
import json, os, re, sys, time, urllib.request, datetime

UA = {"User-Agent": "solana-label-db/1.0", "Accept": "application/json"}
CONF = {"CREATOR": 99, "RUGCHECK": 95, "PROFILE": 80, "INSIDER": 70, "AUTONAME": 50}
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
        for addr, k in (rep.get("knownAccounts") or {}).items():
            typ = clean(k.get("type"), 20).upper() or "KNOWN"
            if typ == "CREATOR":
                continue  # handled below from the creator field
            r = row(addr)
            claim(r, typ, clean(k.get("name")), sym if typ in ("AMM", "LOCKER", "CLOB") else "RUGCHECK", "RUGCHECK")
            seen(r, sym)
        creator = rep.get("creator")
        if creator:
            r = row(creator)
            claim(r, "CREATOR", f"Creator of {sym}" if sym else "Token creator", "", "CREATOR")
            seen(r, sym)
            if "creator" not in r["flags"]:
                r["flags"].append("creator")
            big_holders.add(creator)
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

    for j, w in enumerate(sorted(big_holders)):
        p = get(f"https://frontend-api-v3.pump.fun/users/{w}")
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
        if r["category"] in ("", "INSIDER"):
            claim(r, "PROFILE", name, "PUMP.FUN", "AUTONAME" if auto else "PROFILE")
        else:
            r["sublabel"] = f"@{name}"
            r["evidence"].append("PROFILE")
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
