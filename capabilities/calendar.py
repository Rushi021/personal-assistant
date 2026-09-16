"""Calendar — the day's work, made visible (Apple iCloud over CalDAV).

One rule decides what lands here, and it is a distinction the schema already
makes (D5 note 0):

    planned_on set   -> you intend to DO it on a day   -> it goes on the calendar
    due_on only      -> the world wants it by a date   -> it does not

"finish this by the end of the week" is a deadline, not a block of time, and
putting it on a day you did not choose would be the assistant inventing a plan
you never agreed to.

Discovery is a one-time manual step, which is what keeps the runtime small:

    python -m capabilities.calendar

prints your calendars; put the one you want in ICLOUD_CALENDAR_URL. After that
this module only ever issues PUT and DELETE against a URL it derives from the
task id — no discovery, no XML, no stored event ids.
"""

import logging
import os
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("assistant.calendar")

USER = os.environ.get("ICLOUD_USERNAME", "")
PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "")
CALENDAR_URL = os.environ.get("ICLOUD_CALENDAR_URL", "")
ROOT = "https://caldav.icloud.com"

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"

PROMPT = """\
CALENDAR
A task with planned_on appears on the user's calendar automatically; a task
with only a due_on does not. Never mention syncing, and never offer to add
something to the calendar — setting planned_on is what puts it there.
planned_at ("14:30", 24-hour local) makes it a timed block of estimate_min
minutes instead of an all-day entry. Only set it when the user names a time.
"""

TOOLS: list = []  # nothing for the model to call — this capability reacts to writes


def configured() -> bool:
    return bool(USER and PASSWORD and CALENDAR_URL)


def wanted(task: dict) -> bool:
    """Does this task belong on the calendar? The whole rule, in one place so
    that nothing — including a test — can hold a second copy of it.

    planned_on is an intention to spend time on a day; due_on is a deadline.
    "finish this by the end of the week" is not a block of time, and putting it
    on a day the user never chose would be inventing a plan for them.
    """
    return bool(task.get("planned_on")) and task.get("status") in ("todo", "in progress")


# --- the ICS half -----------------------------------------------------------

def _esc(text: str) -> str:
    """RFC 5545 §3.3.11. An unescaped comma silently truncates the summary."""
    return (text.replace("\\", "\\\\").replace(";", r"\;")
                .replace(",", r"\,").replace("\n", r"\n"))


def _fold(line: str) -> str:
    """Lines cap at 75 octets; continuations start with a single space."""
    out, raw = [], line.encode()
    while len(raw) > 75:
        cut = 75
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:  # never split a UTF-8 char
            cut -= 1
        out.append(raw[:cut].decode())
        raw = b" " + raw[cut:]
    out.append(raw.decode())
    return "\r\n".join(out)


def _ics(task: dict, tz: str) -> str:
    """All-day unless meta.planned_at names a time. Timed events are written in
    UTC so the file needs no VTIMEZONE block to be unambiguous."""
    zone = ZoneInfo(tz)
    day = datetime.strptime(task["planned_on"], "%Y-%m-%d").date()
    at = (task.get("meta") or {}).get("planned_at")

    if at:
        start = datetime.combine(day, datetime.strptime(at, "%H:%M").time(), tzinfo=zone)
        end = start + timedelta(minutes=task.get("estimate_min") or 25)
        when = [f"DTSTART:{start.astimezone(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}",
                f"DTEND:{end.astimezone(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}"]
    else:
        when = [f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}"]

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//kairos//assistant//EN",
        "BEGIN:VEVENT",
        f"UID:{task['id']}",
        f"DTSTAMP:{datetime.now(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{_esc(task['title'])}",
        *when,
    ]
    if task.get("notes"):
        lines.append(f"DESCRIPTION:{_esc(task['notes'])}")
    if task.get("category"):
        lines.append(f"CATEGORIES:{_esc(task['category'])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


# --- the one entry point ----------------------------------------------------

def sync(task: dict, tz: str) -> None:
    """Make the calendar agree with this task row. Idempotent, and derives the
    event URL from the task id so there is no event id to store and drift.

    Never raises. A calendar that is down must not fail a task write — capture
    is never blocked (D8), and that applies to the write path too.
    """
    if not configured() or not task or not task.get("id"):
        return

    url = f"{CALENDAR_URL.rstrip('/')}/{task['id']}.ics"
    keep = wanted(task)
    try:
        with httpx.Client(timeout=15, auth=(USER, PASSWORD), follow_redirects=True) as http:
            if keep:
                r = http.put(url, content=_ics(task, tz).encode(),
                             headers={"Content-Type": "text/calendar; charset=utf-8"})
            else:
                # Unplanned, done or dropped: it should not be on the calendar.
                r = http.delete(url)
                if r.status_code == 404:
                    return  # never been there; nothing to undo
            if r.status_code >= 400:
                log.error("calendar %s %s -> %s %s",
                          "put" if keep else "delete", task["id"], r.status_code, r.text[:200])
    except Exception as e:
        log.error("calendar sync failed for %s: %s", task.get("id"), e)


# --- one-time discovery -----------------------------------------------------

def _propfind(http: httpx.Client, url: str, body: str, depth: str = "0") -> ET.Element:
    r = http.request("PROPFIND", url, content=body.encode(),
                     headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
    r.raise_for_status()
    return ET.fromstring(r.text)


def discover() -> None:
    """Walk current-user-principal -> calendar-home-set -> the calendar list."""
    if not (USER and PASSWORD):
        print("Set ICLOUD_USERNAME and ICLOUD_APP_PASSWORD in .env first.")
        print("App-specific password: appleid.apple.com -> Sign-In and Security.")
        return

    with httpx.Client(timeout=30, auth=(USER, PASSWORD), follow_redirects=True) as http:
        tree = _propfind(http, ROOT,
                         '<d:propfind xmlns:d="DAV:"><d:prop>'
                         "<d:current-user-principal/></d:prop></d:propfind>")
        principal = tree.find(f".//{DAV}current-user-principal/{DAV}href")
        if principal is None:
            print("No principal returned — check the Apple ID and app password.")
            return
        principal_url = httpx.URL(ROOT).join(principal.text)
        print(f"principal: {principal_url}")

        tree = _propfind(http, str(principal_url),
                         '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                         "<d:prop><c:calendar-home-set/></d:prop></d:propfind>")
        home = tree.find(f".//{CAL}calendar-home-set/{DAV}href")
        home_url = httpx.URL(str(principal_url)).join(home.text)
        print(f"home:      {home_url}\n")

        tree = _propfind(http, str(home_url),
                         '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                         "<d:prop><d:displayname/><d:resourcetype/>"
                         "<c:supported-calendar-component-set/></d:prop></d:propfind>", depth="1")
        print("Calendars you can write EVENTS to — copy one into ICLOUD_CALENDAR_URL:\n")
        for resp in tree.findall(f"{DAV}response"):
            if resp.find(f".//{DAV}resourcetype/{CAL}calendar") is None:
                continue
            # iCloud marks reminder lists as calendars too. Only a collection
            # that accepts VEVENT can hold a task block; a VTODO list cannot.
            comps = [c.get("name") for c in
                     resp.findall(f".//{CAL}supported-calendar-component-set/{CAL}comp")]
            if comps and "VEVENT" not in comps:
                continue
            name = resp.find(f".//{DAV}displayname")
            href = resp.find(f"{DAV}href")
            label = (name.text if name is not None and name.text else "(unnamed)")
            print(f"  {label:28} {httpx.URL(str(home_url)).join(href.text)}")


if __name__ == "__main__":
    discover()
