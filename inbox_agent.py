"""
Inbox Agent — Connect-Outlook landing page and OAuth flow.
Served at https://inbox.mychannelview.com/ via host-header routing.

Entry points (callable from app.py):
    init_inbox_schema()            — creates inbox_users table (idempotent)
    register_inbox_routes(app)     — adds the /, /auth/microsoft/start, /auth/microsoft/callback,
                                     /auth/success routes. Active only when
                                     request.host == INBOX_HOST.

Env vars required:
    MS_CLIENT_ID                   (Azure app ID)
    MS_CLIENT_SECRET               (Azure app secret)
    MS_TENANT                      (default: "common")
    INBOX_REDIRECT_URI             (e.g. https://inbox.mychannelview.com/auth/microsoft/callback)
    INBOX_ENCRYPTION_KEY           (Fernet key for refresh-token at-rest encryption)
    INBOX_HOST                     (default: "inbox.mychannelview.com")
"""
import os
import secrets
import urllib.parse
import urllib.request
import urllib.error
import json
import base64
import hashlib
import hmac
import re
from datetime import datetime

from flask import request, redirect, abort, make_response, render_template_string

from database import get_db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INBOX_HOST = os.environ.get('INBOX_HOST', 'inbox.mychannelview.com')
MS_CLIENT_ID = os.environ.get('MS_CLIENT_ID', '')
MS_CLIENT_SECRET = os.environ.get('MS_CLIENT_SECRET', '')
MS_TENANT = os.environ.get('MS_TENANT', 'common')
INBOX_REDIRECT_URI = os.environ.get(
    'INBOX_REDIRECT_URI',
    f'https://{INBOX_HOST}/auth/microsoft/callback'
)
INBOX_ENCRYPTION_KEY = os.environ.get('INBOX_ENCRYPTION_KEY', '')

# --- Agent / inbound config ---
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001')
POSTMARK_SERVER_TOKEN = os.environ.get('POSTMARK_SERVER_TOKEN', '')
POSTMARK_INBOUND_TOKEN = os.environ.get('POSTMARK_INBOUND_TOKEN', '')  # Optional: verifies webhook origin
AGENT_FROM_ADDRESS = os.environ.get('AGENT_FROM_ADDRESS', f'agent@{INBOX_HOST}')

# Scopes requested during OAuth.
# - openid/email/profile/offline_access: needed to get an ID token + refresh token
# - Mail.Read / Mail.Send: primary mailbox read and send (user consent only)
#
# NOTE on scope choices:
#   Mail.Read.Shared / Mail.Send.Shared were intentionally REMOVED. Those scopes
#   trigger "Need admin approval" on any tenant where the user isn't a Global
#   Admin. Since Joe connects multiple M365 mailboxes across tenants he doesn't
#   admin, requiring shared-mailbox permissions would block him on every
#   third-party tenant. If we ever want to support shared mailboxes, it should
#   be a separate opt-in OAuth flow, not a blanket requirement.
#
#   Calendar.Read / Calendar.ReadWrite will be added in tasks #10-12 when we
#   build the morning brief. They also need to be added as delegated API
#   permissions on the Azure app registration before being requested here, or
#   AAD throws AADSTS650053 ("scope does not exist on the resource").
OAUTH_SCOPES = [
    'openid', 'email', 'profile', 'offline_access',
    'Mail.Read', 'Mail.Send',
]

AUTHORIZE_URL = f'https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/authorize'
TOKEN_URL = f'https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token'

STATE_COOKIE = 'inbox_oauth_state'


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ms_object_id TEXT UNIQUE NOT NULL,
    ms_tenant_id TEXT,
    email TEXT NOT NULL,
    display_name TEXT,
    first_name TEXT,
    alias TEXT UNIQUE NOT NULL,
    refresh_token_enc TEXT,
    scopes TEXT,
    brief_enabled INTEGER DEFAULT 1,
    brief_time_local TEXT DEFAULT '07:00',
    timezone TEXT DEFAULT 'America/Chicago',
    last_brief_sent_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_inbox_users_email ON inbox_users(email);
CREATE INDEX IF NOT EXISTS idx_inbox_users_alias ON inbox_users(alias);
"""


def init_inbox_schema():
    """Create inbox_users table if it does not exist. Idempotent."""
    try:
        conn = get_db(autocommit=True)
        # Postgres does not support AUTOINCREMENT — rewrite if running PG
        sql = SCHEMA
        if os.environ.get('DATABASE_URL', '').startswith('postgres'):
            sql = sql.replace(
                'INTEGER PRIMARY KEY AUTOINCREMENT',
                'SERIAL PRIMARY KEY'
            ).replace(
                'brief_enabled INTEGER DEFAULT 1',
                'brief_enabled BOOLEAN DEFAULT TRUE'
            )
        conn.executescript(sql) if hasattr(conn, 'executescript') else [
            conn.cursor().execute(stmt) for stmt in sql.split(';') if stmt.strip()
        ]
        try:
            conn.close()
        except Exception:
            pass
    except Exception as e:
        # Don't crash app startup if inbox schema fails — log and continue.
        print(f"[inbox_agent] init_inbox_schema failed: {e}")


# ---------------------------------------------------------------------------
# Encryption (refresh token at rest)
# ---------------------------------------------------------------------------

def _derive_key(passphrase: str) -> bytes:
    """Derive a 32-byte key from the env passphrase."""
    return hashlib.sha256(passphrase.encode('utf-8')).digest()


def encrypt_token(plaintext: str) -> str:
    """Encrypt a refresh token using AES-via-XOR-with-HMAC (stdlib-only).

    NOT best-in-class; it's a pragmatic stdlib approach until we wire in the
    `cryptography` library. Token is XOR'd with an HKDF-like stream derived
    from the key, and HMAC-signed for integrity.
    """
    if not INBOX_ENCRYPTION_KEY:
        raise RuntimeError('INBOX_ENCRYPTION_KEY not set')
    key = _derive_key(INBOX_ENCRYPTION_KEY)
    nonce = secrets.token_bytes(16)
    stream = b''
    counter = 0
    while len(stream) < len(plaintext.encode('utf-8')):
        stream += hashlib.sha256(key + nonce + counter.to_bytes(4, 'big')).digest()
        counter += 1
    pt_bytes = plaintext.encode('utf-8')
    ct = bytes(a ^ b for a, b in zip(pt_bytes, stream[:len(pt_bytes)]))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + mac + ct).decode('ascii')


def decrypt_token(ciphertext: str) -> str:
    if not INBOX_ENCRYPTION_KEY:
        raise RuntimeError('INBOX_ENCRYPTION_KEY not set')
    key = _derive_key(INBOX_ENCRYPTION_KEY)
    raw = base64.urlsafe_b64decode(ciphertext.encode('ascii'))
    nonce, mac, ct = raw[:16], raw[16:48], raw[48:]
    expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError('MAC check failed — refresh token tampered with')
    stream = b''
    counter = 0
    while len(stream) < len(ct):
        stream += hashlib.sha256(key + nonce + counter.to_bytes(4, 'big')).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(ct, stream[:len(ct)])).decode('utf-8')


# ---------------------------------------------------------------------------
# Alias generator
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '', (name or '').lower())
    return s[:20] or 'user'


def generate_alias(first_name: str, existing_check) -> str:
    """
    Generate a unique `firstname-XXXX@inbox.mychannelview.com` local part.
    `existing_check` is a callable(alias) -> bool that returns True if taken.
    Returns the local part only (no @domain).
    """
    base = _slug(first_name)
    for _ in range(10):
        suffix = secrets.token_hex(2)  # 4 hex chars
        candidate = f'{base}-{suffix}'
        if not existing_check(candidate):
            return candidate
    # Extremely unlikely to hit 10 collisions; use timestamp as fallback
    return f'{base}-{secrets.token_hex(4)}'


def _alias_exists(alias: str) -> bool:
    conn = get_db()
    try:
        cur = conn.execute('SELECT 1 FROM inbox_users WHERE alias = ?', (alias,))
        return cur.fetchone() is not None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _build_authorize_url(state: str) -> str:
    params = {
        'client_id': MS_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': INBOX_REDIRECT_URI,
        'response_mode': 'query',
        'scope': ' '.join(OAUTH_SCOPES),
        'state': state,
        'prompt': 'select_account',
    }
    return f'{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}'


def _exchange_code_for_tokens(code: str) -> dict:
    data = urllib.parse.urlencode({
        'client_id': MS_CLIENT_ID,
        'client_secret': MS_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': INBOX_REDIRECT_URI,
        'scope': ' '.join(OAUTH_SCOPES),
    }).encode('utf-8')
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'token exchange failed: {e.code} {body}')


def _parse_id_token(id_token: str) -> dict:
    """Decode an ID token payload WITHOUT signature verification.
    OK here because the token came directly from Microsoft over TLS in the
    token-endpoint response we just made — it's not a user-supplied value.
    """
    parts = id_token.split('.')
    if len(parts) != 3:
        return {}
    payload_b64 = parts[1] + '=' * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64).decode('utf-8'))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Templates (inline to keep feature self-contained)
# ---------------------------------------------------------------------------

LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inbox Agent — Channel One</title>
<style>
 :root{--green:#0ace0a;--green-dark:#08a808;--ink:#111;--muted:#555;--line:#e5e5e5}
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
      margin:0;color:var(--ink);background:#fff;line-height:1.55}
 .wrap{max-width:620px;margin:0 auto;padding:64px 24px 80px}
 .logo{font-weight:800;letter-spacing:-.02em;font-size:18px;color:#000;display:flex;align-items:center;gap:8px;margin-bottom:48px}
 .logo .u{display:inline-block;width:22px;height:22px;background:var(--green);border-radius:4px}
 h1{font-size:34px;line-height:1.15;margin:0 0 16px;letter-spacing:-.01em}
 .lede{font-size:17px;color:var(--muted);margin:0 0 36px;max-width:520px}
 .card{border:1px solid var(--line);border-radius:12px;padding:28px;margin-bottom:20px;background:#fff}
 .card h2{font-size:15px;text-transform:uppercase;letter-spacing:.05em;margin:0 0 8px;color:#000}
 .card p{margin:0 0 16px;color:var(--muted);font-size:15px}
 .cta{display:inline-flex;align-items:center;gap:10px;background:#000;color:#fff;border:0;
      padding:14px 22px;border-radius:8px;font-size:15px;font-weight:600;text-decoration:none;cursor:pointer}
 .cta:hover{background:#222}
 .cta.green{background:var(--green);color:#000}
 .cta.green:hover{background:var(--green-dark)}
 .steps{counter-reset:step;padding:0;margin:0 0 8px;list-style:none}
 .steps li{counter-increment:step;padding:6px 0 6px 34px;position:relative;font-size:15px;color:var(--muted)}
 .steps li::before{content:counter(step);position:absolute;left:0;top:4px;width:24px;height:24px;
      border-radius:50%;background:var(--green);color:#000;font-weight:700;font-size:13px;
      display:flex;align-items:center;justify-content:center}
 .foot{margin-top:48px;padding-top:24px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
 .foot a{color:#000}
 .ms-logo{width:18px;height:18px;display:inline-block;vertical-align:-3px;margin-right:2px}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo"><span class="u"></span> CHANNEL ONE &nbsp;·&nbsp; Inbox Agent</div>

  <h1>An email address that thinks.</h1>
  <p class="lede">Forward something to Inbox Agent — a scheduling back-and-forth, a messy client thread,
  a half-written reply you don't want to finish — and it handles the next step for you.
  Replies, drafts, summaries, follow-ups, reminders, all inside your existing inbox.</p>

  <div class="card">
    <h2>Connect Outlook to get started</h2>
    <p>Takes about two minutes. You'll sign in with your Microsoft 365 account and approve
    read/send permissions. When you're done, you'll get a personal forwarding address wired
    straight to your mailbox.</p>
    <a class="cta green" href="/auth/microsoft/start">
      <svg class="ms-logo" viewBox="0 0 23 23" xmlns="http://www.w3.org/2000/svg"><path fill="#f1511b" d="M1 1h10v10H1z"/><path fill="#80cc28" d="M12 1h10v10H12z"/><path fill="#00adef" d="M1 12h10v10H1z"/><path fill="#fbbc09" d="M12 12h10v10H12z"/></svg>
      Connect Outlook
    </a>
  </div>

  <div class="card">
    <h2>How it works</h2>
    <ol class="steps">
      <li>Sign in with your Microsoft 365 account (multi-business inboxes are supported — one connect covers all of them)</li>
      <li>Approve the read/send permissions (standard Microsoft Graph scopes, revocable any time)</li>
      <li>Get your personal forwarding address: <code>yourname-a3f8@inbox.mychannelview.com</code></li>
      <li>Forward any thread to it, or email <code>agent@inbox.mychannelview.com</code> with a plain-English ask</li>
    </ol>
  </div>

  <div class="foot">
    Questions? Email <a href="mailto:joe@channelonestrategies.com">joe@channelonestrategies.com</a>.
    Revoke access any time at <a href="https://account.microsoft.com/privacy">account.microsoft.com/privacy</a>.
  </div>
</div>
</body>
</html>
"""


SUCCESS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>You're connected — Inbox Agent</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
      margin:0;color:#111;background:#fff;line-height:1.55}
 .wrap{max-width:620px;margin:0 auto;padding:64px 24px 80px}
 .logo{font-weight:800;letter-spacing:-.02em;font-size:18px;color:#000;display:flex;align-items:center;gap:8px;margin-bottom:48px}
 .logo .u{display:inline-block;width:22px;height:22px;background:#0ace0a;border-radius:4px}
 h1{font-size:32px;line-height:1.15;margin:0 0 16px;letter-spacing:-.01em}
 p{font-size:16px;color:#555;margin:0 0 18px}
 .alias-box{background:#f6fff6;border:1px solid #c6efc6;border-radius:10px;padding:20px 22px;margin:22px 0 30px}
 .alias-box .label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#2a7a2a;margin-bottom:8px}
 .alias-box .addr{font-size:22px;font-weight:700;color:#000;word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 .card{border:1px solid #e5e5e5;border-radius:12px;padding:24px;margin-bottom:16px}
 .card h2{font-size:15px;text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px;color:#000}
 .card p{margin:0 0 8px}
 code{background:#f4f4f4;padding:2px 6px;border-radius:4px;font-size:14px}
 .foot{margin-top:40px;font-size:13px;color:#888}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo"><span class="u"></span> CHANNEL ONE &nbsp;·&nbsp; Inbox Agent</div>
  <h1>You're connected, {{ first_name }}.</h1>
  <p>Inbox Agent is now wired to <strong>{{ email }}</strong>. Here's your personal forwarding address:</p>

  <div class="alias-box">
    <div class="label">Your address</div>
    <div class="addr">{{ alias }}@inbox.mychannelview.com</div>
  </div>

  <div class="card">
    <h2>How to use it</h2>
    <p>Forward or CC <code>{{ alias }}@inbox.mychannelview.com</code> on any thread you want the agent to act on.</p>
    <p>Or email <code>agent@inbox.mychannelview.com</code> directly with a plain-English ask — "summarize the Oracle thread", "draft a follow-up to Henderson", "what's on my plate Thursday".</p>
  </div>

  <div class="card">
    <h2>Coming soon</h2>
    <p>Daily morning brief with your calendar, overnight triage, and open follow-ups — lands in your inbox at 7am. You'll get an email when it's live.</p>
  </div>

  <div class="foot">
    Something off? Reply to the confirmation email — Joe's watching it personally for early users.
    Revoke access at <a href="https://account.microsoft.com/privacy">account.microsoft.com/privacy</a>.
  </div>
</div>
</body>
</html>
"""


ERROR_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Connection failed</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:560px;margin:80px auto;padding:0 20px;color:#111}h1{color:#c00}a{color:#000}</style>
</head><body>
<h1>Connection didn't go through</h1>
<p>Something went sideways during the Microsoft sign-in handoff. The error was:</p>
<pre style="background:#f4f4f4;padding:12px;border-radius:6px;white-space:pre-wrap">{{ message }}</pre>
<p><a href="/">← Try again</a> or email <a href="mailto:joe@channelonestrategies.com">joe@channelonestrategies.com</a>.</p>
</body></html>
"""


# ---------------------------------------------------------------------------
# Agent plumbing: Claude call, Postmark send, thread extraction
# ---------------------------------------------------------------------------

def _http_post_json(url: str, headers: dict, body: dict, timeout: int = 30) -> dict:
    """Small stdlib-only JSON POST helper. Returns parsed JSON response body."""
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {e.code} from {url}: {err_body}')


def claude_summarize(thread_text: str, requester_email: str = '') -> str:
    """Call Anthropic API to summarize a forwarded email thread.
    Returns plain-text summary suitable for email reply.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError('ANTHROPIC_API_KEY not set — cannot summarize.')

    # Cap input to keep cost/latency sane. ~40k chars is roughly 10k tokens.
    capped = thread_text[:40000]
    if len(thread_text) > 40000:
        capped += "\n\n[truncated — original was {} chars]".format(len(thread_text))

    system_prompt = (
        "You are Inbox Agent, a sharp executive assistant who summarizes email "
        "threads forwarded to you. Produce a crisp plain-text summary of the "
        "thread. Structure it as: (1) one-sentence TL;DR, (2) the key people "
        "involved and their positions, (3) the 2-5 main points or decisions, "
        "(4) any explicit asks of the reader, (5) suggested next step if one "
        "is obvious. Skip signatures, legal boilerplate, and quoted duplicates. "
        "Be direct. No preamble. No sign-off. Plain text only (no markdown)."
    )

    user_content = (
        f"Forwarded by: {requester_email}\n\n"
        f"--- BEGIN THREAD ---\n{capped}\n--- END THREAD ---"
    )

    result = _http_post_json(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        body={
            'model': ANTHROPIC_MODEL,
            'max_tokens': 1024,
            'system': system_prompt,
            'messages': [{'role': 'user', 'content': user_content}],
        },
        timeout=60,
    )
    # Response shape: {"content":[{"type":"text","text":"..."}], ...}
    blocks = result.get('content', [])
    text_parts = [b.get('text', '') for b in blocks if b.get('type') == 'text']
    return ('\n'.join(text_parts)).strip() or '(empty summary)'


def postmark_send(to: str, subject: str, text_body: str, reply_to: str = '') -> dict:
    """Send a plain-text email via Postmark. Returns parsed response."""
    if not POSTMARK_SERVER_TOKEN:
        raise RuntimeError('POSTMARK_SERVER_TOKEN not set — cannot send reply.')
    body = {
        'From': AGENT_FROM_ADDRESS,
        'To': to,
        'Subject': subject,
        'TextBody': text_body,
        'MessageStream': 'outbound',
    }
    if reply_to:
        body['ReplyTo'] = reply_to
    return _http_post_json(
        'https://api.postmarkapp.com/email',
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-Postmark-Server-Token': POSTMARK_SERVER_TOKEN,
        },
        body=body,
        timeout=20,
    )


def _strip_html(html: str) -> str:
    """Very lightweight HTML -> text. Good enough for email bodies."""
    if not html:
        return ''
    # Drop script/style blocks entirely
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Convert <br> and </p> to newlines
    html = re.sub(r'<\s*br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</\s*p\s*>', '\n\n', html, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', html)
    # Basic entity decode
    text = (text
            .replace('&nbsp;', ' ')
            .replace('&amp;', '&')
            .replace('&lt;', '<')
            .replace('&gt;', '>')
            .replace('&quot;', '"')
            .replace('&#39;', "'"))
    # Collapse excess blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_thread_text(payload: dict) -> str:
    """Pull the best-available body text from a Postmark inbound payload."""
    text = (payload.get('TextBody') or '').strip()
    if text:
        return text
    html = payload.get('HtmlBody') or ''
    if html:
        return _strip_html(html)
    stripped = (payload.get('StrippedTextReply') or '').strip()
    return stripped


def _extract_recipient_alias(payload: dict) -> str:
    """From a Postmark inbound payload, find our alias in the recipients.
    Checks ToFull, CcFull, BccFull for any address ending in INBOX_HOST.
    Returns the local part (before @) of the first match, or ''.
    """
    candidates = []
    for key in ('ToFull', 'CcFull', 'BccFull'):
        for r in payload.get(key) or []:
            addr = (r.get('Email') or '').lower().strip()
            if addr:
                candidates.append(addr)
    # Fallback to plain To/Cc/Bcc strings
    for key in ('To', 'Cc', 'Bcc', 'OriginalRecipient'):
        v = payload.get(key) or ''
        if not v:
            continue
        # "Name <addr@x>, Name2 <addr2@x>"
        for part in re.findall(r'<([^>]+)>|([^,\s]+@[^,\s]+)', v):
            addr = (part[0] or part[1]).lower().strip()
            if addr:
                candidates.append(addr)

    host = INBOX_HOST.lower()
    for addr in candidates:
        if addr.endswith('@' + host):
            local = addr.split('@', 1)[0]
            return local
    return ''


def _lookup_user_by_alias(alias: str):
    """Return (id, email, first_name, alias) or None."""
    conn = get_db()
    try:
        cur = conn.execute(
            'SELECT id, email, first_name, alias FROM inbox_users WHERE alias = ?',
            (alias,)
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            return {
                'id': row['id'],
                'email': row['email'],
                'first_name': row['first_name'],
                'alias': row['alias'],
            }
        except (TypeError, KeyError, IndexError):
            return {
                'id': row[0], 'email': row[1],
                'first_name': row[2], 'alias': row[3],
            }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def _is_inbox_host() -> bool:
    host = (request.host or '').lower()
    # Strip port if present
    host = host.split(':')[0]
    return host == INBOX_HOST.lower()


def _handle_landing():
    return LANDING_HTML


def _handle_auth_start():
    if not MS_CLIENT_ID or not MS_CLIENT_SECRET:
        return render_template_string(
            ERROR_HTML,
            message='Server not configured (MS_CLIENT_ID / MS_CLIENT_SECRET missing).'
        ), 500
    state = secrets.token_urlsafe(24)
    url = _build_authorize_url(state)
    resp = make_response(redirect(url, code=302))
    resp.set_cookie(
        STATE_COOKIE, state,
        max_age=600, secure=True, httponly=True, samesite='Lax'
    )
    return resp


def _handle_auth_callback():
    err = request.args.get('error')
    if err:
        desc = request.args.get('error_description', '')
        return render_template_string(ERROR_HTML, message=f'{err}: {desc}'), 400

    code = request.args.get('code')
    state = request.args.get('state')
    cookie_state = request.cookies.get(STATE_COOKIE)

    if not code or not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        return render_template_string(
            ERROR_HTML,
            message='State mismatch — possible CSRF. Please start over.'
        ), 400

    try:
        tokens = _exchange_code_for_tokens(code)
    except Exception as e:
        return render_template_string(ERROR_HTML, message=str(e)), 500

    refresh_token = tokens.get('refresh_token', '')
    id_token = tokens.get('id_token', '')
    scopes_granted = tokens.get('scope', '')

    if not refresh_token:
        return render_template_string(
            ERROR_HTML,
            message='No refresh token returned — offline_access scope may be missing.'
        ), 500

    claims = _parse_id_token(id_token)
    ms_object_id = claims.get('oid') or claims.get('sub', '')
    ms_tenant_id = claims.get('tid', '')
    email = (claims.get('email')
             or claims.get('preferred_username')
             or claims.get('upn')
             or '').lower()
    display_name = claims.get('name', '')
    if display_name:
        first_name = claims.get('given_name') or display_name.split(' ')[0]
    else:
        first_name = claims.get('given_name') or 'user'

    if not ms_object_id or not email:
        return render_template_string(
            ERROR_HTML,
            message='Microsoft did not return a usable account identity.'
        ), 500

    conn = get_db(autocommit=True)
    try:
        cur = conn.execute(
            'SELECT id, alias FROM inbox_users WHERE ms_object_id = ?',
            (ms_object_id,)
        )
        existing = cur.fetchone()
        refresh_enc = encrypt_token(refresh_token)

        if existing:
            try:
                alias = existing['alias']
            except (TypeError, KeyError, IndexError):
                alias = existing[1]
            conn.execute(
                """UPDATE inbox_users
                   SET refresh_token_enc = ?, email = ?, display_name = ?,
                       first_name = ?, scopes = ?, ms_tenant_id = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE ms_object_id = ?""",
                (refresh_enc, email, display_name, first_name,
                 scopes_granted, ms_tenant_id, ms_object_id)
            )
        else:
            alias = generate_alias(first_name, _alias_exists)
            conn.execute(
                """INSERT INTO inbox_users
                   (ms_object_id, ms_tenant_id, email, display_name, first_name,
                    alias, refresh_token_enc, scopes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ms_object_id, ms_tenant_id, email, display_name,
                 first_name, alias, refresh_enc, scopes_granted)
            )
    except Exception as e:
        return render_template_string(ERROR_HTML, message=f'Database error: {e}'), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    resp = make_response(redirect('/auth/success', code=302))
    resp.set_cookie('inbox_alias', alias, max_age=300, secure=True, httponly=False, samesite='Lax')
    resp.set_cookie('inbox_first', first_name, max_age=300, secure=True, httponly=False, samesite='Lax')
    resp.set_cookie('inbox_email', email, max_age=300, secure=True, httponly=False, samesite='Lax')
    resp.set_cookie(STATE_COOKIE, '', max_age=0, secure=True, httponly=True)
    return resp


def _handle_auth_success():
    alias = request.cookies.get('inbox_alias', 'your-alias')
    first_name = request.cookies.get('inbox_first', 'there')
    email = request.cookies.get('inbox_email', 'your account')
    return render_template_string(
        SUCCESS_HTML, alias=alias, first_name=first_name, email=email
    )


def _handle_inbound():
    """Postmark inbound webhook.
    Expects a JSON body from Postmark's inbound stream. Looks up the target
    alias in inbox_users, runs the forwarded thread through Claude, and
    emails the summary back to the sender.
    Always returns 200 to Postmark after logging — Postmark retries on non-2xx
    and we never want duplicate summaries.
    """
    # Optional webhook-auth: if POSTMARK_INBOUND_TOKEN is set, require it on a
    # query arg ?token=... so only Postmark (or anyone we trust) can POST here.
    if POSTMARK_INBOUND_TOKEN:
        supplied = request.args.get('token', '')
        if not secrets.compare_digest(supplied, POSTMARK_INBOUND_TOKEN):
            print('[inbox_agent] inbound rejected: bad token')
            return ('forbidden', 403)

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception as e:
        print(f'[inbox_agent] inbound: failed to parse JSON: {e}')
        return ('bad json', 200)  # 200 so Postmark doesn't retry junk

    from_email = (payload.get('From') or '').lower().strip()
    from_name = payload.get('FromName') or ''
    subject = payload.get('Subject') or '(no subject)'
    message_id = payload.get('MessageID') or ''

    print(f'[inbox_agent] inbound from={from_email!r} subject={subject!r} msgid={message_id!r}')

    alias = _extract_recipient_alias(payload)
    if not alias:
        print('[inbox_agent] inbound: no alias found in recipients; dropping')
        return ('no alias', 200)

    user = _lookup_user_by_alias(alias)
    if not user:
        print(f'[inbox_agent] inbound: alias {alias!r} not found in inbox_users')
        # Silently drop — don't reveal whether alias exists
        return ('unknown alias', 200)

    # Security gate: the inbound sender must be the mailbox owner. Prevents
    # a random outsider from triggering summaries on behalf of a connected user.
    if from_email != (user['email'] or '').lower():
        print(f'[inbox_agent] inbound: sender {from_email!r} != owner {user["email"]!r}; rejecting')
        # Bounce with a polite note (to the actual sender), but only once per
        # message by relying on Postmark's dedupe on MessageID.
        try:
            postmark_send(
                to=from_email,
                subject=f'Re: {subject}',
                text_body=(
                    "Thanks for the note. This address is wired to a specific "
                    "Microsoft 365 mailbox and only accepts forwards from that "
                    "mailbox's owner. If you're the owner, forward from the "
                    "signed-in account. Otherwise, please contact the owner "
                    "directly.\n\n— Inbox Agent"
                ),
            )
        except Exception as e:
            print(f'[inbox_agent] bounce send failed: {e}')
        return ('sender mismatch', 200)

    thread = extract_thread_text(payload)
    if not thread:
        print('[inbox_agent] inbound: empty body; nothing to summarize')
        try:
            postmark_send(
                to=from_email,
                subject=f'Re: {subject}',
                text_body=(
                    "I got your message but couldn't find any thread text to "
                    "summarize. Try forwarding the original email (not just a "
                    "link or an attachment).\n\n— Inbox Agent"
                ),
            )
        except Exception:
            pass
        return ('empty body', 200)

    # Summarize via Claude
    try:
        summary = claude_summarize(thread, requester_email=from_email)
    except Exception as e:
        print(f'[inbox_agent] summarize failed: {e}')
        try:
            postmark_send(
                to=from_email,
                subject=f'Re: {subject}',
                text_body=(
                    "Inbox Agent hit a snag running the summary. I've logged "
                    "it — try forwarding again in a few minutes. If it keeps "
                    "happening, reply to this note and Joe will take a look.\n\n"
                    f"(debug: {str(e)[:200]})\n\n— Inbox Agent"
                ),
            )
        except Exception:
            pass
        return ('summarize error', 200)

    # Reply to the forwarder with the summary
    reply_subject = subject if subject.lower().startswith('re:') else f'Re: {subject}'
    reply_body = (
        f"Hi {user['first_name'] or 'there'} —\n\n"
        f"{summary}\n\n"
        "— Inbox Agent\n"
        "(Reply to this email if the summary missed something; I'll take another pass.)"
    )
    try:
        postmark_send(to=from_email, subject=reply_subject, text_body=reply_body)
    except Exception as e:
        print(f'[inbox_agent] reply send failed: {e}')
        return ('send error', 200)

    print(f'[inbox_agent] inbound: summary delivered to {from_email} for alias {alias}')
    return ('ok', 200)


# Map of path -> handler for the inbox host. Keep this narrow; any path not
# listed returns 404 on the inbox host (we don't want to leak channelview UI
# through inbox.mychannelview.com).
_INBOX_ROUTES = {
    '/': _handle_landing,
    '/auth/microsoft/start': _handle_auth_start,
    '/auth/microsoft/callback': _handle_auth_callback,
    '/auth/success': _handle_auth_success,
}

# POST-only routes (webhooks, form posts). Listed separately so the dispatcher
# can enforce the correct method per route.
_INBOX_POST_ROUTES = {
    '/__inbox/inbound': _handle_inbound,
}


def register_inbox_routes(app):
    """Install a before_request hook that serves inbox.mychannelview.com
    entirely, leaving channelview (mychannelview.com) untouched.

    Because this runs as a before_request, it pre-empts Flask's route
    dispatcher for inbox-host requests — even if channelview has a `/`
    route registered, inbox gets there first.
    """

    @app.before_request
    def _inbox_host_dispatcher():
        if not _is_inbox_host():
            return None  # Let channelview's normal routing handle it

        path = request.path or '/'

        # Health check
        if path == '/__inbox/health':
            return {
                'ok': True,
                'service': 'inbox-agent',
                'time': datetime.utcnow().isoformat() + 'Z',
            }

        # POST-only routes (webhooks) get first dibs
        if request.method == 'POST':
            post_handler = _INBOX_POST_ROUTES.get(path)
            if post_handler is not None:
                return post_handler()
            # POSTing to a GET-only route => 405
            if path in _INBOX_ROUTES:
                abort(405)
            abort(404)

        # GET (and HEAD) — look up in the GET route table
        handler = _INBOX_ROUTES.get(path)
        if handler is None:
            abort(404)
        if request.method not in ('GET', 'HEAD'):
            abort(405)
        return handler()
