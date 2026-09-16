"""Gmail — the hourly poll, the router, and the two lanes' write paths (Phase 1b).

Same shape as capabilities/calendar.py: env-var config, a `configured()` guard,
the raw protocol over httpx, and a one-time setup step you run by hand:

    python -m capabilities.gmail

prints a consent URL, takes the code back, and prints the refresh token to put
in .env. After that this module only ever issues two GETs.

TOOLS is empty on purpose. Nothing here is model-callable, and the poll path
never touches the chat model — the lanes are regex, not tokens.

Two facts about correctness, both of them database guarantees rather than code
that has to remember to be careful:

  - email_events (user_id, gmail_message_id) is unique. The poll claims a
    message by inserting there, so a re-listed or retried message never acts
    twice, whatever the lane decided.
  - expenses (user_id, source_ref) is unique. A payment cannot produce two
    rows even if the event ledger were somehow bypassed.

Card SWIPE alerts ("you spent $42 at...") are a different capability, still
blocked on E1 in PLAN-EXPENSES.md. This file handles card BILL PAYMENTS —
settlements, kind='cc_payment' — and job application emails. See PLAN-GMAIL.md.
"""

import base64
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone
from html import unescape

import httpx

from core import audit, db

from . import gmail_parse

log = logging.getLogger("assistant.gmail")

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# The whole cursor. There is no stored historyId: Gmail has no history on a
# first run and discards it after about a week, so the catch-up query gets
# written either way — and a cursor that silently expires while the app is
# down skips a window without saying so. One query, no state, and
# email_events is what stops the overlap becoming duplicate work.
#
# ponytail: a fixed 2-day window tolerates a 2-day outage. Widen the constant
# if the app is ever down longer; add a cursor only when polling many mailboxes
# makes the re-listing cost real.
QUERY = "newer_than:2d -in:chats -in:sent -in:drafts"
MAX_RESULTS = 100

TOOLS: list = []   # nothing for the model to call — this capability polls


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)


# --- the client -------------------------------------------------------------

_token: tuple[str, float] | None = None   # (access_token, expires_at_epoch)


def _access_token() -> str:
    """Refresh-token grant, cached until a minute before it expires. One
    refresh an hour, which is what the tick needs and nothing more."""
    global _token
    now = datetime.now(timezone.utc).timestamp()
    if _token and _token[1] > now:
        return _token[0]
    r = httpx.post(TOKEN_URL, timeout=30, data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token"})
    r.raise_for_status()
    body = r.json()
    _token = (body["access_token"], now + body.get("expires_in", 3600) - 60)
    return _token[0]


def _api(path: str, **params) -> dict:
    r = httpx.get(f"{API}{path}", timeout=30, params=params,
                  headers={"Authorization": f"Bearer {_access_token()}"})
    r.raise_for_status()
    return r.json()


def _list_ids() -> list[str]:
    page = _api("/messages", q=QUERY, maxResults=MAX_RESULTS)
    return [m["id"] for m in page.get("messages", [])]


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", errors="replace")


_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
MAX_BODY = 20_000


def _text(payload: dict) -> str:
    """The decoded body, text/plain preferred. Walks the MIME tree because a
    multipart/alternative hides the plain part one level down.

    This is handed to the parsers and then dropped. It is never persisted:
    evaluation_plan.md §4 is a trust boundary, and the pointer (the message id)
    is what gets stored instead of the payload.
    """
    plain, html_parts = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            html_parts.append(_b64(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain)[:MAX_BODY]
    if html_parts:
        stripped = _TAGS.sub(" ", "\n".join(html_parts))
        return unescape(re.sub(r"<[^>]+>", " ", stripped))[:MAX_BODY]
    return ""


def _message(mid: str) -> dict:
    msg = _api(f"/messages/{mid}", format="full")
    payload = msg.get("payload") or {}
    headers = {h["name"].lower(): h["value"]
               for h in payload.get("headers") or []}
    return {
        "id": mid,
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "body": _text(payload),
        # Gmail's own receipt time, in ms. The honest fallback when an email
        # states no date of its own — unlike now(), it does not drift with
        # when the poll happened to run.
        "at": datetime.fromtimestamp(
            int(msg.get("internalDate", 0)) / 1000, timezone.utc),
    }


# --- Lane A · card bill payments --------------------------------------------

def _write_payment(user: dict, msg: dict) -> tuple[str, dict]:
    """A settlement, not a spend. kind='cc_payment' is excluded from every
    spending total by PLAN-EXPENSES.md E7 — reading one as a spend would
    double-count the whole month, silently."""
    parsed = gmail_parse.parse_payment(
        msg["sender"], msg["subject"], msg["body"], user.get("cards") or {})
    if not parsed:
        # No amount found. A pattern to add, not a token to spend: v1 never
        # escalates an unreadable email to a model.
        return "unparsed_payment", {}

    paid_on = parsed["paid_on"]
    spent_at = (datetime(paid_on.year, paid_on.month, paid_on.day,
                         tzinfo=timezone.utc) if paid_on else msg["at"])
    amount = str(parsed["amount"])
    detail = {"amount": amount, "spent_at": spent_at.isoformat(),
              "card": parsed["card"], "bank_label": parsed["bank_label"]}

    if not parsed["card"]:
        # Do not invent a card. A wrong one silently corrupts E9; a null one is
        # merely incomplete, and this line is how you learn which match string
        # to add to users.cards.
        log.info("payment with no card match: label=%r subject=%r",
                 parsed["bank_label"], msg["subject"][:80])

    try:
        db.sb().table("expenses").insert({
            "user_id": user["id"], "spent_at": spent_at.isoformat(),
            "amount": amount, "share_amount": amount, "owed_amount": 0,
            "headcount": 1, "kind": "cc_payment", "category": None,
            "card": parsed["card"], "source": "email", "source_ref": msg["id"],
            "status": "confirmed",
        }).execute()
    except Exception as e:
        if not _is_duplicate(e):
            raise
        # expenses_dedupe_idx did its job. Not an error: the row exists, which
        # is the outcome we wanted.
        detail["duplicate"] = True
    return "payment", detail


# --- Lane B · applications --------------------------------------------------

def _upsert_application(user: dict, msg: dict) -> tuple[str, dict]:
    """Match an application email to a row, or start one.

    Two rules, both judgement calls, both here rather than spread out:

      rejection -> closes the NEWEST still-open application for that company.
          Role is not part of the match: rejection emails routinely name a
          different role string from the receipt, or none, so matching on it
          would mean the rejection never finds what it is meant to close.
          With no open row, insert one anyway — company from the subject or
          the sender, role left empty. An orphan you can see beats a rejection
          you never hear about (evaluation_plan.md §9: optimise recall).

      receipt   -> updates the row for that company with the same role (or
          with no role on either side), else inserts. Applying to one company
          twice stays two rows.
    """
    parsed = gmail_parse.parse_application(
        msg["sender"], msg["subject"], msg["body"])
    if not parsed:
        return "unparsed_application", {}

    status, key = parsed["status"], parsed["company_key"]
    detail = {"company": parsed["company"], "company_key": key,
              "role": parsed["role"], "status": status}

    rows = (db.sb().table("applications").select("*")
            .eq("user_id", user["id"]).eq("company_key", key)
            .order("created_at", desc=True).execute().data)

    if status == "rejected":
        match = next((r for r in rows if r["status"] == "applied"), None)
    else:
        match = next((r for r in rows
                      if gmail_parse.norm(r["role"]) ==
                      gmail_parse.norm(parsed["role"])), None)

    fields = {"status": status, "source_ref": msg["id"],
              "last_email_at": msg["at"].isoformat()}
    if match:
        # Never blank a role we already know just because this email omitted it.
        if parsed["role"] and not match["role"]:
            fields["role"] = parsed["role"]
        db.sb().table("applications").update(fields).eq("id", match["id"]).execute()
        detail["application_id"] = match["id"]
    else:
        row = db.sb().table("applications").insert({
            "user_id": user["id"], "company": parsed["company"],
            "company_key": key, "role": parsed["role"], "source": "gmail",
            **fields,
        }).execute().data[0]
        detail["application_id"] = row["id"]
        detail["inserted"] = True
    return f"application_{status}", detail


def _is_duplicate(e: Exception) -> bool:
    return "23505" in str(e) or "duplicate key" in str(e)


# --- the poll ---------------------------------------------------------------

def poll(user: dict) -> int:
    """One pass over the window. Returns how many messages were processed.

    Runs from /cron/tick for every user with Gmail configured, independently of
    the 11am check-in.
    """
    ids = _list_ids()
    seen = db.seen_message_ids(user["id"], ids)
    fresh = [mid for mid in ids if mid not in seen]
    audit.step("poll", listed=len(ids), seen=len(seen), fresh=len(fresh))

    processed = 0
    for mid in fresh:
        lane = outcome = None
        detail: dict = {}
        msg = {"id": mid, "sender": "", "subject": "", "body": "",
               "at": datetime.now(timezone.utc)}
        try:
            msg = _message(mid)
            lane = gmail_parse.route(msg["sender"], msg["subject"], msg["body"])
            if lane == "payment":
                outcome, detail = _write_payment(user, msg)
            elif lane == "application":
                outcome, detail = _upsert_application(user, msg)
            else:
                # Still recorded. An email skipped by mistake and one that
                # never arrived look identical unless the skip is written down.
                outcome = "ignored"
        except Exception as e:
            # One malformed email must not stall the poll, and a message that
            # threw must still be claimed — otherwise it throws again every
            # hour for two days.
            outcome, detail = "error", {"error": f"{type(e).__name__}: {e}"}
            log.error("gmail message %s failed: %s", mid, e)

        db.record_email_event(user["id"], mid, lane, outcome,
                              msg["sender"], msg["subject"], detail)
        audit.step("email", id=mid, lane=lane, outcome=outcome, **detail)
        processed += 1
    return processed


# --- one-time setup ---------------------------------------------------------

def setup() -> None:
    """Get a refresh token. Run once, by hand: python -m capabilities.gmail

    Needs a GCP project with the Gmail API enabled and an OAuth client of type
    "Desktop app" — its id and secret go in .env first.
    """
    if not (CLIENT_ID and CLIENT_SECRET):
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.")
        print("console.cloud.google.com -> APIs & Services -> Credentials")
        print("  -> Create credentials -> OAuth client ID -> Desktop app")
        print("Enable the Gmail API for the project, and add yourself as a")
        print("test user on the OAuth consent screen.")
        return

    redirect = "urn:ietf:wg:oauth:2.0:oob"
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": redirect, "scope": SCOPE,
        "response_type": "code", "access_type": "offline", "prompt": "consent"})
    print(f"\n1. Open this and approve read-only Gmail access:\n\n{AUTH_URL}?{params}\n")
    code = input("2. Paste the code Google gives you: ").strip()

    r = httpx.post(TOKEN_URL, timeout=30, data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "code": code, "redirect_uri": redirect,
        "grant_type": "authorization_code"})
    if r.status_code >= 400:
        print(f"\nGoogle refused it: {r.status_code} {r.text[:300]}")
        return
    token = r.json().get("refresh_token")
    if not token:
        print("\nNo refresh token came back — revoke the app's access at "
              "myaccount.google.com/permissions and run this again.")
        return
    print(f"\n3. Put this in .env:\n\nGOOGLE_REFRESH_TOKEN={token}\n")


if __name__ == "__main__":
    setup()
