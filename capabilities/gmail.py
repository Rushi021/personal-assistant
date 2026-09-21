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
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from core import audit, db

from . import gmail_parse

log = logging.getLogger("assistant.gmail")

CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")

# One OAuth client, several mailboxes. Each account is its own env var:
#
#     GMAIL_TOKEN_PERSONAL=1//0g...
#     GMAIL_TOKEN_WORK=1//0g...
#
# Discovered by prefix rather than listed anywhere, so adding a third mailbox
# is one line in .env (or one `fly secrets set`) and no code change. The label
# after the prefix is not decoration: it prefixes every stored message id,
# because a Gmail id is unique WITHIN a mailbox and nothing guarantees two
# accounts never mint the same one.
TOKEN_PREFIX = "GMAIL_TOKEN_"

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


def accounts() -> dict[str, str]:
    """label -> refresh token, read fresh from the environment each call so a
    mailbox added to .env needs no more than a restart."""
    return {k[len(TOKEN_PREFIX):].lower(): v for k, v in os.environ.items()
            if k.startswith(TOKEN_PREFIX) and v}


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and accounts())


# --- the client -------------------------------------------------------------

_tokens: dict[str, tuple[str, float]] = {}   # label -> (access_token, expires_at)


def _access_token(label: str) -> str:
    """Refresh-token grant, cached per account until a minute before it
    expires. One refresh per mailbox per hour, which is what the tick needs."""
    now = datetime.now(timezone.utc).timestamp()
    cached = _tokens.get(label)
    if cached and cached[1] > now:
        return cached[0]
    token = accounts().get(label)
    if not token:
        raise RuntimeError(f"no refresh token for gmail account {label!r}")
    r = httpx.post(TOKEN_URL, timeout=30, data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": token, "grant_type": "refresh_token"})
    if r.status_code >= 400 and "invalid_grant" in r.text:
        # Almost always one of three things, and the raw error says none of
        # them. Worth spelling out: this fires weeks after the setup that
        # caused it, by which time the cause is not remotely obvious.
        raise RuntimeError(
            f"gmail account {label!r}: refresh token rejected (invalid_grant). "
            "Either the OAuth consent screen is still in Testing — which "
            "expires refresh tokens after 7 days, so publish it to production "
            "— or access was revoked, or the token is for a different client. "
            f"Reconnect with: python -m capabilities.gmail {label}")
    r.raise_for_status()
    body = r.json()
    _tokens[label] = (body["access_token"], now + body.get("expires_in", 3600) - 60)
    return _tokens[label][0]


# Gmail answers a rate limit with 403, not 429, and the reason is buried in the
# body — so a burst looks exactly like a permissions failure. The first poll of
# a mailbox fetches up to MAX_RESULTS messages back to back, which is precisely
# the shape that trips it.
_RETRYABLE = ("rateLimitExceeded", "userRateLimitExceeded", "backendError")


def _api(label: str, path: str, _attempt: int = 0, **params) -> dict:
    r = httpx.get(f"{API}{path}", timeout=30, params=params,
                  headers={"Authorization": f"Bearer {_access_token(label)}"})
    if r.status_code >= 400 and _attempt < 4:
        body = r.text
        if r.status_code in (429, 500, 503) or (
                r.status_code == 403 and any(x in body for x in _RETRYABLE)):
            # 1s, 2s, 4s, 8s. Plain exponential: one process, one mailbox at a
            # time, so there is no thundering herd for jitter to break up.
            delay = 2 ** _attempt
            log.warning("gmail %s: %s, retrying in %ss", label, r.status_code, delay)
            time.sleep(delay)
            return _api(label, path, _attempt + 1, **params)
    r.raise_for_status()
    return r.json()


def _list_ids(label: str) -> list[str]:
    page = _api(label, "/messages", q=QUERY, maxResults=MAX_RESULTS)
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


def _message(label: str, mid: str) -> dict:
    msg = _api(label, f"/messages/{mid}", format="full")
    payload = msg.get("payload") or {}
    headers = {h["name"].lower(): h["value"]
               for h in payload.get("headers") or []}
    return {
        # Qualified with the mailbox it came from. This is what is stored as
        # source_ref and as email_events.gmail_message_id, so the uniqueness
        # those indexes promise holds across accounts and not just within one.
        "id": f"{label}:{mid}",
        "account": label,
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
    # evaluation_plan.md §10.4 — log kind and card on EVERY extraction, right
    # or wrong. This is the single largest possible error in the system, and a
    # labelled set of these steps is the only way to measure it (E7).
    audit.step("classify", kind="cc_payment", card=parsed["card"],
               bank_label=parsed["bank_label"])
    return "payment", detail


# --- Lane C · card spend alerts ---------------------------------------------

def _write_spend(user: dict, msg: dict) -> tuple[str, dict]:
    """A swipe. The amount, card and date count immediately; the meaning is
    blank until the user answers the digest (E2, E6).

    The row lands as `status='pending'` — that is not a half-written row, it is
    a complete fact about money with a question attached.
    """
    parsed = gmail_parse.parse_spend(
        msg["sender"], msg["subject"], msg["body"], user.get("cards") or {})
    if not parsed:
        return "unparsed_spend", {}

    spent_on = parsed["spent_on"]
    spent_at = (datetime(spent_on.year, spent_on.month, spent_on.day,
                         tzinfo=timezone.utc) if spent_on else msg["at"])
    amount = str(parsed["amount"])
    detail = {"amount": amount, "spent_at": spent_at.isoformat(),
              "card": parsed["card"], "merchant": parsed["merchant"]}

    # E2, email direction: you already told it about this spend within the hour,
    # so the email adds nothing. The DISCARD is logged — an email that vanishes
    # without a record is indistinguishable from one that never arrived.
    already = db.recent_chat_expense(user["id"], amount)
    if already:
        detail["discarded_against"] = already["id"]
        audit.step("dedupe", decision="discarded", against=already["id"],
                   amount=amount)
        return "spend_discarded", detail

    if not parsed["card"]:
        log.info("spend with no card match: label=%r subject=%r",
                 parsed["bank_label"], msg["subject"][:80])

    try:
        db.sb().table("expenses").insert({
            "user_id": user["id"], "spent_at": spent_at.isoformat(),
            "amount": amount, "share_amount": amount, "owed_amount": 0,
            "headcount": 1, "kind": "spend",
            # No category on purpose (E6). A guess here would read as an answer
            # and the digest would never ask.
            "category": None, "merchant": parsed["merchant"],
            "card": parsed["card"], "source": "email", "source_ref": msg["id"],
            "status": "pending",
        }).execute()
    except Exception as e:
        if not _is_duplicate(e):
            raise
        detail["duplicate"] = True
    audit.step("classify", kind="spend", card=parsed["card"],
               merchant_raw=parsed["merchant"])
    return "spend", detail


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
    """One pass over the window, across every configured mailbox. Returns how
    many messages were processed.

    Runs from /cron/tick for every user with Gmail configured, independently of
    the 11am check-in. One mailbox failing — a revoked token, a rate limit —
    must not cost you the others, so each is caught separately.
    """
    processed = 0
    for label in sorted(accounts()):
        try:
            processed += _poll_account(user, label)
        except Exception as e:
            audit.step("poll", account=label, ok=False,
                       error=f"{type(e).__name__}: {e}")
            log.error("gmail account %s failed: %s", label, e)
    return processed


def _poll_account(user: dict, label: str) -> int:
    ids = _list_ids(label)
    refs = [f"{label}:{mid}" for mid in ids]
    seen = db.seen_message_ids(user["id"], refs)
    fresh = [mid for mid, ref in zip(ids, refs) if ref not in seen]
    audit.step("poll", account=label, listed=len(ids), seen=len(seen),
               fresh=len(fresh))

    processed = 0
    for mid in fresh:
        lane = outcome = None
        detail: dict = {}
        msg = {"id": f"{label}:{mid}", "account": label, "sender": "",
               "subject": "", "body": "", "at": datetime.now(timezone.utc)}
        try:
            msg = _message(label, mid)
            lane = gmail_parse.route(msg["sender"], msg["subject"], msg["body"])
            if lane == "payment":
                outcome, detail = _write_payment(user, msg)
            elif lane == "spend":
                outcome, detail = _write_spend(user, msg)
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

        db.record_email_event(user["id"], msg["id"], lane, outcome,
                              msg["sender"], msg["subject"], detail)
        audit.step("email", id=msg["id"], lane=lane, outcome=outcome, **detail)
        processed += 1
    return processed


# --- one-time setup ---------------------------------------------------------

def _consent(slug: str) -> tuple[str, str] | None:
    """Run the loopback consent flow. Returns (code, redirect_uri).

    Binds port 0 so the OS picks a free one, prints the URL, then serves
    exactly one request — Google's redirect back with ?code=... in the query.
    """
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": redirect, "scope": SCOPE,
        "response_type": "code", "access_type": "offline", "prompt": "consent"})
    url = f"{AUTH_URL}?{params}"

    print(f"\nConnecting the '{slug}' mailbox.")
    print("\nSign your browser in to THAT account first — the consent screen "
          "uses\nwhichever Google account the browser already has.\n")
    print(f"Opening:\n{url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass                      # headless box: the printed URL is the fallback
    print("Waiting for the redirect… (Ctrl-C to give up)")

    try:
        handler = server.get_request()
        # BaseHTTPRequestHandler parses the request line on construction, so
        # this both reads the request and answers it.
        conn, addr = handler
        request = conn.recv(8192).decode("utf-8", "replace")
    except KeyboardInterrupt:
        return None
    finally:
        server.server_close()

    path = request.split(" ", 2)[1] if " " in request else ""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    body = ("Connected. You can close this tab." if query.get("code")
            else f"Something went wrong: {query.get('error', ['no code'])[0]}")
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                 b"Connection: close\r\n\r\n" + body.encode())
    conn.close()

    if not query.get("code"):
        print(f"\n{body}")
        return None
    return query["code"][0], redirect


def setup(label: str = "personal") -> None:
    """Get a refresh token for one mailbox. Run once per account, by hand:

        python -m capabilities.gmail personal
        python -m capabilities.gmail work

    Sign in as THAT account in the browser each time — the consent screen uses
    whichever Google account the browser is already signed into, which is the
    one way to end up with two env vars holding the same mailbox.

    Needs a GCP project with the Gmail API enabled and an OAuth client of type
    "Desktop app" — its id and secret go in .env first. One client covers every
    account; only the token differs.
    """
    if not (CLIENT_ID and CLIENT_SECRET):
        print("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")
        print("console.cloud.google.com -> APIs & Services -> Credentials")
        print("  -> Create credentials -> OAuth client ID -> Desktop app")
        print("Enable the Gmail API for the project, and add every address you")
        print("plan to connect as a test user on the OAuth consent screen.")
        return

    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    if not slug:
        print("Give the account a one-word label: python -m capabilities.gmail work")
        return
    if slug in accounts():
        print(f"GMAIL_TOKEN_{slug.upper()} is already set. Remove it from .env "
              f"first if you want to reconnect this mailbox.")
        return

    # Loopback, not urn:ietf:wg:oauth:2.0:oob — Google blocked the
    # copy-paste-the-code flow in 2022 and it now fails at the consent screen.
    # A Desktop-app client accepts any http://127.0.0.1 port without it being
    # registered, so this needs no extra setup in the console.
    code = _consent(slug)
    if not code:
        return

    r = httpx.post(TOKEN_URL, timeout=30, data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "code": code[0], "redirect_uri": code[1],
        "grant_type": "authorization_code"})
    if r.status_code >= 400:
        print(f"\nGoogle refused it: {r.status_code} {r.text[:300]}")
        return
    token = r.json().get("refresh_token")
    if not token:
        print("\nNo refresh token came back — revoke the app's access at "
              "myaccount.google.com/permissions and run this again.")
        return
    print(f"\n3. Put this in .env:\n\nGMAIL_TOKEN_{slug.upper()}={token}\n")
    print("Run this again with a different label to add another mailbox.")


if __name__ == "__main__":
    setup(sys.argv[1] if len(sys.argv) > 1 else "personal")
