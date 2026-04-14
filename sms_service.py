"""
ChannelView — SMS Service (Cycle 38)
Twilio-powered SMS for candidate communication.
Handles sending, receiving webhooks, opt-out compliance, and conversation history.
"""
import os
import json
import logging
import urllib.request
import urllib.error
import base64

logger = logging.getLogger(__name__)

# Twilio credentials from environment
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_DEFAULT_NUMBER = os.environ.get('TWILIO_DEFAULT_NUMBER', '')


def is_configured():
    """Check if Twilio credentials are set."""
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)


def send_sms(to_number, body, from_number=None):
    """Send an SMS via Twilio REST API.

    Args:
        to_number: Recipient phone number (E.164 format preferred, e.g., +15551234567)
        body: Message body (max 1600 chars for SMS)
        from_number: Sender phone number (defaults to TWILIO_DEFAULT_NUMBER)

    Returns:
        (success: bool, sid_or_error: str)
    """
    if not is_configured():
        logger.warning("Twilio not configured — SMS not sent")
        return False, "Twilio credentials not configured"

    from_num = from_number or TWILIO_DEFAULT_NUMBER
    if not from_num:
        return False, "No Twilio phone number configured"

    # Normalize phone number
    to_num = normalize_phone(to_number)
    if not to_num:
        return False, "Invalid phone number"

    # Truncate body if too long
    if len(body) > 1600:
        body = body[:1597] + '...'

    try:
        url = f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json'

        # URL-encode the form data
        data = urllib.parse.urlencode({
            'To': to_num,
            'From': from_num,
            'Body': body
        }).encode('utf-8')

        # Basic auth header
        credentials = base64.b64encode(
            f'{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}'.encode()
        ).decode()

        req = urllib.request.Request(url, data=data, headers={
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/x-www-form-urlencoded'
        })

        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        sid = result.get('sid', '')
        status = result.get('status', '')
        logger.info(f"SMS sent: {sid} status={status} to={to_num}")
        return True, sid

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='ignore')[:300]
        logger.error(f"Twilio HTTP error {e.code}: {error_body}")
        return False, f"Twilio error {e.code}: {error_body}"
    except Exception as e:
        logger.error(f"SMS send failed: {e}")
        return False, str(e)


def normalize_phone(phone):
    """Normalize a phone number to E.164 format for US numbers.

    Handles: (555) 123-4567, 555-123-4567, 5551234567, +15551234567
    Returns None for invalid numbers.
    """
    if not phone:
        return None

    import re
    # Strip everything except digits and leading +
    digits = re.sub(r'[^\d+]', '', phone)

    # If it already starts with +, validate length
    if digits.startswith('+'):
        if len(digits) >= 11:
            return digits
        return None

    # Strip leading 1 for US numbers
    if digits.startswith('1') and len(digits) == 11:
        return f'+{digits}'
    elif len(digits) == 10:
        return f'+1{digits}'

    return None


def check_opt_out(db, phone_number):
    """Check if a phone number has opted out of SMS.

    Returns True if opted out (do NOT send).
    """
    normalized = normalize_phone(phone_number)
    if not normalized:
        return True  # Can't send to invalid numbers

    result = db.execute("SELECT id FROM sms_opt_outs WHERE phone_number=?", (normalized,)).fetchone()
    return result is not None


def handle_opt_out(db, phone_number):
    """Record a STOP/opt-out request. Returns True if newly opted out."""
    import uuid
    normalized = normalize_phone(phone_number)
    if not normalized:
        return False

    # Check if already opted out
    existing = db.execute("SELECT id FROM sms_opt_outs WHERE phone_number=?", (normalized,)).fetchone()
    if existing:
        return False

    db.execute("INSERT INTO sms_opt_outs (id, phone_number) VALUES (?, ?)",
               (str(uuid.uuid4()), normalized))
    db.commit()
    return True


def handle_opt_in(db, phone_number):
    """Remove an opt-out (START request). Returns True if resubscribed."""
    normalized = normalize_phone(phone_number)
    if not normalized:
        return False

    result = db.execute("DELETE FROM sms_opt_outs WHERE phone_number=?", (normalized,))
    db.commit()
    return result.rowcount > 0 if hasattr(result, 'rowcount') else True


def fill_template(template_body, candidate_data, agency_data=None):
    """Fill SMS template placeholders with candidate/agency data.

    Supported placeholders: {name}, {first_name}, {last_name}, {agency}, {link}, {message}
    """
    replacements = {
        '{name}': f"{candidate_data.get('first_name', '')} {candidate_data.get('last_name', '')}".strip(),
        '{first_name}': candidate_data.get('first_name', ''),
        '{last_name}': candidate_data.get('last_name', ''),
        '{link}': candidate_data.get('interview_link', ''),
        '{agency}': (agency_data or {}).get('agency_name', ''),
        '{message}': candidate_data.get('custom_message', ''),
    }

    result = template_body
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)

    return result
