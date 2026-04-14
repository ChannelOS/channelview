"""
ChannelView — Resume Parsing Service (Cycle 38)
Uses Anthropic Claude API to extract structured data from resumes.
Handles PDF and DOCX text extraction, then sends to AI for parsing.
"""
import os
import json
import logging

logger = logging.getLogger(__name__)

# Anthropic API key from environment
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ======================== TEXT EXTRACTION ========================

def extract_text_from_pdf(file_path):
    """Extract text from a PDF file using pdfplumber (preferred) or PyPDF2 fallback."""
    text = ''
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + '\n'
        if text.strip():
            return text.strip()
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"pdfplumber failed: {e}")

    # Fallback to PyPDF2
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(file_path)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + '\n'
        return text.strip()
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"PyPDF2 failed: {e}")

    return text.strip()


def extract_text_from_docx(file_path):
    """Extract text from a DOCX file using python-docx."""
    try:
        from docx import Document
        doc = Document(file_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return '\n'.join(paragraphs)
    except ImportError:
        logger.warning("python-docx not installed — cannot parse DOCX")
        return ''
    except Exception as e:
        logger.warning(f"DOCX extraction failed: {e}")
        return ''


def extract_text_from_file(file_path):
    """Extract text from a resume file (PDF or DOCX)."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.pdf':
        return extract_text_from_pdf(file_path)
    elif ext in ('.docx', '.doc'):
        return extract_text_from_docx(file_path)
    elif ext == '.txt':
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read().strip()
        except Exception:
            return ''
    return ''


# ======================== AI PARSING ========================

RESUME_PARSE_PROMPT = """You are a resume parser for an insurance recruiting platform. Extract the following from this resume text and return JSON only (no markdown, no explanation):

{
  "summary": "One sentence summary of the candidate — highlight years of experience, relevant industry, and career trajectory. Example: '8 years retail management, customer-facing roles, seeking career change to financial services'",
  "experience": [
    "Most recent/relevant role — Company — Duration — key achievement",
    "Second role — Company — Duration — key achievement",
    "Third role (if notable) — Company — Duration"
  ],
  "skills": ["skill1", "skill2", "skill3", "skill4", "skill5"],
  "insurance_signals": ["Any insurance-related signals: P&C license, Life/Health license, Series 6/7/63/65, B2B sales, financial services experience, compliance, etc."]
}

IMPORTANT:
- "experience" should be 3-5 bullet strings, most recent first
- "skills" should be 3-7 concrete skills (not generic like "hard worker")
- "insurance_signals" should list any insurance/financial services indicators. If none found, return empty array.
- For the summary, prioritize: licensed insurance professional > years in sales/management > customer-facing experience > education
- Keep the summary under 120 characters
- Return ONLY valid JSON, no markdown code fences

RESUME TEXT:
"""


def parse_resume_with_ai(resume_text):
    """Send resume text to Claude API and get structured data back.

    Returns dict with keys: summary, experience, skills, insurance_signals
    Returns None if parsing fails.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping AI resume parsing")
        return None

    if not resume_text or len(resume_text.strip()) < 50:
        logger.warning("Resume text too short for meaningful parsing")
        return None

    # Truncate very long resumes to save tokens
    if len(resume_text) > 8000:
        resume_text = resume_text[:8000] + "\n... [truncated]"

    try:
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 500,
            "messages": [{
                "role": "user",
                "content": RESUME_PARSE_PROMPT + resume_text
            }]
        })

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload.encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01'
            }
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        # Extract the text content from Claude's response
        content_text = ''
        for block in result.get('content', []):
            if block.get('type') == 'text':
                content_text += block['text']

        if not content_text:
            logger.warning("Empty response from Claude API")
            return None

        # Parse the JSON response — handle potential markdown fences
        clean = content_text.strip()
        if clean.startswith('```'):
            # Remove markdown code fences
            lines = clean.split('\n')
            lines = [l for l in lines if not l.strip().startswith('```')]
            clean = '\n'.join(lines)

        parsed = json.loads(clean)

        # Validate expected keys
        result_data = {
            'summary': str(parsed.get('summary', ''))[:200],
            'experience': parsed.get('experience', [])[:5],
            'skills': parsed.get('skills', [])[:10],
            'insurance_signals': parsed.get('insurance_signals', [])
        }

        # Convert lists to JSON strings for storage
        result_data['experience_json'] = json.dumps(result_data['experience'])
        result_data['skills_json'] = json.dumps(result_data['skills'])

        return result_data

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response as JSON: {e}")
        return None
    except urllib.error.HTTPError as e:
        logger.error(f"Claude API HTTP error: {e.code} — {e.read().decode('utf-8', errors='ignore')[:200]}")
        return None
    except Exception as e:
        logger.error(f"Resume parsing failed: {e}")
        return None


def parse_resume_file(file_path):
    """Full pipeline: extract text from file, then parse with AI.

    Returns dict with keys: summary, experience_json, skills_json, insurance_signals
    Returns None if extraction or parsing fails.
    """
    text = extract_text_from_file(file_path)
    if not text:
        logger.warning(f"No text extracted from {file_path}")
        return None

    return parse_resume_with_ai(text)


# ======================== JOB DESCRIPTION GENERATOR ========================

JOB_DESC_PROMPT = """You are a job description writer for insurance recruiting. Write a compelling, candidate-friendly job description for the following role. The description should:

1. Be 150-250 words
2. Emphasize: flexibility, income potential, career growth, helping families/communities
3. AVOID: "sales", "commission-only", "cold calling" — instead use "advisor", "income potential", "outreach"
4. Include 3-4 bullet points for "What You'll Do" and 3-4 for "What We Offer"
5. Sound warm and inviting, not corporate
6. Be appropriate for insurance/financial services recruiting
7. Do NOT include any discriminatory language (age, gender, race, religion, etc.)

Return ONLY the job description text — no JSON, no markdown fences, no headers like "Job Description:". Start directly with the opening paragraph.

ROLE DETAILS:
- Title: {title}
- Company/Agency: {agency_name}
- Location: {location}
- Type: {job_type}
"""


def generate_job_description(title, agency_name='', location='', job_type='Full-time'):
    """Generate an AI-powered job description for insurance recruiting.

    Returns the description string, or None on failure.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — cannot generate job description")
        return None

    prompt = JOB_DESC_PROMPT.format(
        title=title or 'Benefits Advisor',
        agency_name=agency_name or 'Our Agency',
        location=location or 'Flexible / Remote',
        job_type=job_type or 'Full-time'
    )

    try:
        import urllib.request
        import urllib.error

        payload = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 600,
            "messages": [{
                "role": "user",
                "content": prompt
            }]
        })

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload.encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01'
            }
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        content_text = ''
        for block in result.get('content', []):
            if block.get('type') == 'text':
                content_text += block['text']

        return content_text.strip() if content_text else None

    except Exception as e:
        logger.error(f"Job description generation failed: {e}")
        return None
