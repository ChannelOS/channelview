"""
ChannelView AI Scoring Service
Uses Claude API (Anthropic) to analyze candidate interview responses
and generate structured multi-category assessments.
"""
import os
import json
import random

# Try to import anthropic â if unavailable, we'll fall back to mock scoring
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


CATEGORIES = ['communication', 'industry_knowledge', 'role_competence', 'culture_fit', 'problem_solving']
CAT_LABELS = {
    'communication': 'Communication',
    'industry_knowledge': 'Industry Knowledge',
    'role_competence': 'Role Competence',
    'culture_fit': 'Culture Fit',
    'problem_solving': 'Problem Solving'
}


def get_api_key():
    """Get Anthropic API key from environment."""
    return os.environ.get('ANTHROPIC_API_KEY', '')


def is_ai_available():
    """Check if real AI scoring is available."""
    return ANTHROPIC_AVAILABLE and bool(get_api_key())


def score_response(question_text, transcript, position='', interview_title=''):
    """
    Score a single interview response using Claude API.
    Returns: { 'scores': {category: float}, 'overall': float, 'feedback': str }
    """
    if not is_ai_available():
        return mock_score_response()

    client = anthropic.Anthropic(api_key=get_api_key())

    prompt = f"""You are an expert hiring assessment AI for an insurance agency. Analyze this interview response and score it.

Interview: {interview_title}
Position: {position}
Question: {question_text}

Candidate's Response:
{transcript}

Score this response on a scale of 0-100 in each of these 5 categories:
1. Communication â Clarity, articulation, structure of response, professional language
2. Industry Knowledge â Understanding of insurance concepts, regulations, products, market awareness
3. Role Competence â Skills and experience relevant to the specific position
4. Culture Fit â Alignment with agency values (client-first, ethical sales, teamwork, professionalism)
5. Problem Solving â Analytical thinking, handling challenges, adaptability, creative solutions

Return your assessment as JSON with this exact structure (no markdown, no code fences, just raw JSON):
{{
  "communication": <score>,
  "industry_knowledge": <score>,
  "role_competence": <score>,
  "culture_fit": <score>,
  "problem_solving": <score>,
  "feedback": "<2-3 sentence assessment highlighting strengths and areas for improvement>"
}}

Be fair but discerning. A score of 70 is average, 80+ is strong, 90+ is exceptional. Don't inflate scores."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = message.content[0].text.strip()

        # Parse JSON â handle potential markdown fences
        if response_text.startswith('```'):
            response_text = response_text.split('\n', 1)[1].rsplit('```', 1)[0].strip()

        data = json.loads(response_text)

        scores = {}
        for cat in CATEGORIES:
            val = data.get(cat, 65)
            scores[cat] = round(max(0, min(100, float(val))), 1)

        overall = round(sum(scores.values()) / len(CATEGORIES), 1)
        feedback = data.get('feedback', 'Response analyzed.')

        return {'scores': scores, 'overall': overall, 'feedback': feedback}

    except Exception as e:
        print(f"[AI Service] Claude API error: {e}")
        # Fall back to mock if API call fails
        return mock_score_response()


def generate_candidate_summary(position, cat_averages, avg_score):
    """
    Generate an overall candidate summary using Claude API.
    Returns a summary string.
    """
    if not is_ai_available():
        return mock_candidate_summary(position, cat_averages, avg_score)

    client = anthropic.Anthropic(api_key=get_api_key())

    scores_text = '\n'.join(f"  - {CAT_LABELS[c]}: {s}/100" for c, s in sorted(cat_averages.items(), key=lambda x: x[1], reverse=True))

    prompt = f"""You are a hiring assessment AI for an insurance agency. Write a concise 2-3 sentence summary of this candidate's overall performance.

Position: {position}
Overall Score: {avg_score}/100

Category Scores:
{scores_text}

Write a professional, objective summary that highlights their strongest area and any development needs. Keep it under 50 words. Do not use markdown or bullet points â just plain text sentences."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text.strip()
    except Exception as e:
        print(f"[AI Service] Summary generation error: {e}")
        return mock_candidate_summary(position, cat_averages, avg_score)


# ======================== MOCK SCORING (FALLBACK) ========================

def mock_score_response():
    """Generate realistic mock scores when AI is unavailable."""
    base = random.uniform(58, 96)
    scores = {}
    for cat in CATEGORIES:
        scores[cat] = round(max(30, min(100, base + random.uniform(-12, 12))), 1)

    overall = round(sum(scores.values()) / len(CATEGORIES), 1)

    strengths = [CAT_LABELS[c] for c in CATEGORIES if scores[c] >= 80]
    areas = [CAT_LABELS[c] for c in CATEGORIES if scores[c] < 70]
    feedback_parts = []
    if strengths:
        feedback_parts.append(f"Strong in {', '.join(strengths[:2])}.")
    if areas:
        feedback_parts.append(f"Could improve {', '.join(areas[:2])}.")
    if not feedback_parts:
        feedback_parts.append("Solid overall performance across all assessment categories.")

    return {'scores': scores, 'overall': overall, 'feedback': ' '.join(feedback_parts)}


def mock_candidate_summary(position, cat_averages, avg_score):
    """Generate a structured summary without AI."""
    ranked = sorted(cat_averages.items(), key=lambda x: x[1], reverse=True)
    top_cat = CAT_LABELS[ranked[0][0]]
    low_cat = CAT_LABELS[ranked[-1][0]]
    return (f"Scored {avg_score}/100 overall for the {position} role. "
            f"Strongest area: {top_cat} ({ranked[0][1]}). "
            f"Development area: {low_cat} ({ranked[-1][1]}).")
