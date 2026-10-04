import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

HOST = "https://api.oddspapi.io/v4"
STATE_FILE = Path("data/state.json")

API_KEY = os.environ["ODDSPAPI_KEY"]
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DISCORD = os.getenv("DISCORD_WEBHOOK_URL")

BOOKMAKERS = [b.strip() for b in
              os.getenv("BOOKMAKERS", "pinnacle,bet365").split(",") if b.strip()][:3]
HORIZON_HOURS = int(os.getenv("HORIZON_HOURS", "48"))
FIXTURES_DAYS = int(os.getenv("FIXTURES_DAYS", "3"))
REFRESH_HOURS = int(os.getenv("REFRESH_HOURS", "24"))
MAX_FIXTURES = int(os.getenv("MAX_FIXTURES", "60"))
MONTHLY_BUDGET = int(os.getenv("MONTHLY_BUDGET", "235"))

KEYWORDS = ["soccer", "tennis", "basketball"]
EXCLUDE = ["table", "beach", "american"]
GOOD = ("result", "moneyline", "money line", "winner", "to win")
BAD = ("total", "half", "quarter", "set", "period", "map", "corner", "both",
       "double", "draw no bet", "over", "under", "handicap", "spread", "odd",
       "even", "point", "game", "goal", "score", "penalt", "card", "team", "exact")

OPEN_MIN, OPEN_MAX = 1.90, 2.50
CUR_MIN, CUR_MAX = 1.60, 2.00
MIN_DROP = 0.15

GAPS = {"/historical-odds": 5.2, "/fixtures": 2.2}
STATE = {}
_last = {}


class QuotaError(Exception):
    pass


def month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def budget_left():
    return MONTHLY_BUDGET - STATE["billable"].get(month_key(), 0)


def parse_date(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def throttle(path):
    wait = _last.get(path, 0) + GAPS.get(path, 1.2) - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[path] = time.time()


def call(path, billed=True, **params):
    params["apiKey"] = API_KEY
    for _ in range(4):
        throttle(path)
        r = requests.get(HOST + path, params=params, timeout=60)
        if r.status_code == 429:
            if "REQUEST_LIMIT_EXCEEDED" in r.text:
                raise QuotaError(r.text)
            time.sleep(6)
            continue
        if billed:
            k = month_key()
            STATE["billable"][k] = STATE["billable"].get(k, 0) + 1
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"429 persistant sur {path}")


def load_state():
    base = {"meta": {}, "fixtures": {}, "alerted": {}, "billable": {}}
    if STATE_FILE.exists():
        base.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return base


def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def notify(text):
    if TG_TOKEN and TG_CHAT:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text}, timeout=30)
    if DISCORD:
        requests.post(DISCORD, json={"content": text}, timeout=30)


def is_main(m):
    if m.get("playerProp") or m.get("period") != "fulltime":
        return False
    if (m.get("handicap") or 0) != 0 or m.get("marketLength") not in (2, 3):
        return False
    name = (m.get("marketName") or "").lower()
    if any(b in name for b in BAD):
        return False
    if (m.get("marketType") or "").lower() in ("1x2", "moneyline"):
        return True
    return any(g in name for g in GOOD)


def load_meta():
    if STATE["meta"].get("markets") and STATE["meta"].get("sports"):
        return
    wanted = {}
    for s in call("/sports"):
        text = f'{s.get("slug", "")} {s.get("sportName", "")}'.lower()
        if any(k in text for k in KEYWORDS) and not any(x in text for x in EXCLUDE):
            wanted[str(s["sportId"])] = s["sportName"]
    markets = call("/markets")
    mk = {}
    for m in markets:
        sid = str(m.get("sportId"))
        if sid in wanted and is_main(m):
            mk.setdefault(sid, {})[str(m["marketId"])] = {
                str(o["outcomeId"]): o["outcomeName"] for o in m["outcomes"]}
    for sid, name in wanted.items():
        if sid in mk:
            print(f"{name} : marchés retenus {list(mk[sid])}")
        else:
            cands = [f'{m["marketId"]}:{m["marketName"]}({m.get("marketType")})'
                     for m in markets
                     if str(m.get("sportId")) == sid and m.get("period") == "fulltime"
                     and (m.get("handicap") or 0) == 0][:12]
            print(f"ATTENTION {name} : aucun marché principal trouvé. Candidats : {cands}")
    STATE["meta"] = {"sports": wanted, "markets": mk}


def refresh_fixtures(now):
    for sid, name in STATE["meta"]["sports"].items():
        entry = STATE["fixtures"].get(sid)
        if entry and now - parse_date(entry["fetched"]) < timedelta(hours=REFRESH_HOURS):
            continue
        if budget_left() <= 0:
            print("Budget mensuel atteint, liste des matchs non rafraîchie.")
            return
        data = call("/fixtures", sportId=sid,
                    **{"from": now.strftime("%Y-%m-%d"),
                       "to": (now + timedelta(days=FIXTURES_DAYS)).strftime("%Y-%m-%d")},
                    statusId=0, hasOdds="true", bookmakers=",".join(BOOKMAKERS))
        if not isinstance(data, list):
            data = []
        items = [{"id": f["fixtureId"], "sportId": sid,
                  "p1": f.get("participant1Name", "?"), "p2": f.get("participant2Name", "?"),
                  "start": f["startTime"], "sport": f.get("sportName", name),
                  "league": f.get("tournamentName", "")} for f in data]
        STATE["fixtures"][sid] = {"fetched": now.isoformat(), "items": items}
        print(f"{name} : {len(items)} matchs")


def label_for(raw, f):
    r = raw.strip().lower()
    if r in ("1", "home"):
        return f["p1"]
    if r in ("2", "away"):
        return f["p2"]
    if r in ("x", "draw"):
        return "Nul"
    return raw


def analyse(entries):
    rows = sorted((e for e in entries if e.get("price") is not None),
                  key=lambda e: e["createdAt"])
    active = [e for e in rows if e.get("active")]
    if not rows or not active or not rows[-1].get("active"):
        return None
    return float(active[0]["price"]), float(rows[-1]["price"])


def matches_filters(o, c):
    return (OPEN_MIN <= o <= OPEN_MAX and CUR_MIN <= c <= CUR_MAX
            and round(o - c, 2) >= MIN_DROP)


def check(now):
    horizon = now + timedelta(hours=HORIZON_HOURS)
    fx = [f for e in STATE["fixtures"].values() for f in e["items"]
          if now < parse_date(f["start"]) <= horizon]
    fx.sort(key=lambda f: f["start"])
    fx = fx[:MAX_FIXTURES]
    print(f"{len(fx)} matchs à vérifier")
    alerts = errors = 0
    for f in fx:
        mk = STATE["meta"]["markets"].get(f["sportId"], {})
        if not mk:
            continue
        try:
            hist = call("/historical-odds", billed=False, fixtureId=f["id"],
                        bookmakers=",".join(BOOKMAKERS))
        except Exception as exc:
            errors += 1
            print(f"Erreur {f['id']} : {exc}")
            if errors >= 3:
                break
            continue
        root = hist if "bookmakers" in hist else hist.get(f["id"], {})
        for bookie, bdata in (root.get("bookmakers") or {}).items():
            for mid, mdata in (bdata.get("markets") or {}).items():
                labels = mk.get(mid)
                if not labels:
                    continue
                for oid, odata in (mdata.get("outcomes") or {}).items():
                    if oid not in labels:
                        continue
                    res = analyse((odata.get("players") or {}).get("0", []))
                    key = f'{f["id"]}|{bookie}|{mid}|{oid}'
                    if not res or key in STATE["alerted"]:
                        continue
                    op, cur = res
                    if matches_filters(op, cur):
                        drop = round(op - cur, 2)
                        notify(
                            f"📉 ALERTE VARIACOTE\n"
                            f"Sport : {f['sport']}\n"
                            f"Match : {f['p1']} - {f['p2']}\n"
                            f"Sélection : {label_for(labels[oid], f)}\n"
                            f"Bookmaker : {bookie}\n"
                            f"Cote d'ouverture : {op:.2f}\n"
                            f"Cote actuelle : {cur:.2f}\n"
                            f"Variation : -{drop:.2f} (-{drop / op * 100:.1f}%)")
                        STATE["alerted"][key] = now.isoformat()
                        alerts += 1
    print(f"{alerts} alerte(s)")


def main():
    global STATE
    STATE = load_state()
    now = datetime.now(timezone.utc)
    try:
        try:
            load_meta()
            refresh_fixtures(now)
        except QuotaError:
            print("Quota API atteint : on continue avec les matchs en cache.")
        check(now)
        limit = now - timedelta(days=5)
        STATE["alerted"] = {k: v for k, v in STATE["alerted"].items()
                            if parse_date(v) > limit}
        print(f"Requêtes facturées ce mois : {STATE['billable'].get(month_key(), 0)}")
    finally:
        save_state()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        sys.exit(1)
