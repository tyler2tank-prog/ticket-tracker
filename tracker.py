#!/usr/bin/env python3
"""
Ticket price tracker
--------------------
Watches ticket prices on SeatGeek and Ticketmaster over time and charts how they
move as an event gets closer.

Commands
  python tracker.py poll     # take a price snapshot of every watched event that is "due"
  python tracker.py report   # rebuild docs/index.html (the chart page) from saved data
  python tracker.py demo     # make a demo report from simulated data (no API keys needed)
  python tracker.py check    # test your API keys and show what your watchlist finds

API keys come from environment variables (or a .env file next to this script):
  SEATGEEK_CLIENT_ID, SEATGEEK_CLIENT_SECRET (optional), TICKETMASTER_API_KEY
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
SG_BASE = "https://api.seatgeek.com/2"
TM_BASE = "https://app.ticketmaster.com/discovery/v2"

EVENT_FIELDS = [
    "source", "event_id", "search_name", "title", "type", "datetime_local", "datetime_utc",
    "event_weekday", "venue", "city", "state", "performers", "url", "first_seen_utc",
]
SNAP_FIELDS = [
    "snapshot_utc", "source", "event_id", "hours_to_event", "snapshot_local_weekday",
    "snapshot_local_hour", "lowest_price", "median_price", "average_price", "highest_price",
    "listing_count", "popularity", "score", "status",
]


# --------------------------------------------------------------------------- helpers
def load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def num(x):
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def load_config() -> dict:
    with open(ROOT / "watchlist.yaml") as f:
        return yaml.safe_load(f) or {}


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def interval_hours(hours_to_event: float) -> float:
    """How often to snapshot, based on how close the event is.
    Prices move most in the last few days, so we look more often then."""
    if hours_to_event > 720:   # more than 30 days out
        return 24
    if hours_to_event > 168:   # 7-30 days
        return 6
    if hours_to_event > 48:    # 2-7 days
        return 3
    if hours_to_event > 8:     # 8 hours - 2 days
        return 1
    return 0                    # final 8 hours: every run


def excluded(title: str, words: list[str]) -> bool:
    t = (title or "").lower()
    return any(w.lower() in t for w in words)


# --------------------------------------------------------------------------- SeatGeek
def sg_get(path: str, params: dict) -> dict:
    p = dict(params)
    p["client_id"] = os.environ["SEATGEEK_CLIENT_ID"]
    if os.environ.get("SEATGEEK_CLIENT_SECRET"):
        p["client_secret"] = os.environ["SEATGEEK_CLIENT_SECRET"]
    r = requests.get(SG_BASE + path, params=p, timeout=25)
    r.raise_for_status()
    return r.json()


def sg_parse(e: dict, search_name: str) -> tuple[dict, dict]:
    st = e.get("stats") or {}
    venue = e.get("venue") or {}
    dt_utc = parse_utc(e.get("datetime_utc", ""))
    dt_local = (e.get("datetime_local") or "").replace("Z", "")
    ev = {
        "source": "seatgeek",
        "event_id": str(e.get("id")),
        "search_name": search_name,
        "title": e.get("title") or e.get("short_title") or "",
        "type": e.get("type") or "",
        "datetime_local": dt_local,
        "datetime_utc": iso(dt_utc) if dt_utc else "",
        "event_weekday": weekday_of(dt_local),
        "venue": venue.get("name") or "",
        "city": venue.get("city") or "",
        "state": venue.get("state") or "",
        "performers": "; ".join(p.get("name", "") for p in (e.get("performers") or [])),
        "url": e.get("url") or "",
    }
    price = {
        "lowest_price": st.get("lowest_price"),
        "median_price": st.get("median_price"),
        "average_price": st.get("average_price"),
        "highest_price": st.get("highest_price"),
        "listing_count": st.get("listing_count") or st.get("visible_listing_count"),
        "popularity": e.get("popularity"),
        "score": e.get("score"),
        "status": e.get("status") or "",
    }
    return ev, price


def sg_search(s: dict, cfg: dict) -> list[tuple[dict, dict]]:
    look = int(s.get("look_ahead_days", cfg.get("look_ahead_days", 35)))
    start = now_utc() - timedelta(hours=3)
    p = {
        "per_page": int(s.get("max_events", cfg.get("max_events_per_search", 10))),
        "sort": "datetime_utc.asc",
        "datetime_utc.gte": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "datetime_utc.lte": (now_utc() + timedelta(days=look)).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if s.get("performer"):
        p["performers.slug"] = s["performer"]
    if s.get("query"):
        p["q"] = s["query"]
    if s.get("city"):
        p["venue.city"] = s["city"]
    if s.get("state"):
        p["venue.state"] = s["state"]
    if s.get("type"):
        p["taxonomies.name"] = s["type"]
    events = sg_get("/events", p).get("events", [])
    # A wrong performer slug returns nothing: fall back to a keyword search.
    if not events and s.get("performer") and not s.get("query"):
        p.pop("performers.slug")
        p["q"] = s["performer"].replace("-", " ")
        events = sg_get("/events", p).get("events", [])
    return [sg_parse(e, s["name"]) for e in events]


def sg_by_id(event_id: str, name: str) -> list[tuple[dict, dict]]:
    return [sg_parse(sg_get(f"/events/{event_id}", {}), name)]


# --------------------------------------------------------------------------- Ticketmaster
def tm_get(path: str, params: dict) -> dict:
    p = dict(params)
    p["apikey"] = os.environ["TICKETMASTER_API_KEY"]
    for attempt in range(3):
        r = requests.get(TM_BASE + path, params=p, timeout=25)
        if r.status_code == 429:          # rate limited: wait and retry
            time.sleep(2 + attempt * 2)
            continue
        r.raise_for_status()
        time.sleep(0.25)                  # stay under ~5 requests/second
        return r.json()
    r.raise_for_status()
    return {}


def tm_parse(e: dict, search_name: str) -> tuple[dict, dict]:
    dates = (e.get("dates") or {}).get("start") or {}
    dt_utc = parse_utc(dates.get("dateTime", ""))
    dt_local = ""
    if dates.get("localDate"):
        dt_local = dates["localDate"] + "T" + (dates.get("localTime") or "00:00:00")
    venue = ((e.get("_embedded") or {}).get("venues") or [{}])[0]
    ranges = e.get("priceRanges") or []
    lows = [r.get("min") for r in ranges if r.get("min") is not None]
    highs = [r.get("max") for r in ranges if r.get("max") is not None]
    cls = (e.get("classifications") or [{}])[0]
    attractions = ((e.get("_embedded") or {}).get("attractions") or [])
    ev = {
        "source": "ticketmaster",
        "event_id": e.get("id", ""),
        "search_name": search_name,
        "title": e.get("name", ""),
        "type": ((cls.get("segment") or {}).get("name") or "").lower(),
        "datetime_local": dt_local,
        "datetime_utc": iso(dt_utc) if dt_utc else "",
        "event_weekday": weekday_of(dt_local),
        "venue": venue.get("name", ""),
        "city": (venue.get("city") or {}).get("name", ""),
        "state": (venue.get("state") or {}).get("stateCode", ""),
        "performers": "; ".join(a.get("name", "") for a in attractions),
        "url": e.get("url", ""),
    }
    price = {
        "lowest_price": min(lows) if lows else None,
        "highest_price": max(highs) if highs else None,
        "status": ((e.get("dates") or {}).get("status") or {}).get("code", ""),
    }
    return ev, price


def tm_search(s: dict, cfg: dict) -> list[tuple[dict, dict]]:
    look = int(s.get("look_ahead_days", cfg.get("look_ahead_days", 35)))
    p = {
        "size": int(s.get("max_events", cfg.get("max_events_per_search", 10))) * 2,
        "sort": "date,asc",
        "startDateTime": iso(now_utc() - timedelta(hours=3)),
        "endDateTime": iso(now_utc() + timedelta(days=look)),
    }
    for key, param in [("keyword", "keyword"), ("city", "city"), ("state", "stateCode"),
                       ("type", "classificationName")]:
        if s.get(key):
            p[param] = s[key]
    data = tm_get("/events.json", p)
    events = (data.get("_embedded") or {}).get("events", [])
    out = [tm_parse(e, s["name"]) for e in events]
    limit = int(s.get("max_events", cfg.get("max_events_per_search", 10)))
    return out[: limit * 2]


# --------------------------------------------------------------------------- poll
def weekday_of(local: str) -> str:
    try:
        return datetime.fromisoformat(local).strftime("%a")
    except (ValueError, TypeError):
        return ""


def snapshot_row(ev: dict, price: dict, t: datetime) -> dict | None:
    start = parse_utc(ev["datetime_utc"])
    if not start:
        return None
    hours = (start - t).total_seconds() / 3600
    # local time of the snapshot, in the event's own timezone
    try:
        offset = datetime.fromisoformat(ev["datetime_local"]) - start.replace(tzinfo=None)
        local = t.replace(tzinfo=None) + offset
        wd, hr = local.strftime("%a"), local.hour
    except (ValueError, TypeError):
        wd, hr = "", ""
    return {
        "snapshot_utc": iso(t), "source": ev["source"], "event_id": ev["event_id"],
        "hours_to_event": round(hours, 2), "snapshot_local_weekday": wd,
        "snapshot_local_hour": hr, **{k: price.get(k) for k in SNAP_FIELDS if k in price},
    }


def cmd_poll(data_dir: Path, force: bool = False) -> None:
    load_dotenv()
    cfg = load_config()
    excl = cfg.get("exclude_words", [])
    have_sg = bool(os.environ.get("SEATGEEK_CLIENT_ID"))
    have_tm = bool(os.environ.get("TICKETMASTER_API_KEY"))
    if not (have_sg or have_tm):
        sys.exit("No API keys found. Set SEATGEEK_CLIENT_ID and/or TICKETMASTER_API_KEY (see README).")

    found: list[tuple[dict, dict]] = []
    sg_cfg = cfg.get("seatgeek") or {}
    tm_cfg = cfg.get("ticketmaster") or {}
    if have_sg:
        for s in sg_cfg.get("searches") or []:
            try:
                found += sg_search(s, cfg)
            except requests.RequestException as e:
                print(f"  ! SeatGeek search '{s.get('name')}' failed: {e}")
        for item in sg_cfg.get("event_ids") or []:
            try:
                found += sg_by_id(str(item), "Pinned events")
            except requests.RequestException as e:
                print(f"  ! SeatGeek event {item} failed: {e}")
    if have_tm:
        for s in tm_cfg.get("searches") or []:
            try:
                found += tm_search(s, cfg)
            except requests.RequestException as e:
                print(f"  ! Ticketmaster search '{s.get('name')}' failed: {e}")

    t = now_utc()
    events_path, snaps_path = data_dir / "events.csv", data_dir / "snapshots.csv"
    events = {(r["source"], r["event_id"]): r for r in read_csv(events_path)}
    last: dict[tuple, datetime] = {}
    for r in read_csv(snaps_path):
        k = (r["source"], r["event_id"])
        ts = parse_utc(r["snapshot_utc"])
        if ts and (k not in last or ts > last[k]):
            last[k] = ts

    new_rows, seen = [], set()
    for ev, price in found:
        k = (ev["source"], ev["event_id"])
        if k in seen or excluded(ev["title"], excl):
            continue
        seen.add(k)
        if k not in events:
            ev["first_seen_utc"] = iso(t)
            events[k] = ev
        else:
            events[k].update({f: ev[f] for f in EVENT_FIELDS if f in ev and f != "first_seen_utc"})
        row = snapshot_row(ev, price, t)
        if not row or row["hours_to_event"] < -3:
            continue
        gap = interval_hours(row["hours_to_event"])
        age = (t - last[k]).total_seconds() / 3600 if k in last else None
        if force or age is None or age >= gap * 0.9:
            new_rows.append(row)

    write_csv(events_path, EVENT_FIELDS, sorted(events.values(), key=lambda r: r.get("datetime_utc", "")))
    append_csv(snaps_path, SNAP_FIELDS, new_rows)
    print(f"{iso(t)}  found {len(seen)} events, saved {len(new_rows)} new snapshots")
    no_stats = [r for r in new_rows if r["source"] == "seatgeek" and r.get("lowest_price") is None]
    if no_stats and len(no_stats) == len([r for r in new_rows if r["source"] == "seatgeek"]):
        print("  ! SeatGeek returned no price stats. Your key may not include pricing; see README > Troubleshooting.")


def cmd_check() -> None:
    load_dotenv()
    cfg = load_config()
    print("SeatGeek key:     ", "found" if os.environ.get("SEATGEEK_CLIENT_ID") else "MISSING")
    print("Ticketmaster key: ", "found" if os.environ.get("TICKETMASTER_API_KEY") else "MISSING")
    if os.environ.get("SEATGEEK_CLIENT_ID"):
        for s in (cfg.get("seatgeek") or {}).get("searches") or []:
            try:
                res = sg_search(s, cfg)
                print(f"\n[SeatGeek] {s['name']}: {len(res)} events")
                for ev, pr in res[:5]:
                    print(f"   {ev['datetime_local'][:16]}  {ev['title'][:55]:55}  lowest {pr['lowest_price']}  listings {pr['listing_count']}")
            except requests.RequestException as e:
                print(f"\n[SeatGeek] {s['name']}: ERROR {e}")
    if os.environ.get("TICKETMASTER_API_KEY"):
        for s in (cfg.get("ticketmaster") or {}).get("searches") or []:
            try:
                res = [x for x in tm_search(s, cfg) if not excluded(x[0]["title"], cfg.get("exclude_words", []))]
                print(f"\n[Ticketmaster] {s['name']}: {len(res)} events")
                for ev, pr in res[:5]:
                    print(f"   {ev['datetime_local'][:16]}  {ev['title'][:55]:55}  face value {pr['lowest_price']}-{pr['highest_price']}")
            except requests.RequestException as e:
                print(f"\n[Ticketmaster] {s['name']}: ERROR {e}")


# --------------------------------------------------------------------------- report
TYPICAL_BUCKETS = [168, 144, 120, 96, 72, 48, 36, 24, 18, 12, 8, 6, 4, 3, 2, 1, 0]


def tokens(s: str) -> set[str]:
    stop = {"the", "at", "vs", "v", "and", "&", "tour", "live", "with", "presents", "-", "football"}
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in stop and len(w) > 1}


def match_tm_to_sg(events: list[dict]) -> dict[tuple, tuple]:
    """Link a Ticketmaster event to the same SeatGeek event (same day, same city, similar name)."""
    sg = [e for e in events if e["source"] == "seatgeek"]
    links = {}
    for tm in (e for e in events if e["source"] == "ticketmaster"):
        best, best_score = None, 0.0
        for s in sg:
            if tm["datetime_local"][:10] != s["datetime_local"][:10]:
                continue
            if tm["city"] and s["city"] and tm["city"].lower() != s["city"].lower():
                continue
            a, b = tokens(tm["title"] + " " + tm["performers"]), tokens(s["title"] + " " + s["performers"])
            score = len(a & b) / max(1, len(a | b))
            if score > best_score:
                best, best_score = s, score
        if best and best_score >= 0.2:
            links[(tm["source"], tm["event_id"])] = (best["source"], best["event_id"])
    return links


def value_near(points: list[tuple], h: float, tol: float):
    """Lowest price at the snapshot closest to h hours before the event (within tol hours)."""
    best = None
    for ph, low in points:
        if low is None:
            continue
        d = abs(ph - h)
        if d <= tol and (best is None or d < best[0]):
            best = (d, low)
    return best[1] if best else None


def build_report(data_dir: Path, out_path: Path, title_note: str = "") -> None:
    events = read_csv(data_dir / "events.csv")
    snaps = read_csv(data_dir / "snapshots.csv")
    by_key: dict[tuple, list[dict]] = {}
    for r in snaps:
        by_key.setdefault((r["source"], r["event_id"]), []).append(r)
    for rows in by_key.values():
        rows.sort(key=lambda r: r["snapshot_utc"])

    links = match_tm_to_sg(events)
    ev_by_key = {(e["source"], e["event_id"]): e for e in events}
    cards = []
    for e in events:
        k = (e["source"], e["event_id"])
        if k in links and links[k] in by_key:
            continue  # shown inside its matched SeatGeek card
        rows = by_key.get(k, [])
        if not rows:
            continue
        pts = [[num(r["hours_to_event"]), num(r["lowest_price"]), num(r["median_price"]),
                num(r["average_price"]), num(r["listing_count"]), r["snapshot_utc"]] for r in rows]
        face = []
        for tmk, sgk in links.items():
            if sgk == k:
                face = [[num(r["hours_to_event"]), num(r["lowest_price"]), num(r["highest_price"]),
                         r["snapshot_utc"]] for r in by_key.get(tmk, [])]
                e = {**e, "tm_url": ev_by_key[tmk]["url"]}
        cards.append({"key": "|".join(k), "source": e["source"], "title": e["title"],
                      "venue": e["venue"], "city": e["city"], "state": e["state"],
                      "date_local": e["datetime_local"], "weekday": e.get("event_weekday", ""),
                      "group": e.get("search_name", ""), "url": e["url"], "tm_url": e.get("tm_url", ""),
                      "points": pts, "face": face})

    # Typical curve: lowest price at each point before the event, as % of the price 7 days out.
    typical = []
    curves = []
    for c in cards:
        p = [(x[0], x[1]) for x in c["points"] if x[0] is not None]
        base = value_near(p, 168, 24)
        if base:
            curves.append((p, base))
    for h in TYPICAL_BUCKETS:
        tol = max(0.75, h * 0.15)
        vals = [100 * v / base for p, base in curves if (v := value_near(p, h, tol))]
        if vals:
            typical.append({"h": h, "idx": round(sum(vals) / len(vals), 1), "n": len(vals)})

    payload = {"generated_utc": iso(now_utc()), "note": title_note, "events": cards, "typical": typical}
    html = (ROOT / "report_template.html").read_text()
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html.replace("/*__DATA__*/null", blob))
    print(f"Report written: {out_path}  ({len(cards)} events)")


# --------------------------------------------------------------------------- demo
def cmd_demo() -> None:
    """Simulate 30 days of snapshots for a few events so you can see the report."""
    random.seed(7)
    demo_dir = ROOT / "demo" / "data"
    for f in demo_dir.glob("*.csv"):
        f.unlink()
    end = now_utc().replace(minute=0, second=0)
    specs = [
        # title, venue, city, state, base low, base median, listings, demand shape
        ("Philadelphia Eagles at Dallas Cowboys", "AT&T Stadium", "Arlington", "TX", 235, 410, 5200, "hot"),
        ("Kansas State Wildcats at TCU Horned Frogs", "Amon G. Carter Stadium", "Fort Worth", "TX", 38, 72, 2600, "soft"),
        ("Jonas Brothers", "Chase Center", "San Francisco", "CA", 96, 185, 1400, "soldout"),
        ("Utah Utes at TCU Horned Frogs", "Amon G. Carter Stadium", "Fort Worth", "TX", 31, 60, 2900, "soft"),
    ]
    events, snaps = [], []
    for i, (title, venue, city, state, low0, med0, lc0, shape) in enumerate(specs):
        start = end + timedelta(hours=2 + i * 5)
        local = start - timedelta(hours=6 if state == "TX" else 8)
        ev = {"source": "seatgeek", "event_id": f"demo{i}", "search_name": "Demo", "title": title,
              "type": "concert" if "Jonas" in title else "football",
              "datetime_local": local.strftime("%Y-%m-%dT%H:%M:%S"),
              "datetime_utc": iso(start), "event_weekday": local.strftime("%a"), "venue": venue,
              "city": city, "state": state, "performers": title, "url": "https://seatgeek.com",
              "first_seen_utc": iso(start - timedelta(days=30))}
        events.append(ev)
        tm = {**ev, "source": "ticketmaster", "event_id": f"tmdemo{i}", "url": "https://ticketmaster.com"}
        events.append(tm)
        t, last_t, drift = start - timedelta(days=30), None, 0.0
        while t <= start:
            h = (start - t).total_seconds() / 3600
            if last_t is None or (t - last_t).total_seconds() / 3600 >= interval_hours(h) * 0.9:
                d = h / 24
                drift += random.gauss(0, 0.012)
                if shape == "hot":      # rises into the last week, small dip in the final hours
                    f = 1 + 0.18 * math.exp(-d / 6) - (0.10 if h < 6 else 0)
                elif shape == "soft":   # sellers panic late: big drop on game day
                    f = 1 - 0.05 * (1 - d / 30) - 0.38 * math.exp(-h / 14)
                else:                   # sold out at face: climbs steadily
                    f = 1 + 0.35 * (1 - d / 30) ** 2
                f *= 1 + drift + random.gauss(0, 0.02)
                lc = lc0 * (0.7 + 0.5 * math.sin(min(1, (30 - d) / 26) * math.pi / 2)) * (0.55 if h < 24 and shape != "soft" else 1)
                row = snapshot_row(ev, {}, t)
                row.update({"lowest_price": round(low0 * f), "median_price": round(med0 * (0.5 + f / 2)),
                            "average_price": round(med0 * 1.12 * (0.5 + f / 2)), "highest_price": round(med0 * 9),
                            "listing_count": int(lc * random.uniform(0.96, 1.04)), "status": "normal"})
                snaps.append(row)
                if t.hour % 6 == 0 or h < 48:
                    trow = snapshot_row(tm, {}, t)
                    trow.update({"lowest_price": round(low0 * 0.82), "highest_price": round(med0 * 3.5)})
                    snaps.append(trow)
                last_t = t
            t += timedelta(minutes=30)
    write_csv(demo_dir / "events.csv", EVENT_FIELDS, events)
    write_csv(demo_dir / "snapshots.csv", SNAP_FIELDS, snaps)
    build_report(demo_dir, ROOT / "demo" / "demo-report.html",
                 "Demo: simulated prices. Real data appears once your API keys are set.")


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["poll", "report", "demo", "check"])
    ap.add_argument("--force", action="store_true", help="poll: snapshot every event now, ignoring the schedule")
    a = ap.parse_args()
    data_dir = ROOT / "data"
    if a.command == "poll":
        cmd_poll(data_dir, a.force)
    elif a.command == "report":
        build_report(data_dir, ROOT / "docs" / "index.html")
    elif a.command == "demo":
        cmd_demo()
    elif a.command == "check":
        cmd_check()


if __name__ == "__main__":
    main()
