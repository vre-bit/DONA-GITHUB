#!/usr/bin/env python3
"""Credit Note Desk - Flask backend.

Serves index.html, signs the user in to Google with server-side OAuth
(Gmail read + send), proxies Gmail reads, reads pending bills from the ERP,
sends vendor follow-up mail the user has approved, and calls the OpenAI API.
The Google tokens, ERP key and OpenAI API key never reach the browser.

Install: pip install -r requirements.txt
Run:     python server.py
Open:    http://localhost:8080
"""
import base64
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urlencode

import openai
import requests
from flask import Flask, abort, jsonify, redirect, request, session, url_for
from markupsafe import escape
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = os.path.dirname(os.path.abspath(__file__))

# Hosted under a sub-path (e.g. /p/dona-project) behind a proxy that strips it.
# Take SCRIPT_NAME out of the process env so gunicorn stops rejecting "/" and
# apply it ourselves in PrefixMiddleware below.
URL_PREFIX = (os.environ.pop("SCRIPT_NAME", "") or os.environ.get("URL_PREFIX", "")).rstrip("/")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me/"
SCOPES = ("openid email https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/gmail.send")


def load_config():
    cfg = {}
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    get = lambda k, d="": os.environ.get(k) or cfg.get(k) or d
    return {
        "OPENAI_API_KEY": get("OPENAI_API_KEY"),
        "GOOGLE_CLIENT_ID": get("GOOGLE_CLIENT_ID"),
        "GOOGLE_CLIENT_SECRET": get("GOOGLE_CLIENT_SECRET"),
        "OPENAI_MODEL": get("OPENAI_MODEL", "gpt-4.1"),
        "ERP_API_URL": get("ERP_API_URL"),
        "ERP_USER": get("ERP_USER", "dona.basu"),
        "SHEET_ID": get("SHEET_ID"),
        "SHEET_GID": get("SHEET_GID", "0"),
        "SECRET_KEY": get("SECRET_KEY"),
        "PORT": int(get("PORT", "8080")),
    }


CFG = load_config()


def stable_secret():
    # Every gunicorn worker must share one key, or sessions break between workers.
    if CFG["SECRET_KEY"]:
        return CFG["SECRET_KEY"]
    path = os.path.join(ROOT, ".secret_key")
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class PrefixMiddleware:
    def __init__(self, wsgi_app, prefix):
        self.wsgi_app, self.prefix = wsgi_app, prefix

    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_FORWARDED_PREFIX", "").rstrip("/") or self.prefix
        if prefix:
            path = environ.get("PATH_INFO", "")
            if path == prefix or path.startswith(prefix + "/"):  # proxy did not strip it
                path = path[len(prefix):]
            environ["PATH_INFO"] = path or "/"
            environ["SCRIPT_NAME"] = prefix
        return self.wsgi_app(environ, start_response)


app = Flask(__name__, static_folder=None)
app.secret_key = stable_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
# Trust the proxy's X-Forwarded-Proto/Host so the OAuth redirect URL is the public https one.
app.wsgi_app = PrefixMiddleware(ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1), URL_PREFIX)


class TokenStore:
    """Google tokens keyed by a random id kept in the session cookie.

    SQLite so that every gunicorn worker sees the same sign-ins.
    """

    def __init__(self, path):
        self.path = path
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tokens (sid TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def get(self, sid):
        with self._db() as db:
            row = db.execute("SELECT data FROM tokens WHERE sid = ?", (sid,)).fetchone()
        return json.loads(row[0]) if row else None

    def __setitem__(self, sid, tok):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO tokens VALUES (?, ?)", (sid, json.dumps(tok)))

    def pop(self, sid, default=None):
        tok = self.get(sid) if sid else None
        if tok:
            with self._db() as db:
                db.execute("DELETE FROM tokens WHERE sid = ?", (sid,))
        return tok or default


TOKENS = TokenStore(os.path.join(ROOT, "tokens.db"))

ai = openai.OpenAI(api_key=CFG["OPENAI_API_KEY"]) if CFG["OPENAI_API_KEY"] else None

SYSTEM_PROMPT = """You classify email threads about vendor CREDIT NOTES for an accounts-payable executive at Alcove Realty (Kolkata, India).
You receive several email threads. For EACH thread return one JSON object. Reply with a single JSON array only: no prose, no code fences.

Use only facts stated in the emails. Never invent numbers, dates, invoice numbers or amounts; use null when unknown. Dates are YYYY-MM-DD from the message date.

Object keys (all required):
- threadId: string, copied from the input
- subject: string (first subject, trimmed)
- vendor: string, the vendor BUSINESS the credit note concerns, as a clean name (e.g. "Jaquar & Co"); derive it from signature, subject, invoice numbers or sender domain; "Unknown vendor" if truly unknown. If one thread concerns several vendors, use "Multiple vendors (A, B)".
- vendorKey: lowercase slug of the vendor
- entity: one of "Alcove Developers LLP", "Amba Highrise", "Sundarvan", "A N Industries", "Other/Unknown" (only if stated or clearly implied)
- category: "received" (vendor sent/issued credit note(s): copy attached or details given) | "requested" (Alcove asked for credit note(s) and none received in the thread) | "partial" (some received, some awaited) | "adjusted" (settled by deduction or ledger/payment adjustment instead of a credit note document) | "mentioned" (only mentioned in passing) | "not_relevant"
- status: "closed" (nothing awaited) | "open" (a credit note is still awaited or action is pending) | "info"
- credit_notes: array of {ref: string|null, cn_date: string|null, received_date: string|null, invoices: string[], amount: number|null (INR, only if explicitly stated), files: string[] (credit note PDF filenames), reason: string (3 to 8 words)}
- pending: array of {invoice: string|null, reason: string, asked_date: string|null}
- summary: one plain sentence, max 28 words
- next_action: a short, polite-style action for Dona, or ""
- last_activity: date of the last message
- in_period: true if a relevant message is on or after the period start given in the input
- confidence: "high" | "medium" | "low"

A vendor attaching an invoice copy is not a credit note. Internal staff forwarding a vendor's credit note still counts as "received". If an email only says a credit note will be sent, it is "requested" and "open"."""

DEFAULTS = {
    "subject": "", "vendor": "Unknown vendor", "vendorKey": "unknownvendor",
    "entity": "Other/Unknown", "category": "mentioned", "status": "info",
    "credit_notes": [], "pending": [], "summary": "", "next_action": "",
    "last_activity": None, "in_period": True, "confidence": "low",
}


# ---------- Google OAuth ----------

def redirect_uri():
    return url_for("oauth_callback", _external=True)


def current_token():
    sid = session.get("sid")
    return TOKENS.get(sid) if sid else None


def refresh_token(tok):
    if not tok.get("refresh_token"):
        return None
    r = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": CFG["GOOGLE_CLIENT_ID"], "client_secret": CFG["GOOGLE_CLIENT_SECRET"],
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    }, timeout=20)
    if not r.ok:
        return None
    data = r.json()
    tok["access_token"] = data["access_token"]
    tok["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
    return tok


def access_token():
    tok = current_token()
    if not tok:
        return None
    if time.time() >= tok["expires_at"]:
        if not refresh_token(tok):
            TOKENS.pop(session.get("sid"), None)
            return None
        TOKENS[session["sid"]] = tok
    return tok["access_token"]


@app.get("/login")
def login():
    if not (CFG["GOOGLE_CLIENT_ID"] and CFG["GOOGLE_CLIENT_SECRET"]):
        return "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set. See README.md.", 503
    session["oauth_state"] = secrets.token_urlsafe(24)
    params = {
        "client_id": CFG["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": SCOPES, "state": session["oauth_state"],
        "access_type": "offline", "include_granted_scopes": "true", "prompt": "consent",
    }
    return redirect(GOOGLE_AUTH_URL + "?" + urlencode(params))


@app.get("/oauth2callback")
def oauth_callback():
    if request.args.get("error"):
        return redirect(url_for("index", auth_error=request.args["error"]))
    state = session.pop("oauth_state", None)
    if not state or request.args.get("state") != state:
        abort(400, "OAuth state mismatch. Start sign-in again from the app.")
    r = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": CFG["GOOGLE_CLIENT_ID"], "client_secret": CFG["GOOGLE_CLIENT_SECRET"],
        "code": request.args.get("code", ""), "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
    }, timeout=20)
    if not r.ok:
        return "Google token exchange failed: %s" % r.text[:300], 502
    data = r.json()
    if "gmail.readonly" not in data.get("scope", ""):
        return redirect(url_for("index", auth_error="gmail_scope_not_granted"))
    info = requests.get(GOOGLE_USERINFO_URL, headers={"Authorization": "Bearer " + data["access_token"]}, timeout=20)
    sid = secrets.token_urlsafe(24)
    TOKENS[sid] = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": time.time() + int(data.get("expires_in", 3600)) - 60,
        "email": info.json().get("email", "") if info.ok else "",
        "scope": data.get("scope", ""),
    }
    session["sid"] = sid
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    require_page_request()
    tok = TOKENS.pop(session.pop("sid", None), None)
    if tok:
        requests.post(GOOGLE_REVOKE_URL, params={"token": tok.get("refresh_token") or tok["access_token"]}, timeout=10)
    return jsonify(ok=True)


# ---------- API ----------

def require_page_request():
    # Simple CSRF guard: custom header can only be set by same-origin scripts.
    if request.headers.get("X-CND") != "1":
        abort(403, "Blocked: request did not come from this page.")


@app.errorhandler(400)
@app.errorhandler(403)
def json_error(e):
    return jsonify(error=e.description), e.code


@app.get("/")
def index():
    # <base> makes the page's relative URLs (api/..., login) work under a sub-path.
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        html = f.read().replace("<head>", '<head>\n<base href="%s/">' % escape(request.script_root), 1)
    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


@app.get("/api/config")
def api_config():
    tok = current_token()
    return jsonify(
        googleReady=bool(CFG["GOOGLE_CLIENT_ID"] and CFG["GOOGLE_CLIENT_SECRET"]),
        hasAiKey=bool(CFG["OPENAI_API_KEY"]),
        model=CFG["OPENAI_MODEL"],
        signedIn=bool(tok),
        email=tok["email"] if tok else "",
        canSend=bool(tok and "gmail.send" in tok.get("scope", "")),
        erpUser=CFG["ERP_USER"],
        hasErp=bool(CFG["ERP_API_URL"]),
        hasSheet=bool(CFG["SHEET_ID"]),
    )


@app.get("/api/gmail/<path:sub>")
def api_gmail(sub):
    require_page_request()
    if not (sub == "threads" or sub.startswith("threads/")):
        abort(403, "Only Gmail thread reads are allowed.")
    token = access_token()
    if not token:
        return jsonify(error="signed_out"), 401
    r = requests.get(GMAIL_API + sub, params=list(request.args.items(multi=True)),
                     headers={"Authorization": "Bearer " + token}, timeout=60)
    return r.content, r.status_code, {"Content-Type": "application/json"}


# ---------- ERP pending bills ----------

ERP_CACHE = {"at": 0, "items": None}
ERP_LOCK = threading.Lock()

# What a bill is waiting for, read from the ERP remark. Approximate; the remark is shown too.
NEEDS = [
    ("Credit note", re.compile(r"credit\s*note|\bCN\b", re.I)),
    ("MTC", re.compile(r"\bMTC\b|test\s*certificate", re.I)),
    ("Warranty certificate", re.compile(r"\bWC\b|warrant", re.I)),
    ("Invoice correction", re.compile(r"signature|stamp|revised\s+tax\s+invoice|rectified|wrong\s+invoice", re.I)),
    ("GST", re.compile(r"GSTR|\bGST\b", re.I)),
    ("Rate / PO", re.compile(r"\brate\b|\bPO\b", re.I)),
]


def erp_date(s):
    try:
        return datetime.strptime(" ".join(s.split()), "%b %d %Y %I:%M%p").date().isoformat()
    except (ValueError, AttributeError):
        return None


def erp_item(x):
    vendor, _, invoice = (x.get("TokenName") or "").partition(" = ")
    ref = (x.get("Token_Description") or "").strip()
    if " = " in ref:
        ref = ref.split(" = ", 1)[1]
    remark = x.get("Remark") or ""
    created = re.search(r"Token_Creation_Date:\s*(.+?)\s+Token_Desc:", remark)
    note = " ".join(remark.split("Remark:", 1)[-1].split())
    return {
        "id": x.get("Transaction_id"), "token": x.get("Token_id"),
        "vendor": " ".join(vendor.split()), "invoice": invoice.strip(), "ref": ref.strip(),
        "project": (x.get("ProjectName") or "").strip(), "type": (x.get("TokenType_Name") or "").strip(),
        "toUser": (x.get("ToUser") or "").strip(), "fromUser": (x.get("FromUser") or "").strip(),
        "status": (x.get("Status1") or "").strip(), "received": (x.get("TDate") or "")[:10],
        "created": erp_date(created.group(1)) if created else None, "note": note,
        "needs": [name for name, rx in NEEDS if rx.search(note)], "docs": x.get("DocCount"),
    }


def erp_items():
    with ERP_LOCK:
        if ERP_CACHE["items"] is None or time.time() - ERP_CACHE["at"] > 300:
            r = requests.get(CFG["ERP_API_URL"], timeout=60)
            r.raise_for_status()
            ERP_CACHE["items"] = [erp_item(x) for x in r.json() if isinstance(x, dict)]
            ERP_CACHE["at"] = time.time()
        return ERP_CACHE["items"]


@app.get("/api/erp")
def api_erp():
    require_page_request()
    if not current_token():
        return jsonify(error="Sign in with Google first."), 401
    if request.args.get("refresh"):
        ERP_CACHE["items"] = None
    try:
        items = erp_items()
    except (requests.RequestException, ValueError) as e:
        return jsonify(error="Could not read the ERP: %s" % e), 502
    return jsonify(items=items, user=CFG["ERP_USER"], fetchedAt=int(ERP_CACHE["at"]))


# ---------- vendor payment issues (Google Sheet shared as "anyone with the link") ----------

SHEET_CACHE = {"at": 0, "rows": None}
SHEET_COLS = ["ticket", "vendor", "pan", "phone", "email", "raised", "type", "message", "status", "remark"]


def sheet_rows():
    if SHEET_CACHE["rows"] is None or time.time() - SHEET_CACHE["at"] > 120:
        url = "https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s" % (CFG["SHEET_ID"], CFG["SHEET_GID"])
        r = requests.get(url, timeout=60)
        if r.status_code in (401, 403) or "text/csv" not in r.headers.get("Content-Type", ""):
            raise ValueError("the sheet is not shared as 'Anyone with the link can view'")
        r.raise_for_status()
        rows = []
        for i, cells in enumerate(csv.reader(io.StringIO(r.content.decode("utf-8-sig")))):
            cells = [c.strip() for c in cells] + [""] * len(SHEET_COLS)
            row = dict(zip(SHEET_COLS, cells))
            if not row["vendor"] or (i == 0 and re.search(r"vendor|party|name", row["vendor"], re.I)):
                continue  # blank line or a header row
            row["row"] = i + 1
            rows.append(row)
        SHEET_CACHE.update(at=time.time(), rows=rows)
    return SHEET_CACHE["rows"]


@app.get("/api/payment-issues")
def api_payment_issues():
    require_page_request()
    if not current_token():
        return jsonify(error="Sign in with Google first."), 401
    if not CFG["SHEET_ID"]:
        return jsonify(error="SHEET_ID is not set on the server."), 503
    if request.args.get("refresh"):
        SHEET_CACHE["rows"] = None
    try:
        rows = sheet_rows()
    except (requests.RequestException, ValueError) as e:
        return jsonify(error="Could not read the payment issues sheet: %s" % e), 502
    return jsonify(rows=rows, fetchedAt=int(SHEET_CACHE["at"]),
                   sheetUrl="https://docs.google.com/spreadsheets/d/%s/edit#gid=%s" % (CFG["SHEET_ID"], CFG["SHEET_GID"]))


# ---------- send vendor follow-up mail (only when the user approves it on the page) ----------

ADDR = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[A-Za-z]{2,}$")


def addr_list(s):
    out = [a.strip() for a in re.split(r"[,;]", s or "") if a.strip()]
    bad = [a for a in out if not ADDR.match(a)]
    if bad:
        abort(400, "Not a valid email address: %s" % ", ".join(bad))
    return out


@app.post("/api/send")
def api_send():
    require_page_request()
    tok = current_token()
    token = access_token()
    if not token:
        return jsonify(error="Sign in with Google first."), 401
    if "gmail.send" not in tok.get("scope", ""):
        return jsonify(error="Sending is not allowed yet. Sign out, sign in again and allow sending email."), 403
    p = request.get_json(silent=True) or {}
    to, cc = addr_list(p.get("to")), addr_list(p.get("cc"))
    subject, body = (p.get("subject") or "").strip(), (p.get("body") or "").strip()
    if not to or not subject or not body:
        abort(400, "To, subject and message are all required.")
    if len(to) + len(cc) > 10:
        abort(400, "Too many recipients (10 at most).")
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = requests.post(GMAIL_API + "messages/send", json={"raw": raw},
                      headers={"Authorization": "Bearer " + token}, timeout=60)
    if not r.ok:
        return jsonify(error="Gmail did not send the mail (%s): %s" % (r.status_code, r.text[:200])), 502
    return jsonify(ok=True, id=r.json().get("id"), threadId=r.json().get("threadId"))


def call_ai(user_text):
    resp = ai.chat.completions.create(
        model=CFG["OPENAI_MODEL"],
        max_completion_tokens=16000,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": user_text}],
    )
    choice = resp.choices[0]
    if getattr(choice.message, "refusal", None):
        raise RuntimeError("The AI declined to classify this batch.")
    if choice.finish_reason == "length":
        raise RuntimeError("The AI's reply was cut off. Try fewer threads per batch.")
    return choice.message.content or ""


def parse_array(text):
    a, b = text.find("["), text.rfind("]")
    if a < 0 or b <= a:
        raise ValueError("The AI did not return a JSON array")
    return json.loads(text[a:b + 1])


def classify(payload):
    threads = payload.get("threads") or []
    start = payload.get("periodStart") or ""
    parts = ["Period start: %s\n" % start]
    for t in threads:
        parts.append("=== THREAD %s ===\nSubject: %s" % (t.get("threadId"), t.get("subject", "")))
        for m in t.get("messages", []):
            files = ", ".join(m.get("files") or []) or "none"
            parts.append("--- message %s | from %s | attachments: %s\n%s" % (
                m.get("date", ""), m.get("from", ""), files, m.get("body", "")))
    arr = parse_array(call_ai("\n".join(parts)))
    by_id = {str(r.get("threadId")): r for r in arr if isinstance(r, dict)}
    out = []
    for t in threads:
        r = dict(DEFAULTS)
        r.update(by_id.get(str(t.get("threadId")), {}))
        r["threadId"] = t.get("threadId")
        for k in ("credit_notes", "pending"):
            if not isinstance(r.get(k), list):
                r[k] = []
        out.append(r)
    return out


@app.post("/api/classify")
def api_classify():
    require_page_request()
    if not current_token():
        return jsonify(error="Sign in with Google first."), 401
    if not ai:
        return jsonify(error="OPENAI_API_KEY is not set on the server."), 503
    try:
        return jsonify(records=classify(request.get_json(silent=True) or {}))
    except openai.RateLimitError:
        return jsonify(error="OpenAI rate limit or quota reached. Wait a minute and sync again."), 429
    except openai.AuthenticationError:
        return jsonify(error="The OpenAI API key was rejected."), 502
    except openai.NotFoundError:
        return jsonify(error="Model %s not found for this key. Set OPENAI_MODEL." % CFG["OPENAI_MODEL"]), 502
    except openai.APIStatusError as e:
        return jsonify(error="OpenAI API error %s: %s" % (e.status_code, e.message)), 502
    except openai.APIConnectionError:
        return jsonify(error="Could not reach the OpenAI API."), 502
    except (RuntimeError, ValueError) as e:
        return jsonify(error=str(e)), 502


def main():
    if not CFG["OPENAI_API_KEY"]:
        print("Note: OPENAI_API_KEY is not set; classification will not work.")
    if not (CFG["GOOGLE_CLIENT_ID"] and CFG["GOOGLE_CLIENT_SECRET"]):
        print("Note: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set; Gmail sign-in will not work.")
    print("Credit Note Desk running at http://localhost:%d  (Ctrl+C to stop)" % CFG["PORT"])
    app.run(host="127.0.0.1", port=CFG["PORT"], threaded=True)


if __name__ == "__main__":
    main()
