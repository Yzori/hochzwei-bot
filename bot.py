"""Hochzwei Luzern viewing sign-up bot. See hochzwei-viewing-bot.md.

Usage:
  python bot.py            one pass
  python bot.py --loop     poll forever
  python bot.py --dry-run  one pass, never POST (only log + notify)
"""
import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
RAW_DIR = HERE / "raw"

# Personal details never go in the repo: they come from config.local.json (local, gitignored)
# or from environment variables (GitHub Actions secrets).
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
_local = HERE / "config.local.json"
if _local.exists():
    _l = json.loads(_local.read_text(encoding="utf-8"))
    CONFIG["contact"].update(_l.pop("contact", {}))
    CONFIG.update(_l)
for _key, _env in (("firstName", "CONTACT_FIRST_NAME"), ("lastName", "CONTACT_LAST_NAME"),
                   ("email", "CONTACT_EMAIL"), ("phone", "CONTACT_PHONE")):
    if os.environ.get(_env):
        CONFIG["contact"][_key] = os.environ[_env]
if os.environ.get("NTFY_TOPIC"):
    CONFIG["ntfy_topic"] = os.environ["NTFY_TOPIC"]

LISTING_URL = "https://hochzwei-luzern.ch/wohnungen/"
API = "https://api.wincasa.ch/fn-crm-ext-prd-chn/v1/"
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

_SECRETS = [v for k, v in CONFIG["contact"].items() if k != "language" and v] + [CONFIG.get("ntfy_topic") or ""]


def redact(text):
    """Strip personal details: Actions logs and committed state are public."""
    text = str(text)
    for s in _SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


class RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(HERE / "bot.log", encoding="utf-8")],
)
log = logging.getLogger("bot")
log.addFilter(RedactFilter())
session = requests.Session()
session.headers.update(HEADERS)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


STATE_KEYS = ("booked", "waitlisted", "seen_states", "known_units", "notified_slots")


def save_state(state):
    STATE_FILE.write_text(redact(json.dumps(state, indent=2, ensure_ascii=False)), encoding="utf-8")


def save_raw(name, data):
    RAW_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (RAW_DIR / f"{stamp}-{name}.json").write_text(redact(json.dumps(data, indent=2, ensure_ascii=False)),
                                                  encoding="utf-8")


def notify(title, message, priority="default"):
    log.info("NOTIFY %s: %s", title, message)
    topic = CONFIG.get("ntfy_topic")
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": priority},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("ntfy failed: %s", e)


def fetch_units():
    """Return [{unit, rooms, floor, area, rent, status}] from the listing page."""
    r = session.get(LISTING_URL, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    units = []
    for a in soup.find_all("a", href=lambda h: h and "viewing/show/" in h):
        unit = a["href"].rstrip("/").rsplit("/", 1)[1]
        detail = a.find_parent("tr", class_="immo-detail-row")
        main = detail.find_previous_sibling("tr") if detail else None
        cells = [td.get_text(" ", strip=True) for td in main.find_all("td", recursive=False)] if main else []
        if len(cells) < 7:
            log.warning("Unexpected row layout for %s: %s", unit, cells)
            continue
        units.append({"unit": unit, "rooms": cells[1], "floor": cells[2], "area": cells[3],
                      "status": cells[5], "rent": cells[6]})
    return units


def get_viewing(unit):
    r = session.get(API + f"vermietung-by-reference/{unit}", timeout=20)
    r.raise_for_status()
    return r.json()


def appointment_id(appt):
    for key in ("appointmentId", "id", "viewingAppointmentId", "besichtigungsterminId"):
        if appt.get(key):
            return appt[key]
    return None


def appointment_time(appt):
    """Best-effort start time for sorting/notification; field name unconfirmed."""
    for key, val in appt.items():
        if isinstance(val, str) and re.match(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", val):
            if any(k in key.lower() for k in ("start", "from", "von", "date", "datum", "time")):
                try:
                    return datetime.strptime(val, "%d.%m.%Y %H:%M"), val
                except ValueError:
                    pass
    return datetime.max, "?"


def has_capacity(appt):
    for key in ("isFullyBooked", "fullyBooked", "isFull"):
        if appt.get(key) is True:
            return False
    for key in ("freeSlots", "availableSlots", "remainingSlots"):
        if isinstance(appt.get(key), int) and appt[key] <= 0:
            return False
    return True


def book(appt_id, dry_run):
    body = {"appointmentId": appt_id, **CONFIG["contact"]}
    if dry_run or CONFIG["mode"] != "book":
        log.info("Would POST appointment-signup/%s %s", appt_id, body)
        return None
    r = session.post(API + f"appointment-signup/{appt_id}", json=body, timeout=20)
    log.info("appointment-signup -> %s %s", r.status_code, r.text[:500])
    return r


def join_waitlist(viewing_id, dry_run):
    body = {"viewingId": viewing_id, "email": CONFIG["contact"]["email"],
            "language": CONFIG["contact"]["language"]}
    if dry_run:
        log.info("Would POST waitlist-signup/%s %s", viewing_id, body)
        return None
    r = session.post(API + f"waitlist-signup/{viewing_id}", json=body, timeout=20)
    log.info("waitlist-signup -> %s %s", r.status_code, r.text[:500])
    return r


def should_book(u):
    try:
        floor = int(u["floor"])
    except ValueError:
        return False
    return u["rooms"] in CONFIG["book_rooms"] and floor > CONFIG["book_above_floor"]


def handle_unit(u, state, dry_run):
    unit = u["unit"]
    label = f"{unit} ({u['rooms']} Zi, {u['floor']}. OG, {u['area']}, {u['rent']})"
    link = f"https://www.mywincasa.ch/viewing/show/{unit}"
    v = get_viewing(unit)
    vstate = v.get("viewingState")
    appts = v.get("viewingAppointments") or []

    prev = state["seen_states"].get(unit)
    if prev != vstate:
        log.info("%s viewingState %s -> %s", unit, prev, vstate)
        save_raw(f"{unit}-{vstate}", v)
        state["seen_states"][unit] = vstate
        if prev is not None:
            notify("Hochzwei: status change", f"{label}: {prev} -> {vstate}. {link}", "high")

    if unit in state["booked"]:
        return

    if appts and not should_book(u):
        ids = sorted(str(appointment_id(a)) for a in appts)
        if state["notified_slots"].get(unit) != ids:
            save_raw(f"{unit}-appointments", v)
            times = ", ".join(appointment_time(a)[1] for a in appts)
            notify("Hochzwei: viewing slots open", f"{label}: {len(appts)} slot(s) [{times}]. {link}", "high")
            state["notified_slots"][unit] = ids
        return

    if appts:
        save_raw(f"{unit}-appointments", v)
        open_appts = sorted((a for a in appts if has_capacity(a)), key=lambda a: appointment_time(a)[0])
        for appt in open_appts:
            appt_id = appointment_id(appt)
            if not appt_id:
                notify("Hochzwei: slots found, unknown format",
                       f"{label}: couldn't find appointment id, book manually: "
                       f"https://www.mywincasa.ch/viewing/show/{unit}", "urgent")
                return
            when = appointment_time(appt)[1]
            r = book(appt_id, dry_run)
            if r is None:
                notify("Hochzwei: slot open", f"{label} at {when} (not booked, mode={CONFIG['mode']}"
                       f"{', dry-run' if dry_run else ''}). https://www.mywincasa.ch/viewing/show/{unit}", "urgent")
                return
            if r.ok:
                state["booked"][unit] = {"appointmentId": appt_id, "when": when,
                                         "at": datetime.now().isoformat(timespec="seconds"),
                                         "response": r.text[:1000]}
                notify("Hochzwei: viewing BOOKED", f"{label} at {when}. Check your email for confirmation.","urgent")
                return
            notify("Hochzwei: booking failed", f"{label} at {when}: HTTP {r.status_code} {r.text[:200]}. "
                   f"Book manually: https://www.mywincasa.ch/viewing/show/{unit}", "urgent")
            # try the next slot
        return

    if (should_book(u) and vstate == "FULLY_BOOKED" and CONFIG.get("join_waitlist")
            and unit not in state["waitlisted"]
            and v.get("id")):
        r = join_waitlist(v["id"], dry_run)
        if r is None:
            return
        if r.ok:
            state["waitlisted"][unit] = {"viewingId": v["id"], "at": datetime.now().isoformat(timespec="seconds")}
            notify("Hochzwei: waitlisted", f"{label} is fully booked; joined waitlist.")
        else:
            state["waitlisted"][unit] = {"failed": r.status_code, "response": r.text[:500]}
            notify("Hochzwei: waitlist failed", f"{label}: HTTP {r.status_code} {r.text[:200]}")


def run_once(state, dry_run):
    for key in STATE_KEYS:
        state.setdefault(key, {})
    units = fetch_units()
    log.info("%d unit(s): %s", len(units),
             ", ".join(f"{u['unit']} {u['rooms']}Zi {u['floor']}OG{' [BOOK]' if should_book(u) else ''}"
                       for u in units) or "-")
    first_run = not state["known_units"]
    new = [u for u in units if u["unit"] not in state["known_units"]]
    if new:
        lines = "\n".join(f"{u['unit']}: {u['rooms']} Zi, {u['floor']}. OG, {u['area']}, {u['rent']}"
                          f"{' (auto-book)' if should_book(u) else ''}" for u in new)
        notify("Hochzwei: current listings" if first_run else "Hochzwei: NEW apartment listed", lines,
               "default" if first_run else "high")
        for u in new:
            state["known_units"][u["unit"]] = datetime.now().isoformat(timespec="seconds")
    for u in units:
        try:
            handle_unit(u, state, dry_run)
        except requests.RequestException as e:
            log.warning("%s: %s", u["unit"], e)
        save_state(state)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    missing = [k for k in ("firstName", "lastName", "email", "phone") if not CONFIG["contact"].get(k)]
    if missing:
        sys.exit(f"Missing contact details: {missing} (set config.local.json or CONTACT_* env vars)")

    state = load_state()
    if not args.loop:
        run_once(state, args.dry_run)
        return
    notify("Hochzwei bot started", f"mode={CONFIG['mode']}, auto-book {CONFIG['book_rooms']} rooms above floor "
           f"{CONFIG['book_above_floor']}", "low")
    failures = 0
    while True:
        try:
            run_once(state, args.dry_run)
            failures = 0
        except Exception as e:  # keep looping on any error
            failures += 1
            log.exception("run failed")
            if failures == 5:
                notify("Hochzwei bot: errors", f"5 failed runs in a row: {e}", "high")
        time.sleep(CONFIG["poll_seconds"] + random.uniform(-15, 15))


if __name__ == "__main__":
    main()
