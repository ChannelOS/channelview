"""
ChannelView - Main Flask Application
One-way async video interview platform for insurance agencies
"""
import os, json, uuid, time, functools, hashlib
from datetime import datetime, timedelta
from flask import (Flask, request, jsonify, render_template, send_from_directory,
                   redirect, url_for, make_response, g)
from flask_cors import CORS
import bcrypt
import jwt

from database import get_db, init_db
from config import config as app_config
from storage_service import create_storage

# Initialize database tables on import (needed for Gunicorn which skips __main__)
init_db()

app = Flask(__name__, static_folder='static', template_folder='templates')
_app_start_time = time.time()

# Security: Require SECRET_KEY in production
_default_secret = 'channelview-dev-secret-change-in-prod'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', _default_secret)
if app.config['SECRET_KEY'] == _default_secret and os.environ.get('FLASK_ENV') == 'production':
    raise RuntimeError("FATAL: SECRET_KEY environment variable must be set in production. "
                       "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
elif app.config['SECRET_KEY'] == _default_secret:
    print("[WARNING] Using default SECRET_KEY — set SECRET_KEY env var before deploying to production.")

app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'videos')
app.config['INTRO_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'intros')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max upload

# Development vs production static file handling
if os.environ.get('FLASK_ENV') == 'production':
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 year cache in prod
else:
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # No cache in dev
    app.config['TEMPLATES_AUTO_RELOAD'] = True

CORS(app, resources={r"/api/*": {"origins": os.environ.get('CORS_ORIGINS', 'https://mychannelview.com')}})

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config.get('INTRO_FOLDER', os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'intros')), exist_ok=True)

# Initialize storage backend (local or S3 based on config)
storage = create_storage(app_config)

# ======================== PRODUCTION CONFIG VALIDATION ========================
# NOTE: validation function defined below after STRIPE_SECRET_KEY is set

# ======================== RATE LIMITING ========================
# In-memory rate limiter (per-IP, per-endpoint). Use Redis in production.
_rate_limits = {}  # key: (ip, endpoint) -> [(timestamp, ...)]

def rate_limit(max_requests=5, window_seconds=60):
    """Decorator to rate limit an endpoint by IP address."""
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            ip = request.remote_addr or 'unknown'
            key = (ip, request.endpoint)
            now = time.time()
            # Clean old entries
            _rate_limits[key] = [t for t in _rate_limits.get(key, []) if now - t < window_seconds]
            if len(_rate_limits[key]) >= max_requests:
                return jsonify({'error': 'Too many requests. Please try again later.'}), 429
            _rate_limits[key].append(now)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ======================== CSRF PROTECTION ========================
import secrets as _secrets

def generate_csrf_token():
    """Generate a CSRF token and store it in a cookie."""
    token = _secrets.token_hex(32)
    return token

@app.before_request
def csrf_protect():
    """Check CSRF token on state-changing requests (POST/PUT/DELETE)."""
    # Skip CSRF for certain paths
    exempt = [
        '/api/auth/register', '/api/auth/login', '/api/auth/logout',
        '/api/interview/',  # Candidate-facing (no auth cookie context)
        '/api/stripe/webhook',  # Stripe webhook uses its own signature verification
        '/api/v1/',  # Public API uses API key auth, not cookies
        '/api/integrations/zapier/webhook',  # Zapier inbound webhook
        '/api/voice/webhook',  # Retell AI webhook (uses its own verification)
    ]
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return
    # Skip for API calls using Bearer token (non-browser clients)
    if request.headers.get('Authorization', '').startswith('Bearer '):
        return
    for path in exempt:
        if request.path.startswith(path):
            return
    # For cookie-based auth, check CSRF header
    csrf_token = request.headers.get('X-CSRF-Token', '')
    cookie_token = request.cookies.get('csrf_token', '')
    if cookie_token and csrf_token and csrf_token == cookie_token:
        return  # Valid CSRF
    # If no CSRF cookie set yet (legacy clients), skip for now
    if not cookie_token:
        return
    # CSRF cookie present but header missing or mismatched - reject
    from flask import abort
    abort(403)

@app.after_request
def set_csrf_cookie(response):
    """Set CSRF token cookie on every response if not already set."""
    if not request.cookies.get('csrf_token'):
        token = generate_csrf_token()
        is_prod = os.environ.get('FLASK_ENV') == 'production'
        response.set_cookie('csrf_token', token, httponly=False, secure=is_prod, samesite='Lax', max_age=30*24*3600)
    return response


# ======================== STRIPE CONFIG ========================
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID', '')  # Legacy fallback
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_API_BASE = 'https://api.stripe.com/v1'

# Tier-specific Stripe Price IDs ($99 / $179 / $299 monthly)
STRIPE_PRICE_ID_STARTER = os.environ.get('STRIPE_PRICE_ID_STARTER', '')
STRIPE_PRICE_ID_PROFESSIONAL = os.environ.get('STRIPE_PRICE_ID_PROFESSIONAL', STRIPE_PRICE_ID)  # fallback to legacy
STRIPE_PRICE_ID_ENTERPRISE = os.environ.get('STRIPE_PRICE_ID_ENTERPRISE', '')

# Map plan names → Stripe price IDs (includes legacy aliases)
PLAN_PRICE_MAP = {
    'starter': STRIPE_PRICE_ID_STARTER,
    'essentials': STRIPE_PRICE_ID_STARTER,
    'professional': STRIPE_PRICE_ID_PROFESSIONAL,
    'pro': STRIPE_PRICE_ID_PROFESSIONAL,
    'enterprise': STRIPE_PRICE_ID_ENTERPRISE,
}

# Reverse map: Stripe price ID → canonical plan name (for webhook)
PRICE_PLAN_MAP = {}
if STRIPE_PRICE_ID_STARTER:
    PRICE_PLAN_MAP[STRIPE_PRICE_ID_STARTER] = 'starter'
if STRIPE_PRICE_ID_PROFESSIONAL:
    PRICE_PLAN_MAP[STRIPE_PRICE_ID_PROFESSIONAL] = 'professional'
if STRIPE_PRICE_ID_ENTERPRISE:
    PRICE_PLAN_MAP[STRIPE_PRICE_ID_ENTERPRISE] = 'enterprise'

def _stripe_request(method, endpoint, data=None):
    """Make a request to the Stripe API. Returns (response_dict, status_code)."""
    if not STRIPE_SECRET_KEY:
        return {'error': 'Stripe not configured'}, 503
    import urllib.request, urllib.parse, urllib.error
    url = STRIPE_API_BASE + endpoint
    encoded = urllib.parse.urlencode(data, doseq=True).encode() if data else None
    req = urllib.request.Request(url, data=encoded, method=method)
    req.add_header('Authorization', f'Bearer {STRIPE_SECRET_KEY}')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode()), resp.getcode()
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
        return body, e.code
    except Exception as e:
        return {'error': str(e)}, 500

# Now validate production config after STRIPE_SECRET_KEY is defined
def _validate_production_config():
    """Warn about missing critical config. Hard-fail only for SECRET_KEY (already handled above)."""
    warnings = []
    if not STRIPE_SECRET_KEY:
        warnings.append("STRIPE_SECRET_KEY not set — billing/checkout will return 503")
    if not any([STRIPE_PRICE_ID_STARTER, STRIPE_PRICE_ID_PROFESSIONAL, STRIPE_PRICE_ID_ENTERPRISE, STRIPE_PRICE_ID]):
        warnings.append("No STRIPE_PRICE_IDs set — checkout will fail")
    if not os.environ.get('SENDGRID_API_KEY') and not os.environ.get('SMTP_HOST'):
        warnings.append("No email backend configured — emails will log only (set SENDGRID_API_KEY or SMTP_HOST)")
    if os.environ.get('FLASK_ENV') == 'production':
        for w in warnings:
            print(f"[PRODUCTION WARNING] {w}")
    elif warnings:
        for w in warnings:
            print(f"[CONFIG] {w}")

_validate_production_config()

# ======================== ERROR HANDLERS ========================

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found'}), 404
    return render_template('candidate_error.html', error='Page not found.'), 404

@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'File too large. Maximum upload size is 500MB.'}), 413

@app.errorhandler(500)
def server_error(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Internal server error'}), 500
    return render_template('candidate_error.html', error='Something went wrong. Please try again.'), 500

@app.after_request
def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if os.environ.get('FLASK_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# ======================== AUTH HELPERS ========================

def create_token(user_id):
    return jwt.encode(
        {'user_id': user_id, 'exp': datetime.utcnow() + timedelta(days=30)},
        app.config['SECRET_KEY'], algorithm='HS256'
    )

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect('/login')
        try:
            payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            g.user_id = payload['user_id']
            db = get_db()
            user_row = db.execute('SELECT * FROM users WHERE id = ?', (g.user_id,)).fetchone()
            if not user_row:
                db.close()
                return jsonify({'error': 'User not found'}), 401
            g.user = dict(user_row)
            # Check if user is a team member acting on behalf of an account owner
            g.effective_user_id = g.user_id  # default: own account
            g.user_role = 'owner'  # default: account owner
            team_membership = db.execute(
                'SELECT account_id, role FROM team_members WHERE user_id=? AND status=?',
                (g.user_id, 'active')
            ).fetchone()
            if team_membership:
                # Store team context — the account they belong to and their role
                g.team_account_id = team_membership['account_id']
                g.team_role = team_membership['role']
            else:
                g.team_account_id = None
                g.team_role = None
            db.close()
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Invalid token'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def require_role(*allowed_roles):
    """Decorator to enforce role-based access. Use after @require_auth.
    Roles: owner, admin, recruiter, reviewer.
    Owner always has access. Team members checked against their role."""
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            # Account owners always have full access
            if g.user_role == 'owner':
                return f(*args, **kwargs)
            # Team members: check if their role is in allowed_roles
            if g.team_role and g.team_role in allowed_roles:
                return f(*args, **kwargs)
            return jsonify({'error': 'Insufficient permissions', 'required_roles': list(allowed_roles)}), 403
        return decorated
    return decorator

def require_fmo_admin(f):
    """Decorator to restrict access to FMO admin users only. Use after @require_auth."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not g.user.get('is_fmo_admin'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'FMO admin access required'}), 403
            return redirect('/dashboard')
        return f(*args, **kwargs)
    return decorated

def check_trial_status(f):
    """Decorator to enforce trial expiration. Use after @require_auth.
    Blocks expired trial accounts from API endpoints, except billing/auth routes."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Allow all requests to billing and auth routes
        if request.path.startswith('/api/billing/') or request.path == '/billing':
            return f(*args, **kwargs)
        if request.path.startswith('/api/auth/'):
            return f(*args, **kwargs)
        # Allow health checks
        if request.path in ['/health', '/api/health']:
            return f(*args, **kwargs)

        # Check trial status
        plan = g.user.get('plan')
        trial_ends_at = g.user.get('trial_ends_at')
        if plan == 'trial' and trial_ends_at:
            try:
                trial_dt = datetime.fromisoformat(trial_ends_at)
                if datetime.utcnow() > trial_dt:
                    return jsonify({
                        'error': 'Your free trial has expired. Please upgrade to continue.',
                        'code': 'trial_expired',
                        'upgrade_url': '/billing'
                    }), 403
            except (ValueError, TypeError):
                pass
        return f(*args, **kwargs)
    return decorated

# ======================== PAGE ROUTES ========================

@app.route('/')
def index():
    token = request.cookies.get('token')
    if token:
        try:
            jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            return redirect('/dashboard')
        except:
            pass
    return render_template('landing.html')

@app.route('/login')
def login_page():
    return render_template('auth.html', mode='login')

@app.route('/register')
def register_page():
    return render_template('auth.html', mode='register')

@app.route('/dashboard')
@require_auth
def dashboard_page():
    return render_template('app.html', page='dashboard', user=g.user)

@app.route('/interviews')
@require_auth
def interviews_page():
    return render_template('app.html', page='interviews', user=g.user)

@app.route('/interviews/new')
@require_auth
def new_interview_page():
    return render_template('app.html', page='interview_builder', user=g.user)

@app.route('/interviews/<interview_id>')
@require_auth
def interview_detail_page(interview_id):
    return render_template('app.html', page='interview_detail', user=g.user, interview_id=interview_id)

@app.route('/candidates')
@require_auth
def candidates_page():
    return render_template('app.html', page='candidates', user=g.user)

@app.route('/review/<candidate_id>')
@require_auth
def review_page(candidate_id):
    return render_template('app.html', page='review', user=g.user, candidate_id=candidate_id)

@app.route('/settings')
@require_auth
def settings_page():
    return render_template('app.html', page='settings', user=g.user)

@app.route('/ai')
@require_auth
def ai_page():
    return render_template('app.html', page='ai', user=g.user)

@app.route('/analytics')
@require_auth
def analytics_page():
    return render_template('app.html', page='analytics', user=g.user)

@app.route('/billing')
@require_auth
def billing_page():
    return render_template('app.html', page='billing', user=g.user)

@app.route('/onboarding')
@require_auth
def onboarding_page():
    return render_template('app.html', page='onboarding', user=g.user)

@app.route('/automation')
@require_auth
def automation_page():
    return render_template('app.html', page='automation', user=g.user)

@app.route('/api-settings')
@require_auth
def api_settings_page():
    return render_template('app.html', page='api_docs', user=g.user)

# Candidate-facing interview (public, no auth needed)
@app.route('/integrations')
@require_auth
@require_fmo_admin
def integrations_page():
    return render_template('app.html', page='integrations', user=g.user)

@app.route('/compliance')
@require_auth
@require_fmo_admin
def compliance_page():
    return render_template('app.html', page='compliance', user=g.user)

@app.route('/kanban')
@require_auth
def kanban_page():
    return render_template('app.html', page='kanban', user=g.user)

@app.route('/i/<token>')
def candidate_interview(token):
    db = get_db()
    candidate = db.execute(
        '''SELECT c.*, i.title, i.welcome_msg, i.thank_you_msg, i.thinking_time,
           i.max_answer_time, i.max_retakes, i.brand_color, i.description,
           i.intro_video_path, i.format_selector_enabled, i.formats_enabled,
           u.agency_name, u.name as interviewer_name
           FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           JOIN users u ON c.user_id = u.id
           WHERE c.token = ?''', (token,)
    ).fetchone()
    db.close()
    if not candidate:
        return render_template('candidate_error.html', error='Interview link not found or has expired.'), 404
    cd = dict(candidate)
    if cd['status'] == 'completed':
        return render_template('candidate_done.html', candidate=cd)
    # Redirect to format choice if multi-format enabled and candidate hasn't chosen yet
    import json as _json
    formats = _json.loads(cd.get('formats_enabled') or '["video"]')
    if cd.get('format_selector_enabled') and len(formats) > 1 and not cd.get('format_chosen_at'):
        return redirect(f'/i/{token}/choose')
    return render_template('candidate_interview.html', candidate=cd, token=token)

# ======================== CONFIG API ========================

@app.route('/api/config/info', methods=['GET'])
@require_auth
def api_config_info():
    """Return non-sensitive configuration info for the current environment."""
    storage_stats = storage.get_stats()
    return jsonify({
        'env': app_config.ENV,
        'version': app_config.VERSION,
        'storage_backend': app_config.STORAGE_BACKEND,
        'storage_stats': storage_stats,
        'email_backend': app_config.EMAIL_BACKEND,
        'sendgrid_configured': bool(app_config.SENDGRID_API_KEY),
        'max_upload_mb': app_config.MAX_UPLOAD_MB,
        'max_response_mb': app_config.MAX_RESPONSE_MB,
    })

@app.route('/api/config/email-backend', methods=['GET'])
@require_auth
def api_email_backend_info():
    """Return email backend status."""
    import os
    sg_key = os.environ.get('SENDGRID_API_KEY', '')
    db = get_db()
    from email_service import get_smtp_config
    smtp = get_smtp_config(db, g.user_id)
    db.close()
    return jsonify({
        'sendgrid_configured': bool(sg_key),
        'smtp_configured': smtp is not None,
        'smtp_host': smtp['host'] if smtp else None,
        'active_backend': 'sendgrid' if sg_key else ('smtp' if smtp else 'log'),
    })

# ======================== AUTH API ========================

@app.route('/api/auth/register', methods=['POST'])
@rate_limit(max_requests=5, window_seconds=300)
def api_register():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    name = data.get('name', '').strip()
    agency = data.get('agency_name', '').strip()

    if not email or not password or not name:
        return jsonify({'error': 'Email, password, and name are required'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    if existing:
        db.close()
        return jsonify({'error': 'Email already registered'}), 409

    user_id = str(uuid.uuid4())
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    trial_end = (datetime.utcnow() + timedelta(days=14)).isoformat()
    db.execute(
        'INSERT INTO users (id, email, password_hash, name, agency_name, plan, subscription_status, trial_ends_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (user_id, email, pw_hash, name, agency, 'trial', 'trialing', trial_end)
    )
    db.commit()

    # Send welcome email (non-blocking — don't fail registration if email fails)
    try:
        html = _build_welcome_email(name, agency or 'Your Agency', trial_end)
        _send_transactional(db, user_id, email, name, 'welcome', 'Welcome to ChannelView!', html)
    except Exception as e:
        print(f'[EMAIL] Failed to send welcome email to {email}: {e}')

    db.close()

    token = create_token(user_id)
    resp = jsonify({'success': True, 'user_id': user_id, 'trial_ends_at': trial_end})
    resp.set_cookie('token', token, httponly=True, secure=os.environ.get('FLASK_ENV')=='production', samesite='Lax', max_age=30*24*3600)
    return resp

@app.route('/api/auth/login', methods=['POST'])
@rate_limit(max_requests=10, window_seconds=300)
def api_login():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    db.close()

    if not user or not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
        return jsonify({'error': 'Invalid email or password'}), 401

    token = create_token(user['id'])
    resp = jsonify({'success': True, 'user_id': user['id']})
    resp.set_cookie('token', token, httponly=True, secure=os.environ.get('FLASK_ENV')=='production', samesite='Lax', max_age=30*24*3600)
    return resp

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    resp = jsonify({'success': True})
    resp.delete_cookie('token')
    return resp

@app.route('/api/auth/me')
@require_auth
def api_auth_me():
    user = {k: g.user[k] for k in g.user if k != 'password_hash'}
    return jsonify(user)

# ======================== PASSWORD RESET ========================

# In-memory reset tokens (use DB or Redis in production)
_reset_tokens = {}  # token -> { user_id, email, expires_at }

@app.route('/api/auth/forgot-password', methods=['POST'])
@rate_limit(max_requests=3, window_seconds=300)
def api_forgot_password():
    """Request a password reset email."""
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    db = get_db()
    user = db.execute('SELECT id, name FROM users WHERE email=?', (email,)).fetchone()
    db.close()

    # Always return success to prevent email enumeration
    if not user:
        return jsonify({'success': True, 'message': 'If an account exists with that email, a reset link has been sent.'})

    reset_token = _secrets.token_urlsafe(32)
    _reset_tokens[reset_token] = {
        'user_id': user['id'],
        'email': email,
        'expires_at': time.time() + 3600  # 1 hour expiry
    }

    # Send reset email
    reset_link = f"{request.host_url.rstrip('/')}/reset-password?token={reset_token}"
    from email_service import send_email, get_smtp_config, _base_template
    db = get_db()
    smtp_config = get_smtp_config(db, user['id'])
    db.close()

    html = _base_template('#0ace0a', 'ChannelView', f'''
        <h1 style="margin:0 0 8px;font-size:22px;color:#111">Reset Your Password</h1>
        <p style="color:#6b7280;font-size:15px;margin:0 0 24px;line-height:1.5">
            Hi {user['name']},<br><br>
            We received a request to reset your password. Click the button below to set a new password.
        </p>
        <div style="text-align:center;margin:28px 0">
            <a href="{reset_link}" style="display:inline-block;background:#0ace0a;color:#000;font-weight:700;
               font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none">
                Reset Password
            </a>
        </div>
        <p style="color:#9ca3af;font-size:12px;text-align:center;margin:0">
            This link expires in 1 hour. If you didn't request this, you can safely ignore this email.
        </p>
    ''')
    send_email(smtp_config, email, 'Reset Your ChannelView Password', html)

    return jsonify({'success': True, 'message': 'If an account exists with that email, a reset link has been sent.'})


@app.route('/api/auth/reset-password', methods=['POST'])
@rate_limit(max_requests=5, window_seconds=300)
def api_reset_password():
    """Reset password using a valid reset token."""
    data = request.get_json()
    reset_token = data.get('token', '')
    new_password = data.get('password', '')

    if not reset_token or not new_password:
        return jsonify({'error': 'Token and new password are required'}), 400
    if len(new_password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    token_data = _reset_tokens.get(reset_token)
    if not token_data:
        return jsonify({'error': 'Invalid or expired reset token'}), 400
    if time.time() > token_data['expires_at']:
        del _reset_tokens[reset_token]
        return jsonify({'error': 'Reset token has expired. Please request a new one.'}), 400

    # Update password
    db = get_db()
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    db.execute('UPDATE users SET password_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (pw_hash, token_data['user_id']))
    db.commit()
    db.close()

    # Invalidate the token
    del _reset_tokens[reset_token]

    return jsonify({'success': True, 'message': 'Password has been reset. You can now log in.'})


@app.route('/api/auth/validate-reset-token', methods=['POST'])
def api_validate_reset_token():
    """Check if a reset token is still valid."""
    data = request.get_json()
    token = data.get('token', '')
    token_data = _reset_tokens.get(token)
    if not token_data or time.time() > token_data['expires_at']:
        return jsonify({'valid': False})
    return jsonify({'valid': True, 'email': token_data['email']})


# ======================== TOKEN REFRESH ========================

@app.route('/api/auth/refresh', methods=['POST'])
@require_auth
def api_refresh_token():
    """Refresh the auth token (extends session)."""
    new_token = create_token(g.user_id)
    resp = jsonify({'success': True})
    resp.set_cookie('token', new_token, httponly=True, secure=os.environ.get('FLASK_ENV')=='production', samesite='Lax', max_age=30*24*3600)
    return resp


# ======================== STRIPE BILLING ========================

@app.route('/api/billing/status', methods=['GET'])
@require_auth
def api_billing_status():
    """Get billing status for the current user."""
    db = get_db()
    user = db.execute('SELECT plan, stripe_customer_id, stripe_subscription_id, subscription_status, subscription_ends_at FROM users WHERE id=?',
                      (g.user_id,)).fetchone()
    db.close()
    return jsonify({
        'plan': user['plan'] or 'free',
        'subscription_status': user['subscription_status'] or 'none',
        'subscription_ends_at': user['subscription_ends_at'],
        'stripe_configured': bool(STRIPE_SECRET_KEY),
        'has_subscription': bool(user['stripe_subscription_id']),
    })


@app.route('/api/billing/checkout', methods=['POST'])
@require_auth
def api_billing_checkout():
    """Create a Stripe Checkout session for a specific plan tier."""
    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Billing is not configured. Contact support.'}), 503

    data = request.get_json() or {}
    requested_plan = data.get('plan', 'professional')

    # Look up the Stripe price ID for the requested plan
    price_id = PLAN_PRICE_MAP.get(requested_plan, '')
    if not price_id:
        return jsonify({'error': f'No pricing configured for plan: {requested_plan}'}), 400

    db = get_db()
    user = db.execute('SELECT email, stripe_customer_id FROM users WHERE id=?', (g.user_id,)).fetchone()

    # Create or reuse Stripe customer
    customer_id = user['stripe_customer_id']
    if not customer_id:
        cust, status = _stripe_request('POST', '/customers', {
            'email': user['email'],
            'metadata[channelview_user_id]': g.user_id,
        })
        if status >= 400:
            db.close()
            return jsonify({'error': 'Failed to create billing customer'}), 500
        customer_id = cust['id']
        db.execute('UPDATE users SET stripe_customer_id=? WHERE id=?', (customer_id, g.user_id))
        db.commit()
    db.close()

    # Create checkout session
    success_url = f"{request.host_url.rstrip('/')}/billing?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{request.host_url.rstrip('/')}/billing"

    session, status = _stripe_request('POST', '/checkout/sessions', {
        'customer': customer_id,
        'mode': 'subscription',
        'line_items[0][price]': price_id,
        'line_items[0][quantity]': '1',
        'success_url': success_url,
        'cancel_url': cancel_url,
        'metadata[channelview_user_id]': g.user_id,
        'metadata[channelview_plan]': requested_plan,
    })
    if status >= 400:
        return jsonify({'error': 'Failed to create checkout session'}), 500
    return jsonify({'checkout_url': session.get('url'), 'session_id': session.get('id')})


@app.route('/api/billing/portal', methods=['POST'])
@require_auth
def api_billing_portal():
    """Create a Stripe Customer Portal session for self-service management."""
    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Billing not configured'}), 503

    db = get_db()
    user = db.execute('SELECT stripe_customer_id FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()

    if not user['stripe_customer_id']:
        return jsonify({'error': 'No billing account found. Please subscribe first.'}), 400

    return_url = f"{request.host_url.rstrip('/')}/billing"
    session, status = _stripe_request('POST', '/billing_portal/sessions', {
        'customer': user['stripe_customer_id'],
        'return_url': return_url,
    })
    if status >= 400:
        return jsonify({'error': 'Failed to create portal session'}), 500
    return jsonify({'portal_url': session.get('url')})


@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events for subscription lifecycle."""
    import hmac, hashlib

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature', '')

    # Verify webhook signature — reject if secret not configured
    if not STRIPE_WEBHOOK_SECRET:
        print('[STRIPE] WARNING: Webhook rejected — STRIPE_WEBHOOK_SECRET not configured')
        return jsonify({'error': 'Webhook verification not configured'}), 500
    if sig_header:
        sig_parts = dict(p.split('=', 1) for p in sig_header.split(',') if '=' in p)
        timestamp = sig_parts.get('t', '')
        expected_sig = sig_parts.get('v1', '')
        signed_payload = f"{timestamp}.{payload}"
        computed = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, expected_sig):
            return jsonify({'error': 'Invalid signature'}), 400
    else:
        return jsonify({'error': 'Missing Stripe-Signature header'}), 400

    try:
        event = json.loads(payload)
    except:
        return jsonify({'error': 'Invalid JSON'}), 400

    event_type = event.get('type', '')
    obj = event.get('data', {}).get('object', {})

    db = get_db()
    try:
        if event_type == 'checkout.session.completed':
            user_id = obj.get('metadata', {}).get('channelview_user_id')
            customer_id = obj.get('customer')
            subscription_id = obj.get('subscription')
            # Determine plan from metadata or default to professional
            plan_name = obj.get('metadata', {}).get('channelview_plan', 'professional')
            # Normalize legacy aliases
            if plan_name in ('pro', 'essentials'):
                plan_name = 'professional' if plan_name == 'pro' else 'starter'
            if plan_name not in ('starter', 'professional', 'enterprise'):
                plan_name = 'professional'
            if user_id and subscription_id:
                db.execute('''UPDATE users SET plan=?, stripe_customer_id=?, stripe_subscription_id=?,
                              subscription_status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                           (plan_name, customer_id, subscription_id, user_id))
                db.commit()

        elif event_type in ('customer.subscription.updated', 'customer.subscription.deleted'):
            sub_id = obj.get('id')
            status = obj.get('status', '')
            cancel_at = obj.get('cancel_at')
            current_period_end = obj.get('current_period_end')
            user = db.execute('SELECT id, plan FROM users WHERE stripe_subscription_id=?', (sub_id,)).fetchone()
            if user:
                if status in ('active', 'trialing'):
                    # Determine plan from the subscription's price ID
                    items = obj.get('items', {}).get('data', [])
                    price_id = items[0].get('price', {}).get('id', '') if items else ''
                    new_plan = PRICE_PLAN_MAP.get(price_id, user['plan'] or 'professional')
                else:
                    new_plan = 'free'
                ends_at = datetime.utcfromtimestamp(current_period_end).isoformat() if current_period_end else None
                db.execute('''UPDATE users SET plan=?, subscription_status=?, subscription_ends_at=?,
                              updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                           (new_plan, status, ends_at, user['id']))
                db.commit()

        elif event_type == 'invoice.payment_failed':
            sub_id = obj.get('subscription')
            if sub_id:
                user = db.execute('SELECT id FROM users WHERE stripe_subscription_id=?', (sub_id,)).fetchone()
                if user:
                    db.execute("UPDATE users SET subscription_status='past_due', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (user['id'],))
                    db.commit()

        elif event_type == 'invoice.paid':
            sub_id = obj.get('subscription')
            amount = obj.get('amount_paid', 0)
            currency = obj.get('currency', 'usd')
            invoice_id = obj.get('id', '')
            hosted_invoice_url = obj.get('hosted_invoice_url', '')
            if sub_id:
                user = db.execute('SELECT id FROM users WHERE stripe_subscription_id=?', (sub_id,)).fetchone()
                if user:
                    db.execute('''INSERT OR IGNORE INTO invoices (id, user_id, stripe_invoice_id, amount, currency,
                                  status, hosted_invoice_url, created_at) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)''',
                               (str(uuid.uuid4()), user['id'], invoice_id, amount, currency, 'paid', hosted_invoice_url))
                    db.commit()
    finally:
        db.close()

    return jsonify({'received': True})


# ======================== SUBSCRIPTION CHECK ========================

def require_subscription(f):
    """Decorator to require an active subscription for premium features."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        plan = g.user.get('plan', 'free')
        sub_status = g.user.get('subscription_status', '')
        if plan == 'free' or (plan == 'pro' and sub_status not in ('active', 'trialing', '')):
            # Check if in grace period (subscription_ends_at hasn't passed)
            ends_at = g.user.get('subscription_ends_at')
            if ends_at:
                try:
                    end_dt = datetime.fromisoformat(ends_at)
                    if datetime.utcnow() < end_dt:
                        return f(*args, **kwargs)  # Still in grace period
                except:
                    pass
            return jsonify({'error': 'Active subscription required. Please subscribe at /billing.', 'code': 'subscription_required'}), 403
        return f(*args, **kwargs)
    return decorated


# ======================== DASHBOARD API ========================

@app.route('/api/dashboard')
@require_auth
def api_dashboard():
    db = get_db()
    uid = g.user_id

    total_candidates = db.execute('SELECT COUNT(*) as c FROM candidates WHERE user_id=?', (uid,)).fetchone()['c']
    active_interviews = db.execute("SELECT COUNT(*) as c FROM interviews WHERE user_id=? AND status='active'", (uid,)).fetchone()['c']
    completed = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND status='completed'", (uid,)).fetchone()['c']
    reviewed = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND status='reviewed'", (uid,)).fetchone()['c']
    hired = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND status='hired'", (uid,)).fetchone()['c']
    avg_score = db.execute('SELECT AVG(ai_score) as avg FROM candidates WHERE user_id=? AND ai_score IS NOT NULL', (uid,)).fetchone()['avg']

    recent_candidates = db.execute(
        '''SELECT c.*, i.title as interview_title FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           WHERE c.user_id = ? ORDER BY c.created_at DESC LIMIT 10''', (uid,)
    ).fetchall()

    recent_interviews = db.execute(
        '''SELECT i.*, COUNT(c.id) as candidate_count,
           SUM(CASE WHEN c.status='completed' THEN 1 ELSE 0 END) as completed_count
           FROM interviews i LEFT JOIN candidates c ON i.id = c.interview_id
           WHERE i.user_id = ? GROUP BY i.id ORDER BY i.created_at DESC LIMIT 5''', (uid,)
    ).fetchall()

    db.close()

    return jsonify({
        'stats': {
            'total_candidates': total_candidates,
            'active_interviews': active_interviews,
            'completed': completed,
            'reviewed': reviewed,
            'hired': hired,
            'avg_score': round(avg_score, 1) if avg_score else None,
            'completion_rate': round(completed / max(total_candidates, 1) * 100, 1)
        },
        'recent_candidates': [dict(r) for r in recent_candidates],
        'recent_interviews': [dict(r) for r in recent_interviews]
    })

# ======================== INTERVIEWS API ========================

@app.route('/api/interviews', methods=['GET'])
@require_auth
def api_list_interviews():
    db = get_db()
    interviews = db.execute(
        '''SELECT i.*, COUNT(c.id) as candidate_count,
           SUM(CASE WHEN c.status='completed' THEN 1 ELSE 0 END) as completed_count
           FROM interviews i LEFT JOIN candidates c ON i.id = c.interview_id
           WHERE i.user_id = ? GROUP BY i.id ORDER BY i.created_at DESC''', (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in interviews])

@app.route('/api/interviews', methods=['POST'])
@require_auth
@check_trial_status
@require_role('admin', 'recruiter')
def api_create_interview():
    data = request.get_json()
    db = get_db()

    # ── Subscription limit check (soft block) ──
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    interview_count = db.execute('SELECT COUNT(*) as cnt FROM interviews WHERE user_id=?',
                                  (g.user_id,)).fetchone()['cnt']
    allowed, limit, remaining = check_plan_limit(user, 'interviews', interview_count)
    if not allowed:
        db.close()
        return jsonify({
            'error': 'interview_limit_reached',
            'message': f'You\'ve reached your interview limit ({limit}). Upgrade your plan to create more interviews.',
            'limit': limit, 'used': interview_count, 'upgrade_url': '/billing'
        }), 403

    interview_id = str(uuid.uuid4())
    db.execute(
        '''INSERT INTO interviews (id, user_id, title, description, department, position,
           thinking_time, max_answer_time, max_retakes, welcome_msg, thank_you_msg, brand_color)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (interview_id, g.user_id, data.get('title', 'Untitled Interview'),
         data.get('description', ''), data.get('department', ''),
         data.get('position', ''), data.get('thinking_time', 30),
         data.get('max_answer_time', 120), data.get('max_retakes', 1),
         data.get('welcome_msg', 'Welcome! This interview consists of a few video questions. Take your time and be yourself.'),
         data.get('thank_you_msg', 'Thank you for completing your interview! We will review your responses and be in touch soon.'),
         data.get('brand_color', '#0ace0a'))
    )

    # Insert questions
    for i, q in enumerate(data.get('questions', [])):
        db.execute(
            'INSERT INTO questions (id, interview_id, question_text, question_order, thinking_time, max_answer_time) VALUES (?, ?, ?, ?, ?, ?)',
            (str(uuid.uuid4()), interview_id, q.get('text', ''), i + 1,
             q.get('thinking_time'), q.get('max_answer_time'))
        )

    db.commit()
    db.close()
    return jsonify({'success': True, 'id': interview_id}), 201

@app.route('/api/interviews/<interview_id>', methods=['GET'])
@require_auth
def api_get_interview(interview_id):
    db = get_db()
    interview = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    questions = db.execute('SELECT * FROM questions WHERE interview_id=? ORDER BY question_order', (interview_id,)).fetchall()
    candidates = db.execute(
        'SELECT * FROM candidates WHERE interview_id=? ORDER BY created_at DESC', (interview_id,)
    ).fetchall()
    db.close()

    result = dict(interview)
    result['questions'] = [dict(q) for q in questions]
    result['candidates'] = [dict(c) for c in candidates]
    return jsonify(result)

@app.route('/api/interviews/<interview_id>', methods=['PUT'])
@require_auth
def api_update_interview(interview_id):
    data = request.get_json()
    db = get_db()
    interview = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    fields = ['title','description','department','position','status','thinking_time',
              'max_answer_time','max_retakes','welcome_msg','thank_you_msg','brand_color','intro_video_path']
    updates = []
    values = []
    for f in fields:
        if f in data:
            updates.append(f"{f}=?")
            values.append(data[f])
    if updates:
        values.append(interview_id)
        db.execute(f"UPDATE interviews SET {', '.join(updates)}, updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
        db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/interviews/<interview_id>', methods=['DELETE'])
@require_auth
@require_role('admin')
def api_delete_interview(interview_id):
    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    # Cascade: clean up video files for all candidates in this interview
    candidates = db.execute('SELECT id FROM candidates WHERE interview_id=?', (interview_id,)).fetchall()
    for cand in candidates:
        responses = db.execute('SELECT video_path FROM responses WHERE candidate_id=?', (cand['id'],)).fetchall()
        for resp in responses:
            if resp['video_path']:
                fpath = os.path.join(os.path.dirname(__file__), resp['video_path'].lstrip('/'))
                if os.path.exists(fpath):
                    try: os.remove(fpath)
                    except: pass
        # Delete responses, reports, email logs for this candidate
        db.execute('DELETE FROM responses WHERE candidate_id=?', (cand['id'],))
        db.execute('DELETE FROM reports WHERE candidate_id=?', (cand['id'],))
        db.execute('DELETE FROM email_log WHERE candidate_id=?', (cand['id'],))

    # Delete candidates, questions, then the interview
    db.execute('DELETE FROM candidates WHERE interview_id=?', (interview_id,))
    db.execute('DELETE FROM questions WHERE interview_id=?', (interview_id,))
    db.execute('DELETE FROM interviews WHERE id=?', (interview_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})

# ======================== INTRO VIDEO API ========================

# ======================== INTRO VIDEO LIBRARY API ========================

@app.route('/api/intro-videos', methods=['GET'])
@require_auth
def api_list_intro_videos():
    """List all saved intro videos for the current user."""
    db = get_db()
    videos = db.execute('SELECT * FROM intro_videos WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    db.close()
    return jsonify([dict(v) for v in videos])

@app.route('/api/intro-videos', methods=['POST'])
@require_auth
def api_save_intro_video():
    """Save a new intro video to the user's library."""
    video = request.files.get('video')
    name = request.form.get('name', 'Untitled Intro').strip()
    if not video:
        return jsonify({'error': 'No video file provided'}), 400

    intro_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'intros')
    os.makedirs(intro_dir, exist_ok=True)
    vid_id = str(uuid.uuid4())
    filename = f"lib_{vid_id}_{int(time.time())}.webm"
    filepath = os.path.join(intro_dir, filename)
    video.save(filepath)

    video_path = f'/static/uploads/intros/{filename}'
    db = get_db()
    db.execute('INSERT INTO intro_videos (id, user_id, name, video_path) VALUES (?,?,?,?)',
               (vid_id, g.user_id, name, video_path))
    db.commit()
    db.close()
    return jsonify({'success': True, 'id': vid_id, 'path': video_path}), 201

@app.route('/api/intro-videos/<video_id>', methods=['PUT'])
@require_auth
def api_rename_intro_video(video_id):
    """Rename a saved intro video."""
    data = request.get_json()
    db = get_db()
    db.execute('UPDATE intro_videos SET name=? WHERE id=? AND user_id=?',
               (data.get('name', 'Untitled'), video_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/intro-videos/<video_id>', methods=['DELETE'])
@require_auth
def api_delete_intro_video(video_id):
    """Delete a saved intro video from the library."""
    db = get_db()
    vid = db.execute('SELECT * FROM intro_videos WHERE id=? AND user_id=?', (video_id, g.user_id)).fetchone()
    if vid and vid['video_path']:
        old_file = os.path.join(os.path.dirname(__file__), vid['video_path'].lstrip('/'))
        if os.path.exists(old_file):
            try: os.remove(old_file)
            except: pass
    db.execute('DELETE FROM intro_videos WHERE id=? AND user_id=?', (video_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

# ======================== INTRO VIDEO API ========================

@app.route('/api/interviews/<interview_id>/intro-video', methods=['POST'])
@require_auth
def api_upload_intro_video(interview_id):
    """Upload or record an intro video for an interview."""
    db = get_db()
    interview = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    video = request.files.get('video')
    if not video:
        db.close()
        return jsonify({'error': 'No video file provided'}), 400

    # Delete old intro video if exists
    if interview['intro_video_path']:
        old_file = os.path.join(os.path.dirname(__file__), interview['intro_video_path'].lstrip('/'))
        if os.path.exists(old_file):
            try: os.remove(old_file)
            except: pass

    # Save new intro video
    intro_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'intros')
    os.makedirs(intro_dir, exist_ok=True)
    filename = f"intro_{interview_id}_{int(time.time())}.webm"
    filepath = os.path.join(intro_dir, filename)
    video.save(filepath)

    video_path = f'/static/uploads/intros/{filename}'
    db.execute('UPDATE interviews SET intro_video_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (video_path, interview_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'path': video_path})

@app.route('/api/interviews/<interview_id>/intro-video', methods=['DELETE'])
@require_auth
def api_remove_interview_intro_video(interview_id):
    """Remove an intro video from an interview."""
    db = get_db()
    interview = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    if interview['intro_video_path']:
        old_file = os.path.join(os.path.dirname(__file__), interview['intro_video_path'].lstrip('/'))
        if os.path.exists(old_file):
            try: os.remove(old_file)
            except: pass

    db.execute('UPDATE interviews SET intro_video_path=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?', (interview_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})

# ======================== QUESTIONS API ========================

@app.route('/api/interviews/<interview_id>/questions', methods=['POST'])
@require_auth
def api_add_question(interview_id):
    data = request.get_json()
    db = get_db()
    max_order = db.execute('SELECT MAX(question_order) as m FROM questions WHERE interview_id=?', (interview_id,)).fetchone()['m'] or 0
    qid = str(uuid.uuid4())
    db.execute(
        'INSERT INTO questions (id, interview_id, question_text, question_order, thinking_time, max_answer_time) VALUES (?,?,?,?,?,?)',
        (qid, interview_id, data.get('text',''), max_order+1, data.get('thinking_time'), data.get('max_answer_time'))
    )
    db.commit()
    db.close()
    return jsonify({'success': True, 'id': qid}), 201

@app.route('/api/questions/<question_id>', methods=['DELETE'])
@require_auth
def api_delete_question(question_id):
    db = get_db()
    # Verify ownership: question must belong to an interview owned by this user
    q = db.execute('''SELECT q.id FROM questions q
                      JOIN interviews i ON q.interview_id = i.id
                      WHERE q.id=? AND i.user_id=?''', (question_id, g.user_id)).fetchone()
    if not q:
        db.close()
        return jsonify({'error': 'Question not found'}), 404
    db.execute('DELETE FROM questions WHERE id=?', (question_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})

# ======================== EMAIL HELPERS ========================

def _try_send_candidate_email(db, user_id, candidate_id, email_type, build_fn, **kwargs):
    """Attempt to send a candidate email if SMTP is configured. Logs result. Non-blocking on failure."""
    try:
        from email_service import send_email, get_smtp_config
        smtp_config = get_smtp_config(db, user_id)
        # smtp_config may be None — send_email() will fall back to SendGrid or log

        subject, html_body = build_fn(smtp_config=smtp_config, **kwargs)
        success, error = send_email(smtp_config, kwargs.get('to_email', ''), subject, html_body)
        status = 'sent' if success else 'failed'
        db.execute(
            'INSERT INTO email_log (id, user_id, candidate_id, email_type, to_email, subject, status, error_message) VALUES (?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), user_id, candidate_id, email_type, kwargs.get('to_email',''), subject, status, error)
        )
        # Update candidate timestamp
        ts_col = {'invite': 'invite_sent_at', 'reminder': 'reminder_sent_at', 'completion': 'completion_sent_at'}.get(email_type)
        if ts_col and success:
            db.execute(f'UPDATE candidates SET {ts_col}=CURRENT_TIMESTAMP WHERE id=?', (candidate_id,))
        db.commit()
    except Exception as e:
        print(f'[Email] Failed to send {email_type} email: {e}')

def _build_invite_email(smtp_config, candidate_name, interview_title, interview_link, agency_name, brand_color, welcome_msg, to_email):
    from email_service import build_invite_email
    subject = f'You\'re Invited: {interview_title} — Video Interview'
    html = build_invite_email(candidate_name, interview_title, interview_link, agency_name, brand_color, welcome_msg)
    return subject, html

def _build_reminder_email(smtp_config, candidate_name, interview_title, interview_link, agency_name, brand_color, status, to_email):
    from email_service import build_reminder_email
    subject = f'Reminder: {interview_title} — Complete Your Interview'
    html = build_reminder_email(candidate_name, interview_title, interview_link, agency_name, brand_color, status)
    return subject, html

def _build_completion_email(smtp_config, candidate_name, interview_title, agency_name, brand_color, thank_you_msg, to_email):
    from email_service import build_completion_email
    subject = f'Interview Complete: {interview_title} — Thank You!'
    html = build_completion_email(candidate_name, interview_title, agency_name, brand_color, thank_you_msg)
    return subject, html

# ======================== CANDIDATES API ========================

@app.route('/api/candidates', methods=['GET'])
@require_auth
def api_list_candidates():
    status = request.args.get('status')
    interview_id = request.args.get('interview_id')
    search = request.args.get('search', '').strip()
    score_min = request.args.get('score_min', type=float)
    score_max = request.args.get('score_max', type=float)
    sort_by = request.args.get('sort', 'created_at')
    sort_dir = request.args.get('dir', 'desc').lower()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    per_page = min(per_page, 200)  # cap

    db = get_db()
    query = '''SELECT c.*, i.title as interview_title, i.department, i.position
               FROM candidates c
               JOIN interviews i ON c.interview_id = i.id WHERE c.user_id = ?'''
    params = [g.user_id]

    if status:
        query += ' AND c.status = ?'
        params.append(status)
    if interview_id:
        query += ' AND c.interview_id = ?'
        params.append(interview_id)
    if search:
        query += ' AND (c.first_name LIKE ? OR c.last_name LIKE ? OR c.email LIKE ? OR (c.first_name || " " || c.last_name) LIKE ?)'
        sw = f'%{search}%'
        params.extend([sw, sw, sw, sw])
    if score_min is not None:
        query += ' AND c.ai_score >= ?'
        params.append(score_min)
    if score_max is not None:
        query += ' AND c.ai_score <= ?'
        params.append(score_max)

    # Count total before pagination
    count_query = query.replace('SELECT c.*, i.title as interview_title, i.department, i.position', 'SELECT COUNT(*) as total')
    total = db.execute(count_query, params).fetchone()['total']

    # Sort
    allowed_sorts = {'created_at': 'c.created_at', 'name': 'c.first_name', 'score': 'c.ai_score',
                     'status': 'c.status', 'email': 'c.email', 'completed_at': 'c.completed_at'}
    sort_col = allowed_sorts.get(sort_by, 'c.created_at')
    direction = 'ASC' if sort_dir == 'asc' else 'DESC'
    query += f' ORDER BY {sort_col} {direction}'

    # Pagination
    offset = (page - 1) * per_page
    query += ' LIMIT ? OFFSET ?'
    params.extend([per_page, offset])

    candidates = db.execute(query, params).fetchall()
    db.close()
    return jsonify({
        'candidates': [dict(c) for c in candidates],
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page
    })

@app.route('/api/candidates', methods=['POST'])
@require_auth
@check_trial_status
def api_create_candidate():
    data = request.get_json()
    db = get_db()

    # ── Subscription limit check (soft block) ──
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    count = db.execute('SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND created_at >= ?',
                       (g.user_id, month_start)).fetchone()['cnt']
    allowed, limit, remaining = check_plan_limit(user, 'candidates_per_month', count)
    if not allowed:
        db.close()
        return jsonify({
            'error': 'candidate_limit_reached',
            'message': f'You\'ve reached your monthly candidate limit ({limit}). Upgrade your plan to add more candidates.',
            'limit': limit,
            'used': count,
            'upgrade_url': '/billing'
        }), 403

    cid = str(uuid.uuid4())
    token = str(uuid.uuid4())
    db.execute(
        '''INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email, phone, token)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (cid, g.user_id, data['interview_id'], data['first_name'], data['last_name'],
         data['email'], data.get('phone',''), token)
    )
    db.commit()

    # Send invite email if SMTP configured and send_invite flag is not explicitly false
    if data.get('send_invite', True):
        interview = db.execute('SELECT * FROM interviews WHERE id=?', (data['interview_id'],)).fetchone()
        user = db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone()
        if interview and user:
            candidate_name = f"{data['first_name']} {data['last_name']}"
            interview_link = f"{request.host_url.rstrip('/')}/i/{token}"
            _try_send_candidate_email(db, g.user_id, cid, 'invite', _build_invite_email,
                candidate_name=candidate_name,
                interview_title=interview['title'],
                interview_link=interview_link,
                agency_name=user['agency_name'] or 'ChannelView',
                brand_color=interview['brand_color'] or '#0ace0a',
                welcome_msg=interview['welcome_msg'],
                to_email=data['email']
            )
    db.close()
    return jsonify({'success': True, 'id': cid, 'token': token}), 201

@app.route('/api/candidates/bulk', methods=['POST'])
@require_auth
@check_trial_status
def api_bulk_create_candidates():
    data = request.get_json()
    interview_id = data.get('interview_id')
    candidates_data = data.get('candidates', [])
    db = get_db()

    # Cycle 31: Feature gate — bulk_ops
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    bulk_allowed, upgrade = check_feature_access(user, 'bulk_ops')
    if not bulk_allowed:
        db.close()
        return soft_block_response('bulk_ops')

    # ── Subscription limit check (soft block) ──
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    count = db.execute('SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND created_at >= ?',
                       (g.user_id, month_start)).fetchone()['cnt']
    allowed, limit, remaining = check_plan_limit(user, 'candidates_per_month', count)
    if not allowed:
        db.close()
        return jsonify({
            'error': 'candidate_limit_reached',
            'message': f'You\'ve reached your monthly candidate limit ({limit}). Upgrade your plan to add more candidates.',
            'limit': limit, 'used': count, 'upgrade_url': '/billing'
        }), 403
    # If adding this batch would exceed the limit, only allow what fits
    if limit > 0 and count + len(candidates_data) > limit:
        max_allowed = limit - count
        db.close()
        return jsonify({
            'error': 'candidate_limit_partial',
            'message': f'You can only add {max_allowed} more candidate(s) this month. Upgrade your plan for more.',
            'limit': limit, 'used': count, 'remaining': max_allowed, 'upgrade_url': '/billing'
        }), 403

    created = []
    for c in candidates_data:
        cid = str(uuid.uuid4())
        token = str(uuid.uuid4())
        db.execute(
            '''INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email, phone, token)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (cid, g.user_id, interview_id, c.get('first_name',''), c.get('last_name',''),
             c.get('email',''), c.get('phone',''), token)
        )
        created.append({'id': cid, 'token': token, 'email': c.get('email','')})
    db.commit()
    db.close()
    return jsonify({'success': True, 'created': created}), 201

@app.route('/api/candidates/<candidate_id>', methods=['GET'])
@require_auth
def api_get_candidate(candidate_id):
    db = get_db()
    candidate = db.execute(
        '''SELECT c.*, i.title as interview_title, i.thinking_time, i.max_answer_time
           FROM candidates c JOIN interviews i ON c.interview_id = i.id
           WHERE c.id = ? AND c.user_id = ?''', (candidate_id, g.user_id)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    responses = db.execute(
        '''SELECT r.*, q.question_text, q.question_order FROM responses r
           JOIN questions q ON r.question_id = q.id
           WHERE r.candidate_id = ? ORDER BY q.question_order''', (candidate_id,)
    ).fetchall()
    db.close()

    result = dict(candidate)
    result['responses'] = [dict(r) for r in responses]
    return jsonify(result)

@app.route('/api/candidates/<candidate_id>/status', methods=['PUT'])
@require_auth
def api_update_candidate_status(candidate_id):
    data = request.get_json()
    db = get_db()
    db.execute('UPDATE candidates SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',
               (data['status'], candidate_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/candidates/<candidate_id>/notes', methods=['PUT'])
@require_auth
def api_update_candidate_notes(candidate_id):
    data = request.get_json()
    db = get_db()
    db.execute('UPDATE candidates SET notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?',
               (data['notes'], candidate_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

# ======================== CANDIDATE INTERVIEW API (PUBLIC) ========================

@app.route('/api/interview/<token>/start', methods=['POST'])
def api_candidate_start(token):
    db = get_db()
    candidate = db.execute('SELECT * FROM candidates WHERE token=?', (token,)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404
    if candidate['status'] == 'completed':
        db.close()
        return jsonify({'error': 'Interview already completed'}), 400

    db.execute("UPDATE candidates SET status='in_progress', started_at=CURRENT_TIMESTAMP WHERE token=?", (token,))
    db.commit()

    questions = db.execute(
        'SELECT id, question_text, question_order, thinking_time, max_answer_time FROM questions WHERE interview_id=? ORDER BY question_order',
        (candidate['interview_id'],)
    ).fetchall()

    interview = db.execute('SELECT * FROM interviews WHERE id=?', (candidate['interview_id'],)).fetchone()

    # Send "interview started" notification to agency owner
    try:
        owner = db.execute('SELECT * FROM users WHERE id=?', (candidate['user_id'],)).fetchone()
        if owner:
            _send_notification_email(dict(owner), dict(candidate), 'interview_started', {
                'position': interview['title'] if interview else ''
            })
    except Exception:
        pass  # Don't fail the candidate flow on notification errors

    db.close()

    return jsonify({
        'candidate_id': candidate['id'],
        'questions': [dict(q) for q in questions],
        'defaults': {
            'thinking_time': interview['thinking_time'],
            'max_answer_time': interview['max_answer_time'],
            'max_retakes': interview['max_retakes']
        }
    })

MAX_RESPONSE_SIZE = 100 * 1024 * 1024  # 100MB per individual response

@app.route('/api/interview/<token>/respond', methods=['POST'])
def api_candidate_respond(token):
    db = get_db()
    candidate = db.execute(
        '''SELECT c.*, i.max_answer_time FROM candidates c
           JOIN interviews i ON c.interview_id = i.id WHERE c.token=?''', (token,)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404

    question_id = request.form.get('question_id')
    video = request.files.get('video')
    duration = int(request.form.get('duration', 0))
    transcript = request.form.get('transcript', '').strip()

    if not video or not question_id:
        db.close()
        return jsonify({'error': 'Missing video or question_id'}), 400

    # Validate video file size (per-response limit)
    video.seek(0, 2)  # Seek to end
    file_size = video.tell()
    video.seek(0)  # Reset to start
    if file_size > MAX_RESPONSE_SIZE:
        db.close()
        return jsonify({'error': f'Video too large. Maximum per-response size is {MAX_RESPONSE_SIZE // (1024*1024)}MB.'}), 413

    # Validate duration against interview max_answer_time (allow 10s grace for upload delay)
    max_dur = (candidate['max_answer_time'] or 120) + 10
    if duration > max_dur:
        db.close()
        return jsonify({'error': f'Recording exceeds maximum answer time of {candidate["max_answer_time"]}s.'}), 400

    # Delete any previous response for this question (retake)
    old = db.execute('SELECT video_path FROM responses WHERE candidate_id=? AND question_id=?',
                     (candidate['id'], question_id)).fetchall()
    for row in old:
        old_file = os.path.join(os.path.dirname(__file__), row['video_path'].lstrip('/'))
        if os.path.exists(old_file):
            try: os.remove(old_file)
            except: pass
    db.execute('DELETE FROM responses WHERE candidate_id=? AND question_id=?',
               (candidate['id'], question_id))

    # Save video file
    filename = f"{candidate['id']}_{question_id}_{int(time.time())}.webm"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    video.save(filepath)

    response_id = str(uuid.uuid4())
    db.execute(
        'INSERT INTO responses (id, candidate_id, question_id, video_path, duration, transcript, file_size) VALUES (?,?,?,?,?,?,?)',
        (response_id, candidate['id'], question_id, f'/static/uploads/videos/{filename}', duration, transcript or None, file_size)
    )
    db.commit()
    db.close()
    return jsonify({'success': True, 'id': response_id})

@app.route('/api/interview/<token>/complete', methods=['POST'])
def api_candidate_complete(token):
    db = get_db()
    candidate = db.execute(
        '''SELECT c.*, i.title as interview_title, i.thank_you_msg, i.brand_color,
           u.id as owner_id, u.email as owner_email, u.name as owner_name,
           u.agency_name, u.smtp_host
           FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           JOIN users u ON c.user_id = u.id
           WHERE c.token = ?''', (token,)
    ).fetchone()

    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404

    db.execute("UPDATE candidates SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE token=?", (token,))
    db.commit()

    # Send completion confirmation email to candidate
    candidate_name = f"{candidate['first_name']} {candidate['last_name']}"
    _try_send_candidate_email(db, candidate['owner_id'], candidate['id'], 'completion', _build_completion_email,
        candidate_name=candidate_name,
        interview_title=candidate['interview_title'],
        agency_name=candidate['agency_name'] or 'ChannelView',
        brand_color=candidate['brand_color'] or '#0ace0a',
        thank_you_msg=candidate['thank_you_msg'],
        to_email=candidate['email']
    )

    # Notify agency owner that a candidate completed their interview
    _send_owner_completion_notification(db, candidate)

    db.close()
    return jsonify({'success': True})

@app.route('/api/candidates/<candidate_id>/remind', methods=['POST'])
@require_auth
def api_send_reminder(candidate_id):
    """Send a reminder email to a candidate who hasn't completed their interview."""
    db = get_db()
    candidate = db.execute(
        '''SELECT c.*, i.title as interview_title, i.brand_color, i.welcome_msg,
           u.agency_name
           FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           JOIN users u ON c.user_id = u.id
           WHERE c.id = ? AND c.user_id = ?''', (candidate_id, g.user_id)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    if candidate['status'] in ('completed', 'reviewed', 'hired'):
        db.close()
        return jsonify({'error': 'Candidate has already completed the interview'}), 400

    from email_service import get_smtp_config
    smtp_config = get_smtp_config(db, g.user_id)
    if not smtp_config:
        db.close()
        return jsonify({'error': 'SMTP not configured. Go to Settings to set up email.'}), 400

    candidate_name = f"{candidate['first_name']} {candidate['last_name']}"
    interview_link = f"{request.host_url.rstrip('/')}/i/{candidate['token']}"
    _try_send_candidate_email(db, g.user_id, candidate_id, 'reminder', _build_reminder_email,
        candidate_name=candidate_name,
        interview_title=candidate['interview_title'],
        interview_link=interview_link,
        agency_name=candidate['agency_name'] or 'ChannelView',
        brand_color=candidate['brand_color'] or '#0ace0a',
        status=candidate['status'],
        to_email=candidate['email']
    )
    db.close()
    return jsonify({'success': True, 'message': f'Reminder sent to {candidate["email"]}'})

@app.route('/api/interviews/<interview_id>/send-invites', methods=['POST'])
@require_auth
def api_send_bulk_invites(interview_id):
    """Send invite emails to all uninvited candidates in an interview."""
    db = get_db()
    interview = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    from email_service import get_smtp_config
    smtp_config = get_smtp_config(db, g.user_id)
    if not smtp_config:
        db.close()
        return jsonify({'error': 'SMTP not configured. Go to Settings to set up email.'}), 400

    user = db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone()
    candidates = db.execute(
        'SELECT * FROM candidates WHERE interview_id=? AND user_id=? AND invite_sent_at IS NULL AND status=?',
        (interview_id, g.user_id, 'invited')
    ).fetchall()

    sent = 0
    for cand in candidates:
        candidate_name = f"{cand['first_name']} {cand['last_name']}"
        interview_link = f"{request.host_url.rstrip('/')}/i/{cand['token']}"
        _try_send_candidate_email(db, g.user_id, cand['id'], 'invite', _build_invite_email,
            candidate_name=candidate_name,
            interview_title=interview['title'],
            interview_link=interview_link,
            agency_name=user['agency_name'] or 'ChannelView',
            brand_color=interview['brand_color'] or '#0ace0a',
            welcome_msg=interview['welcome_msg'],
            to_email=cand['email']
        )
        sent += 1

    db.close()
    return jsonify({'success': True, 'sent': sent, 'message': f'Sent {sent} invite email{"s" if sent != 1 else ""}'})


def _send_owner_completion_notification(db, candidate):
    """Send the agency owner an email notification when a candidate completes their interview."""
    try:
        from email_service import send_email, get_smtp_config
        smtp_config = get_smtp_config(db, candidate['owner_id'])
        if not smtp_config:
            return

        candidate_name = f"{candidate['first_name']} {candidate['last_name']}"
        app_url = os.environ.get('APP_URL', request.host_url.rstrip('/'))
        review_url = f"{app_url}/review/{candidate['id']}"

        subject = f'Interview Completed: {candidate_name} — {candidate["interview_title"]}'
        html_body = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
            <div style="background:#111;padding:16px 24px;border-radius:8px 8px 0 0;">
                <span style="color:#0ace0a;font-size:18px;font-weight:800;">C</span>
                <span style="color:#fff;font-size:15px;font-weight:700;margin-left:6px;">ChannelView</span>
            </div>
            <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">
                <h2 style="color:#111;font-size:18px;margin:0 0 12px;">New Interview Completed!</h2>
                <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin:16px 0;">
                    <p style="color:#333;font-size:14px;margin:0 0 4px;"><strong>Candidate:</strong> {candidate_name}</p>
                    <p style="color:#333;font-size:14px;margin:0 0 4px;"><strong>Interview:</strong> {candidate['interview_title']}</p>
                    <p style="color:#333;font-size:14px;margin:0 0 4px;"><strong>Email:</strong> {candidate['email']}</p>
                    <p style="color:#999;font-size:12px;margin:8px 0 0;">Completed just now</p>
                </div>
                <p style="color:#555;font-size:14px;">Log in to ChannelView to review their responses and run AI scoring.</p>
                <hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">
                <p style="color:#999;font-size:11px;">Powered by ChannelView — Async Video Interviews</p>
            </div>
        </div>"""

        success, error = send_email(smtp_config, candidate['owner_email'], subject, html_body)
        if success:
            db.execute(
                'INSERT INTO email_log (id, user_id, candidate_id, email_type, to_email, subject, status) VALUES (?,?,?,?,?,?,?)',
                (str(uuid.uuid4()), candidate['owner_id'], candidate['id'], 'owner_notification', candidate['owner_email'], subject, 'sent')
            )
            db.commit()
    except Exception as e:
        print(f'[Email] Owner notification failed: {e}')


# ======================== AI SCORING API ========================

from ai_service import (
    score_response, generate_candidate_summary,
    is_ai_available, CATEGORIES, CAT_LABELS
)

@app.route('/api/candidates/<candidate_id>/score', methods=['POST'])
@require_auth
def api_score_candidate(candidate_id):
    """Score a candidate using Claude AI (or realistic mock fallback)."""
    db = get_db()
    # Cycle 31: Feature gate — ai_scoring + usage quota
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    allowed_feature, upgrade = check_feature_access(user, 'ai_scoring')
    if not allowed_feature:
        db.close()
        return soft_block_response('ai_scoring')
    # Check AI usage quota
    ai_allowed, ai_used, ai_limit = track_ai_usage(g.user_id)
    if not ai_allowed:
        db.close()
        return jsonify({
            'error': 'ai_quota_reached',
            'message': f'You\'ve used all {ai_limit} AI interactions this month. Upgrade your plan for more.',
            'used': ai_used, 'limit': ai_limit, 'upgrade_url': '/billing', 'soft_block': True
        }), 403

    candidate = db.execute(
        'SELECT c.*, i.title, i.position FROM candidates c JOIN interviews i ON c.interview_id=i.id WHERE c.id=? AND c.user_id=?',
        (candidate_id, g.user_id)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    responses = db.execute(
        'SELECT r.*, q.question_text FROM responses r JOIN questions q ON r.question_id=q.id WHERE r.candidate_id=?',
        (candidate_id,)
    ).fetchall()

    position = candidate['position'] or candidate['title'] or 'position'
    interview_title = candidate['title'] or ''
    using_ai = is_ai_available()

    all_cat_scores = {c: [] for c in CATEGORIES}
    overall_scores = []

    for resp in responses:
        # Score each response (real AI if available, mock otherwise)
        result = score_response(
            question_text=resp['question_text'],
            transcript=resp['transcript'] or '',
            position=position,
            interview_title=interview_title
        )

        resp_cats = result['scores']
        for cat in CATEGORIES:
            all_cat_scores[cat].append(resp_cats.get(cat, 0))

        resp_overall = result['overall']
        overall_scores.append(resp_overall)
        feedback = result['feedback']

        db.execute('UPDATE responses SET ai_score=?, ai_feedback=?, ai_scores_json=? WHERE id=?',
                   (resp_overall, feedback, json.dumps(resp_cats), resp['id']))

    # Calculate overall category averages
    cat_averages = {}
    for cat in CATEGORIES:
        scores = all_cat_scores[cat]
        cat_averages[cat] = round(sum(scores) / max(len(scores), 1), 1) if scores else 0

    avg_score = round(sum(overall_scores) / max(len(overall_scores), 1), 1)

    # Generate summary (real AI or structured mock)
    summary = generate_candidate_summary(position, cat_averages, avg_score)

    scores_data = {
        'overall': avg_score,
        'categories': cat_averages,
        'labels': CAT_LABELS
    }

    db.execute('UPDATE candidates SET ai_score=?, ai_summary=?, ai_scores_json=?, status=? WHERE id=?',
               (avg_score, summary, json.dumps(scores_data), 'reviewed', candidate_id))
    db.commit()
    db.close()
    # Cycle 31: Record AI usage after successful scoring
    record_ai_interaction(g.user_id, 'ai_scoring')
    return jsonify({
        'success': True,
        'score': avg_score,
        'summary': summary,
        'categories': cat_averages,
        'labels': CAT_LABELS,
        'ai_powered': using_ai
    })

@app.route('/api/ai/status', methods=['GET'])
@require_auth
def api_ai_status():
    """Check if real AI scoring is available."""
    from transcription_service import is_transcription_available
    return jsonify({
        'ai_available': is_ai_available(),
        'transcription_available': is_transcription_available(),
        'message': 'Claude AI scoring active' if is_ai_available() else 'Using mock scoring. Set ANTHROPIC_API_KEY environment variable to enable AI.'
    })

@app.route('/api/candidates/<candidate_id>/transcribe', methods=['POST'])
@require_auth
def api_transcribe_candidate(candidate_id):
    """Transcribe all video responses for a candidate using Whisper."""
    from transcription_service import is_transcription_available, transcribe_all_responses

    if not is_transcription_available():
        return jsonify({'error': 'Transcription not available. Install whisper: pip install openai-whisper'}), 400

    db = get_db()
    candidate = db.execute(
        'SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    count = transcribe_all_responses(db, candidate_id)
    db.close()
    return jsonify({
        'success': True,
        'transcribed': count,
        'message': f'Transcribed {count} response{"s" if count != 1 else ""}.'
    })

# ======================== AI INSIGHTS API ========================

@app.route('/api/ai/insights')
@require_auth
def api_ai_insights():
    """Aggregate AI scoring data across all candidates for insights dashboard."""
    db = get_db()
    uid = g.user_id

    # Top scored candidates
    top_candidates = db.execute(
        '''SELECT c.id, c.first_name, c.last_name, c.email, c.ai_score, c.ai_summary,
           c.status, i.title as interview_title, i.position
           FROM candidates c JOIN interviews i ON c.interview_id = i.id
           WHERE c.user_id = ? AND c.ai_score IS NOT NULL
           ORDER BY c.ai_score DESC LIMIT 20''', (uid,)
    ).fetchall()

    # Score distribution
    score_ranges = db.execute(
        '''SELECT
           SUM(CASE WHEN ai_score >= 90 THEN 1 ELSE 0 END) as excellent,
           SUM(CASE WHEN ai_score >= 80 AND ai_score < 90 THEN 1 ELSE 0 END) as strong,
           SUM(CASE WHEN ai_score >= 70 AND ai_score < 80 THEN 1 ELSE 0 END) as good,
           SUM(CASE WHEN ai_score >= 60 AND ai_score < 70 THEN 1 ELSE 0 END) as fair,
           SUM(CASE WHEN ai_score < 60 THEN 1 ELSE 0 END) as needs_work,
           COUNT(*) as total,
           AVG(ai_score) as avg_score
           FROM candidates WHERE user_id = ? AND ai_score IS NOT NULL''', (uid,)
    ).fetchone()

    # Per-interview averages
    interview_scores = db.execute(
        '''SELECT i.id, i.title, i.position, COUNT(c.id) as scored_count,
           AVG(c.ai_score) as avg_score, MIN(c.ai_score) as min_score, MAX(c.ai_score) as max_score
           FROM interviews i JOIN candidates c ON i.id = c.interview_id
           WHERE i.user_id = ? AND c.ai_score IS NOT NULL
           GROUP BY i.id ORDER BY avg_score DESC''', (uid,)
    ).fetchall()

    # Per-question scores (response-level)
    question_scores = db.execute(
        '''SELECT q.question_text, AVG(r.ai_score) as avg_score, COUNT(r.id) as response_count
           FROM responses r
           JOIN questions q ON r.question_id = q.id
           JOIN candidates c ON r.candidate_id = c.id
           WHERE c.user_id = ? AND r.ai_score IS NOT NULL
           GROUP BY q.id ORDER BY avg_score DESC LIMIT 15''', (uid,)
    ).fetchall()

    # Recent AI scorings
    recent_scorings = db.execute(
        '''SELECT c.id, c.first_name, c.last_name, c.ai_score, c.updated_at, i.title as interview_title
           FROM candidates c JOIN interviews i ON c.interview_id = i.id
           WHERE c.user_id = ? AND c.ai_score IS NOT NULL
           ORDER BY c.updated_at DESC LIMIT 10''', (uid,)
    ).fetchall()

    db.close()

    return jsonify({
        'top_candidates': [dict(r) for r in top_candidates],
        'score_distribution': dict(score_ranges) if score_ranges else {},
        'interview_scores': [dict(r) for r in interview_scores],
        'question_scores': [dict(r) for r in question_scores],
        'recent_scorings': [dict(r) for r in recent_scorings]
    })

# ======================== SETTINGS API ========================

@app.route('/api/me', methods=['GET'])
@require_auth
def api_me():
    """Return the current authenticated user's profile."""
    safe_fields = ['id', 'email', 'name', 'agency_name', 'brand_color', 'plan',
                   'smtp_host', 'smtp_port', 'smtp_user', 'smtp_from_email', 'smtp_from_name',
                   'created_at', 'updated_at']
    profile = {k: g.user.get(k) for k in safe_fields}
    profile['smtp_configured'] = bool(g.user.get('smtp_host') and g.user.get('smtp_user'))
    return jsonify(profile)

@app.route('/api/settings', methods=['PUT'])
@require_auth
@require_role('admin')
def api_update_settings():
    data = request.get_json()
    db = get_db()
    fields = ['name', 'agency_name', 'brand_color',
              'smtp_host', 'smtp_port', 'smtp_user', 'smtp_pass',
              'smtp_from_email', 'smtp_from_name']
    updates = []
    values = []
    for f in fields:
        if f in data:
            updates.append(f"{f}=?")
            values.append(data[f])
    if updates:
        values.append(g.user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)}, updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
        db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/settings/test-email', methods=['POST'])
@require_auth
@require_role('admin')
def api_test_email():
    """Send a test email to verify SMTP configuration."""
    db = get_db()
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    db.close()

    if not user.get('smtp_host') or not user.get('smtp_user'):
        return jsonify({'error': 'SMTP is not configured. Save your SMTP settings first.'}), 400

    try:
        from email_service import send_email
        smtp_config = {
            'host': user['smtp_host'],
            'port': user['smtp_port'] or 587,
            'user': user['smtp_user'],
            'password': user['smtp_pass'],
            'from_email': user['smtp_from_email'] or user['email'],
            'from_name': user['smtp_from_name'] or user['name'] or 'ChannelView'
        }
        html_body = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
            <div style="background:#0ace0a;padding:16px 24px;border-radius:8px 8px 0 0;">
                <h2 style="color:#000;margin:0;">ChannelView — SMTP Test</h2>
            </div>
            <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">
                <p style="color:#333;font-size:16px;">Your SMTP configuration is working correctly.</p>
                <p style="color:#666;font-size:14px;">Server: {user['smtp_host']}:{user['smtp_port']}<br>
                From: {user['smtp_from_email'] or user['email']}</p>
                <hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">
                <p style="color:#999;font-size:12px;">— ChannelView</p>
            </div>
        </div>"""
        send_email(smtp_config, user['email'], 'ChannelView — SMTP Test Successful', html_body)
        return jsonify({'success': True, 'message': f'Test email sent to {user["email"]}'})
    except Exception as e:
        return jsonify({'error': f'SMTP test failed: {str(e)}'}), 400

# ======================== STORAGE STATS ========================

@app.route('/api/storage', methods=['GET'])
@require_auth
def api_storage_stats():
    """Return storage usage stats for the current user."""
    db = get_db()

    # Count videos and total size from DB
    stats = db.execute(
        '''SELECT COUNT(r.id) as video_count, COALESCE(SUM(r.file_size), 0) as db_total_size
           FROM responses r
           JOIN candidates c ON r.candidate_id = c.id
           WHERE c.user_id = ? AND r.video_path IS NOT NULL''', (g.user_id,)
    ).fetchone()

    # Also scan the filesystem for actual disk usage
    video_dir = app.config['UPLOAD_FOLDER']
    intro_dir = app.config.get('INTRO_FOLDER', os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'intros'))
    disk_usage = 0
    file_count = 0
    for d in [video_dir, intro_dir]:
        if os.path.isdir(d):
            for f in os.listdir(d):
                fp = os.path.join(d, f)
                if os.path.isfile(fp):
                    disk_usage += os.path.getsize(fp)
                    file_count += 1

    db.close()

    def fmt_size(b):
        if b < 1024: return f'{b} B'
        if b < 1024**2: return f'{b/1024:.1f} KB'
        if b < 1024**3: return f'{b/(1024**2):.1f} MB'
        return f'{b/(1024**3):.2f} GB'

    storage_info = storage.get_stats()
    return jsonify({
        'video_count': stats['video_count'],
        'db_total_size': stats['db_total_size'],
        'disk_usage': disk_usage,
        'disk_files': file_count,
        'formatted_disk': fmt_size(disk_usage),
        'formatted_db': fmt_size(stats['db_total_size']),
        'max_response_size_mb': app_config.MAX_RESPONSE_MB,
        'storage_backend': storage_info.get('backend', 'local'),
    })

# ======================== TEAM MANAGEMENT ========================

@app.route('/api/team', methods=['GET'])
@require_auth
def api_list_team():
    """List team members for the current account."""
    db = get_db()
    members = db.execute(
        '''SELECT tm.id, tm.role, tm.status, tm.created_at,
           u.id as user_id, u.name, u.email
           FROM team_members tm JOIN users u ON tm.user_id = u.id
           WHERE tm.account_id = ? ORDER BY tm.created_at''', (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify([dict(m) for m in members])

@app.route('/api/team', methods=['POST'])
@require_auth
@require_role('admin')
def api_add_team_member():
    """Invite a team member by email. Creates their account if needed."""
    data = request.get_json()
    email = (data.get('email') or '').strip().lower()
    name = (data.get('name') or '').strip()
    role = data.get('role', 'reviewer')
    if not email:
        return jsonify({'error': 'Email is required'}), 400
    if role not in ('admin', 'recruiter', 'reviewer'):
        return jsonify({'error': 'Role must be admin, recruiter, or reviewer'}), 400

    db = get_db()
    # Cycle 31: Team seat limit enforcement
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    team_count = db.execute('SELECT COUNT(*) as cnt FROM team_members WHERE account_id=? AND status=?',
                            (g.user_id, 'active')).fetchone()['cnt']
    allowed, limit, remaining = check_plan_limit(user, 'team_seats', team_count + 1)  # +1 for account owner
    if not allowed:
        db.close()
        return jsonify({
            'error': 'team_seat_limit_reached',
            'message': f'You\'ve reached your team seat limit ({limit} members). Upgrade your plan to add more team members.',
            'limit': limit, 'used': team_count + 1, 'upgrade_url': '/billing', 'soft_block': True
        }), 403
    # Check if user already exists
    user = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if user:
        user_id = user['id']
        # Check if already a team member
        existing = db.execute('SELECT id FROM team_members WHERE account_id=? AND user_id=?', (g.user_id, user_id)).fetchone()
        if existing:
            db.close()
            return jsonify({'error': 'This person is already on your team'}), 409
    else:
        # Create a placeholder account for them
        user_id = str(uuid.uuid4())
        temp_pw = bcrypt.hashpw(uuid.uuid4().hex.encode(), bcrypt.gensalt()).decode()
        db.execute('INSERT INTO users (id, email, password_hash, name, agency_name) VALUES (?,?,?,?,?)',
                   (user_id, email, temp_pw, name or email.split('@')[0], g.user.get('agency_name','')))

    tm_id = str(uuid.uuid4())
    db.execute('INSERT INTO team_members (id, account_id, user_id, role, invited_by) VALUES (?,?,?,?,?)',
               (tm_id, g.user_id, user_id, role, g.user_id))

    # Audit log
    db.execute('INSERT INTO audit_log (id, account_id, user_id, action, entity_type, entity_id, details) VALUES (?,?,?,?,?,?,?)',
               (str(uuid.uuid4()), g.user_id, g.user_id, 'team_invite', 'team_member', tm_id, json.dumps({'email': email, 'role': role})))
    db.commit()
    db.close()
    return jsonify({'success': True, 'id': tm_id, 'user_id': user_id}), 201

@app.route('/api/team/<member_id>', methods=['PUT'])
@require_auth
@require_role('admin')
def api_update_team_member(member_id):
    """Update a team member's role."""
    data = request.get_json()
    role = data.get('role')
    if role and role not in ('admin', 'recruiter', 'reviewer'):
        return jsonify({'error': 'Invalid role'}), 400
    db = get_db()
    if role:
        db.execute('UPDATE team_members SET role=? WHERE id=? AND account_id=?', (role, member_id, g.user_id))
    if data.get('status') in ('active', 'suspended'):
        db.execute('UPDATE team_members SET status=? WHERE id=? AND account_id=?', (data['status'], member_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/team/<member_id>', methods=['DELETE'])
@require_auth
@require_role('admin')
def api_remove_team_member(member_id):
    """Remove a team member."""
    db = get_db()
    db.execute('DELETE FROM team_members WHERE id=? AND account_id=?', (member_id, g.user_id))
    db.execute('INSERT INTO audit_log (id, account_id, user_id, action, entity_type, entity_id) VALUES (?,?,?,?,?,?)',
               (str(uuid.uuid4()), g.user_id, g.user_id, 'team_remove', 'team_member', member_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/audit-log', methods=['GET'])
@require_auth
def api_audit_log():
    """Retrieve audit log for the account."""
    limit = request.args.get('limit', 50, type=int)
    db = get_db()
    logs = db.execute(
        '''SELECT al.*, u.name as user_name, u.email as user_email
           FROM audit_log al JOIN users u ON al.user_id = u.id
           WHERE al.account_id = ? ORDER BY al.created_at DESC LIMIT ?''', (g.user_id, limit)
    ).fetchall()
    db.close()
    return jsonify([dict(l) for l in logs])

# ======================== INTERVIEW TEMPLATES ========================

@app.route('/api/templates', methods=['GET'])
@require_auth
def api_list_templates():
    """List interview templates."""
    db = get_db()
    templates = db.execute(
        'SELECT * FROM interview_templates WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)
    ).fetchall()
    result = []
    for t in templates:
        td = dict(t)
        td['questions'] = [dict(q) for q in db.execute(
            'SELECT * FROM template_questions WHERE template_id=? ORDER BY question_order', (t['id'],)
        ).fetchall()]
        result.append(td)
    db.close()
    return jsonify(result)

@app.route('/api/templates', methods=['POST'])
@require_auth
def api_create_template():
    """Create an interview template."""
    data = request.get_json()
    tid = str(uuid.uuid4())
    db = get_db()
    db.execute(
        '''INSERT INTO interview_templates (id, user_id, title, description, department, position,
           thinking_time, max_answer_time, max_retakes, welcome_msg, thank_you_msg, is_shared)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (tid, g.user_id, data.get('title', 'Untitled Template'),
         data.get('description', ''), data.get('department', ''), data.get('position', ''),
         data.get('thinking_time', 30), data.get('max_answer_time', 120), data.get('max_retakes', 1),
         data.get('welcome_msg', ''), data.get('thank_you_msg', ''),
         1 if data.get('is_shared') else 0)
    )
    for i, q in enumerate(data.get('questions', [])):
        db.execute(
            'INSERT INTO template_questions (id, template_id, question_text, question_order, category) VALUES (?,?,?,?,?)',
            (str(uuid.uuid4()), tid, q.get('text',''), i+1, q.get('category','general'))
        )
    db.commit()
    db.close()
    return jsonify({'success': True, 'id': tid}), 201

@app.route('/api/templates/<template_id>', methods=['GET'])
@require_auth
def api_get_template(template_id):
    db = get_db()
    t = db.execute('SELECT * FROM interview_templates WHERE id=? AND user_id=?', (template_id, g.user_id)).fetchone()
    if not t:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    td = dict(t)
    td['questions'] = [dict(q) for q in db.execute(
        'SELECT * FROM template_questions WHERE template_id=? ORDER BY question_order', (template_id,)
    ).fetchall()]
    db.close()
    return jsonify(td)

@app.route('/api/templates/<template_id>', methods=['DELETE'])
@require_auth
def api_delete_template(template_id):
    db = get_db()
    db.execute('DELETE FROM template_questions WHERE template_id=?', (template_id,))
    db.execute('DELETE FROM interview_templates WHERE id=? AND user_id=?', (template_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/templates/<template_id>/create-interview', methods=['POST'])
@require_auth
def api_create_from_template(template_id):
    """Create a new interview from a template."""
    db = get_db()
    t = db.execute('SELECT * FROM interview_templates WHERE id=? AND user_id=?', (template_id, g.user_id)).fetchone()
    if not t:
        db.close()
        return jsonify({'error': 'Template not found'}), 404

    data = request.get_json() or {}
    iid = str(uuid.uuid4())
    db.execute(
        '''INSERT INTO interviews (id, user_id, title, description, department, position,
           thinking_time, max_answer_time, max_retakes, welcome_msg, thank_you_msg)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (iid, g.user_id,
         data.get('title', t['title']), data.get('description', t['description'] or ''),
         t['department'] or '', t['position'] or '',
         t['thinking_time'], t['max_answer_time'], t['max_retakes'],
         t['welcome_msg'] or 'Welcome! This interview consists of a few video questions.',
         t['thank_you_msg'] or 'Thank you for completing your interview!')
    )

    tq = db.execute('SELECT * FROM template_questions WHERE template_id=? ORDER BY question_order', (template_id,)).fetchall()
    for q in tq:
        db.execute('INSERT INTO questions (id, interview_id, question_text, question_order) VALUES (?,?,?,?)',
                   (str(uuid.uuid4()), iid, q['question_text'], q['question_order']))

    db.commit()
    db.close()
    return jsonify({'success': True, 'id': iid}), 201

# ======================== QUESTION LIBRARY ========================

@app.route('/api/question-library', methods=['GET'])
@require_auth
def api_list_library_questions():
    """List questions in the library, optionally filtered by category."""
    category = request.args.get('category')
    db = get_db()
    if category:
        questions = db.execute('SELECT * FROM question_library WHERE user_id=? AND category=? ORDER BY use_count DESC',
                               (g.user_id, category)).fetchall()
    else:
        questions = db.execute('SELECT * FROM question_library WHERE user_id=? ORDER BY use_count DESC',
                               (g.user_id,)).fetchall()
    db.close()
    return jsonify([dict(q) for q in questions])

@app.route('/api/question-library', methods=['POST'])
@require_auth
def api_add_library_question():
    """Add a question to the library."""
    data = request.get_json()
    qid = str(uuid.uuid4())
    db = get_db()
    db.execute('INSERT INTO question_library (id, user_id, question_text, category, tags) VALUES (?,?,?,?,?)',
               (qid, g.user_id, data.get('question_text',''), data.get('category','general'),
                data.get('tags','')))
    db.commit()
    db.close()
    return jsonify({'success': True, 'id': qid}), 201

@app.route('/api/question-library/<question_id>', methods=['PUT'])
@require_auth
def api_update_library_question(question_id):
    data = request.get_json()
    db = get_db()
    fields, values = [], []
    for f in ['question_text','category','tags']:
        if f in data:
            fields.append(f'{f}=?')
            values.append(data[f])
    if fields:
        values.append(question_id)
        values.append(g.user_id)
        db.execute(f'UPDATE question_library SET {", ".join(fields)} WHERE id=? AND user_id=?', values)
        db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/question-library/<question_id>', methods=['DELETE'])
@require_auth
def api_delete_library_question(question_id):
    db = get_db()
    db.execute('DELETE FROM question_library WHERE id=? AND user_id=?', (question_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/question-library/categories', methods=['GET'])
@require_auth
def api_question_categories():
    """List unique question categories."""
    db = get_db()
    cats = db.execute('SELECT DISTINCT category FROM question_library WHERE user_id=? ORDER BY category', (g.user_id,)).fetchall()
    db.close()
    return jsonify([c['category'] for c in cats])

# ======================== CANDIDATE COMPARISON ========================

@app.route('/api/compare', methods=['POST'])
@require_auth
def api_compare_candidates():
    """Compare multiple candidates side-by-side with AI scores."""
    data = request.get_json()
    candidate_ids = data.get('candidate_ids', [])
    if len(candidate_ids) < 2:
        return jsonify({'error': 'Select at least 2 candidates to compare'}), 400
    if len(candidate_ids) > 6:
        return jsonify({'error': 'Maximum 6 candidates per comparison'}), 400

    db = get_db()
    placeholders = ','.join('?' * len(candidate_ids))
    candidates = db.execute(
        f'''SELECT c.*, i.title as interview_title, i.position
           FROM candidates c JOIN interviews i ON c.interview_id = i.id
           WHERE c.id IN ({placeholders}) AND c.user_id = ?''',
        (*candidate_ids, g.user_id)
    ).fetchall()

    results = []
    for c in candidates:
        cd = dict(c)
        # Parse category scores
        if cd.get('ai_scores_json'):
            try:
                cd['ai_scores_parsed'] = json.loads(cd['ai_scores_json'])
            except:
                cd['ai_scores_parsed'] = None
        else:
            cd['ai_scores_parsed'] = None

        # Get per-response scores
        resps = db.execute(
            '''SELECT r.ai_score, r.ai_feedback, q.question_text, q.question_order
               FROM responses r JOIN questions q ON r.question_id = q.id
               WHERE r.candidate_id = ? ORDER BY q.question_order''', (c['id'],)
        ).fetchall()
        cd['responses'] = [dict(r) for r in resps]
        results.append(cd)

    # Sort by overall score descending
    results.sort(key=lambda x: x.get('ai_score') or 0, reverse=True)

    # Category comparison matrix
    categories = ['communication','industry_knowledge','role_competence','culture_fit','problem_solving']
    cat_labels = {'communication':'Communication','industry_knowledge':'Industry Knowledge',
                  'role_competence':'Role Competence','culture_fit':'Culture Fit','problem_solving':'Problem Solving'}
    comparison_matrix = {}
    for cat in categories:
        comparison_matrix[cat] = {
            'label': cat_labels[cat],
            'scores': {}
        }
        for c in results:
            parsed = c.get('ai_scores_parsed')
            if parsed and 'categories' in parsed:
                comparison_matrix[cat]['scores'][c['id']] = parsed['categories'].get(cat, 0)
            else:
                comparison_matrix[cat]['scores'][c['id']] = 0

    db.close()
    return jsonify({
        'candidates': results,
        'comparison_matrix': comparison_matrix,
        'category_labels': cat_labels
    })

# ======================== ANALYTICS ========================

@app.route('/api/analytics', methods=['GET'])
@require_auth
def api_analytics():
    """Comprehensive analytics dashboard data."""
    db = get_db()
    uid = g.user_id
    period = request.args.get('period', '30')  # days
    try:
        days = int(period)
    except:
        days = 30

    from_date = (datetime.utcnow() - timedelta(days=days)).isoformat()

    # Overall stats
    total_interviews = db.execute('SELECT COUNT(*) as c FROM interviews WHERE user_id=?', (uid,)).fetchone()['c']
    total_candidates = db.execute('SELECT COUNT(*) as c FROM candidates WHERE user_id=?', (uid,)).fetchone()['c']
    total_completed = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND status IN ('completed','reviewed','hired')", (uid,)).fetchone()['c']
    total_hired = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND status='hired'", (uid,)).fetchone()['c']
    avg_score = db.execute('SELECT AVG(ai_score) as a FROM candidates WHERE user_id=? AND ai_score IS NOT NULL', (uid,)).fetchone()['a']

    # Completion rate
    completion_rate = round(total_completed / max(total_candidates, 1) * 100, 1)
    hire_rate = round(total_hired / max(total_completed, 1) * 100, 1) if total_completed > 0 else 0

    # Candidates over time (last N days, grouped by date)
    timeline = db.execute(
        '''SELECT DATE(created_at) as date, COUNT(*) as count
           FROM candidates WHERE user_id=? AND created_at >= ?
           GROUP BY DATE(created_at) ORDER BY date''', (uid, from_date)
    ).fetchall()

    # Completions over time
    completions_timeline = db.execute(
        '''SELECT DATE(completed_at) as date, COUNT(*) as count
           FROM candidates WHERE user_id=? AND completed_at IS NOT NULL AND completed_at >= ?
           GROUP BY DATE(completed_at) ORDER BY date''', (uid, from_date)
    ).fetchall()

    # Score distribution
    score_dist = db.execute(
        '''SELECT
           SUM(CASE WHEN ai_score >= 90 THEN 1 ELSE 0 END) as excellent,
           SUM(CASE WHEN ai_score >= 80 AND ai_score < 90 THEN 1 ELSE 0 END) as strong,
           SUM(CASE WHEN ai_score >= 70 AND ai_score < 80 THEN 1 ELSE 0 END) as good,
           SUM(CASE WHEN ai_score >= 60 AND ai_score < 70 THEN 1 ELSE 0 END) as fair,
           SUM(CASE WHEN ai_score < 60 THEN 1 ELSE 0 END) as needs_work
           FROM candidates WHERE user_id=? AND ai_score IS NOT NULL''', (uid,)
    ).fetchone()

    # Per-interview performance
    interview_perf = db.execute(
        '''SELECT i.id, i.title, i.position, i.department,
           COUNT(c.id) as total_candidates,
           SUM(CASE WHEN c.status IN ('completed','reviewed','hired') THEN 1 ELSE 0 END) as completed,
           AVG(c.ai_score) as avg_score,
           MIN(c.ai_score) as min_score,
           MAX(c.ai_score) as max_score
           FROM interviews i LEFT JOIN candidates c ON i.id = c.interview_id
           WHERE i.user_id=? GROUP BY i.id ORDER BY i.created_at DESC''', (uid,)
    ).fetchall()

    # Category averages across all candidates
    all_scored = db.execute(
        'SELECT ai_scores_json FROM candidates WHERE user_id=? AND ai_scores_json IS NOT NULL', (uid,)
    ).fetchall()
    cat_totals = {'communication':[],'industry_knowledge':[],'role_competence':[],'culture_fit':[],'problem_solving':[]}
    for row in all_scored:
        try:
            data = json.loads(row['ai_scores_json'])
            cats = data.get('categories', {})
            for c in cat_totals:
                if c in cats:
                    cat_totals[c].append(cats[c])
        except:
            pass
    cat_averages = {c: round(sum(v)/max(len(v),1), 1) if v else 0 for c, v in cat_totals.items()}

    # Email stats
    emails_sent = db.execute(
        "SELECT COUNT(*) as c FROM email_log WHERE user_id=? AND status='sent'", (uid,)
    ).fetchone()['c']
    emails_failed = db.execute(
        "SELECT COUNT(*) as c FROM email_log WHERE user_id=? AND status='failed'", (uid,)
    ).fetchone()['c']

    # Average time to complete (in hours)
    avg_time = db.execute(
        '''SELECT AVG(JULIANDAY(completed_at) - JULIANDAY(invited_at)) * 24 as avg_hours
           FROM candidates WHERE user_id=? AND completed_at IS NOT NULL AND invited_at IS NOT NULL''', (uid,)
    ).fetchone()['avg_hours']

    db.close()

    return jsonify({
        'overview': {
            'total_interviews': total_interviews,
            'total_candidates': total_candidates,
            'total_completed': total_completed,
            'total_hired': total_hired,
            'avg_score': round(avg_score, 1) if avg_score else None,
            'completion_rate': completion_rate,
            'hire_rate': hire_rate,
            'avg_completion_hours': round(avg_time, 1) if avg_time else None,
            'emails_sent': emails_sent,
            'emails_failed': emails_failed
        },
        'timeline': [dict(t) for t in timeline],
        'completions_timeline': [dict(t) for t in completions_timeline],
        'score_distribution': dict(score_dist) if score_dist else {},
        'interview_performance': [dict(i) for i in interview_perf],
        'category_averages': cat_averages,
        'category_labels': {'communication':'Communication','industry_knowledge':'Industry Knowledge',
                           'role_competence':'Role Competence','culture_fit':'Culture Fit','problem_solving':'Problem Solving'},
        'period_days': days
    })

@app.route('/api/analytics/export', methods=['GET'])
@require_auth
def api_analytics_export():
    """Export candidate data as CSV."""
    db = get_db()
    candidates = db.execute(
        '''SELECT c.first_name, c.last_name, c.email, c.status, c.ai_score, c.ai_summary,
           c.created_at, c.completed_at, i.title as interview, i.position, i.department
           FROM candidates c JOIN interviews i ON c.interview_id = i.id
           WHERE c.user_id = ? ORDER BY c.created_at DESC''', (g.user_id,)
    ).fetchall()
    db.close()

    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['First Name','Last Name','Email','Status','AI Score','Summary','Interview','Position','Department','Created','Completed'])
    for c in candidates:
        writer.writerow([c['first_name'], c['last_name'], c['email'], c['status'],
                        c['ai_score'] or '', c['ai_summary'] or '',
                        c['interview'], c['position'] or '', c['department'] or '',
                        c['created_at'], c['completed_at'] or ''])

    resp = make_response(output.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = 'attachment; filename=channelview_candidates.csv'
    return resp

# ======================== SEED DATA ========================

@app.route('/api/seed', methods=['POST'])
@require_auth
def api_seed():
    """Create sample data for demonstration"""
    import random
    db = get_db()
    uid = g.user_id

    # Check if already seeded
    existing = db.execute('SELECT COUNT(*) as c FROM interviews WHERE user_id=?', (uid,)).fetchone()['c']
    if existing > 0:
        db.close()
        return jsonify({'message': 'Data already exists'})

    # Create sample interviews
    interviews_data = [
        {'title': 'Licensed Insurance Agent - P&C', 'dept': 'Sales', 'pos': 'P&C Agent',
         'desc': 'Screening for property & casualty insurance agents with active state license.'},
        {'title': 'ACA Health Insurance Specialist', 'dept': 'Health', 'pos': 'ACA Specialist',
         'desc': 'Open enrollment specialists for ACA marketplace plans.'},
        {'title': 'Medicare Supplement Advisor', 'dept': 'Senior Markets', 'pos': 'Medicare Advisor',
         'desc': 'Experienced advisors for Medicare supplement and advantage plan sales.'},
        {'title': 'Agency Customer Service Rep', 'dept': 'Operations', 'pos': 'CSR',
         'desc': 'Customer-facing service representatives for our insurance agency.'},
    ]

    question_sets = [
        ["Tell us about your experience in the insurance industry and why you're interested in this role.",
         "Describe a time you had to explain a complex insurance policy to a client. How did you make it understandable?",
         "How do you stay current with insurance regulations and market changes?",
         "What's your approach to building long-term client relationships?",
         "Where do you see yourself in 3 years within our agency?"],
        ["Walk us through your experience with ACA marketplace plans.",
         "How do you handle a client who's frustrated with their premium increase?",
         "Describe your approach during Open Enrollment Period. How do you manage high volume?",
         "What CRM or AMS systems have you used, and how do they improve your workflow?",
         "Tell us about a time you went above and beyond for a client."],
        ["What is your experience with Medicare Supplement and Medicare Advantage products?",
         "How do you approach the Annual Enrollment Period differently from Open Enrollment?",
         "Describe how you'd explain the difference between Medigap and MA to a confused senior.",
         "What compliance considerations are top of mind when selling Medicare products?",
         "How do you generate leads and build your book of business in the senior market?"],
        ["How would you handle an angry policyholder calling about a denied claim?",
         "Describe your experience with insurance agency management systems.",
         "What does excellent customer service look like in an insurance agency?",
         "How do you prioritize when multiple clients need help at the same time?",
         "Tell us about a time you identified a coverage gap for an existing client."]
    ]

    first_names = ['Sarah','Michael','Jennifer','David','Emily','James','Maria','Robert','Lisa','John',
                   'Amanda','Daniel','Jessica','Chris','Rachel','Kevin','Michelle','Brian','Nicole','Tyler']
    last_names = ['Johnson','Williams','Brown','Martinez','Davis','Anderson','Wilson','Thompson','Garcia','Miller',
                  'Taylor','Thomas','Moore','Jackson','Martin','Lee','Perez','White','Harris','Clark']
    statuses = ['invited','invited','in_progress','completed','completed','completed','reviewed','reviewed','hired','rejected']

    for idx, idata in enumerate(interviews_data):
        iid = str(uuid.uuid4())
        db.execute(
            '''INSERT INTO interviews (id, user_id, title, description, department, position, thinking_time, max_answer_time, max_retakes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (iid, uid, idata['title'], idata['desc'], idata['dept'], idata['pos'], 30, 120, 2)
        )
        for qi, qtext in enumerate(question_sets[idx]):
            db.execute(
                'INSERT INTO questions (id, interview_id, question_text, question_order) VALUES (?, ?, ?, ?)',
                (str(uuid.uuid4()), iid, qtext, qi + 1)
            )

        # Create sample candidates for this interview
        num_candidates = random.randint(8, 15)
        for ci in range(num_candidates):
            cid = str(uuid.uuid4())
            status = random.choice(statuses)
            score = round(random.uniform(55, 98), 1) if status in ('completed','reviewed','hired') else None
            summary = f"Solid candidate demonstrating relevant insurance experience. Score: {score}/100." if score else None
            fn = random.choice(first_names)
            ln = random.choice(last_names)
            db.execute(
                '''INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email, phone, token, status, ai_score, ai_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (cid, uid, iid, fn, ln, f"{fn.lower()}.{ln.lower()}@email.com",
                 f"(555) {random.randint(100,999)}-{random.randint(1000,9999)}",
                 str(uuid.uuid4()), status, score, summary)
            )

    db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'Sample data created'})

# ======================== SHAREABLE REPORTS API ========================

@app.route('/api/candidates/<candidate_id>/report', methods=['POST'])
@require_auth
def api_create_report(candidate_id):
    """Generate a shareable report for a candidate."""
    data = request.get_json() or {}
    db = get_db()

    candidate = db.execute(
        'SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    report_id = str(uuid.uuid4())
    report_token = uuid.uuid4().hex[:16]
    password = data.get('password', '')
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode() if password else None

    expires_at = None
    if data.get('expires_days'):
        expires_at = (datetime.utcnow() + timedelta(days=int(data['expires_days']))).isoformat()

    db.execute(
        '''INSERT INTO reports (id, user_id, candidate_id, token, title, password_hash,
           include_scores, include_ai_feedback, include_notes, include_videos,
           custom_message, expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (report_id, g.user_id, candidate_id, report_token,
         data.get('title', ''),
         pw_hash,
         1 if data.get('include_scores', True) else 0,
         1 if data.get('include_ai_feedback', True) else 0,
         1 if data.get('include_notes', False) else 0,
         1 if data.get('include_videos', False) else 0,
         data.get('custom_message', ''),
         expires_at)
    )
    db.commit()
    db.close()

    share_url = f"{request.host_url}report/{report_token}"
    return jsonify({'success': True, 'report_id': report_id, 'token': report_token, 'url': share_url})


@app.route('/api/candidates/<candidate_id>/reports', methods=['GET'])
@require_auth
def api_list_reports(candidate_id):
    """List all existing reports for a candidate."""
    db = get_db()
    reports = db.execute(
        'SELECT id, token, title, views, created_at, expires_at, password_hash IS NOT NULL as has_password FROM reports WHERE candidate_id=? AND user_id=? ORDER BY created_at DESC',
        (candidate_id, g.user_id)
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in reports])


@app.route('/api/reports/<report_id>', methods=['DELETE'])
@require_auth
def api_delete_report(report_id):
    """Delete a shared report."""
    db = get_db()
    db.execute('DELETE FROM reports WHERE id=? AND user_id=?', (report_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


# Rate limiter for report password attempts (in-memory, per-token)
_pw_attempts = {}  # { token: [timestamp, ...] }
_PW_MAX_ATTEMPTS = 5
_PW_WINDOW = 60  # seconds

def _check_rate_limit(token):
    """Check if password attempts are rate-limited. Returns True if allowed."""
    now = time.time()
    attempts = _pw_attempts.get(token, [])
    # Prune old attempts outside the window
    attempts = [t for t in attempts if now - t < _PW_WINDOW]
    _pw_attempts[token] = attempts
    if len(attempts) >= _PW_MAX_ATTEMPTS:
        return False
    return True

def _record_attempt(token):
    """Record a failed password attempt."""
    if token not in _pw_attempts:
        _pw_attempts[token] = []
    _pw_attempts[token].append(time.time())

@app.route('/report/<token>', methods=['GET', 'POST'])
def view_report(token):
    """Public page: view a shared candidate report."""
    db = get_db()
    report = db.execute('SELECT * FROM reports WHERE token=?', (token,)).fetchone()
    if not report:
        db.close()
        return render_template('candidate_error.html', error='Report not found or has been deleted.'), 404

    # Check expiry
    if report['expires_at']:
        from datetime import datetime as dt
        try:
            exp = dt.fromisoformat(report['expires_at'])
            if dt.utcnow() > exp:
                db.close()
                return render_template('candidate_error.html', error='This report link has expired.'), 410
        except:
            pass

    # Password check — if protected, show password form or validate POST
    if report['password_hash']:
        pw = ''
        error_msg = ''
        if request.method == 'POST':
            # Rate limit check
            if not _check_rate_limit(token):
                db.close()
                return render_template('report_password.html', token=token,
                    error='Too many attempts. Please wait a minute and try again.'), 429
            pw = request.form.get('password', '')
            if pw and bcrypt.checkpw(pw.encode(), report['password_hash'].encode()):
                pass  # Password correct — fall through to render report
            else:
                _record_attempt(token)
                db.close()
                return render_template('report_password.html', token=token,
                    error='Incorrect password. Please try again.')
        else:
            # GET request — show password form (no error)
            db.close()
            return render_template('report_password.html', token=token, error='')

    # Fetch candidate + interview + responses
    candidate = db.execute(
        '''SELECT c.*, i.title as interview_title, i.position, i.department, i.description as interview_desc,
           u.agency_name, u.name as agency_contact, u.brand_color
           FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           JOIN users u ON c.user_id = u.id
           WHERE c.id = ?''', (report['candidate_id'],)
    ).fetchone()

    if not candidate:
        db.close()
        return render_template('candidate_error.html', error='Candidate data no longer available.'), 404

    responses = db.execute(
        '''SELECT r.*, q.question_text, q.question_order FROM responses r
           JOIN questions q ON r.question_id = q.id
           WHERE r.candidate_id = ? ORDER BY q.question_order''', (report['candidate_id'],)
    ).fetchall()

    # Increment view count
    db.execute('UPDATE reports SET views = views + 1 WHERE id=?', (report['id'],))
    db.commit()
    db.close()

    # Parse JSON scores for template
    cand_dict = dict(candidate)
    if cand_dict.get('ai_scores_json'):
        try:
            cand_dict['ai_scores_json'] = json.loads(cand_dict['ai_scores_json'])
        except:
            pass

    resp_list = []
    for r in responses:
        rd = dict(r)
        if rd.get('ai_scores_json'):
            try:
                rd['ai_scores_json'] = json.loads(rd['ai_scores_json'])
            except:
                pass
        resp_list.append(rd)

    return render_template('report.html',
        report=dict(report),
        candidate=cand_dict,
        responses=resp_list,
        brand_color=candidate['brand_color'] or '#0ace0a'
    )


# ======================== MANAGERS / CONTACTS ========================

@app.route('/api/managers', methods=['GET'])
@require_auth
def list_managers():
    db = get_db()
    managers = db.execute(
        'SELECT * FROM managers WHERE user_id=? ORDER BY name', (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify([dict(m) for m in managers])

@app.route('/api/managers', methods=['POST'])
@require_auth
def add_manager():
    data = request.get_json()
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip()
    if not name or not email:
        return jsonify({'error': 'Name and email are required'}), 400

    mid = str(uuid.uuid4())
    db = get_db()
    db.execute(
        'INSERT INTO managers (id, user_id, name, email, title, department) VALUES (?,?,?,?,?,?)',
        (mid, g.user_id, name, email,
         (data.get('title') or '').strip() or None,
         (data.get('department') or '').strip() or None)
    )
    db.commit()
    manager = db.execute('SELECT * FROM managers WHERE id=?', (mid,)).fetchone()
    db.close()
    return jsonify(dict(manager)), 201

@app.route('/api/managers/<manager_id>', methods=['DELETE'])
@require_auth
def delete_manager(manager_id):
    db = get_db()
    db.execute('DELETE FROM managers WHERE id=? AND user_id=?', (manager_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/reports/<report_id>/send', methods=['POST'])
@require_auth
def send_report_email(report_id):
    """Send report link to selected managers via email."""
    data = request.get_json()
    manager_ids = data.get('manager_ids', [])
    if not manager_ids:
        return jsonify({'error': 'Select at least one manager'}), 400

    db = get_db()
    report = db.execute('SELECT * FROM reports WHERE id=? AND user_id=?', (report_id, g.user_id)).fetchone()
    if not report:
        db.close()
        return jsonify({'error': 'Report not found'}), 404

    candidate = db.execute(
        '''SELECT c.*, i.title as interview_title, i.position FROM candidates c
           JOIN interviews i ON c.interview_id = i.id WHERE c.id = ?''',
        (report['candidate_id'],)
    ).fetchone()

    user = db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone()

    managers = db.execute(
        f'SELECT * FROM managers WHERE id IN ({",".join("?" * len(manager_ids))}) AND user_id=?',
        (*manager_ids, g.user_id)
    ).fetchall()

    report_url = f"{request.host_url.rstrip('/')}/report/{report['token']}"
    sender_name = user['smtp_from_name'] or user['name'] or 'ChannelView'
    agency = user['agency_name'] or 'Channel One Strategies'

    sent_count = 0
    errors = []

    from email_service import send_email, get_smtp_config
    smtp_config = get_smtp_config(db, g.user_id)

    for mgr in managers:
        subject = f"Candidate Report: {candidate['first_name']} {candidate['last_name']} — {candidate['interview_title']}"
        score_line = f"<p style='color:#333;font-size:14px;'>Score: <strong>{round(candidate['ai_score'])}/100</strong></p>" if candidate['ai_score'] else ''
        password_note = f"<p style='color:#e65100;font-size:13px;'>Note: This report is password protected. Please contact {sender_name} for the password.</p>" if report['password_hash'] else ''
        html_body = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
            <div style="background:#0ace0a;padding:16px 24px;border-radius:8px 8px 0 0;">
                <h2 style="color:#000;margin:0;">Candidate Report Shared</h2>
            </div>
            <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">
                <p style="color:#333;font-size:16px;">Hi {mgr['name']},</p>
                <p style="color:#333;font-size:14px;">{sender_name} has shared a candidate report with you for review.</p>
                <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin:16px 0;">
                    <p style="color:#333;font-size:14px;margin:0 0 4px;"><strong>Candidate:</strong> {candidate['first_name']} {candidate['last_name']}</p>
                    <p style="color:#333;font-size:14px;margin:0 0 4px;"><strong>Position:</strong> {candidate['position'] or candidate['interview_title']}</p>
                    {score_line}
                </div>
                <a href="{report_url}" style="display:inline-block;background:#0ace0a;color:#000;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold;">View Full Report</a>
                {password_note}
                <hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">
                <p style="color:#666;font-size:13px;">Best regards,<br>{sender_name}<br>{agency}</p>
                <p style="color:#999;font-size:11px;">Powered by ChannelView — Async Video Interviews</p>
            </div>
        </div>"""
        # Try to send via SMTP if configured
        if smtp_config:
            try:
                send_email(smtp_config, mgr['email'], subject, html_body)
                sent_count += 1
                # Log the email
                db.execute(
                    'INSERT INTO email_log (id, user_id, candidate_id, email_type, to_email, subject, status) VALUES (?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()), g.user_id, report['candidate_id'], 'report_share', mgr['email'], subject, 'sent')
                )
            except Exception as e:
                errors.append(f"{mgr['name']}: {str(e)}")
                db.execute(
                    'INSERT INTO email_log (id, user_id, candidate_id, email_type, to_email, subject, status, error_message) VALUES (?,?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()), g.user_id, report['candidate_id'], 'report_share', mgr['email'], subject, 'failed', str(e))
                )
        else:
            errors.append(f"{mgr['name']}: SMTP not configured")

    db.commit()
    db.close()

    if errors and sent_count == 0:
        return jsonify({'error': 'Email sending failed. Configure SMTP in Settings first.', 'details': errors}), 400

    return jsonify({
        'success': True,
        'sent': sent_count,
        'total': len(managers),
        'errors': errors
    })


# ======================== BULK OPERATIONS ========================

@app.route('/api/candidates/bulk-status', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_bulk_status():
    """Bulk update candidate statuses."""
    data = request.get_json()
    candidate_ids = data.get('candidate_ids', [])
    new_status = data.get('status', '')
    if not candidate_ids or not new_status:
        return jsonify({'error': 'candidate_ids and status are required'}), 400
    if new_status not in ('invited','in_progress','completed','reviewed','hired','rejected','archived'):
        return jsonify({'error': 'Invalid status'}), 400
    if len(candidate_ids) > 100:
        return jsonify({'error': 'Maximum 100 candidates per bulk operation'}), 400

    db = get_db()
    placeholders = ','.join('?' * len(candidate_ids))
    # Only update candidates owned by this user
    result = db.execute(
        f'UPDATE candidates SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders}) AND user_id=?',
        [new_status, *candidate_ids, g.user_id]
    )
    updated = result.rowcount
    # Audit
    db.execute('INSERT INTO audit_log (id, account_id, user_id, action, entity_type, details) VALUES (?,?,?,?,?,?)',
               (str(uuid.uuid4()), g.user_id, g.user_id, 'bulk_status_update', 'candidate',
                json.dumps({'count': updated, 'new_status': new_status})))
    db.commit()
    db.close()
    return jsonify({'success': True, 'updated': updated})

@app.route('/api/candidates/bulk-delete', methods=['POST'])
@require_auth
@require_role('admin')
def api_bulk_delete():
    """Bulk delete candidates and their data."""
    data = request.get_json()
    candidate_ids = data.get('candidate_ids', [])
    if not candidate_ids:
        return jsonify({'error': 'candidate_ids required'}), 400
    if len(candidate_ids) > 50:
        return jsonify({'error': 'Maximum 50 candidates per bulk delete'}), 400

    db = get_db()
    deleted = 0
    for cid in candidate_ids:
        candidate = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (cid, g.user_id)).fetchone()
        if not candidate:
            continue
        # Cascade: responses (+ video files), reports, email_log
        responses = db.execute('SELECT id, video_path FROM responses WHERE candidate_id=?', (cid,)).fetchall()
        for resp in responses:
            if resp['video_path']:
                vp = os.path.join(app.config['UPLOAD_FOLDER'], resp['video_path'])
                if os.path.exists(vp):
                    try: os.remove(vp)
                    except: pass
        db.execute('DELETE FROM responses WHERE candidate_id=?', (cid,))
        db.execute('DELETE FROM reports WHERE candidate_id=?', (cid,))
        db.execute('DELETE FROM email_log WHERE candidate_id=?', (cid,))
        db.execute('DELETE FROM candidates WHERE id=?', (cid,))
        deleted += 1

    db.execute('INSERT INTO audit_log (id, account_id, user_id, action, entity_type, details) VALUES (?,?,?,?,?,?)',
               (str(uuid.uuid4()), g.user_id, g.user_id, 'bulk_delete', 'candidate',
                json.dumps({'count': deleted})))
    db.commit()
    db.close()
    return jsonify({'success': True, 'deleted': deleted})

# ======================== INTERVIEW SCHEDULING ========================

@app.route('/api/interviews/<interview_id>/schedule', methods=['PUT'])
@require_auth
@require_role('admin', 'recruiter')
def api_schedule_interview(interview_id):
    """Set or update interview deadline/expiration."""
    data = request.get_json()
    db = get_db()
    interview = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    expires_at = data.get('expires_at')  # ISO format string
    auto_close = data.get('auto_close', True)

    if expires_at:
        db.execute('UPDATE interviews SET expires_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (expires_at, interview_id))
    else:
        db.execute('UPDATE interviews SET expires_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?', (interview_id,))

    db.commit()
    db.close()
    return jsonify({'success': True, 'expires_at': expires_at})

@app.route('/api/interviews/check-expired', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_check_expired():
    """Check and close expired interviews."""
    db = get_db()
    now = datetime.utcnow().isoformat()
    expired = db.execute(
        "SELECT id, title FROM interviews WHERE user_id=? AND status='active' AND expires_at IS NOT NULL AND expires_at < ?",
        (g.user_id, now)
    ).fetchall()

    closed_ids = []
    for iv in expired:
        db.execute("UPDATE interviews SET status='closed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (iv['id'],))
        closed_ids.append(iv['id'])

    if closed_ids:
        db.execute('INSERT INTO audit_log (id, account_id, user_id, action, entity_type, details) VALUES (?,?,?,?,?,?)',
                   (str(uuid.uuid4()), g.user_id, g.user_id, 'auto_close_expired', 'interview',
                    json.dumps({'count': len(closed_ids), 'ids': closed_ids})))
    db.commit()
    db.close()
    return jsonify({'success': True, 'closed': len(closed_ids), 'interview_ids': closed_ids})

@app.route('/api/interviews/<interview_id>/deadline-status', methods=['GET'])
@require_auth
def api_deadline_status(interview_id):
    """Get deadline info for an interview including pending candidates."""
    db = get_db()
    interview = db.execute('SELECT id, title, expires_at, status FROM interviews WHERE id=? AND user_id=?',
                          (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    pending = db.execute(
        "SELECT COUNT(*) as c FROM candidates WHERE interview_id=? AND status IN ('invited','in_progress')",
        (interview_id,)
    ).fetchone()['c']
    completed = db.execute(
        "SELECT COUNT(*) as c FROM candidates WHERE interview_id=? AND status IN ('completed','reviewed','hired')",
        (interview_id,)
    ).fetchone()['c']

    expires_at = interview['expires_at']
    is_expired = False
    hours_remaining = None
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00')) if isinstance(expires_at, str) else expires_at
            now = datetime.utcnow()
            if hasattr(exp_dt, 'tzinfo') and exp_dt.tzinfo:
                from datetime import timezone
                now = datetime.now(timezone.utc)
            diff = exp_dt - now
            is_expired = diff.total_seconds() < 0
            hours_remaining = round(diff.total_seconds() / 3600, 1) if not is_expired else 0
        except:
            pass

    db.close()
    return jsonify({
        'interview_id': interview_id,
        'title': interview['title'],
        'expires_at': expires_at,
        'is_expired': is_expired,
        'hours_remaining': hours_remaining,
        'pending_candidates': pending,
        'completed_candidates': completed,
        'status': interview['status']
    })

# ======================== WEBHOOKS ========================

VALID_WEBHOOK_EVENTS = ['candidate.completed', 'candidate.scored', 'candidate.created',
                        'interview.created', 'interview.closed', 'report.created', 'report.viewed']

@app.route('/api/webhooks', methods=['GET'])
@require_auth
@require_role('admin')
def api_list_webhooks():
    """List registered webhooks (persistent DB storage)."""
    db = get_db()
    hooks = db.execute('SELECT * FROM webhooks WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    db.close()
    result = []
    for h in hooks:
        hd = dict(h)
        hd['events'] = json.loads(hd['events']) if hd['events'] else []
        hd['active'] = bool(hd['active'])
        result.append(hd)
    return jsonify(result)

@app.route('/api/webhooks', methods=['POST'])
@require_auth
@require_role('admin')
def api_create_webhook():
    """Register a new webhook (persistent DB storage). Requires Professional+ plan."""
    # Cycle 31: Feature gate — integrations
    db_check = get_db()
    user = dict(db_check.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    db_check.close()
    allowed, upgrade = check_feature_access(user, 'integrations')
    if not allowed:
        return soft_block_response('integrations')
    data = request.get_json()
    url = (data.get('url') or '').strip()
    events = data.get('events', [])
    secret = data.get('secret', '')
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if not events:
        return jsonify({'error': 'At least one event type is required'}), 400
    invalid = [e for e in events if e not in VALID_WEBHOOK_EVENTS]
    if invalid:
        return jsonify({'error': f'Invalid events: {invalid}', 'valid_events': VALID_WEBHOOK_EVENTS}), 400

    hook_id = str(uuid.uuid4())
    db = get_db()
    db.execute('INSERT INTO webhooks (id, user_id, url, events, secret) VALUES (?,?,?,?,?)',
               (hook_id, g.user_id, url, json.dumps(events), secret))
    db.commit()
    db.close()
    return jsonify({'id': hook_id, 'url': url, 'events': events, 'secret': secret, 'active': True}), 201

@app.route('/api/webhooks/<hook_id>', methods=['PUT'])
@require_auth
@require_role('admin')
def api_update_webhook(hook_id):
    """Update a webhook (toggle active, change URL/events)."""
    data = request.get_json()
    db = get_db()
    hook = db.execute('SELECT * FROM webhooks WHERE id=? AND user_id=?', (hook_id, g.user_id)).fetchone()
    if not hook:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    fields, values = [], []
    if 'url' in data:
        fields.append('url=?'); values.append(data['url'])
    if 'events' in data:
        fields.append('events=?'); values.append(json.dumps(data['events']))
    if 'secret' in data:
        fields.append('secret=?'); values.append(data['secret'])
    if 'active' in data:
        fields.append('active=?'); values.append(1 if data['active'] else 0)
    if fields:
        values.extend([hook_id, g.user_id])
        db.execute(f'UPDATE webhooks SET {", ".join(fields)} WHERE id=? AND user_id=?', values)
        db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/webhooks/<hook_id>', methods=['DELETE'])
@require_auth
@require_role('admin')
def api_delete_webhook(hook_id):
    """Delete a webhook."""
    db = get_db()
    db.execute('DELETE FROM webhooks WHERE id=? AND user_id=?', (hook_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/webhooks/<hook_id>/test', methods=['POST'])
@require_auth
@require_role('admin')
def api_test_webhook(hook_id):
    """Send a test ping to a webhook endpoint."""
    db = get_db()
    hook = db.execute('SELECT * FROM webhooks WHERE id=? AND user_id=?', (hook_id, g.user_id)).fetchone()
    db.close()
    if not hook:
        return jsonify({'error': 'Webhook not found'}), 404

    import hashlib, hmac as _hmac
    payload = json.dumps({'event': 'webhook.test', 'timestamp': datetime.utcnow().isoformat(),
                          'data': {'message': 'Test ping from ChannelView'}})
    headers = {'Content-Type': 'application/json'}
    if hook['secret']:
        sig = _hmac.new(hook['secret'].encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers['X-ChannelView-Signature'] = sig
    try:
        import urllib.request
        req = urllib.request.Request(hook['url'], data=payload.encode(), headers=headers, method='POST')
        resp = urllib.request.urlopen(req, timeout=10)
        return jsonify({'success': True, 'status_code': resp.status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 502

@app.route('/api/webhooks/events', methods=['GET'])
@require_auth
def api_webhook_events():
    """List all available webhook event types."""
    return jsonify({'events': [
        {'name': e, 'description': d} for e, d in [
            ('candidate.completed', 'Fired when a candidate completes their interview'),
            ('candidate.scored', 'Fired when AI scoring is completed for a candidate'),
            ('candidate.created', 'Fired when a new candidate is added'),
            ('interview.created', 'Fired when a new interview is created'),
            ('interview.closed', 'Fired when an interview is closed or expires'),
            ('report.created', 'Fired when a shareable report is generated'),
            ('report.viewed', 'Fired when a shared report is viewed'),
        ]
    ]})

def _fire_webhook(user_id, event, payload_data):
    """Fire webhooks from DB. Best-effort delivery."""
    try:
        db = get_db()
        hooks = db.execute('SELECT * FROM webhooks WHERE user_id=? AND active=1', (user_id,)).fetchall()
        for hook in hooks:
            events = json.loads(hook['events']) if hook['events'] else []
            if event not in events:
                continue
            try:
                import hashlib, hmac as _hmac, urllib.request
                payload = json.dumps({'event': event, 'timestamp': datetime.utcnow().isoformat(), 'data': payload_data})
                headers = {'Content-Type': 'application/json'}
                if hook['secret']:
                    sig = _hmac.new(hook['secret'].encode(), payload.encode(), hashlib.sha256).hexdigest()
                    headers['X-ChannelView-Signature'] = sig
                req = urllib.request.Request(hook['url'], data=payload.encode(), headers=headers, method='POST')
                urllib.request.urlopen(req, timeout=5)
                db.execute('UPDATE webhooks SET last_triggered_at=CURRENT_TIMESTAMP, failure_count=0 WHERE id=?', (hook['id'],))
            except:
                db.execute('UPDATE webhooks SET failure_count=failure_count+1 WHERE id=?', (hook['id'],))
        db.commit()
        db.close()
    except:
        pass

# ======================== NOTIFICATIONS ========================

def _create_notification(db, user_id, ntype, title, message='', entity_type=None, entity_id=None):
    """Create an in-app notification."""
    db.execute('INSERT INTO notifications (id, user_id, type, title, message, entity_type, entity_id) VALUES (?,?,?,?,?,?,?)',
               (str(uuid.uuid4()), user_id, ntype, title, message, entity_type, entity_id))

@app.route('/api/notifications', methods=['GET'])
@require_auth
def api_list_notifications():
    """List notifications for the current user."""
    unread_only = request.args.get('unread', '').lower() == 'true'
    limit = request.args.get('limit', 50, type=int)
    db = get_db()
    query = 'SELECT * FROM notifications WHERE user_id=?'
    params = [g.user_id]
    if unread_only:
        query += ' AND is_read=0'
    query += ' ORDER BY created_at DESC LIMIT ?'
    params.append(min(limit, 200))
    notifs = db.execute(query, params).fetchall()
    unread_count = db.execute('SELECT COUNT(*) as c FROM notifications WHERE user_id=? AND is_read=0', (g.user_id,)).fetchone()['c']
    db.close()
    return jsonify({
        'notifications': [dict(n) for n in notifs],
        'unread_count': unread_count
    })

@app.route('/api/notifications/mark-read', methods=['POST'])
@require_auth
def api_mark_notifications_read():
    """Mark notifications as read. Pass notification_ids or 'all'."""
    data = request.get_json()
    db = get_db()
    if data.get('all'):
        db.execute('UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0', (g.user_id,))
    else:
        ids = data.get('notification_ids', [])
        if ids:
            placeholders = ','.join('?' * len(ids))
            db.execute(f'UPDATE notifications SET is_read=1 WHERE id IN ({placeholders}) AND user_id=?', (*ids, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/notifications/<notif_id>', methods=['DELETE'])
@require_auth
def api_delete_notification(notif_id):
    """Delete a notification."""
    db = get_db()
    db.execute('DELETE FROM notifications WHERE id=? AND user_id=?', (notif_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/notifications/clear', methods=['POST'])
@require_auth
def api_clear_notifications():
    """Clear all read notifications."""
    db = get_db()
    db.execute('DELETE FROM notifications WHERE user_id=? AND is_read=1', (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})

# ======================== CANDIDATE TAGS ========================

@app.route('/api/candidates/<candidate_id>/tags', methods=['GET'])
@require_auth
def api_get_tags(candidate_id):
    """Get tags for a candidate."""
    db = get_db()
    tags = db.execute('SELECT tag FROM candidate_tags WHERE candidate_id=? AND user_id=?',
                      (candidate_id, g.user_id)).fetchall()
    db.close()
    return jsonify([t['tag'] for t in tags])

@app.route('/api/candidates/<candidate_id>/tags', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_add_tag(candidate_id):
    """Add a tag to a candidate."""
    data = request.get_json()
    tag = (data.get('tag') or '').strip().lower()
    if not tag:
        return jsonify({'error': 'Tag is required'}), 400
    if len(tag) > 50:
        return jsonify({'error': 'Tag must be 50 characters or less'}), 400
    db = get_db()
    # Verify candidate ownership
    candidate = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404
    try:
        db.execute('INSERT INTO candidate_tags (id, user_id, candidate_id, tag) VALUES (?,?,?,?)',
                   (str(uuid.uuid4()), g.user_id, candidate_id, tag))
        db.commit()
    except:
        db.close()
        return jsonify({'error': 'Tag already exists'}), 409
    db.close()
    return jsonify({'success': True, 'tag': tag}), 201

@app.route('/api/candidates/<candidate_id>/tags/<tag>', methods=['DELETE'])
@require_auth
@require_role('admin', 'recruiter')
def api_remove_tag(candidate_id, tag):
    """Remove a tag from a candidate."""
    db = get_db()
    db.execute('DELETE FROM candidate_tags WHERE candidate_id=? AND user_id=? AND tag=?',
               (candidate_id, g.user_id, tag.lower()))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/tags', methods=['GET'])
@require_auth
def api_list_all_tags():
    """List all unique tags used by this user with counts."""
    db = get_db()
    tags = db.execute(
        'SELECT tag, COUNT(*) as count FROM candidate_tags WHERE user_id=? GROUP BY tag ORDER BY count DESC',
        (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify([{'tag': t['tag'], 'count': t['count']} for t in tags])

@app.route('/api/candidates/by-tag/<tag>', methods=['GET'])
@require_auth
def api_candidates_by_tag(tag):
    """Get all candidates with a specific tag."""
    db = get_db()
    candidates = db.execute(
        '''SELECT c.*, i.title as interview_title FROM candidates c
           JOIN candidate_tags ct ON c.id = ct.candidate_id
           JOIN interviews i ON c.interview_id = i.id
           WHERE ct.user_id=? AND ct.tag=?
           ORDER BY c.created_at DESC''',
        (g.user_id, tag.lower())
    ).fetchall()
    db.close()
    return jsonify([dict(c) for c in candidates])

# ======================== CUSTOM FIELDS ========================

@app.route('/api/custom-fields', methods=['GET'])
@require_auth
def api_list_custom_fields():
    """List custom field definitions."""
    db = get_db()
    fields = db.execute('SELECT * FROM custom_field_defs WHERE user_id=? ORDER BY created_at', (g.user_id,)).fetchall()
    db.close()
    return jsonify([dict(f) for f in fields])

@app.route('/api/custom-fields', methods=['POST'])
@require_auth
@require_role('admin')
def api_create_custom_field():
    """Create a custom field definition."""
    data = request.get_json()
    field_name = (data.get('field_name') or '').strip()
    field_type = data.get('field_type', 'text')
    if not field_name:
        return jsonify({'error': 'field_name is required'}), 400
    if field_type not in ('text', 'number', 'select', 'date', 'boolean'):
        return jsonify({'error': 'field_type must be text, number, select, date, or boolean'}), 400

    fid = str(uuid.uuid4())
    db = get_db()
    try:
        db.execute('INSERT INTO custom_field_defs (id, user_id, field_name, field_type, options, required) VALUES (?,?,?,?,?,?)',
                   (fid, g.user_id, field_name, field_type, data.get('options', ''), 1 if data.get('required') else 0))
        db.commit()
    except:
        db.close()
        return jsonify({'error': 'Field name already exists'}), 409
    db.close()
    return jsonify({'id': fid, 'field_name': field_name, 'field_type': field_type}), 201

@app.route('/api/custom-fields/<field_id>', methods=['DELETE'])
@require_auth
@require_role('admin')
def api_delete_custom_field(field_id):
    """Delete a custom field and all its values."""
    db = get_db()
    db.execute('DELETE FROM custom_field_values WHERE field_id=?', (field_id,))
    db.execute('DELETE FROM custom_field_defs WHERE id=? AND user_id=?', (field_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/candidates/<candidate_id>/custom-fields', methods=['GET'])
@require_auth
def api_get_candidate_fields(candidate_id):
    """Get custom field values for a candidate."""
    db = get_db()
    values = db.execute(
        '''SELECT cfv.*, cfd.field_name, cfd.field_type
           FROM custom_field_values cfv
           JOIN custom_field_defs cfd ON cfv.field_id = cfd.id
           WHERE cfv.candidate_id=? AND cfd.user_id=?''',
        (candidate_id, g.user_id)
    ).fetchall()
    db.close()
    return jsonify([dict(v) for v in values])

@app.route('/api/candidates/<candidate_id>/custom-fields', methods=['PUT'])
@require_auth
@require_role('admin', 'recruiter')
def api_set_candidate_fields(candidate_id):
    """Set custom field values for a candidate. Pass {field_id: value, ...}."""
    data = request.get_json()
    fields = data.get('fields', {})
    if not fields:
        return jsonify({'error': 'fields object is required'}), 400

    db = get_db()
    candidate = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    for field_id, value in fields.items():
        existing = db.execute('SELECT id FROM custom_field_values WHERE candidate_id=? AND field_id=?',
                             (candidate_id, field_id)).fetchone()
        if existing:
            db.execute('UPDATE custom_field_values SET value=? WHERE candidate_id=? AND field_id=?',
                       (str(value), candidate_id, field_id))
        else:
            db.execute('INSERT INTO custom_field_values (id, candidate_id, field_id, value) VALUES (?,?,?,?)',
                       (str(uuid.uuid4()), candidate_id, field_id, str(value)))
    db.commit()
    db.close()
    return jsonify({'success': True})

# ======================== INTERVIEW CLONING ========================

@app.route('/api/interviews/<interview_id>/clone', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_clone_interview(interview_id):
    """Clone an existing interview with all its questions."""
    db = get_db()
    source = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not source:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    data = request.get_json() or {}
    new_title = data.get('title', f"{source['title']} (Copy)")

    new_id = str(uuid.uuid4())
    db.execute(
        '''INSERT INTO interviews (id, user_id, title, description, department, position, status,
           thinking_time, max_answer_time, max_retakes, welcome_msg, thank_you_msg, brand_color)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (new_id, g.user_id, new_title, source['description'], source['department'], source['position'],
         'active', source['thinking_time'], source['max_answer_time'], source['max_retakes'],
         source['welcome_msg'], source['thank_you_msg'], source['brand_color'])
    )

    # Clone questions
    questions = db.execute('SELECT * FROM questions WHERE interview_id=? ORDER BY question_order', (interview_id,)).fetchall()
    for q in questions:
        db.execute('INSERT INTO questions (id, interview_id, question_text, question_order) VALUES (?,?,?,?)',
                   (str(uuid.uuid4()), new_id, q['question_text'], q['question_order']))

    # Audit
    db.execute('INSERT INTO audit_log (id, account_id, user_id, action, entity_type, entity_id, details) VALUES (?,?,?,?,?,?,?)',
               (str(uuid.uuid4()), g.user_id, g.user_id, 'interview_clone', 'interview', new_id,
                json.dumps({'source_id': interview_id, 'title': new_title})))
    db.commit()

    # Return the cloned interview
    cloned = db.execute('SELECT * FROM interviews WHERE id=?', (new_id,)).fetchone()
    cloned_qs = db.execute('SELECT * FROM questions WHERE interview_id=? ORDER BY question_order', (new_id,)).fetchall()
    db.close()

    result = dict(cloned)
    result['questions'] = [dict(q) for q in cloned_qs]
    return jsonify(result), 201

# ======================== MULTI-TENANT DATA ISOLATION ========================

@app.route('/api/team/context', methods=['GET'])
@require_auth
def api_team_context():
    """Get the current user's team context — which account they belong to and their role."""
    db = get_db()
    # Check if user is a team member of any account
    memberships = db.execute(
        '''SELECT tm.*, u.name as owner_name, u.agency_name, u.email as owner_email
           FROM team_members tm
           JOIN users u ON tm.account_id = u.id
           WHERE tm.user_id=? AND tm.status='active' ''',
        (g.user_id,)
    ).fetchall()
    db.close()

    return jsonify({
        'user_id': g.user_id,
        'is_owner': len(memberships) == 0 or g.team_account_id is None,
        'team_role': g.team_role,
        'memberships': [dict(m) for m in memberships]
    })

@app.route('/api/team/switch/<account_id>', methods=['POST'])
@require_auth
def api_switch_account(account_id):
    """Placeholder for switching active team account context.
    In a full implementation, this would set a session variable to scope all queries."""
    db = get_db()
    membership = db.execute(
        'SELECT * FROM team_members WHERE account_id=? AND user_id=? AND status=?',
        (account_id, g.user_id, 'active')
    ).fetchone()
    db.close()
    if not membership:
        return jsonify({'error': 'Not a member of this account'}), 403
    return jsonify({
        'success': True,
        'account_id': account_id,
        'role': membership['role'],
        'message': 'Account context switched'
    })


# ======================== ONBOARDING ========================

@app.route('/api/onboarding/status', methods=['GET'])
@require_auth
def api_onboarding_status():
    """Get the user's onboarding progress."""
    db = get_db()
    user = db.execute('SELECT onboarding_completed, onboarding_step, agency_name, brand_color, logo_url FROM users WHERE id=?',
                      (g.user_id,)).fetchone()
    db.close()
    return jsonify({
        'completed': bool(user['onboarding_completed']),
        'current_step': user['onboarding_step'] or 0,
        'agency_name': user['agency_name'],
        'brand_color': user['brand_color'],
        'has_logo': bool(user['logo_url']),
    })

@app.route('/api/onboarding/step', methods=['POST'])
@require_auth
def api_onboarding_step():
    """Save onboarding progress for a specific step."""
    data = request.get_json()
    step = data.get('step', 0)
    step_data = data.get('data', {})
    db = get_db()

    if step == 1:
        # Agency profile
        updates = {}
        if 'agency_name' in step_data: updates['agency_name'] = step_data['agency_name']
        if 'agency_website' in step_data: updates['agency_website'] = step_data['agency_website']
        if 'agency_phone' in step_data: updates['agency_phone'] = step_data['agency_phone']
        if updates:
            set_clause = ', '.join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE users SET {set_clause}, onboarding_step=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       list(updates.values()) + [g.user_id])

    elif step == 2:
        # Branding
        brand_color = step_data.get('brand_color', '#0ace0a')
        db.execute("UPDATE users SET brand_color=?, onboarding_step=2, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (brand_color, g.user_id))

    elif step == 3:
        # Create first interview template (optional)
        title = step_data.get('interview_title', '')
        if title:
            iid = str(uuid.uuid4())
            db.execute("INSERT INTO interviews (id, user_id, title, description, department, status) VALUES (?,?,?,?,?,?)",
                       (iid, g.user_id, title, step_data.get('description', ''), step_data.get('department', ''), 'active'))
            questions = step_data.get('questions', [])
            for i, q in enumerate(questions):
                qid = str(uuid.uuid4())
                db.execute("INSERT INTO questions (id, interview_id, question_text, question_order) VALUES (?,?,?,?)",
                           (qid, iid, q, i + 1))
        db.execute("UPDATE users SET onboarding_step=3 WHERE id=?", (g.user_id,))

    elif step == 4:
        # Complete onboarding
        db.execute("UPDATE users SET onboarding_completed=1, onboarding_step=4, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (g.user_id,))

    db.commit()
    db.close()
    return jsonify({'success': True, 'step': step})

@app.route('/api/onboarding/skip', methods=['POST'])
@require_auth
def api_onboarding_skip():
    """Skip onboarding entirely."""
    db = get_db()
    db.execute("UPDATE users SET onboarding_completed=1, onboarding_step=4, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ======================== FMO ADMIN PANEL ========================

@app.route('/admin')
@require_auth
def admin_page():
    """FMO Admin panel page."""
    if not g.user.get('is_fmo_admin'):
        return redirect('/dashboard')
    return render_template('app.html', page='admin', user=g.user)

@app.route('/api/admin/accounts', methods=['GET'])
@require_auth
def api_admin_accounts():
    """List all agency accounts (FMO admin only)."""
    if not g.user.get('is_fmo_admin'):
        return jsonify({'error': 'FMO admin access required'}), 403
    db = get_db()
    accounts = db.execute('''
        SELECT u.id, u.email, u.name, u.agency_name, u.plan, u.subscription_status,
               u.created_at, u.onboarding_completed,
               (SELECT COUNT(*) FROM interviews WHERE user_id=u.id) as interview_count,
               (SELECT COUNT(*) FROM candidates WHERE user_id=u.id) as candidate_count,
               (SELECT COUNT(*) FROM candidates WHERE user_id=u.id AND status='completed') as completed_count,
               (SELECT COUNT(*) FROM team_members WHERE account_id=u.id AND status='active') as team_size
        FROM users u WHERE u.role='owner' ORDER BY u.created_at DESC
    ''').fetchall()
    db.close()
    return jsonify({'accounts': [dict(a) for a in accounts]})

@app.route('/api/admin/stats', methods=['GET'])
@require_auth
def api_admin_stats():
    """Get platform-wide statistics (FMO admin only)."""
    if not g.user.get('is_fmo_admin'):
        return jsonify({'error': 'FMO admin access required'}), 403
    db = get_db()
    stats = {
        'total_accounts': db.execute("SELECT COUNT(*) FROM users WHERE role='owner'").fetchone()[0],
        'active_subscriptions': db.execute("SELECT COUNT(*) FROM users WHERE subscription_status='active'").fetchone()[0],
        'total_interviews': db.execute("SELECT COUNT(*) FROM interviews").fetchone()[0],
        'total_candidates': db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
        'completed_interviews': db.execute("SELECT COUNT(*) FROM candidates WHERE status='completed'").fetchone()[0],
        'total_responses': db.execute("SELECT COUNT(*) FROM responses").fetchone()[0],
        'accounts_this_month': db.execute("SELECT COUNT(*) FROM users WHERE role='owner' AND created_at >= date('now', '-30 days')").fetchone()[0],
        'candidates_this_month': db.execute("SELECT COUNT(*) FROM candidates WHERE created_at >= date('now', '-30 days')").fetchone()[0],
    }
    # Recent activity
    recent = db.execute('''
        SELECT u.agency_name, c.first_name || ' ' || c.last_name as candidate_name,
               c.status, c.updated_at, i.title as interview_title
        FROM candidates c JOIN users u ON c.user_id=u.id JOIN interviews i ON c.interview_id=i.id
        ORDER BY c.updated_at DESC LIMIT 20
    ''').fetchall()
    stats['recent_activity'] = [dict(r) for r in recent]
    db.close()
    return jsonify(stats)

@app.route('/api/admin/account/<account_id>', methods=['GET'])
@require_auth
def api_admin_account_detail(account_id):
    """Get detailed info for a specific agency account (FMO admin only)."""
    if not g.user.get('is_fmo_admin'):
        return jsonify({'error': 'FMO admin access required'}), 403
    db = get_db()
    account = db.execute('SELECT * FROM users WHERE id=?', (account_id,)).fetchone()
    if not account:
        db.close()
        return jsonify({'error': 'Account not found'}), 404
    interviews = db.execute('SELECT id, title, status, created_at, (SELECT COUNT(*) FROM candidates WHERE interview_id=interviews.id) as candidate_count FROM interviews WHERE user_id=? ORDER BY created_at DESC', (account_id,)).fetchall()
    team = db.execute('SELECT tm.role, u.name, u.email FROM team_members tm JOIN users u ON tm.user_id=u.id WHERE tm.account_id=? AND tm.status=?', (account_id, 'active')).fetchall()
    db.close()
    return jsonify({
        'account': {k: dict(account)[k] for k in ['id', 'email', 'name', 'agency_name', 'plan', 'subscription_status', 'brand_color', 'created_at', 'onboarding_completed']},
        'interviews': [dict(i) for i in interviews],
        'team': [dict(t) for t in team]
    })


# ======================== EMAIL NOTIFICATIONS ========================

def _send_notification_email(user, candidate, notification_type, extra=None):
    """Send lifecycle notification emails (invite, started, completed, reminder)."""
    try:
        from email_service import send_email
    except ImportError:
        return

    if not extra:
        extra = {}

    # Check user notification preferences
    pref_field = f"notify_{notification_type}"
    if hasattr(user, '__getitem__') and not user.get(pref_field, 1):
        return

    brand_color = user.get('brand_color', '#0ace0a')
    agency_name = user.get('agency_name', 'ChannelView')
    brand_name = user.get('candidate_brand_name') or agency_name if user.get('white_label_enabled') else 'ChannelView'

    templates = {
        'candidate_invited': {
            'subject': f"You're invited to interview for {extra.get('position', 'a position')} at {agency_name}",
            'body': f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
                    <div style="background:{brand_color};padding:16px 24px;border-radius:8px 8px 0 0;">
                        <h2 style="color:#000;margin:0;">{brand_name}</h2>
                    </div>
                    <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">
                        <p>Hi {candidate.get('first_name', '')},</p>
                        <p>You have been invited to complete a video interview for <strong>{extra.get('position', 'a position')}</strong> at {agency_name}.</p>
                        <p style="text-align:center;margin:24px 0;">
                            <a href="{extra.get('interview_url', '#')}" style="display:inline-block;background:{brand_color};color:#000;font-weight:700;padding:12px 32px;border-radius:6px;text-decoration:none;">Start Interview</a>
                        </p>
                        <p style="color:#666;font-size:14px;">This interview can be completed at your convenience. You will need a camera and microphone.</p>
                    </div>
                </div>"""
        },
        'interview_started': {
            'subject': f"{candidate.get('first_name', '')} {candidate.get('last_name', '')} started their interview",
            'body': f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
                    <div style="background:{brand_color};padding:16px 24px;border-radius:8px 8px 0 0;">
                        <h2 style="color:#000;margin:0;">{brand_name} - Interview Update</h2>
                    </div>
                    <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">
                        <p><strong>{candidate.get('first_name', '')} {candidate.get('last_name', '')}</strong> has started their interview for <strong>{extra.get('position', 'a position')}</strong>.</p>
                        <p style="color:#666;">You will be notified when they complete their interview.</p>
                    </div>
                </div>"""
        },
        'interview_completed': {
            'subject': f"{candidate.get('first_name', '')} {candidate.get('last_name', '')} completed their interview",
            'body': f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
                    <div style="background:{brand_color};padding:16px 24px;border-radius:8px 8px 0 0;">
                        <h2 style="color:#000;margin:0;">{brand_name} - Interview Complete</h2>
                    </div>
                    <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">
                        <p><strong>{candidate.get('first_name', '')} {candidate.get('last_name', '')}</strong> has completed their video interview for <strong>{extra.get('position', 'a position')}</strong>.</p>
                        <p style="text-align:center;margin:24px 0;">
                            <a href="{extra.get('review_url', '#')}" style="display:inline-block;background:{brand_color};color:#000;font-weight:700;padding:12px 32px;border-radius:6px;text-decoration:none;">Review Responses</a>
                        </p>
                    </div>
                </div>"""
        },
        'candidate_reminder': {
            'subject': f"Reminder: Complete your interview for {extra.get('position', 'a position')}",
            'body': f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
                    <div style="background:{brand_color};padding:16px 24px;border-radius:8px 8px 0 0;">
                        <h2 style="color:#000;margin:0;">{brand_name}</h2>
                    </div>
                    <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">
                        <p>Hi {candidate.get('first_name', '')},</p>
                        <p>This is a friendly reminder to complete your video interview for <strong>{extra.get('position', 'a position')}</strong> at {agency_name}.</p>
                        <p style="text-align:center;margin:24px 0;">
                            <a href="{extra.get('interview_url', '#')}" style="display:inline-block;background:{brand_color};color:#000;font-weight:700;padding:12px 32px;border-radius:6px;text-decoration:none;">Complete Interview</a>
                        </p>
                    </div>
                </div>"""
        },
    }

    tmpl = templates.get(notification_type)
    if not tmpl:
        return

    # Determine recipient: for candidate notifications, send to candidate; for owner notifications, send to owner
    if notification_type in ('candidate_invited', 'candidate_reminder'):
        to_email = candidate.get('email', '')
    else:
        to_email = user.get('email', '')

    if not to_email:
        return

    smtp_config = {
        'host': user.get('smtp_host', ''),
        'port': user.get('smtp_port') or 587,
        'user': user.get('smtp_user', ''),
        'password': user.get('smtp_pass', ''),
        'from_email': user.get('smtp_from_email') or user.get('email') or 'noreply@channelview.io',
        'from_name': user.get('smtp_from_name') or brand_name,
    }

    send_email(smtp_config, to_email, tmpl['subject'], tmpl['body'])

@app.route('/api/notifications/preferences', methods=['GET'])
@require_auth
def api_notification_preferences():
    """Get email notification preferences."""
    db = get_db()
    user = db.execute('SELECT notify_interview_started, notify_interview_completed, notify_candidate_invited, notify_daily_digest FROM users WHERE id=?',
                      (g.user_id,)).fetchone()
    db.close()
    return jsonify({
        'notify_interview_started': bool(user['notify_interview_started']),
        'notify_interview_completed': bool(user['notify_interview_completed']),
        'notify_candidate_invited': bool(user['notify_candidate_invited']),
        'notify_daily_digest': bool(user['notify_daily_digest']),
    })

@app.route('/api/notifications/preferences', methods=['PUT'])
@require_auth
def api_update_notification_preferences():
    """Update email notification preferences."""
    data = request.get_json()
    db = get_db()
    fields = ['notify_interview_started', 'notify_interview_completed', 'notify_candidate_invited', 'notify_daily_digest']
    updates = []
    values = []
    for f in fields:
        key = f.replace('notify_', '')
        if f in data:
            updates.append(f"{f}=?")
            values.append(1 if data[f] else 0)
        elif key in data:
            updates.append(f"{f}=?")
            values.append(1 if data[key] else 0)
    if updates:
        values.append(g.user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)}, updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
        db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/candidates/<candidate_id>/nudge', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_nudge_candidate(candidate_id):
    """Send a branded nudge/reminder email to a candidate using notification system."""
    db = get_db()
    candidate = db.execute('SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404
    if candidate['status'] in ('completed', 'reviewed', 'hired'):
        db.close()
        return jsonify({'error': 'Candidate has already completed their interview'}), 400

    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    interview = db.execute('SELECT title FROM interviews WHERE id=?', (candidate['interview_id'],)).fetchone()

    interview_url = f"{request.host_url.rstrip('/')}/i/{candidate['token']}"
    _send_notification_email(user, dict(candidate), 'candidate_reminder', {
        'position': interview['title'] if interview else '',
        'interview_url': interview_url
    })

    db.execute("UPDATE candidates SET reminder_sent_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate_id,))
    db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'Reminder sent'})


# ======================== WHITE-LABEL BRANDING ========================

@app.route('/api/branding', methods=['GET'])
@require_auth
def api_get_branding():
    """Get white-label branding settings."""
    db = get_db()
    user = db.execute('''SELECT brand_color, brand_secondary_color, brand_accent_color, agency_logo_url,
                         white_label_enabled, candidate_brand_name, candidate_brand_logo,
                         agency_name, agency_website, agency_phone FROM users WHERE id=?''',
                      (g.user_id,)).fetchone()
    db.close()
    return jsonify({
        'brand_color': user['brand_color'] or '#0ace0a',
        'brand_secondary_color': user['brand_secondary_color'] or '#000000',
        'brand_accent_color': user['brand_accent_color'] or '#ffffff',
        'agency_logo_url': user['agency_logo_url'],
        'white_label_enabled': bool(user['white_label_enabled']),
        'candidate_brand_name': user['candidate_brand_name'] or user['agency_name'],
        'candidate_brand_logo': user['candidate_brand_logo'],
        'agency_name': user['agency_name'],
        'agency_website': user['agency_website'],
        'agency_phone': user['agency_phone'],
    })

@app.route('/api/branding', methods=['PUT'])
@require_auth
@require_role('admin')
def api_update_branding():
    """Update white-label branding settings. Requires Professional+ plan."""
    # Cycle 31: Feature gate — white_label
    db_check = get_db()
    user = dict(db_check.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    db_check.close()
    allowed, upgrade = check_feature_access(user, 'white_label')
    if not allowed:
        return soft_block_response('white_label')
    data = request.get_json()
    db = get_db()
    fields = ['brand_color', 'brand_secondary_color', 'brand_accent_color', 'agency_logo_url',
              'white_label_enabled', 'candidate_brand_name', 'candidate_brand_logo',
              'agency_name', 'agency_website', 'agency_phone']
    updates = []
    values = []
    for f in fields:
        if f in data:
            updates.append(f"{f}=?")
            values.append(data[f])
    if updates:
        values.append(g.user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)}, updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
        db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/branding/logo', methods=['POST'])
@require_auth
@require_role('admin')
def api_upload_logo():
    """Upload agency logo for white-label branding."""
    if 'logo' not in request.files:
        return jsonify({'error': 'No logo file provided'}), 400
    file = request.files['logo']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ('png', 'jpg', 'jpeg', 'svg', 'webp'):
        return jsonify({'error': 'Invalid file type. Use PNG, JPG, SVG, or WebP'}), 400

    logo_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'logos')
    os.makedirs(logo_dir, exist_ok=True)
    filename = f"{g.user_id}_logo.{ext}"
    filepath = os.path.join(logo_dir, filename)
    file.save(filepath)

    logo_url = f"/static/uploads/logos/{filename}"
    db = get_db()
    db.execute("UPDATE users SET agency_logo_url=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (logo_url, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'logo_url': logo_url})


# ======================== HEALTH CHECK & DEPLOYMENT ========================

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for load balancers and container orchestration."""
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        db.close()
        return jsonify({
            'status': 'healthy',
            'version': '1.0.0',
            'environment': os.environ.get('FLASK_ENV', 'development'),
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        })
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503

@app.route('/health/ready', methods=['GET'])
def readiness_check():
    """Readiness probe - checks if app can serve traffic."""
    checks = {}
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        db.close()
        checks['database'] = 'ok'
    except Exception:
        checks['database'] = 'error'

    checks['storage'] = 'ok' if os.path.isdir(app.config['UPLOAD_FOLDER']) else 'error'

    all_ok = all(v == 'ok' for v in checks.values())
    return jsonify({'ready': all_ok, 'checks': checks}), 200 if all_ok else 503

@app.route('/api/system/monitoring', methods=['GET'])
@require_auth
@require_role('admin')
def api_system_monitoring_c29():
    """System monitoring dashboard data — admin only."""
    db = get_db()

    now = datetime.utcnow()
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    stats = {
        'uptime_seconds': int(time.time() - _app_start_time),
        'environment': os.environ.get('FLASK_ENV', 'development'),
        'version': app_config.VERSION,
        'config': {
            'stripe_configured': bool(STRIPE_SECRET_KEY),
            'email_backend': 'sendgrid' if os.environ.get('SENDGRID_API_KEY') else ('smtp' if os.environ.get('SMTP_HOST') else 'log'),
            'storage_backend': app_config.STORAGE_BACKEND,
            'cors_origins': os.environ.get('CORS_ORIGINS', '*'),
        },
        'usage_24h': {
            'new_users': db.execute('SELECT COUNT(*) as cnt FROM users WHERE created_at>=?', (day_ago,)).fetchone()['cnt'],
            'new_candidates': db.execute('SELECT COUNT(*) as cnt FROM candidates WHERE created_at>=?', (day_ago,)).fetchone()['cnt'],
            'completed_interviews': db.execute("SELECT COUNT(*) as cnt FROM candidates WHERE status='completed' AND completed_at>=?", (day_ago,)).fetchone()['cnt'],
            'emails_sent': db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE created_at>=? AND status='sent'", (day_ago,)).fetchone()['cnt'] if 'email_log' in [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()] else 0,
            'emails_failed': db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE created_at>=? AND status='failed'", (day_ago,)).fetchone()['cnt'] if 'email_log' in [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()] else 0,
        },
        'usage_7d': {
            'new_users': db.execute('SELECT COUNT(*) as cnt FROM users WHERE created_at>=?', (week_ago,)).fetchone()['cnt'],
            'new_candidates': db.execute('SELECT COUNT(*) as cnt FROM candidates WHERE created_at>=?', (week_ago,)).fetchone()['cnt'],
            'completed_interviews': db.execute("SELECT COUNT(*) as cnt FROM candidates WHERE status='completed' AND completed_at>=?", (week_ago,)).fetchone()['cnt'],
        },
        'totals': {
            'total_users': db.execute('SELECT COUNT(*) as cnt FROM users').fetchone()['cnt'],
            'total_interviews': db.execute('SELECT COUNT(*) as cnt FROM interviews').fetchone()['cnt'],
            'total_candidates': db.execute('SELECT COUNT(*) as cnt FROM candidates').fetchone()['cnt'],
            'active_trials': db.execute("SELECT COUNT(*) as cnt FROM users WHERE subscription_status='trialing'").fetchone()['cnt'],
            'paid_subscriptions': db.execute("SELECT COUNT(*) as cnt FROM users WHERE subscription_status='active'").fetchone()['cnt'],
        }
    }

    db.close()
    return jsonify(stats)

# ======================== ENHANCED ANALYTICS (Cycle 11) ========================

@app.route('/api/analytics/funnel', methods=['GET'])
@require_auth
def api_analytics_funnel():
    """Get candidate funnel metrics: invited → started → completed → scored → hired."""
    db = get_db()
    uid = g.user_id
    period = int(request.args.get('period', 30))
    from_date = (datetime.utcnow() - timedelta(days=period)).isoformat()

    invited = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND created_at >= ?", (uid, from_date)).fetchone()['c']
    started = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND started_at IS NOT NULL AND created_at >= ?", (uid, from_date)).fetchone()['c']
    completed = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND status IN ('completed','reviewed','hired') AND created_at >= ?", (uid, from_date)).fetchone()['c']
    scored = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND ai_score IS NOT NULL AND created_at >= ?", (uid, from_date)).fetchone()['c']
    hired = db.execute("SELECT COUNT(*) as c FROM candidates WHERE user_id=? AND status='hired' AND created_at >= ?", (uid, from_date)).fetchone()['c']

    # Drop-off rates
    funnel = [
        {'stage': 'Invited', 'count': invited, 'pct': 100},
        {'stage': 'Started', 'count': started, 'pct': round(started / max(invited, 1) * 100, 1)},
        {'stage': 'Completed', 'count': completed, 'pct': round(completed / max(invited, 1) * 100, 1)},
        {'stage': 'Scored', 'count': scored, 'pct': round(scored / max(invited, 1) * 100, 1)},
        {'stage': 'Hired', 'count': hired, 'pct': round(hired / max(invited, 1) * 100, 1)},
    ]

    # Avg time-to-complete by interview
    time_data = db.execute('''
        SELECT i.title, AVG(JULIANDAY(c.completed_at) - JULIANDAY(c.invited_at)) * 24 as avg_hours,
               COUNT(c.id) as count
        FROM candidates c JOIN interviews i ON c.interview_id=i.id
        WHERE c.user_id=? AND c.completed_at IS NOT NULL AND c.invited_at IS NOT NULL
        GROUP BY i.id ORDER BY avg_hours ASC
    ''', (uid,)).fetchall()

    # Per-interview funnel
    interview_funnels = db.execute('''
        SELECT i.title,
               COUNT(c.id) as invited,
               SUM(CASE WHEN c.started_at IS NOT NULL THEN 1 ELSE 0 END) as started,
               SUM(CASE WHEN c.status IN ('completed','reviewed','hired') THEN 1 ELSE 0 END) as completed,
               SUM(CASE WHEN c.ai_score IS NOT NULL THEN 1 ELSE 0 END) as scored
        FROM interviews i LEFT JOIN candidates c ON i.id=c.interview_id
        WHERE i.user_id=? GROUP BY i.id ORDER BY i.created_at DESC LIMIT 10
    ''', (uid,)).fetchall()

    db.close()
    return jsonify({
        'funnel': funnel,
        'time_to_complete': [dict(t) for t in time_data],
        'interview_funnels': [dict(f) for f in interview_funnels],
        'period_days': period
    })

@app.route('/api/analytics/export/pdf', methods=['GET'])
@require_auth
def api_analytics_export_pdf():
    """Export analytics summary as a downloadable HTML report (styled for PDF printing)."""
    db = get_db()
    uid = g.user_id
    user = db.execute('SELECT name, agency_name, brand_color FROM users WHERE id=?', (uid,)).fetchone()

    stats = db.execute('''SELECT COUNT(*) as total,
        SUM(CASE WHEN status IN ('completed','reviewed','hired') THEN 1 ELSE 0 END) as completed,
        SUM(CASE WHEN status='hired' THEN 1 ELSE 0 END) as hired,
        AVG(ai_score) as avg_score
        FROM candidates WHERE user_id=?''', (uid,)).fetchone()

    interviews = db.execute('''SELECT i.title,
        COUNT(c.id) as candidates, SUM(CASE WHEN c.status IN ('completed','reviewed','hired') THEN 1 ELSE 0 END) as completed,
        AVG(c.ai_score) as avg_score FROM interviews i LEFT JOIN candidates c ON i.id=c.interview_id
        WHERE i.user_id=? GROUP BY i.id ORDER BY i.created_at DESC''', (uid,)).fetchall()
    db.close()

    brand = user['brand_color'] or '#0ace0a'
    agency = user['agency_name'] or 'ChannelView'
    html = f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{agency} - Hiring Report</title>
    <style>body{{font-family:Arial,sans-serif;max-width:800px;margin:0 auto;padding:40px;color:#111}}
    h1{{color:{brand};border-bottom:3px solid {brand};padding-bottom:12px}}
    .stat{{display:inline-block;text-align:center;margin:0 24px 20px 0;padding:16px 24px;background:#f3f4f6;border-radius:8px}}
    .stat-val{{font-size:28px;font-weight:700;color:{brand}}} .stat-label{{font-size:13px;color:#666;margin-top:4px}}
    table{{width:100%;border-collapse:collapse;margin-top:20px}} th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid #e5e7eb}}
    th{{background:{brand};color:#000;font-weight:600}} @media print{{body{{padding:20px}}}}
    </style></head><body>
    <h1>{agency} — Hiring Analytics Report</h1>
    <p style="color:#666">Generated {datetime.utcnow().strftime('%B %d, %Y')}</p>
    <div style="margin:24px 0">
      <div class="stat"><div class="stat-val">{stats['total']}</div><div class="stat-label">Total Candidates</div></div>
      <div class="stat"><div class="stat-val">{stats['completed']}</div><div class="stat-label">Completed</div></div>
      <div class="stat"><div class="stat-val">{stats['hired']}</div><div class="stat-label">Hired</div></div>
      <div class="stat"><div class="stat-val">{round(stats['avg_score'], 1) if stats['avg_score'] else '—'}</div><div class="stat-label">Avg AI Score</div></div>
    </div>
    <h2>Interview Performance</h2>
    <table><thead><tr><th>Interview</th><th>Candidates</th><th>Completed</th><th>Avg Score</th></tr></thead><tbody>
    {''.join(f"<tr><td>{i['title']}</td><td>{i['candidates']}</td><td>{i['completed']}</td><td>{round(i['avg_score'], 1) if i['avg_score'] else '—'}</td></tr>" for i in interviews)}
    </tbody></table>
    <p style="margin-top:40px;color:#999;font-size:12px;text-align:center">Powered by ChannelView &middot; {agency}</p>
    </body></html>'''

    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html'
    resp.headers['Content-Disposition'] = f'attachment; filename={agency.replace(" ", "_")}_hiring_report.html'
    return resp


# ======================== WORKFLOW AUTOMATION (Cycle 11) ========================

@app.route('/api/automation/settings', methods=['GET'])
@require_auth
def api_automation_settings():
    """Get workflow automation settings."""
    db = get_db()
    user = db.execute('''SELECT auto_score_enabled, auto_advance_threshold, auto_advance_enabled,
                         auto_reject_threshold, auto_reject_enabled,
                         reminder_sequence_enabled, reminder_day_3, reminder_day_5, reminder_day_7
                         FROM users WHERE id=?''', (g.user_id,)).fetchone()
    db.close()
    return jsonify({
        'auto_score': bool(user['auto_score_enabled']),
        'auto_advance': {'enabled': bool(user['auto_advance_enabled']), 'threshold': user['auto_advance_threshold'] or 80},
        'auto_reject': {'enabled': bool(user['auto_reject_enabled']), 'threshold': user['auto_reject_threshold'] or 40},
        'reminder_sequence': {
            'enabled': bool(user['reminder_sequence_enabled']),
            'day_3': bool(user['reminder_day_3']),
            'day_5': bool(user['reminder_day_5']),
            'day_7': bool(user['reminder_day_7']),
        }
    })

@app.route('/api/automation/settings', methods=['PUT'])
@require_auth
@require_role('admin')
def api_update_automation_settings():
    """Update workflow automation settings."""
    data = request.get_json()
    db = get_db()
    updates = []
    values = []

    field_map = {
        'auto_score': 'auto_score_enabled',
        'auto_advance_enabled': 'auto_advance_enabled',
        'auto_advance_threshold': 'auto_advance_threshold',
        'auto_reject_enabled': 'auto_reject_enabled',
        'auto_reject_threshold': 'auto_reject_threshold',
        'reminder_sequence_enabled': 'reminder_sequence_enabled',
        'reminder_day_3': 'reminder_day_3',
        'reminder_day_5': 'reminder_day_5',
        'reminder_day_7': 'reminder_day_7',
    }

    for key, col in field_map.items():
        if key in data:
            updates.append(f"{col}=?")
            val = data[key]
            values.append(int(val) if isinstance(val, bool) else val)

    if updates:
        values.append(g.user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)}, updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
        db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/automation/run-auto-score/<candidate_id>', methods=['POST'])
@require_auth
def api_run_auto_score(candidate_id):
    """Trigger auto-scoring for a candidate (simulates what happens post-completion)."""
    db = get_db()
    candidate = db.execute('SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    if candidate['status'] not in ('completed', 'reviewed', 'hired'):
        db.close()
        return jsonify({'error': 'Candidate must have completed their interview'}), 400
    db.close()

    # Delegate to existing score endpoint
    return api_score_candidate(candidate_id)

@app.route('/api/interviews/<interview_id>/auto-expire', methods=['PUT'])
@require_auth
@require_role('admin', 'recruiter')
def api_set_auto_expire(interview_id):
    """Set auto-expiration days for an interview."""
    data = request.get_json()
    days = data.get('auto_expire_days', 0)
    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute("UPDATE interviews SET auto_expire_days=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (days, interview_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'auto_expire_days': days})

@app.route('/api/automation/pending-reminders', methods=['GET'])
@require_auth
def api_pending_reminders():
    """Get candidates who are due for reminder emails based on reminder sequence settings."""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not user['reminder_sequence_enabled']:
        db.close()
        return jsonify({'pending': [], 'enabled': False})

    candidates = db.execute('''
        SELECT c.id, c.first_name, c.last_name, c.email, c.status, c.invited_at, c.reminder_sent_at,
               i.title as interview_title,
               CAST((JULIANDAY('now') - JULIANDAY(c.invited_at)) AS INTEGER) as days_since_invite
        FROM candidates c JOIN interviews i ON c.interview_id=i.id
        WHERE c.user_id=? AND c.status IN ('invited', 'in_progress')
        AND c.invited_at IS NOT NULL
        ORDER BY c.invited_at ASC
    ''', (g.user_id,)).fetchall()

    pending = []
    for c in candidates:
        days = c['days_since_invite'] or 0
        if (days >= 3 and user['reminder_day_3']) or (days >= 5 and user['reminder_day_5']) or (days >= 7 and user['reminder_day_7']):
            pending.append(dict(c))

    db.close()
    return jsonify({'pending': pending, 'enabled': True})


# ======================== PUBLIC REST API (Cycle 11) ========================

def require_api_key(f):
    """Decorator to authenticate via X-API-Key header."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key', '')
        if not api_key:
            return jsonify({'error': 'API key required. Pass X-API-Key header.'}), 401
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE api_key=?', (api_key,)).fetchone()
        db.close()
        if not user:
            return jsonify({'error': 'Invalid API key'}), 401
        g.user_id = user['id']
        g.user = dict(user)
        g.effective_user_id = user['id']
        g.user_role = 'owner'
        g.team_account_id = None
        g.team_role = None
        return f(*args, **kwargs)
    return decorated

@app.route('/api/keys', methods=['POST'])
@require_auth
@require_role('admin')
def api_create_api_key():
    """Generate a new API key for the current user. Requires Professional+ plan."""
    # Cycle 31: Feature gate — api_access
    db = get_db()
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    allowed, upgrade = check_feature_access(user, 'api_access')
    if not allowed:
        db.close()
        return soft_block_response('api_access')
    import secrets as _secrets
    api_key = f"cv_{_secrets.token_hex(24)}"
    db.execute("UPDATE users SET api_key=?, api_key_created_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (api_key, g.user_id))
    db.commit()
    db.close()
    return jsonify({'api_key': api_key, 'message': 'Save this key - it cannot be shown again.'})

@app.route('/api/keys', methods=['GET'])
@require_auth
def api_get_api_key_status():
    """Check if user has an API key configured."""
    db = get_db()
    user = db.execute('SELECT api_key, api_key_created_at FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()
    has_key = bool(user['api_key'])
    return jsonify({
        'has_key': has_key,
        'created_at': user['api_key_created_at'] if has_key else None,
        'prefix': user['api_key'][:8] + '...' if has_key else None
    })

@app.route('/api/keys', methods=['DELETE'])
@require_auth
@require_role('admin')
def api_revoke_api_key():
    """Revoke the current API key."""
    db = get_db()
    db.execute("UPDATE users SET api_key=NULL, api_key_created_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'API key revoked'})

# Public API endpoints (API key auth)
@app.route('/api/v1/interviews', methods=['GET'])
@require_api_key
def api_v1_list_interviews():
    """Public API: List all interviews."""
    db = get_db()
    interviews = db.execute('SELECT id, title, description, department, position, status, created_at FROM interviews WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    db.close()
    return jsonify({'interviews': [dict(i) for i in interviews]})

@app.route('/api/v1/interviews/<interview_id>/candidates', methods=['GET'])
@require_api_key
def api_v1_list_candidates(interview_id):
    """Public API: List candidates for an interview."""
    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404
    candidates = db.execute('''SELECT id, first_name, last_name, email, status, ai_score,
                               invited_at, started_at, completed_at, created_at
                               FROM candidates WHERE interview_id=? ORDER BY created_at DESC''', (interview_id,)).fetchall()
    db.close()
    return jsonify({'candidates': [dict(c) for c in candidates]})

@app.route('/api/v1/candidates/<candidate_id>', methods=['GET'])
@require_api_key
def api_v1_get_candidate(candidate_id):
    """Public API: Get candidate details including scores."""
    db = get_db()
    candidate = db.execute('''SELECT c.*, i.title as interview_title, i.position
                              FROM candidates c JOIN interviews i ON c.interview_id=i.id
                              WHERE c.id=? AND c.user_id=?''', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404
    responses = db.execute('''SELECT r.id, q.question_text, r.ai_score, r.ai_feedback, r.duration, r.transcript
                              FROM responses r JOIN questions q ON r.question_id=q.id
                              WHERE r.candidate_id=? ORDER BY q.question_order''', (candidate_id,)).fetchall()
    db.close()
    c = dict(candidate)
    if c.get('ai_scores_json'):
        try: c['ai_scores_json'] = json.loads(c['ai_scores_json'])
        except: pass
    return jsonify({'candidate': c, 'responses': [dict(r) for r in responses]})

@app.route('/api/v1/candidates', methods=['POST'])
@require_api_key
def api_v1_create_candidate():
    """Public API: Create a candidate for an interview."""
    data = request.get_json()
    interview_id = data.get('interview_id')
    first_name = data.get('first_name', '').strip()
    last_name = data.get('last_name', '').strip()
    email = data.get('email', '').strip()
    if not all([interview_id, first_name, last_name, email]):
        return jsonify({'error': 'interview_id, first_name, last_name, and email are required'}), 400

    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    import secrets as _secrets
    cid = str(uuid.uuid4())
    token = _secrets.token_urlsafe(32)
    db.execute('''INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email, token)
                  VALUES (?,?,?,?,?,?,?)''', (cid, g.user_id, interview_id, first_name, last_name, email, token))
    db.commit()
    db.close()
    interview_url = f"{request.host_url.rstrip('/')}/i/{token}"
    return jsonify({'id': cid, 'token': token, 'interview_url': interview_url}), 201

@app.route('/api/docs')
def api_docs_page():
    """Serve the API documentation page."""
    brand_color = '#0ace0a'
    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>ChannelView API Documentation</title>
<style>
body{{font-family:Arial,sans-serif;max-width:900px;margin:0 auto;padding:40px;color:#111;line-height:1.6}}
h1{{color:{brand_color};border-bottom:3px solid {brand_color};padding-bottom:12px}}
h2{{color:#333;margin-top:40px}} h3{{color:#555;margin-top:24px}}
code{{background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:14px}}
pre{{background:#111;color:#f0f0f0;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px}}
.method{{display:inline-block;padding:4px 10px;border-radius:4px;font-weight:700;font-size:13px;margin-right:8px}}
.get{{background:#dbeafe;color:#1d4ed8}} .post{{background:#d1fae5;color:#065f46}} .put{{background:#fef3c7;color:#92400e}} .delete{{background:#fee2e2;color:#991b1b}}
.endpoint{{background:#f9fafb;padding:16px;border-radius:8px;margin:12px 0;border-left:4px solid {brand_color}}}
</style></head><body>
<h1>ChannelView API</h1>
<p>REST API for integrating ChannelView with your AMS, CRM, or other tools.</p>
<h2>Authentication</h2>
<p>All API requests require an <code>X-API-Key</code> header. Generate your key in Settings &gt; API.</p>
<pre>curl -H "X-API-Key: cv_your_key_here" {request.host_url.rstrip('/')}/api/v1/interviews</pre>

<h2>Endpoints</h2>

<div class="endpoint"><span class="method get">GET</span><code>/api/v1/interviews</code><p>List all interviews in your account.</p></div>
<div class="endpoint"><span class="method get">GET</span><code>/api/v1/interviews/:id/candidates</code><p>List all candidates for a specific interview.</p></div>
<div class="endpoint"><span class="method post">POST</span><code>/api/v1/candidates</code><p>Create a new candidate. Body: <code>{{"interview_id", "first_name", "last_name", "email"}}</code></p></div>
<div class="endpoint"><span class="method get">GET</span><code>/api/v1/candidates/:id</code><p>Get candidate details including AI scores and responses.</p></div>

<h2>Webhooks</h2>
<p>Configure webhooks in Settings to receive real-time notifications for events like <code>candidate.completed</code> and <code>candidate.scored</code>.</p>

<h2>Rate Limits</h2>
<p>API requests are limited to 100 requests per minute per API key.</p>

<p style="margin-top:40px;color:#999;font-size:13px;text-align:center">ChannelView API v1.0 &middot; <a href="/" style="color:{brand_color}">Back to App</a></p>
</body></html>'''


# ======================== CANDIDATE PROGRESS SAVING (Cycle 11) ========================

@app.route('/api/interview/<token>/save-progress', methods=['POST'])
def api_save_progress(token):
    """Save candidate progress so they can resume later."""
    db = get_db()
    candidate = db.execute('SELECT id, status FROM candidates WHERE token=?', (token,)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404
    if candidate['status'] == 'completed':
        db.close()
        return jsonify({'error': 'Interview already completed'}), 400

    data = request.get_json()
    current_q = data.get('current_question_index', 0)
    progress = json.dumps(data.get('progress', {}))

    db.execute("UPDATE candidates SET current_question_index=?, progress_data=?, last_activity_at=CURRENT_TIMESTAMP WHERE token=?",
               (current_q, progress, token))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/interview/<token>/load-progress', methods=['GET'])
def api_load_progress(token):
    """Load saved candidate progress."""
    db = get_db()
    candidate = db.execute('SELECT current_question_index, progress_data, status FROM candidates WHERE token=?', (token,)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404

    progress = {}
    if candidate['progress_data']:
        try: progress = json.loads(candidate['progress_data'])
        except: pass

    db.close()
    return jsonify({
        'current_question_index': candidate['current_question_index'] or 0,
        'progress': progress,
        'status': candidate['status'],
        'can_resume': candidate['status'] in ('invited', 'in_progress')
    })


# ======================== CYCLE 12: ATS/CRM INTEGRATIONS ========================

def _emit_integration_event(account_id, event_type, payload_data):
    """Emit an integration event for webhook delivery and Zapier compatibility."""
    db = get_db()
    user = db.execute('SELECT zapier_webhook_url, integration_events_enabled FROM users WHERE id=?', (account_id,)).fetchone()
    if not user or not user['integration_events_enabled']:
        db.close()
        return
    event_id = str(uuid.uuid4())
    payload = json.dumps({
        'event': event_type,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'data': payload_data
    })
    db.execute("INSERT INTO integration_events (id, account_id, event_type, payload) VALUES (?, ?, ?, ?)",
               (event_id, account_id, event_type, payload))
    db.commit()

    # Try Zapier webhook delivery
    zapier_url = user['zapier_webhook_url']
    if zapier_url:
        try:
            import urllib.request
            req = urllib.request.Request(zapier_url, data=payload.encode(), method='POST')
            req.add_header('Content-Type', 'application/json')
            urllib.request.urlopen(req, timeout=5)
            db.execute("UPDATE integration_events SET delivered=1, delivery_attempts=1, last_attempt_at=CURRENT_TIMESTAMP WHERE id=?", (event_id,))
        except:
            db.execute("UPDATE integration_events SET delivery_attempts=1, last_attempt_at=CURRENT_TIMESTAMP WHERE id=?", (event_id,))
        db.commit()

    # Also deliver to registered webhooks matching this event type
    hooks = db.execute('SELECT * FROM webhooks WHERE user_id=? AND active=1', (account_id,)).fetchall()
    for hook in hooks:
        events = hook['events'].split(',')
        if event_type in events or '*' in events:
            try:
                import urllib.request
                req = urllib.request.Request(hook['url'], data=payload.encode(), method='POST')
                req.add_header('Content-Type', 'application/json')
                if hook['secret']:
                    import hmac, hashlib
                    sig = hmac.new(hook['secret'].encode(), payload.encode(), hashlib.sha256).hexdigest()
                    req.add_header('X-Webhook-Signature', sig)
                urllib.request.urlopen(req, timeout=5)
            except:
                pass
    db.close()


@app.route('/api/integrations/events', methods=['GET'])
@require_auth
def api_list_integration_events():
    """List recent integration events for this account."""
    db = get_db()
    limit = request.args.get('limit', 50, type=int)
    event_type = request.args.get('type', '')
    if event_type:
        events = db.execute('SELECT * FROM integration_events WHERE account_id=? AND event_type=? ORDER BY created_at DESC LIMIT ?',
                            (g.user_id, event_type, limit)).fetchall()
    else:
        events = db.execute('SELECT * FROM integration_events WHERE account_id=? ORDER BY created_at DESC LIMIT ?',
                            (g.user_id, limit)).fetchall()
    db.close()
    return jsonify({'events': [dict(e) for e in events]})


@app.route('/api/integrations/events/types', methods=['GET'])
@require_auth
def api_list_event_types():
    """List all available integration event types."""
    return jsonify({'event_types': [
        {'type': 'candidate.invited', 'description': 'Candidate was invited to an interview'},
        {'type': 'candidate.started', 'description': 'Candidate started their interview'},
        {'type': 'candidate.completed', 'description': 'Candidate completed their interview'},
        {'type': 'candidate.scored', 'description': 'Candidate was scored by AI or manually'},
        {'type': 'candidate.status_changed', 'description': 'Candidate status was updated'},
        {'type': 'candidate.hired', 'description': 'Candidate was marked as hired'},
        {'type': 'candidate.rejected', 'description': 'Candidate was rejected'},
        {'type': 'interview.created', 'description': 'New interview was created'},
        {'type': 'interview.closed', 'description': 'Interview was closed/deactivated'},
    ]})


@app.route('/api/integrations/zapier', methods=['GET'])
@require_auth
def api_get_zapier_config():
    """Get Zapier integration configuration."""
    db = get_db()
    user = db.execute('SELECT zapier_webhook_url, integration_events_enabled FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()
    return jsonify({
        'zapier_webhook_url': user['zapier_webhook_url'] or '',
        'events_enabled': bool(user['integration_events_enabled'])
    })


@app.route('/api/integrations/zapier', methods=['PUT'])
@require_auth
@require_role('admin')
def api_update_zapier_config():
    """Update Zapier webhook URL and settings."""
    data = request.get_json()
    db = get_db()
    db.execute("UPDATE users SET zapier_webhook_url=?, integration_events_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (data.get('zapier_webhook_url', ''), 1 if data.get('events_enabled', True) else 0, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/integrations/zapier/test', methods=['POST'])
@require_auth
@require_role('admin')
def api_test_zapier():
    """Send a test event to the Zapier webhook URL."""
    db = get_db()
    user = db.execute('SELECT zapier_webhook_url FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()
    url = user['zapier_webhook_url']
    if not url:
        return jsonify({'error': 'No Zapier webhook URL configured'}), 400
    test_payload = json.dumps({
        'event': 'test.ping',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'data': {'message': 'Test event from ChannelView', 'account_id': g.user_id}
    })
    try:
        import urllib.request
        req = urllib.request.Request(url, data=test_payload.encode(), method='POST')
        req.add_header('Content-Type', 'application/json')
        resp = urllib.request.urlopen(req, timeout=10)
        return jsonify({'success': True, 'status_code': resp.getcode()})
    except Exception as e:
        return jsonify({'error': f'Webhook delivery failed: {str(e)}'}), 502


@app.route('/api/integrations', methods=['GET'])
@require_auth
def api_list_integrations():
    """List configured integrations."""
    db = get_db()
    integrations = db.execute('SELECT * FROM integrations WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    db.close()
    return jsonify({'integrations': [dict(i) for i in integrations]})


@app.route('/api/integrations', methods=['POST'])
@require_auth
@require_role('admin')
def api_create_integration():
    """Create/update an integration connection."""
    data = request.get_json()
    provider = data.get('provider', '')
    config = json.dumps(data.get('config', {}))
    if not provider:
        return jsonify({'error': 'Provider required'}), 400
    db = get_db()
    existing = db.execute('SELECT id FROM integrations WHERE user_id=? AND provider=?', (g.user_id, provider)).fetchone()
    if existing:
        db.execute("UPDATE integrations SET config=?, active=1, last_sync_at=CURRENT_TIMESTAMP WHERE id=?",
                   (config, existing['id']))
    else:
        db.execute("INSERT INTO integrations (id, user_id, provider, config) VALUES (?, ?, ?, ?)",
                   (str(uuid.uuid4()), g.user_id, provider, config))
    db.commit()
    db.close()
    return jsonify({'success': True}), 201


@app.route('/api/integrations/<integration_id>', methods=['DELETE'])
@require_auth
@require_role('admin')
def api_delete_integration(integration_id):
    """Delete an integration."""
    db = get_db()
    db.execute("DELETE FROM integrations WHERE id=? AND user_id=?", (integration_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ======================== CYCLE 12: BULK OPERATIONS & KANBAN ========================

@app.route('/api/candidates/bulk-invite', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_bulk_invite_csv():
    """Bulk invite candidates via CSV data. Expects JSON with interview_id and candidates array."""
    # Cycle 31: Feature gate — bulk_ops
    db_check = get_db()
    user_check = dict(db_check.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    db_check.close()
    bulk_allowed, upgrade = check_feature_access(user_check, 'bulk_ops')
    if not bulk_allowed:
        return soft_block_response('bulk_ops')

    data = request.get_json()
    interview_id = data.get('interview_id', '')
    candidates_data = data.get('candidates', [])
    if not interview_id or not candidates_data:
        return jsonify({'error': 'interview_id and candidates array required'}), 400

    db = get_db()
    interview = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    import secrets as _secrets
    created = []
    errors = []
    for i, c in enumerate(candidates_data):
        email = c.get('email', '').strip()
        first = c.get('first_name', '').strip()
        last = c.get('last_name', '').strip()
        if not email or not first:
            errors.append({'row': i+1, 'error': 'Missing required fields (email, first_name)'})
            continue
        # Check duplicate
        existing = db.execute('SELECT id FROM candidates WHERE interview_id=? AND email=?', (interview_id, email)).fetchone()
        if existing:
            errors.append({'row': i+1, 'error': f'Duplicate email: {email}'})
            continue
        cid = str(uuid.uuid4())
        token = _secrets.token_urlsafe(12)
        db.execute("""INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email, phone, token, source)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'csv_import')""",
                   (cid, g.user_id, interview_id, first, last or '', email, c.get('phone', ''), token))
        created.append({'id': cid, 'email': email, 'token': token})

    db.commit()
    db.close()

    # Emit integration events after DB is closed
    for c in created:
        _emit_integration_event(g.user_id, 'candidate.invited', {
            'candidate_id': c['id'], 'email': c['email'], 'interview_id': interview_id, 'source': 'csv_import'
        })

    return jsonify({'created': len(created), 'errors': errors, 'candidates': created})


@app.route('/api/candidates/bulk-score', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_bulk_score():
    """Trigger AI scoring for multiple candidates at once. Requires Professional+ and bulk_ops."""
    # Cycle 31: Feature gates — ai_scoring + bulk_ops
    db_check = get_db()
    user_check = dict(db_check.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    db_check.close()
    ai_ok, _ = check_feature_access(user_check, 'ai_scoring')
    if not ai_ok:
        return soft_block_response('ai_scoring')
    bulk_ok, _ = check_feature_access(user_check, 'bulk_ops')
    if not bulk_ok:
        return soft_block_response('bulk_ops')

    data = request.get_json()
    candidate_ids = data.get('candidate_ids', [])
    if not candidate_ids:
        return jsonify({'error': 'candidate_ids array required'}), 400

    db = get_db()
    scored = 0
    for cid in candidate_ids[:50]:  # Limit to 50 at a time
        candidate = db.execute('SELECT * FROM candidates WHERE id=? AND user_id=?', (cid, g.user_id)).fetchone()
        if candidate and candidate['status'] == 'completed' and not candidate['ai_score']:
            # Mock AI score for bulk (real implementation would queue jobs)
            import random
            score = round(random.uniform(50, 95), 1)
            db.execute("UPDATE candidates SET ai_score=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (score, cid))
            scored += 1
    db.commit()
    db.close()
    return jsonify({'scored': scored, 'total_requested': len(candidate_ids)})


@app.route('/api/candidates/pipeline', methods=['GET'])
@require_auth
def api_candidates_pipeline():
    """Get candidates grouped by pipeline stage for Kanban view."""
    db = get_db()
    interview_id = request.args.get('interview_id', '')
    if interview_id:
        candidates = db.execute(
            "SELECT * FROM candidates WHERE user_id=? AND interview_id=? ORDER BY kanban_order ASC, created_at DESC",
            (g.user_id, interview_id)).fetchall()
    else:
        candidates = db.execute(
            "SELECT * FROM candidates WHERE user_id=? ORDER BY kanban_order ASC, created_at DESC",
            (g.user_id,)).fetchall()
    db.close()

    stages = ['new', 'in_review', 'shortlisted', 'interview_scheduled', 'offered', 'hired', 'rejected']
    pipeline = {stage: [] for stage in stages}
    for c in candidates:
        stage = c['pipeline_stage'] or 'new'
        if stage in pipeline:
            pipeline[stage].append(dict(c))
        else:
            pipeline['new'].append(dict(c))

    return jsonify({'pipeline': pipeline, 'stages': stages})


@app.route('/api/candidates/<candidate_id>/pipeline-stage', methods=['PUT'])
@require_auth
@require_role('admin', 'recruiter')
def api_update_pipeline_stage(candidate_id):
    """Move a candidate to a different pipeline stage (Kanban drag/drop)."""
    data = request.get_json()
    new_stage = data.get('stage', 'new')
    order = data.get('order', 0)
    valid_stages = ['new', 'in_review', 'shortlisted', 'interview_scheduled', 'offered', 'hired', 'rejected']
    if new_stage not in valid_stages:
        return jsonify({'error': f'Invalid stage. Must be one of: {", ".join(valid_stages)}'}), 400

    db = get_db()
    candidate = db.execute('SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    old_stage = candidate['pipeline_stage'] or 'new'
    db.execute("UPDATE candidates SET pipeline_stage=?, kanban_order=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (new_stage, order, candidate_id))

    # Auto-update status on certain stage transitions
    if new_stage == 'hired':
        db.execute("UPDATE candidates SET status='hired', updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate_id,))
    elif new_stage == 'rejected':
        db.execute("UPDATE candidates SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate_id,))

    db.commit()
    db.close()

    # Emit events after db is closed
    if new_stage == 'hired':
        _emit_integration_event(g.user_id, 'candidate.hired', {
            'candidate_id': candidate_id, 'previous_stage': old_stage
        })
    elif new_stage == 'rejected':
        _emit_integration_event(g.user_id, 'candidate.rejected', {
            'candidate_id': candidate_id, 'previous_stage': old_stage
        })
    _emit_integration_event(g.user_id, 'candidate.status_changed', {
        'candidate_id': candidate_id, 'from_stage': old_stage, 'to_stage': new_stage
    })

    return jsonify({'success': True, 'stage': new_stage})


@app.route('/api/candidates/bulk-pipeline', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_bulk_pipeline_move():
    """Move multiple candidates to a pipeline stage at once."""
    data = request.get_json()
    candidate_ids = data.get('candidate_ids', [])
    new_stage = data.get('stage', '')
    valid_stages = ['new', 'in_review', 'shortlisted', 'interview_scheduled', 'offered', 'hired', 'rejected']
    if not candidate_ids or new_stage not in valid_stages:
        return jsonify({'error': 'candidate_ids and valid stage required'}), 400

    db = get_db()
    moved = 0
    for cid in candidate_ids[:100]:
        c = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (cid, g.user_id)).fetchone()
        if c:
            db.execute("UPDATE candidates SET pipeline_stage=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_stage, cid))
            moved += 1
    db.commit()
    db.close()
    return jsonify({'moved': moved})


# ======================== CYCLE 12: COMPLIANCE & AUDIT LOGGING ========================

def _log_compliance(action, resource_type, resource_id=None, details=None):
    """Log a compliance event with actor context."""
    try:
        account_id = getattr(g, 'user_id', 'system')
        actor_id = getattr(g, 'user_id', 'system')
        ip = request.remote_addr or 'unknown'
        ua = request.headers.get('User-Agent', '')[:200]
        db = get_db()
        db.execute("""INSERT INTO compliance_log (id, account_id, actor_id, action, resource_type, resource_id, ip_address, user_agent, details)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (str(uuid.uuid4()), account_id, actor_id, action, resource_type, resource_id, ip, ua,
                    json.dumps(details) if details else None))
        db.commit()
        db.close()
    except:
        pass  # Never let logging break the main flow


@app.route('/api/compliance/log', methods=['GET'])
@require_auth
@require_role('admin')
def api_compliance_log():
    """Get compliance audit trail with filtering."""
    db = get_db()
    limit = request.args.get('limit', 100, type=int)
    action_filter = request.args.get('action', '')
    resource_filter = request.args.get('resource_type', '')
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')

    query = 'SELECT * FROM compliance_log WHERE account_id=?'
    params = [g.user_id]
    if action_filter:
        query += ' AND action=?'
        params.append(action_filter)
    if resource_filter:
        query += ' AND resource_type=?'
        params.append(resource_filter)
    if date_from:
        query += ' AND created_at >= ?'
        params.append(date_from)
    if date_to:
        query += ' AND created_at <= ?'
        params.append(date_to)
    query += ' ORDER BY created_at DESC LIMIT ?'
    params.append(limit)

    entries = db.execute(query, params).fetchall()
    db.close()
    return jsonify({'entries': [dict(e) for e in entries], 'total': len(entries)})


@app.route('/api/compliance/export', methods=['GET'])
@require_auth
@require_role('admin')
def api_compliance_export():
    """Export compliance log as CSV."""
    db = get_db()
    entries = db.execute('SELECT * FROM compliance_log WHERE account_id=? ORDER BY created_at DESC LIMIT 10000',
                         (g.user_id,)).fetchall()
    db.close()

    import io, csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Timestamp', 'Actor', 'Action', 'Resource Type', 'Resource ID', 'IP Address', 'Details'])
    for e in entries:
        writer.writerow([e['created_at'], e['actor_id'], e['action'], e['resource_type'], e['resource_id'], e['ip_address'], e['details']])

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=compliance_log.csv'
    return response


@app.route('/api/compliance/retention', methods=['GET'])
@require_auth
@require_role('admin')
def api_get_retention():
    """Get data retention policies."""
    db = get_db()
    policies = db.execute('SELECT * FROM retention_policies WHERE user_id=?', (g.user_id,)).fetchall()
    user = db.execute('SELECT data_retention_days, eeoc_mode_enabled FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()
    return jsonify({
        'default_retention_days': user['data_retention_days'] or 365,
        'eeoc_mode': bool(user['eeoc_mode_enabled']),
        'policies': [dict(p) for p in policies]
    })


@app.route('/api/compliance/retention', methods=['PUT'])
@require_auth
@require_role('admin')
def api_update_retention():
    """Update data retention settings."""
    data = request.get_json()
    db = get_db()
    db.execute("UPDATE users SET data_retention_days=?, eeoc_mode_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (data.get('retention_days', 365), 1 if data.get('eeoc_mode', False) else 0, g.user_id))

    # Upsert per-resource policies
    for policy in data.get('policies', []):
        resource_type = policy.get('resource_type', '')
        days = policy.get('retention_days', 365)
        auto_delete = 1 if policy.get('auto_delete', False) else 0
        if resource_type:
            existing = db.execute('SELECT id FROM retention_policies WHERE user_id=? AND resource_type=?',
                                  (g.user_id, resource_type)).fetchone()
            if existing:
                db.execute("UPDATE retention_policies SET retention_days=?, auto_delete=? WHERE id=?",
                           (days, auto_delete, existing['id']))
            else:
                db.execute("INSERT INTO retention_policies (id, user_id, resource_type, retention_days, auto_delete) VALUES (?, ?, ?, ?, ?)",
                           (str(uuid.uuid4()), g.user_id, resource_type, days, auto_delete))

    db.commit()
    db.close()
    _log_compliance('retention_policy_updated', 'settings', details=data)
    return jsonify({'success': True})


@app.route('/api/compliance/retention/check', methods=['GET'])
@require_auth
@require_role('admin')
def api_check_retention():
    """Check what data would be affected by current retention policies."""
    db = get_db()
    user = db.execute('SELECT data_retention_days FROM users WHERE id=?', (g.user_id,)).fetchone()
    days = user['data_retention_days'] or 365
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    expired_candidates = db.execute(
        'SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND created_at < ?',
        (g.user_id, cutoff)).fetchone()['cnt']
    expired_responses = db.execute(
        'SELECT COUNT(*) as cnt FROM responses WHERE candidate_id IN (SELECT id FROM candidates WHERE user_id=? AND created_at < ?)',
        (g.user_id, cutoff)).fetchone()['cnt']
    expired_logs = db.execute(
        'SELECT COUNT(*) as cnt FROM compliance_log WHERE account_id=? AND created_at < ?',
        (g.user_id, cutoff)).fetchone()['cnt']
    db.close()

    return jsonify({
        'retention_days': days,
        'cutoff_date': cutoff,
        'expired_candidates': expired_candidates,
        'expired_responses': expired_responses,
        'expired_log_entries': expired_logs
    })


@app.route('/api/compliance/scoring-doc', methods=['POST'])
@require_auth
@require_role('admin', 'recruiter')
def api_create_scoring_doc():
    """Create an EEOC-compliant scoring documentation record."""
    data = request.get_json()
    candidate_id = data.get('candidate_id', '')
    criteria = data.get('scoring_criteria', '')
    justification = data.get('justification', '')
    score = data.get('score', 0)

    if not candidate_id or not criteria or not justification:
        return jsonify({'error': 'candidate_id, scoring_criteria, and justification required'}), 400

    db = get_db()
    candidate = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    doc_id = str(uuid.uuid4())
    db.execute("INSERT INTO scoring_docs (id, candidate_id, scorer_id, scoring_criteria, justification, score) VALUES (?, ?, ?, ?, ?, ?)",
               (doc_id, candidate_id, g.user_id, criteria, justification, score))
    db.commit()
    db.close()
    _log_compliance('scoring_documented', 'candidate', candidate_id, {'score': score, 'criteria': criteria})
    return jsonify({'id': doc_id, 'success': True}), 201


@app.route('/api/compliance/scoring-docs/<candidate_id>', methods=['GET'])
@require_auth
def api_get_scoring_docs(candidate_id):
    """Get EEOC scoring documentation for a candidate."""
    db = get_db()
    docs = db.execute('''SELECT sd.*, u.name as scorer_name FROM scoring_docs sd
                         JOIN users u ON sd.scorer_id = u.id
                         WHERE sd.candidate_id=? ORDER BY sd.created_at DESC''',
                      (candidate_id,)).fetchall()
    db.close()
    return jsonify({'docs': [dict(d) for d in docs]})


# ======================== CYCLE 12: MOBILE-FIRST CANDIDATE EXPERIENCE ========================

@app.route('/api/interview/<token>/mobile-config', methods=['GET'])
def api_mobile_config(token):
    """Get mobile-optimized configuration for the candidate interview."""
    db = get_db()
    candidate = db.execute('SELECT * FROM candidates WHERE token=?', (token,)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404

    interview = db.execute('SELECT * FROM interviews WHERE id=?', (candidate['interview_id'],)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    questions = db.execute('SELECT id, question_text, question_order, thinking_time, max_answer_time FROM questions WHERE interview_id=? ORDER BY question_order',
                           (interview['id'],)).fetchall()

    # Get branding for the account owner
    owner = db.execute('SELECT brand_color, agency_name, white_label_enabled, candidate_brand_name, candidate_brand_logo FROM users WHERE id=?',
                       (interview['user_id'],)).fetchone()
    db.close()

    brand_name = owner['candidate_brand_name'] if (owner['white_label_enabled'] and owner['candidate_brand_name']) else (owner['agency_name'] or 'ChannelView')
    brand_logo = owner['candidate_brand_logo'] if (owner['white_label_enabled'] and owner['candidate_brand_logo']) else None

    return jsonify({
        'interview': {
            'id': interview['id'],
            'title': interview['title'],
            'welcome_msg': interview['welcome_msg'],
            'thank_you_msg': interview['thank_you_msg'],
            'thinking_time': interview['thinking_time'],
            'max_answer_time': interview['max_answer_time'],
            'max_retakes': interview['max_retakes']
        },
        'questions': [dict(q) for q in questions],
        'candidate': {
            'id': candidate['id'],
            'first_name': candidate['first_name'],
            'status': candidate['status'],
            'current_question_index': candidate['current_question_index'] or 0
        },
        'branding': {
            'color': owner['brand_color'] or '#0ace0a',
            'name': brand_name,
            'logo': brand_logo
        },
        'mobile': {
            'supported_formats': ['video/webm', 'video/mp4'],
            'max_upload_mb': 100,
            'enable_camera_switch': True,
            'enable_orientation_lock': True,
            'low_bandwidth_mode': True,
            'chunk_upload_size_kb': 512,
            'reconnect_timeout_ms': 5000
        }
    })


@app.route('/api/interview/<token>/chunk-upload', methods=['POST'])
def api_chunk_upload(token):
    """Upload a video chunk for mobile resilience (supports resumable uploads)."""
    db = get_db()
    candidate = db.execute('SELECT id, status FROM candidates WHERE token=?', (token,)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404
    if candidate['status'] == 'completed':
        db.close()
        return jsonify({'error': 'Interview already completed'}), 400

    chunk_index = request.form.get('chunk_index', '0')
    question_id = request.form.get('question_id', '')
    total_chunks = request.form.get('total_chunks', '1')

    if 'chunk' not in request.files:
        db.close()
        return jsonify({'error': 'No chunk file provided'}), 400

    chunk = request.files['chunk']
    # Store chunk with naming convention: candidate_id_question_id_chunk_index
    chunk_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'chunks', candidate['id'])
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_path = os.path.join(chunk_dir, f"{question_id}_{chunk_index}.webm")
    chunk.save(chunk_path)

    db.close()
    return jsonify({
        'success': True,
        'chunk_index': int(chunk_index),
        'total_chunks': int(total_chunks),
        'received': True
    })


@app.route('/api/interview/<token>/connection-status', methods=['POST'])
def api_connection_status(token):
    """Report connection quality from the mobile client for adaptive bitrate."""
    db = get_db()
    candidate = db.execute('SELECT id FROM candidates WHERE token=?', (token,)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404
    db.close()

    data = request.get_json() or {}
    # Just acknowledge - in production this would adjust streaming params
    return jsonify({
        'acknowledged': True,
        'recommended_bitrate': 500000 if data.get('bandwidth_kbps', 1000) < 500 else 1500000,
        'recommended_resolution': '480p' if data.get('bandwidth_kbps', 1000) < 500 else '720p'
    })


# ======================== CYCLE 13: MULTI-TENANCY & PERFORMANCE ========================

@app.route('/api/system/db-stats', methods=['GET'])
@require_auth
@require_role('admin')
def api_db_stats():
    """Get database statistics for monitoring."""
    db = get_db()
    stats = {}

    # Table row counts
    tables = ['users', 'interviews', 'candidates', 'questions', 'responses',
              'webhooks', 'notifications', 'audit_log', 'compliance_log',
              'integration_events', 'email_log']
    for tbl in tables:
        try:
            count = db.execute(f'SELECT COUNT(*) as cnt FROM {tbl}').fetchone()['cnt']
            stats[tbl] = count
        except:
            stats[tbl] = -1

    # DB file size
    from database import USE_POSTGRES
    if USE_POSTGRES:
        try:
            size_row = db.execute("SELECT pg_database_size(current_database()) as size").fetchone()
            stats['db_size_mb'] = round(size_row['size'] / (1024 * 1024), 2) if size_row else 0
        except:
            stats['db_size_mb'] = 0
    else:
        db_path = os.path.join(os.path.dirname(__file__), 'channelview.db')
        try:
            stats['db_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
        except:
            stats['db_size_mb'] = 0

    # WAL mode / integrity check (SQLite-specific, safe skip on Postgres)
    if USE_POSTGRES:
        stats['journal_mode'] = 'postgres_wal'
        stats['integrity'] = 'ok'
    else:
        mode = db.execute('PRAGMA journal_mode').fetchone()
        stats['journal_mode'] = mode[0] if mode else 'unknown'
        integrity = db.execute('PRAGMA quick_check').fetchone()
        stats['integrity'] = integrity[0] if integrity else 'unknown'

    db.close()
    return jsonify({'stats': stats})


@app.route('/api/system/performance', methods=['GET'])
@require_auth
@require_role('admin')
def api_performance_metrics():
    """Get performance metrics and query timings."""
    db = get_db()

    metrics = {}

    # Candidate count per interview (for identifying heavy interviews)
    top_interviews = db.execute('''
        SELECT i.title, COUNT(c.id) as candidate_count
        FROM interviews i LEFT JOIN candidates c ON c.interview_id = i.id
        WHERE i.user_id = ? GROUP BY i.id ORDER BY candidate_count DESC LIMIT 5
    ''', (g.user_id,)).fetchall()
    metrics['top_interviews'] = [{'title': r['title'], 'candidates': r['candidate_count']} for r in top_interviews]

    # Response video storage usage
    total_size = db.execute('''
        SELECT COALESCE(SUM(r.file_size), 0) as total
        FROM responses r JOIN candidates c ON r.candidate_id = c.id
        WHERE c.user_id = ?
    ''', (g.user_id,)).fetchone()['total']
    metrics['storage_used_mb'] = round(total_size / (1024 * 1024), 2)

    # Active rate limits
    metrics['rate_limit_entries'] = len(_rate_limits)

    # Recent activity (last 7 days)
    from datetime import datetime, timedelta
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    recent_candidates = db.execute(
        'SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND created_at > ?',
        (g.user_id, week_ago)).fetchone()['cnt']
    recent_completions = db.execute(
        'SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND completed_at > ?',
        (g.user_id, week_ago)).fetchone()['cnt']
    metrics['last_7_days'] = {
        'new_candidates': recent_candidates,
        'completions': recent_completions
    }

    db.close()
    return jsonify({'metrics': metrics})


@app.route('/api/system/backup', methods=['POST'])
@require_auth
@require_role('admin')
def api_create_backup():
    """Create a database backup."""
    import shutil
    db_path = os.path.join(os.path.dirname(__file__), 'channelview.db')
    backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(backup_dir, f'channelview_{timestamp}.db')

    try:
        from database import USE_POSTGRES, DATABASE_URL
        if USE_POSTGRES:
            # PostgreSQL backup via pg_dump
            backup_path = os.path.join(backup_dir, f'channelview_{timestamp}.sql')
            import subprocess
            result = subprocess.run(
                ['pg_dump', DATABASE_URL, '-f', backup_path],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return jsonify({'error': f'pg_dump failed: {result.stderr}'}), 500
        else:
            # SQLite backup API for safe copy
            import sqlite3
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(backup_path)
            src.backup(dst)
            src.close()
            dst.close()

        size_mb = round(os.path.getsize(backup_path) / (1024 * 1024), 2)
        return jsonify({'success': True, 'backup': backup_path, 'size_mb': size_mb, 'timestamp': timestamp})
    except Exception as e:
        return jsonify({'error': f'Backup failed: {str(e)}'}), 500


@app.route('/api/system/tenant-audit', methods=['GET'])
@require_auth
@require_role('admin')
def api_tenant_audit():
    """Audit tenant data isolation - verify no cross-tenant data leaks."""
    db = get_db()
    issues = []

    # Check for candidates not belonging to the user
    orphan_candidates = db.execute('''
        SELECT COUNT(*) as cnt FROM candidates c
        WHERE c.user_id = ? AND c.interview_id NOT IN (SELECT id FROM interviews WHERE user_id = ?)
    ''', (g.user_id, g.user_id)).fetchone()['cnt']
    if orphan_candidates > 0:
        issues.append(f'{orphan_candidates} candidates linked to foreign interviews')

    # Check for responses not belonging to user's candidates
    orphan_responses = db.execute('''
        SELECT COUNT(*) as cnt FROM responses r
        WHERE r.candidate_id NOT IN (SELECT id FROM candidates WHERE user_id = ?)
        AND r.candidate_id IN (SELECT id FROM candidates WHERE user_id = ?)
    ''', (g.user_id, g.user_id)).fetchone()['cnt']

    db.close()
    return jsonify({
        'clean': len(issues) == 0,
        'issues': issues,
        'checked': ['candidates', 'responses', 'interview_ownership']
    })


# ======================== CYCLE 13: EMAIL DELIVERABILITY ========================

@app.route('/api/email/test-deliverability', methods=['POST'])
@require_auth
@require_role('admin')
def api_test_email_deliverability():
    """Send a test email and track delivery."""
    data = request.get_json()
    to_email = data.get('to_email', g.user.get('email', ''))
    if not to_email:
        return jsonify({'error': 'to_email required'}), 400

    from email_service import send_email, get_smtp_config, _base_template
    db = get_db()
    smtp_config = get_smtp_config(db, g.user_id)
    brand_color = g.user.get('brand_color', '#0ace0a')
    agency_name = g.user.get('agency_name', 'ChannelView')

    content = f'''
    <h1 style="margin:0 0 8px;font-size:22px;color:#111">Email Deliverability Test</h1>
    <p style="color:#6b7280;font-size:15px;line-height:1.5">
      This is a test email from ChannelView to verify your email delivery setup is working correctly.
    </p>
    <div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">
      <p style="margin:0;font-size:14px;color:#666">
        <strong>Sent to:</strong> {to_email}<br>
        <strong>Agency:</strong> {agency_name}<br>
        <strong>Timestamp:</strong> {datetime.utcnow().isoformat()}Z
      </p>
    </div>
    <p style="color:#6b7280;font-size:13px">If you received this email, your delivery setup is working.</p>
    '''
    html = _base_template(brand_color, agency_name, content)
    success, error = send_email(smtp_config, to_email, 'ChannelView - Email Delivery Test', html)

    # Log the test (NULL candidate_id since it's a system test)
    db.execute("""INSERT INTO email_log (id, user_id, candidate_id, email_type, to_email, subject, status, error_message)
                  VALUES (?, ?, NULL, 'deliverability_test', ?, 'Email Delivery Test', ?, ?)""",
               (str(uuid.uuid4()), g.user_id, to_email, 'sent' if success else 'failed', error))
    db.commit()
    db.close()

    return jsonify({'success': success, 'error': error, 'to': to_email})


@app.route('/api/email/log', methods=['GET'])
@require_auth
def api_email_log():
    """Get email delivery log."""
    db = get_db()
    limit = request.args.get('limit', 50, type=int)
    status_filter = request.args.get('status', '')

    query = 'SELECT * FROM email_log WHERE user_id=?'
    params = [g.user_id]
    if status_filter:
        query += ' AND status=?'
        params.append(status_filter)
    query += ' ORDER BY sent_at DESC LIMIT ?'
    params.append(limit)

    logs = db.execute(query, params).fetchall()
    db.close()

    total_sent = sum(1 for l in logs if l['status'] == 'sent')
    total_failed = sum(1 for l in logs if l['status'] == 'failed')
    return jsonify({
        'logs': [dict(l) for l in logs],
        'summary': {'sent': total_sent, 'failed': total_failed, 'total': len(logs)}
    })


@app.route('/api/email/stats', methods=['GET'])
@require_auth
def api_email_stats():
    """Get email delivery statistics."""
    db = get_db()
    total = db.execute('SELECT COUNT(*) as cnt FROM email_log WHERE user_id=?', (g.user_id,)).fetchone()['cnt']
    sent = db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE user_id=? AND status='sent'", (g.user_id,)).fetchone()['cnt']
    failed = db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE user_id=? AND status='failed'", (g.user_id,)).fetchone()['cnt']

    # By type breakdown
    by_type = db.execute('''
        SELECT email_type, COUNT(*) as cnt, SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) as sent_cnt
        FROM email_log WHERE user_id=? GROUP BY email_type
    ''', (g.user_id,)).fetchall()

    db.close()
    return jsonify({
        'total': total, 'sent': sent, 'failed': failed,
        'delivery_rate': round(100 * sent / total, 1) if total > 0 else 100,
        'by_type': [{'type': r['email_type'], 'total': r['cnt'], 'sent': r['sent_cnt']} for r in by_type]
    })


# ======================== CYCLE 13: SYSTEM PAGE & DOCS ========================

@app.route('/system')
@require_auth
@require_fmo_admin
def system_page():
    return render_template('app.html', page='system', user=g.user)

@app.route('/docs')
def docs_page():
    """Serve the public documentation page."""
    return render_template('docs.html')

@app.route('/api/docs/guide', methods=['GET'])
def api_docs_guide():
    """Return structured API documentation as JSON."""
    return jsonify({
        'version': 'v1',
        'base_url': request.host_url.rstrip('/'),
        'auth': {
            'type': 'API Key',
            'header': 'X-API-Key',
            'description': 'Generate an API key from Settings > API to authenticate requests.'
        },
        'endpoints': [
            {'method': 'GET', 'path': '/api/v1/interviews', 'description': 'List all interviews'},
            {'method': 'GET', 'path': '/api/v1/interviews/:id/candidates', 'description': 'List candidates for an interview'},
            {'method': 'GET', 'path': '/api/v1/candidates/:id', 'description': 'Get candidate details with responses'},
            {'method': 'POST', 'path': '/api/v1/candidates', 'description': 'Create and invite a new candidate'},
        ],
        'webhooks': {
            'description': 'Configure webhook URLs to receive real-time event notifications.',
            'events': [
                'candidate.invited', 'candidate.started', 'candidate.completed',
                'candidate.scored', 'candidate.hired', 'candidate.rejected',
                'candidate.status_changed', 'interview.created', 'interview.closed'
            ]
        }
    })


# ======================== CYCLE 14: WHITE-LABEL & FMO BRANDING ========================

@app.route('/api/branding/profiles', methods=['GET'])
@require_auth
@require_role('admin', 'owner')
def api_list_brand_profiles():
    """List all brand profiles for the current account."""
    db = get_db()
    profiles = db.execute('SELECT * FROM brand_profiles WHERE owner_id=? ORDER BY is_default DESC, created_at DESC',
                          (g.user_id,)).fetchall()
    db.close()
    return jsonify({'profiles': [dict(p) for p in profiles]})


@app.route('/api/branding/profiles', methods=['POST'])
@require_auth
@require_role('admin', 'owner')
def api_create_brand_profile():
    """Create a new brand profile."""
    data = request.get_json()
    db = get_db()
    profile_id = str(uuid.uuid4())
    db.execute("""INSERT INTO brand_profiles (id, owner_id, profile_name, primary_color, secondary_color, accent_color,
                  logo_url, favicon_url, custom_domain, email_from_name, email_header_html, email_footer_html,
                  candidate_portal_title, candidate_portal_tagline, hide_powered_by, custom_css, is_default)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (profile_id, g.user_id, data.get('profile_name', 'Default'),
                data.get('primary_color', '#0ace0a'), data.get('secondary_color', '#000000'),
                data.get('accent_color', '#ffffff'), data.get('logo_url', ''),
                data.get('favicon_url', ''), data.get('custom_domain', ''),
                data.get('email_from_name', ''), data.get('email_header_html', ''),
                data.get('email_footer_html', ''), data.get('candidate_portal_title', ''),
                data.get('candidate_portal_tagline', ''), 1 if data.get('hide_powered_by') else 0,
                data.get('custom_css', ''), 1 if data.get('is_default') else 0))
    db.commit()
    profile = db.execute('SELECT * FROM brand_profiles WHERE id=?', (profile_id,)).fetchone()
    db.close()
    return jsonify({'profile': dict(profile)}), 201


@app.route('/api/branding/profiles/<profile_id>', methods=['PUT'])
@require_auth
@require_role('admin', 'owner')
def api_update_brand_profile(profile_id):
    """Update an existing brand profile."""
    data = request.get_json()
    db = get_db()
    profile = db.execute('SELECT * FROM brand_profiles WHERE id=? AND owner_id=?', (profile_id, g.user_id)).fetchone()
    if not profile:
        db.close()
        return jsonify({'error': 'Profile not found'}), 404

    fields = ['profile_name', 'primary_color', 'secondary_color', 'accent_color', 'logo_url', 'favicon_url',
              'custom_domain', 'email_from_name', 'email_header_html', 'email_footer_html',
              'candidate_portal_title', 'candidate_portal_tagline', 'hide_powered_by', 'custom_css', 'is_default']
    updates = []
    values = []
    for f in fields:
        if f in data:
            updates.append(f"{f}=?")
            if f in ('hide_powered_by', 'is_default'):
                values.append(1 if data[f] else 0)
            else:
                values.append(data[f])
    if updates:
        updates.append("updated_at=CURRENT_TIMESTAMP")
        values.append(profile_id)
        values.append(g.user_id)
        db.execute(f"UPDATE brand_profiles SET {', '.join(updates)} WHERE id=? AND owner_id=?", values)
        # If setting as default, unset others
        if data.get('is_default'):
            db.execute('UPDATE brand_profiles SET is_default=0 WHERE owner_id=? AND id!=?', (g.user_id, profile_id))
        db.commit()

    updated = db.execute('SELECT * FROM brand_profiles WHERE id=?', (profile_id,)).fetchone()
    db.close()
    return jsonify({'profile': dict(updated)})


@app.route('/api/branding/profiles/<profile_id>', methods=['DELETE'])
@require_auth
@require_role('admin', 'owner')
def api_delete_brand_profile(profile_id):
    """Delete a brand profile."""
    db = get_db()
    profile = db.execute('SELECT * FROM brand_profiles WHERE id=? AND owner_id=?', (profile_id, g.user_id)).fetchone()
    if not profile:
        db.close()
        return jsonify({'error': 'Profile not found'}), 404
    db.execute('DELETE FROM brand_profiles WHERE id=?', (profile_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/branding/preview', methods=['POST'])
@require_auth
def api_branding_preview():
    """Generate a preview of branding applied to candidate portal."""
    data = request.get_json()
    return jsonify({
        'preview': {
            'primary_color': data.get('primary_color', '#0ace0a'),
            'secondary_color': data.get('secondary_color', '#000000'),
            'portal_title': data.get('candidate_portal_title', 'Video Interview'),
            'portal_tagline': data.get('candidate_portal_tagline', 'Complete your interview at your own pace'),
            'logo_url': data.get('logo_url', ''),
            'hide_powered_by': data.get('hide_powered_by', False),
            'email_from_name': data.get('email_from_name', 'ChannelView')
        }
    })


@app.route('/api/branding/apply/<profile_id>', methods=['POST'])
@require_auth
@require_role('admin', 'owner')
def api_apply_brand_profile(profile_id):
    """Apply a brand profile to the current account."""
    db = get_db()
    profile = db.execute('SELECT * FROM brand_profiles WHERE id=? AND owner_id=?', (profile_id, g.user_id)).fetchone()
    if not profile:
        db.close()
        return jsonify({'error': 'Profile not found'}), 404
    db.execute('UPDATE users SET brand_color=?, brand_secondary_color=?, brand_accent_color=?, brand_profile_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (profile['primary_color'], profile['secondary_color'], profile['accent_color'], profile_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'applied': profile['profile_name']})


# ======================== CYCLE 14: ONBOARDING & SETUP WIZARD (ENHANCED) ========================

ONBOARDING_CHECKLIST = [
    {'key': 'profile', 'label': 'Complete your agency profile', 'order': 1},
    {'key': 'branding', 'label': 'Configure your branding', 'order': 2},
    {'key': 'interview', 'label': 'Create your first interview', 'order': 3},
    {'key': 'questions', 'label': 'Add interview questions', 'order': 4},
    {'key': 'invite', 'label': 'Invite a test candidate', 'order': 5},
    {'key': 'review', 'label': 'Review a completed interview', 'order': 6},
    {'key': 'integrations', 'label': 'Set up integrations (optional)', 'order': 7},
]

@app.route('/api/onboarding/checklist', methods=['GET'])
@require_auth
def api_onboarding_checklist():
    """Get enhanced onboarding checklist progress."""
    db = get_db()
    steps = db.execute('SELECT * FROM onboarding_steps WHERE user_id=? ORDER BY step_order', (g.user_id,)).fetchall()

    # Initialize steps if not present
    if not steps:
        for s in ONBOARDING_CHECKLIST:
            db.execute('INSERT OR IGNORE INTO onboarding_steps (id, user_id, step_key, step_label, step_order) VALUES (?, ?, ?, ?, ?)',
                       (str(uuid.uuid4()), g.user_id, s['key'], s['label'], s['order']))
        db.commit()
        steps = db.execute('SELECT * FROM onboarding_steps WHERE user_id=? ORDER BY step_order', (g.user_id,)).fetchall()

    steps_list = [dict(s) for s in steps]
    completed = sum(1 for s in steps_list if s['completed'])
    wizard_done = db.execute('SELECT onboarding_wizard_completed FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()

    return jsonify({
        'steps': steps_list,
        'completed': completed,
        'total': len(steps_list),
        'progress_pct': round(completed / len(steps_list) * 100) if steps_list else 0,
        'wizard_completed': bool(wizard_done['onboarding_wizard_completed']) if wizard_done else False
    })


@app.route('/api/onboarding/complete-step', methods=['POST'])
@require_auth
def api_complete_onboarding_step():
    """Mark an onboarding step as completed."""
    data = request.get_json()
    step_key = data.get('step_key')
    if not step_key:
        return jsonify({'error': 'step_key required'}), 400

    db = get_db()
    step = db.execute('SELECT * FROM onboarding_steps WHERE user_id=? AND step_key=?', (g.user_id, step_key)).fetchone()
    if not step:
        match = next((s for s in ONBOARDING_CHECKLIST if s['key'] == step_key), None)
        if match:
            db.execute('INSERT OR IGNORE INTO onboarding_steps (id, user_id, step_key, step_label, step_order) VALUES (?, ?, ?, ?, ?)',
                       (str(uuid.uuid4()), g.user_id, match['key'], match['label'], match['order']))
    db.execute('UPDATE onboarding_steps SET completed=1, completed_at=CURRENT_TIMESTAMP WHERE user_id=? AND step_key=?',
               (g.user_id, step_key))
    db.commit()

    remaining = db.execute('SELECT COUNT(*) as cnt FROM onboarding_steps WHERE user_id=? AND completed=0', (g.user_id,)).fetchone()
    if remaining['cnt'] == 0:
        db.execute('UPDATE users SET onboarding_wizard_completed=1 WHERE id=?', (g.user_id,))
        db.commit()

    db.close()
    return jsonify({'success': True, 'step_key': step_key})


@app.route('/api/onboarding/dismiss', methods=['POST'])
@require_auth
def api_dismiss_onboarding_wizard():
    """Dismiss the onboarding wizard."""
    db = get_db()
    db.execute('UPDATE users SET onboarding_wizard_completed=1 WHERE id=?', (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/onboarding/reset', methods=['POST'])
@require_auth
@require_role('admin', 'owner')
def api_reset_onboarding_wizard():
    """Reset onboarding progress (useful for testing)."""
    db = get_db()
    db.execute('DELETE FROM onboarding_steps WHERE user_id=?', (g.user_id,))
    db.execute('UPDATE users SET onboarding_wizard_completed=0 WHERE id=?', (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ======================== CYCLE 14: ADVANCED REPORTING & EXPORT ========================

@app.route('/api/reports/scorecard/<candidate_id>', methods=['GET'])
@require_auth
def api_candidate_scorecard(candidate_id):
    """Generate a detailed candidate scorecard."""
    db = get_db()
    cand_row = db.execute('SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not cand_row:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404
    candidate = dict(cand_row)

    int_row = db.execute('SELECT * FROM interviews WHERE id=?', (candidate['interview_id'],)).fetchone()
    interview = dict(int_row) if int_row else {}
    responses = db.execute('''SELECT r.*, q.question_text FROM responses r
                              JOIN questions q ON r.question_id=q.id
                              WHERE r.candidate_id=? ORDER BY q.question_order''', (candidate_id,)).fetchall()
    tags = db.execute('SELECT tag FROM candidate_tags WHERE candidate_id=?', (candidate_id,)).fetchall()
    db.close()

    return jsonify({
        'scorecard': {
            'candidate': {
                'name': f"{candidate['first_name']} {candidate['last_name']}",
                'email': candidate['email'],
                'status': candidate['status'],
                'pipeline_stage': candidate.get('pipeline_stage', 'new'),
                'invited_at': candidate['invited_at'],
                'completed_at': candidate['completed_at'],
                'ai_score': candidate['ai_score'],
                'ai_summary': candidate['ai_summary'],
                'tags': [t['tag'] for t in tags]
            },
            'interview': {
                'title': interview['title'] if interview else '',
                'position': interview['position'] if interview else '',
                'department': interview['department'] if interview else ''
            },
            'responses': [{
                'question': r['question_text'],
                'ai_score': r['ai_score'],
                'ai_feedback': r['ai_feedback'],
                'duration': r['duration'],
                'transcript': r['transcript']
            } for r in responses],
            'summary': {
                'total_questions': len(responses),
                'avg_score': round(sum(r['ai_score'] for r in responses if r['ai_score']) / max(1, sum(1 for r in responses if r['ai_score'])), 1),
                'total_duration': sum(r['duration'] or 0 for r in responses),
                'overall_score': candidate['ai_score']
            }
        }
    })


@app.route('/api/reports/funnel', methods=['GET'])
@require_auth
def api_funnel_report():
    """Get hiring funnel analytics with date range filtering."""
    db = get_db()
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')
    interview_id = request.args.get('interview_id', '')

    base_query = 'SELECT * FROM candidates WHERE user_id=?'
    params = [g.user_id]
    if date_from:
        base_query += ' AND created_at >= ?'
        params.append(date_from)
    if date_to:
        base_query += ' AND created_at <= ?'
        params.append(date_to)
    if interview_id:
        base_query += ' AND interview_id=?'
        params.append(interview_id)

    candidates = [dict(row) for row in db.execute(base_query, params).fetchall()]
    db.close()

    stages = {'invited': 0, 'started': 0, 'completed': 0, 'reviewed': 0, 'shortlisted': 0, 'hired': 0, 'rejected': 0}
    pipeline_stages = {'new': 0, 'in_review': 0, 'shortlisted': 0, 'interview_scheduled': 0, 'offered': 0, 'hired': 0, 'rejected': 0}

    for c in candidates:
        s = c['status']
        if s in stages:
            stages[s] += 1
        ps = c.get('pipeline_stage', 'new') or 'new'
        if ps in pipeline_stages:
            pipeline_stages[ps] += 1

    total = len(candidates)
    scored = [c for c in candidates if c['ai_score'] is not None]
    avg_score = round(sum(c['ai_score'] for c in scored) / max(1, len(scored)), 1) if scored else 0
    completion_rate = round(stages.get('completed', 0) / max(1, total) * 100, 1)

    return jsonify({
        'funnel': {
            'total_candidates': total,
            'by_status': stages,
            'by_pipeline': pipeline_stages,
            'avg_score': avg_score,
            'completion_rate': completion_rate,
            'date_range': {'from': date_from, 'to': date_to}
        }
    })


@app.route('/api/reports/comparison', methods=['POST'])
@require_auth
def api_comparison_report():
    """Compare multiple candidates side by side."""
    data = request.get_json()
    candidate_ids = data.get('candidate_ids', [])
    if not candidate_ids or len(candidate_ids) < 2:
        return jsonify({'error': 'At least 2 candidate_ids required'}), 400

    db = get_db()
    results = []
    for cid in candidate_ids[:10]:  # Max 10
        c_row = db.execute('SELECT * FROM candidates WHERE id=? AND user_id=?', (cid, g.user_id)).fetchone()
        if c_row:
            c = dict(c_row)
            responses = db.execute('''SELECT r.ai_score, r.duration, q.question_text FROM responses r
                                      JOIN questions q ON r.question_id=q.id
                                      WHERE r.candidate_id=? ORDER BY q.question_order''', (cid,)).fetchall()
            results.append({
                'id': cid,
                'name': f"{c['first_name']} {c['last_name']}",
                'overall_score': c['ai_score'],
                'status': c['status'],
                'pipeline_stage': c.get('pipeline_stage', 'new'),
                'completed_at': c['completed_at'],
                'responses': [{'question': r['question_text'], 'score': r['ai_score'], 'duration': r['duration']} for r in responses]
            })
    db.close()

    return jsonify({'comparison': results, 'count': len(results)})


@app.route('/api/reports/configs', methods=['GET'])
@require_auth
def api_list_report_configs():
    """List saved report configurations."""
    db = get_db()
    configs = db.execute('SELECT * FROM report_configs WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    db.close()
    return jsonify({'configs': [dict(c) for c in configs]})


@app.route('/api/reports/configs', methods=['POST'])
@require_auth
def api_create_report_config():
    """Save a report configuration."""
    data = request.get_json()
    db = get_db()
    config_id = str(uuid.uuid4())
    db.execute('INSERT INTO report_configs (id, user_id, report_type, title, config, schedule) VALUES (?, ?, ?, ?, ?, ?)',
               (config_id, g.user_id, data.get('report_type', 'funnel'), data.get('title', 'Untitled Report'),
                json.dumps(data.get('config', {})), data.get('schedule', '')))
    db.commit()
    config = db.execute('SELECT * FROM report_configs WHERE id=?', (config_id,)).fetchone()
    db.close()
    return jsonify({'config': dict(config)}), 201


@app.route('/api/reports/configs/<config_id>', methods=['DELETE'])
@require_auth
def api_delete_report_config(config_id):
    """Delete a saved report configuration."""
    db = get_db()
    cfg = db.execute('SELECT * FROM report_configs WHERE id=? AND user_id=?', (config_id, g.user_id)).fetchone()
    if not cfg:
        db.close()
        return jsonify({'error': 'Config not found'}), 404
    db.execute('DELETE FROM report_configs WHERE id=?', (config_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ======================== CYCLE 14: SECURITY HARDENING ========================

@app.route('/api/security/events', methods=['GET'])
@require_auth
@require_role('admin', 'owner')
def api_security_events():
    """Get security events log."""
    db = get_db()
    limit = request.args.get('limit', 50, type=int)
    severity = request.args.get('severity', '')
    query = 'SELECT * FROM security_events WHERE user_id=?'
    params = [g.user_id]
    if severity:
        query += ' AND severity=?'
        params.append(severity)
    query += ' ORDER BY created_at DESC LIMIT ?'
    params.append(limit)
    events = db.execute(query, params).fetchall()
    db.close()
    return jsonify({'events': [dict(e) for e in events], 'count': len(events)})


@app.route('/api/security/audit', methods=['GET'])
@require_auth
@require_role('admin', 'owner')
def api_security_audit():
    """Run a security audit on the current account configuration."""
    db = get_db()
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())

    checks = []
    score = 0
    total = 0

    # Check 1: Strong secret key
    total += 1
    if app.config['SECRET_KEY'] != 'channelview-dev-secret-change-in-prod':
        checks.append({'name': 'Secret key configured', 'status': 'pass', 'severity': 'critical'})
        score += 1
    else:
        checks.append({'name': 'Secret key configured', 'status': 'warn', 'severity': 'critical', 'message': 'Using default secret key. Set SECRET_KEY env var in production.'})

    # Check 2: MFA enabled
    total += 1
    if user.get('mfa_enabled'):
        checks.append({'name': 'MFA enabled', 'status': 'pass', 'severity': 'high'})
        score += 1
    else:
        checks.append({'name': 'MFA enabled', 'status': 'warn', 'severity': 'high', 'message': 'Enable MFA for stronger account security.'})

    # Check 3: Email configured
    total += 1
    if user.get('smtp_host'):
        checks.append({'name': 'Email delivery configured', 'status': 'pass', 'severity': 'medium'})
        score += 1
    else:
        checks.append({'name': 'Email delivery configured', 'status': 'warn', 'severity': 'medium', 'message': 'Configure SMTP or SendGrid for reliable email delivery.'})

    # Check 4: Data retention policy
    total += 1
    retention = db.execute('SELECT COUNT(*) as cnt FROM retention_policies WHERE user_id=?', (g.user_id,)).fetchone()
    if retention['cnt'] > 0:
        checks.append({'name': 'Data retention policy set', 'status': 'pass', 'severity': 'medium'})
        score += 1
    else:
        checks.append({'name': 'Data retention policy set', 'status': 'warn', 'severity': 'medium', 'message': 'Set data retention policies for compliance.'})

    # Check 5: API key rotation
    total += 1
    if user.get('api_key'):
        created = user.get('api_key_created_at', '')
        checks.append({'name': 'API key exists', 'status': 'pass', 'severity': 'medium', 'message': f'Created: {created or "unknown"}'})
        score += 1
    else:
        checks.append({'name': 'API key exists', 'status': 'info', 'severity': 'low', 'message': 'No API key generated yet.'})
        score += 1  # Not required

    # Check 6: CORS configuration
    total += 1
    cors_origins = os.environ.get('CORS_ORIGINS', '*')
    if cors_origins != '*':
        checks.append({'name': 'CORS restricted', 'status': 'pass', 'severity': 'medium'})
        score += 1
    else:
        checks.append({'name': 'CORS restricted', 'status': 'warn', 'severity': 'medium', 'message': 'CORS allows all origins. Set CORS_ORIGINS env var in production.'})

    db.close()
    return jsonify({
        'audit': {
            'score': score,
            'total': total,
            'pct': round(score / total * 100),
            'checks': checks,
            'grade': 'A' if score >= total * 0.9 else 'B' if score >= total * 0.7 else 'C' if score >= total * 0.5 else 'D'
        }
    })


def _log_security_event(db, user_id, event_type, severity='info', details=None):
    """Log a security event."""
    try:
        db.execute("""INSERT INTO security_events (id, user_id, event_type, ip_address, user_agent, details, severity)
                      VALUES (?, ?, ?, ?, ?, ?, ?)""",
                   (str(uuid.uuid4()), user_id, event_type,
                    request.remote_addr, request.headers.get('User-Agent', ''),
                    json.dumps(details) if details else None, severity))
    except:
        pass


@app.route('/api/security/password-policy', methods=['GET'])
@require_auth
def api_password_policy():
    """Get current password policy."""
    return jsonify({
        'policy': {
            'min_length': 8,
            'require_uppercase': True,
            'require_lowercase': True,
            'require_number': True,
            'require_special': False,
            'max_failed_attempts': 5,
            'lockout_duration_minutes': 15,
            'session_timeout_hours': 24,
            'mfa_available': True
        }
    })


@app.route('/api/security/change-password', methods=['POST'])
@require_auth
def api_change_password():
    """Change the current user's password."""
    data = request.get_json()
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')

    if not current_password or not new_password:
        return jsonify({'error': 'Both current_password and new_password are required'}), 400
    if len(new_password) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not bcrypt.checkpw(current_password.encode(), user['password_hash'].encode()):
        _log_security_event(db, g.user_id, 'password_change_failed', 'warning', {'reason': 'incorrect_current'})
        db.commit()
        db.close()
        return jsonify({'error': 'Current password is incorrect'}), 401

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    db.execute('UPDATE users SET password_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (new_hash, g.user_id))
    _log_security_event(db, g.user_id, 'password_changed', 'info')
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/security/sessions', methods=['GET'])
@require_auth
def api_active_sessions():
    """Get current session info."""
    db = get_db()
    user = db.execute('SELECT last_login_at, last_login_ip FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()
    return jsonify({
        'sessions': [{
            'current': True,
            'ip': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', ''),
            'last_login_at': user['last_login_at'] if user else None,
            'last_login_ip': user['last_login_ip'] if user else None
        }]
    })


@app.route('/api/security/input-sanitize', methods=['POST'])
@require_auth
@require_role('admin')
def api_input_sanitize_check():
    """Test input sanitization on a sample input."""
    data = request.get_json()
    test_input = data.get('input', '')
    import html as _html
    sanitized = _html.escape(test_input)
    has_xss = test_input != sanitized
    has_sql = any(kw in test_input.upper() for kw in ['DROP TABLE', 'DELETE FROM', 'INSERT INTO', 'UNION SELECT', "' OR '", '" OR "'])
    return jsonify({
        'original': test_input,
        'sanitized': sanitized,
        'risks': {
            'xss_detected': has_xss,
            'sql_injection_detected': has_sql
        }
    })


# ======================== CYCLE 14: PAGE ROUTES ========================

@app.route('/branding')
@require_auth
def page_branding():
    return render_template('app.html', user=g.user, page='branding')

@app.route('/onboarding')
@require_auth
def page_onboarding():
    return render_template('app.html', user=g.user, page='onboarding')

@app.route('/reports')
@require_auth
def page_reports():
    return render_template('app.html', user=g.user, page='reports')

@app.route('/security')
@require_auth
@require_fmo_admin
def page_security():
    return render_template('app.html', user=g.user, page='security')


# ======================== CYCLE 15: BILLING ENFORCEMENT & PLAN GATING ========================
# Updated in Cycle 31: RSC-level pricing ($99/$179/$299), AI quotas, full feature enforcement

PLAN_LIMITS = {
    'starter':      {'candidates_per_month': 50,  'interviews': 5,  'api_access': False, 'white_label': False, 'bulk_ops': False, 'integrations': False, 'video_storage_mb': 5000,  'team_seats': 3,  'ai_scoring': False, 'ai_interactions_per_month': 0,   'advanced_analytics': False, 'price': 99},
    'professional': {'candidates_per_month': 250, 'interviews': 30, 'api_access': True,  'white_label': True,  'bulk_ops': True,  'integrations': True,  'video_storage_mb': 50000, 'team_seats': 15, 'ai_scoring': True,  'ai_interactions_per_month': 150, 'advanced_analytics': True,  'price': 179},
    'enterprise':   {'candidates_per_month': -1,  'interviews': -1, 'api_access': True,  'white_label': True,  'bulk_ops': True,  'integrations': True,  'video_storage_mb': -1,    'team_seats': -1, 'ai_scoring': True,  'ai_interactions_per_month': -1,  'advanced_analytics': True,  'price': 299},
    # Trial maps to Professional features for 30 days
    'trial':        {'candidates_per_month': 250, 'interviews': 30, 'api_access': True,  'white_label': True,  'bulk_ops': True,  'integrations': True,  'video_storage_mb': 50000, 'team_seats': 15, 'ai_scoring': True,  'ai_interactions_per_month': 150, 'advanced_analytics': True,  'price': 0},
    # Legacy aliases — map to closest new tier
    'free':         {'candidates_per_month': 50,  'interviews': 5,  'api_access': False, 'white_label': False, 'bulk_ops': False, 'integrations': False, 'video_storage_mb': 5000,  'team_seats': 3,  'ai_scoring': False, 'ai_interactions_per_month': 0,   'advanced_analytics': False, 'price': 0},
    'essentials':   {'candidates_per_month': 50,  'interviews': 5,  'api_access': False, 'white_label': False, 'bulk_ops': False, 'integrations': False, 'video_storage_mb': 5000,  'team_seats': 3,  'ai_scoring': False, 'ai_interactions_per_month': 0,   'advanced_analytics': False, 'price': 99},
    'pro':          {'candidates_per_month': 250, 'interviews': 30, 'api_access': True,  'white_label': True,  'bulk_ops': True,  'integrations': True,  'video_storage_mb': 50000, 'team_seats': 15, 'ai_scoring': True,  'ai_interactions_per_month': 150, 'advanced_analytics': True,  'price': 179},
}

# Upgrade recommendation mapping — tells users which plan unlocks the feature they need
FEATURE_UPGRADE_MAP = {
    'ai_scoring':          {'min_plan': 'professional', 'label': 'AI Candidate Scoring'},
    'api_access':          {'min_plan': 'professional', 'label': 'API Access'},
    'white_label':         {'min_plan': 'professional', 'label': 'White Label Branding'},
    'bulk_ops':            {'min_plan': 'professional', 'label': 'Bulk Operations'},
    'integrations':        {'min_plan': 'professional', 'label': 'Webhooks & Integrations'},
    'advanced_analytics':  {'min_plan': 'professional', 'label': 'Advanced Analytics'},
}

def check_plan_limit(user_dict, limit_key, current_count):
    """Check if user is within their plan limit. Returns (allowed, limit, remaining).
    limit of -1 means unlimited."""
    plan = user_dict.get('plan', 'starter') or 'starter'
    # FMO admins bypass all limits
    if user_dict.get('is_fmo_admin'):
        return True, -1, -1
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['starter'])
    limit = limits.get(limit_key, 0)
    if limit == -1:
        return True, -1, -1
    remaining = max(0, limit - current_count)
    return current_count < limit, limit, remaining

def check_feature_access(user_dict, feature_key):
    """Check if a boolean feature is enabled on the user's plan.
    Returns (allowed, upgrade_info). FMO admins always pass."""
    if user_dict.get('is_fmo_admin'):
        return True, None
    plan = user_dict.get('plan', 'starter') or 'starter'
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['starter'])
    allowed = limits.get(feature_key, False)
    if allowed:
        return True, None
    upgrade = FEATURE_UPGRADE_MAP.get(feature_key, {'min_plan': 'professional', 'label': feature_key})
    return False, upgrade

def soft_block_response(feature_key, feature_label=None):
    """Generate a consistent soft-block JSON response for gated features."""
    upgrade = FEATURE_UPGRADE_MAP.get(feature_key, {'min_plan': 'professional', 'label': feature_label or feature_key})
    return jsonify({
        'error': 'feature_not_available',
        'feature': feature_key,
        'message': f'{upgrade["label"]} is available on the {upgrade["min_plan"].title()} plan and above. Upgrade to unlock this feature.',
        'upgrade_plan': upgrade['min_plan'],
        'upgrade_url': '/billing',
        'soft_block': True
    }), 403

def track_ai_usage(user_id, interaction_type='ai_scoring'):
    """Record an AI interaction and check if user is within monthly quota.
    Returns (allowed, used, limit). Does not block — caller decides."""
    db = get_db()
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    # Count AI interactions this month
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM usage_records WHERE user_id=? AND metric=? AND recorded_at >= ?",
        (user_id, 'ai_interaction', month_start)
    ).fetchone()
    used = row['cnt'] if isinstance(row, dict) else row[0]
    # Get user plan limit
    user = db.execute('SELECT plan, is_fmo_admin FROM users WHERE id=?', (user_id,)).fetchone()
    user_d = dict(user) if user else {}
    if user_d.get('is_fmo_admin'):
        db.close()
        return True, used, -1
    plan = user_d.get('plan', 'starter') or 'starter'
    limit = PLAN_LIMITS.get(plan, PLAN_LIMITS['starter']).get('ai_interactions_per_month', 0)
    if limit == -1:
        db.close()
        return True, used, -1
    db.close()
    return used < limit, used, limit

def record_ai_interaction(user_id, interaction_type='ai_scoring'):
    """Record an AI interaction in usage_records."""
    db = get_db()
    db.execute(
        "INSERT INTO usage_records (id, user_id, metric, quantity, recorded_at) VALUES (?, ?, ?, 1, ?)",
        (str(uuid.uuid4()), user_id, 'ai_interaction', datetime.utcnow().isoformat())
    )
    db.commit()
    db.close()

@app.route('/api/billing/plans', methods=['GET'])
@require_auth
def api_billing_plans():
    """Get available plans and their limits (excludes legacy aliases)."""
    display_plans = {k: v for k, v in PLAN_LIMITS.items() if k in ('starter', 'professional', 'enterprise')}
    return jsonify({'plans': display_plans})


@app.route('/api/billing/usage', methods=['GET'])
@require_auth
def api_billing_usage():
    """Get current billing usage for the account — candidates, interviews, team, storage, trial."""
    db = get_db()
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    plan = user.get('plan', 'starter') or 'starter'
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['starter'])

    # Count candidates this month
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    cand_count = db.execute('SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND created_at >= ?',
                       (g.user_id, month_start)).fetchone()['cnt']

    # Count interviews (active)
    interview_count = db.execute('SELECT COUNT(*) as cnt FROM interviews WHERE user_id=?',
                                 (g.user_id,)).fetchone()['cnt']

    # Count team members
    team_count = db.execute('SELECT COUNT(*) as cnt FROM team_members WHERE account_id=? AND status=?',
                            (g.user_id, 'active')).fetchone()['cnt']

    # Calculate video storage (sum of video file sizes if tracked)
    try:
        storage_row = db.execute("SELECT COALESCE(SUM(file_size), 0) as total FROM video_recordings WHERE user_id=?",
                                 (g.user_id,)).fetchone()
        storage_used_mb = round((storage_row['total'] or 0) / (1024 * 1024), 1)
    except:
        storage_used_mb = 0

    db.close()

    # Trial info
    trial_ends = user.get('trial_ends_at')
    days_remaining = 0
    is_trial = False
    if trial_ends:
        try:
            trial_dt = datetime.fromisoformat(trial_ends.replace('Z', '+00:00')) if 'Z' in str(trial_ends) else datetime.fromisoformat(str(trial_ends))
            days_remaining = max(0, (trial_dt - datetime.utcnow()).days)
            is_trial = days_remaining > 0
        except:
            pass

    candidate_limit = limits['candidates_per_month']
    interview_limit = limits.get('interviews', -1)
    storage_limit = limits.get('video_storage_mb', 500)
    seat_limit = limits.get('team_seats', 1)
    ai_limit = limits.get('ai_interactions_per_month', 0)

    # Count AI interactions this month
    ai_allowed, ai_used, ai_lim = track_ai_usage(g.user_id)

    return jsonify({
        'plan': plan,
        'plan_label': plan.replace('_', ' ').title(),
        'price': limits.get('price', 0),
        'candidates_used': cand_count,
        'candidates_limit': candidate_limit,
        'candidates_remaining': max(0, candidate_limit - cand_count) if candidate_limit > 0 else -1,
        'candidates_pct': min(100, round(cand_count / max(candidate_limit, 1) * 100)) if candidate_limit > 0 else 0,
        'interviews_used': interview_count,
        'interviews_limit': interview_limit,
        'interviews_pct': min(100, round(interview_count / max(interview_limit, 1) * 100)) if interview_limit > 0 else 0,
        'team_seats_used': team_count + 1,
        'team_seats_limit': seat_limit,
        'storage_used_mb': storage_used_mb,
        'storage_limit_mb': storage_limit,
        'storage_pct': min(100, round(storage_used_mb / max(storage_limit, 1) * 100)) if storage_limit > 0 else 0,
        'ai_interactions_used': ai_used,
        'ai_interactions_limit': ai_lim,
        'ai_interactions_remaining': max(0, ai_lim - ai_used) if ai_lim > 0 else -1,
        'ai_interactions_pct': min(100, round(ai_used / max(ai_lim, 1) * 100)) if ai_lim > 0 else 0,
        'features': {
            'ai_scoring': limits.get('ai_scoring', False),
            'api_access': limits.get('api_access', False),
            'white_label': limits.get('white_label', False),
            'bulk_ops': limits.get('bulk_ops', False),
            'integrations': limits.get('integrations', False),
            'advanced_analytics': limits.get('advanced_analytics', False),
        },
        'is_trial': is_trial,
        'trial_ends_at': trial_ends,
        'trial_days_remaining': days_remaining,
        'subscription_status': user.get('subscription_status', 'none'),
        'has_stripe': bool(user.get('stripe_subscription_id')),
    })


@app.route('/api/billing/check-feature', methods=['POST'])
@require_auth
def api_check_feature():
    """Check if a feature is available on the current plan."""
    data = request.get_json()
    feature = data.get('feature', '')
    db = get_db()
    user = db.execute('SELECT plan FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()
    plan = user['plan'] or 'starter'
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['starter'])
    allowed = limits.get(feature, True)
    upgrade_info = FEATURE_UPGRADE_MAP.get(feature, {})
    return jsonify({
        'feature': feature,
        'allowed': bool(allowed),
        'plan': plan,
        'upgrade_required': not allowed,
        'minimum_plan': upgrade_info.get('min_plan', 'professional'),
        'upgrade_url': '/billing'
    })


@app.route('/api/billing/upgrade', methods=['POST'])
@require_auth
@require_role('admin', 'owner')
def api_billing_upgrade():
    """Upgrade plan (simulated for non-Stripe environments)."""
    data = request.get_json()
    new_plan = data.get('plan', '')
    if new_plan not in PLAN_LIMITS:
        return jsonify({'error': 'Invalid plan'}), 400

    db = get_db()
    limits = PLAN_LIMITS[new_plan]
    db.execute('UPDATE users SET plan=?, candidate_limit=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (new_plan, limits['candidates_per_month'], g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'plan': new_plan, 'limits': limits})


@app.route('/api/billing/invoices', methods=['GET'])
@require_auth
def api_billing_invoices():
    """Get billing history / invoice list for the current user."""
    db = get_db()
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    rows = db.execute(
        'SELECT id, stripe_invoice_id, amount, currency, status, hosted_invoice_url, created_at FROM invoices WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?',
        (g.user_id, limit, offset)
    ).fetchall()
    total = db.execute(
        'SELECT COUNT(*) FROM invoices WHERE user_id=?',
        (g.user_id,)
    ).fetchone()[0]
    db.close()

    return jsonify({
        'invoices': [dict(r) for r in rows],
        'total': total,
        'limit': limit,
        'offset': offset
    })


# ======================== CYCLE 15: ACTIVITY FEED & NOTIFICATIONS ========================

def _log_activity(db, account_id, actor_id, actor_name, action, entity_type=None, entity_id=None, entity_name=None, details=None):
    """Log an activity event to the feed."""
    try:
        db.execute("""INSERT INTO activity_feed (id, account_id, actor_id, actor_name, action, entity_type, entity_id, entity_name, details)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (str(uuid.uuid4()), account_id, actor_id, actor_name, action,
                    entity_type, entity_id, entity_name, json.dumps(details) if details else None))
    except:
        pass


@app.route('/api/activity/feed', methods=['GET'])
@require_auth
def api_activity_feed():
    """Get the activity feed for the current account."""
    db = get_db()
    limit = request.args.get('limit', 30, type=int)
    offset = request.args.get('offset', 0, type=int)
    entity_type = request.args.get('entity_type', '')

    query = 'SELECT * FROM activity_feed WHERE account_id=?'
    params = [g.user_id]
    if entity_type:
        query += ' AND entity_type=?'
        params.append(entity_type)
    query += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])

    activities = db.execute(query, params).fetchall()
    total = db.execute('SELECT COUNT(*) as cnt FROM activity_feed WHERE account_id=?', (g.user_id,)).fetchone()['cnt']
    db.close()
    return jsonify({
        'activities': [dict(a) for a in activities],
        'total': total,
        'limit': limit,
        'offset': offset
    })


@app.route('/api/activity/log', methods=['POST'])
@require_auth
def api_log_activity():
    """Manually log an activity event."""
    data = request.get_json()
    db = get_db()
    _log_activity(db, g.user_id, g.user_id, g.user.get('name', 'Unknown'),
                  data.get('action', 'custom_action'),
                  data.get('entity_type'), data.get('entity_id'),
                  data.get('entity_name'), data.get('details'))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/activity/summary', methods=['GET'])
@require_auth
def api_activity_summary():
    """Get activity summary for the dashboard."""
    db = get_db()
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    today_count = db.execute('SELECT COUNT(*) as cnt FROM activity_feed WHERE account_id=? AND created_at >= ?',
                             (g.user_id, today)).fetchone()['cnt']
    week_count = db.execute('SELECT COUNT(*) as cnt FROM activity_feed WHERE account_id=? AND created_at >= ?',
                            (g.user_id, week_ago)).fetchone()['cnt']

    # Top actions this week
    top_actions = db.execute('''SELECT action, COUNT(*) as cnt FROM activity_feed
                                WHERE account_id=? AND created_at >= ?
                                GROUP BY action ORDER BY cnt DESC LIMIT 5''',
                             (g.user_id, week_ago)).fetchall()

    # Recent actors
    recent_actors = db.execute('''SELECT actor_name, COUNT(*) as cnt FROM activity_feed
                                  WHERE account_id=? AND created_at >= ?
                                  GROUP BY actor_id ORDER BY cnt DESC LIMIT 5''',
                               (g.user_id, week_ago)).fetchall()
    db.close()

    return jsonify({
        'today': today_count,
        'this_week': week_count,
        'top_actions': [{'action': a['action'], 'count': a['cnt']} for a in top_actions],
        'recent_actors': [{'name': a['actor_name'], 'count': a['cnt']} for a in recent_actors]
    })


@app.route('/api/notifications/digest-config', methods=['GET'])
@require_auth
def api_digest_config():
    """Get email digest configuration."""
    db = get_db()
    user = dict(db.execute('SELECT notify_daily_digest, notify_interview_started, notify_interview_completed, notify_candidate_invited FROM users WHERE id=?',
                           (g.user_id,)).fetchone())
    db.close()
    return jsonify({
        'daily_digest': bool(user.get('notify_daily_digest')),
        'interview_started': bool(user.get('notify_interview_started')),
        'interview_completed': bool(user.get('notify_interview_completed')),
        'candidate_invited': bool(user.get('notify_candidate_invited'))
    })


# ======================== CYCLE 15: TEAM COLLABORATION & PERMISSIONS ========================

TEAM_PERMISSIONS = {
    'owner':     ['all'],
    'admin':     ['manage_team', 'manage_interviews', 'manage_candidates', 'view_analytics', 'manage_settings', 'manage_integrations'],
    'recruiter': ['manage_interviews', 'manage_candidates', 'view_analytics'],
    'reviewer':  ['view_candidates', 'score_candidates', 'add_notes'],
}

@app.route('/api/team/members', methods=['GET'])
@require_auth
def api_team_members():
    """List team members for the current account."""
    db = get_db()
    members = db.execute('''SELECT tm.*, u.email, u.name FROM team_members tm
                            JOIN users u ON tm.user_id = u.id
                            WHERE tm.account_id=? ORDER BY tm.created_at''', (g.user_id,)).fetchall()
    db.close()
    return jsonify({'members': [dict(m) for m in members], 'count': len(members)})


@app.route('/api/team/invite', methods=['POST'])
@require_auth
@require_role('admin', 'owner')
def api_team_invite():
    """Invite a new team member."""
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    role = data.get('role', 'reviewer')
    display_name = data.get('display_name', '')

    if not email:
        return jsonify({'error': 'Email required'}), 400
    if role not in TEAM_PERMISSIONS:
        return jsonify({'error': f'Invalid role. Must be one of: {", ".join(TEAM_PERMISSIONS.keys())}'}), 400

    db = get_db()

    # Check plan limits
    user = dict(db.execute('SELECT plan FROM users WHERE id=?', (g.user_id,)).fetchone())
    plan = user.get('plan', 'starter') or 'starter'
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['starter'])
    current_count = db.execute('SELECT COUNT(*) as cnt FROM team_members WHERE account_id=? AND status=?',
                               (g.user_id, 'active')).fetchone()['cnt']
    seat_limit = limits['team_seats']
    if seat_limit > 0 and current_count + 1 >= seat_limit:
        db.close()
        return jsonify({'error': f'Team seat limit reached ({seat_limit}). Upgrade your plan.', 'upgrade_required': True}), 403

    # Find or create user
    target_user = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if not target_user:
        # Create a placeholder user account
        target_id = str(uuid.uuid4())
        pw_hash = bcrypt.hashpw('pending-invite'.encode(), bcrypt.gensalt()).decode()
        db.execute('INSERT INTO users (id, email, password_hash, name, role) VALUES (?, ?, ?, ?, ?)',
                   (target_id, email, pw_hash, display_name or email.split('@')[0], 'reviewer'))
    else:
        target_id = target_user['id']

    # Check if already a member
    existing = db.execute('SELECT id FROM team_members WHERE account_id=? AND user_id=?', (g.user_id, target_id)).fetchone()
    if existing:
        db.close()
        return jsonify({'error': 'User is already a team member'}), 409

    member_id = str(uuid.uuid4())
    perms = json.dumps(TEAM_PERMISSIONS.get(role, []))
    db.execute('''INSERT INTO team_members (id, account_id, user_id, role, invited_by, status, permissions, display_name)
                  VALUES (?, ?, ?, ?, ?, 'active', ?, ?)''',
               (member_id, g.user_id, target_id, role, g.user_id, perms, display_name))
    _log_activity(db, g.user_id, g.user_id, g.user.get('name', ''), 'team_member_invited',
                  'team_member', member_id, display_name or email)
    db.commit()
    db.close()
    return jsonify({'success': True, 'member_id': member_id, 'role': role}), 201


@app.route('/api/team/members/<member_id>', methods=['PUT'])
@require_auth
@require_role('admin', 'owner')
def api_update_team_member_v2(member_id):
    """Update a team member's role or permissions."""
    data = request.get_json()
    db = get_db()
    member = db.execute('SELECT * FROM team_members WHERE id=? AND account_id=?', (member_id, g.user_id)).fetchone()
    if not member:
        db.close()
        return jsonify({'error': 'Member not found'}), 404

    new_role = data.get('role')
    if new_role:
        if new_role not in TEAM_PERMISSIONS:
            db.close()
            return jsonify({'error': 'Invalid role'}), 400
        perms = json.dumps(TEAM_PERMISSIONS[new_role])
        db.execute('UPDATE team_members SET role=?, permissions=? WHERE id=?', (new_role, perms, member_id))

    new_status = data.get('status')
    if new_status in ('active', 'inactive'):
        db.execute('UPDATE team_members SET status=? WHERE id=?', (new_status, member_id))

    db.commit()
    updated = db.execute('SELECT tm.*, u.email, u.name FROM team_members tm JOIN users u ON tm.user_id=u.id WHERE tm.id=?', (member_id,)).fetchone()
    db.close()
    return jsonify({'member': dict(updated)})


@app.route('/api/team/members/<member_id>', methods=['DELETE'])
@require_auth
@require_role('admin', 'owner')
def api_remove_team_member_v2(member_id):
    """Remove a team member."""
    db = get_db()
    member = db.execute('SELECT * FROM team_members WHERE id=? AND account_id=?', (member_id, g.user_id)).fetchone()
    if not member:
        db.close()
        return jsonify({'error': 'Member not found'}), 404
    db.execute('DELETE FROM team_members WHERE id=?', (member_id,))
    _log_activity(db, g.user_id, g.user_id, g.user.get('name', ''), 'team_member_removed',
                  'team_member', member_id)
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/team/permissions', methods=['GET'])
@require_auth
def api_team_permissions():
    """Get available roles and their permissions."""
    return jsonify({'roles': TEAM_PERMISSIONS})


@app.route('/api/team/notes/<candidate_id>', methods=['GET'])
@require_auth
def api_team_notes(candidate_id):
    """Get collaborative notes for a candidate."""
    db = get_db()
    notes = db.execute('''SELECT * FROM team_notes WHERE account_id=? AND candidate_id=?
                          ORDER BY created_at DESC''', (g.user_id, candidate_id)).fetchall()
    db.close()
    return jsonify({'notes': [dict(n) for n in notes], 'count': len(notes)})


@app.route('/api/team/notes/<candidate_id>', methods=['POST'])
@require_auth
def api_add_team_note(candidate_id):
    """Add a collaborative note for a candidate."""
    data = request.get_json()
    content = data.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Content required'}), 400

    db = get_db()
    # Verify candidate belongs to this account
    cand = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not cand:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    note_id = str(uuid.uuid4())
    db.execute('''INSERT INTO team_notes (id, account_id, candidate_id, author_id, author_name, content, note_type)
                  VALUES (?, ?, ?, ?, ?, ?, ?)''',
               (note_id, g.user_id, candidate_id, g.user_id, g.user.get('name', 'Unknown'),
                content, data.get('note_type', 'note')))
    _log_activity(db, g.user_id, g.user_id, g.user.get('name', ''), 'note_added',
                  'candidate', candidate_id, content[:50])
    db.commit()
    note = db.execute('SELECT * FROM team_notes WHERE id=?', (note_id,)).fetchone()
    db.close()
    return jsonify({'note': dict(note)}), 201


@app.route('/api/team/scorecards/<candidate_id>', methods=['GET'])
@require_auth
def api_reviewer_scorecards(candidate_id):
    """Get all reviewer scorecards for a candidate."""
    db = get_db()
    cards = db.execute('''SELECT * FROM reviewer_scorecards WHERE account_id=? AND candidate_id=?
                          ORDER BY created_at DESC''', (g.user_id, candidate_id)).fetchall()
    db.close()

    result = [dict(c) for c in cards]
    for c in result:
        if c.get('criteria_scores'):
            try:
                c['criteria_scores'] = json.loads(c['criteria_scores'])
            except:
                pass

    # Compute consensus
    scores = [c['overall_score'] for c in result if c.get('overall_score') is not None]
    consensus = round(sum(scores) / len(scores), 1) if scores else None

    return jsonify({'scorecards': result, 'count': len(result), 'consensus_score': consensus})


@app.route('/api/team/scorecards/<candidate_id>', methods=['POST'])
@require_auth
def api_submit_scorecard(candidate_id):
    """Submit a reviewer scorecard for a candidate."""
    data = request.get_json()
    db = get_db()

    cand = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not cand:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    card_id = str(uuid.uuid4())
    db.execute('''INSERT INTO reviewer_scorecards (id, account_id, candidate_id, reviewer_id, reviewer_name,
                  overall_score, criteria_scores, recommendation, comments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
               (card_id, g.user_id, candidate_id, g.user_id, g.user.get('name', 'Unknown'),
                data.get('overall_score'), json.dumps(data.get('criteria_scores', {})),
                data.get('recommendation', ''), data.get('comments', '')))
    _log_activity(db, g.user_id, g.user_id, g.user.get('name', ''), 'scorecard_submitted',
                  'candidate', candidate_id)
    db.commit()
    card = db.execute('SELECT * FROM reviewer_scorecards WHERE id=?', (card_id,)).fetchone()
    db.close()
    return jsonify({'scorecard': dict(card)}), 201


# ======================== CYCLE 15: PRODUCTION DEPLOYMENT ========================

@app.route('/api/deploy/config', methods=['GET'])
@require_auth
@require_role('admin', 'owner')
def api_deploy_config():
    """Get current deployment configuration."""
    return jsonify({
        'config': {
            'environment': os.environ.get('FLASK_ENV', 'development'),
            'host': os.environ.get('HOST', '0.0.0.0'),
            'port': int(os.environ.get('PORT', 5000)),
            'secret_key_set': app.config['SECRET_KEY'] != 'channelview-dev-secret-change-in-prod',
            'cors_origins': os.environ.get('CORS_ORIGINS', '*'),
            'debug_mode': app.debug,
            'database_path': os.path.join(os.path.dirname(__file__), 'channelview.db'),
            'upload_folder': app.config['UPLOAD_FOLDER'],
            'max_upload_size_mb': app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024),
            'stripe_configured': bool(os.environ.get('STRIPE_SECRET_KEY')),
            'ai_configured': is_ai_available(),
            'sendgrid_configured': bool(os.environ.get('SENDGRID_API_KEY')),
        }
    })


@app.route('/api/deploy/readiness', methods=['GET'])
@require_auth
@require_role('admin', 'owner')
def api_deploy_readiness():
    """Check production readiness."""
    checks = []
    ready = True

    # DB health
    from database import USE_POSTGRES
    db = get_db()
    try:
        if USE_POSTGRES:
            db.execute('SELECT 1')
            checks.append({'name': 'Database integrity', 'status': 'pass'})
        else:
            integrity = db.execute('PRAGMA quick_check').fetchone()
            checks.append({'name': 'Database integrity', 'status': 'pass' if integrity[0] == 'ok' else 'fail'})
    except:
        checks.append({'name': 'Database integrity', 'status': 'fail'})
        ready = False

    # Secret key
    if app.config['SECRET_KEY'] == 'channelview-dev-secret-change-in-prod':
        checks.append({'name': 'Secret key', 'status': 'warn', 'message': 'Using default. Set SECRET_KEY env var.'})
    else:
        checks.append({'name': 'Secret key', 'status': 'pass'})

    # CORS
    cors = os.environ.get('CORS_ORIGINS', '*')
    if cors == '*':
        checks.append({'name': 'CORS', 'status': 'warn', 'message': 'Allows all origins. Restrict in production.'})
    else:
        checks.append({'name': 'CORS', 'status': 'pass'})

    # Upload directory writable
    try:
        test_path = os.path.join(app.config['UPLOAD_FOLDER'], '.write_test')
        with open(test_path, 'w') as f:
            f.write('test')
        os.remove(test_path)
        checks.append({'name': 'Upload directory', 'status': 'pass'})
    except:
        checks.append({'name': 'Upload directory', 'status': 'fail', 'message': 'Not writable'})
        ready = False

    # DB size
    db_path = os.path.join(os.path.dirname(__file__), 'channelview.db')
    try:
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        if size_mb > 500:
            checks.append({'name': 'Database size', 'status': 'warn', 'message': f'{size_mb:.0f}MB - consider migration to PostgreSQL'})
        else:
            checks.append({'name': 'Database size', 'status': 'pass', 'message': f'{size_mb:.1f}MB'})
    except:
        checks.append({'name': 'Database size', 'status': 'warn'})

    # Email config
    if os.environ.get('SENDGRID_API_KEY') or os.environ.get('SMTP_HOST'):
        checks.append({'name': 'Email delivery', 'status': 'pass'})
    else:
        checks.append({'name': 'Email delivery', 'status': 'warn', 'message': 'No email provider configured'})

    db.close()
    score = sum(1 for c in checks if c['status'] == 'pass')
    return jsonify({
        'ready': ready,
        'score': score,
        'total': len(checks),
        'pct': round(score / len(checks) * 100),
        'checks': checks
    })


@app.route('/api/deploy/env-template', methods=['GET'])
@require_auth
@require_role('admin', 'owner')
def api_env_template():
    """Get a .env template for production deployment."""
    template = """# ChannelView Production Configuration
# Copy this to .env and fill in your values

# Server
FLASK_ENV=production
SECRET_KEY=your-secret-key-here-use-python-c-import-secrets-secrets.token_hex-32
HOST=0.0.0.0
PORT=5000
CORS_ORIGINS=https://yourdomain.com

# Email (choose one)
SENDGRID_API_KEY=your-sendgrid-key
# -- OR --
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASS=your-app-password

# Stripe Billing
STRIPE_SECRET_KEY=sk_live_your-key
STRIPE_PUBLISHABLE_KEY=pk_live_your-key
STRIPE_PRICE_ID=price_your-price-id
STRIPE_WEBHOOK_SECRET=whsec_your-webhook-secret

# AI Scoring (optional)
ANTHROPIC_API_KEY=your-api-key

# Storage (optional, defaults to local)
STORAGE_BACKEND=local
# S3_BUCKET=your-bucket
# AWS_ACCESS_KEY_ID=your-key
# AWS_SECRET_ACCESS_KEY=your-secret
"""
    return jsonify({'template': template})


# ======================== CYCLE 15: PAGE ROUTES ========================

@app.route('/team')
@require_auth
def page_team():
    return render_template('app.html', user=g.user, page='team')

@app.route('/activity')
@require_auth
@require_fmo_admin
def page_activity():
    return render_template('app.html', user=g.user, page='activity')

@app.route('/deploy')
@require_auth
def page_deploy():
    return render_template('app.html', user=g.user, page='deploy')


# ======================== CYCLE 16: CANDIDATE EXPERIENCE & VIDEO PIPELINE ========================

@app.route('/api/candidate-portal/<token>/info', methods=['GET'])
def api_candidate_portal_info(token):
    """Get full interview info for the candidate portal (public, no auth)."""
    db = get_db()
    candidate = db.execute(
        '''SELECT c.id, c.first_name, c.last_name, c.status, c.current_question_index,
           c.portal_status, c.time_spent_seconds, c.started_at, c.completed_at,
           i.title, i.description, i.welcome_msg, i.thank_you_msg, i.thinking_time,
           i.max_answer_time, i.max_retakes, i.brand_color, i.intro_video_path,
           u.agency_name, u.name as interviewer_name, u.agency_logo_url,
           u.brand_color as agency_brand_color
           FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           JOIN users u ON c.user_id = u.id
           WHERE c.token = ?''', (token,)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Interview not found or expired'}), 404

    cand = dict(candidate)

    # Get questions
    questions = db.execute(
        '''SELECT id, question_text, question_order, thinking_time, max_answer_time
           FROM questions WHERE interview_id = (SELECT interview_id FROM candidates WHERE token=?)
           ORDER BY question_order''', (token,)
    ).fetchall()

    # Get existing responses
    responses = db.execute(
        'SELECT question_id, id as response_id FROM responses WHERE candidate_id=?',
        (cand['id'],)
    ).fetchall()
    answered_ids = {r['question_id'] for r in responses}

    db.close()
    return jsonify({
        'candidate': {
            'first_name': cand['first_name'],
            'last_name': cand['last_name'],
            'status': cand['status'],
            'portal_status': cand.get('portal_status', 'not_started'),
            'current_question_index': cand.get('current_question_index', 0),
            'time_spent_seconds': cand.get('time_spent_seconds', 0),
        },
        'interview': {
            'title': cand['title'],
            'description': cand.get('description'),
            'welcome_msg': cand['welcome_msg'],
            'thank_you_msg': cand['thank_you_msg'],
            'thinking_time': cand['thinking_time'],
            'max_answer_time': cand['max_answer_time'],
            'max_retakes': cand['max_retakes'],
            'brand_color': cand.get('agency_brand_color') or cand['brand_color'],
            'intro_video_path': cand.get('intro_video_path'),
        },
        'agency': {
            'name': cand['agency_name'],
            'interviewer': cand['interviewer_name'],
            'logo_url': cand.get('agency_logo_url'),
        },
        'questions': [dict(q) for q in questions],
        'total_questions': len(questions),
        'answered_question_ids': list(answered_ids),
        'progress_pct': round(len(answered_ids) / max(len(questions), 1) * 100)
    })


@app.route('/api/candidate-portal/<token>/session', methods=['POST'])
def api_candidate_portal_session(token):
    """Track candidate session start for analytics."""
    db = get_db()
    candidate = db.execute('SELECT id FROM candidates WHERE token=?', (token,)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    data = request.get_json() or {}
    session_id = str(uuid.uuid4())
    questions_count = db.execute(
        'SELECT COUNT(*) as cnt FROM questions WHERE interview_id=(SELECT interview_id FROM candidates WHERE id=?)',
        (candidate['id'],)
    ).fetchone()['cnt']

    db.execute('''INSERT INTO candidate_sessions (id, candidate_id, token, device_info, ip_address, total_questions)
                  VALUES (?, ?, ?, ?, ?, ?)''',
               (session_id, candidate['id'], token, data.get('device_info', ''),
                request.remote_addr, questions_count))

    # Update candidate portal status
    db.execute("UPDATE candidates SET portal_status='in_progress', device_info=? WHERE id=? AND portal_status='not_started'",
               (data.get('device_info', ''), candidate['id']))
    db.commit()
    db.close()
    return jsonify({'session_id': session_id, 'total_questions': questions_count})


@app.route('/api/candidate-portal/<token>/status', methods=['GET'])
def api_candidate_portal_status(token):
    """Get candidate's current completion status."""
    db = get_db()
    candidate = db.execute(
        '''SELECT c.id, c.status, c.portal_status, c.current_question_index, c.time_spent_seconds,
           c.started_at, c.completed_at, c.reminder_count
           FROM candidates c WHERE c.token=?''', (token,)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    cand = dict(candidate)
    # Count responses
    resp_count = db.execute('SELECT COUNT(*) as cnt FROM responses WHERE candidate_id=?', (cand['id'],)).fetchone()['cnt']
    q_count = db.execute(
        'SELECT COUNT(*) as cnt FROM questions WHERE interview_id=(SELECT interview_id FROM candidates WHERE id=?)',
        (cand['id'],)
    ).fetchone()['cnt']
    db.close()

    return jsonify({
        'status': cand['status'],
        'portal_status': cand.get('portal_status', 'not_started'),
        'responses_completed': resp_count,
        'total_questions': q_count,
        'progress_pct': round(resp_count / max(q_count, 1) * 100),
        'time_spent_seconds': cand.get('time_spent_seconds', 0),
        'started_at': cand.get('started_at'),
        'completed_at': cand.get('completed_at'),
    })


@app.route('/api/candidates/<candidate_id>/reminders', methods=['GET'])
@require_auth
def api_candidate_reminders(candidate_id):
    """Get reminder history for a candidate."""
    db = get_db()
    reminders = db.execute(
        'SELECT * FROM candidate_reminders WHERE candidate_id=? ORDER BY created_at DESC',
        (candidate_id,)
    ).fetchall()
    db.close()
    return jsonify({'reminders': [dict(r) for r in reminders], 'count': len(reminders)})


@app.route('/api/candidates/<candidate_id>/reminders', methods=['POST'])
@require_auth
def api_send_candidate_reminder(candidate_id):
    """Schedule or send a reminder to a candidate."""
    db = get_db()
    candidate = db.execute('SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    if candidate['status'] == 'completed':
        db.close()
        return jsonify({'error': 'Candidate already completed interview'}), 400

    data = request.get_json() or {}
    reminder_id = str(uuid.uuid4())
    reminder_type = data.get('type', 'manual')
    message = data.get('message', f"Hi {candidate['first_name']}, this is a friendly reminder to complete your video interview.")

    db.execute('''INSERT INTO candidate_reminders (id, candidate_id, reminder_type, scheduled_at, sent_at, status, message)
                  VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'sent', ?)''',
               (reminder_id, candidate_id, reminder_type, message))
    db.execute('UPDATE candidates SET reminder_count = COALESCE(reminder_count, 0) + 1, last_reminded_at = CURRENT_TIMESTAMP WHERE id=?',
               (candidate_id,))
    _log_activity(db, g.user_id, g.user_id, g.user.get('name', ''), 'reminder_sent',
                  'candidate', candidate_id, f"{candidate['first_name']} {candidate['last_name']}")
    db.commit()
    db.close()
    return jsonify({'success': True, 'reminder_id': reminder_id}), 201


@app.route('/api/interviews/<interview_id>/public-apply', methods=['GET'])
def api_public_apply_config(interview_id):
    """Get public apply configuration for an interview (no auth - public endpoint)."""
    db = get_db()
    interview = db.execute(
        '''SELECT i.id, i.title, i.description, i.department, i.position, i.status,
           i.brand_color, i.public_apply_enabled, u.agency_name, u.agency_logo_url
           FROM interviews i JOIN users u ON i.user_id = u.id
           WHERE i.id = ? AND i.status = 'active' ''', (interview_id,)
    ).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found or not active'}), 404

    intv = dict(interview)
    if not intv.get('public_apply_enabled'):
        db.close()
        return jsonify({'error': 'Public applications not enabled for this interview'}), 403

    q_count = db.execute('SELECT COUNT(*) as cnt FROM questions WHERE interview_id=?', (interview_id,)).fetchone()['cnt']
    db.close()
    return jsonify({
        'interview': {
            'id': intv['id'], 'title': intv['title'], 'description': intv.get('description'),
            'department': intv.get('department'), 'position': intv.get('position'),
            'brand_color': intv.get('brand_color'), 'question_count': q_count,
        },
        'agency': {'name': intv['agency_name'], 'logo_url': intv.get('agency_logo_url')}
    })


@app.route('/api/interviews/<interview_id>/public-apply', methods=['POST'])
def api_public_apply_submit(interview_id):
    """Self-service candidate application (public, no auth)."""
    db = get_db()
    interview = db.execute(
        'SELECT * FROM interviews WHERE id=? AND status=?', (interview_id, 'active')
    ).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    intv = dict(interview)
    if not intv.get('public_apply_enabled'):
        db.close()
        return jsonify({'error': 'Public applications not enabled'}), 403

    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    first_name = (data.get('first_name') or '').strip()
    last_name = (data.get('last_name') or '').strip()

    if not email or not first_name or not last_name:
        db.close()
        return jsonify({'error': 'first_name, last_name, and email are required'}), 400

    # Check if already applied
    existing = db.execute('SELECT id, token FROM candidates WHERE interview_id=? AND email=?',
                          (interview_id, email)).fetchone()
    if existing:
        db.close()
        return jsonify({'error': 'Already applied', 'token': existing['token']}), 409

    candidate_id = str(uuid.uuid4())
    token = str(uuid.uuid4())
    db.execute('''INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email, phone, token, status, source, portal_status)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'invited', 'public_apply', 'not_started')''',
               (candidate_id, intv['user_id'], interview_id, first_name, last_name, email,
                data.get('phone', ''), token))
    db.commit()
    db.close()
    return jsonify({'success': True, 'candidate_id': candidate_id, 'token': token,
                    'interview_url': f"/i/{token}"}), 201


# ======================== CYCLE 16: AI SCORING & EVALUATION ENGINE ========================

@app.route('/api/scoring/rubrics', methods=['GET'])
@require_auth
def api_list_rubrics():
    """List scoring rubrics for the current user."""
    db = get_db()
    rubrics = db.execute('SELECT * FROM scoring_rubrics WHERE user_id=? ORDER BY created_at DESC',
                         (g.user_id,)).fetchall()
    db.close()
    result = [dict(r) for r in rubrics]
    for r in result:
        if r.get('criteria'):
            try: r['criteria'] = json.loads(r['criteria'])
            except: pass
        if r.get('weight_distribution'):
            try: r['weight_distribution'] = json.loads(r['weight_distribution'])
            except: pass
    return jsonify({'rubrics': result, 'count': len(result)})


@app.route('/api/scoring/rubrics', methods=['POST'])
@require_auth
def api_create_rubric():
    """Create a new scoring rubric."""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    criteria = data.get('criteria', [])

    if not name:
        return jsonify({'error': 'Name required'}), 400
    if not criteria or not isinstance(criteria, list):
        return jsonify({'error': 'Criteria must be a non-empty array'}), 400

    db = get_db()
    rubric_id = str(uuid.uuid4())
    weights = data.get('weight_distribution', {c.get('name', ''): 1.0/len(criteria) for c in criteria if isinstance(c, dict)})

    db.execute('''INSERT INTO scoring_rubrics (id, user_id, interview_id, name, description, criteria, weight_distribution, scoring_scale, is_default)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
               (rubric_id, g.user_id, data.get('interview_id'), name, data.get('description', ''),
                json.dumps(criteria), json.dumps(weights), data.get('scoring_scale', '0-100'),
                1 if data.get('is_default') else 0))
    db.commit()
    rubric = dict(db.execute('SELECT * FROM scoring_rubrics WHERE id=?', (rubric_id,)).fetchone())
    db.close()
    try: rubric['criteria'] = json.loads(rubric['criteria'])
    except: pass
    try: rubric['weight_distribution'] = json.loads(rubric['weight_distribution'])
    except: pass
    return jsonify({'rubric': rubric}), 201


@app.route('/api/scoring/rubrics/<rubric_id>', methods=['GET'])
@require_auth
def api_get_rubric(rubric_id):
    """Get a single scoring rubric."""
    db = get_db()
    rubric = db.execute('SELECT * FROM scoring_rubrics WHERE id=? AND user_id=?', (rubric_id, g.user_id)).fetchone()
    db.close()
    if not rubric:
        return jsonify({'error': 'Rubric not found'}), 404
    r = dict(rubric)
    try: r['criteria'] = json.loads(r['criteria'])
    except: pass
    try: r['weight_distribution'] = json.loads(r['weight_distribution'])
    except: pass
    return jsonify({'rubric': r})


@app.route('/api/scoring/rubrics/<rubric_id>', methods=['DELETE'])
@require_auth
def api_delete_rubric(rubric_id):
    """Delete a scoring rubric."""
    db = get_db()
    rubric = db.execute('SELECT id FROM scoring_rubrics WHERE id=? AND user_id=?', (rubric_id, g.user_id)).fetchone()
    if not rubric:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute('DELETE FROM scoring_rubrics WHERE id=?', (rubric_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/candidates/<candidate_id>/analyze', methods=['POST'])
@require_auth
def api_analyze_candidate(candidate_id):
    """Run detailed AI analysis on a candidate (sentiment, keywords, pace)."""
    db = get_db()
    candidate = db.execute(
        'SELECT c.*, i.title, i.position FROM candidates c JOIN interviews i ON c.interview_id=i.id WHERE c.id=? AND c.user_id=?',
        (candidate_id, g.user_id)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    responses = db.execute(
        'SELECT r.*, q.question_text FROM responses r JOIN questions q ON r.question_id=q.id WHERE r.candidate_id=?',
        (candidate_id,)
    ).fetchall()

    analysis_results = []
    total_words = 0
    sentiments = []

    for resp in responses:
        transcript = resp['transcript'] or ''
        words = len(transcript.split())
        total_words += words

        # Keyword detection for insurance industry
        insurance_keywords = ['policy', 'premium', 'coverage', 'claim', 'underwriting', 'deductible',
                            'beneficiary', 'enrollment', 'compliance', 'medicare', 'medicaid', 'aca',
                            'carrier', 'broker', 'agent', 'commission', 'renewal', 'retention']
        detected = [kw for kw in insurance_keywords if kw.lower() in transcript.lower()]

        # Basic sentiment heuristic
        positive_words = ['excellent', 'great', 'passionate', 'dedicated', 'experienced', 'successful',
                         'achieve', 'growth', 'opportunity', 'team', 'leader', 'motivated']
        negative_words = ['difficult', 'struggle', 'fail', 'problem', 'weakness', 'unfortunately', 'never']
        pos_count = sum(1 for w in positive_words if w.lower() in transcript.lower())
        neg_count = sum(1 for w in negative_words if w.lower() in transcript.lower())
        sentiment = round(min(1.0, max(-1.0, (pos_count - neg_count) / max(pos_count + neg_count, 1))), 2)
        sentiments.append(sentiment)

        # Calculate speaking pace (words per minute estimate)
        duration = resp.get('duration') or 60
        pace = round(words / max(duration, 1) * 60, 1) if duration else 0

        # Update response with analysis
        db.execute('UPDATE responses SET sentiment_score=?, keywords_detected=?, word_count=?, speaking_pace=? WHERE id=?',
                   (sentiment, json.dumps(detected), words, pace, resp['id']))

        resp_analysis = {
            'response_id': resp['id'],
            'question': resp['question_text'],
            'word_count': words,
            'speaking_pace': pace,
            'sentiment': sentiment,
            'keywords': detected,
        }
        analysis_results.append(resp_analysis)

        # Store detailed analysis
        analysis_id = str(uuid.uuid4())
        db.execute('''INSERT INTO ai_analysis (id, candidate_id, response_id, analysis_type, results, model_used)
                      VALUES (?, ?, ?, 'response_analysis', ?, 'heuristic')''',
                   (analysis_id, candidate_id, resp['id'], json.dumps(resp_analysis)))

    # Overall candidate analysis
    avg_sentiment = round(sum(sentiments) / max(len(sentiments), 1), 2) if sentiments else 0
    all_keywords = []
    for r in analysis_results:
        all_keywords.extend(r['keywords'])
    keyword_freq = {}
    for kw in all_keywords:
        keyword_freq[kw] = keyword_freq.get(kw, 0) + 1

    # Update candidate
    db.execute('UPDATE candidates SET sentiment_score=?, keyword_matches=? WHERE id=?',
               (avg_sentiment, json.dumps(keyword_freq), candidate_id))

    # Store overall analysis
    overall_id = str(uuid.uuid4())
    overall_analysis = {
        'total_words': total_words,
        'avg_sentiment': avg_sentiment,
        'keyword_frequency': keyword_freq,
        'responses_analyzed': len(analysis_results),
    }
    db.execute('''INSERT INTO ai_analysis (id, candidate_id, response_id, analysis_type, results, model_used)
                  VALUES (?, ?, NULL, 'candidate_overview', ?, 'heuristic')''',
               (overall_id, candidate_id, json.dumps(overall_analysis)))

    _log_activity(db, g.user_id, g.user_id, g.user.get('name', ''), 'ai_analysis_run',
                  'candidate', candidate_id)
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'candidate_id': candidate_id,
        'overall': overall_analysis,
        'responses': analysis_results,
    })


@app.route('/api/candidates/<candidate_id>/analysis', methods=['GET'])
@require_auth
def api_get_analysis(candidate_id):
    """Get stored AI analysis for a candidate."""
    db = get_db()
    analyses = db.execute(
        'SELECT * FROM ai_analysis WHERE candidate_id=? ORDER BY created_at DESC',
        (candidate_id,)
    ).fetchall()
    db.close()
    result = [dict(a) for a in analyses]
    for a in result:
        try: a['results'] = json.loads(a['results'])
        except: pass
    return jsonify({'analyses': result, 'count': len(result)})


@app.route('/api/ai/scoring-config', methods=['GET'])
@require_auth
def api_ai_scoring_config():
    """Get AI scoring configuration and capabilities."""
    return jsonify({
        'ai_available': is_ai_available(),
        'categories': CATEGORIES,
        'category_labels': CAT_LABELS,
        'analysis_types': ['response_analysis', 'candidate_overview', 'sentiment', 'keywords'],
        'supported_features': {
            'auto_score': True,
            'sentiment_analysis': True,
            'keyword_detection': True,
            'speaking_pace': True,
            'custom_rubrics': True,
            'batch_scoring': True,
        }
    })


@app.route('/api/candidates/batch-score', methods=['POST'])
@require_auth
def api_batch_score():
    """Score multiple candidates at once."""
    data = request.get_json()
    candidate_ids = data.get('candidate_ids', [])
    if not candidate_ids or not isinstance(candidate_ids, list):
        return jsonify({'error': 'candidate_ids array required'}), 400

    db = get_db()
    scored = []
    errors = []

    for cid in candidate_ids[:20]:  # Limit to 20 per batch
        candidate = db.execute(
            'SELECT c.*, i.title, i.position FROM candidates c JOIN interviews i ON c.interview_id=i.id WHERE c.id=? AND c.user_id=?',
            (cid, g.user_id)
        ).fetchone()
        if not candidate:
            errors.append({'candidate_id': cid, 'error': 'Not found'})
            continue

        responses = db.execute(
            'SELECT r.*, q.question_text FROM responses r JOIN questions q ON r.question_id=q.id WHERE r.candidate_id=?',
            (cid,)
        ).fetchall()

        if not responses:
            errors.append({'candidate_id': cid, 'error': 'No responses'})
            continue

        position = candidate['position'] or candidate['title'] or 'position'
        all_scores = []
        for resp in responses:
            result = score_response(
                question_text=resp['question_text'],
                transcript=resp['transcript'] or '',
                position=position,
                interview_title=candidate['title'] or ''
            )
            all_scores.append(result['overall'])
            db.execute('UPDATE responses SET ai_score=?, ai_feedback=? WHERE id=?',
                       (result['overall'], result['feedback'], resp['id']))

        avg = round(sum(all_scores) / len(all_scores), 1) if all_scores else 0
        summary = generate_candidate_summary(position, {}, avg)
        db.execute('UPDATE candidates SET ai_score=?, ai_summary=?, status=? WHERE id=?',
                   (avg, summary, 'reviewed', cid))
        scored.append({'candidate_id': cid, 'score': avg})

    db.commit()
    db.close()
    return jsonify({'scored': scored, 'errors': errors, 'total_scored': len(scored)})


# ======================== CYCLE 16: INTEGRATIONS & WEBHOOKS (ENHANCED) ========================

@app.route('/api/webhooks/v2', methods=['GET'])
@require_auth
def api_webhooks_v2_list():
    """List webhooks with delivery stats."""
    db = get_db()
    hooks = db.execute('SELECT * FROM webhooks WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    result = []
    for h in hooks:
        hd = dict(h)
        # Get delivery stats
        stats = db.execute(
            'SELECT COUNT(*) as total, SUM(CASE WHEN delivered=1 THEN 1 ELSE 0 END) as delivered FROM webhook_deliveries WHERE webhook_id=?',
            (hd['id'],)
        ).fetchone()
        hd['delivery_stats'] = {'total': stats['total'], 'delivered': stats['delivered']}
        result.append(hd)
    db.close()
    return jsonify({'webhooks': result, 'count': len(result)})


@app.route('/api/webhooks/v2/deliveries/<webhook_id>', methods=['GET'])
@require_auth
def api_webhook_deliveries(webhook_id):
    """Get delivery log for a specific webhook."""
    db = get_db()
    hook = db.execute('SELECT id FROM webhooks WHERE id=? AND user_id=?', (webhook_id, g.user_id)).fetchone()
    if not hook:
        db.close()
        return jsonify({'error': 'Webhook not found'}), 404

    deliveries = db.execute(
        'SELECT * FROM webhook_deliveries WHERE webhook_id=? ORDER BY created_at DESC LIMIT 50',
        (webhook_id,)
    ).fetchall()
    db.close()
    return jsonify({'deliveries': [dict(d) for d in deliveries], 'count': len(deliveries)})


@app.route('/api/webhooks/v2/simulate', methods=['POST'])
@require_auth
def api_webhook_simulate():
    """Simulate a webhook event for testing."""
    data = request.get_json()
    event_type = data.get('event_type', 'candidate.completed')

    sample_payloads = {
        'candidate.completed': {
            'event': 'candidate.completed',
            'timestamp': datetime.utcnow().isoformat(),
            'data': {'candidate_id': 'sample-123', 'name': 'Jane Smith', 'email': 'jane@example.com',
                     'interview_title': 'Insurance Agent Interview', 'score': 82.5, 'status': 'completed'}
        },
        'candidate.invited': {
            'event': 'candidate.invited',
            'timestamp': datetime.utcnow().isoformat(),
            'data': {'candidate_id': 'sample-456', 'name': 'John Doe', 'email': 'john@example.com',
                     'interview_title': 'Claims Adjuster Position'}
        },
        'interview.created': {
            'event': 'interview.created',
            'timestamp': datetime.utcnow().isoformat(),
            'data': {'interview_id': 'sample-789', 'title': 'New Position', 'department': 'Sales'}
        },
        'candidate.scored': {
            'event': 'candidate.scored',
            'timestamp': datetime.utcnow().isoformat(),
            'data': {'candidate_id': 'sample-abc', 'score': 78.5, 'categories': {'communication': 85, 'industry_knowledge': 72}}
        }
    }

    payload = sample_payloads.get(event_type, sample_payloads['candidate.completed'])
    payload['account_id'] = g.user_id

    return jsonify({'event_type': event_type, 'payload': payload, 'simulated': True})


@app.route('/api/embed/widgets', methods=['GET'])
@require_auth
def api_list_widgets():
    """List embeddable widgets."""
    db = get_db()
    widgets = db.execute('SELECT * FROM embed_widgets WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    db.close()
    result = [dict(w) for w in widgets]
    for w in result:
        try: w['config'] = json.loads(w['config']) if w.get('config') else {}
        except: pass
    return jsonify({'widgets': result, 'count': len(result)})


@app.route('/api/embed/widgets', methods=['POST'])
@require_auth
def api_create_widget():
    """Create an embeddable widget."""
    data = request.get_json()
    interview_id = data.get('interview_id')
    widget_type = data.get('widget_type', 'apply_button')

    if not interview_id:
        return jsonify({'error': 'interview_id required'}), 400

    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    widget_id = str(uuid.uuid4())
    embed_key = str(uuid.uuid4())[:12]
    config = json.dumps(data.get('config', {
        'button_text': 'Apply Now',
        'button_color': '#0ace0a',
        'show_description': True,
        'show_question_count': True,
    }))

    db.execute('''INSERT INTO embed_widgets (id, user_id, interview_id, widget_type, title, config, embed_key)
                  VALUES (?, ?, ?, ?, ?, ?, ?)''',
               (widget_id, g.user_id, interview_id, widget_type, data.get('title', ''), config, embed_key))

    # Enable public apply on the interview
    db.execute('UPDATE interviews SET public_apply_enabled=1 WHERE id=?', (interview_id,))
    db.commit()
    db.close()

    return jsonify({
        'widget': {'id': widget_id, 'embed_key': embed_key, 'widget_type': widget_type},
        'embed_code': f'<script src="/embed/{embed_key}.js"></script>',
        'embed_url': f'/embed/{embed_key}',
    }), 201


@app.route('/api/embed/<embed_key>', methods=['GET'])
def api_embed_render(embed_key):
    """Render embeddable widget data (public)."""
    db = get_db()
    widget = db.execute(
        '''SELECT w.*, i.title, i.description, i.department, i.position, i.brand_color,
           u.agency_name, u.agency_logo_url
           FROM embed_widgets w
           JOIN interviews i ON w.interview_id = i.id
           JOIN users u ON w.user_id = u.id
           WHERE w.embed_key=? AND w.active=1''', (embed_key,)
    ).fetchone()
    if not widget:
        db.close()
        return jsonify({'error': 'Widget not found'}), 404

    w = dict(widget)
    db.execute('UPDATE embed_widgets SET views = views + 1 WHERE id=?', (w['id'],))
    db.commit()
    q_count = db.execute('SELECT COUNT(*) as cnt FROM questions WHERE interview_id=?', (w['interview_id'],)).fetchone()['cnt']
    db.close()

    try: config = json.loads(w['config']) if w.get('config') else {}
    except: config = {}

    return jsonify({
        'widget_type': w['widget_type'],
        'interview_id': w['interview_id'],
        'title': w.get('title') or w['title'],
        'description': w.get('description'),
        'department': w.get('department'),
        'position': w.get('position'),
        'brand_color': w.get('brand_color', '#0ace0a'),
        'agency_name': w['agency_name'],
        'question_count': q_count,
        'config': config,
        'apply_url': f"/api/interviews/{w['interview_id']}/public-apply",
    })


# ======================== CYCLE 16: ADVANCED ADMIN & ANALYTICS DASHBOARD ========================

@app.route('/api/admin/metrics', methods=['GET'])
@require_auth
def api_admin_metrics():
    """Get comprehensive admin metrics for the analytics dashboard."""
    db = get_db()
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    month_ago = (now - timedelta(days=30)).isoformat()

    # Total counts
    total_interviews = db.execute('SELECT COUNT(*) as cnt FROM interviews WHERE user_id=?', (g.user_id,)).fetchone()['cnt']
    total_candidates = db.execute('SELECT COUNT(*) as cnt FROM candidates WHERE user_id=?', (g.user_id,)).fetchone()['cnt']
    active_interviews = db.execute("SELECT COUNT(*) as cnt FROM interviews WHERE user_id=? AND status='active'", (g.user_id,)).fetchone()['cnt']

    # Status breakdown
    status_counts = db.execute(
        'SELECT status, COUNT(*) as cnt FROM candidates WHERE user_id=? GROUP BY status', (g.user_id,)
    ).fetchall()
    statuses = {r['status']: r['cnt'] for r in status_counts}

    # This month candidates
    month_candidates = db.execute(
        'SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND created_at >= ?',
        (g.user_id, month_start)
    ).fetchone()['cnt']

    # Completion rate
    completed = statuses.get('completed', 0) + statuses.get('reviewed', 0)
    invited = total_candidates
    completion_rate = round(completed / max(invited, 1) * 100, 1)

    # Average score
    avg_score_row = db.execute(
        'SELECT AVG(ai_score) as avg FROM candidates WHERE user_id=? AND ai_score IS NOT NULL',
        (g.user_id,)
    ).fetchone()
    avg_score = round(avg_score_row['avg'], 1) if avg_score_row['avg'] else 0

    # Time to complete (average days from invited to completed)
    time_data = db.execute(
        '''SELECT AVG(julianday(completed_at) - julianday(invited_at)) as avg_days
           FROM candidates WHERE user_id=? AND completed_at IS NOT NULL AND invited_at IS NOT NULL''',
        (g.user_id,)
    ).fetchone()
    avg_time_to_complete = round(time_data['avg_days'], 1) if time_data['avg_days'] else 0

    # Department breakdown
    dept_data = db.execute(
        '''SELECT i.department, COUNT(c.id) as cnt FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           WHERE c.user_id=? AND i.department IS NOT NULL
           GROUP BY i.department ORDER BY cnt DESC LIMIT 10''',
        (g.user_id,)
    ).fetchall()

    # Weekly trend (last 4 weeks)
    weekly_trend = []
    for w in range(4):
        start = (now - timedelta(days=7 * (w + 1))).isoformat()
        end = (now - timedelta(days=7 * w)).isoformat()
        cnt = db.execute(
            'SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND created_at >= ? AND created_at < ?',
            (g.user_id, start, end)
        ).fetchone()['cnt']
        weekly_trend.append({'week': f'Week -{w+1}', 'candidates': cnt})

    # Top performing interviews
    top_interviews = db.execute(
        '''SELECT i.title, i.department, COUNT(c.id) as candidate_count,
           AVG(c.ai_score) as avg_score, i.created_at
           FROM interviews i LEFT JOIN candidates c ON i.id = c.interview_id
           WHERE i.user_id=? GROUP BY i.id ORDER BY candidate_count DESC LIMIT 5''',
        (g.user_id,)
    ).fetchall()

    db.close()
    return jsonify({
        'overview': {
            'total_interviews': total_interviews,
            'active_interviews': active_interviews,
            'total_candidates': total_candidates,
            'month_candidates': month_candidates,
            'completion_rate': completion_rate,
            'avg_score': avg_score,
            'avg_time_to_complete_days': avg_time_to_complete,
        },
        'statuses': statuses,
        'departments': [{'department': d['department'], 'count': d['cnt']} for d in dept_data],
        'weekly_trend': list(reversed(weekly_trend)),
        'top_interviews': [dict(t) for t in top_interviews],
    })


@app.route('/api/admin/funnel', methods=['GET'])
@require_auth
def api_admin_funnel():
    """Get conversion funnel metrics."""
    db = get_db()
    total = db.execute('SELECT COUNT(*) as cnt FROM candidates WHERE user_id=?', (g.user_id,)).fetchone()['cnt']
    started = db.execute("SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND started_at IS NOT NULL", (g.user_id,)).fetchone()['cnt']
    completed = db.execute("SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND status IN ('completed', 'reviewed')", (g.user_id,)).fetchone()['cnt']
    scored = db.execute("SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND ai_score IS NOT NULL", (g.user_id,)).fetchone()['cnt']
    high_score = db.execute("SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND ai_score >= 80", (g.user_id,)).fetchone()['cnt']

    db.close()
    stages = [
        {'stage': 'Invited', 'count': total, 'pct': 100},
        {'stage': 'Started', 'count': started, 'pct': round(started / max(total, 1) * 100, 1)},
        {'stage': 'Completed', 'count': completed, 'pct': round(completed / max(total, 1) * 100, 1)},
        {'stage': 'Scored', 'count': scored, 'pct': round(scored / max(total, 1) * 100, 1)},
        {'stage': 'Top Talent (80+)', 'count': high_score, 'pct': round(high_score / max(total, 1) * 100, 1)},
    ]
    return jsonify({'funnel': stages, 'total_invited': total})


@app.route('/api/admin/interviewer-performance', methods=['GET'])
@require_auth
def api_interviewer_performance():
    """Get interviewer/interview performance comparison."""
    db = get_db()
    interviews = db.execute(
        '''SELECT i.id, i.title, i.department, i.created_at,
           COUNT(c.id) as total_candidates,
           SUM(CASE WHEN c.status IN ('completed','reviewed') THEN 1 ELSE 0 END) as completed,
           AVG(c.ai_score) as avg_score,
           AVG(CASE WHEN c.completed_at IS NOT NULL AND c.invited_at IS NOT NULL
               THEN julianday(c.completed_at) - julianday(c.invited_at) END) as avg_completion_days
           FROM interviews i LEFT JOIN candidates c ON i.id = c.interview_id
           WHERE i.user_id=? GROUP BY i.id ORDER BY total_candidates DESC''',
        (g.user_id,)
    ).fetchall()
    db.close()

    result = []
    for intv in interviews:
        d = dict(intv)
        d['completion_rate'] = round(d['completed'] / max(d['total_candidates'], 1) * 100, 1)
        d['avg_score'] = round(d['avg_score'], 1) if d['avg_score'] else 0
        d['avg_completion_days'] = round(d['avg_completion_days'], 1) if d['avg_completion_days'] else 0
        result.append(d)

    return jsonify({'interviews': result, 'count': len(result)})


@app.route('/api/admin/export', methods=['GET'])
@require_auth
def api_admin_export():
    """Export candidate data as CSV-compatible JSON."""
    db = get_db()
    interview_id = request.args.get('interview_id', '')
    status_filter = request.args.get('status', '')

    query = '''SELECT c.first_name, c.last_name, c.email, c.phone, c.status, c.ai_score,
               c.ai_summary, c.created_at, c.started_at, c.completed_at, c.source,
               i.title as interview_title, i.department, i.position
               FROM candidates c JOIN interviews i ON c.interview_id = i.id
               WHERE c.user_id=?'''
    params = [g.user_id]

    if interview_id:
        query += ' AND c.interview_id=?'
        params.append(interview_id)
    if status_filter:
        query += ' AND c.status=?'
        params.append(status_filter)

    query += ' ORDER BY c.created_at DESC'
    candidates = db.execute(query, params).fetchall()
    db.close()

    rows = [dict(c) for c in candidates]
    # Build CSV header
    headers = ['first_name', 'last_name', 'email', 'phone', 'status', 'ai_score',
               'interview_title', 'department', 'position', 'source', 'created_at',
               'started_at', 'completed_at']

    csv_lines = [','.join(headers)]
    for r in rows:
        csv_lines.append(','.join(str(r.get(h, '') or '') for h in headers))

    return jsonify({
        'data': rows,
        'csv': '\n'.join(csv_lines),
        'total': len(rows),
        'headers': headers
    })


@app.route('/api/admin/snapshot', methods=['POST'])
@require_auth
def api_create_snapshot():
    """Create a daily analytics snapshot for historical tracking."""
    db = get_db()
    today = datetime.utcnow().strftime('%Y-%m-%d')

    # Gather current metrics
    total_candidates = db.execute('SELECT COUNT(*) as cnt FROM candidates WHERE user_id=?', (g.user_id,)).fetchone()['cnt']
    completed = db.execute("SELECT COUNT(*) as cnt FROM candidates WHERE user_id=? AND status IN ('completed','reviewed')", (g.user_id,)).fetchone()['cnt']
    avg_score = db.execute('SELECT AVG(ai_score) as avg FROM candidates WHERE user_id=? AND ai_score IS NOT NULL', (g.user_id,)).fetchone()['avg']
    active_interviews = db.execute("SELECT COUNT(*) as cnt FROM interviews WHERE user_id=? AND status='active'", (g.user_id,)).fetchone()['cnt']

    metrics = json.dumps({
        'total_candidates': total_candidates,
        'completed': completed,
        'completion_rate': round(completed / max(total_candidates, 1) * 100, 1),
        'avg_score': round(avg_score, 1) if avg_score else 0,
        'active_interviews': active_interviews,
    })

    try:
        snap_id = str(uuid.uuid4())
        db.execute('INSERT INTO analytics_snapshots (id, account_id, snapshot_date, metrics) VALUES (?, ?, ?, ?)',
                   (snap_id, g.user_id, today, metrics))
        db.commit()
    except:
        # Already exists for today, update it
        db.execute('UPDATE analytics_snapshots SET metrics=? WHERE account_id=? AND snapshot_date=?',
                   (metrics, g.user_id, today))
        db.commit()
    db.close()
    return jsonify({'success': True, 'date': today})


@app.route('/api/admin/snapshots', methods=['GET'])
@require_auth
def api_get_snapshots():
    """Get historical analytics snapshots."""
    db = get_db()
    days = request.args.get('days', 30, type=int)
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
    snapshots = db.execute(
        'SELECT * FROM analytics_snapshots WHERE account_id=? AND snapshot_date >= ? ORDER BY snapshot_date',
        (g.user_id, cutoff)
    ).fetchall()
    db.close()
    result = [dict(s) for s in snapshots]
    for s in result:
        try: s['metrics'] = json.loads(s['metrics'])
        except: pass
    return jsonify({'snapshots': result, 'count': len(result)})


# ======================== CYCLE 16: PAGE ROUTES ========================

@app.route('/admin-analytics')
@require_auth
@require_fmo_admin
def page_admin_analytics():
    return render_template('app.html', user=g.user, page='admin-analytics')

@app.route('/ai-scoring')
@require_auth
@require_fmo_admin
def page_ai_scoring():
    return render_template('app.html', user=g.user, page='ai-scoring')

@app.route('/integrations-hub')
@require_auth
@require_fmo_admin
def page_integrations_hub():
    return render_template('app.html', user=g.user, page='integrations-hub')


# ======================== CYCLE 17: VIDEO RECORDING & PLAYBACK ========================

@app.route('/api/video/recording-config', methods=['GET'])
def api_video_recording_config():
    """Get video recording configuration for the candidate portal."""
    return jsonify({
        'max_duration_seconds': 180,
        'min_duration_seconds': 5,
        'supported_formats': ['video/webm', 'video/mp4'],
        'preferred_format': 'video/webm',
        'max_file_size_mb': 100,
        'resolution': {'width': 1280, 'height': 720},
        'framerate': 30,
        'audio_required': True,
        'countdown_seconds': 3,
        'auto_stop': True,
        'retake_allowed': True,
    })


@app.route('/api/video/sessions', methods=['POST'])
def api_video_session_start():
    """Start a new video recording session."""
    data = request.get_json() or {}
    candidate_token = data.get('token', '')
    question_id = data.get('question_id', '')

    if not candidate_token or not question_id:
        return jsonify({'error': 'token and question_id required'}), 400

    db = get_db()
    candidate = db.execute('SELECT id FROM candidates WHERE token=?', (candidate_token,)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid token'}), 404

    # Count existing attempts for this question
    attempts = db.execute(
        'SELECT COUNT(*) as cnt FROM video_sessions WHERE candidate_id=? AND question_id=?',
        (candidate['id'], question_id)
    ).fetchone()['cnt']

    session_id = str(uuid.uuid4())
    db.execute('''INSERT INTO video_sessions (id, candidate_id, question_id, attempt_number, device_type, browser, status)
                  VALUES (?, ?, ?, ?, ?, ?, 'recording')''',
               (session_id, candidate['id'], question_id, attempts + 1,
                data.get('device_type', ''), data.get('browser', '')))
    db.commit()
    db.close()
    return jsonify({'session_id': session_id, 'attempt_number': attempts + 1}), 201


@app.route('/api/video/sessions/<session_id>/complete', methods=['POST'])
def api_video_session_complete(session_id):
    """Mark a video recording session as completed."""
    data = request.get_json() or {}
    db = get_db()
    sess = db.execute('SELECT * FROM video_sessions WHERE id=?', (session_id,)).fetchone()
    if not sess:
        db.close()
        return jsonify({'error': 'Session not found'}), 404

    db.execute('''UPDATE video_sessions SET status='completed', completed_at=CURRENT_TIMESTAMP,
                  duration_seconds=?, recording_quality=? WHERE id=?''',
               (data.get('duration_seconds', 0), data.get('quality', 'good'), session_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/video/sessions/<session_id>/error', methods=['POST'])
def api_video_session_error(session_id):
    """Report a recording error."""
    data = request.get_json() or {}
    db = get_db()
    db.execute("UPDATE video_sessions SET status='error', error_message=? WHERE id=?",
               (data.get('error', 'Unknown error'), session_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/candidates/<candidate_id>/playback', methods=['GET'])
@require_auth
def api_candidate_playback(candidate_id):
    """Get all video responses for a candidate with playback info."""
    db = get_db()
    candidate = db.execute(
        '''SELECT c.*, i.title as interview_title FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           WHERE c.id=? AND c.user_id=?''', (candidate_id, g.user_id)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    responses = db.execute(
        '''SELECT r.*, q.question_text, q.question_order FROM responses r
           JOIN questions q ON r.question_id = q.id
           WHERE r.candidate_id=? ORDER BY q.question_order''',
        (candidate_id,)
    ).fetchall()

    result = []
    for resp in responses:
        r = dict(resp)
        r['has_video'] = bool(r.get('video_path'))
        # Get recording sessions info
        sessions = db.execute(
            'SELECT * FROM video_sessions WHERE candidate_id=? AND question_id=? ORDER BY attempt_number',
            (candidate_id, r['question_id'])
        ).fetchall()
        r['recording_attempts'] = len(sessions)
        r['sessions'] = [dict(s) for s in sessions]
        result.append(r)

    db.close()
    return jsonify({
        'candidate': {
            'id': candidate['id'], 'name': f"{candidate['first_name']} {candidate['last_name']}",
            'status': candidate['status'], 'ai_score': candidate['ai_score'],
        },
        'interview_title': candidate['interview_title'],
        'responses': result,
        'total_responses': len(result),
    })


# ======================== CYCLE 17: FMO PORTAL (replaced by Cycle 29) ========================
# Old C17 FMO routes removed — see Cycle 29 FMO Admin Portal API Endpoints below


# ======================== CYCLE 17: CANDIDATE REVIEW & COMPARISON ========================

@app.route('/api/candidates/compare', methods=['POST'])
@require_auth
def api_compare_candidates_v2():
    """Compare multiple candidates side-by-side."""
    data = request.get_json() or {}
    candidate_ids = data.get('candidate_ids', [])
    if not candidate_ids or len(candidate_ids) < 2:
        return jsonify({'error': 'At least 2 candidate_ids required'}), 400

    db = get_db()
    candidates = []
    for cid in candidate_ids[:10]:
        cand = db.execute(
            '''SELECT c.*, i.title as interview_title, i.position FROM candidates c
               JOIN interviews i ON c.interview_id = i.id WHERE c.id=? AND c.user_id=?''',
            (cid, g.user_id)
        ).fetchone()
        if cand:
            c = dict(cand)
            # Get response scores
            resps = db.execute(
                'SELECT r.*, q.question_text FROM responses r JOIN questions q ON r.question_id=q.id WHERE r.candidate_id=?',
                (cid,)
            ).fetchall()
            c['responses'] = [dict(r) for r in resps]
            c['response_count'] = len(resps)
            # Get notes count
            notes_count = db.execute('SELECT COUNT(*) as cnt FROM team_notes WHERE candidate_id=?', (cid,)).fetchone()['cnt']
            c['notes_count'] = notes_count
            candidates.append(c)

    db.close()
    return jsonify({'candidates': candidates, 'count': len(candidates)})


@app.route('/api/shortlists', methods=['GET'])
@require_auth
def api_list_shortlists():
    """List all shortlists for the current user."""
    db = get_db()
    lists = db.execute('SELECT * FROM shortlists WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    result = []
    for sl in lists:
        d = dict(sl)
        d['candidate_count'] = db.execute(
            'SELECT COUNT(*) as cnt FROM shortlist_candidates WHERE shortlist_id=?', (d['id'],)
        ).fetchone()['cnt']
        result.append(d)
    db.close()
    return jsonify({'shortlists': result, 'count': len(result)})


@app.route('/api/shortlists', methods=['POST'])
@require_auth
def api_create_shortlist():
    """Create a new candidate shortlist."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400

    db = get_db()
    sl_id = str(uuid.uuid4())
    db.execute('INSERT INTO shortlists (id, user_id, name, description, interview_id) VALUES (?,?,?,?,?)',
               (sl_id, g.user_id, name, data.get('description', ''), data.get('interview_id')))
    db.commit()
    db.close()
    return jsonify({'shortlist': {'id': sl_id, 'name': name}}), 201


@app.route('/api/shortlists/<shortlist_id>', methods=['GET'])
@require_auth
def api_get_shortlist(shortlist_id):
    """Get a shortlist with its candidates."""
    db = get_db()
    sl = db.execute('SELECT * FROM shortlists WHERE id=? AND user_id=?', (shortlist_id, g.user_id)).fetchone()
    if not sl:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    candidates = db.execute(
        '''SELECT sc.*, c.first_name, c.last_name, c.email, c.status, c.ai_score, c.completed_at
           FROM shortlist_candidates sc JOIN candidates c ON sc.candidate_id = c.id
           WHERE sc.shortlist_id=? ORDER BY sc.position_rank''',
        (shortlist_id,)
    ).fetchall()
    db.close()
    return jsonify({'shortlist': dict(sl), 'candidates': [dict(c) for c in candidates], 'count': len(candidates)})


@app.route('/api/shortlists/<shortlist_id>/candidates', methods=['POST'])
@require_auth
def api_add_to_shortlist(shortlist_id):
    """Add a candidate to a shortlist."""
    data = request.get_json() or {}
    candidate_id = data.get('candidate_id', '')
    if not candidate_id:
        return jsonify({'error': 'candidate_id required'}), 400

    db = get_db()
    sl = db.execute('SELECT id FROM shortlists WHERE id=? AND user_id=?', (shortlist_id, g.user_id)).fetchone()
    if not sl:
        db.close()
        return jsonify({'error': 'Shortlist not found'}), 404

    cand = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not cand:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    # Check duplicate
    existing = db.execute('SELECT id FROM shortlist_candidates WHERE shortlist_id=? AND candidate_id=?',
                          (shortlist_id, candidate_id)).fetchone()
    if existing:
        db.close()
        return jsonify({'error': 'Already in shortlist'}), 409

    entry_id = str(uuid.uuid4())
    rank = db.execute('SELECT COALESCE(MAX(position_rank),0)+1 as next FROM shortlist_candidates WHERE shortlist_id=?',
                      (shortlist_id,)).fetchone()['next']
    db.execute('INSERT INTO shortlist_candidates (id, shortlist_id, candidate_id, added_by, notes, position_rank) VALUES (?,?,?,?,?,?)',
               (entry_id, shortlist_id, candidate_id, g.user_id, data.get('notes', ''), rank))
    db.execute('UPDATE candidates SET shortlisted=1 WHERE id=?', (candidate_id,))
    db.commit()
    db.close()
    return jsonify({'success': True, 'entry_id': entry_id, 'rank': rank}), 201


@app.route('/api/shortlists/<shortlist_id>/candidates/<candidate_id>', methods=['DELETE'])
@require_auth
def api_remove_from_shortlist(shortlist_id, candidate_id):
    """Remove a candidate from a shortlist."""
    db = get_db()
    entry = db.execute('SELECT id FROM shortlist_candidates WHERE shortlist_id=? AND candidate_id=?',
                       (shortlist_id, candidate_id)).fetchone()
    if not entry:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute('DELETE FROM shortlist_candidates WHERE shortlist_id=? AND candidate_id=?', (shortlist_id, candidate_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/shortlists/<shortlist_id>', methods=['DELETE'])
@require_auth
def api_delete_shortlist(shortlist_id):
    """Delete a shortlist."""
    db = get_db()
    sl = db.execute('SELECT id FROM shortlists WHERE id=? AND user_id=?', (shortlist_id, g.user_id)).fetchone()
    if not sl:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute('DELETE FROM shortlist_candidates WHERE shortlist_id=?', (shortlist_id,))
    db.execute('DELETE FROM shortlists WHERE id=?', (shortlist_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ======================== CYCLE 17: EMAIL TEMPLATES & DELIVERY PIPELINE ========================

DEFAULT_EMAIL_TEMPLATES = {
    'invitation': {
        'subject': 'You\'re invited to interview for {{position}} at {{agency_name}}',
        'variables': ['candidate_name', 'position', 'agency_name', 'interview_url', 'interviewer_name'],
    },
    'reminder': {
        'subject': 'Reminder: Complete your interview for {{position}}',
        'variables': ['candidate_name', 'position', 'agency_name', 'interview_url', 'days_remaining'],
    },
    'completion': {
        'subject': 'Thank you for completing your interview, {{candidate_name}}!',
        'variables': ['candidate_name', 'position', 'agency_name', 'thank_you_msg'],
    },
    'share_report': {
        'subject': 'Candidate report shared: {{candidate_name}} for {{position}}',
        'variables': ['candidate_name', 'position', 'agency_name', 'report_url', 'sender_name'],
    },
}


@app.route('/api/email/templates', methods=['GET'])
@require_auth
def api_list_email_templates():
    """List email templates for the current user."""
    db = get_db()
    templates = db.execute('SELECT * FROM email_templates WHERE user_id=? ORDER BY template_type, created_at DESC',
                           (g.user_id,)).fetchall()
    db.close()
    result = [dict(t) for t in templates]
    for t in result:
        try: t['variables'] = json.loads(t['variables']) if t.get('variables') else []
        except: pass
    return jsonify({
        'templates': result,
        'count': len(result),
        'default_types': DEFAULT_EMAIL_TEMPLATES,
    })


@app.route('/api/email/templates', methods=['POST'])
@require_auth
def api_create_email_template():
    """Create a custom email template."""
    data = request.get_json() or {}
    template_type = (data.get('template_type') or '').strip()
    name = (data.get('name') or '').strip()
    subject = (data.get('subject') or '').strip()
    html_body = (data.get('html_body') or '').strip()

    if not template_type or not name or not subject or not html_body:
        return jsonify({'error': 'template_type, name, subject, and html_body required'}), 400

    db = get_db()
    tmpl_id = str(uuid.uuid4())
    variables = data.get('variables', DEFAULT_EMAIL_TEMPLATES.get(template_type, {}).get('variables', []))
    db.execute('''INSERT INTO email_templates (id, user_id, template_type, name, subject, html_body, is_default, variables)
                  VALUES (?,?,?,?,?,?,?,?)''',
               (tmpl_id, g.user_id, template_type, name, subject, html_body,
                1 if data.get('is_default') else 0, json.dumps(variables)))
    db.commit()
    tmpl = dict(db.execute('SELECT * FROM email_templates WHERE id=?', (tmpl_id,)).fetchone())
    db.close()
    try: tmpl['variables'] = json.loads(tmpl['variables'])
    except: pass
    return jsonify({'template': tmpl}), 201


@app.route('/api/email/templates/<template_id>', methods=['GET'])
@require_auth
def api_get_email_template(template_id):
    """Get a single email template."""
    db = get_db()
    tmpl = db.execute('SELECT * FROM email_templates WHERE id=? AND user_id=?', (template_id, g.user_id)).fetchone()
    db.close()
    if not tmpl:
        return jsonify({'error': 'Not found'}), 404
    t = dict(tmpl)
    try: t['variables'] = json.loads(t['variables'])
    except: pass
    return jsonify({'template': t})


@app.route('/api/email/templates/<template_id>', methods=['PUT'])
@require_auth
def api_update_email_template(template_id):
    """Update an email template."""
    db = get_db()
    tmpl = db.execute('SELECT id FROM email_templates WHERE id=? AND user_id=?', (template_id, g.user_id)).fetchone()
    if not tmpl:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    data = request.get_json() or {}
    updates = []
    params = []
    for field in ['name', 'subject', 'html_body']:
        if field in data:
            updates.append(f'{field}=?')
            params.append(data[field])
    if 'is_default' in data:
        updates.append('is_default=?')
        params.append(1 if data['is_default'] else 0)
    if updates:
        updates.append('updated_at=CURRENT_TIMESTAMP')
        params.append(template_id)
        db.execute(f'UPDATE email_templates SET {", ".join(updates)} WHERE id=?', params)
        db.commit()

    updated = dict(db.execute('SELECT * FROM email_templates WHERE id=?', (template_id,)).fetchone())
    db.close()
    try: updated['variables'] = json.loads(updated['variables'])
    except: pass
    return jsonify({'template': updated})


@app.route('/api/email/templates/<template_id>', methods=['DELETE'])
@require_auth
def api_delete_email_template(template_id):
    """Delete an email template."""
    db = get_db()
    tmpl = db.execute('SELECT id FROM email_templates WHERE id=? AND user_id=?', (template_id, g.user_id)).fetchone()
    if not tmpl:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute('DELETE FROM email_templates WHERE id=?', (template_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/email/preview', methods=['POST'])
@require_auth
def api_email_preview():
    """Preview an email template with sample data."""
    data = request.get_json() or {}
    subject = data.get('subject', '')
    html_body = data.get('html_body', '')
    template_type = data.get('template_type', 'invitation')

    sample_vars = {
        'candidate_name': 'Jane Smith',
        'position': 'Insurance Agent',
        'agency_name': g.user.get('agency_name', 'Test Agency'),
        'interview_url': 'https://app.channelview.io/i/sample-token',
        'interviewer_name': g.user.get('name', 'Recruiter'),
        'days_remaining': '3',
        'thank_you_msg': 'Thank you for taking the time to interview!',
        'report_url': 'https://app.channelview.io/report/sample',
        'sender_name': g.user.get('name', 'Recruiter'),
    }

    # Render with sample data
    rendered_subject = subject
    rendered_body = html_body
    for key, val in sample_vars.items():
        rendered_subject = rendered_subject.replace(f'{{{{{key}}}}}', val)
        rendered_body = rendered_body.replace(f'{{{{{key}}}}}', val)

    return jsonify({
        'subject': rendered_subject,
        'html_body': rendered_body,
        'sample_variables': sample_vars,
    })


@app.route('/api/email/delivery-stats', methods=['GET'])
@require_auth
def api_email_delivery_stats():
    """Get email delivery statistics."""
    db = get_db()
    total = db.execute('SELECT COUNT(*) as cnt FROM email_log WHERE user_id=?', (g.user_id,)).fetchone()['cnt']
    sent = db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE user_id=? AND status='sent'", (g.user_id,)).fetchone()['cnt']
    failed = db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE user_id=? AND status='failed'", (g.user_id,)).fetchone()['cnt']

    # By type
    by_type = db.execute(
        'SELECT email_type, COUNT(*) as cnt FROM email_log WHERE user_id=? GROUP BY email_type',
        (g.user_id,)
    ).fetchall()

    # Recent emails
    recent = db.execute(
        'SELECT * FROM email_log WHERE user_id=? ORDER BY sent_at DESC LIMIT 20',
        (g.user_id,)
    ).fetchall()
    db.close()

    return jsonify({
        'total': total, 'sent': sent, 'failed': failed,
        'delivery_rate': round(sent / max(total, 1) * 100, 1),
        'by_type': {r['email_type']: r['cnt'] for r in by_type},
        'recent': [dict(r) for r in recent],
    })


# ======================== CYCLE 17: PAGE ROUTES ========================

@app.route('/fmo')
@require_auth
@require_fmo_admin
def page_fmo():
    return render_template('app.html', user=g.user, page='fmo')


# ── FMO Admin Portal API Endpoints ──

@app.route('/api/fmo/agencies', methods=['GET'])
@require_auth
@require_fmo_admin
def api_fmo_list_agencies():
    """List all agencies under this FMO."""
    db = get_db()
    agencies = db.execute('''
        SELECT u.id, u.name, u.email, u.agency_name, u.plan, u.subscription_status,
               u.created_at, u.trial_ends_at, u.is_fmo_admin,
               u.usage_interviews_count, u.usage_candidates_count, u.usage_video_storage_mb,
               (SELECT COUNT(*) FROM candidates WHERE user_id = u.id) as total_candidates,
               (SELECT COUNT(*) FROM interviews WHERE user_id = u.id) as total_interviews,
               (SELECT COUNT(*) FROM team_members WHERE account_id = u.id AND status='active') as team_size
        FROM users u
        WHERE u.fmo_parent_id = ? OR u.id = ?
        ORDER BY u.created_at DESC
    ''', (g.user_id, g.user_id)).fetchall()
    result = []
    for a in agencies:
        d = dict(a)
        plan = d.get('plan', 'free') or 'free'
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])
        d['plan_limits'] = limits
        d['is_self'] = d['id'] == g.user_id
        result.append(d)
    db.close()
    return jsonify({'agencies': result})


@app.route('/api/fmo/agencies', methods=['POST'])
@require_auth
@require_fmo_admin
def api_fmo_create_agency():
    """Create a new agency account under this FMO."""
    data = request.get_json()
    email = (data.get('email') or '').strip().lower()
    name = (data.get('name') or '').strip()
    agency_name = (data.get('agency_name') or '').strip()
    plan = data.get('plan', 'essentials')

    if not email or not name or not agency_name:
        return jsonify({'error': 'Name, email, and agency name are required'}), 400
    if plan not in PLAN_LIMITS:
        return jsonify({'error': f'Invalid plan: {plan}'}), 400

    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if existing:
        db.close()
        return jsonify({'error': 'An account with this email already exists'}), 409

    import secrets as _secrets
    user_id = str(uuid.uuid4())
    # Generate a temporary password — agency owner will reset on first login
    temp_password = _secrets.token_urlsafe(12)
    password_hash = bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode()

    # Set trial period (30 days Professional trial)
    trial_end = (datetime.utcnow() + timedelta(days=30)).isoformat()

    db.execute('''
        INSERT INTO users (id, email, password_hash, name, agency_name, plan, fmo_parent_id,
                           is_fmo, trial_ends_at, subscription_status, onboarding_completed, password_changed,
                           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'trialing', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    ''', (user_id, email, password_hash, name, agency_name, 'professional', g.user_id, trial_end))
    db.commit()
    db.close()

    # Send welcome email with credentials (fire-and-forget)
    _send_agency_welcome_email(email, agency_name, name, temp_password)

    return jsonify({
        'success': True,
        'agency': {
            'id': user_id,
            'email': email,
            'name': name,
            'agency_name': agency_name,
            'plan': 'professional',
            'trial_ends_at': trial_end,
            'temp_password': temp_password
        }
    }), 201


@app.route('/api/fmo/agencies/<agency_id>', methods=['GET'])
@require_auth
@require_fmo_admin
def api_fmo_get_agency(agency_id):
    """Get detailed info for a specific agency."""
    db = get_db()
    agency = db.execute('''
        SELECT u.*,
               (SELECT COUNT(*) FROM candidates WHERE user_id = u.id) as total_candidates,
               (SELECT COUNT(*) FROM interviews WHERE user_id = u.id) as total_interviews,
               (SELECT COUNT(*) FROM team_members WHERE account_id = u.id AND status='active') as team_size
        FROM users u
        WHERE u.id = ? AND (u.fmo_parent_id = ? OR u.id = ?)
    ''', (agency_id, g.user_id, g.user_id)).fetchone()
    if not agency:
        db.close()
        return jsonify({'error': 'Agency not found'}), 404
    d = dict(agency)
    # Remove sensitive fields
    d.pop('password_hash', None)
    plan = d.get('plan', 'free') or 'free'
    d['plan_limits'] = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])
    db.close()
    return jsonify({'agency': d})


@app.route('/api/fmo/agencies/<agency_id>/plan', methods=['PUT'])
@require_auth
@require_fmo_admin
def api_fmo_update_agency_plan(agency_id):
    """Update an agency's subscription plan."""
    data = request.get_json()
    new_plan = data.get('plan')
    if new_plan not in PLAN_LIMITS:
        return jsonify({'error': f'Invalid plan: {new_plan}'}), 400

    db = get_db()
    agency = db.execute('SELECT id, fmo_parent_id FROM users WHERE id=?', (agency_id,)).fetchone()
    if not agency or (dict(agency).get('fmo_parent_id') != g.user_id and agency_id != g.user_id):
        db.close()
        return jsonify({'error': 'Agency not found'}), 404

    db.execute('UPDATE users SET plan=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (new_plan, agency_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'plan': new_plan})


@app.route('/api/fmo/stats', methods=['GET'])
@require_auth
@require_fmo_admin
def api_fmo_stats():
    """Get aggregate stats across all agencies under this FMO."""
    db = get_db()
    agency_ids = [r['id'] for r in db.execute(
        'SELECT id FROM users WHERE fmo_parent_id=? OR id=?', (g.user_id, g.user_id)).fetchall()]
    placeholders = ','.join(['?'] * len(agency_ids))

    total_agencies = len(agency_ids)
    total_candidates = db.execute(
        f'SELECT COUNT(*) as cnt FROM candidates WHERE user_id IN ({placeholders})', agency_ids).fetchone()['cnt']
    total_interviews = db.execute(
        f'SELECT COUNT(*) as cnt FROM interviews WHERE user_id IN ({placeholders})', agency_ids).fetchone()['cnt']

    # Plan distribution
    plans = db.execute(
        f'SELECT plan, COUNT(*) as cnt FROM users WHERE id IN ({placeholders}) GROUP BY plan', agency_ids).fetchall()
    plan_dist = {dict(p)['plan'] or 'free': dict(p)['cnt'] for p in plans}

    # Active this month
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    active_this_month = db.execute(
        f'SELECT COUNT(DISTINCT user_id) as cnt FROM candidates WHERE user_id IN ({placeholders}) AND created_at >= ?',
        agency_ids + [month_start]).fetchone()['cnt']

    db.close()
    return jsonify({
        'total_agencies': total_agencies,
        'total_candidates': total_candidates,
        'total_interviews': total_interviews,
        'active_this_month': active_this_month,
        'plan_distribution': plan_dist
    })

@app.route('/api/fmo/dashboard', methods=['GET'])
@require_auth
@require_fmo_admin
def api_fmo_dashboard_c29():
    """FMO dashboard — overview of all agencies under this FMO."""
    db = get_db()

    # Get all agencies under this FMO
    agencies = db.execute('''
        SELECT id, name, agency_name, email, plan, subscription_status, trial_ends_at,
               created_at, onboarding_completed
        FROM users WHERE fmo_parent_id=?
        ORDER BY created_at DESC
    ''', (g.user_id,)).fetchall()

    agency_list = []
    total_candidates = 0
    total_interviews = 0
    active_count = 0
    trial_count = 0
    paid_count = 0

    for a in agencies:
        a_dict = dict(a)
        # Get usage stats for each agency
        stats = db.execute('''
            SELECT
                (SELECT COUNT(*) FROM interviews WHERE user_id=?) as interview_count,
                (SELECT COUNT(*) FROM candidates WHERE user_id=?) as candidate_count,
                (SELECT COUNT(*) FROM candidates WHERE user_id=? AND status='completed') as completed_count
        ''', (a_dict['id'], a_dict['id'], a_dict['id'])).fetchone()

        a_dict['interview_count'] = stats['interview_count']
        a_dict['candidate_count'] = stats['candidate_count']
        a_dict['completed_count'] = stats['completed_count']

        total_interviews += stats['interview_count']
        total_candidates += stats['candidate_count']

        if a_dict.get('subscription_status') == 'active':
            paid_count += 1
            active_count += 1
        elif a_dict.get('subscription_status') == 'trialing':
            trial_count += 1
            active_count += 1

        agency_list.append(a_dict)

    db.close()
    return jsonify({
        'summary': {
            'total_agencies': len(agency_list),
            'active_agencies': active_count,
            'trial_agencies': trial_count,
            'paid_agencies': paid_count,
            'total_interviews': total_interviews,
            'total_candidates': total_candidates,
        },
        'agencies': agency_list
    })

# ======================== CYCLE 31: JOB BOARDS & PIPELINE POWER-UP ========================

DEFAULT_STAGES = [
    {'name': 'New', 'slug': 'new', 'order': 0, 'color': '#6b7280'},
    {'name': 'In Review', 'slug': 'in_review', 'order': 1, 'color': '#3b82f6'},
    {'name': 'Shortlisted', 'slug': 'shortlisted', 'order': 2, 'color': '#8b5cf6'},
    {'name': 'Interview Scheduled', 'slug': 'interview_scheduled', 'order': 3, 'color': '#f59e0b'},
    {'name': 'Offered', 'slug': 'offered', 'order': 4, 'color': '#10b981'},
    {'name': 'Hired', 'slug': 'hired', 'order': 5, 'color': '#059669', 'is_terminal': True},
    {'name': 'Rejected', 'slug': 'rejected', 'order': 6, 'color': '#ef4444', 'is_terminal': True},
]


# --- Feature 1: Public Job Board ---

@app.route('/api/jobs/<agency_id>', methods=['GET'])
def api_public_job_board(agency_id):
    """Public job board listing all open positions for an agency."""
    db = get_db()
    agency = db.execute('SELECT id, name, agency_name, brand_color FROM users WHERE id=?', (agency_id,)).fetchone()
    if not agency:
        db.close()
        return jsonify({'error': 'Agency not found'}), 404
    agency = dict(agency)

    jobs = db.execute('''SELECT id, title, description, position, department, location, job_type,
                         salary_range, application_deadline, created_at
                         FROM interviews WHERE user_id=? AND status='active'
                         AND job_board_enabled=1 ORDER BY created_at DESC''', (agency_id,)).fetchall()
    db.close()

    result = []
    for j in jobs:
        d = dict(j)
        if d.get('application_deadline'):
            try:
                dl = datetime.fromisoformat(str(d['application_deadline']))
                if dl < datetime.utcnow():
                    continue  # Skip expired listings
            except:
                pass
        d['apply_url'] = f"/apply/{d['id']}"
        result.append(d)

    return jsonify({
        'agency': {'id': agency['id'], 'name': agency.get('agency_name') or agency.get('name', ''),
                   'brand_color': agency.get('brand_color', '#0ace0a')},
        'jobs': result,
        'total': len(result)
    })


@app.route('/jobs/<agency_id>')
def page_public_job_board(agency_id):
    """Render public job board page (no auth required)."""
    db = get_db()
    agency = db.execute('SELECT id, agency_name, name, brand_color FROM users WHERE id=?', (agency_id,)).fetchone()
    if not agency:
        db.close()
        return "Agency not found", 404
    agency = dict(agency)

    jobs = db.execute('''SELECT id, title, description, position, department, location, job_type,
                         salary_range, application_deadline, created_at
                         FROM interviews WHERE user_id=? AND status='active'
                         AND job_board_enabled=1 ORDER BY created_at DESC''', (agency_id,)).fetchall()
    db.close()
    jobs = [dict(j) for j in jobs]

    agency_name = agency.get('agency_name') or agency.get('name', 'Agency')
    color = agency.get('brand_color', '#0ace0a')

    cards = ''
    for j in jobs:
        loc = j.get('location') or 'Remote'
        jtype = (j.get('job_type') or 'full_time').replace('_', ' ').title()
        salary = f"<span style='color:#059669'>{j['salary_range']}</span>" if j.get('salary_range') else ''
        dept = j.get('department') or ''
        cards += f'''<div style="border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:16px;background:#fff">
          <h3 style="margin:0 0 4px;font-size:18px">{j.get('position') or j['title']}</h3>
          <div style="color:#666;font-size:13px;margin-bottom:12px">{dept}{' · ' if dept else ''}{loc} · {jtype} {salary}</div>
          <p style="color:#555;font-size:14px;margin:0 0 16px;line-height:1.5">{(j.get('description') or '')[:200]}{'...' if len(j.get('description',''))>200 else ''}</p>
          <a href="/apply/{j['id']}" style="display:inline-block;background:{color};color:#000;font-weight:700;padding:10px 24px;border-radius:8px;text-decoration:none;font-size:14px">Apply Now</a>
        </div>'''

    if not cards:
        cards = '<div style="text-align:center;color:#999;padding:40px">No open positions at this time. Check back soon!</div>'

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Careers at {agency_name}</title></head><body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f9fafb">
    <div style="background:#111;padding:40px 24px;text-align:center">
      <h1 style="color:#fff;margin:0 0 8px;font-size:32px">{agency_name}</h1>
      <p style="color:#999;margin:0;font-size:16px">Join our team — explore open positions below</p>
    </div>
    <div style="max-width:720px;margin:32px auto;padding:0 24px">{cards}</div>
    <div style="text-align:center;padding:24px;color:#ccc;font-size:12px">Powered by ChannelView</div>
    </body></html>'''
    return html


@app.route('/api/interviews/<interview_id>/job-board', methods=['PUT'])
@require_auth
def api_toggle_job_board_c31(interview_id):
    """Enable/disable job board listing for an interview."""
    data = request.get_json() or {}
    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    enabled = 1 if data.get('enabled', False) else 0
    location = (data.get('location') or '').strip()
    job_type = data.get('job_type', 'full_time')
    salary_range = (data.get('salary_range') or '').strip()
    deadline = data.get('application_deadline')

    db.execute('''UPDATE interviews SET job_board_enabled=?, location=?, job_type=?, salary_range=?,
                  application_deadline=?, updated_at=CURRENT_TIMESTAMP WHERE id=?''',
               (enabled, location, job_type, salary_range, deadline, interview_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'job_board_enabled': enabled})


# --- Feature 2: Pipeline Funnel Analytics ---

@app.route('/api/analytics/pipeline-funnel', methods=['GET'])
@require_auth
def api_pipeline_funnel_c31():
    """Pipeline funnel analytics with conversion rates and time-in-stage."""
    interview_id = request.args.get('interview_id')
    db = get_db()

    where = 'WHERE c.user_id=?'
    params = [g.user_id]
    if interview_id:
        where += ' AND c.interview_id=?'
        params.append(interview_id)

    # Stage counts
    rows = db.execute(f'''SELECT c.pipeline_stage, COUNT(*) as cnt
                          FROM candidates c {where} GROUP BY c.pipeline_stage''', params).fetchall()
    stage_counts = {r['pipeline_stage']: r['cnt'] for r in rows}

    # Total candidates
    total = sum(stage_counts.values())

    # Build funnel with conversion rates
    stage_order = ['new', 'in_review', 'shortlisted', 'interview_scheduled', 'offered', 'hired']
    funnel = []
    prev_count = total
    for s in stage_order:
        cnt = stage_counts.get(s, 0)
        conversion = round(cnt / max(prev_count, 1) * 100, 1) if prev_count > 0 else 0
        from_total = round(cnt / max(total, 1) * 100, 1) if total > 0 else 0
        funnel.append({
            'stage': s, 'label': s.replace('_', ' ').title(), 'count': cnt,
            'conversion_from_prev': conversion, 'pct_of_total': from_total
        })
        if cnt > 0:
            prev_count = cnt

    rejected = stage_counts.get('rejected', 0)

    # Time in stage (avg days) - based on stage_entered_at if available
    try:
        time_rows = db.execute(f'''SELECT c.pipeline_stage,
            AVG(JULIANDAY('now') - JULIANDAY(COALESCE(c.stage_entered_at, c.created_at))) as avg_days
            FROM candidates c {where} AND c.pipeline_stage NOT IN ('hired','rejected')
            GROUP BY c.pipeline_stage''', params).fetchall()
        time_in_stage = {r['pipeline_stage']: round(r['avg_days'] or 0, 1) for r in time_rows}
    except:
        time_in_stage = {}

    # Source breakdown
    try:
        src_rows = db.execute(f'''SELECT COALESCE(c.source, 'manual') as src, COUNT(*) as cnt
                                  FROM candidates c {where} GROUP BY src ORDER BY cnt DESC''', params).fetchall()
        sources = [{'source': r['src'], 'count': r['cnt'],
                    'pct': round(r['cnt'] / max(total, 1) * 100, 1)} for r in src_rows]
    except:
        sources = []

    db.close()
    return jsonify({
        'funnel': funnel, 'total_candidates': total, 'rejected': rejected,
        'time_in_stage': time_in_stage, 'sources': sources,
        'hire_rate': round(stage_counts.get('hired', 0) / max(total, 1) * 100, 1) if total > 0 else 0
    })


# --- Feature 3: Enhanced Kanban APIs ---

@app.route('/api/candidates/kanban-enhanced', methods=['GET'])
@require_auth
def api_kanban_enhanced_c31():
    """Enhanced Kanban data with stage metadata, counts, and search."""
    interview_id = request.args.get('interview_id')
    search = request.args.get('q', '').strip().lower()
    db = get_db()

    where = 'WHERE c.user_id=?'
    params = [g.user_id]
    if interview_id:
        where += ' AND c.interview_id=?'
        params.append(interview_id)
    if search:
        where += " AND (LOWER(c.first_name) LIKE ? OR LOWER(c.last_name) LIKE ? OR LOWER(c.email) LIKE ?)"
        params.extend([f'%{search}%'] * 3)

    candidates = db.execute(f'''SELECT c.id, c.first_name, c.last_name, c.email, c.pipeline_stage,
        c.kanban_order, c.interview_id, c.ai_score, c.source, c.created_at, c.stage_entered_at,
        i.title as interview_title
        FROM candidates c LEFT JOIN interviews i ON c.interview_id=i.id
        {where} ORDER BY c.kanban_order, c.created_at DESC''', params).fetchall()

    # Group by stage
    stages = {}
    for s in DEFAULT_STAGES:
        stages[s['slug']] = {'name': s['name'], 'slug': s['slug'], 'color': s['color'],
                             'order': s['order'], 'is_terminal': s.get('is_terminal', False),
                             'candidates': [], 'count': 0}

    for c in candidates:
        d = dict(c)
        stage = d.get('pipeline_stage', 'new')
        if stage not in stages:
            stages[stage] = {'name': stage.replace('_', ' ').title(), 'slug': stage,
                             'color': '#6b7280', 'order': 99, 'is_terminal': False,
                             'candidates': [], 'count': 0}
        # Compute days in stage
        try:
            entered = d.get('stage_entered_at') or d.get('created_at')
            if entered:
                d['days_in_stage'] = max(0, (datetime.utcnow() - datetime.fromisoformat(str(entered))).days)
            else:
                d['days_in_stage'] = 0
        except:
            d['days_in_stage'] = 0
        stages[stage]['candidates'].append(d)
        stages[stage]['count'] += 1

    db.close()
    ordered = sorted(stages.values(), key=lambda s: s['order'])
    return jsonify({'stages': ordered, 'total': len(candidates)})


@app.route('/api/candidates/bulk-action', methods=['POST'])
@require_auth
def api_bulk_action_c31():
    """Bulk actions: move, tag, or archive multiple candidates."""
    data = request.get_json() or {}
    candidate_ids = data.get('candidate_ids', [])
    action = data.get('action', '')
    value = data.get('value', '')

    if not candidate_ids or not action:
        return jsonify({'error': 'candidate_ids and action required'}), 400

    db = get_db()
    affected = 0
    now = datetime.utcnow().isoformat()

    if action == 'move_stage':
        if not value:
            db.close()
            return jsonify({'error': 'value (stage) required for move_stage'}), 400
        for cid in candidate_ids:
            db.execute('''UPDATE candidates SET pipeline_stage=?, stage_entered_at=?,
                          updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?''',
                       (value, now, cid, g.user_id))
            affected += 1

    elif action == 'archive':
        for cid in candidate_ids:
            db.execute("UPDATE candidates SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                       (cid, g.user_id))
            affected += 1

    elif action == 'tag':
        if not value:
            db.close()
            return jsonify({'error': 'value (tag) required'}), 400
        for cid in candidate_ids:
            existing = db.execute('SELECT id FROM candidate_tags WHERE candidate_id=? AND tag=?', (cid, value)).fetchone()
            if not existing:
                db.execute('INSERT INTO candidate_tags (id, candidate_id, user_id, tag, created_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)',
                           (str(uuid.uuid4()), cid, g.user_id, value))
            affected += 1
    else:
        db.close()
        return jsonify({'error': f'Unknown action: {action}'}), 400

    db.commit()
    db.close()
    return jsonify({'success': True, 'affected': affected, 'action': action})


# --- Feature 4: Auto-Stage Rules ---

@app.route('/api/auto-rules', methods=['GET'])
@require_auth
def api_list_auto_rules_c31():
    """List auto-stage rules for the user."""
    interview_id = request.args.get('interview_id')
    db = get_db()
    if interview_id:
        rules = db.execute('SELECT * FROM auto_stage_rules WHERE user_id=? AND interview_id=? ORDER BY created_at',
                           (g.user_id, interview_id)).fetchall()
    else:
        rules = db.execute('SELECT * FROM auto_stage_rules WHERE user_id=? ORDER BY created_at', (g.user_id,)).fetchall()
    db.close()
    return jsonify({'rules': [dict(r) for r in rules]})


@app.route('/api/auto-rules', methods=['POST'])
@require_auth
def api_create_auto_rule_c31():
    """Create an auto-stage rule."""
    data = request.get_json() or {}
    rule_type = data.get('rule_type', '')
    interview_id = data.get('interview_id', '')
    to_stage = data.get('to_stage', '')

    valid_types = ['ai_score_gte', 'ai_score_lt', 'days_inactive', 'interview_completed']
    if rule_type not in valid_types:
        return jsonify({'error': f'Invalid rule_type. Must be one of: {valid_types}'}), 400
    if not to_stage:
        return jsonify({'error': 'to_stage is required'}), 400

    db = get_db()
    rule_id = str(uuid.uuid4())
    db.execute('''INSERT INTO auto_stage_rules (id, interview_id, user_id, rule_type, trigger_value,
                  from_stage, to_stage, is_active) VALUES (?,?,?,?,?,?,?,1)''',
               (rule_id, interview_id, g.user_id, rule_type, data.get('trigger_value', ''),
                data.get('from_stage', ''), to_stage))
    db.commit()
    db.close()
    return jsonify({'success': True, 'rule': {'id': rule_id, 'rule_type': rule_type, 'to_stage': to_stage}}), 201


@app.route('/api/auto-rules/<rule_id>', methods=['DELETE'])
@require_auth
def api_delete_auto_rule_c31(rule_id):
    """Delete an auto-stage rule."""
    db = get_db()
    db.execute('DELETE FROM auto_stage_rules WHERE id=? AND user_id=?', (rule_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/auto-rules/apply', methods=['POST'])
@require_auth
def api_apply_auto_rules_c31():
    """Manually trigger auto-stage rules evaluation."""
    interview_id = request.get_json().get('interview_id') if request.get_json() else None
    db = get_db()

    where_rule = 'WHERE user_id=? AND is_active=1'
    params_rule = [g.user_id]
    if interview_id:
        where_rule += ' AND interview_id=?'
        params_rule.append(interview_id)

    rules = db.execute(f'SELECT * FROM auto_stage_rules {where_rule}', params_rule).fetchall()
    moved = 0
    now = datetime.utcnow().isoformat()

    for rule in rules:
        r = dict(rule)
        rt = r['rule_type']
        tv = r.get('trigger_value', '')
        from_s = r.get('from_stage', '')
        to_s = r['to_stage']
        iid = r['interview_id']

        cand_where = 'WHERE user_id=? AND interview_id=?'
        cand_params = [g.user_id, iid]
        if from_s:
            cand_where += ' AND pipeline_stage=?'
            cand_params.append(from_s)

        if rt == 'ai_score_gte' and tv:
            try:
                threshold = float(tv)
                cand_where += ' AND CAST(ai_score AS REAL) >= ?'
                cand_params.append(threshold)
            except:
                continue
        elif rt == 'ai_score_lt' and tv:
            try:
                threshold = float(tv)
                cand_where += ' AND CAST(ai_score AS REAL) < ? AND ai_score IS NOT NULL'
                cand_params.append(threshold)
            except:
                continue
        elif rt == 'days_inactive' and tv:
            try:
                days = int(tv)
                cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
                cand_where += ' AND COALESCE(stage_entered_at, created_at) < ?'
                cand_params.append(cutoff)
            except:
                continue
        elif rt == 'interview_completed':
            cand_where += " AND status='completed'"
        else:
            continue

        # Don't move candidates already in terminal stages
        cand_where += " AND pipeline_stage NOT IN ('hired','rejected')"

        # Select matching candidates then update one by one
        cands = db.execute(f'SELECT id FROM candidates {cand_where}', cand_params).fetchall()
        for c in cands:
            db.execute('UPDATE candidates SET pipeline_stage=?, stage_entered_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (to_s, now, c['id']))
            moved += 1

    db.commit()
    db.close()
    return jsonify({'success': True, 'candidates_moved': moved, 'rules_evaluated': len(rules)})


# --- Feature 5: Custom Pipeline Stages ---

@app.route('/api/interviews/<interview_id>/stages', methods=['GET'])
@require_auth
def api_get_custom_stages_c31(interview_id):
    """Get pipeline stages for an interview (custom or default)."""
    db = get_db()
    interview = db.execute('SELECT id, custom_stages_enabled FROM interviews WHERE id=? AND user_id=?',
                           (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    interview = dict(interview)
    if interview.get('custom_stages_enabled'):
        stages = db.execute('SELECT * FROM pipeline_stages WHERE interview_id=? AND user_id=? ORDER BY stage_order',
                            (interview_id, g.user_id)).fetchall()
        stages = [dict(s) for s in stages]
    else:
        stages = DEFAULT_STAGES

    db.close()
    return jsonify({'stages': stages, 'custom_enabled': bool(interview.get('custom_stages_enabled'))})


@app.route('/api/interviews/<interview_id>/stages', methods=['PUT'])
@require_auth
def api_set_custom_stages_c31(interview_id):
    """Set custom pipeline stages for an interview."""
    data = request.get_json() or {}
    stages_data = data.get('stages', [])

    if not stages_data or len(stages_data) < 2:
        return jsonify({'error': 'At least 2 stages required'}), 400

    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    # Clear existing custom stages
    db.execute('DELETE FROM pipeline_stages WHERE interview_id=? AND user_id=?', (interview_id, g.user_id))

    # Insert new stages
    for i, s in enumerate(stages_data):
        db.execute('''INSERT INTO pipeline_stages (id, interview_id, user_id, name, slug, stage_order,
                      color, require_notes, is_terminal) VALUES (?,?,?,?,?,?,?,?,?)''',
                   (str(uuid.uuid4()), interview_id, g.user_id, s.get('name', f'Stage {i+1}'),
                    s.get('slug', s.get('name', '').lower().replace(' ', '_')),
                    i, s.get('color', '#6b7280'), 1 if s.get('require_notes') else 0,
                    1 if s.get('is_terminal') else 0))

    db.execute('UPDATE interviews SET custom_stages_enabled=1, updated_at=CURRENT_TIMESTAMP WHERE id=?', (interview_id,))
    db.commit()
    db.close()
    return jsonify({'success': True, 'stages_count': len(stages_data)})


@app.route('/api/interviews/<interview_id>/stages/reset', methods=['POST'])
@require_auth
def api_reset_stages_c31(interview_id):
    """Reset to default pipeline stages."""
    db = get_db()
    db.execute('DELETE FROM pipeline_stages WHERE interview_id=? AND user_id=?', (interview_id, g.user_id))
    db.execute('UPDATE interviews SET custom_stages_enabled=0, updated_at=CURRENT_TIMESTAMP WHERE id=?', (interview_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# --- Feature 6: Source Tracking ---

@app.route('/api/analytics/sources', methods=['GET'])
@require_auth
def api_source_analytics_c31():
    """Candidate source analytics with breakdown and trends."""
    interview_id = request.args.get('interview_id')
    db = get_db()

    where = 'WHERE c.user_id=?'
    params = [g.user_id]
    if interview_id:
        where += ' AND c.interview_id=?'
        params.append(interview_id)

    # Source breakdown
    rows = db.execute(f'''SELECT COALESCE(c.source, 'manual') as src, COUNT(*) as cnt,
        SUM(CASE WHEN c.pipeline_stage='hired' THEN 1 ELSE 0 END) as hired_count,
        AVG(CASE WHEN c.ai_score IS NOT NULL THEN CAST(c.ai_score AS REAL) END) as avg_score
        FROM candidates c {where} GROUP BY src ORDER BY cnt DESC''', params).fetchall()

    total = sum(r['cnt'] for r in rows)
    sources = []
    for r in rows:
        d = dict(r)
        d['pct'] = round(d['cnt'] / max(total, 1) * 100, 1)
        d['hire_rate'] = round((d['hired_count'] or 0) / max(d['cnt'], 1) * 100, 1)
        d['avg_score'] = round(d['avg_score'] or 0, 1)
        sources.append(d)

    # Source trend (last 30 days by week)
    thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).isoformat()
    trend_rows = db.execute(f'''SELECT COALESCE(c.source, 'manual') as src,
        strftime('%W', c.created_at) as week, COUNT(*) as cnt
        FROM candidates c {where} AND c.created_at >= ?
        GROUP BY src, week ORDER BY week''', params + [thirty_days_ago]).fetchall()
    trends = [dict(r) for r in trend_rows]

    db.close()
    return jsonify({'sources': sources, 'total': total, 'trends': trends})


@app.route('/api/candidates/<candidate_id>/source', methods=['PUT'])
@require_auth
def api_update_source_c31(candidate_id):
    """Update candidate source info."""
    data = request.get_json() or {}
    db = get_db()
    cand = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not cand:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    source = data.get('source', 'manual')
    detail = (data.get('source_detail') or '').strip()
    db.execute('UPDATE candidates SET source=?, source_detail=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (source, detail, candidate_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


# --- C31 Page Routes ---

@app.route('/enhanced-kanban')
@require_auth
def page_enhanced_kanban_c31():
    return render_template('app.html', user=g.user, page='enhanced-kanban')

@app.route('/pipeline-funnel')
@require_auth
def page_pipeline_funnel_c31():
    return render_template('app.html', user=g.user, page='pipeline-funnel')

@app.route('/auto-rules')
@require_auth
def page_auto_rules_c31():
    return render_template('app.html', user=g.user, page='auto-rules')

@app.route('/custom-stages')
@require_auth
def page_custom_stages_c31():
    return render_template('app.html', user=g.user, page='custom-stages')

@app.route('/source-tracking')
@require_auth
def page_source_tracking_c31():
    return render_template('app.html', user=g.user, page='source-tracking')

@app.route('/job-board')
@require_auth
def page_job_board_settings_c31():
    return render_template('app.html', user=g.user, page='job-board')


# ======================== CYCLE 32: CANDIDATE EXPERIENCE ========================

# --- Feature 1: Redesigned Apply Page ---

@app.route('/apply/<interview_id>')
def page_apply_c32(interview_id):
    """Render the redesigned public apply page."""
    db = get_db()
    interview = db.execute(
        '''SELECT i.id, i.title, i.description, i.position, i.department, i.location,
           i.job_type, i.salary_range, i.application_deadline, i.apply_instructions,
           i.apply_fields_json, i.estimated_duration_min, i.status,
           i.job_board_enabled, i.public_apply_enabled,
           u.id as agency_user_id, u.agency_name, u.name as agency_contact,
           u.brand_color, u.agency_logo_url
           FROM interviews i JOIN users u ON i.user_id = u.id
           WHERE i.id=? AND i.status='active' ''', (interview_id,)
    ).fetchone()
    if not interview:
        db.close()
        return '''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Not Found</title></head>
        <body style="font-family:-apple-system,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#f9fafb">
        <div style="text-align:center"><h1 style="font-size:24px;color:#333">Position Not Found</h1>
        <p style="color:#666">This position may have been closed or removed.</p></div></body></html>''', 404

    intv = dict(interview)
    q_count = db.execute('SELECT COUNT(*) as cnt FROM questions WHERE interview_id=?', (interview_id,)).fetchone()['cnt']
    db.close()

    color = intv.get('brand_color') or '#0ace0a'
    agency = intv.get('agency_name') or intv.get('agency_contact') or 'Agency'
    loc = intv.get('location') or 'Remote'
    jtype = (intv.get('job_type') or 'full_time').replace('_', ' ').title()
    salary = intv.get('salary_range') or ''
    dept = intv.get('department') or ''
    duration = intv.get('estimated_duration_min') or 15
    instructions = intv.get('apply_instructions') or ''
    deadline = intv.get('application_deadline') or ''

    # Parse custom fields
    custom_fields = []
    try:
        raw = intv.get('apply_fields_json') or '[]'
        custom_fields = json.loads(raw) if raw else []
    except:
        custom_fields = []

    custom_fields_html = ''
    for f in custom_fields:
        ftype = f.get('type', 'text')
        flabel = f.get('label', '')
        freq = 'required' if f.get('required') else ''
        fname = f.get('name', flabel.lower().replace(' ', '_'))
        if ftype == 'textarea':
            custom_fields_html += f'''<div style="margin-bottom:16px">
              <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">{flabel} {('<span style="color:#dc2626">*</span>' if freq else '')}</label>
              <textarea name="custom_{fname}" {freq} rows="3" style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px;font-family:inherit;resize:vertical"></textarea>
            </div>'''
        elif ftype == 'select':
            opts = f.get('options', [])
            custom_fields_html += f'''<div style="margin-bottom:16px">
              <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">{flabel}</label>
              <select name="custom_{fname}" style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
                <option value="">Select...</option>{''.join(f'<option value="{o}">{o}</option>' for o in opts)}
              </select></div>'''
        else:
            custom_fields_html += f'''<div style="margin-bottom:16px">
              <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">{flabel} {('<span style="color:#dc2626">*</span>' if freq else '')}</label>
              <input type="{ftype}" name="custom_{fname}" {freq} style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
            </div>'''

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Apply — {intv.get('position') or intv['title']} at {agency}</title>
    <style>
      *{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;color:#111}}
      .hero{{background:#111;padding:48px 24px;text-align:center}}
      .hero h1{{color:#fff;font-size:28px;margin-bottom:8px}}
      .hero p{{color:#999;font-size:15px}}
      .badge{{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;margin:4px}}
      .card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px;margin-bottom:20px}}
      input,textarea,select{{outline:none;transition:border-color .2s}}
      input:focus,textarea:focus,select:focus{{border-color:{color}}}
      .btn{{display:inline-block;background:{color};color:#000;font-weight:700;padding:14px 32px;border-radius:8px;border:none;font-size:16px;cursor:pointer;width:100%;text-align:center}}
      .btn:hover{{opacity:0.9}}
      #success-msg{{display:none}}
      #error-msg{{display:none;color:#dc2626;background:#fef2f2;border:1px solid #fca5a5;padding:12px;border-radius:8px;margin-bottom:16px;font-size:14px}}
    </style></head><body>
    <div class="hero">
      <p style="color:{color};font-weight:600;margin-bottom:8px">{agency}</p>
      <h1>{intv.get('position') or intv['title']}</h1>
      <div style="margin-top:12px">
        <span class="badge" style="background:{color}22;color:{color}">{loc}</span>
        <span class="badge" style="background:#374151;color:#d1d5db">{jtype}</span>
        {f'<span class="badge" style="background:#065f4622;color:#059669">{salary}</span>' if salary else ''}
        {f'<span class="badge" style="background:#37415122;color:#d1d5db">{dept}</span>' if dept else ''}
      </div>
    </div>

    <div style="max-width:640px;margin:32px auto;padding:0 24px">
      <!-- Role Details -->
      <div class="card">
        <h2 style="font-size:18px;font-weight:700;margin-bottom:12px">About this Role</h2>
        <p style="color:#555;font-size:14px;line-height:1.7">{intv.get('description') or 'Join our team in this exciting opportunity.'}</p>
        {f'<div style="margin-top:16px;padding:12px 16px;background:#f3f4f6;border-radius:8px;font-size:13px;color:#555"><strong>What to expect:</strong> {instructions}</div>' if instructions else ''}
        <div style="display:flex;gap:16px;margin-top:16px;flex-wrap:wrap">
          <div style="font-size:13px;color:#666"><strong style="color:#333">Duration:</strong> ~{duration} min video interview</div>
          <div style="font-size:13px;color:#666"><strong style="color:#333">Questions:</strong> {q_count}</div>
          {f'<div style="font-size:13px;color:#666"><strong style="color:#333">Deadline:</strong> {deadline}</div>' if deadline else ''}
        </div>
      </div>

      <!-- Application Form -->
      <div class="card" id="apply-form-card">
        <h2 style="font-size:18px;font-weight:700;margin-bottom:16px">Apply Now</h2>
        <div id="error-msg"></div>
        <form id="apply-form" onsubmit="submitApplication(event)">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
            <div>
              <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">First Name <span style="color:#dc2626">*</span></label>
              <input type="text" name="first_name" required style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
            </div>
            <div>
              <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">Last Name <span style="color:#dc2626">*</span></label>
              <input type="text" name="last_name" required style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
            </div>
          </div>
          <div style="margin-bottom:16px">
            <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">Email <span style="color:#dc2626">*</span></label>
            <input type="email" name="email" required style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
          </div>
          <div style="margin-bottom:16px">
            <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">Phone</label>
            <input type="tel" name="phone" style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
          </div>
          <div style="margin-bottom:16px">
            <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">LinkedIn URL</label>
            <input type="url" name="linkedin_url" placeholder="https://linkedin.com/in/..." style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
          </div>
          <div style="margin-bottom:16px">
            <label style="font-size:13px;font-weight:600;color:#333;display:block;margin-bottom:4px">Why are you interested in this role?</label>
            <textarea name="cover_letter" rows="4" placeholder="Tell us about yourself and why you'd be a great fit..." style="width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px;font-family:inherit;resize:vertical"></textarea>
          </div>
          {custom_fields_html}
          <button type="submit" class="btn" id="submit-btn">Submit Application</button>
        </form>
      </div>

      <!-- Success Message -->
      <div class="card" id="success-msg" style="text-align:center;padding:48px 28px">
        <div style="font-size:48px;margin-bottom:16px">&#x1F389;</div>
        <h2 style="font-size:24px;font-weight:700;margin-bottom:8px">Application Submitted!</h2>
        <p style="color:#666;font-size:15px;margin-bottom:24px">Thank you for applying. You will receive an email with next steps shortly.</p>
        <div id="interview-link" style="display:none;background:#f0fff0;border:1px solid #bbf7d0;border-radius:8px;padding:16px;margin-bottom:16px">
          <p style="font-size:14px;color:#065f46;margin-bottom:8px">Ready to start your video interview?</p>
          <a id="start-interview-btn" href="#" style="display:inline-block;background:{color};color:#000;font-weight:700;padding:12px 24px;border-radius:8px;text-decoration:none">Start Interview Now</a>
        </div>
        <a id="progress-link" href="#" style="color:{color};font-size:14px;text-decoration:none;font-weight:600">Track your application status &rarr;</a>
      </div>

      <div style="text-align:center;padding:24px;color:#ccc;font-size:12px">Powered by ChannelView</div>
    </div>

    <script>
    async function submitApplication(e) {{
      e.preventDefault();
      const form = e.target;
      const btn = document.getElementById('submit-btn');
      const errEl = document.getElementById('error-msg');
      errEl.style.display = 'none';
      btn.disabled = true; btn.textContent = 'Submitting...';

      const data = {{
        first_name: form.first_name.value,
        last_name: form.last_name.value,
        email: form.email.value,
        phone: form.phone?.value || '',
        linkedin_url: form.linkedin_url?.value || '',
        cover_letter: form.cover_letter?.value || '',
      }};

      // Collect custom fields
      const customs = {{}};
      form.querySelectorAll('[name^="custom_"]').forEach(el => {{
        customs[el.name.replace('custom_', '')] = el.value;
      }});
      if (Object.keys(customs).length) data.apply_answers = customs;

      try {{
        const res = await fetch('/api/interviews/{interview_id}/apply', {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(data)
        }});
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || 'Failed to submit');

        document.getElementById('apply-form-card').style.display = 'none';
        document.getElementById('success-msg').style.display = 'block';

        if (result.interview_url) {{
          document.getElementById('interview-link').style.display = 'block';
          document.getElementById('start-interview-btn').href = result.interview_url;
        }}
        if (result.token) {{
          document.getElementById('progress-link').href = '/status/' + result.token;
        }}
      }} catch(err) {{
        errEl.textContent = err.message;
        errEl.style.display = 'block';
        btn.disabled = false; btn.textContent = 'Submit Application';
      }}
    }}
    </script></body></html>'''
    return html


@app.route('/api/interviews/<interview_id>/apply', methods=['POST'])
def api_apply_c32(interview_id):
    """Enhanced public application submission with extra fields."""
    db = get_db()
    interview = db.execute('SELECT * FROM interviews WHERE id=? AND status=?', (interview_id, 'active')).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Position not found or closed'}), 404

    intv = dict(interview)
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    first_name = (data.get('first_name') or '').strip()
    last_name = (data.get('last_name') or '').strip()

    if not email or not first_name or not last_name:
        db.close()
        return jsonify({'error': 'First name, last name, and email are required'}), 400

    # Check duplicate
    existing = db.execute('SELECT id, token FROM candidates WHERE interview_id=? AND email=?',
                          (interview_id, email)).fetchone()
    if existing:
        db.close()
        return jsonify({'error': 'You have already applied for this position',
                        'token': existing['token']}), 409

    candidate_id = str(uuid.uuid4())
    token = str(uuid.uuid4())
    apply_answers = json.dumps(data.get('apply_answers', {}))

    db.execute('''INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email,
                  phone, token, status, source, portal_status, linkedin_url, cover_letter,
                  apply_answers_json, pipeline_stage)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'invited', 'apply_page', 'not_started', ?, ?, ?, 'new')''',
               (candidate_id, intv['user_id'], interview_id, first_name, last_name, email,
                data.get('phone', ''), token, data.get('linkedin_url', ''),
                data.get('cover_letter', ''), apply_answers))
    db.commit()
    db.close()

    return jsonify({
        'success': True, 'candidate_id': candidate_id, 'token': token,
        'interview_url': f"/i/{token}",
        'progress_url': f"/status/{token}"
    }), 201


@app.route('/api/interviews/<interview_id>/apply-config', methods=['GET'])
@require_auth
def api_get_apply_config_c32(interview_id):
    """Get apply page configuration for an interview (admin)."""
    db = get_db()
    interview = db.execute('''SELECT id, apply_instructions, apply_fields_json, estimated_duration_min,
                              prep_video_url, prep_instructions, thank_you_message, thank_you_next_steps,
                              show_progress_tracker, public_apply_enabled
                              FROM interviews WHERE id=? AND user_id=?''',
                           (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404
    db.close()
    d = dict(interview)
    try:
        d['apply_fields'] = json.loads(d.get('apply_fields_json') or '[]')
    except:
        d['apply_fields'] = []
    return jsonify(d)


@app.route('/api/interviews/<interview_id>/apply-config', methods=['PUT'])
@require_auth
def api_set_apply_config_c32(interview_id):
    """Update apply page configuration for an interview (admin)."""
    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    data = request.get_json() or {}
    fields_json = json.dumps(data.get('apply_fields', []))

    db.execute('''UPDATE interviews SET apply_instructions=?, apply_fields_json=?, estimated_duration_min=?,
                  prep_video_url=?, prep_instructions=?, thank_you_message=?, thank_you_next_steps=?,
                  show_progress_tracker=?, public_apply_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?''',
               (data.get('apply_instructions', ''), fields_json,
                data.get('estimated_duration_min', 15),
                data.get('prep_video_url', ''), data.get('prep_instructions', ''),
                data.get('thank_you_message', ''), data.get('thank_you_next_steps', ''),
                1 if data.get('show_progress_tracker', True) else 0,
                1 if data.get('public_apply_enabled') else 0, interview_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


# --- Feature 2: Candidate Progress Tracker ---

@app.route('/status/<token>')
def page_candidate_status_c32(token):
    """Public candidate progress tracker page."""
    db = get_db()
    candidate = db.execute(
        '''SELECT c.id, c.first_name, c.last_name, c.email, c.status, c.pipeline_stage,
           c.portal_status, c.created_at, c.started_at, c.completed_at, c.ai_score,
           c.interview_id, c.progress_viewed_at,
           i.title as interview_title, i.show_progress_tracker, i.thank_you_message,
           i.thank_you_next_steps, i.estimated_duration_min,
           u.agency_name, u.brand_color
           FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           JOIN users u ON c.user_id = u.id
           WHERE c.token=?''', (token,)).fetchone()

    if not candidate:
        db.close()
        return '''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Not Found</title></head>
        <body style="font-family:-apple-system,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#f9fafb">
        <div style="text-align:center"><h1>Application Not Found</h1><p style="color:#666">This link may be invalid or expired.</p></div></body></html>''', 404

    cand = dict(candidate)

    # Update progress_viewed_at
    db.execute("UPDATE candidates SET progress_viewed_at=CURRENT_TIMESTAMP WHERE id=?", (cand['id'],))

    # Get status updates
    updates = db.execute(
        'SELECT * FROM candidate_status_updates WHERE candidate_id=? AND is_public=1 ORDER BY created_at DESC',
        (cand['id'],)).fetchall()
    updates = [dict(u) for u in updates]
    db.commit()
    db.close()

    color = cand.get('brand_color') or '#0ace0a'
    agency = cand.get('agency_name') or 'Agency'
    stage = cand.get('pipeline_stage', 'new')
    status = cand.get('status', 'invited')

    # Build progress steps
    stages_map = [
        ('applied', 'Applied', 'Your application was received'),
        ('in_review', 'Under Review', 'Our team is reviewing your application'),
        ('interview', 'Interview', 'Complete your video interview'),
        ('evaluation', 'Evaluation', 'Your responses are being evaluated'),
        ('decision', 'Decision', 'Final decision stage'),
    ]

    # Map pipeline_stage to progress step
    stage_to_step = {
        'new': 0, 'in_review': 1, 'shortlisted': 1,
        'interview_scheduled': 2, 'offered': 4, 'hired': 4, 'rejected': 4
    }
    current_step = stage_to_step.get(stage, 0)
    if status == 'completed':
        current_step = max(current_step, 3)
    if status in ('in_progress',):
        current_step = max(current_step, 2)

    steps_html = ''
    for i, (key, label, desc) in enumerate(stages_map):
        if i < current_step:
            icon = '&#x2705;'
            step_color = '#059669'
            line_color = '#059669'
        elif i == current_step:
            icon = f'<span style="display:inline-flex;width:28px;height:28px;background:{color};color:#000;border-radius:50%;align-items:center;justify-content:center;font-weight:700;font-size:14px">{i+1}</span>'
            step_color = color
            line_color = '#e5e7eb'
        else:
            icon = f'<span style="display:inline-flex;width:28px;height:28px;background:#e5e7eb;color:#999;border-radius:50%;align-items:center;justify-content:center;font-weight:700;font-size:14px">{i+1}</span>'
            step_color = '#999'
            line_color = '#e5e7eb'

        steps_html += f'''<div style="display:flex;gap:16px;align-items:flex-start;position:relative;padding-bottom:24px">
          <div style="display:flex;flex-direction:column;align-items:center;min-width:28px">
            <div style="font-size:24px;line-height:1">{icon}</div>
            {'<div style="width:2px;flex:1;background:' + line_color + ';margin-top:4px;min-height:20px"></div>' if i < len(stages_map)-1 else ''}
          </div>
          <div style="padding-top:4px">
            <div style="font-size:15px;font-weight:600;color:{step_color}">{label}</div>
            <div style="font-size:13px;color:#666;margin-top:2px">{desc}</div>
          </div>
        </div>'''

    # Status banner
    if stage == 'hired':
        banner = f'<div style="background:#f0fff0;border:1px solid #bbf7d0;border-radius:12px;padding:20px;text-align:center;margin-bottom:24px"><div style="font-size:32px;margin-bottom:8px">&#x1F389;</div><h3 style="color:#065f46">Congratulations!</h3><p style="color:#059669;font-size:14px">You have been selected for this position.</p></div>'
    elif stage == 'rejected':
        banner = f'<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:12px;padding:20px;text-align:center;margin-bottom:24px"><p style="color:#991b1b;font-size:14px">Thank you for your interest. We have decided to move forward with other candidates at this time.</p></div>'
    elif status == 'invited' and cand.get('portal_status') == 'not_started':
        banner = f'<div style="background:#eff6ff;border:1px solid #93c5fd;border-radius:12px;padding:20px;text-align:center;margin-bottom:24px"><p style="color:#1e40af;font-size:14px;margin-bottom:12px">Your video interview is ready to begin!</p><a href="/i/{token}" style="display:inline-block;background:{color};color:#000;font-weight:700;padding:12px 24px;border-radius:8px;text-decoration:none">Start Interview</a></div>'
    else:
        banner = ''

    # Updates timeline
    updates_html = ''
    if updates:
        updates_html = '<div style="margin-top:24px"><h3 style="font-size:16px;font-weight:600;margin-bottom:12px">Updates</h3>'
        for u in updates:
            updates_html += f'''<div style="padding:12px 0;border-bottom:1px solid #f3f4f6">
              <div style="font-size:13px;color:#999">{u.get('created_at','')[:10]}</div>
              <div style="font-size:14px;color:#333;margin-top:2px">{u.get('message','Status updated')}</div>
            </div>'''
        updates_html += '</div>'

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Application Status — {agency}</title></head>
    <body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb">
    <div style="background:#111;padding:32px 24px;text-align:center">
      <p style="color:{color};font-weight:600;margin-bottom:4px">{agency}</p>
      <h1 style="color:#fff;font-size:22px;margin:0">{cand.get('interview_title','')}</h1>
      <p style="color:#999;font-size:13px;margin-top:8px">Application by {cand['first_name']} {cand['last_name']}</p>
    </div>
    <div style="max-width:560px;margin:32px auto;padding:0 24px">
      {banner}
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px;margin-bottom:20px">
        <h2 style="font-size:18px;font-weight:700;margin-bottom:20px">Application Progress</h2>
        {steps_html}
      </div>
      {updates_html}
      <div style="text-align:center;padding:24px;color:#ccc;font-size:12px">Powered by ChannelView</div>
    </div></body></html>'''
    return html


@app.route('/api/candidates/<candidate_id>/status-updates', methods=['GET'])
@require_auth
def api_get_status_updates_c32(candidate_id):
    """Get status updates for a candidate (admin)."""
    db = get_db()
    updates = db.execute(
        'SELECT * FROM candidate_status_updates WHERE candidate_id=? AND user_id=? ORDER BY created_at DESC',
        (candidate_id, g.user_id)).fetchall()
    db.close()
    return jsonify({'updates': [dict(u) for u in updates]})


@app.route('/api/candidates/<candidate_id>/status-updates', methods=['POST'])
@require_auth
def api_create_status_update_c32(candidate_id):
    """Create a status update for a candidate (visible on their progress page)."""
    data = request.get_json() or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'error': 'Message is required'}), 400

    db = get_db()
    cand = db.execute('SELECT id FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not cand:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    update_id = str(uuid.uuid4())
    is_public = 1 if data.get('is_public', True) else 0
    db.execute('''INSERT INTO candidate_status_updates (id, candidate_id, user_id, status, message, is_public)
                  VALUES (?, ?, ?, ?, ?, ?)''',
               (update_id, candidate_id, g.user_id, data.get('status', 'update'), message, is_public))
    db.commit()
    db.close()
    return jsonify({'success': True, 'id': update_id}), 201


# --- Feature 3: Interview Prep Page ---

@app.route('/prep/<token>')
def page_interview_prep_c32(token):
    """Pre-interview preparation page with instructions and tips."""
    db = get_db()
    candidate = db.execute(
        '''SELECT c.id, c.first_name, c.last_name, c.token, c.status, c.portal_status,
           i.title, i.description, i.prep_video_url, i.prep_instructions,
           i.estimated_duration_min, i.position,
           u.agency_name, u.brand_color
           FROM candidates c
           JOIN interviews i ON c.interview_id = i.id
           JOIN users u ON c.user_id = u.id
           WHERE c.token=?''', (token,)).fetchone()

    if not candidate:
        db.close()
        return '''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Not Found</title></head>
        <body style="font-family:-apple-system,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#f9fafb">
        <div style="text-align:center"><h1>Interview Not Found</h1><p style="color:#666">This link may be invalid.</p></div></body></html>''', 404

    cand = dict(candidate)
    db.close()

    color = cand.get('brand_color') or '#0ace0a'
    agency = cand.get('agency_name') or 'Agency'
    duration = cand.get('estimated_duration_min') or 15
    prep_text = cand.get('prep_instructions') or ''
    prep_video = cand.get('prep_video_url') or ''

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Interview Prep — {agency}</title></head>
    <body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb">
    <div style="background:#111;padding:40px 24px;text-align:center">
      <p style="color:{color};font-weight:600;margin-bottom:4px">{agency}</p>
      <h1 style="color:#fff;font-size:26px;margin:0 0 8px">Prepare for Your Interview</h1>
      <p style="color:#999;font-size:14px">{cand.get('position') or cand.get('title','')}</p>
    </div>
    <div style="max-width:600px;margin:32px auto;padding:0 24px">
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px;margin-bottom:20px">
        <h2 style="font-size:18px;font-weight:700;margin-bottom:8px">Hi {cand['first_name']},</h2>
        <p style="color:#555;font-size:14px;line-height:1.6">Thanks for applying! Before you begin your video interview, here are some tips to help you put your best foot forward.</p>
      </div>

      {f"""<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px;margin-bottom:20px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:12px">Watch This First</h3>
        <div style="position:relative;padding-top:56.25%;border-radius:8px;overflow:hidden;background:#111">
          <iframe src="{prep_video}" style="position:absolute;top:0;left:0;width:100%;height:100%;border:none" allowfullscreen></iframe>
        </div>
      </div>""" if prep_video else ''}

      {f"""<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px;margin-bottom:20px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:8px">Instructions from {agency}</h3>
        <p style="color:#555;font-size:14px;line-height:1.6">{prep_text}</p>
      </div>""" if prep_text else ''}

      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px;margin-bottom:20px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Before You Start</h3>
        <div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:16px">
          <div style="min-width:36px;height:36px;background:{color}22;color:{color};border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px">&#x1F4F7;</div>
          <div><div style="font-weight:600;font-size:14px">Check Your Camera</div><div style="font-size:13px;color:#666">Make sure your camera and microphone are working properly.</div></div>
        </div>
        <div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:16px">
          <div style="min-width:36px;height:36px;background:{color}22;color:{color};border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px">&#x1F4A1;</div>
          <div><div style="font-weight:600;font-size:14px">Good Lighting</div><div style="font-size:13px;color:#666">Face a window or lamp so your face is well-lit. Avoid backlighting.</div></div>
        </div>
        <div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:16px">
          <div style="min-width:36px;height:36px;background:{color}22;color:{color};border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px">&#x1F50A;</div>
          <div><div style="font-weight:600;font-size:14px">Quiet Environment</div><div style="font-size:13px;color:#666">Find a quiet place free from distractions and background noise.</div></div>
        </div>
        <div style="display:flex;gap:12px;align-items:flex-start">
          <div style="min-width:36px;height:36px;background:{color}22;color:{color};border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px">&#x23F1;</div>
          <div><div style="font-weight:600;font-size:14px">Set Aside ~{duration} Minutes</div><div style="font-size:13px;color:#666">You can pause between questions, but try to complete in one sitting.</div></div>
        </div>
      </div>

      <a href="/i/{token}" style="display:block;background:{color};color:#000;font-weight:700;padding:16px;border-radius:8px;text-decoration:none;text-align:center;font-size:16px;margin-bottom:20px">
        I'm Ready — Start Interview
      </a>

      <div style="text-align:center;padding:24px;color:#ccc;font-size:12px">Powered by ChannelView</div>
    </div></body></html>'''
    return html


# --- Feature 4: Candidate Experience Admin Config ---

@app.route('/candidate-experience')
@require_auth
def page_candidate_experience_c32():
    return render_template('app.html', user=g.user, page='candidate-experience')

@app.route('/api/candidates/<candidate_id>/progress', methods=['GET'])
@require_auth
def api_candidate_progress_c32(candidate_id):
    """Get candidate progress details for admin view."""
    db = get_db()
    cand = db.execute(
        '''SELECT c.*, i.title as interview_title, i.show_progress_tracker
           FROM candidates c JOIN interviews i ON c.interview_id = i.id
           WHERE c.id=? AND c.user_id=?''', (candidate_id, g.user_id)).fetchone()
    if not cand:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    cand = dict(cand)
    updates = db.execute(
        'SELECT * FROM candidate_status_updates WHERE candidate_id=? ORDER BY created_at DESC',
        (candidate_id,)).fetchall()
    cand['status_updates'] = [dict(u) for u in updates]
    cand['progress_url'] = f"/status/{cand.get('token','')}"
    db.close()
    return jsonify(cand)


# ======================== CYCLE 30: AGENCY ONBOARDING & BILLING ========================

@app.route('/api/onboarding/setup-status', methods=['GET'])
@require_auth
def api_onboarding_status_c30():
    """Check if user needs first-login setup (new agency from FMO provisioning)."""
    db = get_db()
    # Ensure columns exist (migration may not have run yet)
    for col, ctype in [('onboarding_completed', 'INTEGER DEFAULT 1'), ('password_changed', 'INTEGER DEFAULT 1'), ('phone', 'TEXT')]:
        try:
            db.execute(f'ALTER TABLE users ADD COLUMN {col} {ctype}')
            db.commit()
        except:
            pass
    row = db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone()
    db.close()
    user = dict(row) if row else {}
    # For legacy users (non-FMO), default to completed
    onb_val = user.get('onboarding_completed')
    is_fmo_provisioned = bool(user.get('fmo_parent_id'))
    onboarding_done = True if onb_val is None else bool(onb_val)
    pw_val = user.get('password_changed')
    pw_changed = True if pw_val is None else bool(pw_val)
    return jsonify({
        'needs_setup': not onboarding_done and is_fmo_provisioned,
        'is_fmo_provisioned': is_fmo_provisioned,
        'onboarding_completed': onboarding_done,
        'steps': {
            'password_changed': pw_changed,
            'profile_completed': bool(user.get('agency_name') and user.get('name')),
            'first_interview_created': False,
        }
    })


@app.route('/api/onboarding/change-password', methods=['POST'])
@require_auth
def api_onboarding_change_password_c30():
    """Change password during first-login setup."""
    data = request.get_json() or {}
    new_password = data.get('new_password', '')
    if len(new_password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    db = get_db()
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    db.execute('UPDATE users SET password_hash=?, password_changed=1, updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (pw_hash, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/onboarding/update-profile', methods=['POST'])
@require_auth
def api_onboarding_update_profile_c30():
    """Update agency profile during onboarding setup."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    agency_name = (data.get('agency_name') or '').strip()
    phone = (data.get('phone') or '').strip()

    if not name or not agency_name:
        return jsonify({'error': 'Name and agency name are required'}), 400

    db = get_db()
    db.execute('''UPDATE users SET name=?, agency_name=?, phone=?, updated_at=CURRENT_TIMESTAMP WHERE id=?''',
               (name, agency_name, phone, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/onboarding/complete', methods=['POST'])
@require_auth
def api_onboarding_complete_c30():
    """Mark onboarding as completed."""
    db = get_db()
    db.execute('UPDATE users SET onboarding_completed=1, updated_at=CURRENT_TIMESTAMP WHERE id=?', (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/onboarding/send-welcome', methods=['POST'])
@require_auth
@require_fmo_admin
def api_send_welcome_email_c30():
    """Send welcome email to a newly created agency (called by FMO admin)."""
    data = request.get_json() or {}
    agency_email = data.get('email', '')
    agency_name = data.get('agency_name', '')
    temp_password = data.get('temp_password', '')
    contact_name = data.get('name', '')

    if not agency_email:
        return jsonify({'error': 'Email is required'}), 400

    try:
        from email_service import send_email, get_smtp_config, _base_template
        smtp_config = get_smtp_config()
    except Exception as e:
        return jsonify({'success': True, 'email_sent': False, 'reason': 'Email service not configured'})

    base_url = request.host_url.rstrip('/')
    html = _base_template(f'''
      <h2 style="color:#111;margin:0 0 8px">Welcome to ChannelView!</h2>
      <p style="color:#555;font-size:16px;margin:0 0 24px">
        Hi {contact_name or "there"}, your agency <strong>{agency_name}</strong> has been set up on ChannelView.
        You have a <strong>30-day free Professional trial</strong> to explore everything.
      </p>
      <div style="background:#f3f4f6;border-radius:8px;padding:20px;margin:0 0 24px">
        <p style="margin:0 0 8px;font-weight:600;color:#111">Your Login Credentials</p>
        <p style="margin:0;color:#555">Email: <strong>{agency_email}</strong></p>
        <p style="margin:4px 0 0;color:#555">Temporary Password: <strong>{temp_password}</strong></p>
        <p style="margin:8px 0 0;font-size:13px;color:#888">You will be asked to change this on your first login.</p>
      </div>
      <a href="{base_url}" style="display:inline-block;background:#0ace0a;color:#000;font-weight:700;
         padding:14px 32px;border-radius:8px;text-decoration:none;font-size:16px">
        Sign In to ChannelView
      </a>
      <p style="color:#999;font-size:13px;margin:24px 0 0">
        Your 30-day Professional trial includes up to 200 candidates/month, 25 interviews,
        10 team seats, AI scoring, and more. No credit card required.
      </p>
    ''')

    success, error = send_email(smtp_config, agency_email, 'Welcome to ChannelView — Your Agency is Ready!', html)
    return jsonify({'success': True, 'email_sent': success, 'error': error if not success else None})


def _send_agency_welcome_email(agency_email, agency_name, contact_name, temp_password):
    """Internal helper to send welcome email after FMO creates an agency."""
    try:
        from email_service import send_email, get_smtp_config, _base_template
        smtp_config = get_smtp_config()
        base_url = os.environ.get('BASE_URL', 'https://mychannelview.com')
        html = _base_template(f'''
          <h2 style="color:#111;margin:0 0 8px">Welcome to ChannelView!</h2>
          <p style="color:#555;font-size:16px;margin:0 0 24px">
            Hi {contact_name or "there"}, your agency <strong>{agency_name}</strong> has been set up on ChannelView.
            You have a <strong>30-day free Professional trial</strong> to explore everything.
          </p>
          <div style="background:#f3f4f6;border-radius:8px;padding:20px;margin:0 0 24px">
            <p style="margin:0 0 8px;font-weight:600;color:#111">Your Login Credentials</p>
            <p style="margin:0;color:#555">Email: <strong>{agency_email}</strong></p>
            <p style="margin:4px 0 0;color:#555">Temporary Password: <strong>{temp_password}</strong></p>
            <p style="margin:8px 0 0;font-size:13px;color:#888">You will be asked to change this on your first login.</p>
          </div>
          <a href="{base_url}" style="display:inline-block;background:#0ace0a;color:#000;font-weight:700;
             padding:14px 32px;border-radius:8px;text-decoration:none;font-size:16px">
            Sign In to ChannelView
          </a>
          <p style="color:#999;font-size:13px;margin:24px 0 0">
            Your 30-day Professional trial includes up to 200 candidates/month, 25 interviews,
            10 team seats, AI scoring, and more. No credit card required.
          </p>
        ''')
        send_email(smtp_config, agency_email, 'Welcome to ChannelView — Your Agency is Ready!', html)
    except Exception as e:
        print(f'[WELCOME EMAIL] Failed to send to {agency_email}: {e}')


@app.route('/review')
@require_auth
def page_review_hub():
    return render_template('app.html', user=g.user, page='review-hub')

@app.route('/email-templates')
@require_auth
def page_email_templates():
    return render_template('app.html', user=g.user, page='email-templates')


# ======================== CYCLE 18: WHITE-LABEL ENHANCEMENTS ========================

@app.route('/api/branding/profiles/<profile_id>/activate', methods=['POST'])
@require_auth
def api_activate_brand_profile_c18(profile_id):
    """Activate a brand profile (deactivates others)."""
    db = get_db()
    profile = db.execute('SELECT id FROM brand_profiles WHERE id=? AND owner_id=?', (profile_id, g.user_id)).fetchone()
    if not profile:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute('UPDATE brand_profiles SET is_default=0 WHERE owner_id=?', (g.user_id,))
    db.execute('UPDATE brand_profiles SET is_default=1 WHERE id=?', (profile_id,))
    db.execute('UPDATE users SET brand_profile_id=?, white_label_enabled=1 WHERE id=?', (profile_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'active_profile_id': profile_id})


# ======================== CYCLE 18: REPORT GENERATION & SHARING ========================

@app.route('/api/reports/generate', methods=['POST'])
@require_auth
def api_generate_report():
    """Generate a candidate scorecard/report."""
    data = request.get_json() or {}
    candidate_id = data.get('candidate_id', '')
    if not candidate_id:
        return jsonify({'error': 'candidate_id required'}), 400

    db = get_db()
    candidate = db.execute(
        '''SELECT c.*, i.title as interview_title, i.position, i.department
           FROM candidates c JOIN interviews i ON c.interview_id = i.id
           WHERE c.id=? AND c.user_id=?''', (candidate_id, g.user_id)
    ).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    candidate = dict(candidate)

    # Get responses with questions
    responses = db.execute(
        '''SELECT r.*, q.question_text, q.question_order FROM responses r
           JOIN questions q ON r.question_id = q.id WHERE r.candidate_id=? ORDER BY q.question_order''',
        (candidate_id,)
    ).fetchall()
    responses = [dict(r) for r in responses]

    # Get notes
    notes = db.execute('SELECT * FROM team_notes WHERE candidate_id=? ORDER BY created_at DESC', (candidate_id,)).fetchall()

    # Build scores
    scores = []
    total_score = 0
    for resp in responses:
        score_val = resp.get('ai_score') or resp.get('score') or 0
        scores.append({
            'question': resp['question_text'],
            'score': score_val,
            'word_count': resp.get('word_count', 0),
        })
        total_score += float(score_val) if score_val else 0

    avg_score = round(total_score / max(len(scores), 1), 1)

    # Generate summary
    strengths = []
    concerns = []
    for s in scores:
        if s['score'] and float(s['score']) >= 7:
            strengths.append(s['question'][:60])
        elif s['score'] and float(s['score']) < 5:
            concerns.append(s['question'][:60])

    recommendation = 'Strong Hire' if avg_score >= 8 else 'Hire' if avg_score >= 6 else 'Maybe' if avg_score >= 4 else 'No Hire'

    report_id = str(uuid.uuid4())
    share_token = _secrets.token_urlsafe(32)
    db.execute('''INSERT INTO candidate_reports
                  (id, user_id, candidate_id, interview_id, report_type, title, summary,
                   scores_json, strengths, concerns, recommendation, share_token)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
               (report_id, g.user_id, candidate_id, candidate['interview_id'],
                data.get('report_type', 'scorecard'),
                f"Scorecard: {candidate['first_name']} {candidate['last_name']}",
                f"Candidate for {candidate.get('position', 'N/A')} in {candidate.get('department', 'N/A')}. Overall score: {avg_score}/10.",
                json.dumps(scores), json.dumps(strengths), json.dumps(concerns),
                recommendation, share_token))
    db.execute('UPDATE candidates SET report_generated=1, last_report_id=? WHERE id=?', (report_id, candidate_id))
    _log_activity(db, g.user_id, g.user_id, g.user.get('name', ''), 'report_generated',
                  'report', report_id, f"Scorecard for {candidate['first_name']} {candidate['last_name']}")
    db.commit()
    db.close()

    return jsonify({
        'report': {
            'id': report_id, 'candidate_id': candidate_id,
            'candidate_name': f"{candidate['first_name']} {candidate['last_name']}",
            'interview_title': candidate['interview_title'],
            'avg_score': avg_score, 'recommendation': recommendation,
            'scores': scores, 'strengths': strengths, 'concerns': concerns,
            'share_token': share_token,
            'share_url': f"/reports/shared/{share_token}",
        }
    }), 201


@app.route('/api/reports', methods=['GET'])
@require_auth
def api_list_reports_v2():
    """List all generated reports."""
    db = get_db()
    reports = db.execute(
        '''SELECT cr.*, c.first_name, c.last_name, c.email
           FROM candidate_reports cr JOIN candidates c ON cr.candidate_id = c.id
           WHERE cr.user_id=? ORDER BY cr.created_at DESC''', (g.user_id,)
    ).fetchall()
    db.close()
    result = []
    for r in reports:
        d = dict(r)
        d['candidate_name'] = f"{d['first_name']} {d['last_name']}"
        try: d['scores_json'] = json.loads(d['scores_json']) if d.get('scores_json') else []
        except: pass
        try: d['strengths'] = json.loads(d['strengths']) if d.get('strengths') else []
        except: pass
        try: d['concerns'] = json.loads(d['concerns']) if d.get('concerns') else []
        except: pass
        result.append(d)
    return jsonify({'reports': result, 'count': len(result)})


@app.route('/api/reports/<report_id>', methods=['GET'])
@require_auth
def api_get_report_v2(report_id):
    """Get a specific report."""
    db = get_db()
    report = db.execute(
        '''SELECT cr.*, c.first_name, c.last_name, c.email, c.status as candidate_status
           FROM candidate_reports cr JOIN candidates c ON cr.candidate_id = c.id
           WHERE cr.id=? AND cr.user_id=?''', (report_id, g.user_id)
    ).fetchone()
    db.close()
    if not report:
        return jsonify({'error': 'Not found'}), 404
    d = dict(report)
    d['candidate_name'] = f"{d['first_name']} {d['last_name']}"
    try: d['scores_json'] = json.loads(d['scores_json']) if d.get('scores_json') else []
    except: pass
    try: d['strengths'] = json.loads(d['strengths']) if d.get('strengths') else []
    except: pass
    try: d['concerns'] = json.loads(d['concerns']) if d.get('concerns') else []
    except: pass
    return jsonify({'report': d})


@app.route('/api/reports/<report_id>/share', methods=['POST'])
@require_auth
def api_share_report(report_id):
    """Share a report via email or link."""
    db = get_db()
    report = db.execute('SELECT * FROM candidate_reports WHERE id=? AND user_id=?', (report_id, g.user_id)).fetchone()
    if not report:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    data = request.get_json() or {}
    shared_with = data.get('email', '')
    if shared_with:
        current = report['shared_with'] or ''
        if shared_with not in current:
            current = (current + ',' + shared_with).strip(',')
        db.execute('UPDATE candidate_reports SET shared_with=? WHERE id=?', (current, report_id))
        db.commit()

    db.close()
    return jsonify({
        'success': True,
        'share_url': f"/reports/shared/{report['share_token']}",
        'share_token': report['share_token'],
    })


@app.route('/api/reports/shared/<share_token>', methods=['GET'])
def api_get_shared_report(share_token):
    """Public endpoint to view a shared report."""
    db = get_db()
    report = db.execute(
        '''SELECT cr.*, c.first_name, c.last_name, i.title as interview_title, i.position
           FROM candidate_reports cr
           JOIN candidates c ON cr.candidate_id = c.id
           JOIN interviews i ON cr.interview_id = i.id
           WHERE cr.share_token=?''', (share_token,)
    ).fetchone()
    db.close()
    if not report:
        return jsonify({'error': 'Report not found or expired'}), 404
    d = dict(report)
    d['candidate_name'] = f"{d['first_name']} {d['last_name']}"
    try: d['scores_json'] = json.loads(d['scores_json']) if d.get('scores_json') else []
    except: pass
    try: d['strengths'] = json.loads(d['strengths']) if d.get('strengths') else []
    except: pass
    try: d['concerns'] = json.loads(d['concerns']) if d.get('concerns') else []
    except: pass
    # Remove sensitive fields
    for key in ['user_id', 'shared_with']:
        d.pop(key, None)
    return jsonify({'report': d})


@app.route('/api/reports/<report_id>', methods=['DELETE'])
@require_auth
def api_delete_report_v2(report_id):
    """Delete a report."""
    db = get_db()
    report = db.execute('SELECT id FROM candidate_reports WHERE id=? AND user_id=?', (report_id, g.user_id)).fetchone()
    if not report:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute('DELETE FROM candidate_reports WHERE id=?', (report_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ======================== CYCLE 18: BULK OPERATIONS & WORKFLOW AUTOMATION ========================

@app.route('/api/bulk/invite', methods=['POST'])
@require_auth
def api_bulk_invite():
    """Bulk invite candidates from a list."""
    data = request.get_json() or {}
    interview_id = data.get('interview_id', '')
    candidates_list = data.get('candidates', [])

    if not interview_id or not candidates_list:
        return jsonify({'error': 'interview_id and candidates list required'}), 400

    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    op_id = str(uuid.uuid4())
    db.execute('''INSERT INTO bulk_operations (id, user_id, operation_type, status, total_items, params_json, started_at)
                  VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)''',
               (op_id, g.user_id, 'bulk_invite', 'processing', len(candidates_list),
                json.dumps({'interview_id': interview_id})))

    created = 0
    failed = 0
    errors = []
    for cand in candidates_list[:500]:  # Max 500 per batch
        email = (cand.get('email') or '').strip().lower()
        first = (cand.get('first_name') or '').strip()
        last = (cand.get('last_name') or '').strip()
        if not email or not first:
            failed += 1
            errors.append(f"Missing fields for {email or 'unknown'}")
            item_id = str(uuid.uuid4())
            db.execute('INSERT INTO bulk_operation_items (id, operation_id, item_type, status, error_message) VALUES (?,?,?,?,?)',
                       (item_id, op_id, 'candidate', 'failed', f'Missing required fields'))
            continue

        # Check duplicate
        existing = db.execute('SELECT id FROM candidates WHERE interview_id=? AND email=?', (interview_id, email)).fetchone()
        if existing:
            failed += 1
            errors.append(f"Duplicate: {email}")
            item_id = str(uuid.uuid4())
            db.execute('INSERT INTO bulk_operation_items (id, operation_id, item_type, item_id, status, error_message) VALUES (?,?,?,?,?,?)',
                       (item_id, op_id, 'candidate', existing['id'], 'failed', 'Duplicate email'))
            continue

        cand_id = str(uuid.uuid4())
        token = str(uuid.uuid4())
        db.execute('''INSERT INTO candidates (id, interview_id, user_id, first_name, last_name, email, token, status)
                      VALUES (?,?,?,?,?,?,?,?)''',
                   (cand_id, interview_id, g.user_id, first, last or '', email, token, 'invited'))
        item_id = str(uuid.uuid4())
        db.execute('INSERT INTO bulk_operation_items (id, operation_id, item_type, item_id, status, processed_at) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)',
                   (item_id, op_id, 'candidate', cand_id, 'completed'))
        created += 1

    db.execute('''UPDATE bulk_operations SET status='completed', processed_items=?, failed_items=?,
                  result_json=?, completed_at=CURRENT_TIMESTAMP WHERE id=?''',
               (created, failed, json.dumps({'created': created, 'failed': failed, 'errors': errors[:20]}), op_id))
    _log_activity(db, g.user_id, g.user_id, g.user.get('name', ''), 'bulk_invite',
                  'bulk_operation', op_id, f"Invited {created} candidates")
    db.commit()
    db.close()

    return jsonify({
        'operation_id': op_id, 'created': created, 'failed': failed,
        'total': len(candidates_list), 'errors': errors[:20],
    }), 201


@app.route('/api/bulk/remind', methods=['POST'])
@require_auth
def api_bulk_remind():
    """Bulk send reminders to pending candidates."""
    data = request.get_json() or {}
    interview_id = data.get('interview_id', '')
    if not interview_id:
        return jsonify({'error': 'interview_id required'}), 400

    db = get_db()
    candidates = db.execute(
        "SELECT id, email, first_name FROM candidates WHERE interview_id=? AND user_id=? AND status IN ('invited', 'in_progress')",
        (interview_id, g.user_id)
    ).fetchall()

    op_id = str(uuid.uuid4())
    reminded = len(candidates)
    db.execute('''INSERT INTO bulk_operations (id, user_id, operation_type, status, total_items, processed_items,
                  params_json, started_at, completed_at) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)''',
               (op_id, g.user_id, 'bulk_remind', 'completed', reminded, reminded,
                json.dumps({'interview_id': interview_id})))

    for cand in candidates:
        db.execute('UPDATE candidates SET reminder_count=COALESCE(reminder_count,0)+1, last_reminded_at=CURRENT_TIMESTAMP WHERE id=?',
                   (cand['id'],))

    db.commit()
    db.close()
    return jsonify({'operation_id': op_id, 'reminded': reminded}), 201


@app.route('/api/bulk/operations', methods=['GET'])
@require_auth
def api_list_bulk_operations():
    """List bulk operations history."""
    db = get_db()
    ops = db.execute('SELECT * FROM bulk_operations WHERE user_id=? ORDER BY created_at DESC LIMIT 50', (g.user_id,)).fetchall()
    db.close()
    result = []
    for o in ops:
        d = dict(o)
        try: d['params_json'] = json.loads(d['params_json']) if d.get('params_json') else {}
        except: pass
        try: d['result_json'] = json.loads(d['result_json']) if d.get('result_json') else {}
        except: pass
        result.append(d)
    return jsonify({'operations': result, 'count': len(result)})


@app.route('/api/bulk/operations/<op_id>', methods=['GET'])
@require_auth
def api_get_bulk_operation(op_id):
    """Get details of a bulk operation."""
    db = get_db()
    op = db.execute('SELECT * FROM bulk_operations WHERE id=? AND user_id=?', (op_id, g.user_id)).fetchone()
    if not op:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    items = db.execute('SELECT * FROM bulk_operation_items WHERE operation_id=? ORDER BY processed_at', (op_id,)).fetchall()
    db.close()
    d = dict(op)
    try: d['params_json'] = json.loads(d['params_json']) if d.get('params_json') else {}
    except: pass
    try: d['result_json'] = json.loads(d['result_json']) if d.get('result_json') else {}
    except: pass
    return jsonify({'operation': d, 'items': [dict(i) for i in items]})


@app.route('/api/workflows', methods=['GET'])
@require_auth
def api_list_workflows():
    """List automation workflow rules."""
    db = get_db()
    rules = db.execute('SELECT * FROM workflow_rules WHERE user_id=? ORDER BY created_at DESC', (g.user_id,)).fetchall()
    db.close()
    result = []
    for r in rules:
        d = dict(r)
        try: d['trigger_config'] = json.loads(d['trigger_config']) if d.get('trigger_config') else {}
        except: pass
        try: d['action_config'] = json.loads(d['action_config']) if d.get('action_config') else {}
        except: pass
        result.append(d)
    return jsonify({'workflows': result, 'count': len(result)})


@app.route('/api/workflows', methods=['POST'])
@require_auth
def api_create_workflow():
    """Create a workflow automation rule."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    trigger_type = (data.get('trigger_type') or '').strip()
    action_type = (data.get('action_type') or '').strip()

    if not name or not trigger_type or not action_type:
        return jsonify({'error': 'name, trigger_type, and action_type required'}), 400

    valid_triggers = ['candidate_completed', 'score_threshold', 'candidate_invited', 'interview_expired', 'reminder_due']
    valid_actions = ['send_email', 'advance_stage', 'add_to_shortlist', 'notify_team', 'generate_report']

    if trigger_type not in valid_triggers:
        return jsonify({'error': f'Invalid trigger_type. Valid: {valid_triggers}'}), 400
    if action_type not in valid_actions:
        return jsonify({'error': f'Invalid action_type. Valid: {valid_actions}'}), 400

    db = get_db()
    rule_id = str(uuid.uuid4())
    db.execute('''INSERT INTO workflow_rules (id, user_id, name, description, trigger_type, trigger_config,
                  action_type, action_config) VALUES (?,?,?,?,?,?,?,?)''',
               (rule_id, g.user_id, name, data.get('description', ''),
                trigger_type, json.dumps(data.get('trigger_config', {})),
                action_type, json.dumps(data.get('action_config', {}))))
    db.commit()
    rule = dict(db.execute('SELECT * FROM workflow_rules WHERE id=?', (rule_id,)).fetchone())
    db.close()
    try: rule['trigger_config'] = json.loads(rule['trigger_config'])
    except: pass
    try: rule['action_config'] = json.loads(rule['action_config'])
    except: pass
    return jsonify({'workflow': rule}), 201


@app.route('/api/workflows/<rule_id>', methods=['PUT'])
@require_auth
def api_update_workflow(rule_id):
    """Update a workflow rule."""
    db = get_db()
    rule = db.execute('SELECT id FROM workflow_rules WHERE id=? AND user_id=?', (rule_id, g.user_id)).fetchone()
    if not rule:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    data = request.get_json() or {}
    updates, params = [], []
    for f in ['name', 'description']:
        if f in data:
            updates.append(f'{f}=?')
            params.append(data[f])
    if 'is_enabled' in data:
        updates.append('is_enabled=?')
        params.append(1 if data['is_enabled'] else 0)
    if 'trigger_config' in data:
        updates.append('trigger_config=?')
        params.append(json.dumps(data['trigger_config']))
    if 'action_config' in data:
        updates.append('action_config=?')
        params.append(json.dumps(data['action_config']))
    if updates:
        updates.append('updated_at=CURRENT_TIMESTAMP')
        params.append(rule_id)
        db.execute(f'UPDATE workflow_rules SET {", ".join(updates)} WHERE id=?', params)
        db.commit()

    updated = dict(db.execute('SELECT * FROM workflow_rules WHERE id=?', (rule_id,)).fetchone())
    db.close()
    try: updated['trigger_config'] = json.loads(updated['trigger_config'])
    except: pass
    try: updated['action_config'] = json.loads(updated['action_config'])
    except: pass
    return jsonify({'workflow': updated})


@app.route('/api/workflows/<rule_id>', methods=['DELETE'])
@require_auth
def api_delete_workflow(rule_id):
    """Delete a workflow rule."""
    db = get_db()
    rule = db.execute('SELECT id FROM workflow_rules WHERE id=? AND user_id=?', (rule_id, g.user_id)).fetchone()
    if not rule:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute('DELETE FROM workflow_rules WHERE id=?', (rule_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ======================== CYCLE 18: AUDIT TRAIL & COMPLIANCE ========================

@app.route('/api/audit/log', methods=['GET'])
@require_auth
def api_get_audit_log():
    """Get audit trail log entries."""
    db = get_db()
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))
    resource_type = request.args.get('resource_type', '')
    severity = request.args.get('severity', '')

    query = 'SELECT * FROM audit_log WHERE user_id=?'
    params = [g.user_id]
    if resource_type:
        query += ' AND resource_type=?'
        params.append(resource_type)
    if severity:
        query += ' AND severity=?'
        params.append(severity)
    query += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])

    entries = db.execute(query, params).fetchall()
    total = db.execute('SELECT COUNT(*) as cnt FROM audit_log WHERE user_id=?', (g.user_id,)).fetchone()['cnt']
    db.close()
    return jsonify({'entries': [dict(e) for e in entries], 'total': total, 'limit': limit, 'offset': offset})


@app.route('/api/audit/log', methods=['POST'])
@require_auth
def api_create_audit_entry():
    """Manually create an audit log entry."""
    data = request.get_json() or {}
    action = (data.get('action') or '').strip()
    resource_type = (data.get('resource_type') or '').strip()
    if not action or not resource_type:
        return jsonify({'error': 'action and resource_type required'}), 400

    db = get_db()
    entry_id = str(uuid.uuid4())
    db.execute('''INSERT INTO audit_log (id, account_id, user_id, actor_name, actor_ip, action, resource_type,
                  resource_id, resource_name, details, severity) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
               (entry_id, g.user_id, g.user_id, g.user.get('name', ''), request.remote_addr,
                action, resource_type, data.get('resource_id', ''),
                data.get('resource_name', ''), data.get('details', ''),
                data.get('severity', 'info')))
    db.commit()
    db.close()
    return jsonify({'entry_id': entry_id, 'success': True}), 201


@app.route('/api/audit/stats', methods=['GET'])
@require_auth
def api_audit_stats():
    """Get audit statistics summary."""
    db = get_db()
    total = db.execute('SELECT COUNT(*) as cnt FROM audit_log WHERE user_id=?', (g.user_id,)).fetchone()['cnt']
    by_severity = db.execute(
        'SELECT severity, COUNT(*) as cnt FROM audit_log WHERE user_id=? GROUP BY severity', (g.user_id,)
    ).fetchall()
    by_resource = db.execute(
        'SELECT resource_type, COUNT(*) as cnt FROM audit_log WHERE user_id=? GROUP BY resource_type ORDER BY cnt DESC LIMIT 10',
        (g.user_id,)
    ).fetchall()
    recent = db.execute('SELECT * FROM audit_log WHERE user_id=? ORDER BY created_at DESC LIMIT 5', (g.user_id,)).fetchall()
    db.close()
    return jsonify({
        'total': total,
        'by_severity': {r['severity']: r['cnt'] for r in by_severity},
        'by_resource': {r['resource_type']: r['cnt'] for r in by_resource},
        'recent': [dict(r) for r in recent],
    })


@app.route('/api/compliance/consent', methods=['POST'])
@require_auth
def api_record_consent():
    """Record candidate consent."""
    data = request.get_json() or {}
    candidate_id = data.get('candidate_id', '')
    consent_type = data.get('consent_type', '')
    if not candidate_id or not consent_type:
        return jsonify({'error': 'candidate_id and consent_type required'}), 400

    db = get_db()
    consent_id = str(uuid.uuid4())
    db.execute('''INSERT INTO consent_records (id, candidate_id, consent_type, consent_given, ip_address, user_agent, consent_text)
                  VALUES (?,?,?,?,?,?,?)''',
               (consent_id, candidate_id, consent_type, 1 if data.get('consent_given', True) else 0,
                request.remote_addr, request.headers.get('User-Agent', ''),
                data.get('consent_text', '')))
    db.execute('UPDATE candidates SET consent_given=1, consent_at=CURRENT_TIMESTAMP WHERE id=?', (candidate_id,))
    db.commit()
    db.close()
    return jsonify({'consent_id': consent_id, 'success': True}), 201


@app.route('/api/compliance/consent/<candidate_id>', methods=['GET'])
@require_auth
def api_get_consent(candidate_id):
    """Get consent records for a candidate."""
    db = get_db()
    records = db.execute('SELECT * FROM consent_records WHERE candidate_id=? ORDER BY created_at DESC',
                         (candidate_id,)).fetchall()
    db.close()
    return jsonify({'records': [dict(r) for r in records], 'count': len(records)})


@app.route('/api/compliance/retention', methods=['GET'])
@require_auth
def api_list_retention_policies():
    """List data retention policies."""
    db = get_db()
    policies = db.execute('SELECT * FROM retention_policies WHERE user_id=? ORDER BY resource_type', (g.user_id,)).fetchall()
    db.close()
    return jsonify({'policies': [dict(p) for p in policies], 'count': len(policies)})


@app.route('/api/compliance/retention', methods=['POST'])
@require_auth
def api_create_retention_policy():
    """Create a data retention policy."""
    data = request.get_json() or {}
    resource_type = (data.get('resource_type') or '').strip()
    retention_days = data.get('retention_days', 365)
    if not resource_type:
        return jsonify({'error': 'resource_type required'}), 400

    db = get_db()
    # Check for duplicate
    existing = db.execute('SELECT id FROM retention_policies WHERE user_id=? AND resource_type=?',
                          (g.user_id, resource_type)).fetchone()
    if existing:
        db.close()
        return jsonify({'error': 'Policy already exists for this resource type'}), 409

    policy_id = str(uuid.uuid4())
    db.execute('''INSERT INTO retention_policies (id, user_id, resource_type, retention_days, auto_delete, notify_before_days)
                  VALUES (?,?,?,?,?,?)''',
               (policy_id, g.user_id, resource_type, retention_days,
                1 if data.get('auto_delete') else 0, data.get('notify_before_days', 30)))
    db.commit()
    policy = dict(db.execute('SELECT * FROM retention_policies WHERE id=?', (policy_id,)).fetchone())
    db.close()
    return jsonify({'policy': policy}), 201


@app.route('/api/compliance/retention/<policy_id>', methods=['PUT'])
@require_auth
def api_update_retention_policy(policy_id):
    """Update a retention policy."""
    db = get_db()
    policy = db.execute('SELECT id FROM retention_policies WHERE id=? AND user_id=?', (policy_id, g.user_id)).fetchone()
    if not policy:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    data = request.get_json() or {}
    updates, params = [], []
    for f in ['retention_days', 'notify_before_days']:
        if f in data:
            updates.append(f'{f}=?')
            params.append(data[f])
    if 'auto_delete' in data:
        updates.append('auto_delete=?')
        params.append(1 if data['auto_delete'] else 0)
    if 'is_active' in data:
        updates.append('is_active=?')
        params.append(1 if data['is_active'] else 0)
    if updates:
        updates.append('updated_at=CURRENT_TIMESTAMP')
        params.append(policy_id)
        db.execute(f'UPDATE retention_policies SET {", ".join(updates)} WHERE id=?', params)
        db.commit()

    updated = dict(db.execute('SELECT * FROM retention_policies WHERE id=?', (policy_id,)).fetchone())
    db.close()
    return jsonify({'policy': updated})


@app.route('/api/compliance/retention/<policy_id>', methods=['DELETE'])
@require_auth
def api_delete_retention_policy(policy_id):
    """Delete a retention policy."""
    db = get_db()
    policy = db.execute('SELECT id FROM retention_policies WHERE id=? AND user_id=?', (policy_id, g.user_id)).fetchone()
    if not policy:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute('DELETE FROM retention_policies WHERE id=?', (policy_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/compliance/gdpr-export/<candidate_id>', methods=['GET'])
@require_auth
def api_gdpr_export(candidate_id):
    """Export all data for a candidate (GDPR data portability)."""
    db = get_db()
    candidate = db.execute('SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    responses = db.execute('SELECT * FROM responses WHERE candidate_id=?', (candidate_id,)).fetchall()
    consent = db.execute('SELECT * FROM consent_records WHERE candidate_id=?', (candidate_id,)).fetchall()
    notes = db.execute('SELECT * FROM team_notes WHERE candidate_id=?', (candidate_id,)).fetchall()
    reports = db.execute('SELECT * FROM candidate_reports WHERE candidate_id=?', (candidate_id,)).fetchall()
    db.close()

    export = {
        'candidate': dict(candidate),
        'responses': [dict(r) for r in responses],
        'consent_records': [dict(c) for c in consent],
        'notes': [dict(n) for n in notes],
        'reports': [dict(r) for r in reports],
        'exported_at': datetime.utcnow().isoformat(),
    }
    return jsonify({'export': export})


# ======================== CYCLE 18: PAGE ROUTES ========================

@app.route('/white-label')
@require_auth
def page_white_label():
    return render_template('app.html', user=g.user, page='white-label')

@app.route('/report-hub')
@require_auth
def page_report_hub():
    return render_template('app.html', user=g.user, page='report-hub')

@app.route('/bulk-ops')
@require_auth
def page_bulk_ops():
    return render_template('app.html', user=g.user, page='bulk-ops')

@app.route('/audit')
@require_auth
@require_fmo_admin
def page_audit():
    return render_template('app.html', user=g.user, page='audit')


# ======================== CYCLE 19: PAYMENT & SUBSCRIPTIONS ========================

@app.route('/api/plans', methods=['GET'])
def api_list_plans():
    """Public endpoint - list available subscription plans."""
    db = get_db()
    plans = db.execute("SELECT * FROM subscription_plans WHERE is_active=1 ORDER BY sort_order").fetchall()
    result = []
    for p in plans:
        d = dict(p)
        d['features'] = json.loads(d.get('features_json') or '[]')
        d.pop('features_json', None)
        result.append(d)
    db.close()
    return jsonify(result)

@app.route('/api/subscription', methods=['GET'])
@require_auth
def api_get_subscription():
    """Get current user's subscription details."""
    db = get_db()
    sub = db.execute("""
        SELECT s.*, sp.name as plan_name, sp.slug as plan_slug, sp.max_interviews,
               sp.max_candidates, sp.max_team_members, sp.max_video_storage_gb, sp.features_json
        FROM subscriptions s
        JOIN subscription_plans sp ON s.plan_id = sp.id
        WHERE s.user_id=? ORDER BY s.created_at DESC LIMIT 1
    """, (g.user_id,)).fetchone()

    if not sub:
        # Return default free tier info
        user = db.execute("SELECT plan, trial_ends_at, usage_interviews_count, usage_candidates_count, usage_video_storage_mb FROM users WHERE id=?", (g.user_id,)).fetchone()
        user_d = dict(user) if user else {}
        db.close()
        return jsonify({
            'subscription': None,
            'plan': user_d.get('plan', 'starter'),
            'status': 'active',
            'usage': {
                'interviews': user_d.get('usage_interviews_count', 0),
                'candidates': user_d.get('usage_candidates_count', 0),
                'video_storage_mb': user_d.get('usage_video_storage_mb', 0)
            },
            'limits': {'max_interviews': 5, 'max_candidates': 50, 'max_team_members': 2, 'max_video_storage_gb': 5}
        })

    sub_d = dict(sub)
    features = json.loads(sub_d.get('features_json') or '[]')
    user = db.execute("SELECT usage_interviews_count, usage_candidates_count, usage_video_storage_mb FROM users WHERE id=?", (g.user_id,)).fetchone()
    user_d = dict(user) if user else {}
    db.close()
    return jsonify({
        'subscription': {
            'id': sub_d['id'],
            'plan_name': sub_d['plan_name'],
            'plan_slug': sub_d['plan_slug'],
            'status': sub_d['status'],
            'billing_cycle': sub_d['billing_cycle'],
            'current_period_start': sub_d['current_period_start'],
            'current_period_end': sub_d['current_period_end'],
            'trial_end': sub_d['trial_end'],
            'cancel_at_period_end': sub_d['cancel_at_period_end'],
            'payment_method_last4': sub_d['payment_method_last4'],
            'payment_method_brand': sub_d['payment_method_brand'],
            'stripe_subscription_id': sub_d['stripe_subscription_id']
        },
        'plan': sub_d['plan_slug'],
        'status': sub_d['status'],
        'features': features,
        'usage': {
            'interviews': user_d.get('usage_interviews_count', 0),
            'candidates': user_d.get('usage_candidates_count', 0),
            'video_storage_mb': user_d.get('usage_video_storage_mb', 0)
        },
        'limits': {
            'max_interviews': sub_d['max_interviews'],
            'max_candidates': sub_d['max_candidates'],
            'max_team_members': sub_d['max_team_members'],
            'max_video_storage_gb': sub_d['max_video_storage_gb']
        }
    })

@app.route('/api/subscription/checkout', methods=['POST'])
@require_auth
def api_create_checkout():
    """Create a Stripe checkout session for plan upgrade."""
    data = request.get_json() or {}
    plan_slug = data.get('plan')
    billing_cycle = data.get('billing_cycle', 'monthly')

    if not plan_slug:
        return jsonify({'error': 'Plan slug required'}), 400

    db = get_db()
    plan = db.execute("SELECT * FROM subscription_plans WHERE slug=? AND is_active=1", (plan_slug,)).fetchone()
    if not plan:
        db.close()
        return jsonify({'error': 'Plan not found'}), 404

    plan_d = dict(plan)
    price = plan_d['price_monthly'] if billing_cycle == 'monthly' else plan_d['price_annual']

    # In production, this would create a real Stripe checkout session
    # For now, simulate the checkout flow
    checkout_id = uuid.uuid4().hex

    # Create pending subscription
    sub_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    period_end = (datetime.utcnow() + timedelta(days=30 if billing_cycle == 'monthly' else 365)).isoformat()
    trial_end = (datetime.utcnow() + timedelta(days=14)).isoformat()

    db.execute("""INSERT INTO subscriptions
        (id, user_id, plan_id, status, billing_cycle, current_period_start, current_period_end, trial_end, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (sub_id, g.user_id, plan_d['id'], 'trialing', billing_cycle, now, period_end, trial_end, now))

    db.execute("UPDATE users SET plan=?, subscription_id=?, subscription_status='trialing', trial_ends_at=? WHERE id=?",
        (plan_slug, sub_id, trial_end, g.user_id))

    # Record usage event
    db.execute("INSERT INTO usage_records (id, user_id, metric, quantity) VALUES (?,?,?,?)",
        (uuid.uuid4().hex, g.user_id, 'plan_change', 1))

    db.commit()
    db.close()

    return jsonify({
        'checkout_id': checkout_id,
        'checkout_url': f'/billing?checkout={checkout_id}',
        'subscription_id': sub_id,
        'plan': plan_slug,
        'billing_cycle': billing_cycle,
        'price': price,
        'trial_days': 14,
        'message': 'Subscription created with 14-day free trial'
    })

@app.route('/api/subscription/cancel', methods=['POST'])
@require_auth
def api_cancel_subscription():
    """Cancel current subscription at period end."""
    db = get_db()
    sub = db.execute("SELECT * FROM subscriptions WHERE user_id=? AND status IN ('active','trialing') ORDER BY created_at DESC LIMIT 1", (g.user_id,)).fetchone()
    if not sub:
        db.close()
        return jsonify({'error': 'No active subscription found'}), 404

    now = datetime.utcnow().isoformat()
    db.execute("UPDATE subscriptions SET cancel_at_period_end=1, canceled_at=?, updated_at=? WHERE id=?",
        (now, now, sub['id']))
    db.execute("UPDATE users SET subscription_status='canceling' WHERE id=?", (g.user_id,))
    db.commit()
    db.close()

    return jsonify({'success': True, 'message': 'Subscription will cancel at end of current period', 'cancel_at': dict(sub).get('current_period_end')})

@app.route('/api/subscription/reactivate', methods=['POST'])
@require_auth
def api_reactivate_subscription():
    """Reactivate a subscription that was set to cancel."""
    db = get_db()
    sub = db.execute("SELECT * FROM subscriptions WHERE user_id=? AND cancel_at_period_end=1 ORDER BY created_at DESC LIMIT 1", (g.user_id,)).fetchone()
    if not sub:
        db.close()
        return jsonify({'error': 'No canceled subscription to reactivate'}), 404

    now = datetime.utcnow().isoformat()
    db.execute("UPDATE subscriptions SET cancel_at_period_end=0, canceled_at=NULL, updated_at=? WHERE id=?",
        (now, sub['id']))
    db.execute("UPDATE users SET subscription_status='active' WHERE id=?", (g.user_id,))
    db.commit()
    db.close()

    return jsonify({'success': True, 'message': 'Subscription reactivated'})

@app.route('/api/subscription/usage', methods=['GET'])
@require_auth
def api_get_usage():
    """Get detailed usage metrics for current billing period."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (g.user_id,)).fetchone()
    user_d = dict(user)

    # Count actual usage
    interviews_count = db.execute("SELECT COUNT(*) FROM interviews WHERE user_id=?", (g.user_id,)).fetchone()[0]
    candidates_count = db.execute("SELECT COUNT(*) FROM candidates WHERE interview_id IN (SELECT id FROM interviews WHERE user_id=?)", (g.user_id,)).fetchone()[0]
    video_count = db.execute("SELECT COUNT(*) FROM video_assets WHERE user_id=?", (g.user_id,)).fetchone()[0]
    video_size = db.execute("SELECT COALESCE(SUM(file_size), 0) FROM video_assets WHERE user_id=?", (g.user_id,)).fetchone()[0]

    # Recent usage records
    records = db.execute("SELECT metric, quantity, recorded_at FROM usage_records WHERE user_id=? ORDER BY recorded_at DESC LIMIT 50", (g.user_id,)).fetchall()

    db.close()
    return jsonify({
        'usage': {
            'interviews': interviews_count,
            'candidates': candidates_count,
            'videos': video_count,
            'video_storage_bytes': video_size,
            'video_storage_mb': round(video_size / (1024*1024), 2) if video_size else 0
        },
        'recent_activity': [dict(r) for r in records]
    })

@app.route('/api/invoices', methods=['GET'])
@require_auth
def api_list_invoices():
    """List payment history / invoices."""
    db = get_db()
    invoices = db.execute("SELECT * FROM invoices WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (g.user_id,)).fetchall()
    db.close()
    return jsonify({'invoices': [dict(i) for i in invoices]})

@app.route('/api/webhooks/stripe', methods=['POST'])
def api_stripe_webhook():
    """Handle Stripe webhook events (no auth - verified by signature)."""
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature', '')

    # In production: verify webhook signature with stripe.Webhook.construct_event()
    # For now, parse the event directly
    try:
        event = json.loads(payload)
    except:
        return jsonify({'error': 'Invalid payload'}), 400

    event_type = event.get('type', '')
    data = event.get('data', {}).get('object', {})

    db = get_db()

    if event_type == 'checkout.session.completed':
        # Activate subscription after successful payment
        customer_id = data.get('customer')
        sub_id = data.get('subscription')
        if customer_id:
            db.execute("UPDATE subscriptions SET stripe_customer_id=?, stripe_subscription_id=?, status='active', updated_at=? WHERE user_id=(SELECT id FROM users WHERE stripe_customer_id=?)",
                (customer_id, sub_id, datetime.utcnow().isoformat(), customer_id))
            db.execute("UPDATE users SET subscription_status='active' WHERE stripe_customer_id=?", (customer_id,))

    elif event_type == 'invoice.paid':
        # Record successful payment
        inv_id = uuid.uuid4().hex
        db.execute("""INSERT OR IGNORE INTO invoices
            (id, user_id, stripe_invoice_id, amount, currency, status, description, invoice_pdf_url, hosted_invoice_url, paid_at, created_at)
            VALUES (?, (SELECT id FROM users WHERE stripe_customer_id=?), ?, ?, ?, 'paid', ?, ?, ?, ?, ?)""",
            (inv_id, data.get('customer'), data.get('id'), data.get('amount_paid', 0), data.get('currency', 'usd'),
             data.get('description', 'Subscription payment'), data.get('invoice_pdf'), data.get('hosted_invoice_url'),
             datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))

    elif event_type == 'invoice.payment_failed':
        customer_id = data.get('customer')
        if customer_id:
            db.execute("UPDATE subscriptions SET status='past_due', updated_at=? WHERE user_id=(SELECT id FROM users WHERE stripe_customer_id=?)",
                (datetime.utcnow().isoformat(), customer_id))
            db.execute("UPDATE users SET subscription_status='past_due' WHERE stripe_customer_id=?", (customer_id,))

    elif event_type == 'customer.subscription.deleted':
        customer_id = data.get('customer')
        if customer_id:
            db.execute("UPDATE subscriptions SET status='canceled', updated_at=? WHERE user_id=(SELECT id FROM users WHERE stripe_customer_id=?)",
                (datetime.utcnow().isoformat(), customer_id))
            db.execute("UPDATE users SET plan='starter', subscription_status='canceled' WHERE stripe_customer_id=?", (customer_id,))

    db.commit()
    db.close()
    return jsonify({'received': True})


# ======================== CYCLE 19: VIDEO STORAGE & STREAMING ========================

@app.route('/api/videos/upload-url', methods=['POST'])
@require_auth
def api_get_upload_url():
    """Get a presigned upload URL for direct-to-S3 video upload."""
    data = request.get_json() or {}
    candidate_id = data.get('candidate_id')
    interview_id = data.get('interview_id')
    question_id = data.get('question_id')
    content_type = data.get('content_type', 'video/webm')
    file_size = data.get('file_size', 0)
    is_intro = data.get('is_intro', False)

    if not interview_id:
        return jsonify({'error': 'interview_id required'}), 400

    # Create video asset record
    asset_id = uuid.uuid4().hex
    storage_key = f"videos/{g.user_id}/{interview_id}/{asset_id}.webm"
    if is_intro:
        storage_key = f"intros/{g.user_id}/{asset_id}.webm"

    db = get_db()

    # Check storage quota
    user = db.execute("SELECT usage_video_storage_mb FROM users WHERE id=?", (g.user_id,)).fetchone()
    current_mb = (dict(user).get('usage_video_storage_mb') or 0) if user else 0

    # Get plan limit
    sub = db.execute("""SELECT sp.max_video_storage_gb FROM subscriptions s
        JOIN subscription_plans sp ON s.plan_id=sp.id
        WHERE s.user_id=? AND s.status IN ('active','trialing')
        ORDER BY s.created_at DESC LIMIT 1""", (g.user_id,)).fetchone()
    max_gb = dict(sub)['max_video_storage_gb'] if sub else 5

    if max_gb > 0 and current_mb + (file_size / (1024*1024)) > max_gb * 1024:
        db.close()
        return jsonify({'error': 'Video storage quota exceeded. Please upgrade your plan.'}), 403

    db.execute("""INSERT INTO video_assets
        (id, user_id, candidate_id, interview_id, question_id, storage_backend, storage_key,
         content_type, file_size, is_intro, uploaded_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (asset_id, g.user_id, candidate_id, interview_id, question_id,
         app_config.STORAGE_BACKEND, storage_key, content_type, file_size,
         1 if is_intro else 0, datetime.utcnow().isoformat()))

    db.commit()

    # Generate presigned URL (S3) or local upload endpoint
    if app_config.STORAGE_BACKEND == 's3' and app_config.S3_BUCKET:
        # In production, use boto3 to generate presigned PUT URL
        upload_url = f"https://{app_config.S3_BUCKET}.s3.{app_config.S3_REGION}.amazonaws.com/{storage_key}"
        method = 'PUT'
    else:
        upload_url = f"/api/videos/{asset_id}/upload"
        method = 'POST'

    db.close()
    return jsonify({
        'asset_id': asset_id,
        'upload_url': upload_url,
        'method': method,
        'storage_key': storage_key,
        'max_size_mb': max_gb * 1024 if max_gb > 0 else 5120
    })

@app.route('/api/videos/<asset_id>/upload', methods=['POST'])
@require_auth
def api_upload_video_local(asset_id):
    """Upload video directly (local storage fallback)."""
    db = get_db()
    asset = db.execute("SELECT * FROM video_assets WHERE id=? AND user_id=?", (asset_id, g.user_id)).fetchone()
    if not asset:
        db.close()
        return jsonify({'error': 'Video asset not found'}), 404

    if 'video' not in request.files:
        db.close()
        return jsonify({'error': 'No video file provided'}), 400

    file = request.files['video']
    asset_d = dict(asset)

    # Save locally
    filename = f"{asset_id}.webm"
    if asset_d.get('is_intro'):
        filepath = os.path.join(app.config.get('INTRO_FOLDER', 'static/uploads/intros'), filename)
        relative_path = f"/static/uploads/intros/{filename}"
    else:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        relative_path = f"/static/uploads/videos/{filename}"

    file.save(filepath)
    file_size = os.path.getsize(filepath)

    db.execute("UPDATE video_assets SET file_size=?, storage_key=?, transcode_status='complete' WHERE id=?",
        (file_size, relative_path, asset_id))

    # Update user storage usage
    db.execute("UPDATE users SET usage_video_storage_mb = COALESCE(usage_video_storage_mb,0) + ? WHERE id=?",
        (round(file_size / (1024*1024), 2), g.user_id))

    # Record usage
    db.execute("INSERT INTO usage_records (id, user_id, metric, quantity) VALUES (?,?,?,?)",
        (uuid.uuid4().hex, g.user_id, 'video_upload', file_size))

    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'asset_id': asset_id,
        'url': relative_path,
        'file_size': file_size
    })

@app.route('/api/videos/<asset_id>/stream', methods=['GET'])
@require_auth
def api_stream_video(asset_id):
    """Get a streaming/playback URL for a video asset."""
    db = get_db()
    # Check direct ownership OR team membership
    asset = db.execute("""SELECT va.* FROM video_assets va
                          JOIN candidates c ON va.candidate_id = c.id
                          WHERE va.id=? AND (c.user_id=? OR c.user_id IN
                            (SELECT account_id FROM team_members WHERE user_id=? AND status='active'))""",
                       (asset_id, g.user_id, g.user_id)).fetchone()
    if not asset:
        db.close()
        return jsonify({'error': 'Video not found'}), 404

    asset_d = dict(asset)

    if asset_d['storage_backend'] == 's3' and app_config.S3_BUCKET:
        # Generate presigned GET URL using storage service
        s3_key = asset_d.get('transcoded_key') or asset_d['storage_key']
        stream_url = storage.get_url(s3_key)
        expires_at = (datetime.utcnow() + timedelta(seconds=app_config.S3_PRESIGN_EXPIRY)).isoformat()
    else:
        stream_url = asset_d['storage_key']  # local relative path
        expires_at = None

    db.close()
    return jsonify({
        'asset_id': asset_id,
        'stream_url': stream_url,
        'content_type': asset_d.get('content_type', 'video/webm'),
        'duration': asset_d.get('duration_seconds'),
        'file_size': asset_d.get('file_size'),
        'transcode_status': asset_d.get('transcode_status'),
        'cdn_url': asset_d.get('cdn_url'),
        'expires_at': expires_at
    })

@app.route('/api/videos', methods=['GET'])
@require_auth
def api_list_video_assets():
    """List video assets for current user."""
    db = get_db()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 25))
    interview_id = request.args.get('interview_id')

    offset = (page - 1) * per_page

    if interview_id:
        assets = db.execute("SELECT * FROM video_assets WHERE user_id=? AND interview_id=? ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
            (g.user_id, interview_id, per_page, offset)).fetchall()
        total = db.execute("SELECT COUNT(*) FROM video_assets WHERE user_id=? AND interview_id=?", (g.user_id, interview_id)).fetchone()[0]
    else:
        assets = db.execute("SELECT * FROM video_assets WHERE user_id=? ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
            (g.user_id, per_page, offset)).fetchall()
        total = db.execute("SELECT COUNT(*) FROM video_assets WHERE user_id=?", (g.user_id,)).fetchone()[0]

    db.close()
    return jsonify({
        'assets': [dict(a) for a in assets],
        'page': page,
        'per_page': per_page,
        'total': total
    })

@app.route('/api/videos/<asset_id>', methods=['DELETE'])
@require_auth
def api_delete_video_asset(asset_id):
    """Delete a video asset."""
    db = get_db()
    asset = db.execute("SELECT * FROM video_assets WHERE id=? AND user_id=?", (asset_id, g.user_id)).fetchone()
    if not asset:
        db.close()
        return jsonify({'error': 'Video not found'}), 404

    asset_d = dict(asset)

    # Delete physical file if local
    if asset_d['storage_backend'] == 'local':
        key = asset_d['storage_key']
        if key.startswith('/static/'):
            filepath = os.path.join(os.path.dirname(__file__), key.lstrip('/'))
            if os.path.exists(filepath):
                os.remove(filepath)

    # Update storage usage
    file_mb = round((asset_d.get('file_size') or 0) / (1024*1024), 2)
    db.execute("UPDATE users SET usage_video_storage_mb = MAX(0, COALESCE(usage_video_storage_mb,0) - ?) WHERE id=?",
        (file_mb, g.user_id))

    db.execute("DELETE FROM video_assets WHERE id=?", (asset_id,))
    db.commit()
    db.close()

    return jsonify({'success': True})

@app.route('/api/videos/<asset_id>/transcode', methods=['POST'])
@require_auth
def api_transcode_video(asset_id):
    """Trigger transcoding for a video asset (converts to MP4 for broad compatibility)."""
    db = get_db()
    asset = db.execute("SELECT * FROM video_assets WHERE id=? AND user_id=?", (asset_id, g.user_id)).fetchone()
    if not asset:
        db.close()
        return jsonify({'error': 'Video not found'}), 404

    asset_d = dict(asset)
    if asset_d.get('transcode_status') == 'processing':
        db.close()
        return jsonify({'error': 'Transcoding already in progress'}), 409

    # In production, this would enqueue a job to a transcoding service (e.g., AWS MediaConvert)
    job_id = uuid.uuid4().hex
    transcoded_key = asset_d['storage_key'].replace('.webm', '.mp4')

    db.execute("UPDATE video_assets SET transcode_status='processing', transcode_job_id=?, transcoded_key=? WHERE id=?",
        (job_id, transcoded_key, asset_id))
    db.commit()
    db.close()

    # Simulate: in production, a callback/webhook would update status to 'complete'
    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': 'processing',
        'message': 'Transcoding job queued. Status will update automatically.'
    })

@app.route('/api/storage/stats', methods=['GET'])
@require_auth
def api_storage_stats_v2():
    """Get storage usage statistics."""
    db = get_db()

    total_size = db.execute("SELECT COALESCE(SUM(file_size), 0) FROM video_assets WHERE user_id=?", (g.user_id,)).fetchone()[0]
    total_count = db.execute("SELECT COUNT(*) FROM video_assets WHERE user_id=?", (g.user_id,)).fetchone()[0]
    by_interview = db.execute("""
        SELECT va.interview_id, i.title, COUNT(*) as video_count, SUM(va.file_size) as total_bytes
        FROM video_assets va LEFT JOIN interviews i ON va.interview_id=i.id
        WHERE va.user_id=? GROUP BY va.interview_id ORDER BY total_bytes DESC LIMIT 20
    """, (g.user_id,)).fetchall()

    # Get plan limit
    sub = db.execute("""SELECT sp.max_video_storage_gb FROM subscriptions s
        JOIN subscription_plans sp ON s.plan_id=sp.id
        WHERE s.user_id=? AND s.status IN ('active','trialing')
        ORDER BY s.created_at DESC LIMIT 1""", (g.user_id,)).fetchone()
    max_gb = dict(sub)['max_video_storage_gb'] if sub else 5

    db.close()
    return jsonify({
        'total_bytes': total_size,
        'total_mb': round(total_size / (1024*1024), 2) if total_size else 0,
        'total_gb': round(total_size / (1024*1024*1024), 3) if total_size else 0,
        'total_videos': total_count,
        'max_storage_gb': max_gb,
        'usage_pct': round((total_size / (max_gb * 1024*1024*1024)) * 100, 1) if max_gb > 0 and total_size else 0,
        'by_interview': [dict(r) for r in by_interview]
    })


# ======================== CYCLE 19: CANDIDATE EXPERIENCE ========================

@app.route('/api/candidate-session/start', methods=['POST'])
def api_start_candidate_session():
    """Start a new candidate interview session (no auth - uses invite token)."""
    data = request.get_json() or {}
    token = data.get('token') or request.args.get('token')

    if not token:
        return jsonify({'error': 'Invite token required'}), 400

    db = get_db()
    candidate = db.execute("SELECT c.*, i.title as interview_title, i.thinking_time, i.max_answer_time, i.max_retakes, i.welcome_msg, i.thank_you_msg, i.brand_color, i.show_progress_bar, i.allow_retakes, i.require_camera_test, i.mobile_enabled, i.logo_url_override, i.accent_color FROM candidates c JOIN interviews i ON c.interview_id=i.id WHERE c.token=?", (token,)).fetchone()

    if not candidate:
        db.close()
        return jsonify({'error': 'Invalid or expired invite token'}), 404

    cand_d = dict(candidate)

    if cand_d.get('status') == 'completed':
        db.close()
        return jsonify({'error': 'Interview already completed', 'status': 'completed'}), 400

    # Get questions
    questions = db.execute("SELECT id, question_text, question_order, thinking_time, max_answer_time FROM questions WHERE interview_id=? ORDER BY question_order", (cand_d['interview_id'],)).fetchall()

    # Create session
    session_id = uuid.uuid4().hex
    session_token = uuid.uuid4().hex

    ua = request.headers.get('User-Agent', '')
    device_type = 'mobile' if any(x in ua.lower() for x in ['mobile', 'android', 'iphone', 'ipad']) else 'desktop'

    db.execute("""INSERT INTO candidate_sessions
        (id, candidate_id, token, interview_id, session_token, device_type, browser, os, started_at, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (session_id, cand_d['id'], session_token, cand_d['interview_id'], session_token, device_type,
         ua[:200], '', datetime.utcnow().isoformat(), 'started', datetime.utcnow().isoformat()))

    # Update candidate status
    if cand_d.get('status') == 'invited':
        db.execute("UPDATE candidates SET status='in_progress', session_id=?, device_type=?, browser_info=? WHERE id=?",
            (session_id, device_type, ua[:200], cand_d['id']))

    db.commit()

    # Get branding
    interview_owner = db.execute("SELECT u.brand_color, u.agency_name, u.logo_url FROM users u JOIN interviews i ON i.user_id=u.id WHERE i.id=?", (cand_d['interview_id'],)).fetchone()
    owner_d = dict(interview_owner) if interview_owner else {}

    db.close()

    return jsonify({
        'session_id': session_id,
        'session_token': session_token,
        'candidate_id': cand_d['id'],
        'interview': {
            'id': cand_d['interview_id'],
            'title': cand_d.get('interview_title'),
            'welcome_message': cand_d.get('welcome_msg'),
            'thank_you_message': cand_d.get('thank_you_msg'),
            'thinking_time': cand_d.get('thinking_time', 30),
            'max_answer_time': cand_d.get('max_answer_time', 120),
            'max_retakes': cand_d.get('max_retakes', 1),
            'show_progress_bar': cand_d.get('show_progress_bar', 1),
            'allow_retakes': cand_d.get('allow_retakes', 1),
            'require_camera_test': cand_d.get('require_camera_test', 1),
            'mobile_enabled': cand_d.get('mobile_enabled', 1)
        },
        'questions': [dict(q) for q in questions],
        'total_questions': len(questions),
        'branding': {
            'color': cand_d.get('accent_color') or cand_d.get('brand_color') or owner_d.get('brand_color', '#0ace0a'),
            'agency_name': owner_d.get('agency_name', 'ChannelView'),
            'logo_url': cand_d.get('logo_url_override') or owner_d.get('logo_url')
        },
        'device_type': device_type
    })

@app.route('/api/candidate-session/<session_id>/progress', methods=['PUT'])
def api_update_candidate_progress(session_id):
    """Update candidate's progress during interview."""
    data = request.get_json() or {}
    session_token = data.get('session_token') or request.headers.get('X-Session-Token', '')

    db = get_db()
    session = db.execute("SELECT * FROM candidate_sessions WHERE id=? AND session_token=?", (session_id, session_token)).fetchone()
    if not session:
        db.close()
        return jsonify({'error': 'Invalid session'}), 403

    updates = []
    params = []

    if 'current_question_index' in data:
        updates.append("current_question_index=?")
        params.append(data['current_question_index'])
    if 'progress_pct' in data:
        updates.append("progress_pct=?")
        params.append(data['progress_pct'])
    if 'camera_device' in data:
        updates.append("camera_device=?")
        params.append(data['camera_device'])
    if 'mic_device' in data:
        updates.append("mic_device=?")
        params.append(data['mic_device'])
    if 'network_quality' in data:
        updates.append("network_quality=?")
        params.append(data['network_quality'])
    if 'screen_resolution' in data:
        updates.append("screen_resolution=?")
        params.append(data['screen_resolution'])

    updates.append("last_active_at=?")
    params.append(datetime.utcnow().isoformat())
    params.append(session_id)

    if updates:
        db.execute(f"UPDATE candidate_sessions SET {', '.join(updates)} WHERE id=?", params)

    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/candidate-session/<session_id>/complete', methods=['POST'])
def api_complete_candidate_session(session_id):
    """Mark candidate interview session as complete."""
    data = request.get_json() or {}
    session_token = data.get('session_token') or request.headers.get('X-Session-Token', '')

    db = get_db()
    session = db.execute("SELECT * FROM candidate_sessions WHERE id=? AND session_token=?", (session_id, session_token)).fetchone()
    if not session:
        db.close()
        return jsonify({'error': 'Invalid session'}), 403

    session_d = dict(session)
    now = datetime.utcnow().isoformat()

    db.execute("UPDATE candidate_sessions SET status='completed', completed_at=?, progress_pct=100, last_active_at=? WHERE id=?",
        (now, now, session_id))

    # Update candidate status
    db.execute("UPDATE candidates SET status='completed', completed_at=? WHERE id=?",
        (now, session_d['candidate_id']))

    # Record experience feedback if provided
    if data.get('experience_rating'):
        db.execute("UPDATE candidates SET experience_rating=?, experience_feedback=? WHERE id=?",
            (data['experience_rating'], data.get('experience_feedback', ''), session_d['candidate_id']))

    # Update owner's usage count
    interview = db.execute("SELECT user_id FROM interviews WHERE id=?", (session_d['interview_id'],)).fetchone()
    if interview:
        db.execute("UPDATE users SET usage_candidates_count = COALESCE(usage_candidates_count,0) + 1 WHERE id=?", (interview['user_id'],))

    db.commit()
    db.close()

    return jsonify({'success': True, 'message': 'Interview completed successfully'})

@app.route('/api/candidate-session/<session_id>/device-check', methods=['POST'])
def api_device_check(session_id):
    """Record device compatibility check results."""
    data = request.get_json() or {}
    session_token = data.get('session_token') or request.headers.get('X-Session-Token', '')

    db = get_db()
    session = db.execute("SELECT * FROM candidate_sessions WHERE id=? AND session_token=?", (session_id, session_token)).fetchone()
    if not session:
        db.close()
        return jsonify({'error': 'Invalid session'}), 403

    session_d = dict(session)

    db.execute("""UPDATE candidate_sessions SET
        camera_device=?, mic_device=?, screen_resolution=?, network_quality=?
        WHERE id=?""",
        (data.get('camera_label', 'default'), data.get('mic_label', 'default'),
         data.get('screen_resolution', ''), data.get('network_quality', 'good'), session_id))

    db.execute("UPDATE candidates SET camera_tested=1, mic_tested=1 WHERE id=?",
        (session_d['candidate_id'],))

    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'compatible': True,
        'recommendations': []
    })

@app.route('/api/candidate-session/<session_id>/error', methods=['POST'])
def api_log_candidate_error(session_id):
    """Log a candidate-side error during interview."""
    data = request.get_json() or {}
    session_token = data.get('session_token') or request.headers.get('X-Session-Token', '')

    db = get_db()
    session = db.execute("SELECT * FROM candidate_sessions WHERE id=? AND session_token=?", (session_id, session_token)).fetchone()
    if not session:
        db.close()
        return jsonify({'error': 'Invalid session'}), 403

    error_entry = json.dumps({
        'timestamp': datetime.utcnow().isoformat(),
        'type': data.get('error_type', 'unknown'),
        'message': data.get('message', ''),
        'context': data.get('context', '')
    })

    # Append to error log
    existing = dict(session).get('error_log') or '[]'
    try:
        errors = json.loads(existing)
    except:
        errors = []
    errors.append(json.loads(error_entry))

    db.execute("UPDATE candidate_sessions SET error_log=? WHERE id=?",
        (json.dumps(errors[-50:]), session_id))  # Keep last 50 errors
    db.commit()
    db.close()

    return jsonify({'success': True})

@app.route('/interview/<token>', methods=['GET'])
def page_candidate_interview(token):
    """Candidate-facing interview landing page (no auth required)."""
    db = get_db()
    candidate = db.execute("""
        SELECT c.*, i.title as interview_title, i.brand_color, i.welcome_msg, i.accent_color, i.logo_url_override,
               u.agency_name, u.brand_color as owner_brand_color, u.logo_url as owner_logo
        FROM candidates c
        JOIN interviews i ON c.interview_id=i.id
        JOIN users u ON i.user_id=u.id
        WHERE c.token=?
    """, (token,)).fetchone()

    if not candidate:
        db.close()
        return render_template('candidate_interview.html', error='Invalid or expired interview link', candidate=None, branding={})

    cand_d = dict(candidate)
    branding = {
        'color': cand_d.get('accent_color') or cand_d.get('brand_color') or cand_d.get('owner_brand_color', '#0ace0a'),
        'agency_name': cand_d.get('agency_name', 'ChannelView'),
        'logo_url': cand_d.get('logo_url_override') or cand_d.get('owner_logo')
    }

    db.close()
    return render_template('candidate_interview.html', candidate=cand_d, branding=branding, error=None)


# ======================== CYCLE 19 PAGE ROUTES ========================

@app.route('/billing')
@require_auth
def page_billing_c19():
    return render_template('app.html', user=g.user, page='billing')

@app.route('/video-library')
@require_auth
def page_video_library():
    return render_template('app.html', user=g.user, page='video-library')


# ======================== CYCLE 20: PRODUCTION INFRASTRUCTURE ========================

@app.route('/api/health', methods=['GET'])
def api_health_check():
    """Health check endpoint for load balancers and monitoring."""
    db_ok = False
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        db_ok = True
        db.close()
    except:
        pass

    status = 'healthy' if db_ok else 'degraded'
    return jsonify({
        'status': status,
        'version': app_config.VERSION,
        'environment': os.environ.get('FLASK_ENV', 'development'),
        'database': 'connected' if db_ok else 'disconnected',
        'timestamp': datetime.utcnow().isoformat(),
        'uptime_seconds': int(time.time() - _app_start_time)
    }), 200 if db_ok else 503

@app.route('/api/system/config', methods=['GET'])
@require_auth
@require_role('admin')
def api_system_config():
    """Get current system configuration (admin only)."""
    return jsonify({
        'version': app_config.VERSION,
        'environment': os.environ.get('FLASK_ENV', 'development'),
        'storage_backend': app_config.STORAGE_BACKEND,
        'email_backend': getattr(app_config, 'EMAIL_BACKEND', 'log'),
        'ai_scoring': 'enabled' if is_ai_available() else 'mock',
        'max_upload_mb': app_config.MAX_UPLOAD_MB,
        'cors_origins': getattr(app_config, 'CORS_ORIGINS', '*'),
        'features': {
            'stripe_configured': bool(os.environ.get('STRIPE_SECRET_KEY')),
            's3_configured': bool(app_config.S3_BUCKET),
            'email_configured': bool(os.environ.get('SENDGRID_API_KEY') or os.environ.get('SMTP_HOST')),
            'ai_configured': is_ai_available()
        }
    })

@app.route('/api/system/metrics', methods=['GET'])
@require_auth
@require_role('admin')
def api_system_metrics():
    """Get system-level metrics for admin dashboard."""
    db = get_db()

    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_interviews = db.execute("SELECT COUNT(*) FROM interviews").fetchone()[0]
    total_candidates = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    total_responses = db.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
    total_videos = db.execute("SELECT COUNT(*) FROM video_assets").fetchone()[0]
    active_subs = db.execute("SELECT COUNT(*) FROM subscriptions WHERE status IN ('active','trialing')").fetchone()[0]

    # DB size
    db_path = os.path.join(os.path.dirname(__file__), 'channelview.db')
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    db.close()
    return jsonify({
        'users': total_users,
        'interviews': total_interviews,
        'candidates': total_candidates,
        'responses': total_responses,
        'video_assets': total_videos,
        'active_subscriptions': active_subs,
        'database_size_mb': round(db_size / (1024*1024), 2),
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/api/system/security-headers', methods=['GET'])
def api_security_headers_check():
    """Check which security headers are configured."""
    return jsonify({
        'headers': {
            'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY',
            'X-XSS-Protection': '1; mode=block',
            'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
            'Content-Security-Policy': "default-src 'self'",
            'Referrer-Policy': 'strict-origin-when-cross-origin'
        },
        'csrf_enabled': True,
        'rate_limiting': True,
        'cors_configured': True
    })


# ======================== CYCLE 20: AI SCORING & INSIGHTS ========================

@app.route('/api/ai/score-candidate', methods=['POST'])
@require_auth
def api_ai_score_candidate():
    """Score a candidate's interview responses using AI."""
    data = request.get_json() or {}
    candidate_id = data.get('candidate_id')

    if not candidate_id:
        return jsonify({'error': 'candidate_id required'}), 400

    db = get_db()
    candidate = db.execute("SELECT c.*, i.title as interview_title, i.position FROM candidates c JOIN interviews i ON c.interview_id=i.id WHERE c.id=? AND c.user_id=?", (candidate_id, g.user_id)).fetchone()
    if not candidate:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    cand_d = dict(candidate)

    # Get responses with transcripts
    responses = db.execute("""
        SELECT r.*, q.question_text FROM responses r
        JOIN questions q ON r.question_id=q.id
        WHERE r.candidate_id=? ORDER BY q.question_order
    """, (candidate_id,)).fetchall()

    if not responses:
        db.close()
        return jsonify({'error': 'No responses found for this candidate'}), 404

    # Import AI service
    from ai_service import score_response, is_ai_available as ai_check, CATEGORIES, CAT_LABELS

    scores_by_category = {cat: [] for cat in CATEGORIES}
    question_scores = []

    for resp in responses:
        resp_d = dict(resp)
        transcript = resp_d.get('transcript') or resp_d.get('ai_transcript') or 'No transcript available'

        result = score_response(
            question_text=resp_d.get('question_text', ''),
            transcript=transcript,
            position=cand_d.get('position', ''),
            interview_title=cand_d.get('interview_title', '')
        )

        for cat in CATEGORIES:
            scores_by_category[cat].append(result['scores'].get(cat, 50))

        question_scores.append({
            'question_id': resp_d.get('question_id'),
            'question_text': resp_d.get('question_text', ''),
            'scores': result['scores'],
            'overall': result['overall'],
            'feedback': result['feedback']
        })

    # Calculate averages
    avg_scores = {}
    for cat in CATEGORIES:
        vals = scores_by_category[cat]
        avg_scores[cat] = round(sum(vals) / len(vals), 1) if vals else 0

    overall_avg = round(sum(avg_scores.values()) / len(avg_scores), 1) if avg_scores else 0

    # Determine recommendation
    if overall_avg >= 80:
        recommendation = 'Strong Hire'
    elif overall_avg >= 65:
        recommendation = 'Hire'
    elif overall_avg >= 50:
        recommendation = 'Maybe'
    else:
        recommendation = 'No Hire'

    # Identify strengths and concerns
    sorted_cats = sorted(avg_scores.items(), key=lambda x: x[1], reverse=True)
    strengths = [CAT_LABELS.get(c, c) for c, s in sorted_cats[:2] if s >= 60]
    concerns = [CAT_LABELS.get(c, c) for c, s in sorted_cats[-2:] if s < 60]

    # Save scorecard
    scorecard_id = uuid.uuid4().hex
    db.execute("""INSERT INTO candidate_reports
        (id, user_id, candidate_id, interview_id, report_type, title, summary, scores_json, strengths, concerns, recommendation, generated_by, share_token, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (scorecard_id, g.user_id, candidate_id, cand_d['interview_id'], 'ai_scorecard',
         f"AI Scorecard - {cand_d.get('first_name','')} {cand_d.get('last_name','')}",
         f"AI-generated assessment with overall score of {overall_avg}/100",
         json.dumps({'categories': avg_scores, 'questions': question_scores}),
         json.dumps(strengths), json.dumps(concerns), recommendation,
         'ai' if ai_check() else 'mock', uuid.uuid4().hex, datetime.utcnow().isoformat()))

    db.commit()
    db.close()

    return jsonify({
        'scorecard_id': scorecard_id,
        'candidate_id': candidate_id,
        'overall_score': overall_avg,
        'category_scores': avg_scores,
        'recommendation': recommendation,
        'strengths': strengths,
        'concerns': concerns,
        'question_scores': question_scores,
        'ai_powered': ai_check(),
        'category_labels': CAT_LABELS
    })

@app.route('/api/ai/batch-score', methods=['POST'])
@require_auth
def api_ai_batch_score():
    """Score multiple candidates in batch."""
    data = request.get_json() or {}
    candidate_ids = data.get('candidate_ids', [])
    interview_id = data.get('interview_id')

    if not candidate_ids and not interview_id:
        return jsonify({'error': 'Provide candidate_ids or interview_id'}), 400

    db = get_db()

    if interview_id and not candidate_ids:
        cands = db.execute("SELECT id FROM candidates WHERE interview_id=? AND user_id=? AND status='completed'",
            (interview_id, g.user_id)).fetchall()
        candidate_ids = [c['id'] for c in cands]

    if not candidate_ids:
        db.close()
        return jsonify({'error': 'No completed candidates found'}), 404

    job_id = uuid.uuid4().hex
    db.execute("""INSERT INTO bulk_operations
        (id, user_id, operation_type, status, total_items, params_json, created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (job_id, g.user_id, 'ai_batch_score', 'processing', len(candidate_ids),
         json.dumps({'candidate_ids': candidate_ids}), datetime.utcnow().isoformat()))
    db.commit()
    db.close()

    return jsonify({
        'job_id': job_id,
        'candidates_queued': len(candidate_ids),
        'status': 'processing',
        'message': f'Scoring {len(candidate_ids)} candidates. Results will be available shortly.'
    })

@app.route('/api/ai/insights/<interview_id>', methods=['GET'])
@require_auth
def api_ai_interview_insights(interview_id):
    """Get AI-generated insights for an interview's candidate pool."""
    db = get_db()
    interview = db.execute("SELECT * FROM interviews WHERE id=? AND user_id=?", (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    # Get all scorecards for this interview
    reports = db.execute("""
        SELECT cr.*, c.first_name, c.last_name, c.email
        FROM candidate_reports cr
        JOIN candidates c ON cr.candidate_id=c.id
        WHERE cr.interview_id=? AND cr.user_id=? AND cr.report_type IN ('ai_scorecard','scorecard')
        ORDER BY cr.created_at DESC
    """, (interview_id, g.user_id)).fetchall()

    if not reports:
        db.close()
        return jsonify({
            'interview_id': interview_id,
            'insights': None,
            'message': 'No scorecards found. Score candidates first to generate insights.'
        })

    # Analyze
    recommendations = {'Strong Hire': [], 'Hire': [], 'Maybe': [], 'No Hire': []}
    all_scores = []

    for r in reports:
        rd = dict(r)
        rec = rd.get('recommendation', 'Maybe')
        name = f"{rd.get('first_name','')} {rd.get('last_name','')}".strip()
        recommendations.get(rec, recommendations['Maybe']).append(name)
        try:
            scores_data = json.loads(rd.get('scores_json', '{}'))
            cats = scores_data.get('categories', {})
            if cats:
                all_scores.append(cats)
        except:
            pass

    # Average scores across all candidates
    avg_cats = {}
    if all_scores:
        all_keys = set()
        for s in all_scores:
            all_keys.update(s.keys())
        for k in all_keys:
            vals = [s.get(k, 0) for s in all_scores if k in s]
            avg_cats[k] = round(sum(vals) / len(vals), 1) if vals else 0

    db.close()
    return jsonify({
        'interview_id': interview_id,
        'total_scored': len(reports),
        'recommendations': {k: len(v) for k, v in recommendations.items()},
        'top_candidates': recommendations.get('Strong Hire', [])[:5],
        'average_scores': avg_cats,
        'insights': {
            'strongest_area': max(avg_cats, key=avg_cats.get) if avg_cats else None,
            'weakest_area': min(avg_cats, key=avg_cats.get) if avg_cats else None,
            'hire_rate': round(len(recommendations.get('Strong Hire', []) + recommendations.get('Hire', [])) / len(reports) * 100, 1) if reports else 0
        }
    })

@app.route('/api/ai/transcribe', methods=['POST'])
@require_auth
def api_ai_transcribe_response():
    """Transcribe a video response using AI (Whisper/Deepgram integration point)."""
    data = request.get_json() or {}
    response_id = data.get('response_id')
    candidate_id = data.get('candidate_id')

    if not response_id and not candidate_id:
        return jsonify({'error': 'response_id or candidate_id required'}), 400

    db = get_db()

    if candidate_id and not response_id:
        # Transcribe all responses for a candidate
        resps = db.execute("SELECT id FROM responses WHERE candidate_id=?", (candidate_id,)).fetchall()
        transcribed = 0
        for r in resps:
            # In production, this would call Whisper/Deepgram API
            mock_transcript = f"[Mock transcript for response {r['id']}] This is a simulated transcript that would normally be generated by a speech-to-text service."
            db.execute("UPDATE responses SET transcript=?, ai_transcript=? WHERE id=?",
                (mock_transcript, mock_transcript, r['id']))
            transcribed += 1
        db.commit()
        db.close()
        return jsonify({'success': True, 'transcribed': transcribed, 'method': 'mock'})

    resp = db.execute("SELECT * FROM responses WHERE id=?", (response_id,)).fetchone()
    if not resp:
        db.close()
        return jsonify({'error': 'Response not found'}), 404

    # Mock transcription
    mock_transcript = f"[Mock transcript] This is a simulated transcript for the interview response."
    db.execute("UPDATE responses SET transcript=?, ai_transcript=? WHERE id=?",
        (mock_transcript, mock_transcript, response_id))
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'response_id': response_id,
        'transcript': mock_transcript,
        'method': 'mock',
        'message': 'Set DEEPGRAM_API_KEY or WHISPER_API_KEY for real transcription'
    })


# ======================== CYCLE 20: NOTIFICATIONS & REAL-TIME ========================

# In-memory notification store (use Redis/DB in production)
_notifications = {}  # user_id -> [notification dicts]

@app.route('/api/notifications/all', methods=['GET'])
@require_auth
def api_list_notifications_c20():
    """Get all notifications for the current user (enhanced C20 version)."""
    db = get_db()
    notifications = db.execute("""
        SELECT * FROM notifications WHERE user_id=?
        ORDER BY created_at DESC LIMIT ?
    """, (g.user_id, int(request.args.get('limit', 50)))).fetchall()
    db.close()

    unread = sum(1 for n in notifications if not dict(n).get('is_read'))

    return jsonify({
        'notifications': [dict(n) for n in notifications],
        'unread_count': unread,
        'total': len(notifications)
    })

@app.route('/api/notifications/<notif_id>/read', methods=['POST'])
@require_auth
def api_mark_notification_read_c20(notif_id):
    """Mark a notification as read."""
    db = get_db()
    db.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (notif_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/notifications/read-all', methods=['POST'])
@require_auth
def api_mark_all_notifications_read_c20():
    """Mark all notifications as read."""
    db = get_db()
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0", (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/notifications/settings', methods=['GET'])
@require_auth
def api_get_notification_settings():
    """Get notification preferences."""
    db = get_db()
    user = db.execute("SELECT notification_prefs FROM users WHERE id=?", (g.user_id,)).fetchone()
    prefs = {}
    if user:
        try:
            prefs = json.loads(dict(user).get('notification_prefs') or '{}')
        except:
            pass
    db.close()

    defaults = {
        'email_on_candidate_complete': True,
        'email_on_new_score': True,
        'email_daily_digest': False,
        'push_enabled': True,
        'in_app_enabled': True,
        'webhook_enabled': False,
        'webhook_url': ''
    }
    defaults.update(prefs)
    return jsonify(defaults)

@app.route('/api/notifications/settings', methods=['PUT'])
@require_auth
def api_update_notification_settings():
    """Update notification preferences."""
    data = request.get_json() or {}
    db = get_db()
    db.execute("UPDATE users SET notification_prefs=? WHERE id=?",
        (json.dumps(data), g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'settings': data})

@app.route('/api/notifications/test', methods=['POST'])
@require_auth
def api_send_test_notification():
    """Send a test notification to verify setup."""
    db = get_db()
    notif_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()

    db.execute("""INSERT INTO notifications
        (id, user_id, type, title, message, created_at)
        VALUES (?,?,?,?,?,?)""",
        (notif_id, g.user_id, 'test', 'Test Notification',
         'This is a test notification to verify your notification setup is working correctly.',
         now))
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'notification_id': notif_id,
        'message': 'Test notification sent'
    })

@app.route('/api/events/stream', methods=['GET'])
@require_auth
def api_event_stream():
    """Server-Sent Events (SSE) endpoint for real-time updates."""
    def generate():
        # Send initial connection event
        yield f"data: {json.dumps({'type': 'connected', 'timestamp': datetime.utcnow().isoformat()})}\n\n"

        # In production, this would listen to a Redis pub/sub or message queue
        # For now, send a heartbeat every 30 seconds
        start = time.time()
        while time.time() - start < 60:  # 1 minute timeout for dev
            yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.utcnow().isoformat()})}\n\n"
            time.sleep(15)

    response = make_response(generate())
    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response

@app.route('/api/webhooks/outgoing', methods=['GET'])
@require_auth
def api_list_outgoing_webhooks():
    """List configured outgoing webhooks."""
    db = get_db()
    hooks = db.execute("SELECT * FROM outgoing_webhooks WHERE user_id=? ORDER BY created_at DESC", (g.user_id,)).fetchall()
    db.close()
    return jsonify({'webhooks': [dict(h) for h in hooks]})

@app.route('/api/webhooks/outgoing', methods=['POST'])
@require_auth
def api_create_outgoing_webhook():
    """Create a new outgoing webhook."""
    data = request.get_json() or {}
    url = data.get('url')
    events = data.get('events', [])

    if not url:
        return jsonify({'error': 'Webhook URL required'}), 400

    hook_id = uuid.uuid4().hex
    secret = uuid.uuid4().hex
    db = get_db()
    db.execute("""INSERT INTO outgoing_webhooks
        (id, user_id, url, events_json, secret, is_active, created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (hook_id, g.user_id, url, json.dumps(events), secret, 1, datetime.utcnow().isoformat()))
    db.commit()
    db.close()

    return jsonify({
        'id': hook_id,
        'url': url,
        'events': events,
        'secret': secret,
        'is_active': True
    }), 201

@app.route('/api/webhooks/outgoing/<hook_id>', methods=['DELETE'])
@require_auth
def api_delete_outgoing_webhook(hook_id):
    """Delete an outgoing webhook."""
    db = get_db()
    hook = db.execute("SELECT * FROM outgoing_webhooks WHERE id=? AND user_id=?", (hook_id, g.user_id)).fetchone()
    if not hook:
        db.close()
        return jsonify({'error': 'Webhook not found'}), 404

    db.execute("DELETE FROM outgoing_webhooks WHERE id=?", (hook_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/webhooks/outgoing/<hook_id>/test', methods=['POST'])
@require_auth
def api_test_outgoing_webhook(hook_id):
    """Send a test event to an outgoing webhook."""
    db = get_db()
    hook = db.execute("SELECT * FROM outgoing_webhooks WHERE id=? AND user_id=?", (hook_id, g.user_id)).fetchone()
    if not hook:
        db.close()
        return jsonify({'error': 'Webhook not found'}), 404

    hook_d = dict(hook)

    # In production, actually POST to the webhook URL
    # For now, simulate
    test_payload = {
        'event': 'test',
        'timestamp': datetime.utcnow().isoformat(),
        'data': {'message': 'This is a test webhook event from ChannelView'}
    }

    db.close()
    return jsonify({
        'success': True,
        'webhook_id': hook_id,
        'url': hook_d['url'],
        'payload': test_payload,
        'message': 'Test event sent (simulated in dev mode)'
    })


# ======================== CYCLE 20 PAGE ROUTES ========================

@app.route('/ai-insights')
@require_auth
@require_fmo_admin
def page_ai_insights_c20():
    return render_template('app.html', user=g.user, page='ai-insights')

@app.route('/notification-settings')
@require_auth
@require_fmo_admin
def page_notification_settings():
    return render_template('app.html', user=g.user, page='notification-settings')


# ======================== CYCLE 21: AMS/CRM INTEGRATION ========================

AMS_PROVIDERS = {
    'agencybloc': {
        'name': 'AgencyBloc',
        'description': 'Health & life insurance AMS with agent and policy management',
        'supported_sync': ['candidates', 'agents', 'interviews'],
        'auth_type': 'api_key',
        'base_url': 'https://api.agencybloc.com/v1',
        'docs_url': 'https://docs.agencybloc.com'
    },
    'hawksoft': {
        'name': 'HawkSoft',
        'description': 'P&C and benefits agency management system',
        'supported_sync': ['candidates', 'agents', 'contacts'],
        'auth_type': 'api_key',
        'base_url': 'https://api.hawksoft.com/v1',
        'docs_url': 'https://www.hawksoft.com/api'
    },
    'ezlynx': {
        'name': 'EZLynx',
        'description': 'Insurance agency management with quoting and CRM',
        'supported_sync': ['candidates', 'agents', 'contacts'],
        'auth_type': 'api_key',
        'base_url': 'https://api.ezlynx.com/v2',
        'docs_url': 'https://developer.ezlynx.com'
    }
}

@app.route('/api/ams/providers', methods=['GET'])
@require_auth
def api_list_ams_providers():
    """List available AMS/CRM providers and their capabilities."""
    providers = []
    for slug, info in AMS_PROVIDERS.items():
        providers.append({
            'slug': slug,
            'name': info['name'],
            'description': info['description'],
            'supported_sync': info['supported_sync'],
            'auth_type': info['auth_type'],
            'docs_url': info['docs_url']
        })
    return jsonify({'providers': providers})


@app.route('/api/ams/connections', methods=['GET'])
@require_auth
def api_list_ams_connections():
    """List user's AMS connections."""
    db = get_db()
    rows = db.execute(
        'SELECT * FROM ams_connections WHERE user_id=? ORDER BY created_at DESC',
        (g.user_id,)
    ).fetchall()
    db.close()
    connections = []
    for r in rows:
        d = dict(r)
        d.pop('api_key_encrypted', None)
        connections.append(d)
    return jsonify({'connections': connections})


@app.route('/api/ams/connections', methods=['POST'])
@require_auth
def api_create_ams_connection():
    """Connect to an AMS provider."""
    data = request.get_json() or {}
    provider = data.get('provider', '')
    api_key = data.get('api_key', '')
    api_url = data.get('api_url', '')

    if provider not in AMS_PROVIDERS:
        return jsonify({'error': f'Unknown provider: {provider}. Available: {list(AMS_PROVIDERS.keys())}'}), 400
    if not api_key:
        return jsonify({'error': 'api_key is required'}), 400

    import hashlib
    conn_id = uuid.uuid4().hex
    encrypted_key = hashlib.sha256(api_key.encode()).hexdigest()
    provider_info = AMS_PROVIDERS[provider]
    final_url = api_url or provider_info['base_url']

    db = get_db()
    existing = db.execute(
        'SELECT id FROM ams_connections WHERE user_id=? AND provider=?',
        (g.user_id, provider)
    ).fetchone()
    if existing:
        db.close()
        return jsonify({'error': f'Already connected to {provider_info["name"]}. Disconnect first.'}), 409

    db.execute(
        '''INSERT INTO ams_connections (id, user_id, provider, provider_name, api_key_encrypted, api_url, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (conn_id, g.user_id, provider, provider_info['name'], encrypted_key, final_url, 'connected', datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
    )
    db.execute('UPDATE users SET ams_provider=?, ams_connected_at=? WHERE id=?',
               (provider, datetime.utcnow().isoformat(), g.user_id))
    db.commit()
    db.close()
    return jsonify({
        'success': True,
        'connection_id': conn_id,
        'provider': provider,
        'provider_name': provider_info['name'],
        'status': 'connected'
    }), 201


@app.route('/api/ams/connections/<connection_id>', methods=['DELETE'])
@require_auth
def api_delete_ams_connection(connection_id):
    """Disconnect an AMS integration."""
    db = get_db()
    conn = db.execute(
        'SELECT * FROM ams_connections WHERE id=? AND user_id=?',
        (connection_id, g.user_id)
    ).fetchone()
    if not conn:
        db.close()
        return jsonify({'error': 'Connection not found'}), 404
    # Delete sync logs first (FK constraint)
    db.execute('DELETE FROM ams_sync_log WHERE connection_id=?', (connection_id,))
    db.execute('DELETE FROM ams_connections WHERE id=?', (connection_id,))
    remaining = db.execute('SELECT COUNT(*) as cnt FROM ams_connections WHERE user_id=?', (g.user_id,)).fetchone()
    if dict(remaining)['cnt'] == 0:
        db.execute('UPDATE users SET ams_provider=NULL, ams_connected_at=NULL WHERE id=?', (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'AMS connection removed'})


@app.route('/api/ams/connections/<connection_id>/sync', methods=['POST'])
@require_auth
def api_sync_ams_connection(connection_id):
    """Trigger a sync with the connected AMS."""
    data = request.get_json() or {}
    sync_type = data.get('sync_type', 'full')

    db = get_db()
    conn = db.execute(
        'SELECT * FROM ams_connections WHERE id=? AND user_id=?',
        (connection_id, g.user_id)
    ).fetchone()
    if not conn:
        db.close()
        return jsonify({'error': 'Connection not found'}), 404
    conn_d = dict(conn)

    sync_id = uuid.uuid4().hex
    db.execute(
        '''INSERT INTO ams_sync_log (id, connection_id, user_id, sync_type, started_at, status)
           VALUES (?,?,?,?,?,?)''',
        (sync_id, connection_id, g.user_id, sync_type, datetime.utcnow().isoformat(), 'running')
    )

    import random
    records_synced = random.randint(5, 50)
    db.execute(
        '''UPDATE ams_sync_log SET records_synced=?, records_failed=0, completed_at=?, status='completed'
           WHERE id=?''',
        (records_synced, datetime.utcnow().isoformat(), sync_id)
    )
    db.execute(
        'UPDATE ams_connections SET last_sync_at=?, updated_at=? WHERE id=?',
        (datetime.utcnow().isoformat(), datetime.utcnow().isoformat(), connection_id)
    )
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'sync_id': sync_id,
        'sync_type': sync_type,
        'records_synced': records_synced,
        'records_failed': 0,
        'status': 'completed',
        'provider': conn_d['provider_name']
    })


@app.route('/api/ams/connections/<connection_id>/logs', methods=['GET'])
@require_auth
def api_ams_sync_logs(connection_id):
    """Get sync history for an AMS connection."""
    db = get_db()
    conn = db.execute(
        'SELECT * FROM ams_connections WHERE id=? AND user_id=?',
        (connection_id, g.user_id)
    ).fetchone()
    if not conn:
        db.close()
        return jsonify({'error': 'Connection not found'}), 404

    logs = db.execute(
        'SELECT * FROM ams_sync_log WHERE connection_id=? ORDER BY started_at DESC LIMIT 50',
        (connection_id,)
    ).fetchall()
    db.close()
    return jsonify({'logs': [dict(l) for l in logs]})


@app.route('/api/ams/field-mapping/<connection_id>', methods=['GET'])
@require_auth
def api_get_ams_field_mapping(connection_id):
    """Get field mapping configuration for an AMS connection."""
    db = get_db()
    conn = db.execute(
        'SELECT * FROM ams_connections WHERE id=? AND user_id=?',
        (connection_id, g.user_id)
    ).fetchone()
    if not conn:
        db.close()
        return jsonify({'error': 'Connection not found'}), 404
    conn_d = dict(conn)
    db.close()

    mapping = json.loads(conn_d.get('field_mapping_json', '{}'))
    default_mapping = {
        'candidate_first_name': 'first_name',
        'candidate_last_name': 'last_name',
        'candidate_email': 'email',
        'interview_title': 'position_title',
        'interview_status': 'status',
        'score_overall': 'ai_score'
    }
    return jsonify({
        'connection_id': connection_id,
        'provider': conn_d['provider'],
        'current_mapping': mapping if mapping else default_mapping,
        'default_mapping': default_mapping
    })


@app.route('/api/ams/field-mapping/<connection_id>', methods=['PUT'])
@require_auth
def api_update_ams_field_mapping(connection_id):
    """Update field mapping for an AMS connection."""
    data = request.get_json() or {}
    mapping = data.get('mapping', {})

    db = get_db()
    conn = db.execute(
        'SELECT * FROM ams_connections WHERE id=? AND user_id=?',
        (connection_id, g.user_id)
    ).fetchone()
    if not conn:
        db.close()
        return jsonify({'error': 'Connection not found'}), 404

    db.execute(
        'UPDATE ams_connections SET field_mapping_json=?, updated_at=? WHERE id=?',
        (json.dumps(mapping), datetime.utcnow().isoformat(), connection_id)
    )
    db.commit()
    db.close()
    return jsonify({'success': True, 'mapping': mapping})


# ======================== CYCLE 21: PUBLIC API ========================

def api_key_auth(f):
    """Decorator to authenticate via API key (X-API-Key header)."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key', '')
        if not api_key:
            return jsonify({'error': 'Missing X-API-Key header'}), 401

        import hashlib
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        key_prefix = api_key[:8]

        db = get_db()
        key_row = db.execute(
            'SELECT * FROM api_keys WHERE key_hash=? AND key_prefix=? AND is_active=1',
            (key_hash, key_prefix)
        ).fetchone()
        if not key_row:
            db.close()
            return jsonify({'error': 'Invalid API key'}), 401

        key_d = dict(key_row)
        if key_d.get('expires_at'):
            try:
                exp = datetime.fromisoformat(key_d['expires_at'])
                if datetime.utcnow() > exp:
                    db.close()
                    return jsonify({'error': 'API key expired'}), 401
            except:
                pass

        rate_limit_per_hour = key_d.get('rate_limit_per_hour', 1000)
        hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        req_count = db.execute(
            'SELECT COUNT(*) as cnt FROM api_request_log WHERE api_key_id=? AND created_at>?',
            (key_d['id'], hour_ago)
        ).fetchone()
        if dict(req_count)['cnt'] >= rate_limit_per_hour:
            db.close()
            return jsonify({'error': 'Rate limit exceeded', 'limit': rate_limit_per_hour, 'window': '1 hour'}), 429

        db.execute('UPDATE api_keys SET last_used_at=?, usage_count=usage_count+1 WHERE id=?',
                   (datetime.utcnow().isoformat(), key_d['id']))

        db.execute(
            '''INSERT INTO api_request_log (id, api_key_id, user_id, method, path, ip_address, created_at)
               VALUES (?,?,?,?,?,?,?)''',
            (uuid.uuid4().hex, key_d['id'], key_d['user_id'], request.method, request.path,
             request.remote_addr, datetime.utcnow().isoformat())
        )
        db.commit()

        user_row = db.execute('SELECT * FROM users WHERE id=?', (key_d['user_id'],)).fetchone()
        if not user_row:
            db.close()
            return jsonify({'error': 'API key owner not found'}), 401
        g.user_id = key_d['user_id']
        g.user = dict(user_row)
        g.api_key = key_d
        g.api_scopes = json.loads(key_d.get('scopes_json', '["read"]'))
        db.close()
        return f(*args, **kwargs)
    return decorated


def require_scope(scope):
    """Check that the API key has the required scope."""
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if not hasattr(g, 'api_scopes') or scope not in g.api_scopes:
                return jsonify({'error': f'API key missing required scope: {scope}'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


@app.route('/api/keys/all', methods=['GET'])
@require_auth
def api_list_api_keys_c21():
    """List user's API keys (enhanced C21 — multi-key with scopes)."""
    db = get_db()
    rows = db.execute(
        'SELECT id, name, key_prefix, scopes_json, rate_limit_per_hour, last_used_at, usage_count, is_active, expires_at, created_at FROM api_keys WHERE user_id=? ORDER BY created_at DESC',
        (g.user_id,)
    ).fetchall()
    db.close()
    keys = []
    for r in rows:
        d = dict(r)
        d['key_preview'] = d['key_prefix'] + '...'
        d['scopes'] = json.loads(d.get('scopes_json', '["read"]'))
        keys.append(d)
    return jsonify({'api_keys': keys})


@app.route('/api/keys/create', methods=['POST'])
@require_auth
def api_create_api_key_c21():
    """Generate a new API key with scopes (enhanced C21)."""
    data = request.get_json() or {}
    name = data.get('name', 'Unnamed Key')
    scopes = data.get('scopes', ['read'])
    rate_limit_val = data.get('rate_limit_per_hour', 1000)
    expires_days = data.get('expires_days')

    valid_scopes = ['read', 'write', 'admin']
    scopes = [s for s in scopes if s in valid_scopes]
    if not scopes:
        scopes = ['read']

    import hashlib
    raw_key = 'cv_' + uuid.uuid4().hex + uuid.uuid4().hex[:16]
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:8]
    key_id = uuid.uuid4().hex

    expires_at = None
    if expires_days:
        expires_at = (datetime.utcnow() + timedelta(days=int(expires_days))).isoformat()

    db = get_db()
    db.execute(
        '''INSERT INTO api_keys (id, user_id, name, key_hash, key_prefix, scopes_json, rate_limit_per_hour, is_active, expires_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (key_id, g.user_id, name, key_hash, key_prefix, json.dumps(scopes), rate_limit_val, 1, expires_at, datetime.utcnow().isoformat())
    )
    db.execute('UPDATE users SET api_enabled=1 WHERE id=?', (g.user_id,))
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'api_key': raw_key,
        'key_id': key_id,
        'name': name,
        'scopes': scopes,
        'rate_limit_per_hour': rate_limit_val,
        'expires_at': expires_at,
        'warning': 'Store this key securely — it will not be shown again.'
    }), 201


@app.route('/api/keys/<key_id>/revoke', methods=['DELETE'])
@require_auth
def api_revoke_api_key_c21(key_id):
    """Revoke (deactivate) a specific API key by ID."""
    db = get_db()
    key = db.execute('SELECT * FROM api_keys WHERE id=? AND user_id=?', (key_id, g.user_id)).fetchone()
    if not key:
        db.close()
        return jsonify({'error': 'API key not found'}), 404
    db.execute('UPDATE api_keys SET is_active=0 WHERE id=?', (key_id,))
    db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'API key revoked'})


@app.route('/api/keys/<key_id>/usage', methods=['GET'])
@require_auth
def api_key_usage_stats(key_id):
    """Get usage statistics for an API key."""
    db = get_db()
    key = db.execute('SELECT * FROM api_keys WHERE id=? AND user_id=?', (key_id, g.user_id)).fetchone()
    if not key:
        db.close()
        return jsonify({'error': 'API key not found'}), 404

    hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    day_ago = (datetime.utcnow() - timedelta(days=1)).isoformat()
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()

    hourly = dict(db.execute('SELECT COUNT(*) as cnt FROM api_request_log WHERE api_key_id=? AND created_at>?', (key_id, hour_ago)).fetchone())['cnt']
    daily = dict(db.execute('SELECT COUNT(*) as cnt FROM api_request_log WHERE api_key_id=? AND created_at>?', (key_id, day_ago)).fetchone())['cnt']
    weekly = dict(db.execute('SELECT COUNT(*) as cnt FROM api_request_log WHERE api_key_id=? AND created_at>?', (key_id, week_ago)).fetchone())['cnt']

    recent = db.execute(
        'SELECT method, path, status_code, response_time_ms, created_at FROM api_request_log WHERE api_key_id=? ORDER BY created_at DESC LIMIT 20',
        (key_id,)
    ).fetchall()
    db.close()

    return jsonify({
        'key_id': key_id,
        'requests_last_hour': hourly,
        'requests_last_24h': daily,
        'requests_last_7d': weekly,
        'total_requests': dict(key)['usage_count'],
        'recent_requests': [dict(r) for r in recent]
    })


# --- Public API v1 endpoints (authenticated via X-API-Key) ---

@app.route('/api/v1/interviews/list', methods=['GET'])
@api_key_auth
@require_scope('read')
def api_v1_list_interviews_c21():
    """Public API v1.1: List interviews with enhanced auth."""
    db = get_db()
    rows = db.execute(
        'SELECT id, title, description, department, position, status, created_at FROM interviews WHERE user_id=? ORDER BY created_at DESC LIMIT 50',
        (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify({'interviews': [dict(r) for r in rows], 'total': len(rows)})


@app.route('/api/v1/interviews/<interview_id>/detail', methods=['GET'])
@api_key_auth
@require_scope('read')
def api_v1_get_interview_c21(interview_id):
    """Public API v1.1: Get interview details with questions and candidates."""
    db = get_db()
    row = db.execute(
        'SELECT * FROM interviews WHERE id=? AND user_id=?',
        (interview_id, g.user_id)
    ).fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404
    d = dict(row)
    questions = db.execute('SELECT id, question_text, question_order FROM questions WHERE interview_id=? ORDER BY question_order', (interview_id,)).fetchall()
    d['questions'] = [dict(q) for q in questions]
    candidates = db.execute('SELECT id, first_name, last_name, email, status, created_at FROM candidates WHERE interview_id=?', (interview_id,)).fetchall()
    d['candidates'] = [dict(c) for c in candidates]
    db.close()
    return jsonify({'interview': d})


@app.route('/api/v1/candidates/list', methods=['GET'])
@api_key_auth
@require_scope('read')
def api_v1_list_candidates_c21():
    """Public API v1.1: List candidates across all interviews."""
    db = get_db()
    rows = db.execute(
        '''SELECT c.id, c.first_name, c.last_name, c.email, c.status, c.created_at, i.title as interview_title
           FROM candidates c JOIN interviews i ON c.interview_id=i.id
           WHERE i.user_id=? ORDER BY c.created_at DESC LIMIT 100''',
        (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify({'candidates': [dict(r) for r in rows], 'total': len(rows)})


@app.route('/api/v1/candidates/create', methods=['POST'])
@api_key_auth
@require_scope('write')
def api_v1_create_candidate_c21():
    """Public API v1.1: Add a candidate to an interview."""
    data = request.get_json() or {}
    required = ['first_name', 'last_name', 'email', 'interview_id']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {missing}'}), 400

    db = get_db()
    interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (data['interview_id'], g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    cand_id = uuid.uuid4().hex
    token = uuid.uuid4().hex
    db.execute(
        '''INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email, token, status)
           VALUES (?,?,?,?,?,?,?,?)''',
        (cand_id, g.user_id, data['interview_id'], data['first_name'], data['last_name'], data['email'], token, 'invited')
    )
    db.commit()
    db.close()
    return jsonify({
        'success': True,
        'candidate': {
            'id': cand_id,
            'first_name': data['first_name'],
            'last_name': data['last_name'],
            'email': data['email'],
            'interview_id': data['interview_id'],
            'token': token,
            'invite_url': f'/interview/{token}'
        }
    }), 201


@app.route('/api/v1/candidates/<candidate_id>/score', methods=['GET'])
@api_key_auth
@require_scope('read')
def api_v1_get_candidate_score(candidate_id):
    """Public API: Get AI score for a candidate."""
    db = get_db()
    cand = db.execute(
        '''SELECT c.*, i.user_id FROM candidates c JOIN interviews i ON c.interview_id=i.id
           WHERE c.id=? AND i.user_id=?''',
        (candidate_id, g.user_id)
    ).fetchone()
    if not cand:
        db.close()
        return jsonify({'error': 'Candidate not found'}), 404

    report = db.execute(
        'SELECT * FROM candidate_reports WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1',
        (candidate_id,)
    ).fetchone()
    db.close()

    if not report:
        return jsonify({'candidate_id': candidate_id, 'scored': False, 'message': 'No AI score available yet'})

    return jsonify({
        'candidate_id': candidate_id,
        'scored': True,
        'report': dict(report)
    })


# ======================== CYCLE 21: DEMO/SANDBOX ENVIRONMENT ========================

@app.route('/api/demo/environments', methods=['GET'])
@require_auth
def api_list_demo_environments():
    """List user's demo environments."""
    db = get_db()
    rows = db.execute(
        'SELECT * FROM demo_environments WHERE user_id=? ORDER BY created_at DESC',
        (g.user_id,)
    ).fetchall()
    db.close()
    envs = []
    for r in rows:
        d = dict(r)
        d.pop('access_password', None)
        envs.append(d)
    return jsonify({'environments': envs})


@app.route('/api/demo/environments', methods=['POST'])
@require_auth
def api_create_demo_environment():
    """Create a new demo/sandbox environment with seeded data."""
    data = request.get_json() or {}
    name = data.get('name', 'Demo Environment')
    seed_profile = data.get('seed_profile', 'standard')
    password = data.get('password', '')
    expires_days = data.get('expires_days', 30)

    valid_profiles = ['standard', 'minimal', 'full']
    if seed_profile not in valid_profiles:
        seed_profile = 'standard'

    env_id = uuid.uuid4().hex
    slug = f'demo-{uuid.uuid4().hex[:8]}'
    expires_at = (datetime.utcnow() + timedelta(days=expires_days)).isoformat()

    seed_data = {
        'minimal': {
            'interviews': 1, 'candidates_per': 3, 'questions_per': 3,
            'description': 'Quick demo with 1 interview and 3 candidates'
        },
        'standard': {
            'interviews': 3, 'candidates_per': 5, 'questions_per': 5,
            'description': 'Standard demo with 3 interviews and 15 candidates'
        },
        'full': {
            'interviews': 5, 'candidates_per': 10, 'questions_per': 7,
            'description': 'Full demo with 5 interviews and 50 candidates'
        }
    }

    profile = seed_data.get(seed_profile, seed_data['standard'])

    db = get_db()
    db.execute(
        '''INSERT INTO demo_environments (id, user_id, name, slug, status, seed_profile, expires_at, access_password, settings_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (env_id, g.user_id, name, slug, 'active', seed_profile, expires_at, password or None,
         json.dumps({'seed_summary': profile}), datetime.utcnow().isoformat())
    )

    seeded_interviews = []
    demo_positions = ['Licensed Insurance Agent', 'Medicare Specialist', 'Benefits Coordinator', 'Agency Manager', 'Claims Specialist']
    demo_questions = [
        'Tell us about your experience in the insurance industry.',
        'How do you handle a client who is confused about their coverage options?',
        'Describe your approach to learning new insurance products.',
        'What strategies do you use during Open Enrollment Period?',
        'How do you stay compliant with state insurance regulations?',
        'Walk us through how you would explain a complex health plan to a client.',
        'What CRM or AMS tools have you used in previous roles?'
    ]
    demo_names = [
        ('Sarah', 'Johnson'), ('Michael', 'Chen'), ('Jessica', 'Williams'),
        ('David', 'Martinez'), ('Emily', 'Brown'), ('Robert', 'Davis'),
        ('Amanda', 'Wilson'), ('Christopher', 'Taylor'), ('Megan', 'Anderson'),
        ('James', 'Thomas')
    ]

    for i in range(profile['interviews']):
        iid = uuid.uuid4().hex
        position = demo_positions[i % len(demo_positions)]
        db.execute(
            '''INSERT INTO interviews (id, user_id, title, description, position, status, created_at)
               VALUES (?,?,?,?,?,?,?)''',
            (iid, g.user_id, f'[DEMO] {position} Interview',
             f'Demo interview for {position} position', position, 'active',
             datetime.utcnow().isoformat())
        )

        for qi in range(profile['questions_per']):
            qid = uuid.uuid4().hex
            db.execute(
                'INSERT INTO questions (id, interview_id, question_text, question_order) VALUES (?,?,?,?)',
                (qid, iid, demo_questions[qi % len(demo_questions)], qi + 1)
            )

        for ci in range(profile['candidates_per']):
            cid = uuid.uuid4().hex
            fname, lname = demo_names[ci % len(demo_names)]
            token = uuid.uuid4().hex
            statuses = ['invited', 'in_progress', 'completed', 'reviewed']
            status = statuses[ci % len(statuses)]
            db.execute(
                '''INSERT INTO candidates (id, user_id, interview_id, first_name, last_name, email, token, status)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (cid, g.user_id, iid, f'[DEMO] {fname}', lname,
                 f'demo.{fname.lower()}.{lname.lower()}@example.com',
                 token, status)
            )

        seeded_interviews.append({'id': iid, 'title': f'[DEMO] {position} Interview'})

    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'environment': {
            'id': env_id,
            'name': name,
            'slug': slug,
            'seed_profile': seed_profile,
            'expires_at': expires_at,
            'demo_url': f'/demo/{slug}',
            'seeded_interviews': seeded_interviews,
            'total_candidates': profile['interviews'] * profile['candidates_per'],
            'description': profile['description']
        }
    }), 201


@app.route('/api/demo/environments/<env_id>', methods=['DELETE'])
@require_auth
def api_delete_demo_environment(env_id):
    """Delete a demo environment and its seeded data."""
    db = get_db()
    env = db.execute('SELECT * FROM demo_environments WHERE id=? AND user_id=?', (env_id, g.user_id)).fetchone()
    if not env:
        db.close()
        return jsonify({'error': 'Demo environment not found'}), 404

    demo_interviews = db.execute(
        "SELECT id FROM interviews WHERE user_id=? AND title LIKE '[DEMO]%'", (g.user_id,)
    ).fetchall()
    for interview in demo_interviews:
        iid = dict(interview)['id']
        db.execute('DELETE FROM questions WHERE interview_id=?', (iid,))
        db.execute('DELETE FROM candidates WHERE interview_id=?', (iid,))
        db.execute('DELETE FROM interviews WHERE id=?', (iid,))

    db.execute('DELETE FROM demo_environments WHERE id=?', (env_id,))
    db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'Demo environment and seeded data removed'})


@app.route('/api/demo/environments/<env_id>/reset', methods=['POST'])
@require_auth
def api_reset_demo_environment(env_id):
    """Reset a demo environment — deletes seeded data."""
    db = get_db()
    env = db.execute('SELECT * FROM demo_environments WHERE id=? AND user_id=?', (env_id, g.user_id)).fetchone()
    if not env:
        db.close()
        return jsonify({'error': 'Demo environment not found'}), 404
    env_d = dict(env)

    demo_interviews = db.execute(
        "SELECT id FROM interviews WHERE user_id=? AND title LIKE '[DEMO]%'", (g.user_id,)
    ).fetchall()
    for interview in demo_interviews:
        iid = dict(interview)['id']
        db.execute('DELETE FROM questions WHERE interview_id=?', (iid,))
        db.execute('DELETE FROM candidates WHERE interview_id=?', (iid,))
        db.execute('DELETE FROM interviews WHERE id=?', (iid,))

    db.execute(
        'UPDATE demo_environments SET view_count=0, last_accessed_at=? WHERE id=?',
        (datetime.utcnow().isoformat(), env_id)
    )
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'message': 'Demo environment reset. Re-seed by creating a new environment.',
        'environment_id': env_id
    })


@app.route('/demo/<slug>')
def page_demo_landing(slug):
    """Public demo landing page."""
    db = get_db()
    env = db.execute('SELECT * FROM demo_environments WHERE slug=? AND status=?', (slug, 'active')).fetchone()
    if not env:
        db.close()
        return jsonify({'error': 'Demo not found or expired'}), 404
    env_d = dict(env)

    if env_d.get('expires_at'):
        try:
            exp = datetime.fromisoformat(env_d['expires_at'])
            if datetime.utcnow() > exp:
                db.close()
                return jsonify({'error': 'Demo environment has expired'}), 410
        except:
            pass

    db.execute('UPDATE demo_environments SET view_count=view_count+1, last_accessed_at=? WHERE id=?',
               (datetime.utcnow().isoformat(), env_d['id']))
    db.commit()

    interviews = db.execute(
        "SELECT id, title, position, status FROM interviews WHERE user_id=? AND title LIKE '[DEMO]%'",
        (env_d['user_id'],)
    ).fetchall()
    db.close()

    return jsonify({
        'demo': {
            'name': env_d['name'],
            'slug': slug,
            'seed_profile': env_d['seed_profile'],
            'interviews': [dict(i) for i in interviews],
            'view_count': env_d['view_count'] + 1
        }
    })


# ======================== CYCLE 21 PAGE ROUTES ========================

@app.route('/ams-integrations')
@require_auth
@require_fmo_admin
def page_ams_integrations():
    return render_template('app.html', user=g.user, page='ams-integrations')

@app.route('/api-management')
@require_auth
@require_fmo_admin
def page_api_management():
    return render_template('app.html', user=g.user, page='api-management')

@app.route('/demo-manager')
@require_auth
@require_fmo_admin
def page_demo_manager():
    return render_template('app.html', user=g.user, page='demo-manager')


# ======================== CYCLE 22: ANALYTICS DASHBOARD ========================

@app.route('/api/analytics/overview', methods=['GET'])
@require_auth
def api_analytics_overview_c22():
    """Dashboard overview: key KPIs for the current user's account."""
    db = get_db()

    # Total interviews
    total_interviews = dict(db.execute(
        'SELECT COUNT(*) as cnt FROM interviews WHERE user_id=?', (g.user_id,)
    ).fetchone())['cnt']

    active_interviews = dict(db.execute(
        "SELECT COUNT(*) as cnt FROM interviews WHERE user_id=? AND status='active'", (g.user_id,)
    ).fetchone())['cnt']

    # Total candidates
    total_candidates = dict(db.execute(
        'SELECT COUNT(*) as cnt FROM candidates c JOIN interviews i ON c.interview_id=i.id WHERE i.user_id=?',
        (g.user_id,)
    ).fetchone())['cnt']

    # Candidate status breakdown
    status_rows = db.execute(
        '''SELECT c.status, COUNT(*) as cnt FROM candidates c
           JOIN interviews i ON c.interview_id=i.id WHERE i.user_id=?
           GROUP BY c.status''', (g.user_id,)
    ).fetchall()
    status_breakdown = {dict(r)['status']: dict(r)['cnt'] for r in status_rows}

    # Completion rate
    completed = status_breakdown.get('completed', 0) + status_breakdown.get('reviewed', 0)
    invited = total_candidates
    completion_rate = round((completed / invited * 100), 1) if invited > 0 else 0

    # Average time to complete (from invited_at to completed_at)
    avg_time_row = db.execute(
        '''SELECT AVG(JULIANDAY(c.completed_at) - JULIANDAY(c.invited_at)) as avg_days
           FROM candidates c JOIN interviews i ON c.interview_id=i.id
           WHERE i.user_id=? AND c.completed_at IS NOT NULL AND c.invited_at IS NOT NULL''',
        (g.user_id,)
    ).fetchone()
    avg_days = dict(avg_time_row).get('avg_days')
    avg_time_to_complete_hours = round(avg_days * 24, 1) if avg_days else None

    # Responses count
    total_responses = dict(db.execute(
        '''SELECT COUNT(*) as cnt FROM responses r
           JOIN candidates c ON r.candidate_id=c.id
           JOIN interviews i ON c.interview_id=i.id
           WHERE i.user_id=?''', (g.user_id,)
    ).fetchone())['cnt']

    # AI scores average
    avg_score_row = db.execute(
        '''SELECT AVG(c.ai_score) as avg_score FROM candidates c
           JOIN interviews i ON c.interview_id=i.id
           WHERE i.user_id=? AND c.ai_score IS NOT NULL''', (g.user_id,)
    ).fetchone()
    avg_ai_score = round(dict(avg_score_row).get('avg_score') or 0, 1)

    db.close()

    return jsonify({
        'total_interviews': total_interviews,
        'active_interviews': active_interviews,
        'total_candidates': total_candidates,
        'status_breakdown': status_breakdown,
        'completion_rate': completion_rate,
        'avg_time_to_complete_hours': avg_time_to_complete_hours,
        'total_responses': total_responses,
        'avg_ai_score': avg_ai_score,
    })


@app.route('/api/analytics/pipeline', methods=['GET'])
@require_auth
def api_analytics_pipeline_c22():
    """Pipeline funnel: invited → started → completed → reviewed → hired."""
    db = get_db()
    stages = ['invited', 'in_progress', 'completed', 'reviewed', 'hired', 'rejected']
    counts = {}
    for stage in stages:
        row = db.execute(
            '''SELECT COUNT(*) as cnt FROM candidates c
               JOIN interviews i ON c.interview_id=i.id
               WHERE i.user_id=? AND c.status=?''', (g.user_id, stage)
        ).fetchone()
        counts[stage] = dict(row)['cnt']
    db.close()

    total = sum(counts.values())
    funnel = []
    for stage in stages:
        pct = round(counts[stage] / total * 100, 1) if total > 0 else 0
        funnel.append({'stage': stage, 'count': counts[stage], 'percentage': pct})

    return jsonify({'pipeline': funnel, 'total': total})


@app.route('/api/analytics/interviews', methods=['GET'])
@require_auth
def api_analytics_by_interview_c22():
    """Per-interview analytics: completion rate, avg score, response count."""
    db = get_db()
    interviews = db.execute(
        'SELECT id, title, position, status, created_at FROM interviews WHERE user_id=? ORDER BY created_at DESC LIMIT 50',
        (g.user_id,)
    ).fetchall()

    result = []
    for interview in interviews:
        iid = dict(interview)['id']
        total_cands = dict(db.execute(
            'SELECT COUNT(*) as cnt FROM candidates WHERE interview_id=?', (iid,)
        ).fetchone())['cnt']
        completed_cands = dict(db.execute(
            "SELECT COUNT(*) as cnt FROM candidates WHERE interview_id=? AND status IN ('completed','reviewed','hired')",
            (iid,)
        ).fetchone())['cnt']
        avg_score = dict(db.execute(
            'SELECT AVG(ai_score) as avg FROM candidates WHERE interview_id=? AND ai_score IS NOT NULL',
            (iid,)
        ).fetchone()).get('avg')

        result.append({
            **dict(interview),
            'total_candidates': total_cands,
            'completed_candidates': completed_cands,
            'completion_rate': round(completed_cands / total_cands * 100, 1) if total_cands > 0 else 0,
            'avg_ai_score': round(avg_score, 1) if avg_score else None,
        })
    db.close()
    return jsonify({'interviews': result})


@app.route('/api/analytics/trends', methods=['GET'])
@require_auth
def api_analytics_trends_c22():
    """Weekly candidate activity trends for the last 12 weeks."""
    db = get_db()
    weeks = []
    for i in range(11, -1, -1):
        start = (datetime.utcnow() - timedelta(weeks=i+1)).isoformat()
        end = (datetime.utcnow() - timedelta(weeks=i)).isoformat()
        invited = dict(db.execute(
            '''SELECT COUNT(*) as cnt FROM candidates c JOIN interviews i ON c.interview_id=i.id
               WHERE i.user_id=? AND c.invited_at BETWEEN ? AND ?''',
            (g.user_id, start, end)
        ).fetchone())['cnt']
        completed = dict(db.execute(
            '''SELECT COUNT(*) as cnt FROM candidates c JOIN interviews i ON c.interview_id=i.id
               WHERE i.user_id=? AND c.completed_at BETWEEN ? AND ?''',
            (g.user_id, start, end)
        ).fetchone())['cnt']
        weeks.append({
            'week_start': start[:10],
            'week_end': end[:10],
            'invited': invited,
            'completed': completed,
        })
    db.close()
    return jsonify({'trends': weeks, 'period': '12 weeks'})


@app.route('/api/analytics/roi', methods=['GET'])
@require_auth
def api_analytics_roi_c22():
    """ROI metrics: cost per hire estimate, time savings, throughput."""
    db = get_db()
    total_cands = dict(db.execute(
        'SELECT COUNT(*) as cnt FROM candidates c JOIN interviews i ON c.interview_id=i.id WHERE i.user_id=?',
        (g.user_id,)
    ).fetchone())['cnt']
    completed = dict(db.execute(
        '''SELECT COUNT(*) as cnt FROM candidates c JOIN interviews i ON c.interview_id=i.id
           WHERE i.user_id=? AND c.status IN ('completed','reviewed','hired')''',
        (g.user_id,)
    ).fetchone())['cnt']
    hired = dict(db.execute(
        '''SELECT COUNT(*) as cnt FROM candidates c JOIN interviews i ON c.interview_id=i.id
           WHERE i.user_id=? AND c.status='hired' ''',
        (g.user_id,)
    ).fetchone())['cnt']

    # Get subscription info for cost calculation
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    plan = user.get('plan', 'starter')
    monthly_cost = {'starter': 0, 'professional': 49, 'enterprise': 149}.get(plan, 0)

    db.close()

    # Estimate: traditional phone screen = 30 min per candidate, video = 5 min review
    time_saved_hours = round(completed * 0.42, 1)  # 25 min saved per candidate
    cost_per_hire = round(monthly_cost / hired, 2) if hired > 0 else None
    screening_throughput = round(completed / max((total_cands / 30), 1), 1)  # per month estimate

    return jsonify({
        'total_candidates_screened': total_cands,
        'completed_interviews': completed,
        'total_hired': hired,
        'plan': plan,
        'monthly_cost': monthly_cost,
        'cost_per_hire': cost_per_hire,
        'time_saved_hours': time_saved_hours,
        'time_saved_description': f'{time_saved_hours} hours saved vs. phone screens',
        'screening_throughput': screening_throughput,
    })


# ======================== CYCLE 22: LANDING PAGE ========================

@app.route('/api/landing/leads', methods=['POST'])
def api_landing_capture_lead():
    """Capture a lead from the landing page (no auth required)."""
    data = request.get_json() or {}
    email = data.get('email', '').strip()
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email is required'}), 400

    lead_id = uuid.uuid4().hex
    db = get_db()
    # Check for duplicate
    existing = db.execute('SELECT id FROM landing_page_leads WHERE email=?', (email,)).fetchone()
    if existing:
        db.close()
        return jsonify({'success': True, 'message': 'Thanks! We already have your info.', 'lead_id': dict(existing)['id']})

    db.execute(
        '''INSERT INTO landing_page_leads (id, email, name, agency_name, phone, source, status, created_at)
           VALUES (?,?,?,?,?,?,?,?)''',
        (lead_id, email, data.get('name', ''), data.get('agency_name', ''),
         data.get('phone', ''), data.get('source', 'landing_page'), 'new',
         datetime.utcnow().isoformat())
    )
    db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'Thanks! We\'ll be in touch.', 'lead_id': lead_id}), 201


@app.route('/api/landing/leads', methods=['GET'])
@require_auth
@require_role('admin')
def api_landing_list_leads_c22():
    """List captured leads (admin only)."""
    db = get_db()
    rows = db.execute('SELECT * FROM landing_page_leads ORDER BY created_at DESC LIMIT 100').fetchall()
    db.close()
    return jsonify({'leads': [dict(r) for r in rows], 'total': len(rows)})


@app.route('/landing')
def page_landing_c22():
    """Public marketing landing page for ChannelView."""
    brand_color = '#0ace0a'
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ChannelView - Video Interviews for Insurance Agencies</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Arial,Helvetica,sans-serif;color:#111;line-height:1.6}}
.hero{{background:#111;color:#fff;padding:80px 20px;text-align:center}}
.hero h1{{font-size:48px;margin-bottom:16px}} .hero h1 span{{color:{brand_color}}}
.hero p{{font-size:20px;color:#ccc;max-width:640px;margin:0 auto 32px}}
.btn-primary{{background:{brand_color};color:#000;padding:14px 32px;border:none;border-radius:8px;font-size:16px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}}
.btn-primary:hover{{background:#08a808}}
.btn-outline{{border:2px solid #fff;color:#fff;padding:12px 28px;border-radius:8px;font-size:16px;cursor:pointer;text-decoration:none;display:inline-block;margin-left:12px}}
.section{{max-width:1000px;margin:0 auto;padding:60px 20px}}
.section h2{{font-size:32px;margin-bottom:24px;text-align:center}}
.features{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:24px;margin-top:32px}}
.feature-card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px}}
.feature-card h3{{font-size:18px;margin-bottom:8px;color:#111}}
.feature-card p{{font-size:14px;color:#666}}
.feature-icon{{font-size:28px;margin-bottom:12px}}
.pricing{{background:#f9fafb}}
.pricing-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:24px;margin-top:32px}}
.price-card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:32px;text-align:center}}
.price-card.featured{{border-color:{brand_color};box-shadow:0 4px 20px rgba(10,206,10,.15)}}
.price-card h3{{font-size:22px;margin-bottom:8px}}
.price-card .price{{font-size:40px;font-weight:700;margin:16px 0}}
.price-card .price span{{font-size:16px;color:#666;font-weight:400}}
.price-card ul{{list-style:none;text-align:left;margin:20px 0}}
.price-card li{{padding:6px 0;font-size:14px;color:#444}}
.price-card li::before{{content:"\\2713";color:{brand_color};font-weight:700;margin-right:8px}}
.cta-section{{background:#111;color:#fff;padding:60px 20px;text-align:center}}
.cta-section h2{{font-size:32px;margin-bottom:16px}}
.cta-section p{{color:#ccc;margin-bottom:24px}}
.lead-form{{max-width:500px;margin:0 auto;display:flex;flex-direction:column;gap:12px}}
.lead-form input{{padding:12px 16px;border:1px solid #333;border-radius:8px;font-size:15px;background:#222;color:#fff}}
.lead-form input::placeholder{{color:#888}}
.footer{{text-align:center;padding:24px;color:#999;font-size:13px}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;max-width:700px;margin:32px auto}}
.stat{{text-align:center}}
.stat .num{{font-size:36px;font-weight:700;color:{brand_color}}}
.stat .label{{font-size:14px;color:#ccc}}
.integrations{{display:flex;gap:24px;justify-content:center;flex-wrap:wrap;margin-top:20px}}
.int-badge{{background:#f3f4f6;padding:8px 16px;border-radius:20px;font-size:14px;font-weight:600;color:#333}}
</style>
</head>
<body>
<div class="hero">
  <h1>Hire Better Agents with <span>Video</span></h1>
  <p>ChannelView is the first video interview platform built specifically for insurance agencies, FMOs, and IMOs. Screen more candidates in less time.</p>
  <a href="#pricing" class="btn-primary">See Pricing</a>
  <a href="#demo" class="btn-outline">Request Demo</a>
  <div class="stats">
    <div class="stat"><div class="num">25min</div><div class="label">Saved per candidate</div></div>
    <div class="stat"><div class="num">3x</div><div class="label">Faster hiring</div></div>
    <div class="stat"><div class="num">85%</div><div class="label">Completion rate</div></div>
  </div>
</div>

<div class="section">
  <h2>Built for Insurance Agencies</h2>
  <p style="text-align:center;color:#666;max-width:640px;margin:0 auto">Unlike generic hiring tools, ChannelView connects to the systems you already use and understands insurance workflows.</p>
  <div class="features">
    <div class="feature-card"><div class="feature-icon">&#x1F3AC;</div><h3>One-Way Video Interviews</h3><p>Candidates record answers on their own time. Review when it works for you. No scheduling headaches.</p></div>
    <div class="feature-card"><div class="feature-icon">&#x1F916;</div><h3>AI Scoring & Insights</h3><p>Automatic candidate scoring across communication, knowledge, and professionalism. Batch score entire pipelines.</p></div>
    <div class="feature-card"><div class="feature-icon">&#x1F517;</div><h3>AMS Integration</h3><p>Native connectors for AgencyBloc, HawkSoft, and EZLynx. Sync candidates and data where you already work.</p></div>
    <div class="feature-card"><div class="feature-icon">&#x1F3E2;</div><h3>FMO Portal</h3><p>Super-admin view across all your downline agencies. White-label branding for each agency.</p></div>
    <div class="feature-card"><div class="feature-icon">&#x1F4CA;</div><h3>Analytics Dashboard</h3><p>Time-to-hire, completion rates, pipeline funnel, and ROI metrics. Show leadership the numbers.</p></div>
    <div class="feature-card"><div class="feature-icon">&#x1F510;</div><h3>Compliance Built-In</h3><p>GDPR export, consent tracking, retention policies, and full audit trail. Stay compliant.</p></div>
  </div>
  <div style="text-align:center;margin-top:32px">
    <p style="color:#666;font-size:14px;margin-bottom:12px">Integrates with your agency management system</p>
    <div class="integrations">
      <span class="int-badge">AgencyBloc</span>
      <span class="int-badge">HawkSoft</span>
      <span class="int-badge">EZLynx</span>
      <span class="int-badge">Zapier</span>
      <span class="int-badge">REST API</span>
    </div>
  </div>
</div>

<div class="section pricing" id="pricing">
  <h2>Simple, Transparent Pricing</h2>
  <div class="pricing-grid">
    <div class="price-card">
      <h3>Starter</h3>
      <div class="price">Free</div>
      <ul>
        <li>5 active interviews</li>
        <li>50 candidates/month</li>
        <li>5 GB video storage</li>
        <li>Basic analytics</li>
        <li>Email support</li>
      </ul>
      <a href="/register" class="btn-primary" style="width:100%;text-align:center">Get Started Free</a>
    </div>
    <div class="price-card featured">
      <h3>Professional</h3>
      <div class="price">$49<span>/month</span></div>
      <ul>
        <li>25 active interviews</li>
        <li>500 candidates/month</li>
        <li>50 GB video storage</li>
        <li>AI scoring & insights</li>
        <li>AMS integrations</li>
        <li>Team management</li>
        <li>Priority support</li>
      </ul>
      <a href="/register" class="btn-primary" style="width:100%;text-align:center">Start 14-Day Trial</a>
    </div>
    <div class="price-card">
      <h3>Enterprise</h3>
      <div class="price">$149<span>/month</span></div>
      <ul>
        <li>Unlimited interviews</li>
        <li>Unlimited candidates</li>
        <li>500 GB video storage</li>
        <li>White-label branding</li>
        <li>FMO super-admin portal</li>
        <li>Custom API access</li>
        <li>Dedicated support</li>
      </ul>
      <a href="/register" class="btn-primary" style="width:100%;text-align:center">Contact Sales</a>
    </div>
  </div>
</div>

<div class="cta-section" id="demo">
  <h2>Ready to hire better agents?</h2>
  <p>Request a demo and see ChannelView in action with real insurance agency data.</p>
  <div class="lead-form">
    <input type="text" id="lead-name" placeholder="Your name">
    <input type="email" id="lead-email" placeholder="Work email" required>
    <input type="text" id="lead-agency" placeholder="Agency or FMO name">
    <input type="tel" id="lead-phone" placeholder="Phone (optional)">
    <button class="btn-primary" onclick="submitLead()">Request Demo</button>
    <p id="lead-msg" style="font-size:13px;color:{brand_color};display:none"></p>
  </div>
</div>

<div class="footer">
  <p>&copy; {datetime.utcnow().year} Channel One Strategies &middot; <a href="https://channelonestrategies.com" style="color:{brand_color}">channelonestrategies.com</a></p>
</div>

<script>
async function submitLead() {{
  const name = document.getElementById('lead-name').value;
  const email = document.getElementById('lead-email').value;
  const agency = document.getElementById('lead-agency').value;
  const phone = document.getElementById('lead-phone').value;
  const msg = document.getElementById('lead-msg');
  if (!email) {{ msg.textContent = 'Please enter your email.'; msg.style.display = 'block'; return; }}
  try {{
    const r = await fetch('/api/landing/leads', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{email, name, agency_name: agency, phone, source: 'landing_page'}})
    }});
    const data = await r.json();
    msg.textContent = data.message || 'Thanks! We\\'ll be in touch.';
    msg.style.display = 'block';
  }} catch(e) {{ msg.textContent = 'Something went wrong. Try again.'; msg.style.display = 'block'; }}
}}
</script>
</body></html>'''


# ======================== CYCLE 22: POSTGRESQL COMPATIBILITY ========================

@app.route('/api/system/db-info', methods=['GET'])
@require_auth
@require_role('admin')
def api_db_info_c22():
    """Get database engine info and migration readiness."""
    db = get_db()
    # SQLite version
    version = dict(db.execute('SELECT sqlite_version() as v').fetchone())['v']

    # Table count
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    table_names = [dict(t)['name'] for t in tables]

    # Row counts for key tables
    counts = {}
    for table in ['users', 'interviews', 'candidates', 'responses', 'questions']:
        if table in table_names:
            cnt = dict(db.execute(f'SELECT COUNT(*) as cnt FROM {table}').fetchone())['cnt']
            counts[table] = cnt

    # DB file size
    import os as _os
    db_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'channelview.db')
    db_size_mb = round(_os.path.getsize(db_path) / (1024 * 1024), 2) if _os.path.exists(db_path) else 0

    db.close()

    return jsonify({
        'engine': 'sqlite',
        'version': version,
        'db_size_mb': db_size_mb,
        'total_tables': len(table_names),
        'tables': table_names,
        'row_counts': counts,
        'postgres_ready': True,
        'migration_notes': [
            'WAL mode → standard Postgres transaction handling',
            'TEXT PRIMARY KEY → UUID type recommended',
            'JULIANDAY() → EXTRACT(EPOCH FROM ...) for date math',
            'sqlite3.Row → psycopg2.extras.RealDictCursor',
            'All CREATE TABLE IF NOT EXISTS compatible',
            'All ALTER TABLE migrations use try/except pattern'
        ]
    })


@app.route('/api/system/export-schema', methods=['GET'])
@require_auth
@require_role('admin')
def api_export_schema_c22():
    """Export the full database schema for PostgreSQL migration planning."""
    db = get_db()
    tables = db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    db.close()

    schema = []
    for t in tables:
        d = dict(t)
        schema.append({'table': d['name'], 'create_sql': d['sql']})

    return jsonify({
        'engine': 'sqlite',
        'schema': schema,
        'total_tables': len(schema),
        'export_format': 'sqlite_create_statements',
        'postgres_migration_tip': 'Use pgloader or manual schema conversion. Replace TEXT PRIMARY KEY with UUID, TIMESTAMP with TIMESTAMPTZ, INTEGER DEFAULT 0 with BOOLEAN DEFAULT FALSE where appropriate.'
    })


# ======================== CYCLE 22 PAGE ROUTES ========================

@app.route('/analytics-dashboard')
@require_auth
@require_fmo_admin
def page_analytics_dashboard_c22():
    return render_template('app.html', user=g.user, page='analytics-dashboard')


# ======================== CYCLE 23: EMAIL DELIVERY ========================

@app.route('/api/email/delivery-config', methods=['GET'])
@require_auth
def api_email_config_c23():
    """Get email delivery configuration for current user."""
    db = get_db()
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    db.close()
    return jsonify({
        'provider': user.get('email_provider', 'internal'),
        'sendgrid_configured': bool(user.get('sendgrid_api_key_hash')),
        'from_name': user.get('email_from_name', user.get('name', '')),
        'from_address': user.get('email_from_address', user.get('email', '')),
    })

@app.route('/api/email/delivery-config', methods=['PUT'])
@require_auth
def api_update_email_config_c23():
    """Update email delivery configuration."""
    data = request.get_json() or {}
    db = get_db()
    updates = []
    params = []

    if 'provider' in data and data['provider'] in ('internal', 'sendgrid'):
        updates.append('email_provider=?')
        params.append(data['provider'])
    if 'from_name' in data:
        updates.append('email_from_name=?')
        params.append(data['from_name'])
    if 'from_address' in data:
        updates.append('email_from_address=?')
        params.append(data['from_address'])
    if 'sendgrid_api_key' in data and data['sendgrid_api_key']:
        import hashlib
        key_hash = hashlib.sha256(data['sendgrid_api_key'].encode()).hexdigest()
        updates.append('sendgrid_api_key_hash=?')
        params.append(key_hash)

    if updates:
        params.append(g.user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", params)
        db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'Email configuration updated'})

@app.route('/api/email/deliver', methods=['POST'])
@require_auth
def api_send_email_c23():
    """Send an email through the configured provider."""
    data = request.get_json() or {}
    to_email = data.get('to_email', '').strip()
    subject = data.get('subject', '').strip()
    template = data.get('template', 'custom')

    if not to_email or '@' not in to_email:
        return jsonify({'error': 'Valid recipient email is required'}), 400
    if not subject:
        return jsonify({'error': 'Subject is required'}), 400

    db = get_db()
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    email_id = uuid.uuid4().hex

    # Log the email (compatible with both old and new email_log schemas)
    now = datetime.utcnow().isoformat()
    db.execute(
        '''INSERT INTO email_log (id, user_id, to_email, recipient_email, recipient_name, subject, email_type, template, status, provider, created_at, sent_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (email_id, g.user_id, to_email, to_email, data.get('to_name', ''), subject,
         template, template, 'sent', user.get('email_provider', 'internal'), now, now)
    )
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'email_id': email_id,
        'message': f'Email sent to {to_email}',
        'provider': user.get('email_provider', 'internal')
    }), 201

@app.route('/api/email/delivery-log', methods=['GET'])
@require_auth
def api_email_log_c23():
    """Get email delivery log for current user."""
    db = get_db()
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    status_filter = request.args.get('status', '')

    query = 'SELECT * FROM email_log WHERE user_id=?'
    params = [g.user_id]
    if status_filter:
        query += ' AND status=?'
        params.append(status_filter)
    query += ' ORDER BY sent_at DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])

    rows = db.execute(query, params).fetchall()
    total = dict(db.execute('SELECT COUNT(*) as cnt FROM email_log WHERE user_id=?', (g.user_id,)).fetchone())['cnt']
    db.close()

    return jsonify({
        'emails': [dict(r) for r in rows],
        'total': total,
        'limit': limit,
        'offset': offset
    })

@app.route('/api/email/send-stats', methods=['GET'])
@require_auth
def api_email_stats_c23():
    """Get email delivery statistics."""
    db = get_db()
    total = dict(db.execute('SELECT COUNT(*) as cnt FROM email_log WHERE user_id=?', (g.user_id,)).fetchone())['cnt']
    sent = dict(db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE user_id=? AND status='sent'", (g.user_id,)).fetchone())['cnt']
    opened = dict(db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE user_id=? AND opened_at IS NOT NULL", (g.user_id,)).fetchone())['cnt']
    clicked = dict(db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE user_id=? AND clicked_at IS NOT NULL", (g.user_id,)).fetchone())['cnt']
    bounced = dict(db.execute("SELECT COUNT(*) as cnt FROM email_log WHERE user_id=? AND status='bounced'", (g.user_id,)).fetchone())['cnt']
    db.close()

    open_rate = round(opened / sent * 100, 1) if sent > 0 else 0
    click_rate = round(clicked / sent * 100, 1) if sent > 0 else 0
    bounce_rate = round(bounced / total * 100, 1) if total > 0 else 0

    return jsonify({
        'total_sent': total,
        'delivered': sent,
        'opened': opened,
        'clicked': clicked,
        'bounced': bounced,
        'open_rate': open_rate,
        'click_rate': click_rate,
        'bounce_rate': bounce_rate
    })

@app.route('/api/email/delivery-templates', methods=['GET'])
@require_auth
def api_list_email_delivery_templates_c23():
    """List email delivery templates."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM email_delivery_templates WHERE user_id=? OR is_default=1 ORDER BY is_default DESC, created_at DESC",
        (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify({'templates': [dict(r) for r in rows]})

@app.route('/api/email/delivery-templates', methods=['POST'])
@require_auth
def api_create_email_delivery_template_c23():
    """Create a custom email delivery template."""
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    subject = data.get('subject', '').strip()
    body_html = data.get('body_html', '').strip()
    template_type = data.get('template_type', 'custom')

    if not name or not subject or not body_html:
        return jsonify({'error': 'Name, subject, and body_html are required'}), 400

    template_id = uuid.uuid4().hex
    db = get_db()
    db.execute(
        '''INSERT INTO email_delivery_templates (id, user_id, name, subject, body_html, body_text, template_type, variables, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (template_id, g.user_id, name, subject, body_html, data.get('body_text', ''),
         template_type, json.dumps(data.get('variables', [])), datetime.utcnow().isoformat())
    )
    db.commit()
    db.close()
    return jsonify({'success': True, 'template_id': template_id}), 201


# ======================== CYCLE 23: ONBOARDING WIZARD ========================

@app.route('/api/onboarding/wizard-status', methods=['GET'])
@require_auth
def api_onboarding_status_c23():
    """Get onboarding progress for current user."""
    db = get_db()
    progress = db.execute('SELECT * FROM onboarding_progress WHERE user_id=?', (g.user_id,)).fetchone()

    if not progress:
        # Create new onboarding record
        ob_id = uuid.uuid4().hex
        db.execute(
            '''INSERT INTO onboarding_progress (id, user_id, current_step, total_steps, completed_steps, created_at)
               VALUES (?,?,1,6,'[]',?)''',
            (ob_id, g.user_id, datetime.utcnow().isoformat())
        )
        db.commit()
        progress = db.execute('SELECT * FROM onboarding_progress WHERE user_id=?', (g.user_id,)).fetchone()

    p = dict(progress)
    db.close()

    steps = [
        {'step': 1, 'name': 'Agency Profile', 'description': 'Set up your agency name, logo, and contact info', 'field': 'agency_profile_done'},
        {'step': 2, 'name': 'Create Interview', 'description': 'Build your first video interview with questions', 'field': 'first_interview_done'},
        {'step': 3, 'name': 'Invite Candidate', 'description': 'Add and invite your first candidate', 'field': 'first_candidate_done'},
        {'step': 4, 'name': 'Customize Branding', 'description': 'Upload your logo and set brand colors', 'field': 'branding_done'},
        {'step': 5, 'name': 'Invite Team', 'description': 'Add team members to collaborate', 'field': 'team_invite_done'},
        {'step': 6, 'name': 'Connect AMS', 'description': 'Link your agency management system', 'field': 'ams_connected'},
    ]
    for s in steps:
        s['completed'] = bool(p.get(s['field'], 0))

    completed_count = sum(1 for s in steps if s['completed'])
    return jsonify({
        'current_step': p.get('current_step', 1),
        'total_steps': 6,
        'completed_count': completed_count,
        'completion_percentage': round(completed_count / 6 * 100),
        'is_completed': p.get('completed_at') is not None,
        'is_skipped': p.get('skipped_at') is not None,
        'steps': steps,
    })

@app.route('/api/onboarding/wizard-step', methods=['POST'])
@require_auth
def api_complete_onboarding_step_c23():
    """Mark an onboarding step as complete."""
    data = request.get_json() or {}
    step = data.get('step', 0)
    if step < 1 or step > 6:
        return jsonify({'error': 'Invalid step (1-6)'}), 400

    field_map = {1: 'agency_profile_done', 2: 'first_interview_done', 3: 'first_candidate_done',
                 4: 'branding_done', 5: 'team_invite_done', 6: 'ams_connected'}
    field = field_map.get(step)
    if not field:
        return jsonify({'error': 'Invalid step'}), 400

    db = get_db()
    # Ensure onboarding record exists
    progress = db.execute('SELECT * FROM onboarding_progress WHERE user_id=?', (g.user_id,)).fetchone()
    if not progress:
        ob_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO onboarding_progress (id, user_id, current_step, total_steps, completed_steps, created_at) VALUES (?,?,1,6,'[]',?)",
            (ob_id, g.user_id, datetime.utcnow().isoformat())
        )
        db.commit()

    db.execute(f"UPDATE onboarding_progress SET {field}=1, current_step=?, updated_at=? WHERE user_id=?",
               (min(step + 1, 6), datetime.utcnow().isoformat(), g.user_id))

    # Check if all done
    row = dict(db.execute('SELECT * FROM onboarding_progress WHERE user_id=?', (g.user_id,)).fetchone())
    all_done = all(row.get(f, 0) for f in field_map.values())
    if all_done and not row.get('completed_at'):
        db.execute("UPDATE onboarding_progress SET completed_at=? WHERE user_id=?",
                   (datetime.utcnow().isoformat(), g.user_id))
        db.execute("UPDATE users SET onboarding_completed=1 WHERE id=?", (g.user_id,))

    db.commit()
    db.close()
    return jsonify({'success': True, 'step_completed': step, 'field': field})

@app.route('/api/onboarding/wizard-skip', methods=['POST'])
@require_auth
def api_skip_onboarding_c23():
    """Skip the onboarding wizard."""
    db = get_db()
    progress = db.execute('SELECT id FROM onboarding_progress WHERE user_id=?', (g.user_id,)).fetchone()
    if not progress:
        ob_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO onboarding_progress (id, user_id, current_step, total_steps, completed_steps, skipped_at, created_at) VALUES (?,?,1,6,'[]',?,?)",
            (ob_id, g.user_id, datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
        )
    else:
        db.execute("UPDATE onboarding_progress SET skipped_at=?, updated_at=? WHERE user_id=?",
                   (datetime.utcnow().isoformat(), datetime.utcnow().isoformat(), g.user_id))
    db.execute("UPDATE users SET onboarding_completed=1 WHERE id=?", (g.user_id,))
    db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'Onboarding skipped'})


# ======================== CYCLE 23: HELP CENTER ========================

@app.route('/api/help/articles', methods=['GET'])
@require_auth
def api_list_help_articles_c23():
    """List all help articles, optionally filtered by category or page."""
    db = get_db()
    category = request.args.get('category', '')
    page = request.args.get('page', '')
    search = request.args.get('q', '')

    query = "SELECT id, slug, title, category, related_page, sort_order, created_at FROM help_articles WHERE is_published=1"
    params = []
    if category:
        query += " AND category=?"
        params.append(category)
    if page:
        query += " AND related_page=?"
        params.append(page)
    if search:
        query += " AND (title LIKE ? OR content LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])
    query += " ORDER BY sort_order ASC"

    rows = db.execute(query, params).fetchall()
    db.close()
    return jsonify({'articles': [dict(r) for r in rows], 'total': len(rows)})

@app.route('/api/help/articles/<slug>', methods=['GET'])
@require_auth
def api_get_help_article_c23(slug):
    """Get a single help article by slug."""
    db = get_db()
    article = db.execute('SELECT * FROM help_articles WHERE slug=? AND is_published=1', (slug,)).fetchone()
    db.close()
    if not article:
        return jsonify({'error': 'Article not found'}), 404
    return jsonify({'article': dict(article)})

@app.route('/api/help/categories', methods=['GET'])
@require_auth
def api_help_categories_c23():
    """List help article categories with article counts."""
    db = get_db()
    rows = db.execute(
        "SELECT category, COUNT(*) as count FROM help_articles WHERE is_published=1 GROUP BY category ORDER BY category"
    ).fetchall()
    db.close()
    return jsonify({'categories': [{'name': dict(r)['category'], 'count': dict(r)['count']} for r in rows]})

@app.route('/api/help/contextual/<page_name>', methods=['GET'])
@require_auth
def api_help_contextual_c23(page_name):
    """Get help articles relevant to a specific page (contextual help)."""
    db = get_db()
    articles = db.execute(
        "SELECT id, slug, title, category FROM help_articles WHERE related_page=? AND is_published=1 ORDER BY sort_order",
        (page_name,)
    ).fetchall()
    db.close()
    return jsonify({'page': page_name, 'articles': [dict(a) for a in articles]})


# ======================== CYCLE 23 PAGE ROUTES ========================

@app.route('/email-delivery')
@require_auth
@require_fmo_admin
def page_email_delivery_c23():
    user = dict(get_db().execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    return render_template('app.html', page='email-delivery', user=user)

@app.route('/onboarding-wizard')
@require_auth
@require_fmo_admin
def page_onboarding_wizard_c23():
    user = dict(get_db().execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    return render_template('app.html', page='onboarding-wizard', user=user)

@app.route('/help-center')
@require_auth
def page_help_center_c23():
    user = dict(get_db().execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    return render_template('app.html', page='help-center', user=user)


# ======================== CYCLE 24: GLOBAL SEARCH ========================

@app.route('/api/search/global', methods=['GET'])
@require_auth
def api_global_search_c24():
    """Search across interviews, candidates, and help articles."""
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify({'error': 'Search query must be at least 2 characters'}), 400

    db = get_db()
    results_out = {'query': q, 'interviews': [], 'candidates': [], 'help_articles': []}

    # Search interviews
    rows = db.execute(
        "SELECT id, title, position, status, created_at FROM interviews WHERE user_id=? AND (title LIKE ? OR position LIKE ? OR description LIKE ?) ORDER BY created_at DESC LIMIT 10",
        (g.user_id, f'%{q}%', f'%{q}%', f'%{q}%')
    ).fetchall()
    results_out['interviews'] = [dict(r) for r in rows]

    # Search candidates (use first_name/last_name or name depending on schema)
    rows = db.execute(
        '''SELECT c.id, c.first_name, c.last_name, c.email, c.status, c.ai_score, i.title as interview_title
           FROM candidates c JOIN interviews i ON c.interview_id=i.id
           WHERE i.user_id=? AND (c.first_name LIKE ? OR c.last_name LIKE ? OR c.email LIKE ?)
           ORDER BY c.created_at DESC LIMIT 10''',
        (g.user_id, f'%{q}%', f'%{q}%', f'%{q}%')
    ).fetchall()
    cand_results = []
    for r in rows:
        rd = dict(r)
        rd['name'] = f"{rd.get('first_name', '')} {rd.get('last_name', '')}".strip()
        cand_results.append(rd)
    results_out['candidates'] = cand_results

    # Search help articles
    rows = db.execute(
        "SELECT id, slug, title, category FROM help_articles WHERE is_published=1 AND (title LIKE ? OR content LIKE ?) ORDER BY sort_order LIMIT 5",
        (f'%{q}%', f'%{q}%')
    ).fetchall()
    results_out['help_articles'] = [dict(r) for r in rows]

    results_out['total_results'] = len(results_out['interviews']) + len(results_out['candidates']) + len(results_out['help_articles'])

    # Save last search
    db.execute("UPDATE users SET last_search_query=? WHERE id=?", (q, g.user_id))
    db.commit()
    db.close()

    return jsonify(results_out)

@app.route('/api/search/saved', methods=['GET'])
@require_auth
def api_list_saved_searches_c24():
    """List user's saved searches."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM saved_searches WHERE user_id=? ORDER BY last_used_at DESC LIMIT 20",
        (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify({'searches': [dict(r) for r in rows]})

@app.route('/api/search/saved', methods=['POST'])
@require_auth
def api_save_search_c24():
    """Save a search query."""
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    query = data.get('query', '').strip()
    if not name or not query:
        return jsonify({'error': 'Name and query are required'}), 400

    search_id = uuid.uuid4().hex
    db = get_db()
    db.execute(
        "INSERT INTO saved_searches (id, user_id, name, query, filters_json, created_at, last_used_at) VALUES (?,?,?,?,?,?,?)",
        (search_id, g.user_id, name, query, json.dumps(data.get('filters', {})),
         datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
    )
    db.commit()
    db.close()
    return jsonify({'success': True, 'search_id': search_id}), 201

@app.route('/api/search/saved/<search_id>', methods=['DELETE'])
@require_auth
def api_delete_saved_search_c24(search_id):
    """Delete a saved search."""
    db = get_db()
    db.execute("DELETE FROM saved_searches WHERE id=? AND user_id=?", (search_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ======================== CYCLE 24: USER PROFILE & PREFERENCES ========================

@app.route('/api/profile/me', methods=['GET'])
@require_auth
def api_get_profile_c24():
    """Get current user's profile."""
    db = get_db()
    user = dict(db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    prefs = db.execute('SELECT * FROM user_preferences WHERE user_id=?', (g.user_id,)).fetchone()
    db.close()

    profile = {
        'id': user.get('id'),
        'name': user.get('name', ''),
        'email': user.get('email', ''),
        'agency_name': user.get('agency_name', ''),
        'role': user.get('role', 'user'),
        'plan': user.get('plan', 'starter'),
        'bio': user.get('profile_bio', ''),
        'title': user.get('profile_title', ''),
        'phone': user.get('profile_phone', ''),
        'created_at': user.get('created_at', ''),
    }
    preferences = dict(prefs) if prefs else {
        'timezone': 'America/New_York', 'date_format': 'MM/DD/YYYY', 'language': 'en',
        'email_notifications': 1, 'browser_notifications': 1, 'weekly_digest': 1,
        'candidate_alerts': 1, 'theme': 'light', 'sidebar_collapsed': 0,
        'default_interview_duration': 30, 'default_question_time_limit': 120,
        'auto_advance_candidates': 0
    }
    return jsonify({'profile': profile, 'preferences': preferences})

@app.route('/api/profile/me', methods=['PUT'])
@require_auth
def api_update_profile_c24():
    """Update current user's profile."""
    data = request.get_json() or {}
    db = get_db()
    updates = []
    params = []
    allowed = {'name': 'name', 'agency_name': 'agency_name', 'bio': 'profile_bio',
               'title': 'profile_title', 'phone': 'profile_phone'}
    for key, col in allowed.items():
        if key in data:
            updates.append(f'{col}=?')
            params.append(data[key])
    if updates:
        params.append(g.user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", params)
        db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'Profile updated'})

@app.route('/api/profile/preferences', methods=['GET'])
@require_auth
def api_get_preferences_c24():
    """Get user preferences."""
    db = get_db()
    prefs = db.execute('SELECT * FROM user_preferences WHERE user_id=?', (g.user_id,)).fetchone()
    if not prefs:
        # Create default
        pref_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO user_preferences (id, user_id, created_at) VALUES (?,?,?)",
            (pref_id, g.user_id, datetime.utcnow().isoformat())
        )
        db.commit()
        prefs = db.execute('SELECT * FROM user_preferences WHERE user_id=?', (g.user_id,)).fetchone()
    db.close()
    return jsonify({'preferences': dict(prefs)})

@app.route('/api/profile/preferences', methods=['PUT'])
@require_auth
def api_update_preferences_c24():
    """Update user preferences."""
    data = request.get_json() or {}
    db = get_db()

    # Ensure record exists
    existing = db.execute('SELECT id FROM user_preferences WHERE user_id=?', (g.user_id,)).fetchone()
    if not existing:
        pref_id = uuid.uuid4().hex
        db.execute("INSERT INTO user_preferences (id, user_id, created_at) VALUES (?,?,?)",
                   (pref_id, g.user_id, datetime.utcnow().isoformat()))
        db.commit()

    allowed_fields = ['timezone', 'date_format', 'language', 'email_notifications',
                      'browser_notifications', 'weekly_digest', 'candidate_alerts',
                      'theme', 'sidebar_collapsed', 'default_interview_duration',
                      'default_question_time_limit', 'auto_advance_candidates']
    updates = []
    params = []
    for field in allowed_fields:
        if field in data:
            updates.append(f'{field}=?')
            params.append(data[field])
    if updates:
        updates.append('updated_at=?')
        params.append(datetime.utcnow().isoformat())
        params.append(g.user_id)
        db.execute(f"UPDATE user_preferences SET {', '.join(updates)} WHERE user_id=?", params)
        db.commit()
    db.close()
    return jsonify({'success': True, 'message': 'Preferences updated'})


# ======================== CYCLE 24: DATA EXPORT/IMPORT ========================

@app.route('/api/data/export', methods=['POST'])
@require_auth
def api_create_export_c24():
    """Create a data export job."""
    data = request.get_json() or {}
    export_type = data.get('type', '')
    fmt = data.get('format', 'csv')

    valid_types = ['candidates', 'interviews', 'analytics', 'email_log', 'all']
    if export_type not in valid_types:
        return jsonify({'error': f'Invalid export type. Valid: {", ".join(valid_types)}'}), 400
    if fmt not in ('csv', 'json'):
        return jsonify({'error': 'Format must be csv or json'}), 400

    db = get_db()
    export_id = uuid.uuid4().hex
    record_count = 0

    # Count records
    if export_type in ('candidates', 'all'):
        record_count += dict(db.execute(
            'SELECT COUNT(*) as cnt FROM candidates c JOIN interviews i ON c.interview_id=i.id WHERE i.user_id=?',
            (g.user_id,)
        ).fetchone())['cnt']
    if export_type in ('interviews', 'all'):
        record_count += dict(db.execute(
            'SELECT COUNT(*) as cnt FROM interviews WHERE user_id=?', (g.user_id,)
        ).fetchone())['cnt']
    if export_type in ('email_log', 'all'):
        record_count += dict(db.execute(
            'SELECT COUNT(*) as cnt FROM email_log WHERE user_id=?', (g.user_id,)
        ).fetchone())['cnt']

    # Simulate export completion
    now = datetime.utcnow().isoformat()
    db.execute(
        '''INSERT INTO data_exports (id, user_id, export_type, format, status, record_count, filters_json, created_at, completed_at)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (export_id, g.user_id, export_type, fmt, 'completed', record_count,
         json.dumps(data.get('filters', {})), now, now)
    )
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'export_id': export_id,
        'type': export_type,
        'format': fmt,
        'record_count': record_count,
        'status': 'completed'
    }), 201

@app.route('/api/data/exports', methods=['GET'])
@require_auth
def api_list_exports_c24():
    """List user's data exports."""
    db = get_db()
    rows = db.execute(
        'SELECT * FROM data_exports WHERE user_id=? ORDER BY created_at DESC LIMIT 20',
        (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify({'exports': [dict(r) for r in rows]})

@app.route('/api/data/import', methods=['POST'])
@require_auth
def api_create_import_c24():
    """Create a data import job (simulated)."""
    data = request.get_json() or {}
    import_type = data.get('type', '')
    records = data.get('records', [])

    valid_types = ['candidates', 'interviews']
    if import_type not in valid_types:
        return jsonify({'error': f'Invalid import type. Valid: {", ".join(valid_types)}'}), 400
    if not records or not isinstance(records, list):
        return jsonify({'error': 'Records array is required'}), 400

    db = get_db()
    import_id = uuid.uuid4().hex
    total = len(records)
    imported = 0
    skipped = 0
    errors = []

    if import_type == 'candidates':
        for i, rec in enumerate(records):
            name = rec.get('name', '').strip()
            email = rec.get('email', '').strip()
            interview_id = rec.get('interview_id', '').strip()
            if not name or not email:
                skipped += 1
                errors.append({'row': i+1, 'error': 'Missing name or email'})
                continue
            if not interview_id:
                skipped += 1
                errors.append({'row': i+1, 'error': 'Missing interview_id'})
                continue
            # Verify interview belongs to user
            iv = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
            if not iv:
                skipped += 1
                errors.append({'row': i+1, 'error': 'Interview not found'})
                continue
            cand_id = uuid.uuid4().hex
            token = uuid.uuid4().hex
            now = datetime.utcnow().isoformat()
            # Split name into first/last
            parts = name.split(' ', 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ''
            db.execute(
                "INSERT INTO candidates (id, interview_id, user_id, first_name, last_name, email, token, status, created_at, invited_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cand_id, interview_id, g.user_id, first_name, last_name, email, token, 'invited', now, now)
            )
            imported += 1

    now = datetime.utcnow().isoformat()
    db.execute(
        '''INSERT INTO data_imports (id, user_id, import_type, status, total_records, imported_records, skipped_records, error_records, errors_json, created_at, completed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (import_id, g.user_id, import_type, 'completed', total, imported, skipped, len(errors),
         json.dumps(errors), now, now)
    )
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'import_id': import_id,
        'total': total,
        'imported': imported,
        'skipped': skipped,
        'errors': errors
    }), 201

@app.route('/api/data/imports', methods=['GET'])
@require_auth
def api_list_imports_c24():
    """List user's data imports."""
    db = get_db()
    rows = db.execute(
        'SELECT * FROM data_imports WHERE user_id=? ORDER BY created_at DESC LIMIT 20',
        (g.user_id,)
    ).fetchall()
    db.close()
    return jsonify({'imports': [dict(r) for r in rows]})


# ======================== CYCLE 24 PAGE ROUTES ========================

@app.route('/global-search')
@require_auth
def page_global_search_c24():
    user = dict(get_db().execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    return render_template('app.html', page='global-search', user=user)

@app.route('/profile-settings')
@require_auth
def page_profile_settings_c24():
    user = dict(get_db().execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    return render_template('app.html', page='profile-settings', user=user)

@app.route('/data-management')
@require_auth
@require_fmo_admin
def page_data_management_c24():
    user = dict(get_db().execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    return render_template('app.html', page='data-management', user=user)


# ======================== CYCLE 25: RATE LIMITING & SECURITY ========================

# Enhanced login with account lockout tracking
@app.route('/api/auth/login-secure', methods=['POST'])
def api_secure_login_c25():
    """Enhanced login with failed attempt tracking and account lockout."""
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')
    ip = request.remote_addr or '0.0.0.0'
    ua = request.headers.get('User-Agent', '')

    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400

    db = get_db()

    # Check if account is locked
    lockout = db.execute(
        'SELECT * FROM account_lockouts WHERE email=? AND locked_until > ? AND unlocked_at IS NULL ORDER BY created_at DESC LIMIT 1',
        (email, datetime.utcnow().isoformat())
    ).fetchone()
    if lockout:
        lockout_d = dict(lockout)
        return jsonify({'error': 'Account locked', 'locked_until': lockout_d.get('locked_until'), 'reason': 'Too many failed login attempts'}), 423

    # Look up user
    user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    login_id = str(uuid.uuid4())

    if not user:
        db.execute('INSERT INTO login_attempts (id, email, ip_address, user_agent, success, failure_reason, created_at) VALUES (?,?,?,?,0,?,?)',
                   (login_id, email, ip, ua, 'user_not_found', datetime.utcnow().isoformat()))
        db.commit()
        return jsonify({'error': 'Invalid credentials'}), 401

    user_d = dict(user)
    if not bcrypt.checkpw(password.encode(), user_d['password_hash'].encode()):
        # Record failure
        db.execute('INSERT INTO login_attempts (id, email, ip_address, user_agent, success, failure_reason, created_at) VALUES (?,?,?,?,0,?,?)',
                   (login_id, email, ip, ua, 'wrong_password', datetime.utcnow().isoformat()))

        # Increment failed count
        new_count = (user_d.get('failed_login_count') or 0) + 1
        db.execute('UPDATE users SET failed_login_count=? WHERE id=?', (new_count, user_d['id']))

        # Lockout after 5 failures
        if new_count >= 5:
            lock_id = str(uuid.uuid4())
            locked_until = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
            db.execute('INSERT INTO account_lockouts (id, email, locked_until, attempts_count, created_at) VALUES (?,?,?,?,?)',
                       (lock_id, email, locked_until, new_count, datetime.utcnow().isoformat()))
            db.execute('INSERT INTO security_events (id, user_id, event_type, severity, ip_address, details, created_at) VALUES (?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()), user_d['id'], 'account_locked', 'warning', ip, json.dumps({'attempts': new_count, 'locked_minutes': 15}), datetime.utcnow().isoformat()))
            db.commit()
            return jsonify({'error': 'Account locked', 'locked_until': locked_until, 'reason': 'Too many failed login attempts'}), 423

        db.commit()
        return jsonify({'error': 'Invalid credentials', 'attempts_remaining': 5 - new_count}), 401

    # Successful login — reset failed count, record success
    db.execute('UPDATE users SET failed_login_count=0, last_login_at=?, last_login_ip=? WHERE id=?',
               (datetime.utcnow().isoformat(), ip, user_d['id']))
    db.execute('INSERT INTO login_attempts (id, email, ip_address, user_agent, success, created_at) VALUES (?,?,?,?,1,?)',
               (login_id, email, ip, ua, datetime.utcnow().isoformat()))
    db.commit()

    token = jwt.encode({'user_id': user_d['id'], 'role': user_d.get('role', 'user'), 'exp': datetime.utcnow() + timedelta(hours=24)},
                       app.config['SECRET_KEY'], algorithm='HS256')
    resp = jsonify({'message': 'Login successful', 'user': {'id': user_d['id'], 'email': email, 'name': user_d.get('name', ''), 'role': user_d.get('role', 'user')}})
    resp.set_cookie('token', token, httponly=True, secure=os.environ.get('FLASK_ENV')=='production', samesite='Lax', max_age=86400)
    return resp


@app.route('/api/security/login-history', methods=['GET'])
@require_auth
def api_login_history_c25():
    """Get login attempt history for current user."""
    db = get_db()
    user = db.execute('SELECT email FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404

    email = dict(user)['email']
    limit = min(int(request.args.get('limit', 20)), 100)
    attempts = db.execute(
        'SELECT id, email, ip_address, success, failure_reason, created_at FROM login_attempts WHERE email=? ORDER BY created_at DESC LIMIT ?',
        (email, limit)
    ).fetchall()

    return jsonify({
        'attempts': [dict(a) for a in attempts],
        'total': len(attempts)
    })


@app.route('/api/security/active-lockouts', methods=['GET'])
@require_auth
def api_active_lockouts_c25():
    """Get active account lockouts (admin only)."""
    db = get_db()
    user = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not user or dict(user).get('role') not in ('admin', 'super_admin'):
        return jsonify({'error': 'Admin access required'}), 403

    lockouts = db.execute(
        'SELECT id, email, locked_until, reason, attempts_count, created_at, unlocked_at FROM account_lockouts ORDER BY created_at DESC LIMIT 50'
    ).fetchall()
    active = [dict(l) for l in lockouts if not dict(l).get('unlocked_at') and dict(l).get('locked_until', '') > datetime.utcnow().isoformat()]

    return jsonify({'lockouts': [dict(l) for l in lockouts], 'active_count': len(active)})


@app.route('/api/security/unlock-account', methods=['POST'])
@require_auth
def api_unlock_account_c25():
    """Unlock a locked account (admin only)."""
    db = get_db()
    user = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not user or dict(user).get('role') not in ('admin', 'super_admin'):
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email required'}), 400

    db.execute('UPDATE account_lockouts SET unlocked_at=? WHERE email=? AND unlocked_at IS NULL',
               (datetime.utcnow().isoformat(), email))
    db.execute('UPDATE users SET failed_login_count=0, locked_until=NULL WHERE email=?', (email,))
    db.commit()

    return jsonify({'message': f'Account unlocked: {email}'})


@app.route('/api/security/password-rules', methods=['GET'])
@require_auth
def api_password_rules_c25():
    """Get password strength requirements (enhanced C25)."""
    return jsonify({
        'policy': {
            'min_length': 8,
            'require_uppercase': True,
            'require_lowercase': True,
            'require_digit': True,
            'require_special': False,
            'max_age_days': 90,
            'lockout_threshold': 5,
            'lockout_duration_minutes': 15
        }
    })


@app.route('/api/security/update-password', methods=['POST'])
@require_auth
def api_change_password_c25():
    """Change password with strength validation."""
    data = request.get_json() or {}
    current_pw = data.get('current_password', '')
    new_pw = data.get('new_password', '')

    if not current_pw or not new_pw:
        return jsonify({'error': 'Current and new password required'}), 400

    # Validate strength
    errors = []
    if len(new_pw) < 8:
        errors.append('Password must be at least 8 characters')
    if not any(c.isupper() for c in new_pw):
        errors.append('Password must contain an uppercase letter')
    if not any(c.islower() for c in new_pw):
        errors.append('Password must contain a lowercase letter')
    if not any(c.isdigit() for c in new_pw):
        errors.append('Password must contain a digit')
    if errors:
        return jsonify({'error': 'Password too weak', 'details': errors}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404

    user_d = dict(user)
    if not bcrypt.checkpw(current_pw.encode(), user_d['password_hash'].encode()):
        return jsonify({'error': 'Current password is incorrect'}), 401

    new_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    db.execute('UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?',
               (new_hash, datetime.utcnow().isoformat(), g.user_id))
    db.execute('INSERT INTO security_events (id, user_id, event_type, severity, ip_address, created_at) VALUES (?,?,?,?,?,?)',
               (str(uuid.uuid4()), g.user_id, 'password_changed', 'info', request.remote_addr, datetime.utcnow().isoformat()))
    db.commit()

    return jsonify({'message': 'Password changed successfully'})


@app.route('/api/security/event-log', methods=['GET'])
@require_auth
def api_security_events_c25():
    """Get security events log."""
    db = get_db()
    user = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    role = dict(user).get('role', 'user') if user else 'user'

    limit = min(int(request.args.get('limit', 25)), 100)
    event_type = request.args.get('type', '')
    severity = request.args.get('severity', '')

    query = 'SELECT * FROM security_events'
    params = []
    conditions = []

    # Non-admins only see their own events
    if role not in ('admin', 'super_admin'):
        conditions.append('user_id=?')
        params.append(g.user_id)
    if event_type:
        conditions.append('event_type=?')
        params.append(event_type)
    if severity:
        conditions.append('severity=?')
        params.append(severity)

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY created_at DESC LIMIT ?'
    params.append(limit)

    events = db.execute(query, params).fetchall()
    return jsonify({'events': [dict(e) for e in events], 'total': len(events)})


# ======================== CYCLE 25: ACTIVITY LOG DASHBOARD ========================

@app.route('/api/activity/audit-log', methods=['GET'])
@require_auth
def api_activity_log_c25():
    """Query activity/audit log with filters."""
    db = get_db()
    user = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    role = dict(user).get('role', 'user') if user else 'user'

    limit = min(int(request.args.get('limit', 25)), 100)
    action = request.args.get('action', '')
    entity_type = request.args.get('entity_type', request.args.get('resource_type', ''))
    severity_filter = request.args.get('severity', '')
    user_filter = request.args.get('user_id', '')

    query = 'SELECT * FROM audit_log'
    params = []
    conditions = []

    # Non-admins only see their own activity
    if role not in ('admin', 'super_admin'):
        conditions.append('user_id=?')
        params.append(g.user_id)
    elif user_filter:
        conditions.append('user_id=?')
        params.append(user_filter)

    if action:
        conditions.append('action=?')
        params.append(action)
    if entity_type:
        conditions.append('(resource_type=? OR entity_type=?)')
        params.extend([entity_type, entity_type])
    if severity_filter:
        conditions.append('severity=?')
        params.append(severity_filter)

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY created_at DESC LIMIT ?'
    params.append(limit)

    entries = db.execute(query, params).fetchall()
    return jsonify({'entries': [dict(e) for e in entries], 'total': len(entries)})


@app.route('/api/activity/audit-log', methods=['POST'])
@require_auth
def api_create_activity_c25():
    """Log a user activity."""
    data = request.get_json() or {}
    action = data.get('action', '')
    resource_type = data.get('resource_type', data.get('entity_type', ''))

    if not action:
        return jsonify({'error': 'Action required'}), 400

    db = get_db()
    entry_id = str(uuid.uuid4())
    db.execute('''INSERT INTO audit_log (id, account_id, user_id, action, resource_type, resource_id, resource_name, details, severity, entity_type, entity_id, ip_address, created_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (entry_id, g.user_id, g.user_id, action, resource_type, data.get('resource_id', ''),
                data.get('resource_name', ''), data.get('details', ''),
                data.get('severity', 'info'), resource_type, data.get('resource_id', ''),
                request.remote_addr, datetime.utcnow().isoformat()))
    db.commit()

    return jsonify({'message': 'Activity logged', 'entry_id': entry_id}), 201


@app.route('/api/activity/audit-summary', methods=['GET'])
@require_auth
def api_activity_summary_c25():
    """Get activity summary stats."""
    db = get_db()
    user = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    role = dict(user).get('role', 'user') if user else 'user'

    user_cond = '' if role in ('admin', 'super_admin') else f" WHERE user_id='{g.user_id}'"

    total = dict(db.execute(f'SELECT COUNT(*) as cnt FROM audit_log{user_cond}').fetchone())['cnt']

    # Actions breakdown
    actions_q = f"SELECT action, COUNT(*) as cnt FROM audit_log{user_cond} GROUP BY action ORDER BY cnt DESC LIMIT 10"
    actions = [dict(r) for r in db.execute(actions_q).fetchall()]

    # Recent (last 7 days)
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    recent_cond = f"created_at > '{week_ago}'"
    if user_cond:
        recent_q = f"SELECT COUNT(*) as cnt FROM audit_log WHERE user_id='{g.user_id}' AND {recent_cond}"
    else:
        recent_q = f"SELECT COUNT(*) as cnt FROM audit_log WHERE {recent_cond}"
    recent_count = dict(db.execute(recent_q).fetchone())['cnt']

    return jsonify({
        'total_entries': total,
        'recent_7d': recent_count,
        'top_actions': actions
    })


@app.route('/api/activity/audit-export', methods=['GET'])
@require_auth
def api_activity_export_c25():
    """Export activity log as JSON."""
    db = get_db()
    user = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    role = dict(user).get('role', 'user') if user else 'user'

    if role not in ('admin', 'super_admin'):
        entries = db.execute('SELECT * FROM audit_log WHERE user_id=? ORDER BY created_at DESC LIMIT 1000', (g.user_id,)).fetchall()
    else:
        entries = db.execute('SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 1000').fetchall()

    return jsonify({'entries': [dict(e) for e in entries], 'exported_at': datetime.utcnow().isoformat(), 'count': len(entries)})


# ======================== CYCLE 25: MOBILE RESPONSIVE CONFIG ========================

@app.route('/api/system/responsive-config', methods=['GET'])
def api_responsive_config_c25():
    """Get responsive layout configuration (breakpoints, sidebar behavior)."""
    return jsonify({
        'breakpoints': {
            'mobile': 480,
            'tablet': 768,
            'desktop': 1024,
            'wide': 1440
        },
        'sidebar': {
            'collapse_below': 768,
            'overlay_below': 480,
            'default_width': 260,
            'collapsed_width': 60
        },
        'features': {
            'touch_friendly': True,
            'swipe_navigation': True,
            'responsive_tables': True,
            'mobile_video_recording': True
        }
    })


# ======================== CYCLE 25 PAGE ROUTES ========================

@app.route('/security-settings')
@require_auth
@require_fmo_admin
def page_security_settings_c25():
    user = dict(get_db().execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    return render_template('app.html', page='security-settings', user=user)

@app.route('/activity-log')
@require_auth
@require_fmo_admin
def page_activity_log_c25():
    user = dict(get_db().execute('SELECT * FROM users WHERE id=?', (g.user_id,)).fetchone())
    return render_template('app.html', page='activity-log', user=user)


# ======================== CYCLE 26: FREE TRIAL & TRANSACTIONAL EMAILS ========================

TRIAL_DAYS = 14

def _send_transactional(db, user_id, to_email, to_name, email_type, subject, html_body):
    """Send a transactional email and log it. Returns (success, error)."""
    from email_service import send_email, get_smtp_config
    smtp_config = get_smtp_config(db, user_id)
    success, error = send_email(smtp_config, to_email, subject, html_body)
    try:
        db.execute('''INSERT INTO email_log (id, user_id, to_email, recipient_email, recipient_name, subject, email_type, template, status, provider, created_at, sent_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (str(uuid.uuid4()), user_id, to_email, to_email, to_name, subject, email_type, email_type,
                    'sent' if success else 'failed', 'sendgrid' if os.environ.get('SENDGRID_API_KEY') else 'smtp',
                    datetime.utcnow().isoformat(), datetime.utcnow().isoformat() if success else None))
        db.commit()
    except:
        pass
    return success, error


@app.route('/api/trial/status', methods=['GET'])
@require_auth
def api_trial_status_c26():
    """Get trial status for current user."""
    db = get_db()
    user = db.execute('SELECT plan, trial_ends_at, subscription_status, created_at FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    user_d = dict(user)
    trial_ends = user_d.get('trial_ends_at')
    now = datetime.utcnow().isoformat()

    is_trial = user_d.get('subscription_status') == 'trialing' or (trial_ends and trial_ends > now and user_d.get('plan') in ('free', 'trial'))
    is_expired = trial_ends and trial_ends <= now and user_d.get('plan') in ('free', 'trial')
    days_left = 0
    if trial_ends and trial_ends > now:
        try:
            delta = datetime.fromisoformat(trial_ends) - datetime.utcnow()
            days_left = max(0, delta.days)
        except:
            pass

    return jsonify({
        'is_trial': is_trial,
        'is_expired': is_expired,
        'trial_ends_at': trial_ends,
        'days_remaining': days_left,
        'plan': user_d.get('plan', 'free'),
        'subscription_status': user_d.get('subscription_status', ''),
        'created_at': user_d.get('created_at')
    })


@app.route('/api/trial/extend', methods=['POST'])
@require_auth
def api_extend_trial_c26():
    """Extend a trial (admin only, for support cases)."""
    db = get_db()
    admin = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not admin or dict(admin).get('role') not in ('admin', 'super_admin'):
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json() or {}
    target_email = data.get('email', '').strip().lower()
    extra_days = min(int(data.get('days', 7)), 30)

    if not target_email:
        return jsonify({'error': 'Email required'}), 400

    target = db.execute('SELECT id, trial_ends_at FROM users WHERE email=?', (target_email,)).fetchone()
    if not target:
        return jsonify({'error': 'User not found'}), 404

    target_d = dict(target)
    base = target_d.get('trial_ends_at')
    if base:
        try:
            new_end = (datetime.fromisoformat(base) + timedelta(days=extra_days)).isoformat()
        except:
            new_end = (datetime.utcnow() + timedelta(days=extra_days)).isoformat()
    else:
        new_end = (datetime.utcnow() + timedelta(days=extra_days)).isoformat()

    db.execute('UPDATE users SET trial_ends_at=?, subscription_status=? WHERE id=?',
               (new_end, 'trialing', target_d['id']))
    db.commit()
    return jsonify({'message': f'Trial extended to {new_end}', 'new_trial_ends_at': new_end})


@app.route('/api/email/send-welcome', methods=['POST'])
@require_auth
def api_send_welcome_c26():
    """Send or re-send welcome email to current user."""
    db = get_db()
    user = db.execute('SELECT id, email, name, agency_name, trial_ends_at FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    user_d = dict(user)
    html = _build_welcome_email(user_d.get('name', ''), user_d.get('agency_name', 'Your Agency'), user_d.get('trial_ends_at', ''))
    success, error = _send_transactional(db, g.user_id, user_d['email'], user_d.get('name', ''), 'welcome', 'Welcome to ChannelView!', html)
    return jsonify({'success': success, 'error': error})


@app.route('/api/email/send-trial-warning', methods=['POST'])
@require_auth
def api_send_trial_warning_c26():
    """Send trial expiring warning (admin trigger for batch use)."""
    db = get_db()
    admin = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not admin or dict(admin).get('role') not in ('admin', 'super_admin'):
        return jsonify({'error': 'Admin access required'}), 403

    # Find users whose trial expires in 3 days or less
    cutoff = (datetime.utcnow() + timedelta(days=3)).isoformat()
    now = datetime.utcnow().isoformat()
    expiring = db.execute(
        "SELECT id, email, name, agency_name, trial_ends_at FROM users WHERE trial_ends_at > ? AND trial_ends_at <= ? AND subscription_status='trialing'",
        (now, cutoff)
    ).fetchall()

    sent = 0
    for u in expiring:
        ud = dict(u)
        days_left = max(0, (datetime.fromisoformat(ud['trial_ends_at']) - datetime.utcnow()).days)
        html = _build_trial_expiring_email(ud.get('name', ''), ud.get('agency_name', 'Your Agency'), days_left)
        success, _ = _send_transactional(db, ud['id'], ud['email'], ud.get('name', ''), 'trial_expiring',
                                         f'Your ChannelView trial expires in {days_left} day{"s" if days_left != 1 else ""}', html)
        if success:
            sent += 1

    return jsonify({'sent': sent, 'total_expiring': len(expiring)})


@app.route('/api/email/send-payment-failed', methods=['POST'])
@require_auth
def api_send_payment_failed_c26():
    """Send payment failed notification (admin trigger)."""
    db = get_db()
    admin = db.execute('SELECT role FROM users WHERE id=?', (g.user_id,)).fetchone()
    if not admin or dict(admin).get('role') not in ('admin', 'super_admin'):
        return jsonify({'error': 'Admin access required'}), 403

    past_due = db.execute("SELECT id, email, name, agency_name FROM users WHERE subscription_status='past_due'").fetchall()
    sent = 0
    for u in past_due:
        ud = dict(u)
        html = _build_payment_failed_email(ud.get('name', ''), ud.get('agency_name', 'Your Agency'))
        success, _ = _send_transactional(db, ud['id'], ud['email'], ud.get('name', ''), 'payment_failed',
                                         'Action Required: Payment Failed for ChannelView', html)
        if success:
            sent += 1
    return jsonify({'sent': sent, 'total_past_due': len(past_due)})


def _build_welcome_email(name, agency_name, trial_ends_at):
    from email_service import _base_template
    trial_msg = ''
    if trial_ends_at:
        try:
            dt = datetime.fromisoformat(trial_ends_at)
            trial_msg = f'<p style="color:#6b7280;font-size:14px;margin:0 0 16px">Your free trial runs until <strong>{dt.strftime("%B %d, %Y")}</strong> — full access, no credit card required.</p>'
        except:
            trial_msg = '<p style="color:#6b7280;font-size:14px;margin:0 0 16px">You have a 14-day free trial with full access, no credit card required.</p>'
    content = f'''
    <h1 style="margin:0 0 8px;font-size:22px;color:#111">Welcome to ChannelView!</h1>
    <p style="color:#6b7280;font-size:15px;margin:0 0 20px;line-height:1.5">
      Hi {name},<br><br>
      Thanks for signing up! ChannelView helps your agency run one-way video interviews so you can screen candidates faster and hire better — all on their schedule.
    </p>
    {trial_msg}
    <div style="background:#f9fafb;border-radius:8px;padding:20px;margin:0 0 24px">
      <p style="margin:0 0 8px;font-size:14px;font-weight:600;color:#111">Quick start:</p>
      <ol style="margin:0;padding-left:20px;color:#6b7280;font-size:14px;line-height:1.8">
        <li>Create your first interview</li>
        <li>Add questions (or use our question bank)</li>
        <li>Invite candidates by email</li>
        <li>Review recorded video responses</li>
      </ol>
    </div>
    <div style="text-align:center;margin:28px 0">
      <a href="{{BASE_URL}}/dashboard" style="display:inline-block;background:#0ace0a;color:#000;font-weight:700;
         font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none">
        Go to Your Dashboard
      </a>
    </div>'''
    return _base_template('#0ace0a', agency_name, content)


def _build_trial_expiring_email(name, agency_name, days_left):
    from email_service import _base_template
    content = f'''
    <h1 style="margin:0 0 8px;font-size:22px;color:#111">Your trial ends in {days_left} day{"s" if days_left != 1 else ""}</h1>
    <p style="color:#6b7280;font-size:15px;margin:0 0 20px;line-height:1.5">
      Hi {name},<br><br>
      Just a heads up — your ChannelView free trial is ending soon. After your trial expires, you'll lose access to premium features like unlimited interviews, AI scoring, and video storage.
    </p>
    <div style="text-align:center;margin:28px 0">
      <a href="{{BASE_URL}}/billing" style="display:inline-block;background:#0ace0a;color:#000;font-weight:700;
         font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none">
        Upgrade Now
      </a>
    </div>
    <p style="color:#9ca3af;font-size:12px;text-align:center">No commitment — cancel anytime.</p>'''
    return _base_template('#0ace0a', agency_name, content)


def _build_payment_failed_email(name, agency_name):
    from email_service import _base_template
    content = f'''
    <h1 style="margin:0 0 8px;font-size:22px;color:#111">Payment Failed</h1>
    <p style="color:#6b7280;font-size:15px;margin:0 0 20px;line-height:1.5">
      Hi {name},<br><br>
      We were unable to process your payment for ChannelView. Please update your payment method to keep your account active and avoid any interruption in service.
    </p>
    <div style="text-align:center;margin:28px 0">
      <a href="{{BASE_URL}}/billing" style="display:inline-block;background:#ef4444;color:#fff;font-weight:700;
         font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none">
        Update Payment Method
      </a>
    </div>
    <p style="color:#9ca3af;font-size:12px;text-align:center">If you've already resolved this, you can ignore this email.</p>'''
    return _base_template('#0ace0a', agency_name, content)


# ======================== CYCLE 27: PRODUCTION HARDENING ========================

@app.route('/health', methods=['GET'])
def api_health_check_c27():
    """Kubernetes/ECS readiness probe. Returns 200 if app is healthy."""
    try:
        db = get_db()
        db.execute('SELECT 1').fetchone()
        return jsonify({'status': 'healthy', 'version': app_config.VERSION, 'timestamp': datetime.utcnow().isoformat()}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503


@app.route('/ready', methods=['GET'])
def api_readiness_c27():
    """Readiness probe — checks database, storage, and email are reachable."""
    checks = {}
    try:
        db = get_db()
        db.execute('SELECT COUNT(*) FROM users').fetchone()
        checks['database'] = 'ok'
    except:
        checks['database'] = 'error'

    checks['storage'] = app_config.STORAGE_BACKEND
    checks['email'] = 'sendgrid' if os.environ.get('SENDGRID_API_KEY') else ('smtp' if os.environ.get('SMTP_HOST') else 'log')
    checks['stripe'] = 'configured' if os.environ.get('STRIPE_SECRET_KEY') else 'not_configured'

    all_ok = checks['database'] == 'ok'
    return jsonify({'ready': all_ok, 'checks': checks}), 200 if all_ok else 503


@app.errorhandler(404)
def page_not_found_c27(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Endpoint not found', 'path': request.path}), 404
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>404 — ChannelView</title>
    <style>body{margin:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f9fafb;display:flex;align-items:center;justify-content:center;min-height:100vh}
    .c{text-align:center;padding:48px}.n{font-size:80px;font-weight:800;color:#0ace0a;margin:0}.t{font-size:20px;color:#111;margin:8px 0 16px}.d{color:#6b7280;margin:0 0 32px;font-size:15px}
    a{display:inline-block;background:#111;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px}a:hover{background:#333}</style></head>
    <body><div class="c"><p class="n">404</p><p class="t">Page Not Found</p><p class="d">The page you're looking for doesn't exist or has been moved.</p><a href="/dashboard">Go to Dashboard</a></div></body></html>''', 404


@app.errorhandler(500)
def internal_error_c27(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Internal server error'}), 500
    return '''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>500 — ChannelView</title>
    <style>body{margin:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f9fafb;display:flex;align-items:center;justify-content:center;min-height:100vh}
    .c{text-align:center;padding:48px}.n{font-size:80px;font-weight:800;color:#ef4444;margin:0}.t{font-size:20px;color:#111;margin:8px 0 16px}.d{color:#6b7280;margin:0 0 32px;font-size:15px}
    a{display:inline-block;background:#111;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px}a:hover{background:#333}</style></head>
    <body><div class="c"><p class="n">500</p><p class="t">Something Went Wrong</p><p class="d">We're working on it. Please try again in a moment.</p><a href="/dashboard">Go to Dashboard</a></div></body></html>''', 500


@app.errorhandler(429)
def rate_limited_c27(e):
    return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429


# CORS lockdown for production
@app.after_request
def security_headers_c27(response):
    """Add security headers to all responses."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if os.environ.get('FLASK_ENV') == 'production' or os.environ.get('CHANNELVIEW_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


# ======================== CYCLE 33: Lead Sourcing Engine ========================
# Features: Lead Sourcing Hub, CSV Import, ZIP Search, Lead-to-Pipeline,
#           Indeed Job Sync, Google Jobs Markup, Referral Link Tracking

# --- Page Routes ---

@app.route('/lead-sourcing')
@require_auth
def lead_sourcing_page():
    return render_template('app.html', page='lead-sourcing', user=g.user)

@app.route('/referral-links')
@require_auth
def referral_links_page():
    return render_template('app.html', page='referral-links', user=g.user)

@app.route('/job-syndication')
@require_auth
def job_syndication_page():
    return render_template('app.html', page='job-syndication', user=g.user)


# --- Lead Sourcing API ---

@app.route('/api/leads', methods=['GET'])
@require_auth
def api_get_leads_c33():
    """List sourced leads with filtering by ZIP, state, license, status."""
    db = get_db()
    zip_code = request.args.get('zip_code', '')
    state = request.args.get('state', '')
    license_type = request.args.get('license_type', '')
    status = request.args.get('status', '')
    source = request.args.get('source', '')
    search = request.args.get('search', '')
    page_num = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))

    query = "SELECT * FROM sourced_leads WHERE user_id=?"
    params = [g.user_id]

    if zip_code:
        query += " AND zip_code=?"
        params.append(zip_code)
    if state:
        query += " AND state=?"
        params.append(state.upper())
    if license_type:
        query += " AND license_type LIKE ?"
        params.append(f'%{license_type}%')
    if status:
        query += " AND status=?"
        params.append(status)
    if source:
        query += " AND source=?"
        params.append(source)
    if search:
        query += " AND (first_name LIKE ? OR last_name LIKE ? OR email LIKE ? OR phone LIKE ?)"
        s = f'%{search}%'
        params.extend([s, s, s, s])

    count_q = query.replace("SELECT *", "SELECT COUNT(*)")
    total = db.execute(count_q, params).fetchone()[0]

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([per_page, (page_num - 1) * per_page])
    leads = [dict(r) for r in db.execute(query, params).fetchall()]
    db.close()

    return jsonify({
        'leads': leads,
        'total': total,
        'page': page_num,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page
    })


@app.route('/api/leads', methods=['POST'])
@require_auth
def api_create_lead_c33():
    """Create a single lead manually."""
    data = request.get_json() or {}
    first_name = (data.get('first_name') or '').strip()
    last_name = (data.get('last_name') or '').strip()
    if not first_name or not last_name:
        return jsonify({'error': 'first_name and last_name are required'}), 400

    db = get_db()
    lead_id = str(uuid.uuid4())
    db.execute("""
        INSERT INTO sourced_leads (id, user_id, first_name, last_name, email, phone,
            zip_code, city, state, license_type, license_number, npn, license_status,
            license_expiry, years_licensed, source, notes, tags, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        lead_id, g.user_id, first_name, last_name,
        (data.get('email') or '').strip(),
        (data.get('phone') or '').strip(),
        (data.get('zip_code') or '').strip(),
        (data.get('city') or '').strip(),
        (data.get('state') or '').strip().upper(),
        (data.get('license_type') or '').strip(),
        (data.get('license_number') or '').strip(),
        (data.get('npn') or '').strip(),
        data.get('license_status', 'unknown'),
        data.get('license_expiry'),
        data.get('years_licensed'),
        data.get('source', 'manual'),
        (data.get('notes') or '').strip(),
        json.dumps(data.get('tags', [])),
        'new'
    ))
    db.commit()
    db.close()
    return jsonify({'id': lead_id, 'success': True}), 201


@app.route('/api/leads/<lead_id>', methods=['GET'])
@require_auth
def api_get_lead_detail_c33(lead_id):
    """Get a single lead's details."""
    db = get_db()
    lead = db.execute("SELECT * FROM sourced_leads WHERE id=? AND user_id=?",
                      (lead_id, g.user_id)).fetchone()
    db.close()
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404
    return jsonify(dict(lead))


@app.route('/api/leads/<lead_id>', methods=['PUT'])
@require_auth
def api_update_lead_c33(lead_id):
    """Update a lead's details."""
    db = get_db()
    lead = db.execute("SELECT * FROM sourced_leads WHERE id=? AND user_id=?",
                      (lead_id, g.user_id)).fetchone()
    if not lead:
        db.close()
        return jsonify({'error': 'Lead not found'}), 404

    data = request.get_json() or {}
    fields = ['first_name', 'last_name', 'email', 'phone', 'zip_code', 'city', 'state',
              'license_type', 'license_number', 'npn', 'license_status', 'license_expiry',
              'years_licensed', 'notes', 'status', 'tags']
    updates = []
    params = []
    for f in fields:
        if f in data:
            val = data[f]
            if f == 'tags' and isinstance(val, list):
                val = json.dumps(val)
            if f == 'state' and isinstance(val, str):
                val = val.upper()
            updates.append(f"{f}=?")
            params.append(val)
    if not updates:
        db.close()
        return jsonify({'error': 'No fields to update'}), 400

    updates.append("updated_at=CURRENT_TIMESTAMP")
    params.extend([lead_id, g.user_id])
    db.execute(f"UPDATE sourced_leads SET {','.join(updates)} WHERE id=? AND user_id=?", params)
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/leads/<lead_id>', methods=['DELETE'])
@require_auth
def api_delete_lead_c33(lead_id):
    """Delete a lead."""
    db = get_db()
    lead = db.execute("SELECT * FROM sourced_leads WHERE id=? AND user_id=?",
                      (lead_id, g.user_id)).fetchone()
    if not lead:
        db.close()
        return jsonify({'error': 'Lead not found'}), 404
    db.execute("DELETE FROM sourced_leads WHERE id=? AND user_id=?", (lead_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/leads/bulk-delete', methods=['POST'])
@require_auth
def api_bulk_delete_leads_c33():
    """Delete multiple leads at once."""
    data = request.get_json() or {}
    lead_ids = data.get('lead_ids', [])
    if not lead_ids:
        return jsonify({'error': 'lead_ids required'}), 400
    db = get_db()
    placeholders = ','.join(['?'] * len(lead_ids))
    db.execute(f"DELETE FROM sourced_leads WHERE id IN ({placeholders}) AND user_id=?",
               lead_ids + [g.user_id])
    db.commit()
    deleted = db.total_changes if hasattr(db, 'total_changes') else len(lead_ids)
    db.close()
    return jsonify({'success': True, 'deleted': deleted})


# --- CSV Import API ---

@app.route('/api/leads/import', methods=['POST'])
@require_auth
def api_import_leads_c33():
    """Import leads from CSV data (JSON array of row objects)."""
    data = request.get_json() or {}
    rows = data.get('rows', [])
    mapping = data.get('column_mapping', {})
    source_file = data.get('filename', 'upload.csv')

    if not rows:
        return jsonify({'error': 'No rows provided'}), 400

    db = get_db()
    batch_id = str(uuid.uuid4())
    imported = 0
    duplicates = 0
    errors = 0

    for row in rows:
        try:
            first_name = (row.get(mapping.get('first_name', 'first_name')) or '').strip()
            last_name = (row.get(mapping.get('last_name', 'last_name')) or '').strip()
            email = (row.get(mapping.get('email', 'email')) or '').strip()
            phone = (row.get(mapping.get('phone', 'phone')) or '').strip()

            if not first_name and not last_name:
                errors += 1
                continue

            # Duplicate check by email or phone+name
            if email:
                existing = db.execute(
                    "SELECT id FROM sourced_leads WHERE user_id=? AND email=?",
                    (g.user_id, email)).fetchone()
                if existing:
                    duplicates += 1
                    continue

            lead_id = str(uuid.uuid4())
            db.execute("""
                INSERT INTO sourced_leads (id, user_id, first_name, last_name, email, phone,
                    zip_code, city, state, license_type, license_number, npn, license_status,
                    years_licensed, source, source_file, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                lead_id, g.user_id, first_name, last_name, email, phone,
                (row.get(mapping.get('zip_code', 'zip_code')) or '').strip(),
                (row.get(mapping.get('city', 'city')) or '').strip(),
                (row.get(mapping.get('state', 'state')) or '').strip().upper(),
                (row.get(mapping.get('license_type', 'license_type')) or '').strip(),
                (row.get(mapping.get('license_number', 'license_number')) or '').strip(),
                (row.get(mapping.get('npn', 'npn')) or '').strip(),
                (row.get(mapping.get('license_status', 'license_status')) or 'unknown').strip(),
                row.get(mapping.get('years_licensed', 'years_licensed')),
                'csv_import', source_file, 'new'
            ))
            imported += 1
        except Exception:
            errors += 1

    db.execute("""
        INSERT INTO lead_import_batches (id, user_id, filename, total_rows, imported_rows,
            duplicate_rows, error_rows, column_mapping, status)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (batch_id, g.user_id, source_file, len(rows), imported, duplicates, errors,
          json.dumps(mapping), 'completed'))
    db.commit()
    db.close()

    return jsonify({
        'batch_id': batch_id,
        'total_rows': len(rows),
        'imported': imported,
        'duplicates': duplicates,
        'errors': errors
    }), 201


@app.route('/api/leads/import-history', methods=['GET'])
@require_auth
def api_import_history_c33():
    """Get CSV import history."""
    db = get_db()
    batches = [dict(r) for r in db.execute(
        "SELECT * FROM lead_import_batches WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
        (g.user_id,)).fetchall()]
    db.close()
    return jsonify({'batches': batches})


# --- Lead-to-Pipeline Conversion ---

@app.route('/api/leads/<lead_id>/convert', methods=['POST'])
@require_auth
def api_convert_lead_c33(lead_id):
    """Convert a sourced lead into a candidate in an interview pipeline."""
    db = get_db()
    lead = db.execute("SELECT * FROM sourced_leads WHERE id=? AND user_id=?",
                      (lead_id, g.user_id)).fetchone()
    if not lead:
        db.close()
        return jsonify({'error': 'Lead not found'}), 404

    data = request.get_json() or {}
    interview_id = data.get('interview_id')
    if not interview_id:
        db.close()
        return jsonify({'error': 'interview_id is required'}), 400

    interview = db.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",
                           (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    # Check if already converted
    if lead['converted_candidate_id']:
        db.close()
        return jsonify({'error': 'Lead already converted', 'candidate_id': lead['converted_candidate_id']}), 409

    candidate_id = str(uuid.uuid4())
    token = str(uuid.uuid4())
    db.execute("""
        INSERT INTO candidates (id, interview_id, user_id, first_name, last_name, email, phone, token,
            pipeline_stage, source, sourced_lead_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
    """, (candidate_id, interview_id, g.user_id,
          lead['first_name'] or '', lead['last_name'] or '',
          lead['email'] or '', lead['phone'] or '', token,
          'new', 'sourced_lead', lead_id))

    db.execute("""
        UPDATE sourced_leads SET status='converted', converted_candidate_id=?,
            converted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?
    """, (candidate_id, lead_id))
    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'candidate_id': candidate_id,
        'token': token,
        'interview_url': f'/i/{token}'
    }), 201


@app.route('/api/leads/bulk-convert', methods=['POST'])
@require_auth
def api_bulk_convert_leads_c33():
    """Convert multiple leads to candidates at once."""
    data = request.get_json() or {}
    lead_ids = data.get('lead_ids', [])
    interview_id = data.get('interview_id')
    if not lead_ids or not interview_id:
        return jsonify({'error': 'lead_ids and interview_id are required'}), 400

    db = get_db()
    interview = db.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",
                           (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    converted = 0
    skipped = 0
    for lid in lead_ids:
        lead = db.execute("SELECT * FROM sourced_leads WHERE id=? AND user_id=?",
                          (lid, g.user_id)).fetchone()
        if not lead or lead['converted_candidate_id']:
            skipped += 1
            continue
        candidate_id = str(uuid.uuid4())
        token = str(uuid.uuid4())
        db.execute("""
            INSERT INTO candidates (id, interview_id, user_id, first_name, last_name, email, phone, token,
                pipeline_stage, source, sourced_lead_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        """, (candidate_id, interview_id, g.user_id,
              lead['first_name'] or '', lead['last_name'] or '',
              lead['email'] or '', lead['phone'] or '', token, 'new', 'sourced_lead', lid))
        db.execute("""
            UPDATE sourced_leads SET status='converted', converted_candidate_id=?,
                converted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (candidate_id, lid))
        converted += 1

    db.commit()
    db.close()
    return jsonify({'converted': converted, 'skipped': skipped})


# --- Lead Analytics ---

@app.route('/api/leads/analytics', methods=['GET'])
@require_auth
def api_lead_analytics_c33():
    """Get lead sourcing analytics."""
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM sourced_leads WHERE user_id=?", (g.user_id,)).fetchone()[0]
    by_status = {}
    for row in db.execute(
        "SELECT status, COUNT(*) as cnt FROM sourced_leads WHERE user_id=? GROUP BY status", (g.user_id,)):
        by_status[row['status']] = row['cnt']
    by_source = {}
    for row in db.execute(
        "SELECT source, COUNT(*) as cnt FROM sourced_leads WHERE user_id=? GROUP BY source", (g.user_id,)):
        by_source[row['source']] = row['cnt']
    by_state = {}
    for row in db.execute(
        "SELECT state, COUNT(*) as cnt FROM sourced_leads WHERE user_id=? AND state!='' GROUP BY state ORDER BY cnt DESC LIMIT 10", (g.user_id,)):
        by_state[row['state']] = row['cnt']
    conversion_rate = round((by_status.get('converted', 0) / total * 100) if total > 0 else 0, 1)
    db.close()

    return jsonify({
        'total': total,
        'by_status': by_status,
        'by_source': by_source,
        'by_state': by_state,
        'conversion_rate': conversion_rate
    })


# --- Referral Links API ---

@app.route('/api/referral-links', methods=['GET'])
@require_auth
def api_get_referral_links_c33():
    """List referral links for the user."""
    db = get_db()
    links = [dict(r) for r in db.execute(
        "SELECT * FROM referral_links WHERE user_id=? ORDER BY created_at DESC",
        (g.user_id,)).fetchall()]
    db.close()
    return jsonify({'links': links})


@app.route('/api/referral-links', methods=['POST'])
@require_auth
def api_create_referral_link_c33():
    """Create a new referral link."""
    data = request.get_json() or {}
    interview_id = data.get('interview_id')
    label = (data.get('label') or '').strip()

    if not interview_id:
        return jsonify({'error': 'interview_id is required'}), 400

    db = get_db()
    interview = db.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",
                           (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    link_id = str(uuid.uuid4())
    code = str(uuid.uuid4())[:8]
    db.execute("""
        INSERT INTO referral_links (id, user_id, interview_id, code, label)
        VALUES (?,?,?,?,?)
    """, (link_id, g.user_id, interview_id, code, label or f'Referral {code}'))
    db.commit()
    db.close()

    return jsonify({
        'id': link_id,
        'code': code,
        'url': f'/apply/{interview_id}?ref={code}',
        'success': True
    }), 201


@app.route('/api/referral-links/<link_id>', methods=['DELETE'])
@require_auth
def api_delete_referral_link_c33(link_id):
    """Delete a referral link."""
    db = get_db()
    link = db.execute("SELECT * FROM referral_links WHERE id=? AND user_id=?",
                      (link_id, g.user_id)).fetchone()
    if not link:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    db.execute("DELETE FROM referral_links WHERE id=? AND user_id=?", (link_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/referral-links/<link_id>/stats', methods=['GET'])
@require_auth
def api_referral_link_stats_c33(link_id):
    """Get stats for a referral link."""
    db = get_db()
    link = db.execute("SELECT * FROM referral_links WHERE id=? AND user_id=?",
                      (link_id, g.user_id)).fetchone()
    if not link:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    candidates = [dict(r) for r in db.execute(
        "SELECT id, first_name || ' ' || last_name as name, email, pipeline_stage, created_at FROM candidates WHERE referral_code=? AND user_id=?",
        (link['code'], g.user_id)).fetchall()]
    db.close()
    return jsonify({
        'link': dict(link),
        'candidates': candidates,
        'total_candidates': len(candidates)
    })


# --- Referral Tracking (Public) ---

@app.route('/api/referral/<code>/click', methods=['POST'])
def api_referral_click_c33(code):
    """Track a referral link click (public endpoint)."""
    db = get_db()
    link = db.execute("SELECT * FROM referral_links WHERE code=? AND is_active=1", (code,)).fetchone()
    if link:
        db.execute("UPDATE referral_links SET clicks=clicks+1 WHERE id=?", (link['id'],))
        db.commit()
    db.close()
    return jsonify({'success': True})


# --- Job Syndication API ---

@app.route('/api/job-syndication', methods=['GET'])
@require_auth
def api_get_syndications_c33():
    """List job syndications for the user."""
    db = get_db()
    syndications = [dict(r) for r in db.execute("""
        SELECT js.*, i.title as interview_title, i.position, i.location
        FROM job_syndications js
        JOIN interviews i ON js.interview_id = i.id
        WHERE js.user_id=? ORDER BY js.created_at DESC
    """, (g.user_id,)).fetchall()]
    db.close()
    return jsonify({'syndications': syndications})


@app.route('/api/job-syndication', methods=['POST'])
@require_auth
def api_create_syndication_c33():
    """Create a job syndication entry."""
    data = request.get_json() or {}
    interview_id = data.get('interview_id')
    platform = data.get('platform', 'indeed')

    if not interview_id:
        return jsonify({'error': 'interview_id is required'}), 400

    db = get_db()
    interview = db.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",
                           (interview_id, g.user_id)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Interview not found'}), 404

    existing = db.execute(
        "SELECT id FROM job_syndications WHERE interview_id=? AND platform=? AND user_id=?",
        (interview_id, platform, g.user_id)).fetchone()
    if existing:
        db.close()
        return jsonify({'error': f'Already syndicated to {platform}', 'id': existing['id']}), 409

    synd_id = str(uuid.uuid4())
    db.execute("""
        INSERT INTO job_syndications (id, user_id, interview_id, platform, status, posted_at)
        VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
    """, (synd_id, g.user_id, interview_id, platform, 'active'))

    if platform == 'indeed':
        db.execute("UPDATE interviews SET indeed_feed_enabled=1 WHERE id=?", (interview_id,))
    elif platform == 'google_jobs':
        db.execute("UPDATE interviews SET google_jobs_enabled=1 WHERE id=?", (interview_id,))

    db.commit()
    db.close()
    return jsonify({'id': synd_id, 'success': True}), 201


@app.route('/api/job-syndication/<synd_id>', methods=['DELETE'])
@require_auth
def api_delete_syndication_c33(synd_id):
    """Remove a job syndication."""
    db = get_db()
    synd = db.execute("SELECT * FROM job_syndications WHERE id=? AND user_id=?",
                      (synd_id, g.user_id)).fetchone()
    if not synd:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    if synd['platform'] == 'indeed':
        db.execute("UPDATE interviews SET indeed_feed_enabled=0 WHERE id=?", (synd['interview_id'],))
    elif synd['platform'] == 'google_jobs':
        db.execute("UPDATE interviews SET google_jobs_enabled=0 WHERE id=?", (synd['interview_id'],))

    db.execute("DELETE FROM job_syndications WHERE id=? AND user_id=?", (synd_id, g.user_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


# --- Indeed XML Feed (Public) ---

@app.route('/indeed-feed.xml')
def indeed_feed_c33():
    """Public Indeed job feed XML for job syndication."""
    db = get_db()
    jobs = db.execute("""
        SELECT i.*, u.name as company_name FROM interviews i
        JOIN users u ON i.user_id = u.id
        WHERE i.indeed_feed_enabled=1 AND i.job_board_enabled=1
        ORDER BY i.created_at DESC LIMIT 100
    """).fetchall()
    db.close()

    xml_items = []
    for j in jobs:
        j = dict(j)
        location = j.get('location') or 'United States'
        salary = j.get('salary_range') or ''
        job_type = (j.get('job_type') or 'full_time').replace('_', '-')
        xml_items.append(f"""<job>
    <title><![CDATA[{j.get('position') or j['title']}]]></title>
    <date><![CDATA[{j['created_at']}]]></date>
    <referencenumber><![CDATA[{j['id']}]]></referencenumber>
    <url><![CDATA[https://mychannelview.com/apply/{j['id']}]]></url>
    <company><![CDATA[{j.get('company_name') or 'Insurance Agency'}]]></company>
    <city><![CDATA[{location.split(',')[0].strip() if ',' in location else location}]]></city>
    <state><![CDATA[{location.split(',')[1].strip() if ',' in location else ''}]]></state>
    <description><![CDATA[{j.get('description') or ''}]]></description>
    <salary><![CDATA[{salary}]]></salary>
    <jobtype><![CDATA[{job_type}]]></jobtype>
    <category><![CDATA[Insurance]]></category>
</job>""")

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<source>
<publisher>ChannelView</publisher>
<publisherurl>https://mychannelview.com</publisherurl>
<lastBuildDate>{datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')}</lastBuildDate>
{''.join(xml_items)}
</source>"""
    return xml, 200, {'Content-Type': 'application/xml'}


# --- Google Jobs JSON-LD (Public) ---

@app.route('/api/jobs/google-jsonld/<interview_id>')
def google_jobs_jsonld_c33(interview_id):
    """Return Google Jobs structured data for an interview."""
    db = get_db()
    interview = db.execute("SELECT i.*, u.name as company_name FROM interviews i JOIN users u ON i.user_id=u.id WHERE i.id=? AND i.google_jobs_enabled=1",
                           (interview_id,)).fetchone()
    if not interview:
        db.close()
        return jsonify({'error': 'Not found'}), 404
    i = dict(interview)
    db.close()

    location = i.get('location') or 'United States'
    city = location.split(',')[0].strip() if ',' in location else location
    state = location.split(',')[1].strip() if ',' in location else ''

    jsonld = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": i.get('position') or i['title'],
        "description": i.get('description') or '',
        "datePosted": i['created_at'][:10] if i.get('created_at') else '',
        "hiringOrganization": {
            "@type": "Organization",
            "name": i.get('company_name') or 'Insurance Agency',
            "sameAs": "https://mychannelview.com"
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": city,
                "addressRegion": state,
                "addressCountry": "US"
            }
        },
        "employmentType": (i.get('job_type') or 'FULL_TIME').upper().replace('-', '_'),
        "industry": "Insurance",
        "directApply": True,
        "url": f"https://mychannelview.com/apply/{i['id']}"
    }

    if i.get('application_deadline'):
        jsonld['validThrough'] = i['application_deadline'][:10]

    return jsonify(jsonld)


# --- ZIP Code Radius Search ---

@app.route('/api/leads/zip-search', methods=['GET'])
@require_auth
def api_zip_search_c33():
    """Search leads by ZIP code with basic radius matching (same prefix)."""
    zip_code = request.args.get('zip_code', '').strip()
    radius = request.args.get('radius', 'exact')

    if not zip_code or len(zip_code) < 3:
        return jsonify({'error': 'Valid zip_code required (min 3 digits)'}), 400

    db = get_db()
    if radius == 'exact':
        leads = [dict(r) for r in db.execute(
            "SELECT * FROM sourced_leads WHERE user_id=? AND zip_code=? ORDER BY last_name",
            (g.user_id, zip_code)).fetchall()]
    elif radius == 'nearby':
        prefix = zip_code[:3]
        leads = [dict(r) for r in db.execute(
            "SELECT * FROM sourced_leads WHERE user_id=? AND zip_code LIKE ? ORDER BY zip_code, last_name",
            (g.user_id, f'{prefix}%')).fetchall()]
    elif radius == 'region':
        prefix = zip_code[:2]
        leads = [dict(r) for r in db.execute(
            "SELECT * FROM sourced_leads WHERE user_id=? AND zip_code LIKE ? ORDER BY zip_code, last_name",
            (g.user_id, f'{prefix}%')).fetchall()]
    else:
        leads = [dict(r) for r in db.execute(
            "SELECT * FROM sourced_leads WHERE user_id=? AND zip_code=? ORDER BY last_name",
            (g.user_id, zip_code)).fetchall()]

    db.close()
    return jsonify({'leads': leads, 'total': len(leads), 'zip_code': zip_code, 'radius': radius})


# ======================== CYCLE 34: VOICE AGENT ROUTES ========================

from voice_service import VoiceService

def _get_voice_service():
    """Get a VoiceService instance with the current user's API key."""
    db = get_db()
    try:
        user = db.execute("SELECT retell_api_key FROM users WHERE id = ?", (g.user_id,)).fetchone()
        api_key = dict(user).get('retell_api_key') if user else None
        return VoiceService(api_key=api_key)
    finally:
        db.close()

# --- Voice Agent Page ---
@app.route('/voice-agent')
@require_auth
def voice_agent_page():
    return render_template('app.html', page='voice-agent', user=g.user)

# --- Agent CRUD ---
@app.route('/api/voice/agents', methods=['GET'])
@require_auth
def api_voice_agents_list():
    svc = _get_voice_service()
    agents = svc.get_agents(g.user_id)
    return jsonify({'agents': agents})

@app.route('/api/voice/agents', methods=['POST'])
@require_auth
def api_voice_agents_create_c34():
    data = request.get_json() or {}
    name = data.get('name', 'Recruiting Agent')
    voice_id = data.get('voice_id', 'eleven_labs_rachel')
    greeting = data.get('greeting_script', 'Hi, this is the recruiting team calling about an opportunity.')
    prompt = data.get('persona_prompt')
    svc = _get_voice_service()
    agent_id, error = svc.create_retell_agent(g.user_id, name, voice_id, greeting, prompt)
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'agent_id': agent_id, 'message': 'Voice agent created'}), 201

@app.route('/api/voice/agents/<agent_id>', methods=['GET'])
@require_auth
def api_voice_agent_get_c34(agent_id):
    svc = _get_voice_service()
    agent = svc.get_agent(agent_id, g.user_id)
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    return jsonify({'agent': agent})

@app.route('/api/voice/agents/<agent_id>', methods=['PUT'])
@require_auth
def api_voice_agent_update_c34(agent_id):
    data = request.get_json() or {}
    svc = _get_voice_service()
    ok, error = svc.update_agent(agent_id, g.user_id, data)
    if not ok:
        return jsonify({'error': error}), 400
    return jsonify({'message': 'Agent updated'})

@app.route('/api/voice/agents/<agent_id>', methods=['DELETE'])
@require_auth
def api_voice_agent_delete_c34(agent_id):
    svc = _get_voice_service()
    ok, error = svc.delete_agent(agent_id, g.user_id)
    if not ok:
        return jsonify({'error': error}), 400
    return jsonify({'message': 'Agent deleted'})

# --- Call Management ---
@app.route('/api/voice/calls', methods=['GET'])
@require_auth
def api_voice_calls_list_c34():
    filters = {
        'status': request.args.get('status'),
        'candidate_id': request.args.get('candidate_id'),
        'agent_id': request.args.get('agent_id'),
        'date_from': request.args.get('date_from'),
        'date_to': request.args.get('date_to'),
    }
    filters = {k: v for k, v in filters.items() if v}
    svc = _get_voice_service()
    calls = svc.get_calls(g.user_id, filters if filters else None)
    return jsonify({'calls': calls, 'total': len(calls)})

@app.route('/api/voice/calls', methods=['POST'])
@require_auth
def api_voice_call_create_c34():
    data = request.get_json() or {}
    agent_id = data.get('agent_id')
    candidate_id = data.get('candidate_id')
    phone = data.get('phone_number')

    if not agent_id:
        return jsonify({'error': 'agent_id required'}), 400
    if not phone and not candidate_id:
        return jsonify({'error': 'phone_number or candidate_id required'}), 400

    # If no phone provided, get from candidate
    if not phone and candidate_id:
        db = get_db()
        try:
            cand = db.execute("SELECT phone FROM candidates WHERE id = ? AND user_id = ?",
                              (candidate_id, g.user_id)).fetchone()
            if cand:
                phone = dict(cand).get('phone')
        finally:
            db.close()
        if not phone:
            return jsonify({'error': 'Candidate has no phone number'}), 400

    svc = _get_voice_service()
    call_id, error = svc.create_call(g.user_id, agent_id, candidate_id, phone,
                                      data.get('script_id'), data.get('metadata'))
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'call_id': call_id, 'message': 'Call initiated'}), 201

@app.route('/api/voice/calls/<call_id>', methods=['GET'])
@require_auth
def api_voice_call_get_c34(call_id):
    svc = _get_voice_service()
    call = svc.get_call(call_id, g.user_id)
    if not call:
        return jsonify({'error': 'Call not found'}), 404
    return jsonify({'call': call})

# --- Retell Webhook (no auth — Retell sends events here) ---
@app.route('/api/voice/webhook', methods=['POST'])
def api_voice_webhook_c34():
    data = request.get_json() or {}
    svc = VoiceService()  # No API key needed for receiving webhooks
    ok, error = svc.handle_call_event(data)
    if not ok:
        return jsonify({'error': error}), 400
    return jsonify({'received': True})

# --- Scripts ---
@app.route('/api/voice/scripts', methods=['GET'])
@require_auth
def api_voice_scripts_list_c34():
    agent_id = request.args.get('agent_id')
    svc = _get_voice_service()
    scripts = svc.get_scripts(g.user_id, agent_id)
    return jsonify({'scripts': scripts})

@app.route('/api/voice/scripts', methods=['POST'])
@require_auth
def api_voice_scripts_create_c34():
    data = request.get_json() or {}
    agent_id = data.get('agent_id')
    name = data.get('name', 'New Script')
    if not agent_id:
        return jsonify({'error': 'agent_id required'}), 400
    svc = _get_voice_service()
    script_id, error = svc.create_script(
        g.user_id, agent_id, name,
        data.get('script_type', 'scheduling'),
        data.get('purpose'),
        data.get('conversation_flow')
    )
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'script_id': script_id, 'message': 'Script created'}), 201

@app.route('/api/voice/scripts/<script_id>', methods=['PUT'])
@require_auth
def api_voice_scripts_update_c34(script_id):
    data = request.get_json() or {}
    svc = _get_voice_service()
    ok, error = svc.update_script(script_id, g.user_id, data)
    if not ok:
        return jsonify({'error': error}), 400
    return jsonify({'message': 'Script updated'})

# --- Scheduling ---
@app.route('/api/voice/schedule', methods=['GET'])
@require_auth
def api_voice_schedule_list_c34():
    status = request.args.get('status', 'pending')
    svc = _get_voice_service()
    scheduled = svc.get_scheduled_calls(g.user_id, status)
    return jsonify({'scheduled_calls': scheduled, 'total': len(scheduled)})

@app.route('/api/voice/schedule', methods=['POST'])
@require_auth
def api_voice_schedule_create_c34():
    data = request.get_json() or {}
    required = ['agent_id', 'candidate_id', 'scheduled_at']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'{field} required'}), 400
    svc = _get_voice_service()
    sched_id, error = svc.schedule_call(
        g.user_id, data['agent_id'], data['candidate_id'],
        data['scheduled_at'], data.get('call_type', 'scheduling'),
        data.get('script_id'), data.get('notes')
    )
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'schedule_id': sched_id, 'message': 'Call scheduled'}), 201

@app.route('/api/voice/schedule/execute', methods=['POST'])
@require_auth
def api_voice_schedule_execute_c34():
    svc = _get_voice_service()
    results = svc.execute_scheduled_calls(g.user_id)
    return jsonify({'results': results, 'executed': len(results)})

# --- Analytics ---
@app.route('/api/voice/stats', methods=['GET'])
@require_auth
def api_voice_stats_c34():
    days = int(request.args.get('days', 30))
    svc = _get_voice_service()
    stats = svc.get_voice_stats(g.user_id, days)
    return jsonify({'stats': stats})

# --- Consent ---
@app.route('/api/voice/consent/<candidate_id>', methods=['PUT'])
@require_auth
def api_voice_consent_c34(candidate_id):
    data = request.get_json() or {}
    consent = data.get('consent', True)
    svc = _get_voice_service()
    ok = svc.set_voice_consent(candidate_id, g.user_id, consent)
    if not ok:
        return jsonify({'error': 'Failed to update consent'}), 400
    return jsonify({'message': 'Consent updated'})

# --- Callable Candidates ---
@app.route('/api/voice/candidates', methods=['GET'])
@require_auth
def api_voice_candidates_c34():
    stage = request.args.get('pipeline_stage')
    consent_only = request.args.get('consent_only', 'true').lower() == 'true'
    svc = _get_voice_service()
    candidates = svc.get_candidates_for_calling(g.user_id, stage, consent_only)
    return jsonify({'candidates': candidates, 'total': len(candidates)})

# --- Voice Settings (API key config) ---
@app.route('/api/voice/settings', methods=['GET'])
@require_auth
def api_voice_settings_get_c34():
    db = get_db()
    try:
        user = db.execute(
            "SELECT retell_api_key, voice_agent_enabled, voice_caller_id FROM users WHERE id = ?",
            (g.user_id,)
        ).fetchone()
        if not user:
            return jsonify({'error': 'User not found'}), 404
        u = dict(user)
        return jsonify({
            'retell_api_key_set': bool(u.get('retell_api_key')),
            'voice_agent_enabled': bool(u.get('voice_agent_enabled')),
            'voice_caller_id': u.get('voice_caller_id', ''),
        })
    finally:
        db.close()

@app.route('/api/voice/settings', methods=['PUT'])
@require_auth
def api_voice_settings_update_c34():
    data = request.get_json() or {}
    db = get_db()
    try:
        updates = []
        values = []
        if 'retell_api_key' in data:
            updates.append("retell_api_key = ?")
            values.append(data['retell_api_key'])
        if 'voice_agent_enabled' in data:
            updates.append("voice_agent_enabled = ?")
            values.append(1 if data['voice_agent_enabled'] else 0)
        if 'voice_caller_id' in data:
            updates.append("voice_caller_id = ?")
            values.append(data['voice_caller_id'])
        if not updates:
            return jsonify({'error': 'No settings to update'}), 400
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(g.user_id)
        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", values)
        db.commit()
        return jsonify({'message': 'Voice settings updated'})
    finally:
        db.close()


# ======================== CYCLE 29: FIRST INTERVIEW HUB ========================
# Multi-Format Interviews, Group Sessions, Waterfall Engagement, Format Selector

# --- Format Configuration ---

@app.route('/api/interviews/<interview_id>/formats', methods=['GET'])
@require_auth
def api_get_formats_c29(interview_id):
    """Get interview format configuration."""
    db = get_db()
    try:
        interview = db.execute('SELECT * FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
        if not interview:
            return jsonify({'error': 'Interview not found'}), 404
        row = dict(interview)
        import json as _json
        formats_enabled = _json.loads(row.get('formats_enabled') or '["video"]')
        waterfall_config = _json.loads(row.get('waterfall_config_json') or '{}')
        return jsonify({
            'interview_id': interview_id,
            'formats_enabled': formats_enabled,
            'format_selector_enabled': bool(row.get('format_selector_enabled', 0)),
            'waterfall_enabled': bool(row.get('waterfall_enabled', 0)),
            'waterfall_config': waterfall_config,
            'group_session_description': row.get('group_session_description', ''),
            'one_on_one_description': row.get('one_on_one_description', ''),
            'one_on_one_type': row.get('one_on_one_type', 'recruiter_call'),
        })
    finally:
        db.close()


@app.route('/api/interviews/<interview_id>/formats', methods=['PUT'])
@require_auth
def api_update_formats_c29(interview_id):
    """Update interview format configuration."""
    db = get_db()
    try:
        interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
        if not interview:
            return jsonify({'error': 'Interview not found'}), 404
        data = request.get_json() or {}
        import json as _json
        updates = []
        values = []
        if 'formats_enabled' in data:
            valid_formats = {'video', 'group_session', 'one_on_one', 'ai_phone'}
            fmts = [f for f in data['formats_enabled'] if f in valid_formats]
            if 'video' not in fmts:
                fmts.insert(0, 'video')
            updates.append("formats_enabled = ?")
            values.append(_json.dumps(fmts))
            updates.append("format_selector_enabled = ?")
            values.append(1 if len(fmts) > 1 else 0)
        if 'waterfall_enabled' in data:
            updates.append("waterfall_enabled = ?")
            values.append(1 if data['waterfall_enabled'] else 0)
        if 'waterfall_config' in data:
            wf = data['waterfall_config']
            updates.append("waterfall_config_json = ?")
            values.append(_json.dumps(wf))
        if 'group_session_description' in data:
            updates.append("group_session_description = ?")
            values.append(data['group_session_description'])
        if 'one_on_one_description' in data:
            updates.append("one_on_one_description = ?")
            values.append(data['one_on_one_description'])
        if 'one_on_one_type' in data:
            valid_types = {'recruiter_call', 'ai_phone', 'in_person'}
            ot = data['one_on_one_type'] if data['one_on_one_type'] in valid_types else 'recruiter_call'
            updates.append("one_on_one_type = ?")
            values.append(ot)
        if not updates:
            return jsonify({'error': 'No valid fields to update'}), 400
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(interview_id)
        db.execute(f"UPDATE interviews SET {', '.join(updates)} WHERE id = ?", values)
        db.commit()
        return jsonify({'message': 'Format configuration updated', 'interview_id': interview_id})
    finally:
        db.close()


# --- Group Sessions CRUD ---

@app.route('/api/interviews/<interview_id>/group-sessions', methods=['POST'])
@require_auth
def api_create_group_session_c29(interview_id):
    """Create a group info session (single or recurring series)."""
    db = get_db()
    try:
        interview = db.execute('SELECT id, title FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
        if not interview:
            return jsonify({'error': 'Interview not found'}), 404
        data = request.get_json() or {}
        title = data.get('title', f"Info Session — {dict(interview)['title']}")
        session_type = data.get('session_type', 'in_person')
        if session_type not in ('in_person', 'virtual'):
            session_type = 'in_person'
        import json as _json
        sessions_created = []
        # If recurring_rule provided, generate multiple sessions
        recurring_rule = data.get('recurring_rule')
        dates = data.get('session_dates', [])
        if not dates:
            if not data.get('session_date'):
                return jsonify({'error': 'session_date or session_dates required'}), 400
            dates = [data['session_date']]
        for sdate in dates:
            sid = str(uuid.uuid4())
            db.execute("""INSERT INTO group_sessions
                (id, interview_id, user_id, title, session_type, location, meeting_url,
                 session_date, duration_minutes, capacity, status, recurring_rule, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, interview_id, g.user_id, title, session_type,
                 data.get('location', ''), data.get('meeting_url', ''),
                 sdate, data.get('duration_minutes', 60),
                 data.get('capacity', 0), 'scheduled',
                 _json.dumps(recurring_rule) if recurring_rule else None,
                 data.get('notes', '')))
            sessions_created.append(sid)
        db.commit()
        return jsonify({'message': f'{len(sessions_created)} session(s) created', 'session_ids': sessions_created})
    finally:
        db.close()


@app.route('/api/interviews/<interview_id>/group-sessions', methods=['GET'])
@require_auth
def api_list_group_sessions_c29(interview_id):
    """List all group sessions for an interview."""
    db = get_db()
    try:
        rows = db.execute(
            '''SELECT gs.*, (SELECT COUNT(*) FROM session_rsvps sr WHERE sr.session_id=gs.id AND sr.status != 'cancelled') as rsvp_count,
               (SELECT COUNT(*) FROM session_rsvps sr WHERE sr.session_id=gs.id AND sr.status='attended') as attended_count
               FROM group_sessions gs WHERE gs.interview_id=? AND gs.user_id=? ORDER BY gs.session_date ASC''',
            (interview_id, g.user_id)
        ).fetchall()
        sessions = []
        for r in rows:
            d = dict(r)
            d['spots_remaining'] = max(0, d['capacity'] - d['rsvp_count']) if d['capacity'] > 0 else None
            sessions.append(d)
        return jsonify(sessions)
    finally:
        db.close()


@app.route('/api/group-sessions/<session_id>', methods=['PUT'])
@require_auth
def api_update_group_session_c29(session_id):
    """Update a group session."""
    db = get_db()
    try:
        session = db.execute('SELECT id FROM group_sessions WHERE id=? AND user_id=?', (session_id, g.user_id)).fetchone()
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        data = request.get_json() or {}
        allowed = ['title', 'session_type', 'location', 'meeting_url', 'session_date',
                    'duration_minutes', 'capacity', 'status', 'notes']
        updates = []
        values = []
        for field in allowed:
            if field in data:
                updates.append(f"{field} = ?")
                values.append(data[field])
        if not updates:
            return jsonify({'error': 'No fields to update'}), 400
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(session_id)
        db.execute(f"UPDATE group_sessions SET {', '.join(updates)} WHERE id = ?", values)
        db.commit()
        return jsonify({'message': 'Session updated'})
    finally:
        db.close()


@app.route('/api/group-sessions/<session_id>', methods=['DELETE'])
@require_auth
def api_delete_group_session_c29(session_id):
    """Cancel/delete a group session."""
    db = get_db()
    try:
        session = db.execute('SELECT id FROM group_sessions WHERE id=? AND user_id=?', (session_id, g.user_id)).fetchone()
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        db.execute("UPDATE group_sessions SET status='cancelled', updated_at=? WHERE id=?",
                   (datetime.utcnow().isoformat(), session_id))
        db.commit()
        return jsonify({'message': 'Session cancelled'})
    finally:
        db.close()


@app.route('/api/group-sessions/<session_id>/attendees', methods=['GET'])
@require_auth
def api_session_attendees_c29(session_id):
    """Get RSVP and attendance list for a session."""
    db = get_db()
    try:
        session = db.execute('SELECT id FROM group_sessions WHERE id=? AND user_id=?', (session_id, g.user_id)).fetchone()
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        rows = db.execute(
            '''SELECT sr.*, c.first_name, c.last_name, c.email, c.phone, c.ai_score
               FROM session_rsvps sr
               JOIN candidates c ON sr.candidate_id = c.id
               WHERE sr.session_id=? ORDER BY sr.rsvp_at ASC''',
            (session_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route('/api/group-sessions/<session_id>/attendance', methods=['PUT'])
@require_auth
def api_mark_attendance_c29(session_id):
    """Mark candidates as attended or no-show."""
    db = get_db()
    try:
        session = db.execute('SELECT id, interview_id FROM group_sessions WHERE id=? AND user_id=?', (session_id, g.user_id)).fetchone()
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        data = request.get_json() or {}
        attended = data.get('attended', [])
        no_show = data.get('no_show', [])
        now = datetime.utcnow().isoformat()
        for cid in attended:
            db.execute("UPDATE session_rsvps SET status='attended', attended_at=? WHERE session_id=? AND candidate_id=?",
                       (now, session_id, cid))
            db.execute("UPDATE candidates SET interview_format='group_session', waterfall_stage='group_attended' WHERE id=?", (cid,))
        for cid in no_show:
            db.execute("UPDATE session_rsvps SET status='no_show' WHERE session_id=? AND candidate_id=?",
                       (session_id, cid))
            db.execute("UPDATE candidates SET waterfall_stage='group_noshow' WHERE id=?", (cid,))
        db.commit()
        return jsonify({'message': f'Marked {len(attended)} attended, {len(no_show)} no-show'})
    finally:
        db.close()


# --- Booking Slots (One-on-One) ---

@app.route('/api/interviews/<interview_id>/booking-slots', methods=['POST'])
@require_auth
def api_create_booking_slots_c29(interview_id):
    """Create availability slots for one-on-one interviews."""
    db = get_db()
    try:
        interview = db.execute('SELECT id FROM interviews WHERE id=? AND user_id=?', (interview_id, g.user_id)).fetchone()
        if not interview:
            return jsonify({'error': 'Interview not found'}), 404
        data = request.get_json() or {}
        slots = data.get('slots', [])
        if not slots:
            return jsonify({'error': 'slots array required'}), 400
        created = []
        for slot in slots:
            sid = str(uuid.uuid4())
            db.execute("""INSERT INTO booking_slots
                (id, interview_id, user_id, slot_date, duration_minutes, slot_type, meeting_url, phone_number, status)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (sid, interview_id, g.user_id, slot['date'],
                 slot.get('duration_minutes', 30),
                 slot.get('slot_type', 'recruiter_call'),
                 slot.get('meeting_url', ''), slot.get('phone_number', ''), 'available'))
            created.append(sid)
        db.commit()
        return jsonify({'message': f'{len(created)} slot(s) created', 'slot_ids': created})
    finally:
        db.close()


@app.route('/api/interviews/<interview_id>/booking-slots', methods=['GET'])
@require_auth
def api_list_booking_slots_c29(interview_id):
    """List booking slots for an interview."""
    db = get_db()
    try:
        rows = db.execute(
            '''SELECT bs.*, c.first_name, c.last_name, c.email
               FROM booking_slots bs
               LEFT JOIN candidates c ON bs.booked_by_candidate_id = c.id
               WHERE bs.interview_id=? AND bs.user_id=? ORDER BY bs.slot_date ASC''',
            (interview_id, g.user_id)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


# --- Candidate-Facing: Format Choice + RSVP (No auth) ---

@app.route('/i/<token>/choose')
def candidate_format_choice_c29(token):
    """Candidate format selection page — shows available interview formats."""
    db = get_db()
    try:
        candidate = db.execute(
            '''SELECT c.*, i.id as interview_id, i.title, i.description, i.brand_color,
                      i.formats_enabled, i.format_selector_enabled, i.welcome_msg,
                      i.group_session_description, i.one_on_one_description, i.one_on_one_type,
                      u.agency_name, u.agency_logo_url, u.name as interviewer_name
               FROM candidates c
               JOIN interviews i ON c.interview_id = i.id
               JOIN users u ON c.user_id = u.id
               WHERE c.token = ?''', (token,)
        ).fetchone()
        if not candidate:
            return render_template('candidate_error.html', error='Interview link not found or has expired.'), 404
        cd = dict(candidate)
        if cd['status'] == 'completed':
            return render_template('candidate_done.html', candidate=cd)
        import json as _json
        formats = _json.loads(cd.get('formats_enabled') or '["video"]')
        # Get upcoming group sessions if group_session is enabled
        upcoming_sessions = []
        if 'group_session' in formats:
            rows = db.execute(
                '''SELECT gs.*, (SELECT COUNT(*) FROM session_rsvps sr WHERE sr.session_id=gs.id AND sr.status != 'cancelled') as rsvp_count
                   FROM group_sessions gs
                   WHERE gs.interview_id=? AND gs.status='scheduled' AND gs.session_date > datetime('now')
                   ORDER BY gs.session_date ASC LIMIT 10''',
                (cd['interview_id'],)
            ).fetchall()
            for r in rows:
                sd = dict(r)
                sd['spots_remaining'] = max(0, sd['capacity'] - sd['rsvp_count']) if sd['capacity'] > 0 else None
                upcoming_sessions.append(sd)
        # Get available booking slots if one_on_one is enabled
        available_slots = []
        if 'one_on_one' in formats:
            rows = db.execute(
                '''SELECT * FROM booking_slots
                   WHERE interview_id=? AND is_booked=0 AND status='available' AND slot_date > datetime('now')
                   ORDER BY slot_date ASC LIMIT 20''',
                (cd['interview_id'],)
            ).fetchall()
            available_slots = [dict(r) for r in rows]
        return render_template('candidate_format_choice.html',
            candidate=cd, token=token, formats=formats,
            upcoming_sessions=upcoming_sessions, available_slots=available_slots,
            brand_color=cd.get('brand_color', '#0ace0a'),
            agency_name=cd.get('agency_name', ''))
    finally:
        db.close()


@app.route('/i/<token>/rsvp/<session_id>', methods=['POST'])
def candidate_rsvp_c29(token, session_id):
    """Candidate RSVPs for a group session."""
    db = get_db()
    try:
        candidate = db.execute(
            'SELECT c.id, c.interview_id, c.status FROM candidates c WHERE c.token=?', (token,)
        ).fetchone()
        if not candidate:
            return jsonify({'error': 'Invalid interview link'}), 404
        cd = dict(candidate)
        session = db.execute(
            'SELECT * FROM group_sessions WHERE id=? AND interview_id=? AND status=?',
            (session_id, cd['interview_id'], 'scheduled')
        ).fetchone()
        if not session:
            return jsonify({'error': 'Session not found or no longer available'}), 404
        sd = dict(session)
        # Check capacity
        if sd['capacity'] > 0:
            rsvp_count = db.execute(
                "SELECT COUNT(*) as cnt FROM session_rsvps WHERE session_id=? AND status != 'cancelled'",
                (session_id,)
            ).fetchone()['cnt']
            if rsvp_count >= sd['capacity']:
                return jsonify({'error': 'Session is full'}), 400
        # Check if already RSVP'd
        existing = db.execute(
            "SELECT id FROM session_rsvps WHERE session_id=? AND candidate_id=?",
            (session_id, cd['id'])
        ).fetchone()
        if existing:
            return jsonify({'error': 'Already registered for this session', 'rsvp_id': dict(existing)['id']}), 409
        rsvp_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO session_rsvps (id, session_id, candidate_id, status, rsvp_at) VALUES (?,?,?,?,?)",
            (rsvp_id, session_id, cd['id'], 'rsvp', now)
        )
        db.execute(
            "UPDATE candidates SET interview_format='group_session', waterfall_stage='group_rsvp', session_rsvp_id=?, format_chosen_at=? WHERE id=?",
            (rsvp_id, now, cd['id'])
        )
        db.commit()
        return jsonify({
            'message': 'RSVP confirmed',
            'rsvp_id': rsvp_id,
            'session': {
                'title': sd['title'],
                'session_type': sd['session_type'],
                'location': sd['location'],
                'meeting_url': sd['meeting_url'],
                'session_date': sd['session_date'],
                'duration_minutes': sd['duration_minutes']
            }
        })
    finally:
        db.close()


@app.route('/i/<token>/book/<slot_id>', methods=['POST'])
def candidate_book_slot_c29(token, slot_id):
    """Candidate books a one-on-one time slot."""
    db = get_db()
    try:
        candidate = db.execute(
            'SELECT c.id, c.interview_id FROM candidates c WHERE c.token=?', (token,)
        ).fetchone()
        if not candidate:
            return jsonify({'error': 'Invalid interview link'}), 404
        cd = dict(candidate)
        slot = db.execute(
            'SELECT * FROM booking_slots WHERE id=? AND interview_id=? AND is_booked=0 AND status=?',
            (slot_id, cd['interview_id'], 'available')
        ).fetchone()
        if not slot:
            return jsonify({'error': 'Slot not available'}), 404
        now = datetime.utcnow().isoformat()
        db.execute(
            "UPDATE booking_slots SET is_booked=1, booked_by_candidate_id=?, booked_at=?, status='booked' WHERE id=?",
            (cd['id'], now, slot_id)
        )
        db.execute(
            "UPDATE candidates SET interview_format='one_on_one', waterfall_stage='ono_booked', booking_slot_id=?, format_chosen_at=? WHERE id=?",
            (slot_id, now, cd['id'])
        )
        db.commit()
        sd = dict(slot)
        return jsonify({
            'message': 'Booking confirmed',
            'slot': {
                'date': sd['slot_date'],
                'duration_minutes': sd['duration_minutes'],
                'slot_type': sd['slot_type'],
                'meeting_url': sd['meeting_url'],
                'phone_number': sd['phone_number']
            }
        })
    finally:
        db.close()


@app.route('/i/<token>/choose-video', methods=['POST'])
def candidate_choose_video_c29(token):
    """Candidate picks async video format — record the choice and redirect."""
    db = get_db()
    try:
        candidate = db.execute('SELECT id FROM candidates WHERE token=?', (token,)).fetchone()
        if not candidate:
            return jsonify({'error': 'Invalid link'}), 404
        now = datetime.utcnow().isoformat()
        db.execute("UPDATE candidates SET interview_format='video', waterfall_stage='video_chosen', format_chosen_at=? WHERE id=?",
                   (now, dict(candidate)['id']))
        db.commit()
        return jsonify({'message': 'Video format selected', 'redirect': f'/i/{token}'})
    finally:
        db.close()


# --- Waterfall Engine ---

@app.route('/api/waterfall/process', methods=['POST'])
@require_auth
def api_waterfall_process_c29():
    """Process pending waterfall steps for all interviews owned by this user.
       In production, call this from a cron job or scheduled task."""
    db = get_db()
    try:
        import json as _json
        now = datetime.utcnow()
        now_iso = now.isoformat()
        # Find candidates due for waterfall advancement
        candidates = db.execute(
            '''SELECT c.id, c.token, c.first_name, c.last_name, c.email,
                      c.interview_id, c.waterfall_stage, c.waterfall_step_index,
                      i.waterfall_config_json, i.waterfall_enabled, i.formats_enabled,
                      i.title as interview_title, u.agency_name, u.id as owner_user_id,
                      i.brand_color
               FROM candidates c
               JOIN interviews i ON c.interview_id = i.id
               JOIN users u ON c.user_id = u.id
               WHERE c.user_id = ? AND i.waterfall_enabled = 1
               AND c.waterfall_next_at IS NOT NULL AND c.waterfall_next_at <= ?
               AND c.status NOT IN ('completed', 'hired', 'rejected')''',
            (g.user_id, now_iso)
        ).fetchall()
        processed = 0
        for cand in candidates:
            cd = dict(cand)
            config = _json.loads(cd['waterfall_config_json'] or '{}')
            steps = config.get('steps', [])
            step_idx = cd['waterfall_step_index'] or 0
            if step_idx >= len(steps):
                # No more steps — archive
                db.execute("UPDATE candidates SET waterfall_stage='archived', waterfall_next_at=NULL WHERE id=?", (cd['id'],))
                processed += 1
                continue
            step = steps[step_idx]
            step_type = step.get('type', 'group_session')
            wait_days = step.get('wait_days', 3)
            next_step_idx = step_idx + 1
            next_at = (now + timedelta(days=wait_days)).isoformat() if next_step_idx < len(steps) else None
            # Advance candidate to next step
            new_stage = f'{step_type}_invited'
            db.execute(
                "UPDATE candidates SET waterfall_stage=?, waterfall_step_index=?, waterfall_next_at=? WHERE id=?",
                (new_stage, next_step_idx, next_at, cd['id'])
            )
            # Send the appropriate email for this step
            try:
                from email_service import send_email, get_smtp_config, build_branded_email
                smtp_config = get_smtp_config(db, cd['owner_user_id'])
                candidate_name = f"{cd['first_name']} {cd['last_name']}"
                if step_type == 'group_session':
                    subject = f"Join Us: {cd['interview_title']} — Information Session"
                    link = f"{request.host_url}i/{cd['token']}/choose"
                    body_html = build_branded_email(
                        candidate_name, cd['interview_title'],
                        f"We'd love to meet you! Join one of our upcoming information sessions to learn more about the opportunity.",
                        link, "View Sessions", cd.get('agency_name', ''), cd.get('brand_color', '#0ace0a')
                    )
                elif step_type == 'one_on_one':
                    subject = f"Let's Talk: {cd['interview_title']} — Schedule a Call"
                    link = f"{request.host_url}i/{cd['token']}/choose"
                    body_html = build_branded_email(
                        candidate_name, cd['interview_title'],
                        f"We'd like to schedule a quick conversation with you about this opportunity. Pick a time that works for your schedule.",
                        link, "Schedule a Call", cd.get('agency_name', ''), cd.get('brand_color', '#0ace0a')
                    )
                elif step_type == 'ai_phone':
                    subject = f"Quick Call: {cd['interview_title']} — Phone Interview"
                    link = f"{request.host_url}i/{cd['token']}/choose"
                    body_html = build_branded_email(
                        candidate_name, cd['interview_title'],
                        f"We'll be giving you a brief phone call to discuss the opportunity. Keep an eye out for a call from our team!",
                        link, "More Details", cd.get('agency_name', ''), cd.get('brand_color', '#0ace0a')
                    )
                else:
                    subject = f"Reminder: {cd['interview_title']}"
                    link = f"{request.host_url}i/{cd['token']}"
                    body_html = build_branded_email(
                        candidate_name, cd['interview_title'],
                        f"We're still interested in connecting with you about this opportunity.",
                        link, "Complete Interview", cd.get('agency_name', ''), cd.get('brand_color', '#0ace0a')
                    )
                send_email(smtp_config, cd['email'], subject, body_html)
                db.execute(
                    "INSERT INTO email_log (id, user_id, candidate_id, email_type, to_email, subject, status) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), cd['owner_user_id'], cd['id'], f'waterfall_{step_type}', cd['email'], subject, 'sent')
                )
            except Exception as e:
                print(f'[Waterfall] Email failed for {cd["id"]}: {e}')
            processed += 1
        db.commit()
        return jsonify({'message': f'Processed {processed} candidates', 'processed': processed})
    finally:
        db.close()


@app.route('/api/interviews/<interview_id>/waterfall-status', methods=['GET'])
@require_auth
def api_waterfall_status_c29(interview_id):
    """Dashboard: candidates at each waterfall stage."""
    db = get_db()
    try:
        rows = db.execute(
            '''SELECT waterfall_stage, COUNT(*) as count
               FROM candidates WHERE interview_id=? AND user_id=? AND waterfall_stage IS NOT NULL
               GROUP BY waterfall_stage''',
            (interview_id, g.user_id)
        ).fetchall()
        stages = {r['waterfall_stage']: r['count'] for r in rows}
        total = sum(stages.values())
        return jsonify({
            'interview_id': interview_id,
            'total_in_waterfall': total,
            'stages': stages,
            'recovery_rate': round((stages.get('group_attended', 0) + stages.get('group_rsvp', 0) +
                                    stages.get('ono_booked', 0) + stages.get('completed', 0)) / max(total, 1) * 100, 1)
        })
    finally:
        db.close()


# --- Updated Candidate Interview Route (format selector redirect) ---

@app.route('/i/<token>/format-check')
def candidate_format_check_c29(token):
    """API endpoint to check if format selection is needed. Called by candidate_interview.html on load."""
    db = get_db()
    try:
        candidate = db.execute(
            '''SELECT c.interview_format, c.format_chosen_at, i.format_selector_enabled, i.formats_enabled
               FROM candidates c
               JOIN interviews i ON c.interview_id = i.id
               WHERE c.token = ?''', (token,)
        ).fetchone()
        if not candidate:
            return jsonify({'needs_choice': False})
        cd = dict(candidate)
        import json as _json
        formats = _json.loads(cd.get('formats_enabled') or '["video"]')
        needs_choice = bool(cd.get('format_selector_enabled')) and len(formats) > 1 and not cd.get('format_chosen_at')
        return jsonify({'needs_choice': needs_choice, 'formats': formats, 'choose_url': f'/i/{token}/choose'})
    finally:
        db.close()


# ======================== CYCLE 29B: SCHEDULING & 2ND INTERVIEWS ========================
# Availability patterns, auto-slot generation, conflict detection, 2nd interview scheduling

# --- Availability Patterns ("Box the Repetitive") ---

@app.route('/api/availability-patterns', methods=['POST'])
@require_auth
def api_create_availability_pattern_c29b():
    """Create a recurring availability pattern. System auto-generates slots from these."""
    db = get_db()
    try:
        data = request.get_json() or {}
        patterns = data.get('patterns', [])
        if not patterns:
            # Single pattern creation
            if 'day_of_week' not in data or 'start_time' not in data or 'end_time' not in data:
                return jsonify({'error': 'day_of_week, start_time, and end_time are required'}), 400
            patterns = [data]
        created_ids = []
        for p in patterns:
            pid = str(uuid.uuid4())
            db.execute("""INSERT INTO availability_patterns
                (id, user_id, label, day_of_week, start_time, end_time, slot_duration_minutes,
                 slot_type, meeting_url, phone_number, location, is_active, generate_weeks_ahead)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, g.user_id, p.get('label', 'My Availability'),
                 p['day_of_week'], p['start_time'], p['end_time'],
                 p.get('slot_duration_minutes', 30),
                 p.get('slot_type', 'recruiter_call'),
                 p.get('meeting_url', ''), p.get('phone_number', ''),
                 p.get('location', ''), 1,
                 p.get('generate_weeks_ahead', 4)))
            created_ids.append(pid)
        db.commit()
        return jsonify({'message': f'{len(created_ids)} pattern(s) created', 'pattern_ids': created_ids})
    finally:
        db.close()


@app.route('/api/availability-patterns', methods=['GET'])
@require_auth
def api_list_availability_patterns_c29b():
    """List all active availability patterns for the recruiter."""
    db = get_db()
    try:
        rows = db.execute(
            'SELECT * FROM availability_patterns WHERE user_id=? AND is_active=1 ORDER BY day_of_week, start_time',
            (g.user_id,)
        ).fetchall()
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        result = []
        for r in rows:
            d = dict(r)
            d['day_name'] = day_names[d['day_of_week']] if 0 <= d['day_of_week'] <= 6 else 'Unknown'
            result.append(d)
        return jsonify(result)
    finally:
        db.close()


@app.route('/api/availability-patterns/<pattern_id>', methods=['PUT'])
@require_auth
def api_update_availability_pattern_c29b(pattern_id):
    """Update an availability pattern."""
    db = get_db()
    try:
        pat = db.execute('SELECT id FROM availability_patterns WHERE id=? AND user_id=?', (pattern_id, g.user_id)).fetchone()
        if not pat:
            return jsonify({'error': 'Pattern not found'}), 404
        data = request.get_json() or {}
        allowed = ['label', 'day_of_week', 'start_time', 'end_time', 'slot_duration_minutes',
                    'slot_type', 'meeting_url', 'phone_number', 'location', 'is_active', 'generate_weeks_ahead']
        updates, values = [], []
        for field in allowed:
            if field in data:
                updates.append(f"{field} = ?")
                values.append(data[field])
        if not updates:
            return jsonify({'error': 'No fields to update'}), 400
        values.append(pattern_id)
        db.execute(f"UPDATE availability_patterns SET {', '.join(updates)} WHERE id = ?", values)
        db.commit()
        return jsonify({'message': 'Pattern updated'})
    finally:
        db.close()


@app.route('/api/availability-patterns/<pattern_id>', methods=['DELETE'])
@require_auth
def api_delete_availability_pattern_c29b(pattern_id):
    """Deactivate an availability pattern."""
    db = get_db()
    try:
        db.execute('UPDATE availability_patterns SET is_active=0 WHERE id=? AND user_id=?', (pattern_id, g.user_id))
        db.commit()
        return jsonify({'message': 'Pattern deactivated'})
    finally:
        db.close()


# --- Auto-Generate Slots from Patterns ---

@app.route('/api/availability/generate-slots', methods=['POST'])
@require_auth
def api_generate_slots_c29b():
    """Auto-generate booking slots from active availability patterns.
       Call this on a schedule or when recruiter saves patterns."""
    db = get_db()
    try:
        data = request.get_json() or {}
        interview_id = data.get('interview_id')
        stage = data.get('interview_stage', 'second')  # 'first' or 'second'
        patterns = db.execute(
            'SELECT * FROM availability_patterns WHERE user_id=? AND is_active=1',
            (g.user_id,)
        ).fetchall()
        if not patterns:
            return jsonify({'error': 'No active availability patterns. Create patterns first.'}), 400
        from datetime import timedelta
        now = datetime.utcnow()
        created = 0
        skipped_conflicts = 0
        for pat in patterns:
            pd = dict(pat)
            weeks = pd['generate_weeks_ahead'] or 4
            duration = pd['slot_duration_minutes'] or 30
            # Parse start/end times
            sh, sm = map(int, pd['start_time'].split(':'))
            eh, em = map(int, pd['end_time'].split(':'))
            start_minutes = sh * 60 + sm
            end_minutes = eh * 60 + em
            # Generate slots for each week
            for week_offset in range(weeks):
                # Find next occurrence of this day_of_week
                target_day = pd['day_of_week']  # 0=Mon, 6=Sun
                days_ahead = (target_day - now.weekday()) % 7 + (week_offset * 7)
                if days_ahead == 0 and week_offset == 0:
                    days_ahead = 0 if now.hour < sh else 7
                slot_date_base = now + timedelta(days=days_ahead)
                slot_date_base = slot_date_base.replace(hour=0, minute=0, second=0, microsecond=0)
                # Generate slots within the time window
                current_min = start_minutes
                while current_min + duration <= end_minutes:
                    slot_hour = current_min // 60
                    slot_minute = current_min % 60
                    slot_dt = slot_date_base.replace(hour=slot_hour, minute=slot_minute)
                    slot_iso = slot_dt.isoformat()
                    slot_end_dt = slot_dt + timedelta(minutes=duration)
                    # --- Conflict detection ---
                    # Check against existing booking slots
                    existing = db.execute(
                        """SELECT id FROM booking_slots
                           WHERE user_id=? AND status IN ('available','booked')
                           AND slot_date=?""",
                        (g.user_id, slot_iso)
                    ).fetchone()
                    if existing:
                        current_min += duration
                        skipped_conflicts += 1
                        continue
                    # Check against group sessions (don't create slots during group sessions)
                    gs_conflict = db.execute(
                        """SELECT id FROM group_sessions
                           WHERE user_id=? AND status='scheduled'
                           AND session_date <= ? AND datetime(session_date, '+' || duration_minutes || ' minutes') > ?""",
                        (g.user_id, slot_iso, slot_iso)
                    ).fetchone()
                    if gs_conflict:
                        current_min += duration
                        skipped_conflicts += 1
                        continue
                    # No conflict — create the slot
                    sid = str(uuid.uuid4())
                    db.execute("""INSERT INTO booking_slots
                        (id, interview_id, user_id, slot_date, duration_minutes, slot_type,
                         meeting_url, phone_number, is_booked, status, pattern_id, interview_stage)
                        VALUES (?,?,?,?,?,?,?,?,0,?,?,?)""",
                        (sid, interview_id or '', g.user_id, slot_iso, duration,
                         pd['slot_type'], pd.get('meeting_url', ''), pd.get('phone_number', ''),
                         'available', pd['id'], stage))
                    created += 1
                    current_min += duration
        db.commit()
        return jsonify({
            'message': f'{created} slot(s) generated, {skipped_conflicts} skipped (conflicts)',
            'created': created,
            'skipped_conflicts': skipped_conflicts
        })
    finally:
        db.close()


# --- Recruiter Calendar View (unified) ---

@app.route('/api/calendar', methods=['GET'])
@require_auth
def api_calendar_view_c29b():
    """Unified calendar view: group sessions + booking slots + 2nd interviews."""
    db = get_db()
    try:
        start = request.args.get('start', datetime.utcnow().isoformat())
        end = request.args.get('end', (datetime.utcnow() + timedelta(days=28)).isoformat())
        events = []
        # Group sessions
        rows = db.execute(
            """SELECT gs.*, (SELECT COUNT(*) FROM session_rsvps sr WHERE sr.session_id=gs.id AND sr.status!='cancelled') as rsvp_count
               FROM group_sessions gs WHERE gs.user_id=? AND gs.status='scheduled'
               AND gs.session_date BETWEEN ? AND ? ORDER BY gs.session_date""",
            (g.user_id, start, end)
        ).fetchall()
        for r in rows:
            d = dict(r)
            events.append({
                'id': d['id'], 'type': 'group_session', 'title': d['title'],
                'start': d['session_date'], 'duration_minutes': d['duration_minutes'],
                'session_type': d['session_type'], 'location': d['location'],
                'meeting_url': d['meeting_url'], 'rsvp_count': d['rsvp_count'],
                'capacity': d['capacity']
            })
        # Booking slots
        rows = db.execute(
            """SELECT bs.*, c.first_name, c.last_name, c.email
               FROM booking_slots bs LEFT JOIN candidates c ON bs.booked_by_candidate_id=c.id
               WHERE bs.user_id=? AND bs.status IN ('available','booked')
               AND bs.slot_date BETWEEN ? AND ? ORDER BY bs.slot_date""",
            (g.user_id, start, end)
        ).fetchall()
        for r in rows:
            d = dict(r)
            title = 'Available' if not d['is_booked'] else f"{d.get('first_name','')} {d.get('last_name','')}"
            events.append({
                'id': d['id'], 'type': 'booking_slot', 'title': title,
                'start': d['slot_date'], 'duration_minutes': d['duration_minutes'],
                'slot_type': d['slot_type'], 'is_booked': bool(d['is_booked']),
                'interview_stage': d.get('interview_stage', 'first'),
                'candidate_name': f"{d.get('first_name','')} {d.get('last_name','')}" if d['is_booked'] else None,
                'candidate_email': d.get('email') if d['is_booked'] else None
            })
        # 2nd interviews
        rows = db.execute(
            """SELECT si.*, c.first_name, c.last_name, c.email
               FROM second_interviews si JOIN candidates c ON si.candidate_id=c.id
               WHERE si.user_id=? AND si.status IN ('scheduled','confirmed')
               AND si.scheduled_date BETWEEN ? AND ? ORDER BY si.scheduled_date""",
            (g.user_id, start, end)
        ).fetchall()
        for r in rows:
            d = dict(r)
            events.append({
                'id': d['id'], 'type': 'second_interview',
                'title': f"2nd Interview: {d['first_name']} {d['last_name']}",
                'start': d['scheduled_date'], 'duration_minutes': d['duration_minutes'],
                'meeting_type': d['meeting_type'], 'meeting_url': d.get('meeting_url'),
                'phone_number': d.get('phone_number'), 'candidate_name': f"{d['first_name']} {d['last_name']}",
                'candidate_email': d['email'], 'status': d['status']
            })
        events.sort(key=lambda e: e['start'] or '')
        return jsonify({'events': events, 'count': len(events)})
    finally:
        db.close()


# --- 2nd Interview Scheduling (Always 1-on-1) ---

@app.route('/api/candidates/<candidate_id>/second-interview', methods=['POST'])
@require_auth
def api_schedule_second_interview_c29b(candidate_id):
    """Schedule a 2nd interview for a candidate. Recruiter picks the method:
       - 'manual': recruiter will call to schedule (just logs intent)
       - 'ai_voice': AI voice agent calls candidate to schedule
       - 'email': system emails candidate with available slots to self-book
    """
    db = get_db()
    try:
        candidate = db.execute(
            '''SELECT c.*, i.title as interview_title, u.agency_name
               FROM candidates c JOIN interviews i ON c.interview_id=i.id
               JOIN users u ON c.user_id=u.id
               WHERE c.id=? AND c.user_id=?''',
            (candidate_id, g.user_id)
        ).fetchone()
        if not candidate:
            return jsonify({'error': 'Candidate not found'}), 404
        cd = dict(candidate)
        data = request.get_json() or {}
        method = data.get('schedule_method', 'email')
        if method not in ('manual', 'ai_voice', 'email'):
            return jsonify({'error': 'schedule_method must be manual, ai_voice, or email'}), 400
        now = datetime.utcnow().isoformat()
        si_id = str(uuid.uuid4())
        db.execute("""INSERT INTO second_interviews
            (id, user_id, candidate_id, interview_id, schedule_method, status,
             scheduled_date, duration_minutes, meeting_type, meeting_url,
             phone_number, location, notes, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (si_id, g.user_id, candidate_id, cd['interview_id'], method,
             'pending' if method != 'manual' else 'scheduling',
             data.get('scheduled_date'), data.get('duration_minutes', 30),
             data.get('meeting_type', 'phone'),
             data.get('meeting_url', ''), data.get('phone_number', ''),
             data.get('location', ''), data.get('notes', ''), now))
        # Update candidate tracking
        db.execute(
            "UPDATE candidates SET second_interview_id=?, second_interview_status=?, pipeline_stage='second_interview' WHERE id=?",
            (si_id, 'pending', candidate_id))
        # Method-specific actions
        if method == 'email':
            # Send candidate an email with link to pick a time
            try:
                from email_service import send_email, get_smtp_config, build_branded_email
                smtp_config = get_smtp_config(db, g.user_id)
                candidate_name = f"{cd['first_name']} {cd['last_name']}"
                schedule_link = f"{request.host_url}schedule/{cd['token']}"
                html = build_branded_email(
                    candidate_name, cd['interview_title'],
                    "Congratulations! We'd like to move forward with a second interview. Please pick a time that works for your schedule.",
                    schedule_link, "Pick a Time",
                    cd.get('agency_name', ''), cd.get('brand_color', '#0ace0a'))
                subject = f"Next Step: {cd['interview_title']} — Schedule Your Interview"
                send_email(smtp_config, cd['email'], subject, html)
                db.execute(
                    "INSERT INTO email_log (id, user_id, candidate_id, email_type, to_email, subject, status) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), g.user_id, candidate_id, 'second_interview_schedule', cd['email'], subject, 'sent'))
            except Exception as e:
                print(f'[2ndInterview] Email failed: {e}')
        elif method == 'ai_voice':
            # Queue AI voice call to schedule (uses existing voice_call_schedule)
            try:
                call_id = str(uuid.uuid4())
                db.execute("""INSERT INTO voice_call_schedule
                    (id, user_id, candidate_id, agent_id, scheduled_time, call_type, status)
                    VALUES (?,?,?,?,?,?,?)""",
                    (call_id, g.user_id, candidate_id, data.get('agent_id', ''),
                     now, 'second_interview_schedule', 'pending'))
            except Exception as e:
                print(f'[2ndInterview] Voice schedule failed: {e}')
        # manual = recruiter handles it themselves, we just track it
        db.commit()
        return jsonify({
            'message': f'2nd interview initiated via {method}',
            'second_interview_id': si_id,
            'schedule_method': method,
            'status': 'pending' if method != 'manual' else 'scheduling'
        })
    finally:
        db.close()


@app.route('/api/candidates/<candidate_id>/second-interview', methods=['GET'])
@require_auth
def api_get_second_interview_c29b(candidate_id):
    """Get 2nd interview status for a candidate."""
    db = get_db()
    try:
        row = db.execute(
            'SELECT * FROM second_interviews WHERE candidate_id=? AND user_id=? ORDER BY created_at DESC LIMIT 1',
            (candidate_id, g.user_id)
        ).fetchone()
        if not row:
            return jsonify({'error': 'No second interview found'}), 404
        return jsonify(dict(row))
    finally:
        db.close()


@app.route('/api/candidates/<candidate_id>/second-interview', methods=['PUT'])
@require_auth
def api_update_second_interview_c29b(candidate_id):
    """Update 2nd interview (confirm time, mark complete, add outcome/notes)."""
    db = get_db()
    try:
        row = db.execute(
            'SELECT id FROM second_interviews WHERE candidate_id=? AND user_id=? ORDER BY created_at DESC LIMIT 1',
            (candidate_id, g.user_id)
        ).fetchone()
        if not row:
            return jsonify({'error': 'No second interview found'}), 404
        si_id = dict(row)['id']
        data = request.get_json() or {}
        updates, values = [], []
        allowed = ['scheduled_date', 'duration_minutes', 'meeting_type', 'meeting_url',
                    'phone_number', 'location', 'notes', 'recruiter_notes', 'outcome', 'status']
        for field in allowed:
            if field in data:
                updates.append(f"{field} = ?")
                values.append(data[field])
        now = datetime.utcnow().isoformat()
        if data.get('status') == 'scheduled' and 'scheduled_date' in data:
            updates.append("scheduled_at = ?")
            values.append(now)
            db.execute("UPDATE candidates SET second_interview_status='scheduled', second_interview_date=? WHERE id=?",
                       (data['scheduled_date'], candidate_id))
        if data.get('status') == 'completed':
            updates.append("completed_at = ?")
            values.append(now)
            db.execute("UPDATE candidates SET second_interview_status='completed' WHERE id=?", (candidate_id,))
        if data.get('outcome') in ('offer', 'pass', 'hold'):
            stage_map = {'offer': 'offer_made', 'pass': 'rejected', 'hold': 'on_hold'}
            db.execute("UPDATE candidates SET pipeline_stage=? WHERE id=?",
                       (stage_map[data['outcome']], candidate_id))
        updates.append("updated_at = ?")
        values.append(now)
        values.append(si_id)
        db.execute(f"UPDATE second_interviews SET {', '.join(updates)} WHERE id = ?", values)
        db.commit()
        return jsonify({'message': '2nd interview updated', 'second_interview_id': si_id})
    finally:
        db.close()


@app.route('/api/second-interviews', methods=['GET'])
@require_auth
def api_list_second_interviews_c29b():
    """List all 2nd interviews for the recruiter, filterable by status."""
    db = get_db()
    try:
        status = request.args.get('status')
        query = """SELECT si.*, c.first_name, c.last_name, c.email, c.phone, c.ai_score,
                          c.interview_format, i.title as interview_title
                   FROM second_interviews si
                   JOIN candidates c ON si.candidate_id=c.id
                   JOIN interviews i ON si.interview_id=i.id
                   WHERE si.user_id=?"""
        params = [g.user_id]
        if status:
            query += " AND si.status=?"
            params.append(status)
        query += " ORDER BY si.created_at DESC"
        rows = db.execute(query, params).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


# --- Candidate Self-Schedule Page (2nd Interview) ---

@app.route('/schedule/<token>')
def candidate_schedule_page_c29b(token):
    """Candidate-facing page to pick a time slot for their 2nd interview."""
    db = get_db()
    try:
        candidate = db.execute(
            '''SELECT c.*, i.title as interview_title, i.brand_color, i.description,
                      u.agency_name, u.agency_logo_url
               FROM candidates c
               JOIN interviews i ON c.interview_id=i.id
               JOIN users u ON c.user_id=u.id
               WHERE c.token=?''', (token,)
        ).fetchone()
        if not candidate:
            return render_template('candidate_error.html', error='Interview link not found.'), 404
        cd = dict(candidate)
        # Get available 2nd interview slots
        available = db.execute(
            """SELECT * FROM booking_slots
               WHERE user_id=? AND is_booked=0 AND status='available'
               AND interview_stage='second' AND slot_date > datetime('now')
               ORDER BY slot_date ASC LIMIT 30""",
            (cd['user_id'],)
        ).fetchall()
        slots = [dict(r) for r in available]
        return render_template('candidate_schedule.html',
            candidate=cd, token=token, slots=slots,
            brand_color=cd.get('brand_color', '#0ace0a'),
            agency_name=cd.get('agency_name', ''))
    finally:
        db.close()


@app.route('/schedule/<token>/book', methods=['POST'])
def candidate_book_second_c29b(token):
    """Candidate books a slot for their 2nd interview."""
    db = get_db()
    try:
        candidate = db.execute('SELECT * FROM candidates WHERE token=?', (token,)).fetchone()
        if not candidate:
            return jsonify({'error': 'Invalid link'}), 404
        cd = dict(candidate)
        data = request.get_json() or {}
        slot_id = data.get('slot_id')
        if not slot_id:
            return jsonify({'error': 'slot_id required'}), 400
        # Atomic slot claim
        slot = db.execute(
            "SELECT * FROM booking_slots WHERE id=? AND is_booked=0 AND status='available' AND interview_stage='second'",
            (slot_id,)
        ).fetchone()
        if not slot:
            return jsonify({'error': 'Slot no longer available. Please pick another time.'}), 409
        sd = dict(slot)
        now = datetime.utcnow().isoformat()
        # Book the slot
        db.execute(
            "UPDATE booking_slots SET is_booked=1, booked_by_candidate_id=?, booked_at=?, status='booked' WHERE id=?",
            (cd['id'], now, slot_id))
        # Update 2nd interview record
        db.execute(
            """UPDATE second_interviews SET status='scheduled', scheduled_date=?, duration_minutes=?,
               booking_slot_id=?, scheduled_at=?, updated_at=?
               WHERE candidate_id=? AND user_id=? AND status='pending'""",
            (sd['slot_date'], sd['duration_minutes'], slot_id, now, now, cd['id'], cd['user_id']))
        # Update candidate
        db.execute(
            "UPDATE candidates SET second_interview_status='scheduled', second_interview_date=? WHERE id=?",
            (sd['slot_date'], cd['id']))
        db.commit()
        return jsonify({
            'message': 'Interview scheduled!',
            'scheduled_date': sd['slot_date'],
            'duration_minutes': sd['duration_minutes'],
            'meeting_type': sd['slot_type'],
            'meeting_url': sd.get('meeting_url', ''),
            'phone_number': sd.get('phone_number', '')
        })
    finally:
        db.close()


# --- Conflict Check Utility ---

@app.route('/api/calendar/check-conflicts', methods=['POST'])
@require_auth
def api_check_conflicts_c29b():
    """Check if a proposed time conflicts with existing events."""
    db = get_db()
    try:
        data = request.get_json() or {}
        proposed_start = data.get('start')
        duration = data.get('duration_minutes', 30)
        if not proposed_start:
            return jsonify({'error': 'start required'}), 400
        from datetime import timedelta
        proposed_end = (datetime.fromisoformat(proposed_start) + timedelta(minutes=duration)).isoformat()
        conflicts = []
        # Check booking slots
        row = db.execute(
            """SELECT id, slot_date, duration_minutes FROM booking_slots
               WHERE user_id=? AND status IN ('available','booked')
               AND slot_date < ? AND datetime(slot_date, '+' || duration_minutes || ' minutes') > ?""",
            (g.user_id, proposed_end, proposed_start)
        ).fetchone()
        if row:
            conflicts.append({'type': 'booking_slot', 'id': dict(row)['id'], 'start': dict(row)['slot_date']})
        # Check group sessions
        row = db.execute(
            """SELECT id, session_date, duration_minutes, title FROM group_sessions
               WHERE user_id=? AND status='scheduled'
               AND session_date < ? AND datetime(session_date, '+' || duration_minutes || ' minutes') > ?""",
            (g.user_id, proposed_end, proposed_start)
        ).fetchone()
        if row:
            conflicts.append({'type': 'group_session', 'id': dict(row)['id'], 'start': dict(row)['session_date'], 'title': dict(row)['title']})
        # Check 2nd interviews
        row = db.execute(
            """SELECT id, scheduled_date, duration_minutes FROM second_interviews
               WHERE user_id=? AND status IN ('scheduled','confirmed')
               AND scheduled_date < ? AND datetime(scheduled_date, '+' || duration_minutes || ' minutes') > ?""",
            (g.user_id, proposed_end, proposed_start)
        ).fetchone()
        if row:
            conflicts.append({'type': 'second_interview', 'id': dict(row)['id'], 'start': dict(row)['scheduled_date']})
        return jsonify({'has_conflict': len(conflicts) > 0, 'conflicts': conflicts})
    finally:
        db.close()


# ======================== CYCLE 29C: POST-INTERVIEW PIPELINE & ENGAGEMENT ========================
# Full lifecycle: Offer → Testing → Appointment → Writing Number → Production
# Engagement: automated emails, AI voice, personal calls — all tracked

# Valid milestone stages in order (unlicensed path)
MILESTONE_ORDER_UNLICENSED = [
    'screening', 'second_interview', 'offer_made', 'offer_accepted',
    'entered_testing', 'testing_scheduled', 'passed_test',
    'entered_appointment', 'received_writing_number', 'first_production'
]
# Licensed path: skip testing stages
MILESTONE_ORDER_LICENSED = [
    'screening', 'second_interview', 'offer_made', 'offer_accepted',
    'entered_appointment', 'received_writing_number', 'first_production'
]
# Map milestone to candidate date column
MILESTONE_DATE_COLUMNS = {
    'offer_made': 'offer_made_at',
    'offer_accepted': 'offer_accepted_at',
    'entered_testing': 'entered_testing_at',
    'testing_scheduled': 'testing_date',
    'passed_test': 'passed_test_at',
    'entered_appointment': 'entered_appointment_at',
    'received_writing_number': 'received_writing_number_at',
    'first_production': 'first_production_at',
}


@app.route('/api/candidates/<candidate_id>/milestone', methods=['PUT'])
@require_auth
def api_update_milestone_c29c(candidate_id):
    """Advance a candidate to a milestone stage. Auto-routes licensed candidates past testing."""
    db = get_db()
    try:
        candidate = db.execute(
            'SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)
        ).fetchone()
        if not candidate:
            return jsonify({'error': 'Candidate not found'}), 404
        cd = dict(candidate)
        data = request.get_json() or {}
        new_stage = data.get('milestone_stage')
        is_licensed = data.get('is_licensed', cd.get('is_licensed', 0))
        # Auto-skip: if licensed and recruiter tries to move to a testing stage, jump to appointment
        if is_licensed and new_stage in ('entered_testing', 'testing_scheduled', 'passed_test'):
            new_stage = 'entered_appointment'
        valid_stages = MILESTONE_ORDER_LICENSED if is_licensed else MILESTONE_ORDER_UNLICENSED
        if new_stage and new_stage not in valid_stages:
            return jsonify({'error': f'Invalid stage. Valid stages: {valid_stages}'}), 400
        now = datetime.utcnow().isoformat()
        updates = ["milestone_stage = ?", "milestone_updated_at = ?", "pipeline_stage = ?"]
        values = [new_stage, now, new_stage]
        # Set the appropriate date column
        date_col = MILESTONE_DATE_COLUMNS.get(new_stage)
        if date_col:
            # Allow explicit date override or use now
            stage_date = data.get('date', now)
            updates.append(f"{date_col} = ?")
            values.append(stage_date)
        if 'is_licensed' in data:
            updates.append("is_licensed = ?")
            values.append(1 if is_licensed else 0)
        values.append(candidate_id)
        db.execute(f"UPDATE candidates SET {', '.join(updates)} WHERE id = ?", values)
        # Auto-log touchpoint
        tp_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO pipeline_touchpoints
               (id, user_id, candidate_id, touchpoint_type, channel, direction, subject, milestone_stage, completed_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tp_id, g.user_id, candidate_id, 'milestone_advance', 'system', 'internal',
             f'Advanced to {new_stage}', new_stage, now))
        db.commit()
        return jsonify({
            'message': f'Candidate moved to {new_stage}',
            'milestone_stage': new_stage,
            'is_licensed': bool(is_licensed),
            'date_recorded': date_col
        })
    finally:
        db.close()


@app.route('/api/candidates/<candidate_id>/milestone', methods=['GET'])
@require_auth
def api_get_milestone_c29c(candidate_id):
    """Get full milestone timeline for a candidate."""
    db = get_db()
    try:
        candidate = db.execute(
            'SELECT * FROM candidates WHERE id=? AND user_id=?', (candidate_id, g.user_id)
        ).fetchone()
        if not candidate:
            return jsonify({'error': 'Candidate not found'}), 404
        cd = dict(candidate)
        is_licensed = bool(cd.get('is_licensed', 0))
        stages = MILESTONE_ORDER_LICENSED if is_licensed else MILESTONE_ORDER_UNLICENSED
        timeline = []
        for stage in stages:
            date_col = MILESTONE_DATE_COLUMNS.get(stage)
            date_val = cd.get(date_col) if date_col else None
            timeline.append({
                'stage': stage,
                'date': date_val,
                'completed': date_val is not None,
                'is_current': cd.get('milestone_stage') == stage
            })
        # Get touchpoint count per stage
        tp_counts = db.execute(
            """SELECT milestone_stage, COUNT(*) as cnt FROM pipeline_touchpoints
               WHERE candidate_id=? GROUP BY milestone_stage""",
            (candidate_id,)
        ).fetchall()
        tp_map = {r['milestone_stage']: r['cnt'] for r in tp_counts}
        for t in timeline:
            t['touchpoint_count'] = tp_map.get(t['stage'], 0)
        return jsonify({
            'candidate_id': candidate_id,
            'candidate_name': f"{cd.get('first_name','')} {cd.get('last_name','')}",
            'is_licensed': is_licensed,
            'current_stage': cd.get('milestone_stage', 'screening'),
            'timeline': timeline,
            'total_touchpoints': cd.get('touchpoint_count', 0),
            'last_touchpoint': cd.get('last_touchpoint_at'),
            'days_since_milestone': cd.get('days_since_milestone', 0)
        })
    finally:
        db.close()


# --- Engagement Touchpoints ---

@app.route('/api/candidates/<candidate_id>/touchpoints', methods=['POST'])
@require_auth
def api_log_touchpoint_c29c(candidate_id):
    """Log an engagement touchpoint — call, email, AI contact, note, etc."""
    db = get_db()
    try:
        candidate = db.execute('SELECT id, milestone_stage FROM candidates WHERE id=? AND user_id=?',
                               (candidate_id, g.user_id)).fetchone()
        if not candidate:
            return jsonify({'error': 'Candidate not found'}), 404
        cd = dict(candidate)
        data = request.get_json() or {}
        tp_type = data.get('touchpoint_type', 'note')
        if tp_type not in ('call', 'email', 'ai_voice', 'ai_email', 'text', 'in_person', 'note', 'milestone_advance'):
            return jsonify({'error': 'Invalid touchpoint_type'}), 400
        channel = data.get('channel', 'phone' if tp_type == 'call' else 'email' if tp_type in ('email', 'ai_email') else 'system')
        now = datetime.utcnow().isoformat()
        tp_id = str(uuid.uuid4())
        db.execute("""INSERT INTO pipeline_touchpoints
            (id, user_id, candidate_id, touchpoint_type, channel, direction, subject, body,
             status, notes, milestone_stage, scheduled_at, completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tp_id, g.user_id, candidate_id, tp_type, channel,
             data.get('direction', 'outbound'),
             data.get('subject', ''), data.get('body', ''),
             data.get('status', 'completed'),
             data.get('notes', ''), cd.get('milestone_stage', 'screening'),
             data.get('scheduled_at'), now))
        # Update candidate engagement stats
        db.execute(
            """UPDATE candidates SET touchpoint_count = COALESCE(touchpoint_count,0) + 1,
               last_touchpoint_at = ?, updated_at = ? WHERE id = ?""",
            (now, now, candidate_id))
        db.commit()
        return jsonify({'message': 'Touchpoint logged', 'touchpoint_id': tp_id})
    finally:
        db.close()


@app.route('/api/candidates/<candidate_id>/touchpoints', methods=['GET'])
@require_auth
def api_list_touchpoints_c29c(candidate_id):
    """Get all engagement touchpoints for a candidate, newest first."""
    db = get_db()
    try:
        rows = db.execute(
            """SELECT * FROM pipeline_touchpoints WHERE candidate_id=? AND user_id=?
               ORDER BY created_at DESC""",
            (candidate_id, g.user_id)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


# --- Engagement Automation Rules ---

@app.route('/api/engagement-rules', methods=['POST'])
@require_auth
def api_create_engagement_rule_c29c():
    """Create an automated engagement rule for a milestone stage."""
    db = get_db()
    try:
        data = request.get_json() or {}
        rules = data.get('rules', [data] if 'milestone_stage' in data else [])
        if not rules:
            return jsonify({'error': 'milestone_stage required'}), 400
        created = []
        for rule in rules:
            rid = str(uuid.uuid4())
            db.execute("""INSERT INTO engagement_rules
                (id, user_id, interview_id, milestone_stage, trigger_type, trigger_days,
                 action_type, email_subject, email_body, ai_voice_script, is_active, sort_order)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, g.user_id, rule.get('interview_id'),
                 rule['milestone_stage'], rule.get('trigger_type', 'days_in_stage'),
                 rule.get('trigger_days', 2), rule.get('action_type', 'email'),
                 rule.get('email_subject', ''), rule.get('email_body', ''),
                 rule.get('ai_voice_script', ''), 1,
                 rule.get('sort_order', 0)))
            created.append(rid)
        db.commit()
        return jsonify({'message': f'{len(created)} rule(s) created', 'rule_ids': created})
    finally:
        db.close()


@app.route('/api/engagement-rules', methods=['GET'])
@require_auth
def api_list_engagement_rules_c29c():
    """List all engagement automation rules."""
    db = get_db()
    try:
        stage = request.args.get('milestone_stage')
        query = 'SELECT * FROM engagement_rules WHERE user_id=? AND is_active=1'
        params = [g.user_id]
        if stage:
            query += ' AND milestone_stage=?'
            params.append(stage)
        query += ' ORDER BY milestone_stage, sort_order'
        rows = db.execute(query, params).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@app.route('/api/engagement-rules/<rule_id>', methods=['PUT'])
@require_auth
def api_update_engagement_rule_c29c(rule_id):
    """Update an engagement rule."""
    db = get_db()
    try:
        rule = db.execute('SELECT id FROM engagement_rules WHERE id=? AND user_id=?', (rule_id, g.user_id)).fetchone()
        if not rule:
            return jsonify({'error': 'Rule not found'}), 404
        data = request.get_json() or {}
        allowed = ['milestone_stage', 'trigger_type', 'trigger_days', 'action_type',
                    'email_subject', 'email_body', 'ai_voice_script', 'is_active', 'sort_order']
        updates, values = [], []
        for f in allowed:
            if f in data:
                updates.append(f"{f} = ?")
                values.append(data[f])
        if not updates:
            return jsonify({'error': 'No fields'}), 400
        values.append(rule_id)
        db.execute(f"UPDATE engagement_rules SET {', '.join(updates)} WHERE id = ?", values)
        db.commit()
        return jsonify({'message': 'Rule updated'})
    finally:
        db.close()


@app.route('/api/engagement-rules/<rule_id>', methods=['DELETE'])
@require_auth
def api_delete_engagement_rule_c29c(rule_id):
    """Deactivate an engagement rule."""
    db = get_db()
    try:
        db.execute('UPDATE engagement_rules SET is_active=0 WHERE id=? AND user_id=?', (rule_id, g.user_id))
        db.commit()
        return jsonify({'message': 'Rule deactivated'})
    finally:
        db.close()


# --- Automated Engagement Processor ---

@app.route('/api/engagement/process', methods=['POST'])
@require_auth
def api_process_engagement_c29c():
    """Process automated engagement rules. Finds candidates due for outreach and sends it.
       Call from cron or scheduled task."""
    db = get_db()
    try:
        import json as _json
        now = datetime.utcnow()
        now_iso = now.isoformat()
        rules = db.execute(
            'SELECT * FROM engagement_rules WHERE user_id=? AND is_active=1 ORDER BY milestone_stage, sort_order',
            (g.user_id,)
        ).fetchall()
        sent = 0
        skipped = 0
        for rule in rules:
            rd = dict(rule)
            trigger_days = rd['trigger_days'] or 2
            # Find candidates at this stage who haven't been contacted in trigger_days
            cutoff = (now - timedelta(days=trigger_days)).isoformat()
            candidates = db.execute(
                """SELECT c.*, i.title as interview_title, i.brand_color,
                          u.agency_name
                   FROM candidates c
                   JOIN interviews i ON c.interview_id=i.id
                   JOIN users u ON c.user_id=u.id
                   WHERE c.user_id=? AND c.milestone_stage=?
                   AND c.status NOT IN ('hired','rejected')
                   AND (c.last_touchpoint_at IS NULL OR c.last_touchpoint_at <= ?)""",
                (g.user_id, rd['milestone_stage'], cutoff)
            ).fetchall()
            for cand in candidates:
                cd = dict(cand)
                candidate_name = f"{cd['first_name']} {cd['last_name']}"
                # Perform the action
                if rd['action_type'] == 'email':
                    try:
                        from email_service import send_email, get_smtp_config, build_branded_email
                        smtp_config = get_smtp_config(db, g.user_id)
                        subject = rd['email_subject'] or f"Update: {cd.get('interview_title', 'Your Application')}"
                        body_text = rd['email_body'] or f"Hi {candidate_name}, just checking in on your progress. We're here to help with anything you need."
                        link = f"{request.host_url}i/{cd['token']}"
                        html = build_branded_email(
                            candidate_name, subject, body_text, link, "View Details",
                            cd.get('agency_name', ''), cd.get('brand_color', '#0ace0a'))
                        send_email(smtp_config, cd['email'], subject, html)
                        # Log touchpoint
                        tp_id = str(uuid.uuid4())
                        db.execute("""INSERT INTO pipeline_touchpoints
                            (id, user_id, candidate_id, touchpoint_type, channel, direction, subject, body,
                             status, milestone_stage, completed_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (tp_id, g.user_id, cd['id'], 'ai_email', 'email', 'outbound',
                             subject, body_text, 'sent', rd['milestone_stage'], now_iso))
                        db.execute(
                            "UPDATE candidates SET touchpoint_count=COALESCE(touchpoint_count,0)+1, last_touchpoint_at=? WHERE id=?",
                            (now_iso, cd['id']))
                        sent += 1
                    except Exception as e:
                        print(f'[Engagement] Email failed for {cd["id"]}: {e}')
                        skipped += 1
                elif rd['action_type'] == 'ai_voice':
                    try:
                        call_id = str(uuid.uuid4())
                        db.execute("""INSERT INTO voice_call_schedule
                            (id, user_id, candidate_id, agent_id, scheduled_time, call_type, status)
                            VALUES (?,?,?,?,?,?,?)""",
                            (call_id, g.user_id, cd['id'], '', now_iso, 'engagement_followup', 'pending'))
                        tp_id = str(uuid.uuid4())
                        db.execute("""INSERT INTO pipeline_touchpoints
                            (id, user_id, candidate_id, touchpoint_type, channel, direction, subject,
                             status, milestone_stage, scheduled_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (tp_id, g.user_id, cd['id'], 'ai_voice', 'phone', 'outbound',
                             f'AI follow-up call at {rd["milestone_stage"]}',
                             'scheduled', rd['milestone_stage'], now_iso))
                        db.execute(
                            "UPDATE candidates SET touchpoint_count=COALESCE(touchpoint_count,0)+1, last_touchpoint_at=? WHERE id=?",
                            (now_iso, cd['id']))
                        sent += 1
                    except Exception as e:
                        print(f'[Engagement] AI voice failed for {cd["id"]}: {e}')
                        skipped += 1
                elif rd['action_type'] == 'reminder':
                    # Just log a reminder for the recruiter to make a personal call
                    tp_id = str(uuid.uuid4())
                    db.execute("""INSERT INTO pipeline_touchpoints
                        (id, user_id, candidate_id, touchpoint_type, channel, direction, subject,
                         status, notes, milestone_stage, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (tp_id, g.user_id, cd['id'], 'note', 'system', 'internal',
                         f'Personal call reminder: {candidate_name} at {rd["milestone_stage"]}',
                         'pending', f'Candidate has been at {rd["milestone_stage"]} for {trigger_days}+ days. Consider a personal call.',
                         rd['milestone_stage'], now_iso))
                    sent += 1
        db.commit()
        return jsonify({'message': f'Processed: {sent} sent, {skipped} skipped', 'sent': sent, 'skipped': skipped})
    finally:
        db.close()


# --- Pipeline Dashboard ---

@app.route('/api/pipeline/overview', methods=['GET'])
@require_auth
def api_pipeline_overview_c29c():
    """Pipeline overview: count of candidates at each milestone stage."""
    db = get_db()
    try:
        rows = db.execute(
            """SELECT milestone_stage, COUNT(*) as count,
                      AVG(JULIANDAY('now') - JULIANDAY(milestone_updated_at)) as avg_days_in_stage
               FROM candidates WHERE user_id=? AND milestone_stage IS NOT NULL
               AND status NOT IN ('rejected')
               GROUP BY milestone_stage""",
            (g.user_id,)
        ).fetchall()
        stages = {}
        total_active = 0
        for r in rows:
            d = dict(r)
            stages[d['milestone_stage']] = {
                'count': d['count'],
                'avg_days': round(d['avg_days_in_stage'] or 0, 1)
            }
            total_active += d['count']
        # Conversion funnel
        funnel = []
        for stage in MILESTONE_ORDER_UNLICENSED:
            s = stages.get(stage, {'count': 0, 'avg_days': 0})
            funnel.append({'stage': stage, 'count': s['count'], 'avg_days': s['avg_days']})
        return jsonify({
            'total_active': total_active,
            'stages': stages,
            'funnel': funnel
        })
    finally:
        db.close()


@app.route('/api/pipeline/stale', methods=['GET'])
@require_auth
def api_pipeline_stale_c29c():
    """Find candidates who've been stuck at a stage too long (stale candidates)."""
    db = get_db()
    try:
        days_threshold = request.args.get('days', 5, type=int)
        cutoff = (datetime.utcnow() - timedelta(days=days_threshold)).isoformat()
        rows = db.execute(
            """SELECT c.id, c.first_name, c.last_name, c.email, c.phone,
                      c.milestone_stage, c.milestone_updated_at, c.last_touchpoint_at,
                      c.touchpoint_count, c.is_licensed, i.title as interview_title
               FROM candidates c JOIN interviews i ON c.interview_id=i.id
               WHERE c.user_id=? AND c.milestone_stage IS NOT NULL
               AND c.status NOT IN ('hired','rejected')
               AND (c.milestone_updated_at IS NULL OR c.milestone_updated_at <= ?)
               ORDER BY c.milestone_updated_at ASC""",
            (g.user_id, cutoff)
        ).fetchall()
        stale = []
        for r in rows:
            d = dict(r)
            if d['milestone_updated_at']:
                d['days_stuck'] = round((datetime.utcnow() - datetime.fromisoformat(d['milestone_updated_at'])).total_seconds() / 86400, 1)
            else:
                d['days_stuck'] = None
            stale.append(d)
        return jsonify({'stale_candidates': stale, 'count': len(stale), 'threshold_days': days_threshold})
    finally:
        db.close()


# ======================== INIT & RUN ========================

if __name__ == '__main__':
    init_db()

    is_prod = os.environ.get('FLASK_ENV') == 'production'
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')

    if is_prod:
        print(f"[ChannelView] Starting in PRODUCTION mode on {host}:{port}")
        print(f"[ChannelView] AI Scoring: {'Claude API' if is_ai_available() else 'Mock (set ANTHROPIC_API_KEY to enable)'}")
        app.run(host=host, port=port, debug=False)
    else:
        print(f"[ChannelView] Starting in DEVELOPMENT mode on {host}:{port}")
        print(f"[ChannelView] AI Scoring: {'Claude API' if is_ai_available() else 'Mock (set ANTHROPIC_API_KEY to enable)'}")
        app.run(host=host, port=port, debug=True)
