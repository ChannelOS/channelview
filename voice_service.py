"""
ChannelView - Voice Agent Service (Retell AI Integration)
Handles all voice AI operations: agent creation, call management, webhooks.

Cycle 34: Foundation layer
"""
import os
import json
import uuid
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from database import get_db

RETELL_API_BASE = 'https://api.retellai.com'


class VoiceService:
    """Service layer for Retell AI voice agent integration."""

    def __init__(self, api_key=None):
        self.api_key = api_key

    def _retell_request(self, method, endpoint, data=None):
        """Make a request to the Retell AI API."""
        if not self.api_key:
            return {'error': 'Retell API key not configured'}, 503
        url = RETELL_API_BASE + endpoint
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header('Authorization', f'Bearer {self.api_key}')
        req.add_header('Content-Type', 'application/json')
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read().decode()), resp.getcode()
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except:
                body = {'error': f'HTTP {e.code}'}
            return body, e.code
        except Exception as e:
            return {'error': str(e)}, 500

    # ======================== AGENT MANAGEMENT ========================

    def create_retell_agent(self, user_id, name, voice_id='eleven_labs_rachel',
                            greeting='Hi, this is the recruiting team.', prompt=None):
        """Create a voice agent on Retell and store locally."""
        agent_data = {
            'agent_name': name,
            'voice_id': voice_id,
            'response_engine': {
                'type': 'retell-llm',
                'llm_id': None  # Will use Retell's default LLM
            },
            'language': 'en-US',
            'begin_message': greeting,
        }
        if prompt:
            agent_data['general_prompt'] = prompt

        resp, status = self._retell_request('POST', '/create-agent', agent_data)
        if status not in (200, 201):
            return None, resp.get('error', 'Failed to create Retell agent')

        # Store locally
        agent_id = str(uuid.uuid4())
        db = get_db()
        try:
            db.execute("""
                INSERT INTO voice_agents (id, user_id, name, retell_agent_id, voice_id, greeting_script, persona_prompt)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (agent_id, user_id, name, resp.get('agent_id'), voice_id, greeting, prompt))
            db.commit()
            return agent_id, None
        except Exception as e:
            return None, str(e)
        finally:
            db.close()

    def get_agents(self, user_id):
        """Get all voice agents for a user."""
        db = get_db()
        try:
            rows = db.execute(
                "SELECT * FROM voice_agents WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def get_agent(self, agent_id, user_id):
        """Get a specific voice agent."""
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM voice_agents WHERE id = ? AND user_id = ?",
                (agent_id, user_id)
            ).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def update_agent(self, agent_id, user_id, updates):
        """Update a voice agent's configuration."""
        allowed_fields = ['name', 'greeting_script', 'persona_prompt', 'voice_id',
                          'language', 'max_call_duration', 'active']
        set_parts = []
        values = []
        for field in allowed_fields:
            if field in updates:
                set_parts.append(f"{field} = ?")
                values.append(updates[field])
        if not set_parts:
            return False, 'No valid fields to update'

        set_parts.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.extend([agent_id, user_id])

        db = get_db()
        try:
            db.execute(
                f"UPDATE voice_agents SET {', '.join(set_parts)} WHERE id = ? AND user_id = ?",
                values
            )
            db.commit()

            # Sync to Retell if agent exists there
            agent = self.get_agent(agent_id, user_id)
            if agent and agent.get('retell_agent_id'):
                retell_updates = {}
                if 'greeting_script' in updates:
                    retell_updates['begin_message'] = updates['greeting_script']
                if 'persona_prompt' in updates:
                    retell_updates['general_prompt'] = updates['persona_prompt']
                if 'voice_id' in updates:
                    retell_updates['voice_id'] = updates['voice_id']
                if retell_updates:
                    self._retell_request('PATCH', f'/update-agent/{agent["retell_agent_id"]}', retell_updates)

            return True, None
        except Exception as e:
            return False, str(e)
        finally:
            db.close()

    def delete_agent(self, agent_id, user_id):
        """Delete a voice agent."""
        db = get_db()
        try:
            agent = db.execute(
                "SELECT retell_agent_id FROM voice_agents WHERE id = ? AND user_id = ?",
                (agent_id, user_id)
            ).fetchone()
            if not agent:
                return False, 'Agent not found'

            # Delete from Retell
            if agent['retell_agent_id']:
                self._retell_request('DELETE', f'/delete-agent/{agent["retell_agent_id"]}')

            db.execute("DELETE FROM voice_agents WHERE id = ? AND user_id = ?", (agent_id, user_id))
            db.commit()
            return True, None
        except Exception as e:
            return False, str(e)
        finally:
            db.close()

    # ======================== CALL MANAGEMENT ========================

    def create_call(self, user_id, agent_id, candidate_id, phone_number, script_id=None, metadata=None):
        """Initiate an outbound voice call via Retell."""
        db = get_db()
        try:
            # Get agent details
            agent = db.execute(
                "SELECT * FROM voice_agents WHERE id = ? AND user_id = ?",
                (agent_id, user_id)
            ).fetchone()
            if not agent:
                return None, 'Agent not found'
            agent = dict(agent)

            if not agent.get('retell_agent_id'):
                return None, 'Agent not registered with Retell'

            # Get candidate info for personalizing the call
            candidate = None
            if candidate_id:
                candidate = db.execute(
                    "SELECT * FROM candidates WHERE id = ? AND user_id = ?",
                    (candidate_id, user_id)
                ).fetchone()
                if candidate:
                    candidate = dict(candidate)

            # Build dynamic variables for the call
            retell_metadata = metadata or {}
            if candidate:
                retell_metadata['candidate_name'] = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip()
                retell_metadata['candidate_email'] = candidate.get('email', '')

            # Create call on Retell
            call_data = {
                'agent_id': agent['retell_agent_id'],
                'customer_number': phone_number,
                'metadata': retell_metadata,
            }
            if agent.get('retell_phone_number'):
                call_data['from_number'] = agent['retell_phone_number']

            resp, status = self._retell_request('POST', '/create-phone-call', call_data)
            if status not in (200, 201):
                return None, resp.get('error', 'Failed to create call')

            # Store call record
            call_id = str(uuid.uuid4())
            db.execute("""
                INSERT INTO voice_calls
                (id, user_id, agent_id, candidate_id, script_id, retell_call_id,
                 direction, status, phone_number, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'outbound', 'initiated', ?, ?, ?)
            """, (call_id, user_id, agent_id, candidate_id, script_id,
                  resp.get('call_id'), phone_number, json.dumps(retell_metadata),
                  datetime.utcnow().isoformat()))
            db.commit()
            return call_id, None
        except Exception as e:
            return None, str(e)
        finally:
            db.close()

    def get_calls(self, user_id, filters=None):
        """Get call history for a user with optional filters."""
        db = get_db()
        try:
            query = """
                SELECT vc.*, va.name as agent_name,
                       c.first_name || ' ' || c.last_name as candidate_name,
                       c.email as candidate_email
                FROM voice_calls vc
                LEFT JOIN voice_agents va ON vc.agent_id = va.id
                LEFT JOIN candidates c ON vc.candidate_id = c.id
                WHERE vc.user_id = ?
            """
            params = [user_id]

            if filters:
                if filters.get('status'):
                    query += " AND vc.status = ?"
                    params.append(filters['status'])
                if filters.get('candidate_id'):
                    query += " AND vc.candidate_id = ?"
                    params.append(filters['candidate_id'])
                if filters.get('agent_id'):
                    query += " AND vc.agent_id = ?"
                    params.append(filters['agent_id'])
                if filters.get('date_from'):
                    query += " AND vc.created_at >= ?"
                    params.append(filters['date_from'])
                if filters.get('date_to'):
                    query += " AND vc.created_at <= ?"
                    params.append(filters['date_to'])

            query += " ORDER BY vc.created_at DESC LIMIT 100"
            rows = db.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def get_call(self, call_id, user_id):
        """Get a specific call record."""
        db = get_db()
        try:
            row = db.execute("""
                SELECT vc.*, va.name as agent_name,
                       c.first_name || ' ' || c.last_name as candidate_name
                FROM voice_calls vc
                LEFT JOIN voice_agents va ON vc.agent_id = va.id
                LEFT JOIN candidates c ON vc.candidate_id = c.id
                WHERE vc.id = ? AND vc.user_id = ?
            """, (call_id, user_id)).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    # ======================== WEBHOOK HANDLING ========================

    def handle_call_event(self, event_data):
        """Process a Retell webhook event for a call."""
        retell_call_id = event_data.get('call_id')
        event_type = event_data.get('event')

        if not retell_call_id:
            return False, 'Missing call_id'

        db = get_db()
        try:
            call = db.execute(
                "SELECT * FROM voice_calls WHERE retell_call_id = ?",
                (retell_call_id,)
            ).fetchone()
            if not call:
                return False, 'Call not found'
            call = dict(call)

            updates = {"updated_at": datetime.utcnow().isoformat()}

            if event_type == 'call_started':
                updates['status'] = 'in_progress'
                updates['started_at'] = datetime.utcnow().isoformat()

            elif event_type == 'call_ended':
                updates['status'] = 'completed'
                updates['ended_at'] = datetime.utcnow().isoformat()
                if event_data.get('transcript'):
                    updates['transcript'] = event_data['transcript']
                if event_data.get('recording_url'):
                    updates['recording_url'] = event_data['recording_url']
                if event_data.get('call_analysis'):
                    analysis = event_data['call_analysis']
                    updates['summary'] = analysis.get('call_summary', '')
                    updates['sentiment_score'] = analysis.get('user_sentiment', 0)
                    updates['outcome'] = analysis.get('custom_analysis_data', {}).get('outcome', 'unknown')
                    updates['outcome_details'] = json.dumps(analysis.get('custom_analysis_data', {}))
                # Calculate duration
                if call.get('started_at'):
                    try:
                        start = datetime.fromisoformat(call['started_at'])
                        updates['duration_seconds'] = int((datetime.utcnow() - start).total_seconds())
                    except:
                        pass

            elif event_type == 'call_analyzed':
                analysis = event_data.get('call_analysis', {})
                updates['summary'] = analysis.get('call_summary', '')
                updates['sentiment_score'] = analysis.get('user_sentiment', 0)
                outcome_data = analysis.get('custom_analysis_data', {})
                updates['outcome'] = outcome_data.get('outcome', 'unknown')
                updates['outcome_details'] = json.dumps(outcome_data)

            elif event_type in ('call_failed', 'call_error'):
                updates['status'] = 'failed'
                updates['error_message'] = event_data.get('error_message', 'Call failed')

            # Apply updates
            set_parts = [f"{k} = ?" for k in updates]
            values = list(updates.values()) + [retell_call_id]
            db.execute(
                f"UPDATE voice_calls SET {', '.join(set_parts)} WHERE retell_call_id = ?",
                values
            )

            # Update candidate engagement tracking
            if call.get('candidate_id') and event_type == 'call_ended':
                self._update_candidate_engagement(db, call['user_id'], call['candidate_id'], updates)

            db.commit()
            return True, None
        except Exception as e:
            return False, str(e)
        finally:
            db.close()

    def _update_candidate_engagement(self, db, user_id, candidate_id, call_data):
        """Update candidate engagement score after a call."""
        engagement_id = str(uuid.uuid4())
        sentiment = call_data.get('sentiment_score', 0)
        duration = call_data.get('duration_seconds', 0)

        # Calculate risk level based on call outcome
        risk_level = 'low'
        if sentiment and sentiment < -0.3:
            risk_level = 'high'
        elif sentiment and sentiment < 0:
            risk_level = 'medium'
        elif duration and duration < 30:
            risk_level = 'medium'  # Very short call = possible disengagement

        # Calculate engagement score (0-100)
        engagement_score = 50  # baseline
        if duration:
            engagement_score += min(20, duration / 15)  # Up to 20 pts for longer calls
        if sentiment:
            engagement_score += sentiment * 30  # -30 to +30 based on sentiment

        details = {
            'duration': duration,
            'sentiment': sentiment,
            'outcome': call_data.get('outcome', 'unknown'),
        }

        db.execute("""
            INSERT INTO candidate_engagement
            (id, user_id, candidate_id, engagement_type, channel, details, engagement_score, risk_level)
            VALUES (?, ?, ?, 'voice_call', 'voice', ?, ?, ?)
        """, (engagement_id, user_id, candidate_id, json.dumps(details), engagement_score, risk_level))

        # Update candidate voice fields
        db.execute("""
            UPDATE candidates SET
                last_voice_contact = ?,
                voice_engagement_score = ?,
                voice_risk_level = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
        """, (datetime.utcnow().isoformat(), engagement_score, risk_level,
              datetime.utcnow().isoformat(), candidate_id, user_id))

    # ======================== SCRIPTS MANAGEMENT ========================

    def create_script(self, user_id, agent_id, name, script_type='scheduling',
                      purpose=None, conversation_flow=None):
        """Create a voice conversation script."""
        script_id = str(uuid.uuid4())
        db = get_db()
        try:
            db.execute("""
                INSERT INTO voice_scripts
                (id, user_id, agent_id, name, script_type, purpose, conversation_flow)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (script_id, user_id, agent_id, name, script_type, purpose,
                  json.dumps(conversation_flow or self._default_flow(script_type))))
            db.commit()
            return script_id, None
        except Exception as e:
            return None, str(e)
        finally:
            db.close()

    def get_scripts(self, user_id, agent_id=None):
        """Get scripts, optionally filtered by agent."""
        db = get_db()
        try:
            if agent_id:
                rows = db.execute(
                    "SELECT * FROM voice_scripts WHERE user_id = ? AND agent_id = ? ORDER BY created_at DESC",
                    (user_id, agent_id)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM voice_scripts WHERE user_id = ? ORDER BY created_at DESC",
                    (user_id,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def update_script(self, script_id, user_id, updates):
        """Update a voice script."""
        allowed = ['name', 'script_type', 'purpose', 'conversation_flow', 'active']
        set_parts = []
        values = []
        for f in allowed:
            if f in updates:
                set_parts.append(f"{f} = ?")
                val = updates[f]
                if f == 'conversation_flow' and isinstance(val, dict):
                    val = json.dumps(val)
                values.append(val)
        if not set_parts:
            return False, 'No valid fields'
        set_parts.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.extend([script_id, user_id])
        db = get_db()
        try:
            db.execute(f"UPDATE voice_scripts SET {', '.join(set_parts)} WHERE id = ? AND user_id = ?", values)
            db.commit()
            return True, None
        except Exception as e:
            return False, str(e)
        finally:
            db.close()

    # ======================== CALL SCHEDULING ========================

    def schedule_call(self, user_id, agent_id, candidate_id, scheduled_at,
                      call_type='scheduling', script_id=None, notes=None):
        """Schedule a future voice call."""
        sched_id = str(uuid.uuid4())
        db = get_db()
        try:
            db.execute("""
                INSERT INTO voice_call_schedule
                (id, user_id, agent_id, candidate_id, script_id, scheduled_at, call_type, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (sched_id, user_id, agent_id, candidate_id, script_id, scheduled_at, call_type, notes))
            db.commit()
            return sched_id, None
        except Exception as e:
            return None, str(e)
        finally:
            db.close()

    def get_scheduled_calls(self, user_id, status='pending'):
        """Get scheduled calls."""
        db = get_db()
        try:
            rows = db.execute("""
                SELECT vcs.*, c.first_name || ' ' || c.last_name as candidate_name,
                       c.phone as candidate_phone, va.name as agent_name
                FROM voice_call_schedule vcs
                LEFT JOIN candidates c ON vcs.candidate_id = c.id
                LEFT JOIN voice_agents va ON vcs.agent_id = va.id
                WHERE vcs.user_id = ? AND vcs.status = ?
                ORDER BY vcs.scheduled_at ASC
            """, (user_id, status)).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def execute_scheduled_calls(self, user_id):
        """Execute calls that are past their scheduled time."""
        db = get_db()
        try:
            now = datetime.utcnow().isoformat()
            pending = db.execute("""
                SELECT vcs.*, c.phone as candidate_phone
                FROM voice_call_schedule vcs
                LEFT JOIN candidates c ON vcs.candidate_id = c.id
                WHERE vcs.user_id = ? AND vcs.status = 'pending' AND vcs.scheduled_at <= ?
                AND vcs.attempt_count < vcs.max_attempts
                ORDER BY vcs.priority ASC, vcs.scheduled_at ASC
                LIMIT 10
            """, (user_id, now)).fetchall()

            results = []
            for sched in pending:
                sched = dict(sched)
                phone = sched.get('candidate_phone')
                if not phone:
                    db.execute(
                        "UPDATE voice_call_schedule SET status = 'failed', notes = 'No phone number' WHERE id = ?",
                        (sched['id'],)
                    )
                    continue

                call_id, error = self.create_call(
                    user_id, sched['agent_id'], sched['candidate_id'],
                    phone, sched.get('script_id')
                )

                db.execute("""
                    UPDATE voice_call_schedule SET
                        attempt_count = attempt_count + 1,
                        last_attempt_at = ?,
                        status = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (now, 'completed' if call_id else 'retry',
                      now, sched['id']))

                results.append({
                    'schedule_id': sched['id'],
                    'call_id': call_id,
                    'error': error
                })

            db.commit()
            return results
        finally:
            db.close()

    # ======================== ANALYTICS ========================

    def get_voice_stats(self, user_id, days=30):
        """Get voice agent analytics for a user."""
        db = get_db()
        try:
            since = (datetime.utcnow() - timedelta(days=days)).isoformat()
            stats = {}

            # Overall call stats
            row = db.execute("""
                SELECT
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_calls,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_calls,
                    AVG(CASE WHEN duration_seconds > 0 THEN duration_seconds END) as avg_duration,
                    SUM(cost_cents) as total_cost,
                    AVG(sentiment_score) as avg_sentiment
                FROM voice_calls
                WHERE user_id = ? AND created_at >= ?
            """, (user_id, since)).fetchone()
            stats['calls'] = dict(row) if row else {}

            # Outcome breakdown
            outcomes = db.execute("""
                SELECT outcome, COUNT(*) as count
                FROM voice_calls
                WHERE user_id = ? AND created_at >= ? AND outcome IS NOT NULL
                GROUP BY outcome
            """, (user_id, since)).fetchall()
            stats['outcomes'] = {r['outcome']: r['count'] for r in outcomes}

            # Daily call volume
            daily = db.execute("""
                SELECT DATE(created_at) as date, COUNT(*) as calls,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as connected
                FROM voice_calls
                WHERE user_id = ? AND created_at >= ?
                GROUP BY DATE(created_at)
                ORDER BY date
            """, (user_id, since)).fetchall()
            stats['daily'] = [dict(r) for r in daily]

            # At-risk candidates
            at_risk = db.execute("""
                SELECT c.id, c.first_name, c.last_name, c.email, c.phone,
                       c.voice_risk_level, c.voice_engagement_score, c.pipeline_stage
                FROM candidates c
                WHERE c.user_id = ? AND c.voice_risk_level IN ('medium', 'high')
                ORDER BY c.voice_engagement_score ASC
                LIMIT 20
            """, (user_id,)).fetchall()
            stats['at_risk_candidates'] = [dict(r) for r in at_risk]

            # Scheduled calls pending
            pending = db.execute(
                "SELECT COUNT(*) as count FROM voice_call_schedule WHERE user_id = ? AND status = 'pending'",
                (user_id,)
            ).fetchone()
            stats['pending_scheduled'] = pending['count'] if pending else 0

            return stats
        finally:
            db.close()

    # ======================== DEFAULT SCRIPT TEMPLATES ========================

    def _default_flow(self, script_type):
        """Return a default conversation flow template."""
        templates = {
            'scheduling': {
                'name': 'Interview Scheduling',
                'steps': [
                    {'id': 'greeting', 'type': 'speak', 'content': 'Hi {{candidate_name}}, this is {{agent_name}} from {{agency_name}}. I\'m reaching out about an insurance career opportunity we discussed.'},
                    {'id': 'interest_check', 'type': 'question', 'content': 'Are you still interested in learning more about this opportunity?', 'responses': {'yes': 'schedule', 'no': 'objection_handle', 'maybe': 'pitch'}},
                    {'id': 'pitch', 'type': 'speak', 'content': 'I completely understand. Many of our top agents started with some hesitation. What we offer is comprehensive training, mentorship, and the ability to build your own book of business.'},
                    {'id': 'schedule', 'type': 'question', 'content': 'Great! I\'d love to set up a brief meeting to go over the details. Would you prefer an in-person meeting, or would a video call work better for your schedule?', 'responses': {'in_person': 'book_inperson', 'video': 'book_video', 'group': 'book_group'}},
                    {'id': 'book_inperson', 'type': 'action', 'action': 'schedule_interview', 'params': {'type': 'in_person'}},
                    {'id': 'book_video', 'type': 'action', 'action': 'schedule_interview', 'params': {'type': 'video'}},
                    {'id': 'book_group', 'type': 'action', 'action': 'schedule_interview', 'params': {'type': 'info_session'}},
                    {'id': 'objection_handle', 'type': 'speak', 'content': 'I understand. Is there anything specific holding you back? Many people have concerns about the commission-only structure, and I\'d be happy to explain how our training and support system helps new agents succeed quickly.'},
                    {'id': 'closing', 'type': 'speak', 'content': 'Thank you for your time, {{candidate_name}}. We\'ll send you a confirmation with all the details. Looking forward to meeting you!'}
                ]
            },
            'check_in': {
                'name': 'Pipeline Check-In',
                'steps': [
                    {'id': 'greeting', 'type': 'speak', 'content': 'Hi {{candidate_name}}, this is {{agent_name}} from {{agency_name}}. I\'m calling to check in on how things are going with your onboarding process.'},
                    {'id': 'status_check', 'type': 'question', 'content': 'How has everything been going? Any questions or concerns I can help with?'},
                    {'id': 'licensing', 'type': 'question', 'content': 'How is the licensing process going? Have you been able to schedule your exam?'},
                    {'id': 'support', 'type': 'speak', 'content': 'That\'s great to hear. Remember, our team is here to support you through every step. Don\'t hesitate to reach out if you need anything.'},
                    {'id': 'next_steps', 'type': 'speak', 'content': 'Your next milestone is {{next_milestone}}. We\'ll check back in with you in a few days to see how things are progressing.'},
                    {'id': 'closing', 'type': 'speak', 'content': 'Thanks for the update, {{candidate_name}}. Talk to you soon!'}
                ]
            },
            'reengagement': {
                'name': 'Re-engagement',
                'steps': [
                    {'id': 'greeting', 'type': 'speak', 'content': 'Hi {{candidate_name}}, this is {{agent_name}} from {{agency_name}}. I wanted to reach out because we noticed it\'s been a little while since we last connected.'},
                    {'id': 'check_interest', 'type': 'question', 'content': 'Are you still interested in the opportunity we discussed?'},
                    {'id': 'address_concerns', 'type': 'speak', 'content': 'I completely understand life gets busy. We\'d love to keep the door open for you. Is there anything we can do to make the process easier?'},
                    {'id': 'closing', 'type': 'speak', 'content': 'Thanks for being upfront with me, {{candidate_name}}. We wish you all the best, and the door is always open if you change your mind.'}
                ]
            }
        }
        return templates.get(script_type, templates['scheduling'])

    # ======================== CONSENT MANAGEMENT ========================

    def set_voice_consent(self, candidate_id, user_id, consent=True):
        """Record candidate's consent for voice calls."""
        db = get_db()
        try:
            db.execute(
                "UPDATE candidates SET voice_consent = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (1 if consent else 0, datetime.utcnow().isoformat(), candidate_id, user_id)
            )
            db.commit()
            return True
        except Exception as e:
            return False
        finally:
            db.close()

    def get_candidates_for_calling(self, user_id, pipeline_stage=None, has_consent=True):
        """Get candidates eligible for voice calls."""
        db = get_db()
        try:
            query = """
                SELECT c.*, i.title as interview_title
                FROM candidates c
                LEFT JOIN interviews i ON c.interview_id = i.id
                WHERE c.user_id = ? AND c.phone IS NOT NULL AND c.phone != ''
            """
            params = [user_id]
            if has_consent:
                query += " AND c.voice_consent = 1"
            if pipeline_stage:
                query += " AND c.pipeline_stage = ?"
                params.append(pipeline_stage)
            query += " ORDER BY c.created_at DESC"
            rows = db.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()
