import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import requests

HOST = "https://api.oddspapi.io/v4"
STATE_FILE = Path("data/state.json")
ALERTS_FILE = Path("data/alerts.json")

API_KEY = os.environ["ODDSPAPI_KEY"]
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DISCORD = os.getenv("DISCORD_WEBHOOK_URL")

ALERT_BOOKMAKER = os.getenv("ALERT_BOOKMAKER", "winamax.fr").strip()
REF_BOOKMAKERS = [b.strip() for b in
                  os.getenv("REF_BOOKMAKERS", "pinnacle,bet365").split(",")
                  if b.strip()][:2]
ALL_BOOKS = ",".join([ALERT_BOOKMAKER] + REF_BOOKMAKERS)
FLASH_URL = os.getenv("FLASH_URL", "https://www.google.com/search?q=flashscore+")
APP_URL = os.getenv("APP_URL", "").strip()

HORIZON_HOURS = int(os.getenv("HORIZON_HOURS", "24"))
FIXTURES_DAYS = int(os.getenv("FIXTURES_DAYS", "3"))
REFRESH_HOURS = int(os.getenv("REFRESH_HOURS", "24"))
MAX_FIXTURES = int(os.getenv("MAX_FIXTURES", "150"))
MONTHLY_BUDGET = int(os.getenv("MONTHLY_BUDGET", "235"))
REF_HOURS = int(os.getenv("REF_HOURS", "72"))
MAX_POINTS = 120

WANTED = {"tennis", "basketball", "volleyball"}
MAIN_NAMES = {"full time result", "match winner", "winner", "moneyline",
              "money line", "match result", "winner (incl. overtime)",
              "regular time result"}

OPEN_MIN, OPEN_MAX = 1.90, 2.50
CUR_MIN, CUR_MAX = 1.60, 2.00
MIN_DROP = 0.15

GAPS = {"/historical-odds": 5.2, "/fixtures": 2.2}
STATE = {}
ALERTS = {}
_last = {}


class QuotaError(Exception):
    pass


def month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def budget_left():
    return MONTHLY_BUDGET - STATE["billable"].get(month_key(), 0)


def parse_date(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def short(b):
    return b.split(".")[0].capitalize()


def flash_url(f):
    names = " ".join(p.split(",")[0].strip() for p in (f["p1"], f["p2"]))
    return FLASH_URL + quote_plus(names)


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


def load_alert_records():
    if ALERTS_FILE.exists():
        return {a["id"]: a for a in json.loads(ALERTS_FILE.read_text(encoding="utf-8"))}
    return {}


def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    limit = datetime.now(timezone.utc) - timedelta(days=7)
    keep = [a for a in ALERTS.values()
            if parse_date(a["start"]) > limit and a["bookmaker"] == ALERT_BOOKMAKER]
    keep.sort(key=lambda a: a["alerted_at"], reverse=True)
    ALERTS_FILE.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def notify(text):
    if TG_TOKEN and TG_CHAT:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text}, timeout=30)
    if DISCORD:
        requests.post(DISCORD, json={"content": text}, timeout=30)


def is_main(m):
    if m.get("playerProp"):
        return False
    if (m.get("handicap") or 0) != 0 or m.get("marketLength") not in (2, 3):
        return False
    return (m.get("marketName") or "").strip().lower() in MAIN_NAMES


def load_meta():
    meta = STATE["meta"]
    if (meta.get("markets") and meta.get("sports")
            and meta.get("wanted") == sorted(WANTED)
            and meta.get("names") == sorted(MAIN_NAMES)):
        return
    wanted = {str(s["sportId"]): s["sportName"] for s in call("/sports")
              if (s.get("sportName") or "").strip().lower() in WANTED}
    markets = call("/markets")
    cands = {}
    for m in markets:
        sid = str(m.get("sportId"))
        if sid in wanted and is_main(m):
            cands.setdefault(sid, []).append(m)
    mk = {}
    for sid, lst in cands.items():
        full = [m for m in lst if m.get("period") == "fulltime"]
        for m in (full or lst):
            mk.setdefault(sid, {})[str(m["marketId"])] = {
                str(o["outcomeId"]): o["outcomeName"] for o in m["outcomes"]}
    for sid, name in wanted.items():
        if sid in mk:
            print(f"{name} : marchés retenus {list(mk[sid])}")
        else:
            seen = [f'{m["marketId"]}:{m["marketName"]}/{m.get("period")}'
                    for m in markets
                    if str(m.get("sportId")) == sid and m.get("marketLength") in (2, 3)][:25]
            print(f"ATTENTION {name} : aucun marché principal trouvé. Marchés : {seen}")
    STATE["meta"] = {"sports": wanted, "markets": mk, "wanted": sorted(WANTED),
                     "names": sorted(MAIN_NAMES)}


def refresh_fixtures(now):
    for sid, name in STATE["meta"]["sports"].items():
        entry = STATE["fixtures"].get(sid)
        if entry and now - parse_date(entry["fetched"]) < timedelta(hours=REFRESH_HOURS):
            continue
        if budget_left() <= 0:
            print("Budget mensuel atteint, liste des matchs non rafraîchie.")
            return
        try:
            data = call("/fixtures", sportId=sid,
                        **{"from": now.strftime("%Y-%m-%d"),
                           "to": (now + timedelta(days=FIXTURES_DAYS)).strftime("%Y-%m-%d")},
                        statusId=0, hasOdds="true", bookmakers=ALERT_BOOKMAKER)
        except requests.HTTPError as exc:
            print(f"{name} : erreur {exc.response.status_code}, sport ignoré")
            continue
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
    if r in ("1", "home", "player 1"):
        return f["p1"]
    if r in ("2", "away", "player 2"):
        return f["p2"]
    if r in ("x", "draw"):
        return "Nul"
    return raw


def analyse(entries, start_dt):
    """Retourne (cote de référence, cote actuelle).
    Référence = dernière cote en vigueur REF_HOURS avant le match,
    sinon première cote connue."""
    rows = sorted((e for e in entries if e.get("price") is not None),
                  key=lambda e: e["createdAt"])
    active = [e for e in rows if e.get("active")]
    if not rows or not active or not rows[-1].get("active"):
        return None
    ref_time = start_dt - timedelta(hours=REF_HOURS)
    before = [e for e in active if parse_date(e["createdAt"]) <= ref_time]
    ref = before[-1] if before else active[0]
    return float(ref["price"]), float(rows[-1]["price"])


def build_series(entries, start_dt):
    rows = sorted((e for e in entries if e.get("price") is not None and e.get("active")),
                  key=lambda e: e["createdAt"])
    ref_time = start_dt - timedelta(hours=REF_HOURS)
    before = [e for e in rows if parse_date(e["createdAt"]) <= ref_time]
    after = [e for e in rows if parse_date(e["createdAt"]) > ref_time]
    pts = []
    if before:
        pts.append({"t": ref_time.isoformat(), "p": float(before[-1]["price"])})
    pts += [{"t": e["createdAt"], "p": float(e["price"])} for e in after]
    if len(pts) > MAX_POINTS:
        step = len(pts) / MAX_POINTS
        pts = [pts[int(i * step)] for i in range(MAX_POINTS)] + [pts[-1]]
    return pts


def matches_filters(o, c):
    return (OPEN_MIN <= o <= OPEN_MAX and CUR_MIN <= c <= CUR_MAX
            and round(o - c, 2) >= MIN_DROP)


def collect(mdata, labels, start_dt):
    outs = {}
    for oid, odata in (mdata.get("outcomes") or {}).items():
        if oid not in labels:
            continue
        entries = (odata.get("players") or {}).get("0", [])
        res = analyse(entries, start_dt)
        if res:
            outs[oid] = (entries, res[0], res[1])
    return outs


def ref_data(books, mid, oid, start_dt):
    out = {}
    for b in REF_BOOKMAKERS:
        markets = (books.get(b) or {}).get("markets") or {}
        odata = ((markets.get(mid) or {}).get("outcomes") or {}).get(oid)
        if not odata:
            continue
        entries = (odata.get("players") or {}).get("0", [])
        res = analyse(entries, start_dt)
        if res:
            out[b] = (entries, res[0], res[1])
    return out


def build_message(f, selection, op, cur, refs):
    drop = round(op - cur, 2)
    lines = [
        "📉 ALERTE VARIACOTE",
        f"Sport : {f['sport']}",
        f"Match : {f['p1']} - {f['p2']}",
        f"Sélection : {selection}",
        f"Bookmaker : {ALERT_BOOKMAKER}",
        f"Cote de référence : {op:.2f}",
        f"Cote actuelle : {cur:.2f}",
        f"Variation : -{drop:.2f} (-{drop / op * 100:.1f}%)"]
    if refs:
        lines.append("Autres bookmakers :")
        for b, (_, o2, c2) in refs.items():
            lines.append(f"{short(b)} : {o2:.2f} → {c2:.2f} ({c2 - o2:+.2f})")
    if APP_URL:
        lines.append(f"📊 Graphique : {APP_URL}")
    return "\n".join(lines)


def upsert_alert(key, f, labels, oid, outs, refs, now):
    start_dt = parse_date(f["start"])
    _, op, cur = outs[oid]
    drop = round(op - cur, 2)
    old = ALERTS.get(key, {})
    outcomes = []
    for o2 in sorted(outs, key=lambda x: int(x) if x.isdigit() else 0):
        e2, op2, cur2 = outs[o2]
        outcomes.append({"label": label_for(labels[o2], f), "selected": o2 == oid,
                         "open": op2, "current": cur2,
                         "series": build_series(e2, start_dt) if o2 == oid else []})
    ref_list = [{"bookmaker": b, "open": o2, "current": c2,
                 "series": build_series(e2, start_dt)}
                for b, (e2, o2, c2) in refs.items()]
    ALERTS[key] = {
        "id": key, "sport": f["sport"], "league": f.get("league", ""),
        "match": f"{f['p1']} - {f['p2']}", "selection": label_for(labels[oid], f),
        "bookmaker": ALERT_BOOKMAKER, "start": f["start"], "flash": flash_url(f),
        "open": op, "current": cur, "drop": drop, "pct": round(drop / op * 100, 1),
        "alerted_at": old.get("alerted_at") or STATE["alerted"][key],
        "updated_at": now.isoformat(), "outcomes": outcomes, "refs": ref_list}


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
        start_dt = parse_date(f["start"])
        try:
            hist = call("/historical-odds", billed=False, fixtureId=f["id"],
                        bookmakers=ALL_BOOKS)
        except Exception as exc:
            errors += 1
            print(f"Erreur {f['id']} : {exc}")
            if errors >= 3:
                break
            continue
        root = hist if "bookmakers" in hist else hist.get(f["id"], {})
        books = root.get("bookmakers") or {}
        main = books.get(ALERT_BOOKMAKER) or {}
        for mid, mdata in (main.get("markets") or {}).items():
            labels = mk.get(mid)
            if not labels:
                continue
            outs = collect(mdata, labels, start_dt)
            for oid, (entries, op, cur) in outs.items():
                key = f'{f["id"]}|{ALERT_BOOKMAKER}|{mid}|{oid}'
                refs = ref_data(books, mid, oid, start_dt)
                if key not in STATE["alerted"] and matches_filters(op, cur):
                    notify(build_message(f, label_for(labels[oid], f), op, cur, refs))
                    STATE["alerted"][key] = now.isoformat()
                    alerts += 1
                if key in STATE["alerted"]:
                    upsert_alert(key, f, labels, oid, outs, refs, now)
    print(f"{alerts} alerte(s)")


def main():
    global STATE, ALERTS
    STATE = load_state()
    ALERTS = load_alert_records()
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
