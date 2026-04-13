"""
Seed script: Pre-fill a new RSC account with ready-to-use content.
Run inside the Docker container:
  docker exec channelview-app python3 seed_rsc_defaults.py matherly@us.aflac.com

Creates:
  - 3 interview templates with real insurance recruiting questions
  - 4 email templates (invite, reminder, thank you, follow-up)
  - Sets smart defaults (auto-reminders, AI scoring on, etc.)
"""

import sys
import uuid
from datetime import datetime
from database import get_db


def seed_interviews(db, user_id):
    """Create 3 ready-to-go interview templates with questions."""

    templates = [
        {
            'title': 'Licensed Insurance Agent',
            'description': 'Standard interview for licensed P&C or life/health agents. Covers sales ability, client relationships, and industry knowledge.',
            'department': 'Sales',
            'position': 'Licensed Insurance Agent',
            'questions': [
                "Tell us about yourself and why you're interested in a career in insurance.",
                "Describe a time you helped a client understand a policy or coverage option. How did you break it down for them?",
                "How do you handle it when a prospect says 'I need to think about it' or 'I already have an agent'?",
                "Walk us through how you would approach a new territory or client list. What's your process for building relationships?",
                "What does great customer service look like to you in the insurance business?",
            ]
        },
        {
            'title': 'Entry-Level Agent (No License Yet)',
            'description': 'For candidates new to insurance who are interested in getting licensed. Focuses on work ethic, motivation, and coachability.',
            'department': 'Recruiting',
            'position': 'Agent Trainee',
            'questions': [
                "Tell us a bit about your background and what drew you to the insurance industry.",
                "This role involves a lot of self-directed work — setting your own schedule, making your own calls. How have you stayed motivated in past roles?",
                "Describe a situation where you had to learn something new quickly. How did you approach it?",
                "How comfortable are you with talking to people you don't know? Give us an example.",
                "Where do you see yourself in 2-3 years if this opportunity works out?",
            ]
        },
        {
            'title': 'Team Lead / Senior Agent',
            'description': 'For experienced agents being considered for leadership or mentoring roles. Covers management style, training ability, and results.',
            'department': 'Management',
            'position': 'Team Lead',
            'questions': [
                "Tell us about your experience in insurance and what's driven your success.",
                "How would you describe your approach to mentoring or training newer agents?",
                "Describe a time you helped a struggling team member improve their performance.",
                "What metrics do you track to measure your own success, and how would you apply that to a team?",
                "What's the biggest challenge facing insurance agents right now, and how would you help your team navigate it?",
            ]
        },
    ]

    created = []
    for t in templates:
        iid = str(uuid.uuid4())
        db.execute(
            """INSERT INTO interviews (id, user_id, title, description, department, position,
               thinking_time, max_answer_time, max_retakes, status, welcome_msg, thank_you_msg, brand_color,
               intro_type, intro_template_id, intro_video_path, interest_rating_enabled, interest_prompt)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (iid, user_id, t['title'], t['description'], t['department'], t['position'],
             30, 120, 1, 'active',
             "Welcome! This interview has a few short video questions. Take your time, be yourself, and don't worry about being perfect — we just want to get to know you.",
             "Thank you for completing your interview! We'll review your responses and be in touch soon. If you have any questions in the meantime, don't hesitate to reach out.",
             '#0ace0a',
             'template', 'intro_opportunity', '/static/intros/opportunity.html', 1,
             "Now that you've learned a little about this opportunity, how interested are you in having a conversation to learn more?")
        )
        for idx, q in enumerate(t['questions']):
            db.execute(
                "INSERT INTO questions (id, interview_id, question_text, question_order, thinking_time, max_answer_time) VALUES (%s,%s,%s,%s,%s,%s)",
                (str(uuid.uuid4()), iid, q, idx + 1, 30, 120)
            )
        created.append(t['title'])
        print(f"  Created interview: {t['title']} ({len(t['questions'])} questions)")

    return created


def seed_email_templates(db, user_id):
    """Create 4 ready-to-use email templates."""

    templates = [
        {
            'type': 'invitation',
            'name': 'Interview Invitation',
            'subject': "{{interviewer_name}} with {{agency_name}} — I'd love to learn more about you",
            'body': """<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
<p>Hi {{candidate_name}},</p>

<p>I came across your background and something about it caught my eye. I think you might be a great fit for an opportunity on my team, and I'd love to learn a little more about you.</p>

<p>I've put together a short video interview — just a few questions so I can get a feel for who you are. It's casual, there are no wrong answers, and it only takes about 10-15 minutes.</p>

<p><strong>Here's how it works:</strong></p>
<ul style="color:#444;line-height:1.8">
  <li>Click the link below when you're ready</li>
  <li>You'll see each question one at a time and record a short video answer</li>
  <li>Do it from your phone or computer — whenever works for you</li>
  <li>No scheduling, no pressure — just be yourself</li>
</ul>

<p style="text-align:center;margin:28px 0">
  <a href="{{interview_url}}" style="display:inline-block;background:#0ace0a;color:#000;font-weight:700;font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none">Start Your Interview</a>
</p>

<p>I'm really looking forward to hearing from you.</p>

<p>{{interviewer_name}}<br>{{agency_name}}</p>
</div>""",
            'is_default': 1,
            'variables': '["candidate_name","agency_name","interview_url","interviewer_name","position"]'
        },
        {
            'type': 'reminder',
            'name': 'Friendly Nudge',
            'subject': "Still interested? I'd hate for you to miss this",
            'body': """<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
<p>Hi {{candidate_name}},</p>

<p>I wanted to follow up — I sent over a short video interview a few days ago and haven't heard back yet. Totally understand if the timing wasn't right, but I didn't want you to miss the opportunity.</p>

<p>It only takes about 10-15 minutes, and you can do it right from your phone whenever you have a few minutes.</p>

<p style="text-align:center;margin:28px 0">
  <a href="{{interview_url}}" style="display:inline-block;background:#0ace0a;color:#000;font-weight:700;font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none">Complete Your Interview</a>
</p>

<p>If you have any questions at all, just hit reply — I'm happy to chat.</p>

<p>{{interviewer_name}}<br>{{agency_name}}</p>
</div>""",
            'is_default': 1,
            'variables': '["candidate_name","agency_name","interview_url","interviewer_name","days_remaining"]'
        },
        {
            'type': 'completion',
            'name': 'Thank You - Interview Complete',
            'subject': "Got it — thanks so much, {{candidate_name}}!",
            'body': """<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
<p>Hi {{candidate_name}},</p>

<p>I just received your video interview — thank you for taking the time to do that. I really appreciate it.</p>

<p>I'm going to review your responses over the next few days. If it looks like we're a good match, I'll reach out to set up a time for us to talk and I can tell you more about the opportunity.</p>

<p>In the meantime, feel free to reply if anything comes to mind — I'm always happy to answer questions.</p>

<p>Talk soon,<br>{{interviewer_name}}<br>{{agency_name}}</p>
</div>""",
            'is_default': 1,
            'variables': '["candidate_name","agency_name","interviewer_name","position"]'
        },
        {
            'type': 'share_report',
            'name': 'Candidate Report - Internal',
            'subject': "Candidate Report: {{candidate_name}} for {{position}}",
            'body': """<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
<p>Hi {{recipient_name}},</p>

<p>I'm sharing a candidate report for your review:</p>

<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin:16px 0">
  <p style="margin:0"><strong>Candidate:</strong> {{candidate_name}}</p>
  <p style="margin:4px 0 0"><strong>Position:</strong> {{position}}</p>
</div>

<p style="text-align:center;margin:24px 0">
  <a href="{{report_url}}" style="display:inline-block;background:#0ace0a;color:#000;font-weight:700;font-size:15px;padding:12px 32px;border-radius:8px;text-decoration:none">View Full Report</a>
</p>

<p>Let me know what you think!</p>

<p>Best,<br>{{sender_name}}</p>
</div>""",
            'is_default': 1,
            'variables': '["candidate_name","position","report_url","sender_name","recipient_name"]'
        },
    ]

    for t in templates:
        db.execute(
            """INSERT INTO email_templates (id, user_id, template_type, name, subject, html_body, is_default, variables)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (str(uuid.uuid4()), user_id, t['type'], t['name'], t['subject'], t['body'], t['is_default'], t['variables'])
        )
        print(f"  Created email template: {t['name']}")


def seed_smart_defaults(db, user_id):
    """Set smart defaults so the RSC doesn't need to configure anything."""
    updates = {
        'auto_score_enabled': 1,
        'auto_advance_enabled': 0,
        'auto_reject_enabled': 0,
        'reminder_sequence_enabled': 1,
        'reminder_day_3': 1,
        'reminder_day_5': 1,
        'reminder_day_7': 1,
        'notify_interview_started': 1,
        'notify_interview_completed': 1,
        'notify_candidate_invited': 1,
        'brand_color': '#0ace0a',
    }
    for col, val in updates.items():
        try:
            db.execute(f"UPDATE users SET {col}=%s WHERE id=%s", (val, user_id))
        except Exception as e:
            print(f"  Warning: Could not set {col}: {e}")
    print("  Smart defaults applied (auto-score, reminders, notifications)")


def seed_help_articles(db):
    """Replace help articles with plain-language, task-based guides for RSCs."""

    # Clear existing articles
    db.execute("DELETE FROM help_articles")

    articles = [
        # GETTING STARTED
        ('how-to-sign-in', 'How do I sign in?', 'Getting Started',
         """<h3>Signing In</h3>
<p>Go to <strong>mychannelview.com</strong> and enter your email and password. If this is your first time, you'll be asked to set a new password and fill in your profile.</p>
<p><strong>Forgot your password?</strong> Click "Forgot Password" on the sign-in page and we'll email you a reset link.</p>
<p><strong>Tip:</strong> Bookmark mychannelview.com so you can get back quickly.</p>""",
         'dashboard', 1),

        ('what-is-channelview', 'What is ChannelView and what can it do?', 'Getting Started',
         """<h3>ChannelView in 30 Seconds</h3>
<p>ChannelView is a recruiting tool built for insurance agencies. Instead of scheduling phone screens with every candidate, you send them a link. They record video answers to your questions on their own time. You watch the responses when it's convenient for you.</p>
<p><strong>What you can do:</strong></p>
<p>Send video interviews to candidates without scheduling calls. Watch and compare responses side-by-side. Get AI scoring to help you spot the best candidates faster. Track everyone in a simple pipeline from "invited" to "hired." Share candidate reports with your team or upline.</p>
<p><strong>The big benefit:</strong> You save hours of phone screening time, and candidates can interview anytime — evenings, weekends, whenever works for them.</p>""",
         'dashboard', 2),

        ('first-interview', 'How do I send my first interview?', 'Getting Started',
         """<h3>Send Your First Interview in 3 Steps</h3>
<p><strong>Step 1: Pick a template.</strong> Go to <strong>Interviews</strong> in the sidebar. You'll see ready-made templates for different roles (Licensed Agent, Entry-Level, Team Lead). Click one to open it.</p>
<p><strong>Step 2: Add a candidate.</strong> Click "Add Candidate" and enter their name and email address. That's it.</p>
<p><strong>Step 3: Send the invite.</strong> Click "Send Invitation." The candidate gets an email with a link to record their video answers. You'll get notified when they're done.</p>
<p><strong>Tip:</strong> You can add multiple candidates to the same interview. They each get their own unique link.</p>""",
         'interviews', 3),

        # INTERVIEWS
        ('what-candidates-see', 'What does the candidate see?', 'Interviews',
         """<h3>The Candidate Experience</h3>
<p>When a candidate clicks their interview link, here's what happens:</p>
<p><strong>1. Welcome screen</strong> — They see your agency name and a friendly welcome message.</p>
<p><strong>2. Camera check</strong> — They test their camera and microphone (works on phones too).</p>
<p><strong>3. Questions</strong> — They see one question at a time. They get 30 seconds to think, then up to 2 minutes to record their answer. They can re-record if they want.</p>
<p><strong>4. Done</strong> — They see a thank-you message and you get notified.</p>
<p>Most candidates finish in 10-15 minutes. They can do it from any device with a camera — phone, tablet, or laptop.</p>""",
         'interviews', 4),

        ('customize-interview', 'Can I change the questions or add my own?', 'Interviews',
         """<h3>Customizing Your Interviews</h3>
<p>Absolutely. The templates are just a starting point.</p>
<p><strong>To edit questions:</strong> Open any interview, click the edit icon next to a question, and change the text. You can also reorder them by dragging.</p>
<p><strong>To add a question:</strong> Click "Add Question" at the bottom of the question list. Type your question and set the time limit.</p>
<p><strong>To delete a question:</strong> Click the trash icon next to any question you don't need.</p>
<p><strong>To create a brand new interview:</strong> Click "+ New Interview" at the top right of the Interviews page. Give it a title, add your questions, and you're set.</p>
<p><strong>Pro tip:</strong> Keep it to 5-7 questions max. Candidates drop off when interviews are too long.</p>""",
         'interviews', 5),

        ('track-candidates', 'How do I track who\'s done and who hasn\'t?', 'Candidates',
         """<h3>Tracking Candidates</h3>
<p>Go to <strong>Candidates</strong> in the sidebar. You'll see everyone in a list with their status:</p>
<p><strong>Invited</strong> — They got the email but haven't started yet.<br>
<strong>In Progress</strong> — They started recording but haven't finished.<br>
<strong>Completed</strong> — They finished all questions. Ready for you to review.<br>
<strong>Reviewed</strong> — You've watched their responses.<br>
<strong>Hired / Passed</strong> — Your final decision.</p>
<p>Click any candidate to watch their video responses, see their AI score, and make notes.</p>
<p><strong>Tip:</strong> Candidates who are "Invited" for more than 3 days will automatically get a reminder email (this is already turned on for you).</p>""",
         'candidates', 6),

        # AI FEATURES
        ('what-is-ai-scoring', 'What is AI scoring and how does it work?', 'AI Features',
         """<h3>AI Scoring — Your Assistant, Not Your Boss</h3>
<p>When a candidate finishes their interview, ChannelView's AI watches their responses and gives them a score from 0-100 across several areas:</p>
<p><strong>Communication</strong> — Are they clear and articulate?<br>
<strong>Industry Knowledge</strong> — Do they understand insurance basics?<br>
<strong>Professionalism</strong> — How do they present themselves?<br>
<strong>Problem Solving</strong> — Can they think on their feet?<br>
<strong>Cultural Fit</strong> — Would they mesh with your team?</p>
<p>The AI score is a starting point to help you prioritize who to review first. <strong>It's not a replacement for your judgment</strong> — always watch the videos yourself before making a decision.</p>
<p><strong>Tip:</strong> Candidates scoring 80+ are usually strong. Under 60 may need a closer look. But scores are just one data point.</p>""",
         'ai-scoring', 7),

        # EMAIL & COMMUNICATION
        ('email-not-received', 'My candidate says they didn\'t get the email', 'Troubleshooting',
         """<h3>Candidate Didn't Receive the Invitation</h3>
<p>This happens sometimes. Here's what to check:</p>
<p><strong>1. Check their spam/junk folder</strong> — Ask the candidate to look there first. The email comes from noreply@mychannelview.com.</p>
<p><strong>2. Verify the email address</strong> — Go to the candidate's profile and make sure you typed their email correctly.</p>
<p><strong>3. Resend the invitation</strong> — Click the "Resend" button on the candidate's card. This sends a fresh email.</p>
<p><strong>4. Share the link directly</strong> — Every candidate has a unique interview link. You can copy it and text or message it to them directly.</p>""",
         'candidates', 8),

        # PIPELINE & WORKFLOW
        ('pipeline-board', 'What is the Pipeline Board?', 'Your Candidates',
         """<h3>Pipeline Board — See Everyone at a Glance</h3>
<p>The Pipeline Board shows all your candidates organized in columns by status. Think of it like a visual to-do list for your hiring:</p>
<p><strong>Invited → In Progress → Completed → Reviewed → Hired/Passed</strong></p>
<p>You can drag candidates between columns as you make decisions. This makes it easy to see where everyone stands without clicking into each person.</p>
<p><strong>Tip:</strong> Focus on the "Completed" column each day — those are the people waiting for your review.</p>""",
         'kanban', 9),

        # SETTINGS & ACCOUNT
        ('change-password', 'How do I change my password?', 'My Account',
         """<h3>Changing Your Password</h3>
<p>Go to <strong>Settings</strong> in the sidebar, then look for the "Change Password" section. Enter your current password and your new one, then click Save.</p>
<p>Use something you'll remember but that's at least 8 characters long.</p>""",
         'settings', 10),

        ('billing-plans', 'What\'s included in my plan?', 'My Account',
         """<h3>Your Plan</h3>
<p>Go to <strong>Billing</strong> in the sidebar to see your current plan, what's included, and how much you've used this month.</p>
<p>You'll see meters for candidates, interviews, team seats, and storage. If you're approaching a limit, you'll see a warning.</p>
<p>To upgrade or change your plan, click "View Plans & Upgrade" on the billing page.</p>""",
         'billing', 11),

        ('getting-help', 'How do I get help?', 'Getting Started',
         """<h3>Getting Help</h3>
<p>You're in the right place! Search this Help Center for answers to common questions.</p>
<p>If you can't find what you need, reach out to your Channel One contact directly. We're here to help you get the most out of ChannelView.</p>
<p><strong>Tip:</strong> Many pages in ChannelView have contextual help — look for the "?" icon in the top right corner of any page.</p>""",
         'help-center', 12),
    ]

    for slug, title, category, content, page, order in articles:
        db.execute(
            "INSERT INTO help_articles (id, slug, title, category, content, related_page, sort_order, is_published) VALUES (%s,%s,%s,%s,%s,%s,%s,1)",
            (uuid.uuid4().hex, slug, title, category, content, page, order)
        )
    print(f"  Created {len(articles)} help articles (plain language, task-based)")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 seed_rsc_defaults.py <email>")
        sys.exit(1)

    email = sys.argv[1].strip().lower()
    db = get_db()

    # Find user
    user = db.execute("SELECT id, name, agency_name, plan FROM users WHERE email=%s", (email,)).fetchone()
    if not user:
        print(f"Error: No user found with email {email}")
        db.close()
        sys.exit(1)

    user = dict(user)
    print(f"\nSeeding account for: {user['name']} ({email})")
    print(f"Plan: {user['plan']}\n")

    # 1. Interview templates
    print("1. Creating interview templates...")
    seed_interviews(db, user['id'])

    # 2. Email templates
    print("\n2. Creating email templates...")
    seed_email_templates(db, user['id'])

    # 3. Smart defaults
    print("\n3. Setting smart defaults...")
    seed_smart_defaults(db, user['id'])

    # 4. Help articles (global — benefits everyone)
    print("\n4. Updating help articles (plain language)...")
    seed_help_articles(db)

    db.commit()
    db.close()
    print("\nDone! Account is ready to use.")


if __name__ == '__main__':
    main()
