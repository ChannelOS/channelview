"""
ChannelView - Email Service
Multi-backend email delivery: SendGrid API, SMTP, or log-only fallback.
Handles branded invite, reminder, and completion emails.
"""
import smtplib
import ssl
import json
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger('channelview.email')


# ======================== BACKEND: SENDGRID API ========================

def send_via_sendgrid(api_key, from_email, from_name, to_email, subject, html_body):
    """Send email via SendGrid v3 API (no SDK dependency)."""
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": "Please view this email in an HTML-compatible email client."},
            {"type": "text/html", "value": html_body}
        ]
    }
    data = json.dumps(payload).encode('utf-8')
    req = Request('https://api.sendgrid.com/v3/mail/send', data=data, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    try:
        resp = urlopen(req, timeout=15)
        status = resp.getcode()
        if status in (200, 201, 202):
            logger.info(f'SendGrid: sent to {to_email} (status {status})')
            return True, None
        body = resp.read().decode('utf-8', errors='replace')
        return False, f'SendGrid returned status {status}: {body[:200]}'
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            err = json.loads(body)
            msg = err.get('errors', [{}])[0].get('message', body[:200])
        except:
            msg = body[:200]
        return False, f'SendGrid error ({e.code}): {msg}'
    except URLError as e:
        return False, f'SendGrid connection error: {e.reason}'
    except Exception as e:
        return False, f'SendGrid error: {str(e)}'


def test_sendgrid_connection(api_key):
    """Verify SendGrid API key is valid by checking scopes."""
    req = Request('https://api.sendgrid.com/v3/scopes', method='GET')
    req.add_header('Authorization', f'Bearer {api_key}')
    try:
        resp = urlopen(req, timeout=10)
        if resp.getcode() == 200:
            return True, None
        return False, f'SendGrid returned status {resp.getcode()}'
    except HTTPError as e:
        if e.code == 401:
            return False, 'Invalid SendGrid API key'
        return False, f'SendGrid error ({e.code})'
    except Exception as e:
        return False, str(e)


# ======================== BACKEND: SMTP ========================

def send_via_smtp(smtp_config, to_email, subject, html_body):
    """Send an HTML email via SMTP. Returns (success: bool, error: str or None)."""
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{smtp_config['from_name']} <{smtp_config['from_email']}>"
        msg['To'] = to_email

        plain = "Please view this email in an HTML-compatible email client."
        msg.attach(MIMEText(plain, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        port = int(smtp_config['port'])
        context = ssl.create_default_context()

        if port == 465:
            with smtplib.SMTP_SSL(smtp_config['host'], port, context=context, timeout=15) as server:
                if smtp_config['user'] and smtp_config['password']:
                    server.login(smtp_config['user'], smtp_config['password'])
                server.sendmail(smtp_config['from_email'], to_email, msg.as_string())
        else:
            with smtplib.SMTP(smtp_config['host'], port, timeout=15) as server:
                server.starttls(context=context)
                if smtp_config['user'] and smtp_config['password']:
                    server.login(smtp_config['user'], smtp_config['password'])
                server.sendmail(smtp_config['from_email'], to_email, msg.as_string())

        logger.info(f'SMTP: sent to {to_email} via {smtp_config["host"]}')
        return True, None
    except smtplib.SMTPAuthenticationError:
        return False, 'SMTP authentication failed. Check your username and password.'
    except smtplib.SMTPConnectError:
        return False, 'Could not connect to SMTP server. Check host and port.'
    except smtplib.SMTPException as e:
        return False, f'SMTP error: {str(e)}'
    except Exception as e:
        return False, f'Email error: {str(e)}'


def test_smtp_connection(smtp_config):
    """Test SMTP connection without sending. Returns (success, error)."""
    try:
        port = int(smtp_config['port'])
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(smtp_config['host'], port, context=context, timeout=10) as server:
                if smtp_config['user'] and smtp_config['password']:
                    server.login(smtp_config['user'], smtp_config['password'])
        else:
            with smtplib.SMTP(smtp_config['host'], port, timeout=10) as server:
                server.starttls(context=context)
                if smtp_config['user'] and smtp_config['password']:
                    server.login(smtp_config['user'], smtp_config['password'])
        return True, None
    except Exception as e:
        return False, str(e)


# ======================== BACKEND: LOG-ONLY ========================

def send_via_log(to_email, subject, html_body):
    """Log the email instead of sending (development fallback)."""
    logger.info(f'[LOG EMAIL] To: {to_email} | Subject: {subject} | Body length: {len(html_body)}')
    return True, None


# ======================== UNIFIED SEND FUNCTION ========================

def get_smtp_config(db, user_id):
    """Fetch SMTP settings for a user from the database."""
    row = db.execute(
        'SELECT smtp_host, smtp_port, smtp_user, smtp_pass, smtp_from_email, smtp_from_name FROM users WHERE id=?',
        (user_id,)
    ).fetchone()
    if not row or not row['smtp_host'] or not row['smtp_from_email']:
        return None
    return {
        'host': row['smtp_host'],
        'port': row['smtp_port'] or 587,
        'user': row['smtp_user'],
        'password': row['smtp_pass'],
        'from_email': row['smtp_from_email'],
        'from_name': row['smtp_from_name'] or 'ChannelView'
    }


def send_email(smtp_config, to_email, subject, html_body):
    """Send email using the best available backend.

    Priority:
    1. SendGrid API (if SENDGRID_API_KEY is set in env)
    2. SMTP (if smtp_config is provided and valid)
    3. Log fallback (development only)
    """
    import os

    # Try SendGrid first if configured
    sg_key = os.environ.get('SENDGRID_API_KEY', '')
    if sg_key:
        from_email = os.environ.get('SENDGRID_FROM_EMAIL', '')
        from_name = os.environ.get('SENDGRID_FROM_NAME', 'ChannelView')
        if not from_email and smtp_config:
            from_email = smtp_config.get('from_email', '')
            from_name = smtp_config.get('from_name', 'ChannelView')
        if from_email:
            success, error = send_via_sendgrid(sg_key, from_email, from_name, to_email, subject, html_body)
            if success:
                return success, error
            logger.warning(f'SendGrid failed ({error}), falling back to SMTP')

    # Try SMTP
    if smtp_config and smtp_config.get('host'):
        return send_via_smtp(smtp_config, to_email, subject, html_body)

    # Log fallback (dev mode)
    env = os.environ.get('CHANNELVIEW_ENV') or os.environ.get('FLASK_ENV', 'development')
    if env in ('development', 'staging'):
        return send_via_log(to_email, subject, html_body)

    return False, 'No email backend configured. Set up SMTP in Settings or set SENDGRID_API_KEY.'


# ======================== EMAIL TEMPLATES ========================

def _base_template(brand_color, agency_name, content_html):
    """Wrap content in a branded email shell."""
    return f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:32px 16px">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06)">
  <tr><td style="background:#111111;padding:24px 32px;text-align:center">
    <span style="color:{brand_color};font-size:22px;font-weight:800;letter-spacing:-0.5px">C</span>
    <span style="color:#ffffff;font-size:18px;font-weight:700;margin-left:6px">{agency_name}</span>
  </td></tr>
  <tr><td style="padding:32px">
    {content_html}
  </td></tr>
  <tr><td style="background:#f9fafb;padding:20px 32px;text-align:center;border-top:1px solid #e5e7eb">
    <p style="margin:0;color:#9ca3af;font-size:12px">Powered by <span style="color:{brand_color};font-weight:600">ChannelView</span></p>
    <p style="margin:4px 0 0;color:#9ca3af;font-size:11px">Async video interviews for modern hiring</p>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>'''


def build_invite_email(candidate_name, interview_title, interview_link, agency_name, brand_color, welcome_msg=None):
    """Build the invitation email HTML."""
    welcome = welcome_msg or "We'd like to invite you to complete a short video interview."
    content = f'''
    <h1 style="margin:0 0 8px;font-size:22px;color:#111">You're Invited!</h1>
    <p style="color:#6b7280;font-size:15px;margin:0 0 24px;line-height:1.5">
      Hi {candidate_name},<br><br>
      {welcome}
    </p>
    <div style="background:#f9fafb;border-radius:8px;padding:16px 20px;margin-bottom:24px">
      <p style="margin:0 0 4px;font-size:13px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Interview</p>
      <p style="margin:0;font-size:16px;color:#111;font-weight:600">{interview_title}</p>
    </div>
    <p style="color:#6b7280;font-size:14px;line-height:1.6;margin:0 0 24px">
      This is a one-way video interview â you'll record your responses on your own time, at your own pace.
      All you need is a device with a camera and microphone.
    </p>
    <div style="text-align:center;margin:28px 0">
      <a href="{interview_link}" style="display:inline-block;background:{brand_color};color:#000;font-weight:700;
         font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none;letter-spacing:0.3px">
        Start Your Interview
      </a>
    </div>
    <p style="color:#9ca3af;font-size:12px;text-align:center;margin:0">
      If the button doesn't work, copy and paste this link:<br>
      <a href="{interview_link}" style="color:{brand_color};word-break:break-all">{interview_link}</a>
    </p>'''
    return _base_template(brand_color, agency_name, content)


def build_reminder_email(candidate_name, interview_title, interview_link, agency_name, brand_color, status='invited'):
    """Build the reminder email HTML."""
    if status == 'in_progress':
        headline = "Don't Forget to Finish!"
        message = f"You've started your interview for <strong>{interview_title}</strong> but haven't completed it yet. Your progress has been saved â pick up right where you left off."
        button_text = "Continue Your Interview"
    else:
        headline = "Friendly Reminder"
        message = f"We noticed you haven't started your video interview for <strong>{interview_title}</strong> yet. It only takes a few minutes!"
        button_text = "Start Your Interview"

    content = f'''
    <h1 style="margin:0 0 8px;font-size:22px;color:#111">{headline}</h1>
    <p style="color:#6b7280;font-size:15px;margin:0 0 24px;line-height:1.5">
      Hi {candidate_name},<br><br>
      {message}
    </p>
    <div style="text-align:center;margin:28px 0">
      <a href="{interview_link}" style="display:inline-block;background:{brand_color};color:#000;font-weight:700;
         font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none;letter-spacing:0.3px">
        {button_text}
      </a>
    </div>
    <p style="color:#9ca3af;font-size:12px;text-align:center;margin:0">
      If the button doesn't work, copy and paste this link:<br>
      <a href="{interview_link}" style="color:{brand_color};word-break:break-all">{interview_link}</a>
    </p>'''
    return _base_template(brand_color, agency_name, content)


def build_completion_email(candidate_name, interview_title, agency_name, brand_color, thank_you_msg=None):
    """Build the completion confirmation email HTML."""
    thanks = thank_you_msg or "Thank you for completing your interview! We will review your responses and be in touch soon."
    content = f'''
    <div style="text-align:center;margin-bottom:24px">
      <div style="display:inline-block;width:56px;height:56px;border-radius:50%;background:{brand_color}20;
           line-height:56px;font-size:28px">&#10003;</div>
    </div>
    <h1 style="margin:0 0 8px;font-size:22px;color:#111;text-align:center">Interview Complete!</h1>
    <p style="color:#6b7280;font-size:15px;margin:0 0 24px;line-height:1.5;text-align:center">
      Hi {candidate_name},<br><br>
      {thanks}
    </p>
    <div style="background:#f9fafb;border-radius:8px;padding:16px 20px;margin-bottom:24px;text-align:center">
      <p style="margin:0 0 4px;font-size:13px;color:#9ca3af;font-weight:600;text-transform:uppercase;letter-spacing:0.5px">Interview</p>
      <p style="margin:0;font-size:16px;color:#111;font-weight:600">{interview_title}</p>
    </div>
    <p style="color:#6b7280;font-size:14px;line-height:1.6;margin:0;text-align:center">
      Our team will review your responses carefully. We'll reach out with next steps as soon as possible.
    </p>'''
    return _base_template(brand_color, agency_name, content)


def build_branded_email(candidate_name, interview_title, message_text, action_link, button_text, agency_name, brand_color):
    """Build a generic branded email with a custom message and CTA button. Used by waterfall engine."""
    content = f'''
    <h1 style="margin:0 0 8px;font-size:22px;color:#111">{interview_title}</h1>
    <p style="color:#6b7280;font-size:15px;margin:0 0 24px;line-height:1.5">
      Hi {candidate_name},<br><br>
      {message_text}
    </p>
    <div style="text-align:center;margin:28px 0">
      <a href="{action_link}" style="display:inline-block;background:{brand_color};color:#000;font-weight:700;
         font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none;letter-spacing:0.3px">
        {button_text}
      </a>
    </div>
    <p style="color:#9ca3af;font-size:12px;text-align:center;margin:0">
      If the button doesn't work, copy and paste this link:<br>
      <a href="{action_link}" style="color:{brand_color};word-break:break-all">{action_link}</a>
    </p>'''
    return _base_template(brand_color, agency_name, content)
