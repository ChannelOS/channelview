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
from datetime import datetime, timedelta

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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    verified INTEGER DEFAULT 0,
    verify_token TEXT,
    token_expires_at TIMESTAMP,
    signup_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_users_email ON inbox_users(email);
CREATE INDEX IF NOT EXISTS idx_inbox_users_alias ON inbox_users(alias);
CREATE INDEX IF NOT EXISTS idx_inbox_users_verify_token ON inbox_users(verify_token);
"""

# Idempotent ALTER TABLE migrations for when the table already exists without
# the new signup columns. Each is wrapped in try/except in init_inbox_schema.
SIGNUP_MIGRATIONS_PG = [
    "ALTER TABLE inbox_users ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE",
    "ALTER TABLE inbox_users ADD COLUMN IF NOT EXISTS verify_token TEXT",
    "ALTER TABLE inbox_users ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMP",
    "ALTER TABLE inbox_users ADD COLUMN IF NOT EXISTS signup_ip TEXT",
    "CREATE INDEX IF NOT EXISTS idx_inbox_users_verify_token ON inbox_users(verify_token)",
]
SIGNUP_MIGRATIONS_SQLITE = [
    "ALTER TABLE inbox_users ADD COLUMN verified INTEGER DEFAULT 0",
    "ALTER TABLE inbox_users ADD COLUMN verify_token TEXT",
    "ALTER TABLE inbox_users ADD COLUMN token_expires_at TIMESTAMP",
    "ALTER TABLE inbox_users ADD COLUMN signup_ip TEXT",
    "CREATE INDEX IF NOT EXISTS idx_inbox_users_verify_token ON inbox_users(verify_token)",
]


def init_inbox_schema():
    """Create inbox_users table if it does not exist, and run signup-column
    migrations for older deployments. Idempotent.
    """
    is_pg = os.environ.get('DATABASE_URL', '').startswith('postgres')

    # ---- Step 1: Ensure the base table + indexes exist ----
    try:
        conn = get_db(autocommit=True)
        sql = SCHEMA
        if is_pg:
            sql = sql.replace(
                'INTEGER PRIMARY KEY AUTOINCREMENT',
                'SERIAL PRIMARY KEY'
            ).replace(
                'brief_enabled INTEGER DEFAULT 1',
                'brief_enabled BOOLEAN DEFAULT TRUE'
            ).replace(
                'verified INTEGER DEFAULT 0',
                'verified BOOLEAN DEFAULT FALSE'
            )
        conn.executescript(sql) if hasattr(conn, 'executescript') else [
            conn.cursor().execute(stmt) for stmt in sql.split(';') if stmt.strip()
        ]
        try:
            conn.close()
        except Exception:
            pass
    except Exception as e:
        print(f"[inbox_agent] init_inbox_schema (base) failed: {e}")

    # ---- Step 2: Run signup-column migrations for older tables ----
    # Each ALTER is wrapped individually — "already exists" errors are expected
    # on re-runs and are safely ignored. PG gets IF NOT EXISTS; SQLite doesn't
    # support it on ADD COLUMN so we rely on try/except.
    migrations = SIGNUP_MIGRATIONS_PG if is_pg else SIGNUP_MIGRATIONS_SQLITE
    for stmt in migrations:
        try:
            conn = get_db(autocommit=True)
            try:
                conn.cursor().execute(stmt) if not hasattr(conn, 'executescript') \
                    else conn.executescript(stmt)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            msg = str(e).lower()
            if 'already exists' in msg or 'duplicate column' in msg:
                continue  # Already applied; fine.
            print(f"[inbox_agent] migration skipped ({stmt[:60]}...): {e}")


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

_SHARED_CSS = r"""
 :root{--green:#0ace0a;--green-dark:#08a808;--ink:#111;--muted:#555;--line:#e5e5e5;--tint:#e6fce6}
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
 .card p{margin:0 0 14px;color:var(--muted);font-size:15px}
 label{display:block;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#000;margin:14px 0 6px;font-weight:600}
 input[type=email],input[type=text]{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:8px;font-size:15px;font-family:inherit;color:#000}
 input:focus{outline:none;border-color:var(--green);box-shadow:0 0 0 3px rgba(10,206,10,.18)}
 .cta{display:inline-flex;align-items:center;gap:10px;background:var(--green);color:#000;border:0;
      padding:14px 22px;border-radius:8px;font-size:15px;font-weight:700;text-decoration:none;cursor:pointer;margin-top:18px}
 .cta:hover{background:var(--green-dark)}
 .cta-full{width:100%;justify-content:center}
 .steps{counter-reset:step;padding:0;margin:0 0 8px;list-style:none}
 .steps li{counter-increment:step;padding:6px 0 6px 34px;position:relative;font-size:15px;color:var(--muted)}
 .steps li::before{content:counter(step);position:absolute;left:0;top:4px;width:24px;height:24px;
      border-radius:50%;background:var(--green);color:#000;font-weight:700;font-size:13px;
      display:flex;align-items:center;justify-content:center}
 .alias-box{background:var(--tint);border:1px solid #c6efc6;border-radius:10px;padding:20px 22px;margin:22px 0 24px}
 .alias-box .label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#2a7a2a;margin-bottom:8px;font-weight:700}
 .alias-box .addr{font-size:22px;font-weight:700;color:#000;word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 .banner-error{background:#fff4f4;border:1px solid #f3c6c6;color:#9a1f1f;padding:12px 14px;border-radius:8px;margin:0 0 20px;font-size:14px}
 .foot{margin-top:48px;padding-top:24px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
 .foot a{color:#000}
 code{background:#f4f4f4;padding:2px 6px;border-radius:4px;font-size:14px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
"""


LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inbox Agent — Channel One</title>
<style>""" + _SHARED_CSS + r"""</style>
</head>
<body>
<div class="wrap">
  <div class="logo"><span class="u"></span> CHANNEL ONE &nbsp;·&nbsp; Inbox Agent</div>

  <h1>An email address that thinks.</h1>
  <p class="lede">Forward any email thread to your personal Inbox Agent address and get a clean
  TL;DR back in about ten seconds. Key people, decisions, what they're asking of you, and
  the obvious next step — all in plain English.</p>

  {% if error %}<div class="banner-error">{{ error }}</div>{% endif %}

  <div class="card">
    <h2>Get your address</h2>
    <p>Sign up in fifteen seconds. You'll get a verification email — click the link and your
    forwarding address is live.</p>
    <form method="POST" action="">
      <label for="first_name">First name</label>
      <input type="text" id="first_name" name="first_name" required maxlength="30"
             autocomplete="given-name" value="{{ first_name or '' }}">
      <label for="email">Your email address</label>
      <input type="email" id="email" name="email" required
             autocomplete="email" value="{{ email or '' }}"
             placeholder="you@company.com">
      <button class="cta cta-full" type="submit">Send my verification link →</button>
    </form>
  </div>

  <div class="card">
    <h2>How it works</h2>
    <ol class="steps">
      <li>Sign up and verify your email (one click)</li>
      <li>Get your personal forwarding address: <code>yourname-a3f8@inbox.mychannelview.com</code></li>
      <li>Forward any thread to it from your verified address</li>
      <li>Get a crisp summary back in your inbox within ~10 seconds</li>
    </ol>
  </div>

  <div class="foot">
    Questions? Email <a href="mailto:joe@channelonestrategies.com">joe@channelonestrategies.com</a>.
    Only the address you verify can forward threads to your alias — no one else can piggyback on it.
  </div>
</div>
</body>
</html>
"""


SIGNUP_PENDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Check your email — Inbox Agent</title>
<style>""" + _SHARED_CSS + r"""</style>
</head>
<body>
<div class="wrap">
  <div class="logo"><span class="u"></span> CHANNEL ONE &nbsp;·&nbsp; Inbox Agent</div>
  <h1>Check your email, {{ first_name }}.</h1>
  <p class="lede">We just sent a verification link to <strong>{{ email }}</strong>.
  Click it to activate your forwarding address. The link is good for 24 hours.</p>

  <div class="card">
    <h2>Didn't see it?</h2>
    <p>Give it a minute, then check your junk or spam folder — Inbox Agent is a new sender so
    some mailboxes route the first message there. Mark it "not junk" and future summaries
    will land in your inbox.</p>
    <p>Still nothing? <a href="/">Start over</a> or email
    <a href="mailto:joe@channelonestrategies.com">joe@channelonestrategies.com</a>.</p>
  </div>
</div>
</body>
</html>
"""


VERIFY_SUCCESS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>You're verified — Inbox Agent</title>
<style>""" + _SHARED_CSS + r"""</style>
</head>
<body>
<div class="wrap">
  <div class="logo"><span class="u"></span> CHANNEL ONE &nbsp;·&nbsp; Inbox Agent</div>
  <h1>You're in, {{ first_name }}.</h1>
  <p class="lede">Your personal forwarding address is live. Forward any email thread to it from
  <strong>{{ email }}</strong> and you'll get a summary back within about ten seconds.</p>

  <div class="alias-box">
    <div class="label">Your forwarding address</div>
    <div class="addr">{{ alias }}@{{ inbox_host }}</div>
  </div>

  <div class="card">
    <h2>How to use it</h2>
    <p>In your email client, forward any thread you want summarized to
    <code>{{ alias }}@{{ inbox_host }}</code>. The summary comes back as a reply.</p>
    <p>Only emails sent from <strong>{{ email }}</strong> will trigger a summary — no one else
    can use your alias.</p>
  </div>

  <div class="foot">
    Something off? Reply to your welcome email and Joe will take a look.
  </div>
</div>
</body>
</html>
"""


VERIFY_FAILED_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verification link expired — Inbox Agent</title>
<style>""" + _SHARED_CSS + r"""</style>
</head>
<body>
<div class="wrap">
  <div class="logo"><span class="u"></span> CHANNEL ONE &nbsp;·&nbsp; Inbox Agent</div>
  <h1>That link didn't work.</h1>
  <p class="lede">{{ message }}</p>
  <div class="card">
    <p><a href="/">← Start a new signup</a> or email
    <a href="mailto:joe@channelonestrategies.com">joe@channelonestrategies.com</a>.</p>
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


def _from_with_display_name() -> str:
    """Return AGENT_FROM_ADDRESS wrapped with a display name so it lands in
    real inboxes rather than junk folders. If the env var already includes a
    display name (RFC 5322 '"Name" <addr@x>'), leave it alone.
    """
    raw = AGENT_FROM_ADDRESS or ''
    if '<' in raw and '>' in raw:
        return raw
    return f'ChannelView Inbox Agent <{raw}>'


def postmark_send(to: str, subject: str, text_body: str,
                  reply_to: str = '', html_body: str = '') -> dict:
    """Send an email via Postmark. Returns parsed response.
    Always includes a plain-text body; optionally includes HTML for better
    client rendering. The From header carries a display name for deliverability.
    """
    if not POSTMARK_SERVER_TOKEN:
        raise RuntimeError('POSTMARK_SERVER_TOKEN not set — cannot send reply.')
    body = {
        'From': _from_with_display_name(),
        'To': to,
        'Subject': subject,
        'TextBody': text_body,
        'MessageStream': 'outbound',
    }
    if html_body:
        body['HtmlBody'] = html_body
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


def _verify_url(token: str) -> str:
    """Build the absolute verification URL for an email signup.

    Uses VERIFY_URL_BASE if set (e.g. 'https://mychannelview.com/__inbox'),
    otherwise defaults to https://{INBOX_HOST}. The main-domain path keeps
    the verify link working even before inbox.mychannelview.com has TLS.
    """
    base = os.environ.get('VERIFY_URL_BASE', '').rstrip('/')
    if not base:
        base = f'https://{INBOX_HOST}'
    return f'{base}/verify?token={urllib.parse.quote(token, safe="")}'


def send_verification_email(to: str, first_name: str, token: str) -> None:
    """Send the 'click-to-verify' email to a new signup."""
    url = _verify_url(token)
    text = (
        f"Hi {first_name},\n\n"
        "Thanks for signing up for Inbox Agent.\n\n"
        "Click the link below to activate your personal forwarding address:\n\n"
        f"{url}\n\n"
        "This link is good for 24 hours. If you didn't sign up, just ignore this "
        "message — your alias stays inactive.\n\n"
        "— Inbox Agent\n"
        "Channel One Strategies"
    )
    html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#111;line-height:1.55;max-width:560px;margin:0 auto;padding:32px 20px">
<p>Hi {first_name},</p>
<p>Thanks for signing up for <strong>Inbox Agent</strong>.</p>
<p>Click the button below to activate your personal forwarding address:</p>
<p style="margin:28px 0"><a href="{url}" style="background:#0ace0a;color:#000;padding:14px 22px;border-radius:8px;font-weight:700;text-decoration:none;display:inline-block">Verify my email</a></p>
<p style="font-size:13px;color:#555">Or paste this link in your browser:<br><a href="{url}" style="color:#555;word-break:break-all">{url}</a></p>
<p style="font-size:13px;color:#555">This link is good for 24 hours. If you didn't sign up, just ignore this message &mdash; your alias stays inactive.</p>
<p style="font-size:13px;color:#888;margin-top:36px">&mdash; Inbox Agent, Channel One Strategies</p>
</body></html>"""
    postmark_send(
        to=to,
        subject='Activate your Inbox Agent address',
        text_body=text,
        html_body=html,
        reply_to='joe@channelonestrategies.com',
    )


def send_welcome_email(to: str, first_name: str, alias: str) -> None:
    """Send the 'you're verified, here's your alias' email."""
    forward_addr = f'{alias}@{INBOX_HOST}'
    text = (
        f"Hi {first_name},\n\n"
        "Your Inbox Agent is live. Here's your personal forwarding address:\n\n"
        f"    {forward_addr}\n\n"
        f"Forward any email thread to {forward_addr} (from {to}) and you'll get "
        "a clean summary back in about ten seconds. Key people, main points, "
        "what they're asking of you, and the obvious next step.\n\n"
        "Give it a try with any messy thread you want a fast read on.\n\n"
        "— Inbox Agent\n"
        "Channel One Strategies"
    )
    html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#111;line-height:1.55;max-width:560px;margin:0 auto;padding:32px 20px">
<p>Hi {first_name},</p>
<p>Your <strong>Inbox Agent</strong> is live. Here's your personal forwarding address:</p>
<div style="background:#e6fce6;border:1px solid #c6efc6;border-radius:10px;padding:18px 20px;margin:22px 0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:18px;font-weight:700;color:#000;word-break:break-all">{forward_addr}</div>
<p>Forward any email thread to <strong>{forward_addr}</strong> (from {to}) and you'll get a clean summary back in about ten seconds. Key people, main points, what they're asking of you, and the obvious next step.</p>
<p>Give it a try with any messy thread you want a fast read on.</p>
<p style="font-size:13px;color:#888;margin-top:36px">&mdash; Inbox Agent, Channel One Strategies</p>
</body></html>"""
    postmark_send(
        to=to,
        subject='Your Inbox Agent address is live',
        text_body=text,
        html_body=html,
        reply_to='joe@channelonestrategies.com',
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
    """Return dict with id/email/first_name/alias/verified, or None."""
    conn = get_db()
    try:
        cur = conn.execute(
            'SELECT id, email, first_name, alias, verified FROM inbox_users WHERE alias = ?',
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
                'verified': bool(row['verified']) if row['verified'] is not None else False,
            }
        except (TypeError, KeyError, IndexError):
            return {
                'id': row[0], 'email': row[1],
                'first_name': row[2], 'alias': row[3],
                'verified': bool(row[4]) if row[4] is not None else False,
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
    return render_template_string(LANDING_HTML, error=None, email='', first_name='')


# ---------------------------------------------------------------------------
# Email-signup flow (replaces OAuth for MVP; OAuth remains wired for later)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')


def _find_user_by_email(email: str):
    """Return row dict or None. Used to detect duplicate signups."""
    conn = get_db()
    try:
        cur = conn.execute(
            'SELECT id, alias, email, first_name, verified FROM inbox_users WHERE LOWER(email) = ?',
            (email.lower(),)
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            return {
                'id': row['id'],
                'alias': row['alias'],
                'email': row['email'],
                'first_name': row['first_name'],
                'verified': bool(row['verified']) if row['verified'] is not None else False,
            }
        except (TypeError, KeyError, IndexError):
            return {
                'id': row[0], 'alias': row[1], 'email': row[2],
                'first_name': row[3],
                'verified': bool(row[4]) if row[4] is not None else False,
            }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _render_signup_form(error=None, email='', first_name=''):
    return render_template_string(
        LANDING_HTML, error=error, email=email, first_name=first_name
    )


def _handle_signup_post():
    """Process the signup form. Creates an unverified user row and sends the
    click-to-verify email. Idempotent on re-submits.
    """
    email = (request.form.get('email') or '').strip().lower()
    first_name = (request.form.get('first_name') or '').strip()[:30]

    if not email or not _EMAIL_RE.match(email):
        return _render_signup_form(
            error='Please enter a valid email address.',
            email=email, first_name=first_name
        ), 400
    if not first_name or not re.match(r"^[A-Za-z][A-Za-z\s\-'.]{0,29}$", first_name):
        return _render_signup_form(
            error='Please enter a first name (letters only).',
            email=email, first_name=first_name
        ), 400

    client_ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                 or request.remote_addr or '')
    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=24)

    existing = _find_user_by_email(email)

    conn = get_db(autocommit=True)
    try:
        if existing:
            if existing.get('verified'):
                try:
                    send_welcome_email(email, existing.get('first_name') or first_name,
                                       existing['alias'])
                except Exception as e:
                    print(f'[inbox_agent] re-welcome send failed: {e}')
                return render_template_string(
                    SIGNUP_PENDING_HTML,
                    first_name=existing.get('first_name') or first_name,
                    email=email,
                )
            conn.execute(
                """UPDATE inbox_users
                   SET verify_token = ?, token_expires_at = ?,
                       first_name = ?, signup_ip = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (token, expires_at, first_name, client_ip, existing['id'])
            )
        else:
            alias = generate_alias(first_name, _alias_exists)
            ms_obj_placeholder = f'local-{alias}'
            conn.execute(
                """INSERT INTO inbox_users
                   (ms_object_id, email, first_name, alias,
                    verified, verify_token, token_expires_at, signup_ip)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ms_obj_placeholder, email, first_name, alias,
                 False, token, expires_at, client_ip)
            )
    except Exception as e:
        print(f'[inbox_agent] signup db error: {e}')
        return _render_signup_form(
            error='Something went wrong on our end. Try again in a minute.',
            email=email, first_name=first_name
        ), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        send_verification_email(email, first_name, token)
    except Exception as e:
        print(f'[inbox_agent] verify email send failed: {e}')
        return _render_signup_form(
            error="We couldn't send the verification email. Check the address and try again.",
            email=email, first_name=first_name
        ), 500

    print(f'[inbox_agent] signup: sent verify link to {email} (first_name={first_name!r})')
    return render_template_string(
        SIGNUP_PENDING_HTML, first_name=first_name, email=email
    )


def _handle_verify():
    """GET /verify?token=... — activates an account and shows the success page."""
    token = (request.args.get('token') or '').strip()
    if not token:
        return render_template_string(
            VERIFY_FAILED_HTML,
            message='This verification link is missing its token. Try signing up again.'
        ), 400

    conn = get_db(autocommit=True)
    try:
        cur = conn.execute(
            """SELECT id, email, first_name, alias, verified, token_expires_at
               FROM inbox_users WHERE verify_token = ?""",
            (token,)
        )
        row = cur.fetchone()
        if not row:
            return render_template_string(
                VERIFY_FAILED_HTML,
                message=("We couldn't find a signup for this link. It may have "
                         "already been used or the signup was never completed.")
            ), 404

        try:
            rec = {
                'id': row['id'], 'email': row['email'],
                'first_name': row['first_name'], 'alias': row['alias'],
                'verified': row['verified'], 'token_expires_at': row['token_expires_at'],
            }
        except (TypeError, KeyError, IndexError):
            rec = {
                'id': row[0], 'email': row[1], 'first_name': row[2],
                'alias': row[3], 'verified': row[4], 'token_expires_at': row[5],
            }

        if rec.get('verified'):
            return render_template_string(
                VERIFY_SUCCESS_HTML,
                first_name=rec['first_name'] or 'there',
                email=rec['email'],
                alias=rec['alias'],
                inbox_host=INBOX_HOST,
            )

        exp = rec.get('token_expires_at')
        if exp is not None:
            if isinstance(exp, str):
                try:
                    exp = datetime.fromisoformat(exp.replace('Z', ''))
                except Exception:
                    exp = None
            if exp and exp < datetime.utcnow():
                return render_template_string(
                    VERIFY_FAILED_HTML,
                    message='This link expired (links are good for 24 hours). Please sign up again.'
                ), 410

        conn.execute(
            """UPDATE inbox_users
               SET verified = ?, verify_token = NULL, token_expires_at = NULL,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (True, rec['id'])
        )

        try:
            send_welcome_email(rec['email'], rec['first_name'] or 'there', rec['alias'])
        except Exception as e:
            print(f'[inbox_agent] welcome email send failed: {e}')

        print(f'[inbox_agent] verify: activated alias={rec["alias"]} email={rec["email"]}')
        return render_template_string(
            VERIFY_SUCCESS_HTML,
            first_name=rec['first_name'] or 'there',
            email=rec['email'],
            alias=rec['alias'],
            inbox_host=INBOX_HOST,
        )
    except Exception as e:
        print(f'[inbox_agent] verify error: {e}')
        return render_template_string(
            VERIFY_FAILED_HTML,
            message='Something went wrong verifying your email. Try the link again or sign up again.'
        ), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# OAuth handlers (kept for future use; not linked from the new landing page)
# ---------------------------------------------------------------------------

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
                       verified = ?, verify_token = NULL, token_expires_at = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE ms_object_id = ?""",
                (refresh_enc, email, display_name, first_name,
                 scopes_granted, ms_tenant_id, True, ms_object_id)
            )
        else:
            alias = generate_alias(first_name, _alias_exists)
            conn.execute(
                """INSERT INTO inbox_users
                   (ms_object_id, ms_tenant_id, email, display_name, first_name,
                    alias, refresh_token_enc, scopes, verified)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ms_object_id, ms_tenant_id, email, display_name,
                 first_name, alias, refresh_enc, scopes_granted, True)
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
        VERIFY_SUCCESS_HTML, alias=alias, first_name=first_name, email=email,
        inbox_host=INBOX_HOST,
    )


# ---------------------------------------------------------------------------
# Postmark inbound webhook
# ---------------------------------------------------------------------------

def _handle_inbound():
    """Postmark inbound webhook."""
    if POSTMARK_INBOUND_TOKEN:
        supplied = request.args.get('token', '')
        if not secrets.compare_digest(supplied, POSTMARK_INBOUND_TOKEN):
            print('[inbox_agent] inbound rejected: bad token')
            return ('forbidden', 403)

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception as e:
        print(f'[inbox_agent] inbound: failed to parse JSON: {e}')
        return ('bad json', 200)

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
        return ('unknown alias', 200)

    if not user.get('verified'):
        print(f'[inbox_agent] inbound: alias {alias!r} unverified; dropping silently')
        return ('unverified', 200)

    if from_email != (user['email'] or '').lower():
        print(f'[inbox_agent] inbound: sender {from_email!r} != owner {user["email"]!r}; rejecting')
        try:
            postmark_send(
                to=from_email,
                subject=f'Re: {subject}',
                text_body=(
                    "Thanks for the note. This Inbox Agent address only accepts "
                    "forwards from its owner's verified email. If you're the owner, "
                    "forward from the address you signed up with. Otherwise, please "
                    "contact the owner directly.\n\n- Inbox Agent"
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
                    "link or an attachment).\n\n- Inbox Agent"
                ),
            )
        except Exception:
            pass
        return ('empty body', 200)

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
                    "it - try forwarding again in a few minutes.\n\n"
                    f"(debug: {str(e)[:200]})\n\n- Inbox Agent"
                ),
            )
        except Exception:
            pass
        return ('summarize error', 200)

    reply_subject = subject if subject.lower().startswith('re:') else f'Re: {subject}'
    reply_body = (
        f"Hi {user['first_name'] or 'there'} -\n\n"
        f"{summary}\n\n"
        "- Inbox Agent\n"
        "(Reply to this email if the summary missed something; Joe will take a look.)"
    )
    html_summary = (
        '<p>Hi ' + (user['first_name'] or 'there') + ' &mdash;</p>'
        '<div style="white-space:pre-wrap;font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI',Helvetica,Arial,sans-serif;color:#111;line-height:1.55;font-size:15px"
        '">' + (summary.replace('<', '&lt;').replace('>', '&gt;')) + '</div>'
        '<p style="font-size:13px;color:#888;margin-top:24px">&mdash; Inbox Agent<br>'
        '<em>Reply to this email if the summary missed something; Joe will take a look.</em></p>'
    )
    try:
        postmark_send(
            to=from_email,
            subject=reply_subject,
            text_body=reply_body,
            html_body=html_summary,
            reply_to='joe@channelonestrategies.com',
        )
    except Exception as e:
        print(f'[inbox_agent] reply send failed: {e}')
        return ('send error', 200)

    print(f'[inbox_agent] inbound: summary delivered to {from_email} for alias {alias}')
    return ('ok', 200)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

_INBOX_ROUTES = {
    '/': _handle_landing,
    '/signup': _handle_landing,
    '/verify': _handle_verify,
    '/auth/microsoft/start': _handle_auth_start,
    '/auth/microsoft/callback': _handle_auth_callback,
    '/auth/success': _handle_auth_success,
}

_INBOX_POST_ROUTES = {
    '/__inbox/inbound': _handle_inbound,
    '/signup': _handle_signup_post,
}


def register_inbox_routes(app):
    """Install a before_request hook that serves inbox.mychannelview.com."""

    @app.before_request
    def _inbox_host_dispatcher():
        if not _is_inbox_host():
            return None

        path = request.path or '/'

        if path == '/__inbox/health':
            return {
                'ok': True,
                'service': 'inbox-agent',
                'time': datetime.utcnow().isoformat() + 'Z',
            }

        if request.method == 'POST':
            post_handler = _INBOX_POST_ROUTES.get(path)
            if post_handler is not None:
                return post_handler()
            if path in _INBOX_ROUTES:
                abort(405)
            abort(404)

        handler = _INBOX_ROUTES.get(path)
        if handler is None:
            abort(404)
        if request.method not in ('GET', 'HEAD'):
            abort(405)
        return handler()

    # Path-based fallbacks on the main domain so the signup flow is reachable
    # at https://mychannelview.com/__inbox/* even before inbox.mychannelview.com
    # has its own cert. Postmark's inbound webhook already uses this path.
    @app.route('/__inbox/signup', methods=['GET', 'POST'], endpoint='_inbox_path_signup')
    def _inbox_path_signup():
        if request.method == 'POST':
            return _handle_signup_post()
        return _render_signup_form()

    @app.route('/__inbox/verify', methods=['GET'], endpoint='_inbox_path_verify')
    def _inbox_path_verify():
        return _handle_verify()

    @app.route('/__inbox/inbound', methods=['POST'], endpoint='_inbox_path_inbound')
    def _inbox_path_inbound():
        return _handle_inbound()

    @app.route('/__inbox/health', methods=['GET'], endpoint='_inbox_path_health')
    def _inbox_path_health():
        return {
            'ok': True,
            'service': 'inbox-agent',
            'time': datetime.utcnow().isoformat() + 'Z',
        }
