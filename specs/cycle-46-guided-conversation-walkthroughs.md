# Cycle 46 — Guided Conversation Setup Walkthroughs

## Why

Today, the Cycle 40C Setup Wizard is a checklist with "Set Up" buttons that dispatch an
RSC to a separate full page (`/territory-setup`, `/jobs-manage`, `/settings`,
`/outreach-campaigns`). That's a form-first experience. The strategic direction (per
`ChannelView_Next_Cycle_Game_Plan.docx` + `ChannelView_Guided_Walkthrough_Plan.docx`,
both 2026-04-16) is: **"Every feature becomes a conversation, not a form."**

This cycle replaces the checklist behavior with inline, step-by-step guided
conversations. Each setup step opens as a focused panel with:
- One plain-language question per step
- A "what happens behind the scenes" side explaining what the AI agent does
  with the answer
- Skip / Back / Continue controls
- Progress saved after every step so RSCs can abandon and resume

The component (`WalkthroughPanel`) is intentionally reusable — Cycles 47+ will reuse
it for Campaign Launch, Pipeline walkthroughs, and AI Agent Rules.

## Scope (in)

Backend:
- `walkthrough_progress` table: one row per (user_id, flow_key) with JSON answers,
  current step index, status (not_started/in_progress/completed/skipped),
  started_at, completed_at
- `GET /api/walkthrough/flows` — returns the list of available flows + per-user
  status for each
- `GET /api/walkthrough/:flow_key` — returns saved progress for the user (for resume)
- `POST /api/walkthrough/:flow_key/answer` — save one step's answer, advance pointer
- `POST /api/walkthrough/:flow_key/complete` — finalize, apply answers to underlying
  system (e.g. write to `rsc_territories`, `jobs`, `users.notification_prefs`,
  `users.brand_color`)
- `POST /api/walkthrough/:flow_key/skip` — mark flow as skipped without completing
- `POST /api/walkthrough/:flow_key/reset` — reopen a completed flow (for editing later)

Four flows:
1. **territory** — "What zip codes do you cover?" → writes `rsc_territories` row
2. **first_job** — "What's the first job you're hiring for?" → writes `jobs` row
3. **notifications** — "How do you want to hear about new candidates?" → writes
   new `users.notification_prefs` JSON column (email / sms / agent-handles)
4. **brand** — "What's your agency's name and color?" → writes `users.agency_name`
   + `users.brand_color`

Frontend:
- New `WalkthroughPanel` component in `app.js`
- Replaces the "Set Up" dispatch buttons on the dashboard wizard with "Start
  walkthrough" that opens the panel inline (no page navigation)
- Reusable shape: `WalkthroughPanel.open(flowKey, { onComplete })`
- Renders as a modal-style focused panel; each step shows question + agent-side
  explanation + input + Back / Skip / Continue
- Pulls saved progress on open so the RSC resumes mid-flow
- After last step, calls `/complete` and shows a "you're all set" moment

Diagnostic:
- `diagnostic_c46.py` — covers table creation, all four flows (status → answer →
  complete), progress persistence, resume, skip, reset, side-effect verification
  (territory/job/notification/brand row gets written), auth + RBAC, and page load.

## Scope (out, for later cycles)

- The AI agent actually *acting* on the answers (Cycle 47+ with SendGrid/Twilio
  identity)
- Channel Careers landing page (needs channelcareers.com domain — Joe's task)
- Campaign Launch walkthrough (uses the same component — Cycle 47)
- Pipeline / Candidate walkthroughs (Cycle 48)
- AI Agent Rules walkthrough (Cycle 49)

## Flow definitions

Flows live as a JSON-like structure on the backend so we can add more without
touching frontend. Each flow has: key, title, description, agent_summary,
completion_action, and a list of steps. Each step has: key, question,
agent_does (plain-language explanation of what the agent does with the answer),
input (type, placeholder, help), optional, validator.

### Flow: territory
1. **zips** — "What zip codes do you cover?" (textarea, comma-separated)
   - Agent does: "I'll only reach out to candidates in these zips — no cold-
     calling people you can't serve."
2. **center** — "What's the zip you're based out of?" (text, 5 digits)
   - Agent does: "I'll use this as the radius center for nearby-candidate
     prioritization."
3. **radius** — "How far are you willing to travel?" (number, miles, default 25)
   - Agent does: "I'll widen or tighten the search to this radius when zip list
     isn't enough."

### Flow: first_job
1. **title** — "What's the job title?" (text)
2. **summary** — "One sentence: what's this role about?" (textarea)
   - Agent does: "I'll turn this into the public job description + the AI email
     subject line."
3. **pay_style** — "How do these producers get paid?" (select: commission /
   salary + commission / salary)
   - Agent does: "This shows up on the apply page and shapes the screening
     script."

### Flow: notifications
1. **email_mode** — "How do you want to hear about new candidates?"
   (radio: every candidate / daily digest / only shortlisted / never)
2. **sms_enabled** — "Text me when a candidate gets shortlisted?" (yes/no)
3. **agent_handles** — "Want the agent to answer candidate questions without
   pinging you?" (yes/no)
   - Agent does: "I'll reply to candidate FAQs, scheduling requests, and basic
     status checks. You get a ping when there's a real decision to make."

### Flow: brand
1. **agency_name** — "What should we call your agency on emails and the apply
   page?" (text)
2. **brand_color** — "Pick a primary color for your emails and apply page."
   (color, default #0ace0a)
3. **tone** — "What tone do you want in outreach?" (radio: warm / professional /
   direct)
   - Agent does: "I'll use this tone in every email, SMS, and scheduling
     message I send on your behalf."

## Files touched

- `specs/cycle-46-guided-conversation-walkthroughs.md` (new, this file)
- `database.py` — new `walkthrough_progress` table + `users.notification_prefs`
  migration
- `app.py` — 6 new endpoints under `_c46` suffix + `FLOWS_C46` definitions
- `static/js/app.js` — WalkthroughPanel component + flow launcher + dashboard
  wizard rewrite
- `static/css/app.css` — walkthrough panel styles
- `diagnostic_c46.py` — test suite
- `generate_report_c46.js` — branded DOCX report

## Acceptance (for the diagnostic)

At least 35 tests covering:
- Register + login + enterprise upgrade
- `walkthrough_progress` table exists
- `GET /api/walkthrough/flows` returns 4 flows, all `not_started`
- Each flow: answer step 1 → status becomes `in_progress`, step_index
  advances, can `GET` and see persisted answer
- Each flow: answer all steps → `/complete` → side effect row exists in the
  underlying table (territory/job/users row)
- Resume: after partial answering, re-fetch shows the same step_index
- Skip: sets status to `skipped`, does NOT write side-effect row
- Reset: takes a completed flow back to `in_progress`
- Unauthenticated requests return 401
- Dashboard loads and includes walkthrough wiring (smoke)

Target: 100% pass.
