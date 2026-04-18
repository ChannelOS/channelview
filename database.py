"""
ChannelView - Database Schema & Initialization
Dual-mode: SQLite (dev/test) or PostgreSQL (production)
Set DATABASE_URL env var to use PostgreSQL, otherwise SQLite.
"""
import sqlite3
import os
import re

DB_PATH = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(__file__), 'channelview.db'))
DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_POSTGRES = DATABASE_URL.startswith('postgres')


class PgCursorWrapper:
    """Wraps a psycopg2 cursor to return dicts like sqlite3.Row."""
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        return self._cursor.fetchmany(size) if size else self._cursor.fetchmany()

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def __iter__(self):
        return iter(self._cursor)

    def close(self):
        self._cursor.close()


class PgConnectionWrapper:
    """Wraps a psycopg2 connection to provide SQLite-compatible API.
    Auto-converts ? placeholders to %s and handles executescript."""
    def __init__(self, pg_conn):
        self._conn = pg_conn

    @staticmethod
    def _convert_sql(sql):
        # Replace ? param placeholders with %s (skip ? inside single-quoted strings)
        result = []
        in_quote = False
        for ch in sql:
            if ch == "'" and not in_quote:
                in_quote = True
                result.append(ch)
            elif ch == "'" and in_quote:
                in_quote = False
                result.append(ch)
            elif ch == '?' and not in_quote:
                result.append('%s')
            else:
                result.append(ch)
        sql_out = ''.join(result)
        # Convert SQLite datetime functions to PostgreSQL
        sql_out = sql_out.replace("datetime('now')", "NOW()")
        # datetime(col, '+' || minutes || ' minutes') → col + (minutes || ' minutes')::INTERVAL
        sql_out = re.sub(
            r"datetime\((\w+),\s*'\+'\s*\|\|\s*(\w+)\s*\|\|\s*'\s*minutes'\)",
            r"(\1 + (\2 || ' minutes')::INTERVAL)",
            sql_out
        )
        # Replace SQLite double-quoted strings with single quotes in concatenation
        sql_out = sql_out.replace('|| " " ||', "|| ' ' ||")
        # SQLite `INSERT OR IGNORE INTO ...` → PostgreSQL `INSERT INTO ... ON CONFLICT DO NOTHING`
        # Regex handles case-insensitive match and varying whitespace. If the original SQL already
        # contains its own ON CONFLICT clause we leave it alone; otherwise we append DO NOTHING.
        if re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql_out, re.IGNORECASE):
            sql_out = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT", sql_out, flags=re.IGNORECASE)
            if not re.search(r"\bON\s+CONFLICT\b", sql_out, re.IGNORECASE):
                # Trim trailing semicolons/whitespace before appending
                sql_out = sql_out.rstrip().rstrip(";")
                sql_out = sql_out + " ON CONFLICT DO NOTHING"
        # SQLite `INSERT OR REPLACE INTO ...` is rare in the codebase; we don't translate it
        # because the Postgres equivalent requires an explicit conflict target column.
        return sql_out

    def execute(self, sql, params=None):
        from psycopg2.extras import RealDictCursor
        sql = self._convert_sql(sql)
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return PgCursorWrapper(cur)

    def executescript(self, sql):
        from psycopg2.extras import RealDictCursor
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        # Remove SQL comments before splitting
        lines = []
        for line in sql.split('\n'):
            stripped = line.strip()
            if not stripped.startswith('--'):
                lines.append(line)
        cleaned = '\n'.join(lines)
        stmts = [s.strip() for s in cleaned.split(';') if s.strip()]
        for stmt in stmts:
            if stmt:
                try:
                    cur.execute(self._convert_sql(stmt))
                except Exception:
                    # In autocommit mode, each statement is independent.
                    # In transaction mode, we need a new cursor after failure.
                    if not self._conn.autocommit:
                        self._conn.rollback()
                    cur = self._conn.cursor(cursor_factory=RealDictCursor)
        return PgCursorWrapper(cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def cursor(self):
        from psycopg2.extras import RealDictCursor
        return self._conn.cursor(cursor_factory=RealDictCursor)


def get_db(autocommit=False):
    if USE_POSTGRES:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        if autocommit:
            conn.autocommit = True
        return PgConnectionWrapper(conn)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

def init_db():
    conn = get_db(autocommit=True)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        name TEXT NOT NULL,
        agency_name TEXT,
        role TEXT DEFAULT 'owner',
        plan TEXT DEFAULT 'starter',
        brand_color TEXT DEFAULT '#0ace0a',
        logo_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS interviews (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        department TEXT,
        position TEXT,
        status TEXT DEFAULT 'active',
        thinking_time INTEGER DEFAULT 30,
        max_answer_time INTEGER DEFAULT 120,
        max_retakes INTEGER DEFAULT 3,
        welcome_msg TEXT DEFAULT 'Thanks for your interest! This is a quick video interview — just a few questions so we can get to know you. Be yourself, there are no wrong answers. You can re-record if you need to.',
        thank_you_msg TEXT DEFAULT 'Thank you so much for taking the time! We really appreciate it. We''ll review your responses and reach out soon. If you have any questions in the meantime, don''t hesitate to reach out.',
        brand_color TEXT DEFAULT '#0ace0a',
        intro_video_path TEXT,
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS questions (
        id TEXT PRIMARY KEY,
        interview_id TEXT NOT NULL,
        question_text TEXT NOT NULL,
        question_order INTEGER NOT NULL,
        thinking_time INTEGER,
        max_answer_time INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (interview_id) REFERENCES interviews(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS candidates (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        interview_id TEXT NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT,
        token TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'invited',
        invited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        ai_score REAL,
        ai_summary TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (interview_id) REFERENCES interviews(id)
    );

    CREATE TABLE IF NOT EXISTS responses (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        video_path TEXT,
        duration INTEGER,
        transcript TEXT,
        ai_score REAL,
        ai_feedback TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        FOREIGN KEY (question_id) REFERENCES questions(id)
    );

    CREATE TABLE IF NOT EXISTS intro_videos (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        video_path TEXT NOT NULL,
        duration INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE INDEX IF NOT EXISTS idx_intro_videos_user ON intro_videos(user_id);
    CREATE INDEX IF NOT EXISTS idx_candidates_token ON candidates(token);
    CREATE INDEX IF NOT EXISTS idx_candidates_interview ON candidates(interview_id);
    CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
    CREATE INDEX IF NOT EXISTS idx_questions_interview ON questions(interview_id);
    CREATE INDEX IF NOT EXISTS idx_responses_candidate ON responses(candidate_id);
    """)
    # Reports table for shareable candidate reports
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL,
        title TEXT,
        password_hash TEXT,
        include_scores INTEGER DEFAULT 1,
        include_ai_feedback INTEGER DEFAULT 1,
        include_notes INTEGER DEFAULT 0,
        include_videos INTEGER DEFAULT 0,
        custom_message TEXT,
        views INTEGER DEFAULT 0,
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (candidate_id) REFERENCES candidates(id)
    );
    CREATE INDEX IF NOT EXISTS idx_reports_token ON reports(token);
    CREATE INDEX IF NOT EXISTS idx_reports_candidate ON reports(candidate_id);
    """)

    # Managers / contacts table for sharing reports
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS managers (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        title TEXT,
        department TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_managers_user ON managers(user_id);
    """)

    # Team members table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS team_members (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role TEXT DEFAULT 'reviewer',
        invited_by TEXT,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_team_account ON team_members(account_id);
    CREATE INDEX IF NOT EXISTS idx_team_user ON team_members(user_id);
    """)

    # Audit log table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_audit_account ON audit_log(account_id);
    """)

    # Interview templates table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS interview_templates (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        department TEXT,
        position TEXT,
        thinking_time INTEGER DEFAULT 30,
        max_answer_time INTEGER DEFAULT 120,
        max_retakes INTEGER DEFAULT 3,
        welcome_msg TEXT,
        thank_you_msg TEXT,
        is_shared INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_templates_user ON interview_templates(user_id);
    """)

    # Question library table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS question_library (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        question_text TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        tags TEXT,
        use_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_qlib_user ON question_library(user_id);
    CREATE INDEX IF NOT EXISTS idx_qlib_category ON question_library(category);
    """)

    # Template questions (join table)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS template_questions (
        id TEXT PRIMARY KEY,
        template_id TEXT NOT NULL,
        question_text TEXT NOT NULL,
        question_order INTEGER NOT NULL,
        category TEXT DEFAULT 'general',
        FOREIGN KEY (template_id) REFERENCES interview_templates(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_tq_template ON template_questions(template_id);
    """)

    # Webhooks table (persistent storage)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS webhooks (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        url TEXT NOT NULL,
        events TEXT NOT NULL,
        secret TEXT,
        active INTEGER DEFAULT 1,
        last_triggered_at TIMESTAMP,
        failure_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_webhooks_user ON webhooks(user_id);
    """)

    # Notifications table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        type TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT,
        entity_type TEXT,
        entity_id TEXT,
        is_read INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id);
    CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(user_id, is_read);
    """)

    # Candidate tags table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS candidate_tags (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        tag TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        UNIQUE(candidate_id, tag)
    );
    CREATE INDEX IF NOT EXISTS idx_ctags_candidate ON candidate_tags(candidate_id);
    CREATE INDEX IF NOT EXISTS idx_ctags_user_tag ON candidate_tags(user_id, tag);
    """)

    # Custom fields definition table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS custom_field_defs (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        field_type TEXT DEFAULT 'text',
        options TEXT,
        required INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, field_name)
    );
    CREATE INDEX IF NOT EXISTS idx_cfd_user ON custom_field_defs(user_id);
    """)

    # Custom field values table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS custom_field_values (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        field_id TEXT NOT NULL,
        value TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        FOREIGN KEY (field_id) REFERENCES custom_field_defs(id) ON DELETE CASCADE,
        UNIQUE(candidate_id, field_id)
    );
    CREATE INDEX IF NOT EXISTS idx_cfv_candidate ON custom_field_values(candidate_id);
    """)

    # Email log table
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS email_log (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        candidate_id TEXT,
        email_type TEXT NOT NULL,
        to_email TEXT NOT NULL,
        subject TEXT,
        status TEXT DEFAULT 'sent',
        error_message TEXT,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (candidate_id) REFERENCES candidates(id)
    );
    CREATE INDEX IF NOT EXISTS idx_email_log_candidate ON email_log(candidate_id);
    """)

    # Cycle 12: Integration events table (Zapier-compatible webhook event log)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS integration_events (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        delivered INTEGER DEFAULT 0,
        delivery_attempts INTEGER DEFAULT 0,
        last_attempt_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_events_account ON integration_events(account_id);
    CREATE INDEX IF NOT EXISTS idx_events_type ON integration_events(event_type);
    CREATE INDEX IF NOT EXISTS idx_events_delivered ON integration_events(delivered);
    """)

    # Cycle 12: Integration connections (ATS/CRM)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS integrations (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        config TEXT,
        active INTEGER DEFAULT 1,
        last_sync_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, provider)
    );
    CREATE INDEX IF NOT EXISTS idx_integrations_user ON integrations(user_id);
    """)

    # Cycle 12: Compliance audit trail (enhanced from existing audit_log)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS compliance_log (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT,
        ip_address TEXT,
        user_agent TEXT,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_compliance_account ON compliance_log(account_id);
    CREATE INDEX IF NOT EXISTS idx_compliance_action ON compliance_log(action);
    CREATE INDEX IF NOT EXISTS idx_compliance_resource ON compliance_log(resource_type, resource_id);
    CREATE INDEX IF NOT EXISTS idx_compliance_created ON compliance_log(created_at);
    """)

    # Cycle 12: Data retention policies
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS retention_policies (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        retention_days INTEGER NOT NULL DEFAULT 365,
        auto_delete INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, resource_type)
    );
    CREATE INDEX IF NOT EXISTS idx_retention_user ON retention_policies(user_id);
    """)

    # Cycle 12: EEOC scoring documentation
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS scoring_docs (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        scorer_id TEXT NOT NULL,
        scoring_criteria TEXT NOT NULL,
        justification TEXT NOT NULL,
        score REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id),
        FOREIGN KEY (scorer_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_scoring_docs_candidate ON scoring_docs(candidate_id);
    """)

    # Cycle 14: White-label branding profiles (FMO-level)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS brand_profiles (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        profile_name TEXT NOT NULL,
        primary_color TEXT DEFAULT '#0ace0a',
        secondary_color TEXT DEFAULT '#000000',
        accent_color TEXT DEFAULT '#ffffff',
        logo_url TEXT,
        favicon_url TEXT,
        custom_domain TEXT,
        email_from_name TEXT,
        email_header_html TEXT,
        email_footer_html TEXT,
        candidate_portal_title TEXT,
        candidate_portal_tagline TEXT,
        hide_powered_by INTEGER DEFAULT 0,
        custom_css TEXT,
        is_default INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (owner_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_brand_profiles_owner ON brand_profiles(owner_id);
    """)

    # Cycle 14: Onboarding checklists
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS onboarding_steps (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        step_key TEXT NOT NULL,
        step_label TEXT NOT NULL,
        step_order INTEGER NOT NULL,
        completed INTEGER DEFAULT 0,
        completed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, step_key)
    );
    CREATE INDEX IF NOT EXISTS idx_onboarding_user ON onboarding_steps(user_id);
    """)

    # Cycle 14: Saved report configurations
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS report_configs (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        report_type TEXT NOT NULL,
        title TEXT NOT NULL,
        config TEXT NOT NULL,
        schedule TEXT,
        last_generated_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_report_configs_user ON report_configs(user_id);
    """)

    # Cycle 14: Security events log
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS security_events (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        event_type TEXT NOT NULL,
        ip_address TEXT,
        user_agent TEXT,
        details TEXT,
        severity TEXT DEFAULT 'info',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_security_events_type ON security_events(event_type);
    CREATE INDEX IF NOT EXISTS idx_security_events_user ON security_events(user_id);
    CREATE INDEX IF NOT EXISTS idx_security_events_created ON security_events(created_at);
    """)

    # Cycle 15: Activity feed
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS activity_feed (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        actor_name TEXT,
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        entity_name TEXT,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_activity_account ON activity_feed(account_id);
    CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_feed(created_at);
    """)

    # Cycle 15: Team notes (collaborative candidate notes)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS team_notes (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        author_id TEXT NOT NULL,
        author_name TEXT,
        content TEXT NOT NULL,
        note_type TEXT DEFAULT 'note',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        FOREIGN KEY (author_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_team_notes_candidate ON team_notes(candidate_id);
    CREATE INDEX IF NOT EXISTS idx_team_notes_account ON team_notes(account_id);
    """)

    # Cycle 15: Team reviewer scorecards
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS reviewer_scorecards (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        reviewer_id TEXT NOT NULL,
        reviewer_name TEXT,
        overall_score REAL,
        criteria_scores TEXT,
        recommendation TEXT,
        comments TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        FOREIGN KEY (reviewer_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_scorecards_candidate ON reviewer_scorecards(candidate_id);
    CREATE INDEX IF NOT EXISTS idx_scorecards_reviewer ON reviewer_scorecards(reviewer_id);
    """)

    # Cycle 16: Candidate portal sessions (track candidate progress through interview)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS candidate_sessions (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        token TEXT NOT NULL,
        device_info TEXT,
        ip_address TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_activity_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        questions_answered INTEGER DEFAULT 0,
        total_questions INTEGER DEFAULT 0,
        time_spent_seconds INTEGER DEFAULT 0,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_csess_candidate ON candidate_sessions(candidate_id);
    CREATE INDEX IF NOT EXISTS idx_csess_token ON candidate_sessions(token);
    """)

    # Cycle 16: Candidate reminders (automated follow-up)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS candidate_reminders (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        reminder_type TEXT NOT NULL,
        scheduled_at TIMESTAMP NOT NULL,
        sent_at TIMESTAMP,
        status TEXT DEFAULT 'pending',
        message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_creminder_candidate ON candidate_reminders(candidate_id);
    CREATE INDEX IF NOT EXISTS idx_creminder_status ON candidate_reminders(status);
    """)

    # Cycle 16: AI scoring rubrics (configurable per interview)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS scoring_rubrics (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        interview_id TEXT,
        name TEXT NOT NULL,
        description TEXT,
        criteria TEXT NOT NULL,
        weight_distribution TEXT,
        scoring_scale TEXT DEFAULT '0-100',
        is_default INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (interview_id) REFERENCES interviews(id)
    );
    CREATE INDEX IF NOT EXISTS idx_rubrics_user ON scoring_rubrics(user_id);
    CREATE INDEX IF NOT EXISTS idx_rubrics_interview ON scoring_rubrics(interview_id);
    """)

    # Cycle 16: AI analysis results (sentiment, keywords, detailed breakdowns)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ai_analysis (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        response_id TEXT,
        analysis_type TEXT NOT NULL,
        results TEXT NOT NULL,
        model_used TEXT,
        tokens_used INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        FOREIGN KEY (response_id) REFERENCES responses(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_analysis_candidate ON ai_analysis(candidate_id);
    CREATE INDEX IF NOT EXISTS idx_analysis_type ON ai_analysis(analysis_type);
    """)

    # Cycle 16: Webhook delivery log
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS webhook_deliveries (
        id TEXT PRIMARY KEY,
        webhook_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        response_status INTEGER,
        response_body TEXT,
        delivered INTEGER DEFAULT 0,
        attempts INTEGER DEFAULT 0,
        next_retry_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_wh_delivery_webhook ON webhook_deliveries(webhook_id);
    CREATE INDEX IF NOT EXISTS idx_wh_delivery_event ON webhook_deliveries(event_type);
    """)

    # Cycle 16: Embeddable widgets configuration
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS embed_widgets (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        interview_id TEXT,
        widget_type TEXT DEFAULT 'apply_button',
        title TEXT,
        config TEXT,
        embed_key TEXT UNIQUE NOT NULL,
        views INTEGER DEFAULT 0,
        submissions INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (interview_id) REFERENCES interviews(id)
    );
    CREATE INDEX IF NOT EXISTS idx_widgets_user ON embed_widgets(user_id);
    CREATE INDEX IF NOT EXISTS idx_widgets_key ON embed_widgets(embed_key);
    """)

    # Cycle 16: Admin analytics snapshots (for time-series metrics)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS analytics_snapshots (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        metrics TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(account_id, snapshot_date)
    );
    CREATE INDEX IF NOT EXISTS idx_snapshots_account ON analytics_snapshots(account_id);
    """)

    # Cycle 17: FMO super-admin portal (multi-level agency management)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS fmo_agencies (
        id TEXT PRIMARY KEY,
        fmo_id TEXT NOT NULL,
        agency_id TEXT NOT NULL,
        agency_name TEXT,
        status TEXT DEFAULT 'active',
        onboarded_at TIMESTAMP,
        brand_profile_id TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (fmo_id) REFERENCES users(id),
        FOREIGN KEY (agency_id) REFERENCES users(id),
        UNIQUE(fmo_id, agency_id)
    );
    CREATE INDEX IF NOT EXISTS idx_fmo_agencies_fmo ON fmo_agencies(fmo_id);
    CREATE INDEX IF NOT EXISTS idx_fmo_agencies_agency ON fmo_agencies(agency_id);
    """)

    # Cycle 17: Candidate shortlists (for review & comparison)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS shortlists (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        interview_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (interview_id) REFERENCES interviews(id)
    );
    CREATE INDEX IF NOT EXISTS idx_shortlists_user ON shortlists(user_id);
    """)

    # Cycle 17: Shortlist candidates (join table)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS shortlist_candidates (
        id TEXT PRIMARY KEY,
        shortlist_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        added_by TEXT NOT NULL,
        notes TEXT,
        position_rank INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (shortlist_id) REFERENCES shortlists(id) ON DELETE CASCADE,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        UNIQUE(shortlist_id, candidate_id)
    );
    CREATE INDEX IF NOT EXISTS idx_slc_shortlist ON shortlist_candidates(shortlist_id);
    """)

    # Cycle 17: Email templates (customizable branded templates)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS email_templates (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        template_type TEXT NOT NULL,
        name TEXT NOT NULL,
        subject TEXT NOT NULL,
        html_body TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        variables TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_email_templates_user ON email_templates(user_id);
    CREATE INDEX IF NOT EXISTS idx_email_templates_type ON email_templates(template_type);
    """)

    # Cycle 17: Video recording sessions (track recording attempts & quality)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS video_sessions (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        attempt_number INTEGER DEFAULT 1,
        duration_seconds INTEGER,
        recording_quality TEXT,
        device_type TEXT,
        browser TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        status TEXT DEFAULT 'recording',
        error_message TEXT,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        FOREIGN KEY (question_id) REFERENCES questions(id)
    );
    CREATE INDEX IF NOT EXISTS idx_vsess_candidate ON video_sessions(candidate_id);
    """)

    # ======================== CYCLE 35: INTRO TEMPLATES & INTEREST RATING ========================
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS intro_templates (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        thumbnail_emoji TEXT DEFAULT '👋',
        html_path TEXT NOT NULL,
        media_type TEXT DEFAULT 'html',
        category TEXT DEFAULT 'general',
        is_system INTEGER DEFAULT 1,
        user_id TEXT,
        duration_seconds INTEGER DEFAULT 30,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_intro_templates_category ON intro_templates(category);
    """)

    # Seed system intro templates if empty
    try:
        conn.commit()  # Ensure clean transaction state
        count = list(conn.execute("SELECT COUNT(*) FROM intro_templates WHERE is_system=1").fetchone().values())[0]
        if count == 0:
            conn.execute("""INSERT INTO intro_templates (id, name, description, thumbnail_emoji, html_path, category, is_system, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)""", ('intro_welcome', 'Welcome & What to Expect', 'Sets the tone, explains the video interview process, and makes the candidate comfortable.', '\U0001f44b', '/static/intros/welcome.html', 'general', 20))
            conn.execute("""INSERT INTO intro_templates (id, name, description, thumbnail_emoji, html_path, category, is_system, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)""", ('intro_opportunity', 'The Opportunity', 'Paints the picture - what the role looks like day-to-day, benefits, flexibility, and growth potential.', '\U0001f680', '/static/intros/opportunity.html', 'opportunity', 30))
            conn.execute("""INSERT INTO intro_templates (id, name, description, thumbnail_emoji, html_path, category, is_system, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)""", ('intro_team', 'Why Our Team', 'Culture, support system, training path, and what makes this team different.', '\U0001f91d', '/static/intros/team.html', 'culture', 30))
            conn.commit()
    except:
        try:
            conn.rollback()
        except:
            pass

    # Seed pro video intro templates (Cycle 37)
    try:
        existing_pro = conn.execute("SELECT id FROM intro_templates WHERE id LIKE 'pro_%'").fetchall()
        if len(existing_pro) == 0:
            conn.execute("""INSERT OR IGNORE INTO intro_templates
                (id, name, description, thumbnail_emoji, html_path, media_type, category, is_system, duration_seconds)
                VALUES (?, ?, ?, ?, ?, 'video', ?, 1, ?)""",
                ('pro_welcome', 'Pro: Welcome Message',
                 'Professionally produced welcome video — polished, warm, and sets the right tone for candidates.',
                 '🎬', '/static/intros/pro_welcome.mp4', 'general', 60))
            conn.execute("""INSERT OR IGNORE INTO intro_templates
                (id, name, description, thumbnail_emoji, html_path, media_type, category, is_system, duration_seconds)
                VALUES (?, ?, ?, ?, ?, 'video', ?, 1, ?)""",
                ('pro_opportunity', 'Pro: The Opportunity',
                 'Professionally produced overview of the career opportunity — benefits, flexibility, and earning potential.',
                 '🎬', '/static/intros/pro_opportunity.mp4', 'opportunity', 90))
            conn.execute("""INSERT OR IGNORE INTO intro_templates
                (id, name, description, thumbnail_emoji, html_path, media_type, category, is_system, duration_seconds)
                VALUES (?, ?, ?, ?, ?, 'video', ?, 1, ?)""",
                ('pro_culture', 'Pro: Team & Culture',
                 'Professionally produced team culture video — real stories, real people, why this team is different.',
                 '🎬', '/static/intros/pro_culture.mp4', 'culture', 90))
            conn.commit()
    except:
        try:
            conn.rollback()
        except:
            pass

    # Migrations for existing databases
    migrations = [
        ("interviews", "intro_video_path", "TEXT"),
        ("users", "smtp_host", "TEXT"),
        ("users", "smtp_port", "INTEGER DEFAULT 587"),
        ("users", "smtp_user", "TEXT"),
        ("users", "smtp_pass", "TEXT"),
        ("users", "smtp_from_email", "TEXT"),
        ("users", "smtp_from_name", "TEXT"),
        ("candidates", "invite_sent_at", "TIMESTAMP"),
        ("candidates", "reminder_sent_at", "TIMESTAMP"),
        ("candidates", "completion_sent_at", "TIMESTAMP"),
        ("candidates", "ai_scores_json", "TEXT"),
        ("responses", "ai_scores_json", "TEXT"),
        ("responses", "file_size", "INTEGER DEFAULT 0"),
        ("users", "stripe_customer_id", "TEXT"),
        ("users", "stripe_subscription_id", "TEXT"),
        ("users", "subscription_status", "TEXT DEFAULT 'none'"),
        ("users", "subscription_ends_at", "TIMESTAMP"),
        # Cycle 10: Onboarding & White-label
        ("users", "onboarding_completed", "INTEGER DEFAULT 0"),
        ("users", "onboarding_step", "INTEGER DEFAULT 0"),
        ("users", "is_fmo_admin", "INTEGER DEFAULT 0"),
        ("users", "agency_logo_url", "TEXT"),
        ("users", "agency_website", "TEXT"),
        ("users", "agency_phone", "TEXT"),
        ("users", "brand_secondary_color", "TEXT DEFAULT '#000000'"),
        ("users", "brand_accent_color", "TEXT DEFAULT '#ffffff'"),
        ("users", "white_label_enabled", "INTEGER DEFAULT 0"),
        ("users", "candidate_brand_name", "TEXT"),
        ("users", "candidate_brand_logo", "TEXT"),
        # Cycle 10: Email notification preferences
        ("users", "notify_interview_started", "INTEGER DEFAULT 1"),
        ("users", "notify_interview_completed", "INTEGER DEFAULT 1"),
        ("users", "notify_candidate_invited", "INTEGER DEFAULT 1"),
        ("users", "notify_daily_digest", "INTEGER DEFAULT 0"),
        # Cycle 11: Workflow automation, API keys, candidate progress
        ("users", "api_key", "TEXT"),
        ("users", "api_key_created_at", "TIMESTAMP"),
        ("users", "auto_score_enabled", "INTEGER DEFAULT 0"),
        ("users", "auto_advance_threshold", "INTEGER DEFAULT 0"),
        ("users", "auto_advance_enabled", "INTEGER DEFAULT 0"),
        ("users", "auto_reject_threshold", "INTEGER DEFAULT 0"),
        ("users", "auto_reject_enabled", "INTEGER DEFAULT 0"),
        ("users", "reminder_sequence_enabled", "INTEGER DEFAULT 0"),
        ("users", "reminder_day_3", "INTEGER DEFAULT 1"),
        ("users", "reminder_day_5", "INTEGER DEFAULT 1"),
        ("users", "reminder_day_7", "INTEGER DEFAULT 1"),
        ("interviews", "auto_expire_days", "INTEGER DEFAULT 0"),
        ("candidates", "progress_data", "TEXT"),
        ("candidates", "current_question_index", "INTEGER DEFAULT 0"),
        ("candidates", "last_activity_at", "TIMESTAMP"),
        # Cycle 12: ATS/CRM Integrations & Compliance
        ("users", "zapier_webhook_url", "TEXT"),
        ("users", "integration_events_enabled", "INTEGER DEFAULT 1"),
        ("users", "data_retention_days", "INTEGER DEFAULT 365"),
        ("users", "eeoc_mode_enabled", "INTEGER DEFAULT 0"),
        ("candidates", "pipeline_stage", "TEXT DEFAULT 'new'"),
        ("candidates", "kanban_order", "INTEGER DEFAULT 0"),
        ("candidates", "source", "TEXT DEFAULT 'manual'"),
        ("interviews", "candidate_count", "INTEGER DEFAULT 0"),
        # Cycle 14: White-label & Security
        ("users", "brand_profile_id", "TEXT"),
        ("users", "password_reset_token", "TEXT"),
        ("users", "password_reset_expires", "TIMESTAMP"),
        ("users", "mfa_secret", "TEXT"),
        ("users", "mfa_enabled", "INTEGER DEFAULT 0"),
        ("users", "failed_login_count", "INTEGER DEFAULT 0"),
        ("users", "locked_until", "TIMESTAMP"),
        ("users", "last_login_at", "TIMESTAMP"),
        ("users", "last_login_ip", "TEXT"),
        ("users", "onboarding_wizard_completed", "INTEGER DEFAULT 0"),
        # Cycle 15: Billing enforcement & Team collaboration
        ("users", "candidate_limit", "INTEGER DEFAULT 25"),
        ("users", "candidates_used_this_month", "INTEGER DEFAULT 0"),
        ("users", "billing_cycle_start", "TIMESTAMP"),
        ("users", "trial_ends_at", "TIMESTAMP"),
        ("users", "features_locked", "TEXT"),
        ("team_members", "permissions", "TEXT"),
        ("team_members", "last_active_at", "TIMESTAMP"),
        ("team_members", "display_name", "TEXT"),
        # Cycle 16: Candidate experience & AI scoring
        ("candidates", "portal_status", "TEXT DEFAULT 'not_started'"),
        ("candidates", "device_info", "TEXT"),
        ("candidates", "time_spent_seconds", "INTEGER DEFAULT 0"),
        ("candidates", "reminder_count", "INTEGER DEFAULT 0"),
        ("candidates", "last_reminded_at", "TIMESTAMP"),
        ("candidates", "sentiment_score", "REAL"),
        ("candidates", "keyword_matches", "TEXT"),
        ("interviews", "rubric_id", "TEXT"),
        ("interviews", "embed_enabled", "INTEGER DEFAULT 0"),
        ("interviews", "public_apply_enabled", "INTEGER DEFAULT 0"),
        ("interviews", "redirect_url", "TEXT"),
        ("responses", "sentiment_score", "REAL"),
        ("responses", "keywords_detected", "TEXT"),
        ("responses", "word_count", "INTEGER DEFAULT 0"),
        ("responses", "speaking_pace", "REAL"),
        # Cycle 16: Webhook & embed enhancements
        ("webhooks", "retry_count", "INTEGER DEFAULT 3"),
        ("webhooks", "headers", "TEXT"),
        ("webhooks", "delivery_count", "INTEGER DEFAULT 0"),
        ("webhooks", "last_delivery_status", "INTEGER"),
        # Cycle 17: FMO & Video & Email
        ("users", "is_fmo", "INTEGER DEFAULT 0"),
        ("users", "fmo_parent_id", "TEXT"),
        ("users", "default_email_template_id", "TEXT"),
        ("candidates", "shortlisted", "INTEGER DEFAULT 0"),
        ("candidates", "video_quality_score", "REAL"),
        ("candidates", "recording_device", "TEXT"),
        ("responses", "recording_attempts", "INTEGER DEFAULT 1"),
        ("responses", "video_thumbnail_path", "TEXT"),
        ("interviews", "email_template_id", "TEXT"),
        ("interviews", "reminder_template_id", "TEXT"),
        # Cycle 35: Intro templates & Interest rating
        ("interviews", "intro_type", "TEXT DEFAULT 'none'"),
        ("interviews", "intro_template_id", "TEXT"),
        ("interviews", "interest_rating_enabled", "INTEGER DEFAULT 1"),
        ("interviews", "interest_prompt", "TEXT"),
        ("candidates", "interest_rating", "INTEGER"),
        ("candidates", "interest_comment", "TEXT"),
        ("candidates", "interest_rated_at", "TIMESTAMP"),
        # Cycle 37: Pro video intros
        ("intro_templates", "media_type", "TEXT DEFAULT 'html'"),
    ]
    for table, col, coltype in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass  # Column already exists

    # ======================== CYCLE 18 TABLES ========================

    conn.executescript("""
    -- White-label branding profiles per agency
    CREATE TABLE IF NOT EXISTS brand_profiles (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        profile_name TEXT NOT NULL,
        primary_color TEXT DEFAULT '#0ace0a',
        secondary_color TEXT DEFAULT '#000000',
        background_color TEXT DEFAULT '#ffffff',
        text_color TEXT DEFAULT '#111111',
        logo_url TEXT,
        favicon_url TEXT,
        custom_domain TEXT,
        company_name TEXT,
        tagline TEXT,
        welcome_message TEXT,
        thank_you_message TEXT,
        email_header_html TEXT,
        email_footer_html TEXT,
        css_overrides TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Generated candidate scorecards / reports
    CREATE TABLE IF NOT EXISTS candidate_reports (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        interview_id TEXT NOT NULL,
        report_type TEXT DEFAULT 'scorecard',
        title TEXT,
        summary TEXT,
        scores_json TEXT,
        strengths TEXT,
        concerns TEXT,
        recommendation TEXT,
        generated_by TEXT DEFAULT 'system',
        share_token TEXT UNIQUE,
        shared_with TEXT,
        is_public INTEGER DEFAULT 0,
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Bulk operation jobs
    CREATE TABLE IF NOT EXISTS bulk_operations (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        total_items INTEGER DEFAULT 0,
        processed_items INTEGER DEFAULT 0,
        failed_items INTEGER DEFAULT 0,
        params_json TEXT,
        result_json TEXT,
        error_message TEXT,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Bulk operation item tracking
    CREATE TABLE IF NOT EXISTS bulk_operation_items (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        item_type TEXT NOT NULL,
        item_id TEXT,
        status TEXT DEFAULT 'pending',
        error_message TEXT,
        processed_at TIMESTAMP,
        FOREIGN KEY (operation_id) REFERENCES bulk_operations(id)
    );

    -- Workflow automation rules
    CREATE TABLE IF NOT EXISTS workflow_rules (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        trigger_type TEXT NOT NULL,
        trigger_config TEXT,
        action_type TEXT NOT NULL,
        action_config TEXT,
        is_enabled INTEGER DEFAULT 1,
        execution_count INTEGER DEFAULT 0,
        last_executed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Audit trail log
    CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        actor_name TEXT,
        actor_ip TEXT,
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT,
        resource_name TEXT,
        details TEXT,
        previous_value TEXT,
        new_value TEXT,
        severity TEXT DEFAULT 'info',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Consent tracking for candidates
    CREATE TABLE IF NOT EXISTS consent_records (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        consent_type TEXT NOT NULL,
        consent_given INTEGER DEFAULT 0,
        ip_address TEXT,
        user_agent TEXT,
        consent_text TEXT,
        withdrawn_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Data retention policies
    CREATE TABLE IF NOT EXISTS retention_policies (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        retention_days INTEGER NOT NULL DEFAULT 365,
        auto_delete INTEGER DEFAULT 0,
        notify_before_days INTEGER DEFAULT 30,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Cycle 18 migrations
    c18_migrations = [
        # White-label
        ("users", "brand_profile_id", "TEXT"),
        ("users", "custom_domain", "TEXT"),
        ("users", "white_label_enabled", "INTEGER DEFAULT 0"),
        ("interviews", "brand_profile_id", "TEXT"),
        # Reports
        ("candidates", "report_generated", "INTEGER DEFAULT 0"),
        ("candidates", "last_report_id", "TEXT"),
        # Bulk ops
        ("interviews", "bulk_invite_enabled", "INTEGER DEFAULT 1"),
        # Audit log - add missing columns to old table
        ("audit_log", "actor_name", "TEXT"),
        ("audit_log", "actor_ip", "TEXT"),
        ("audit_log", "resource_type", "TEXT"),
        ("audit_log", "resource_id", "TEXT"),
        ("audit_log", "resource_name", "TEXT"),
        ("audit_log", "previous_value", "TEXT"),
        ("audit_log", "new_value", "TEXT"),
        ("audit_log", "severity", "TEXT DEFAULT 'info'"),
        ("users", "audit_enabled", "INTEGER DEFAULT 1"),
        ("users", "retention_policy_id", "TEXT"),
        # Retention policies - add missing columns
        ("retention_policies", "notify_before_days", "INTEGER DEFAULT 30"),
        ("retention_policies", "is_active", "INTEGER DEFAULT 1"),
        ("retention_policies", "updated_at", "TIMESTAMP"),
        # Consent
        ("candidates", "consent_given", "INTEGER DEFAULT 0"),
        ("candidates", "consent_at", "TIMESTAMP"),
        ("candidates", "gdpr_delete_requested", "INTEGER DEFAULT 0"),
    ]
    for table, col, coltype in c18_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 19 TABLES ========================

    conn.executescript("""
    -- Subscription plans catalog
    CREATE TABLE IF NOT EXISTS subscription_plans (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT UNIQUE NOT NULL,
        description TEXT,
        price_monthly INTEGER DEFAULT 0,
        price_annual INTEGER DEFAULT 0,
        currency TEXT DEFAULT 'usd',
        max_interviews INTEGER DEFAULT 5,
        max_candidates INTEGER DEFAULT 50,
        max_team_members INTEGER DEFAULT 2,
        max_video_storage_gb INTEGER DEFAULT 5,
        features_json TEXT,
        stripe_price_monthly TEXT,
        stripe_price_annual TEXT,
        is_active INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Customer subscriptions
    CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT UNIQUE,
        status TEXT DEFAULT 'trialing',
        billing_cycle TEXT DEFAULT 'monthly',
        current_period_start TIMESTAMP,
        current_period_end TIMESTAMP,
        trial_end TIMESTAMP,
        cancel_at_period_end INTEGER DEFAULT 0,
        canceled_at TIMESTAMP,
        payment_method_last4 TEXT,
        payment_method_brand TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (plan_id) REFERENCES subscription_plans(id)
    );

    -- Payment / invoice history
    CREATE TABLE IF NOT EXISTS invoices (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        subscription_id TEXT,
        stripe_invoice_id TEXT UNIQUE,
        amount INTEGER DEFAULT 0,
        currency TEXT DEFAULT 'usd',
        status TEXT DEFAULT 'draft',
        description TEXT,
        invoice_pdf_url TEXT,
        hosted_invoice_url TEXT,
        period_start TIMESTAMP,
        period_end TIMESTAMP,
        paid_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
    );

    -- Usage tracking for metered billing
    CREATE TABLE IF NOT EXISTS usage_records (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        quantity INTEGER DEFAULT 1,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    -- Video assets with cloud storage metadata
    CREATE TABLE IF NOT EXISTS video_assets (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        candidate_id TEXT,
        interview_id TEXT,
        question_id TEXT,
        storage_backend TEXT DEFAULT 'local',
        storage_key TEXT NOT NULL,
        original_filename TEXT,
        content_type TEXT DEFAULT 'video/webm',
        file_size INTEGER DEFAULT 0,
        duration_seconds REAL,
        resolution TEXT,
        thumbnail_key TEXT,
        transcode_status TEXT DEFAULT 'pending',
        transcode_job_id TEXT,
        transcoded_key TEXT,
        transcoded_size INTEGER,
        cdn_url TEXT,
        presign_expires_at TIMESTAMP,
        is_intro INTEGER DEFAULT 0,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    -- Candidate interview sessions (for candidate-facing experience)
    CREATE TABLE IF NOT EXISTS candidate_sessions (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        interview_id TEXT NOT NULL,
        session_token TEXT UNIQUE NOT NULL,
        device_type TEXT,
        browser TEXT,
        os TEXT,
        screen_resolution TEXT,
        camera_device TEXT,
        mic_device TEXT,
        network_quality TEXT,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        last_active_at TIMESTAMP,
        current_question_index INTEGER DEFAULT 0,
        total_retakes_used INTEGER DEFAULT 0,
        progress_pct INTEGER DEFAULT 0,
        status TEXT DEFAULT 'started',
        error_log TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id),
        FOREIGN KEY (interview_id) REFERENCES interviews(id)
    );
    """)

    # Cycle 19 migrations - add columns to existing tables
    c19_migrations = [
        # Subscription fields on users
        ("users", "stripe_customer_id", "TEXT"),
        ("users", "subscription_id", "TEXT"),
        ("users", "subscription_status", "TEXT DEFAULT 'trialing'"),
        ("users", "trial_ends_at", "TIMESTAMP"),
        ("users", "usage_video_storage_mb", "INTEGER DEFAULT 0"),
        ("users", "usage_interviews_count", "INTEGER DEFAULT 0"),
        ("users", "usage_candidates_count", "INTEGER DEFAULT 0"),
        # Video storage fields on responses table
        ("responses", "video_asset_id", "TEXT"),
        ("responses", "duration_seconds", "REAL"),
        ("responses", "file_size", "INTEGER"),
        ("responses", "transcode_status", "TEXT DEFAULT 'none'"),
        # Candidate experience fields
        ("candidates", "device_type", "TEXT"),
        ("candidates", "browser_info", "TEXT"),
        ("candidates", "session_id", "TEXT"),
        ("candidates", "camera_tested", "INTEGER DEFAULT 0"),
        ("candidates", "mic_tested", "INTEGER DEFAULT 0"),
        ("candidates", "experience_rating", "INTEGER"),
        ("candidates", "experience_feedback", "TEXT"),
        # Candidate sessions - add missing columns from old C16 table
        ("candidate_sessions", "interview_id", "TEXT"),
        ("candidate_sessions", "session_token", "TEXT"),
        ("candidate_sessions", "device_type", "TEXT"),
        ("candidate_sessions", "browser", "TEXT"),
        ("candidate_sessions", "os", "TEXT"),
        ("candidate_sessions", "screen_resolution", "TEXT"),
        ("candidate_sessions", "camera_device", "TEXT"),
        ("candidate_sessions", "mic_device", "TEXT"),
        ("candidate_sessions", "network_quality", "TEXT"),
        ("candidate_sessions", "last_active_at", "TIMESTAMP"),
        ("candidate_sessions", "current_question_index", "INTEGER DEFAULT 0"),
        ("candidate_sessions", "total_retakes_used", "INTEGER DEFAULT 0"),
        ("candidate_sessions", "progress_pct", "INTEGER DEFAULT 0"),
        ("candidate_sessions", "status", "TEXT DEFAULT 'started'"),
        ("candidate_sessions", "error_log", "TEXT"),
        ("candidate_sessions", "created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        # Interview branding for candidate-facing
        ("interviews", "landing_page_html", "TEXT"),
        ("interviews", "logo_url_override", "TEXT"),
        ("interviews", "accent_color", "TEXT"),
        ("interviews", "show_progress_bar", "INTEGER DEFAULT 1"),
        ("interviews", "allow_retakes", "INTEGER DEFAULT 1"),
        ("interviews", "require_camera_test", "INTEGER DEFAULT 1"),
        ("interviews", "mobile_enabled", "INTEGER DEFAULT 1"),
    ]
    for table, col, coltype in c19_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Seed default subscription plans if none exist
    count_row = conn.execute("SELECT COUNT(*) as cnt FROM subscription_plans").fetchone()
    existing_plans = count_row['cnt'] if isinstance(count_row, dict) else count_row[0]
    if existing_plans == 0:
        import uuid as _uuid
        plans = [
            (_uuid.uuid4().hex, 'Starter', 'starter', 'Essential recruiting tools for individual agencies', 9900, 95000, 5, 50, 3, 5, '["basic_interviews","email_support"]', 0),
            (_uuid.uuid4().hex, 'Professional', 'professional', 'Full-featured recruiting suite with AI and integrations', 17900, 172000, 30, 250, 15, 50, '["ai_scoring","white_label","bulk_ops","api_access","integrations","advanced_analytics","priority_support"]', 1),
            (_uuid.uuid4().hex, 'Enterprise', 'enterprise', 'Unlimited recruiting power for large organizations', 29900, 287000, -1, -1, -1, 500, '["ai_scoring","white_label","bulk_ops","api_access","integrations","advanced_analytics","custom_domain","sso","dedicated_support"]', 2),
        ]
        for p in plans:
            conn.execute("""INSERT INTO subscription_plans
                (id, name, slug, description, price_monthly, price_annual, max_interviews, max_candidates, max_team_members, max_video_storage_gb, features_json, sort_order)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", p)

    # ======================== CYCLE 20 TABLES ========================

    conn.executescript("""
    -- Outgoing webhooks for real-time integrations
    CREATE TABLE IF NOT EXISTS outgoing_webhooks (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        url TEXT NOT NULL,
        events_json TEXT DEFAULT '[]',
        secret TEXT,
        is_active INTEGER DEFAULT 1,
        last_triggered_at TIMESTAMP,
        last_status_code INTEGER,
        failure_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    """)

    # Cycle 20 migrations
    c20_migrations = [
        # User notification preferences
        ("users", "notification_prefs", "TEXT DEFAULT '{}'"),
        ("users", "push_token", "TEXT"),
        # Notifications table enhancements
        ("notifications", "type", "TEXT DEFAULT 'info'"),
        ("notifications", "title", "TEXT"),
        ("notifications", "link", "TEXT"),
        ("notifications", "priority", "TEXT DEFAULT 'normal'"),
        # Responses: AI transcript fields
        ("responses", "ai_transcript", "TEXT"),
        ("responses", "transcript_method", "TEXT"),
        ("responses", "transcript_confidence", "REAL"),
    ]
    for table, col, coltype in c20_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 21 TABLES ========================

    conn.executescript("""
    -- AMS/CRM Integration connections
    CREATE TABLE IF NOT EXISTS ams_connections (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        provider_name TEXT NOT NULL,
        api_key_encrypted TEXT,
        api_url TEXT,
        status TEXT DEFAULT 'disconnected',
        last_sync_at TIMESTAMP,
        sync_direction TEXT DEFAULT 'bidirectional',
        field_mapping_json TEXT DEFAULT '{}',
        sync_errors_json TEXT DEFAULT '[]',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    -- AMS sync log for audit trail
    CREATE TABLE IF NOT EXISTS ams_sync_log (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        sync_type TEXT NOT NULL,
        records_synced INTEGER DEFAULT 0,
        records_failed INTEGER DEFAULT 0,
        error_details TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        status TEXT DEFAULT 'running',
        FOREIGN KEY (connection_id) REFERENCES ams_connections(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    -- Public API keys
    CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        key_prefix TEXT NOT NULL,
        scopes_json TEXT DEFAULT '["read"]',
        rate_limit_per_hour INTEGER DEFAULT 1000,
        last_used_at TIMESTAMP,
        usage_count INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    -- API request log
    CREATE TABLE IF NOT EXISTS api_request_log (
        id TEXT PRIMARY KEY,
        api_key_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        status_code INTEGER,
        response_time_ms INTEGER,
        ip_address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (api_key_id) REFERENCES api_keys(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    -- Demo/sandbox environments
    CREATE TABLE IF NOT EXISTS demo_environments (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        slug TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'active',
        seed_profile TEXT DEFAULT 'standard',
        expires_at TIMESTAMP,
        access_password TEXT,
        view_count INTEGER DEFAULT 0,
        last_accessed_at TIMESTAMP,
        settings_json TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    """)

    # Cycle 21 migrations
    c21_migrations = [
        # Users: API access tracking
        ("users", "api_enabled", "INTEGER DEFAULT 0"),
        ("users", "api_rate_limit", "INTEGER DEFAULT 1000"),
        # Users: AMS connection tracking
        ("users", "ams_provider", "TEXT"),
        ("users", "ams_connected_at", "TIMESTAMP"),
    ]
    for table, col, coltype in c21_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 22 TABLES ========================

    conn.executescript("""
    -- Analytics snapshots for dashboard metrics
    CREATE TABLE IF NOT EXISTS analytics_snapshots (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        metric_type TEXT NOT NULL,
        metric_value REAL NOT NULL,
        dimension TEXT,
        period TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    -- Landing page leads
    CREATE TABLE IF NOT EXISTS landing_page_leads (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        name TEXT,
        agency_name TEXT,
        phone TEXT,
        source TEXT DEFAULT 'landing_page',
        status TEXT DEFAULT 'new',
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # ======================== CYCLE 23 TABLES ========================

    # Email delivery log
    conn.execute("""
    CREATE TABLE IF NOT EXISTS email_log (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        recipient_email TEXT NOT NULL,
        recipient_name TEXT,
        subject TEXT NOT NULL,
        template TEXT NOT NULL,
        status TEXT DEFAULT 'queued',
        provider TEXT DEFAULT 'sendgrid',
        provider_message_id TEXT,
        error_message TEXT,
        opened_at TIMESTAMP,
        clicked_at TIMESTAMP,
        bounced_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sent_at TIMESTAMP
    );
    """)

    # Email templates (customizable)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS email_delivery_templates (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        name TEXT NOT NULL,
        subject TEXT NOT NULL,
        body_html TEXT NOT NULL,
        body_text TEXT,
        template_type TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        variables TEXT DEFAULT '[]',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP
    );
    """)

    # Onboarding progress
    conn.execute("""
    CREATE TABLE IF NOT EXISTS onboarding_progress (
        id TEXT PRIMARY KEY,
        user_id TEXT UNIQUE NOT NULL,
        current_step INTEGER DEFAULT 1,
        total_steps INTEGER DEFAULT 6,
        completed_steps TEXT DEFAULT '[]',
        agency_profile_done INTEGER DEFAULT 0,
        first_interview_done INTEGER DEFAULT 0,
        first_candidate_done INTEGER DEFAULT 0,
        branding_done INTEGER DEFAULT 0,
        team_invite_done INTEGER DEFAULT 0,
        ams_connected INTEGER DEFAULT 0,
        completed_at TIMESTAMP,
        skipped_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP
    );
    """)

    # Help articles
    conn.execute("""
    CREATE TABLE IF NOT EXISTS help_articles (
        id TEXT PRIMARY KEY,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        category TEXT NOT NULL,
        content TEXT NOT NULL,
        related_page TEXT,
        sort_order INTEGER DEFAULT 0,
        is_published INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP
    );
    """)

    # Cycle 22 migrations
    c22_migrations = [
        # Users: analytics preferences
        ("users", "analytics_dashboard_json", "TEXT DEFAULT '{}'"),
        # Interviews: tracking fields for analytics
        ("interviews", "archived_at", "TIMESTAMP"),
        # Candidates: analytics timing fields
        ("candidates", "time_to_complete_hours", "REAL"),
        ("candidates", "reviewed_at", "TIMESTAMP"),
    ]
    for table, col, coltype in c22_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Cycle 23 migrations
    c23_migrations = [
        ("users", "onboarding_completed", "INTEGER DEFAULT 0"),
        ("users", "email_provider", "TEXT DEFAULT 'internal'"),
        ("users", "sendgrid_api_key_hash", "TEXT"),
        ("users", "email_from_name", "TEXT"),
        ("users", "email_from_address", "TEXT"),
        # email_log columns that may be missing from older schema
        ("email_log", "recipient_email", "TEXT"),
        ("email_log", "recipient_name", "TEXT"),
        ("email_log", "template", "TEXT DEFAULT 'custom'"),
        ("email_log", "provider", "TEXT DEFAULT 'internal'"),
        ("email_log", "provider_message_id", "TEXT"),
        ("email_log", "opened_at", "TIMESTAMP"),
        ("email_log", "clicked_at", "TIMESTAMP"),
        ("email_log", "bounced_at", "TIMESTAMP"),
        ("email_log", "created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]
    for table, col, coltype in c23_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Seed default help articles
    help_row = conn.execute("SELECT COUNT(*) as cnt FROM help_articles").fetchone()
    existing_help = help_row['cnt'] if isinstance(help_row, dict) else help_row[0]
    if existing_help == 0:
        import uuid
        from datetime import datetime
        help_articles = [
            ('getting-started', 'Getting Started with ChannelView', 'getting-started',
             'Welcome to ChannelView! This guide walks you through setting up your account, creating your first interview, and inviting candidates. ChannelView is built specifically for insurance agencies, FMOs, and IMOs to streamline their hiring process with one-way video interviews.', 'dashboard', 1),
            ('create-interview', 'Creating Your First Interview', 'interviews',
             'To create an interview, navigate to Interviews and click "New Interview." Give it a title (e.g., "Licensed Insurance Agent - Q2"), add a position description, then add questions from the question bank or create custom ones. You can set time limits per question and add preparation prompts.', 'interviews', 2),
            ('invite-candidates', 'Inviting Candidates', 'candidates',
             'After creating an interview, add candidates by name and email. ChannelView sends them a branded invitation with a unique link. Candidates record their answers on their own schedule — no coordination needed. Track their status in the pipeline view.', 'candidates', 3),
            ('ai-scoring-guide', 'Understanding AI Scoring', 'ai-features',
             'ChannelView uses AI to score candidate responses across five categories: communication skills, industry knowledge, professionalism, problem-solving, and cultural fit. Scores range from 0-100. Use batch scoring to evaluate multiple candidates at once. AI scores are recommendations — always review responses yourself before making decisions.', 'ai-scoring', 4),
            ('ams-setup', 'Connecting Your AMS', 'integrations',
             'ChannelView integrates with AgencyBloc, HawkSoft, and EZLynx. Navigate to AMS Integrations, select your provider, and enter your API credentials. Once connected, candidate data syncs automatically between your AMS and ChannelView. You can customize field mapping to match your workflow.', 'ams-integrations', 5),
            ('white-label-setup', 'White Labeling Your Portal', 'customization',
             'Make ChannelView your own with white-label branding. Upload your agency logo, set primary and secondary colors, customize the candidate-facing portal with your agency name and messaging. Available on Professional and Enterprise plans.', 'white-label', 6),
            ('fmo-portal-guide', 'Using the FMO Portal', 'fmo',
             'The FMO Portal gives you oversight across all your downline agencies. View aggregate metrics, manage agency accounts, and share interview templates. Perfect for FMOs and IMOs who need to standardize hiring across their agency network.', 'fmo', 7),
            ('email-delivery', 'Setting Up Email Delivery', 'email',
             'ChannelView can send candidate invitations and notifications through its built-in email system or through your SendGrid account. Go to Settings > Email Delivery to configure. Using your own SendGrid account lets you send from your agency domain for better deliverability and brand consistency.', 'settings', 8),
            ('analytics-guide', 'Reading Your Analytics Dashboard', 'analytics',
             'The Analytics Dashboard shows key hiring metrics: completion rates, pipeline funnel, weekly trends, per-interview performance, and ROI estimates. Use these metrics to optimize your hiring process and demonstrate value to your agency leadership.', 'analytics-dashboard', 9),
            ('api-access', 'Using the Public API', 'developers',
             'ChannelView offers a REST API for programmatic access. Generate API keys in API Management, set scopes (read/write/admin), and integrate with your existing tools. Rate limits are applied per key. Full API documentation is available at /api-docs.', 'api-management', 10),
        ]
        for slug, title, category, content, page, order in help_articles:
            conn.execute(
                "INSERT INTO help_articles (id, slug, title, category, content, related_page, sort_order) VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, slug, title, category, content, page, order)
            )

    # ======================== CYCLE 24 TABLES ========================

    # User preferences
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_preferences (
        id TEXT PRIMARY KEY,
        user_id TEXT UNIQUE NOT NULL,
        timezone TEXT DEFAULT 'America/New_York',
        date_format TEXT DEFAULT 'MM/DD/YYYY',
        language TEXT DEFAULT 'en',
        email_notifications INTEGER DEFAULT 1,
        browser_notifications INTEGER DEFAULT 1,
        weekly_digest INTEGER DEFAULT 1,
        candidate_alerts INTEGER DEFAULT 1,
        theme TEXT DEFAULT 'light',
        sidebar_collapsed INTEGER DEFAULT 0,
        default_interview_duration INTEGER DEFAULT 30,
        default_question_time_limit INTEGER DEFAULT 120,
        auto_advance_candidates INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP
    );
    """)

    # Data exports
    conn.execute("""
    CREATE TABLE IF NOT EXISTS data_exports (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        export_type TEXT NOT NULL,
        format TEXT DEFAULT 'csv',
        status TEXT DEFAULT 'pending',
        file_path TEXT,
        file_size_bytes INTEGER,
        record_count INTEGER,
        filters_json TEXT DEFAULT '{}',
        error_message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    );
    """)

    # Data imports
    conn.execute("""
    CREATE TABLE IF NOT EXISTS data_imports (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        import_type TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        file_name TEXT,
        total_records INTEGER DEFAULT 0,
        imported_records INTEGER DEFAULT 0,
        skipped_records INTEGER DEFAULT 0,
        error_records INTEGER DEFAULT 0,
        errors_json TEXT DEFAULT '[]',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    );
    """)

    # Saved searches
    conn.execute("""
    CREATE TABLE IF NOT EXISTS saved_searches (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        query TEXT NOT NULL,
        filters_json TEXT DEFAULT '{}',
        result_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_used_at TIMESTAMP
    );
    """)

    # Cycle 24 migrations
    c24_migrations = [
        ("users", "profile_bio", "TEXT"),
        ("users", "profile_title", "TEXT"),
        ("users", "profile_phone", "TEXT"),
        ("users", "last_search_query", "TEXT"),
    ]
    for table, col, coltype in c24_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 25 ========================
    # Rate Limiting & Security Hardening, Activity Log Dashboard, Mobile-Responsive

    # Failed login attempts tracking
    conn.execute("""
    CREATE TABLE IF NOT EXISTS login_attempts (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        ip_address TEXT,
        user_agent TEXT,
        success INTEGER DEFAULT 0,
        failure_reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Account lockouts
    conn.execute("""
    CREATE TABLE IF NOT EXISTS account_lockouts (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        locked_until TIMESTAMP NOT NULL,
        reason TEXT DEFAULT 'too_many_failed_attempts',
        attempts_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        unlocked_at TIMESTAMP
    );
    """)

    # Security events (broader than login — password changes, role changes, suspicious activity)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS security_events (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        event_type TEXT NOT NULL,
        severity TEXT DEFAULT 'info',
        ip_address TEXT,
        user_agent TEXT,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Cycle 25 migrations
    c25_migrations = [
        ("users", "failed_login_count", "INTEGER DEFAULT 0"),
        ("users", "locked_until", "TIMESTAMP"),
        ("users", "last_login_at", "TIMESTAMP"),
        ("users", "last_login_ip", "TEXT"),
        ("users", "password_changed_at", "TIMESTAMP"),
        ("audit_log", "ip_address", "TEXT"),
        ("audit_log", "user_agent", "TEXT"),
        ("audit_log", "entity_type", "TEXT"),
        ("audit_log", "entity_id", "TEXT"),
    ]
    for table, col, coltype in c25_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Cycle 26-27 migrations (trial & production hardening)
    c26_migrations = [
        ("users", "trial_ends_at", "TIMESTAMP"),
        ("users", "welcome_email_sent", "INTEGER DEFAULT 0"),
        ("users", "trial_warning_sent", "INTEGER DEFAULT 0"),
    ]
    for table, col, coltype in c26_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Cycle 30 migrations (onboarding & billing)
    c30_migrations = [
        ("users", "onboarding_completed", "INTEGER DEFAULT 0"),
        ("users", "password_changed", "INTEGER DEFAULT 0"),
        ("users", "phone", "TEXT"),
    ]
    for table, col, coltype in c30_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Cycle 31: Job Boards & Pipeline Power-Up
    # New tables
    conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_stages (
        id TEXT PRIMARY KEY,
        interview_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
        stage_order INTEGER DEFAULT 0,
        color TEXT DEFAULT '#6b7280',
        require_notes INTEGER DEFAULT 0,
        is_terminal INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (interview_id) REFERENCES interviews(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS auto_stage_rules (
        id TEXT PRIMARY KEY,
        interview_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        rule_type TEXT NOT NULL,
        trigger_value TEXT,
        from_stage TEXT,
        to_stage TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (interview_id) REFERENCES interviews(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    c31_migrations = [
        ("candidates", "source_detail", "TEXT"),
        ("candidates", "source_utm", "TEXT"),
        ("candidates", "stage_entered_at", "TIMESTAMP"),
        ("candidates", "days_in_stage", "INTEGER DEFAULT 0"),
        ("interviews", "job_board_enabled", "INTEGER DEFAULT 0"),
        ("interviews", "location", "TEXT"),
        ("interviews", "job_type", "TEXT DEFAULT 'full_time'"),
        ("interviews", "salary_range", "TEXT"),
        ("interviews", "application_deadline", "TIMESTAMP"),
        ("interviews", "custom_stages_enabled", "INTEGER DEFAULT 0"),
    ]
    for table, col, coltype in c31_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 32: CANDIDATE EXPERIENCE ========================
    # Apply Page Redesign, Candidate Progress Tracker, Interview Prep, Thank You Pages

    # Candidate status updates (public-facing progress notifications)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS candidate_status_updates (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL,
        message TEXT,
        is_public INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
    );
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cstatus_candidate ON candidate_status_updates(candidate_id)")
    except:
        pass

    c32_migrations = [
        ("interviews", "apply_instructions", "TEXT"),
        ("interviews", "apply_fields_json", "TEXT DEFAULT '[]'"),
        ("interviews", "prep_video_url", "TEXT"),
        ("interviews", "prep_instructions", "TEXT"),
        ("interviews", "estimated_duration_min", "INTEGER DEFAULT 15"),
        ("interviews", "thank_you_message", "TEXT"),
        ("interviews", "thank_you_next_steps", "TEXT"),
        ("interviews", "show_progress_tracker", "INTEGER DEFAULT 1"),
        ("candidates", "apply_answers_json", "TEXT DEFAULT '{}'"),
        ("candidates", "resume_url", "TEXT"),
        ("candidates", "linkedin_url", "TEXT"),
        ("candidates", "cover_letter", "TEXT"),
        ("candidates", "progress_viewed_at", "TIMESTAMP"),
    ]
    for table, col, coltype in c32_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 33: Lead Sourcing Engine ========================
    # Features: Lead Sourcing Hub, CSV Import, ZIP Search, Lead-to-Pipeline,
    #           Indeed Job Sync, Google Jobs Markup, Referral Link Tracking

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sourced_leads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            zip_code TEXT,
            city TEXT,
            state TEXT,
            license_type TEXT,
            license_number TEXT,
            npn TEXT,
            license_status TEXT DEFAULT 'unknown',
            license_expiry TEXT,
            years_licensed INTEGER,
            source TEXT DEFAULT 'manual',
            source_file TEXT,
            tags TEXT DEFAULT '[]',
            notes TEXT,
            status TEXT DEFAULT 'new',
            converted_candidate_id TEXT,
            converted_at TIMESTAMP,
            last_contacted_at TIMESTAMP,
            contact_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_import_batches (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            total_rows INTEGER DEFAULT 0,
            imported_rows INTEGER DEFAULT 0,
            duplicate_rows INTEGER DEFAULT 0,
            error_rows INTEGER DEFAULT 0,
            column_mapping TEXT DEFAULT '{}',
            status TEXT DEFAULT 'completed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_links (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            interview_id TEXT,
            code TEXT NOT NULL UNIQUE,
            label TEXT,
            clicks INTEGER DEFAULT 0,
            applications INTEGER DEFAULT 0,
            hires INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (interview_id) REFERENCES interviews(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_syndications (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            interview_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            external_job_id TEXT,
            status TEXT DEFAULT 'draft',
            posted_at TIMESTAMP,
            expires_at TIMESTAMP,
            clicks INTEGER DEFAULT 0,
            applications INTEGER DEFAULT 0,
            sync_data TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (interview_id) REFERENCES interviews(id)
        )
    """)

    c33_migrations = [
        ("interviews", "indeed_feed_enabled", "INTEGER DEFAULT 0"),
        ("interviews", "google_jobs_enabled", "INTEGER DEFAULT 1"),
        ("interviews", "job_category", "TEXT DEFAULT 'insurance'"),
        ("interviews", "experience_level", "TEXT DEFAULT 'entry'"),
        ("interviews", "compensation_type", "TEXT DEFAULT 'commission'"),
        ("interviews", "remote_option", "TEXT DEFAULT 'onsite'"),
        ("candidates", "referral_link_id", "TEXT"),
        ("candidates", "referral_code", "TEXT"),
        ("candidates", "sourced_lead_id", "TEXT"),
    ]
    for table, col, coltype in c33_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 34: VOICE AGENT TABLES ========================

    # Voice agent configuration per account
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_agents (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT 'Recruiting Agent',
            agent_type TEXT NOT NULL DEFAULT 'scheduling',
            retell_agent_id TEXT,
            retell_phone_number TEXT,
            voice_id TEXT DEFAULT 'eleven_labs_rachel',
            language TEXT DEFAULT 'en-US',
            greeting_script TEXT DEFAULT 'Hi, this is the recruiting team calling about an opportunity we think you would be great for.',
            persona_prompt TEXT,
            max_call_duration INTEGER DEFAULT 300,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_agents_user ON voice_agents(user_id)")

    # Voice call scripts / conversation templates
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_scripts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            name TEXT NOT NULL,
            script_type TEXT NOT NULL DEFAULT 'scheduling',
            purpose TEXT,
            conversation_flow TEXT DEFAULT '{}',
            variables TEXT DEFAULT '[]',
            active INTEGER DEFAULT 1,
            use_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (agent_id) REFERENCES voice_agents(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_scripts_agent ON voice_scripts(agent_id)")

    # Individual voice call records
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_calls (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            candidate_id TEXT,
            script_id TEXT,
            retell_call_id TEXT UNIQUE,
            direction TEXT NOT NULL DEFAULT 'outbound',
            status TEXT DEFAULT 'queued',
            phone_number TEXT NOT NULL,
            duration_seconds INTEGER,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            transcript TEXT,
            summary TEXT,
            sentiment_score REAL,
            outcome TEXT,
            outcome_details TEXT DEFAULT '{}',
            recording_url TEXT,
            cost_cents INTEGER DEFAULT 0,
            error_message TEXT,
            metadata TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (agent_id) REFERENCES voice_agents(id),
            FOREIGN KEY (candidate_id) REFERENCES candidates(id),
            FOREIGN KEY (script_id) REFERENCES voice_scripts(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_calls_user ON voice_calls(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_calls_candidate ON voice_calls(candidate_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_calls_status ON voice_calls(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_calls_retell ON voice_calls(retell_call_id)")

    # Scheduled voice calls (queue for future calls)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_call_schedule (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            script_id TEXT,
            scheduled_at TIMESTAMP NOT NULL,
            call_type TEXT NOT NULL DEFAULT 'scheduling',
            priority INTEGER DEFAULT 5,
            attempt_count INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 3,
            last_attempt_at TIMESTAMP,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (agent_id) REFERENCES voice_agents(id),
            FOREIGN KEY (candidate_id) REFERENCES candidates(id),
            FOREIGN KEY (script_id) REFERENCES voice_scripts(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_schedule_status ON voice_call_schedule(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_schedule_time ON voice_call_schedule(scheduled_at)")

    # Voice agent analytics / daily rollups
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_analytics (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            total_calls INTEGER DEFAULT 0,
            connected_calls INTEGER DEFAULT 0,
            avg_duration_seconds INTEGER DEFAULT 0,
            interviews_scheduled INTEGER DEFAULT 0,
            candidates_engaged INTEGER DEFAULT 0,
            candidates_at_risk INTEGER DEFAULT 0,
            total_cost_cents INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_analytics_user_date ON voice_analytics(user_id, date)")

    # Candidate engagement tracking (voice-specific touchpoints)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_engagement (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            engagement_type TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'voice',
            details TEXT DEFAULT '{}',
            engagement_score REAL,
            risk_level TEXT DEFAULT 'low',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_engagement_candidate ON candidate_engagement(candidate_id)")

    # C34 migrations — add voice-related columns to existing tables
    c34_migrations = [
        ("candidates", "voice_consent", "INTEGER DEFAULT 0"),
        ("candidates", "preferred_call_time", "TEXT"),
        ("candidates", "last_voice_contact", "TIMESTAMP"),
        ("candidates", "voice_engagement_score", "REAL"),
        ("candidates", "voice_risk_level", "TEXT DEFAULT 'unknown'"),
        ("users", "retell_api_key", "TEXT"),
        ("users", "voice_agent_enabled", "INTEGER DEFAULT 0"),
        ("users", "voice_caller_id", "TEXT"),
    ]
    for table, col, coltype in c34_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 29: FIRST INTERVIEW HUB ========================
    # Features: Multi-Format Interviews, Group Info Sessions, Waterfall Engagement,
    #           Candidate Format Selector, RSVP Tracking, One-on-One Booking

    # Group info sessions (in-person or virtual recurring sessions)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_sessions (
            id TEXT PRIMARY KEY,
            interview_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            session_type TEXT NOT NULL DEFAULT 'in_person',
            location TEXT,
            meeting_url TEXT,
            session_date TIMESTAMP NOT NULL,
            duration_minutes INTEGER DEFAULT 60,
            capacity INTEGER DEFAULT 0,
            status TEXT DEFAULT 'scheduled',
            recurring_rule TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (interview_id) REFERENCES interviews(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gsess_interview ON group_sessions(interview_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gsess_user ON group_sessions(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gsess_date ON group_sessions(session_date)")
    except:
        pass

    # RSVP tracking for group sessions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_rsvps (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            status TEXT DEFAULT 'rsvp',
            rsvp_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            confirmed_at TIMESTAMP,
            attended_at TIMESTAMP,
            cancelled_at TIMESTAMP,
            notes TEXT,
            FOREIGN KEY (session_id) REFERENCES group_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rsvp_session ON session_rsvps(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rsvp_candidate ON session_rsvps(candidate_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_rsvp_unique ON session_rsvps(session_id, candidate_id)")
    except:
        pass

    # One-on-one booking slots (recruiter availability)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS booking_slots (
            id TEXT PRIMARY KEY,
            interview_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            slot_date TIMESTAMP NOT NULL,
            duration_minutes INTEGER DEFAULT 30,
            slot_type TEXT DEFAULT 'recruiter_call',
            meeting_url TEXT,
            phone_number TEXT,
            location TEXT,
            is_booked INTEGER DEFAULT 0,
            booked_by_candidate_id TEXT,
            booked_at TIMESTAMP,
            status TEXT DEFAULT 'available',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (interview_id) REFERENCES interviews(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bslot_interview ON booking_slots(interview_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bslot_date ON booking_slots(slot_date)")
    except:
        pass

    c29_migrations = [
        # Interview format configuration
        ("interviews", "formats_enabled", "TEXT DEFAULT '[\"video\"]'"),
        ("interviews", "format_selector_enabled", "INTEGER DEFAULT 0"),
        ("interviews", "waterfall_enabled", "INTEGER DEFAULT 0"),
        ("interviews", "waterfall_config_json", "TEXT DEFAULT '{}'"),
        ("interviews", "group_session_description", "TEXT"),
        ("interviews", "one_on_one_description", "TEXT"),
        ("interviews", "one_on_one_type", "TEXT DEFAULT 'recruiter_call'"),
        # Candidate format tracking
        ("candidates", "interview_format", "TEXT DEFAULT 'video'"),
        ("candidates", "waterfall_stage", "TEXT"),
        ("candidates", "waterfall_next_at", "TIMESTAMP"),
        ("candidates", "waterfall_step_index", "INTEGER DEFAULT 0"),
        ("candidates", "format_chosen_at", "TIMESTAMP"),
        ("candidates", "session_rsvp_id", "TEXT"),
        ("candidates", "booking_slot_id", "TEXT"),
    ]
    for table, col, coltype in c29_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Cycle 37: Add location column to booking_slots for in-person meetings
    try:
        conn.execute("ALTER TABLE booking_slots ADD COLUMN location TEXT")
    except:
        pass

    # ======================== CYCLE 29B: SCHEDULING & 2ND INTERVIEWS ========================
    # Recurring availability patterns, conflict detection, 2nd interview scheduling

    # Recruiter availability patterns ("box the repetitive")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS availability_patterns (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'My Availability',
            day_of_week INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            slot_duration_minutes INTEGER DEFAULT 30,
            slot_type TEXT DEFAULT 'recruiter_call',
            meeting_url TEXT,
            phone_number TEXT,
            location TEXT,
            is_active INTEGER DEFAULT 1,
            generate_weeks_ahead INTEGER DEFAULT 4,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_avail_user ON availability_patterns(user_id)")
    except:
        pass

    # 2nd interview scheduling — always 1-on-1, recruiter picks method
    conn.execute("""
        CREATE TABLE IF NOT EXISTS second_interviews (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            interview_id TEXT NOT NULL,
            schedule_method TEXT NOT NULL DEFAULT 'email',
            status TEXT DEFAULT 'pending',
            scheduled_date TIMESTAMP,
            duration_minutes INTEGER DEFAULT 30,
            meeting_type TEXT DEFAULT 'phone',
            meeting_url TEXT,
            phone_number TEXT,
            location TEXT,
            booking_slot_id TEXT,
            notes TEXT,
            recruiter_notes TEXT,
            outcome TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scheduled_at TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (candidate_id) REFERENCES candidates(id),
            FOREIGN KEY (interview_id) REFERENCES interviews(id)
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_2nd_user ON second_interviews(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_2nd_candidate ON second_interviews(candidate_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_2nd_date ON second_interviews(scheduled_date)")
    except:
        pass

    c29b_migrations = [
        # 2nd interview tracking on candidate
        ("candidates", "second_interview_id", "TEXT"),
        ("candidates", "second_interview_status", "TEXT"),
        ("candidates", "second_interview_date", "TIMESTAMP"),
        # Availability pattern link on booking slots
        ("booking_slots", "pattern_id", "TEXT"),
        ("booking_slots", "interview_stage", "TEXT DEFAULT 'first'"),
    ]
    for table, col, coltype in c29b_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 29C: POST-INTERVIEW PIPELINE & ENGAGEMENT ========================
    # Full candidate lifecycle: Offer → Testing → Appointment → Writing Number → Production
    # Engagement tracking: every call, email, AI contact logged and tracked

    # Pipeline touchpoints — every engagement action tracked
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_touchpoints (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            touchpoint_type TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'email',
            direction TEXT DEFAULT 'outbound',
            subject TEXT,
            body TEXT,
            status TEXT DEFAULT 'completed',
            notes TEXT,
            milestone_stage TEXT,
            scheduled_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_touch_candidate ON pipeline_touchpoints(candidate_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_touch_user ON pipeline_touchpoints(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_touch_stage ON pipeline_touchpoints(milestone_stage)")
    except:
        pass

    # Engagement automation rules — what to send at each milestone stage
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engagement_rules (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            interview_id TEXT,
            milestone_stage TEXT NOT NULL,
            trigger_type TEXT NOT NULL DEFAULT 'days_in_stage',
            trigger_days INTEGER DEFAULT 2,
            action_type TEXT NOT NULL DEFAULT 'email',
            email_subject TEXT,
            email_body TEXT,
            ai_voice_script TEXT,
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_erule_user ON engagement_rules(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_erule_stage ON engagement_rules(milestone_stage)")
    except:
        pass

    c29c_migrations = [
        # Pipeline milestone dates on candidates
        ("candidates", "is_licensed", "INTEGER DEFAULT 0"),
        ("candidates", "offer_made_at", "TIMESTAMP"),
        ("candidates", "offer_accepted_at", "TIMESTAMP"),
        ("candidates", "entered_testing_at", "TIMESTAMP"),
        ("candidates", "testing_date", "TIMESTAMP"),
        ("candidates", "passed_test_at", "TIMESTAMP"),
        ("candidates", "entered_appointment_at", "TIMESTAMP"),
        ("candidates", "received_writing_number_at", "TIMESTAMP"),
        ("candidates", "first_production_at", "TIMESTAMP"),
        ("candidates", "milestone_stage", "TEXT DEFAULT 'screening'"),
        ("candidates", "milestone_updated_at", "TIMESTAMP"),
        ("candidates", "days_since_milestone", "INTEGER DEFAULT 0"),
        ("candidates", "engagement_score_total", "REAL DEFAULT 0"),
        ("candidates", "last_touchpoint_at", "TIMESTAMP"),
        ("candidates", "touchpoint_count", "INTEGER DEFAULT 0"),
        ("candidates", "next_engagement_at", "TIMESTAMP"),
    ]
    for table, col, coltype in c29c_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 36: RSC TIME-SAVING TOOLS ========================

    # Screening call scripts (pre-loaded phone scripts with variations)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS screening_scripts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        script_type TEXT NOT NULL DEFAULT 'warm_lead',
        opening TEXT NOT NULL,
        qualifying_questions TEXT NOT NULL,
        opportunity_pitch TEXT NOT NULL,
        next_steps TEXT NOT NULL,
        objection_handling TEXT NOT NULL,
        is_system INTEGER DEFAULT 1,
        user_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_screening_scripts_type ON screening_scripts(script_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_screening_scripts_user ON screening_scripts(user_id)")

    # Scheduling templates (one-click copy/paste scheduling language)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS scheduling_templates (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        format_type TEXT NOT NULL DEFAULT 'group_virtual',
        subject_line TEXT NOT NULL,
        body_text TEXT NOT NULL,
        is_system INTEGER DEFAULT 1,
        user_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduling_templates_format ON scheduling_templates(format_type)")

    # Message templates (pre-filled messages for candidate communications)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS message_templates (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'invitation',
        tone TEXT DEFAULT 'professional',
        subject_line TEXT,
        body_text TEXT NOT NULL,
        is_system INTEGER DEFAULT 1,
        user_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_templates_category ON message_templates(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_templates_user ON message_templates(user_id)")
    conn.commit()

    # Seed screening scripts
    try:
        conn.commit()  # Ensure clean transaction state
        count = list(conn.execute("SELECT COUNT(*) FROM screening_scripts WHERE is_system=1").fetchone().values())[0]
        if count == 0:
            conn.execute("""INSERT INTO screening_scripts (id, name, script_type, opening, qualifying_questions, opportunity_pitch, next_steps, objection_handling, is_system)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""", (
                'script_warm_lead', 'Warm Lead Script', 'warm_lead',
                'Hi [CANDIDATE_NAME], this is [RSC_NAME] with [AGENCY_NAME]. Thanks so much for your interest in our Benefits Advisor opportunity! I saw you came across our opening and wanted to connect personally. Do you have a few minutes to chat about what we do and see if it might be a fit?',
                '["What made you interested in exploring this opportunity?","Tell me a little about your professional background  --  what are you doing currently?","Are you looking for something full-time, or more of a flexible side opportunity?","Whats most important to you in your next role  --  income potential, flexibility, helping people, or something else?","Have you ever worked in a commission-based or entrepreneurial role before?"]',
                'Great  --  so let me tell you a bit about what we do. We help families and individuals navigate their benefits options  --  health insurance, supplemental coverage, retirement planning. Think of it as being a trusted advisor, not a salesperson. The role is flexible  --  you set your own schedule, work from wherever you want, and there is genuinely uncapped earning potential. Most of our top performers started exactly where you are now, with no insurance background. We provide full training, mentorship, and licensing support.',
                'Here is what I would love to do  --  I would like to invite you to a short virtual information session where you can learn more, meet some of our team, and ask any questions. No commitment, no pressure. If after that you are excited, we will talk about next steps. Does [DATE] at [TIME] work for you?',
                'I totally understand the hesitation around commission-based work. What I can tell you is that our training program is designed to get you producing quickly  --  most new team members see their first paycheck within 2-3 weeks. And you are not doing this alone  --  you will have a mentor and a full support system. Why not come to the info session, get all your questions answered, and then decide?'
            ))
            conn.execute("""INSERT INTO screening_scripts (id, name, script_type, opening, qualifying_questions, opportunity_pitch, next_steps, objection_handling, is_system)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""", (
                'script_cold_outreach', 'Cold Outreach Script', 'cold_outreach',
                'Hi [CANDIDATE_NAME], this is [RSC_NAME] with [AGENCY_NAME]. I know I am catching you out of the blue, and I appreciate you taking the call. I am reaching out because we are expanding our team of Benefits Advisors in the [AREA] area, and your background caught my attention. Do you have just two minutes? I promise I will be respectful of your time.',
                '["Im curious  --  are you currently happy in your role, or are you open to exploring something new?","What do you do currently, if you dont mind me asking?","If the right opportunity came along  --  something flexible with strong earning potential  --  would you be open to hearing about it?","What would an ideal work situation look like for you?","Have you ever thought about working in financial services or helping people with their benefits?"]',
                'I appreciate your honesty. Here is the quick version  --  we help families and individuals with their insurance and benefits decisions. Health coverage, supplemental plans, retirement. It is a role where you genuinely help people, and the compensation reflects that. Our advisors set their own schedules, work remotely or in-person, and have access to full training even if they have zero insurance experience. We are growing fast, which is why I reached out.',
                'I know cold calls can feel random, so here is what I would suggest  --  let me send you a quick 3-minute video overview that explains what we do. No commitment, no follow-up pressure. If it resonates, you can book a time to chat further. Can I grab your email to send that over?',
                'I get it  --  unsolicited calls are not everyone is favorite thing, and I respect that. All I am asking for is 3 minutes of your time to watch a short video. If it is not for you, no hard feelings at all. But I would hate for you to miss out on something that could be a great fit just because of how you heard about it. Fair enough?'
            ))
            conn.execute("""INSERT INTO screening_scripts (id, name, script_type, opening, qualifying_questions, opportunity_pitch, next_steps, objection_handling, is_system)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""", (
                'script_referral', 'Referral Script', 'referral',
                'Hi [CANDIDATE_NAME], this is [RSC_NAME] with [AGENCY_NAME]. I am calling because [REFERRER_NAME] mentioned you might be a great fit for an opportunity on our team. They spoke really highly of you! Do you have a couple minutes? I would love to tell you what this is about.',
                '["[REFERRER_NAME] said some great things  --  can you tell me a little about your background?","What are you up to professionally right now?","[REFERRER_NAME] thought you would be great at this because of your people skills  --  would you say that is a strength of yours?","Are you open to exploring something new, even if it is different from what you are doing now?","What matters most to you in a career  --  flexibility, income, purpose, growth?"]',
                'So here is what [REFERRER_NAME] thought you would be great at  --  we are Benefits Advisors. We help families navigate health insurance, supplemental coverage, and retirement planning. It is a role that is all about building relationships and helping people make smart decisions about their benefits. The reason [REFERRER_NAME] thought of you is that the people who excel in this role are exactly the kind of people who are great with people  --  which sounds like you. Full training is provided, no experience needed, and the earning potential is significant.',
                'Here is what I would love to do  --  [REFERRER_NAME] is actually coming to our next info session on [DATE]. How about you join them? You two can check it out together, ask questions, and see if it clicks. No pressure, just information. Would that work?',
                'I completely understand wanting to know more before committing your time. How about this  --  I will send you the same info packet I sent [REFERRER_NAME] when they first looked into it. Take a look on your own time, and if you have questions, you can reach out to me or ask [REFERRER_NAME] directly since they have been through the whole process. Sound fair?'
            ))
            conn.commit()
    except:
        try:
            conn.rollback()
        except:
            pass

    # Seed scheduling templates
    try:
        conn.commit()  # Ensure clean transaction state
        count = list(conn.execute("SELECT COUNT(*) FROM scheduling_templates WHERE is_system=1").fetchone().values())[0]
        if count == 0:
            conn.execute("""INSERT INTO scheduling_templates (id, name, format_type, subject_line, body_text, is_system)
            VALUES (?, ?, ?, ?, ?, 1)""", (
                'sched_group_virtual', 'Group Virtual Session', 'group_virtual',
                'You are Invited: Virtual Benefits Advisor Info Session  --  [DATE]',
                'Hi [CANDIDATE_NAME],\n\nThank you for your interest in learning more about the Benefits Advisor opportunity with [AGENCY_NAME]!\n\nI would like to invite you to our upcoming virtual information session:\n\nDate: [DATE]\nTime: [TIME]\nFormat: Virtual (Zoom/Video Call)\nLink: [MEETING_LINK]\nDuration: Approximately [DURATION] minutes\n\nDuring this session, you will:\n- Learn about the role and what a typical day looks like\n- Hear from current team members about their experience\n- Get your questions answered in a relaxed, no-pressure setting\n\nNo preparation needed  --  just show up with an open mind and any questions you have.\n\nLooking forward to seeing you there!\n\n[RSC_NAME]\n[AGENCY_NAME]\n[PHONE]'
            ))
            conn.execute("""INSERT INTO scheduling_templates (id, name, format_type, subject_line, body_text, is_system)
            VALUES (?, ?, ?, ?, ?, 1)""", (
                'sched_group_inperson', 'Group In-Person Session', 'group_inperson',
                'You are Invited: In-Person Benefits Advisor Info Session  --  [DATE]',
                'Hi [CANDIDATE_NAME],\n\nThank you for your interest in the Benefits Advisor opportunity with [AGENCY_NAME]!\n\nI would like to invite you to our upcoming in-person information session:\n\nDate: [DATE]\nTime: [TIME]\nLocation: [LOCATION]\nDuration: Approximately [DURATION] minutes\nParking: [PARKING_DETAILS]\n\nDuring this session, you will:\n- Tour our office and meet the team\n- Learn about the role and career path\n- Get your questions answered face-to-face in a relaxed setting\n\nPlease arrive 5-10 minutes early. Business casual attire is fine  --  no need to dress up.\n\nLooking forward to meeting you!\n\n[RSC_NAME]\n[AGENCY_NAME]\n[PHONE]'
            ))
            conn.execute("""INSERT INTO scheduling_templates (id, name, format_type, subject_line, body_text, is_system)
            VALUES (?, ?, ?, ?, ?, 1)""", (
                'sched_1on1_virtual', '1-on-1 Virtual Meeting', 'one_on_one_virtual',
                'Let us Connect: Virtual Chat About the Benefits Advisor Role',
                'Hi [CANDIDATE_NAME],\n\nThank you for taking the time to learn about what we do! I would love to set up a quick 1-on-1 virtual chat to answer your questions and tell you more about the Benefits Advisor opportunity.\n\nHere are the details:\n\nDate: [DATE]\nTime: [TIME]\nFormat: Virtual (Video Call)\nLink: [MEETING_LINK]\nDuration: About [DURATION] minutes\n\nThis is a casual conversation  --  no interview, no pressure. I just want to learn a bit about you and share what makes this opportunity unique.\n\nIf the time does not work, just reply and we will find something better.\n\nTalk soon!\n\n[RSC_NAME]\n[AGENCY_NAME]\n[PHONE]'
            ))
            conn.execute("""INSERT INTO scheduling_templates (id, name, format_type, subject_line, body_text, is_system)
            VALUES (?, ?, ?, ?, ?, 1)""", (
                'sched_1on1_inperson', '1-on-1 In-Person Meeting', 'one_on_one_inperson',
                'Let us Meet: Coffee Chat About the Benefits Advisor Role',
                'Hi [CANDIDATE_NAME],\n\nGreat connecting with you! I would love to meet up in person for a quick chat about the Benefits Advisor opportunity with [AGENCY_NAME].\n\nHere are the details:\n\nDate: [DATE]\nTime: [TIME]\nLocation: [LOCATION]\nDuration: About [DURATION] minutes\n\nThis is a relaxed, get-to-know-you conversation  --  no formal interview. I want to hear about your background and share what makes our team special.\n\nIf something comes up or you need to reschedule, just let me know.\n\nLooking forward to meeting you!\n\n[RSC_NAME]\n[AGENCY_NAME]\n[PHONE]'
            ))
            conn.commit()
    except:
        try:
            conn.rollback()
        except:
            pass

    # Seed message templates
    try:
        conn.commit()  # Ensure clean transaction state
        count = list(conn.execute("SELECT COUNT(*) FROM message_templates WHERE is_system=1").fetchone().values())[0]
        if count == 0:
            msg_seeds = [
                ('msg_invite_professional', 'Professional Invitation', 'invitation', 'professional',
                 'Opportunity to Join Our Benefits Team',
                 'Dear [CANDIDATE_NAME],\n\nI came across your profile and believe you could be an excellent fit for our Benefits Advisor team at [AGENCY_NAME].\n\nWe are currently expanding and looking for motivated individuals who enjoy helping others. The role offers flexible scheduling, comprehensive training, and significant earning potential  --  no prior insurance experience required.\n\nI would love to share more details. Would you be available for a brief conversation this week?\n\nBest regards,\n[RSC_NAME]\n[AGENCY_NAME]'),
                ('msg_invite_casual', 'Casual Invitation', 'invitation', 'casual',
                 'Quick Question for You',
                 'Hey [CANDIDATE_NAME]!\n\nI hope this finds you well. I am reaching out because we are growing our team and I think you might be a great fit.\n\nWe help people navigate their insurance and benefits options  --  it is rewarding work with great flexibility and earning potential. No experience needed  --  we train you on everything.\n\nWould you be open to a quick chat to learn more? No pressure at all.\n\nCheers,\n[RSC_NAME]'),
                ('msg_invite_enthusiastic', 'Enthusiastic Invitation', 'invitation', 'enthusiastic',
                 'Exciting Opportunity  --  I Thought of You!',
                 'Hi [CANDIDATE_NAME]!\n\nI am so excited to reach out to you! We are building something special at [AGENCY_NAME] and I genuinely think you could be a fantastic addition to our team.\n\nAs a Benefits Advisor, you would help families make smart decisions about their health insurance and benefits  --  all while enjoying a flexible schedule and unlimited earning potential. The best part? We provide all the training and support you need to succeed, even if you have never worked in insurance before.\n\nI would love to tell you more! Are you free for a quick call this week?\n\nCan not wait to connect!\n[RSC_NAME]\n[AGENCY_NAME]'),
                ('msg_followup', 'Post-Interview Follow-Up', 'follow_up', 'professional',
                 'Great Meeting You  --  Next Steps',
                 'Hi [CANDIDATE_NAME],\n\nThank you for taking the time to meet with us! It was great learning about your background and I enjoyed our conversation.\n\nI wanted to follow up with the next steps:\n\n1. [NEXT_STEP_1]\n2. [NEXT_STEP_2]\n3. [NEXT_STEP_3]\n\nIf you have any questions in the meantime, do not hesitate to reach out. I am here to help!\n\nBest,\n[RSC_NAME]\n[AGENCY_NAME]\n[PHONE]'),
                ('msg_rejection', 'Not Moving Forward', 'rejection', 'professional',
                 'Update on Your Application',
                 'Hi [CANDIDATE_NAME],\n\nThank you for your interest in the Benefits Advisor opportunity with [AGENCY_NAME] and for the time you invested in our process.\n\nAfter careful consideration, we have decided to move forward with other candidates whose experience more closely aligns with our current needs.\n\nThis is not a reflection of your abilities  --  we were impressed by [POSITIVE_NOTE]. We encourage you to keep us in mind for future opportunities, as our team is always growing.\n\nWishing you all the best in your career journey.\n\nWarm regards,\n[RSC_NAME]\n[AGENCY_NAME]'),
                ('msg_offer', 'Offer / Next Steps', 'offer', 'enthusiastic',
                 'Welcome to the Team  --  Next Steps!',
                 'Hi [CANDIDATE_NAME],\n\nI am thrilled to officially welcome you to the [AGENCY_NAME] team!\n\nWe were so impressed with you throughout the process, and we are excited about what you will bring to our team.\n\nHere are your next steps to get started:\n\n1. Complete your onboarding paperwork (link below)\n2. Schedule your licensing study plan\n3. Meet your training mentor\n\n[ONBOARDING_LINK]\n\nYour first training session is on [DATE]. Please reach out if you have any questions before then.\n\nWelcome aboard!\n[RSC_NAME]\n[AGENCY_NAME]'),
                ('msg_noshow', 'No-Show Follow-Up', 'no_show', 'casual',
                 'We Missed You  --  Reschedule?',
                 'Hey [CANDIDATE_NAME],\n\nI noticed you were not able to make it to our session on [DATE]. No worries at all  --  life happens!\n\nI still think you would be a great fit for our team and I would love to reschedule. We have another session coming up on [NEW_DATE]  --  would that work better for you?\n\nIf your schedule has changed or you are no longer interested, totally understand. Just let me know either way.\n\nHope to hear from you!\n[RSC_NAME]\n[AGENCY_NAME]'),
            ]
            for seed in msg_seeds:
                conn.execute("""INSERT INTO message_templates (id, name, category, tone, subject_line, body_text, is_system)
                VALUES (?, ?, ?, ?, ?, ?, 1)""", seed)
            conn.commit()
    except:
        try:
            conn.rollback()
        except:
            pass

    # ======================== CYCLE 38: CLOSE THE GAPS ========================

    # Resume Parser: Add parsed resume fields to candidates and leads
    c38_resume_migrations = [
        ("candidates", "parsed_summary", "TEXT"),
        ("candidates", "parsed_experience", "TEXT"),
        ("candidates", "parsed_skills", "TEXT"),
        ("candidates", "resume_parsed_at", "TIMESTAMP"),
        ("sourced_leads", "parsed_summary", "TEXT"),
        ("sourced_leads", "parsed_experience", "TEXT"),
        ("sourced_leads", "parsed_skills", "TEXT"),
        ("sourced_leads", "resume_url", "TEXT"),
        ("sourced_leads", "resume_parsed_at", "TIMESTAMP"),
    ]
    for table, col, coltype in c38_resume_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Auto-Engage: Add auto-engage mode to interviews
    c38_autoengage_migrations = [
        ("interviews", "auto_engage_mode", "TEXT DEFAULT 'hold'"),
    ]
    for table, col, coltype in c38_autoengage_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # SMS Messaging: New table for SMS messages + Twilio config on users
    conn.execute("""
    CREATE TABLE IF NOT EXISTS sms_messages (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        interview_id TEXT,
        direction TEXT NOT NULL DEFAULT 'outbound',
        from_number TEXT,
        to_number TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT DEFAULT 'queued',
        twilio_sid TEXT,
        template_id TEXT,
        error_message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (candidate_id) REFERENCES candidates(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_candidate ON sms_messages(candidate_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_user ON sms_messages(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_direction ON sms_messages(direction)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_created ON sms_messages(created_at)")

    # SMS templates table
    conn.execute("""
    CREATE TABLE IF NOT EXISTS sms_templates (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        name TEXT NOT NULL,
        body TEXT NOT NULL,
        is_system INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_templates_user ON sms_templates(user_id)")

    # Twilio config on users table
    c38_sms_user_migrations = [
        ("users", "twilio_phone_number", "TEXT"),
        ("users", "sms_enabled", "INTEGER DEFAULT 0"),
        ("users", "sms_opt_out_message", "TEXT DEFAULT 'You have been unsubscribed. Reply START to resubscribe.'"),
    ]
    for table, col, coltype in c38_sms_user_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # SMS opt-out tracking
    conn.execute("""
    CREATE TABLE IF NOT EXISTS sms_opt_outs (
        id TEXT PRIMARY KEY,
        phone_number TEXT NOT NULL UNIQUE,
        opted_out_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_optout_phone ON sms_opt_outs(phone_number)")

    # AI Job Description: Add generated_description to interviews
    c38_jobdesc_migrations = [
        ("interviews", "generated_description", "TEXT"),
    ]
    for table, col, coltype in c38_jobdesc_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Seed SMS templates
    try:
        conn.commit()
        count = list(conn.execute("SELECT COUNT(*) FROM sms_templates WHERE is_system=1").fetchone().values())[0]
        if count == 0:
            sms_seeds = [
                ('sms_invite', 'Interview Invite', 'Hi {name}! {agency} here. We\'d love to learn more about you. Complete a quick video intro at your convenience: {link}', 1),
                ('sms_reminder', 'Friendly Reminder', 'Hi {name}, just a reminder about your video interview for {agency}. Complete it anytime here: {link}', 1),
                ('sms_followup', 'Follow-Up Nudge', 'Hi {name}, following up on the interview invite I sent. Any questions? Happy to chat — just reply here. {agency}', 1),
                ('sms_thankyou', 'Thank You', 'Hi {name}, thanks for completing your interview! We\'ll review and be in touch soon. — {agency}', 1),
                ('sms_custom', 'Custom Message', '{message}', 0),
            ]
            for sid, name, body, is_sys in sms_seeds:
                conn.execute("INSERT INTO sms_templates (id, name, body, is_system) VALUES (?, ?, ?, ?)",
                             (sid, name, body, is_sys))
            conn.commit()
    except:
        try:
            conn.rollback()
        except:
            pass

    # ======================== CYCLE 40: OUTBOUND CAMPAIGN ENGINE ========================

    # Campaigns table — each campaign is tied to an interview and contains the job description content
    conn.execute("""
    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        interview_id TEXT NOT NULL,
        name TEXT NOT NULL,
        subject_line TEXT NOT NULL DEFAULT 'An Exciting Career Opportunity',
        headline TEXT NOT NULL DEFAULT 'We Think You''d Be a Great Fit',
        body_html TEXT NOT NULL,
        cta_text TEXT NOT NULL DEFAULT 'I''m Interested — Tell Me More',
        status TEXT NOT NULL DEFAULT 'draft',
        sent_count INTEGER DEFAULT 0,
        delivered_count INTEGER DEFAULT 0,
        opened_count INTEGER DEFAULT 0,
        clicked_count INTEGER DEFAULT 0,
        applied_count INTEGER DEFAULT 0,
        scheduled_at TIMESTAMP,
        sent_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (interview_id) REFERENCES interviews(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_user ON campaigns(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_interview ON campaigns(interview_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status)")

    # Campaign recipients — tracks each person who receives a campaign email
    conn.execute("""
    CREATE TABLE IF NOT EXISTS campaign_recipients (
        id TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        first_name TEXT,
        last_name TEXT,
        email TEXT NOT NULL,
        phone TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        sent_at TIMESTAMP,
        delivered_at TIMESTAMP,
        opened_at TIMESTAMP,
        clicked_at TIMESTAMP,
        applied_at TIMESTAMP,
        candidate_id TEXT,
        sendgrid_message_id TEXT,
        error_message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (candidate_id) REFERENCES candidates(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_recip_campaign ON campaign_recipients(campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_recip_email ON campaign_recipients(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_recip_status ON campaign_recipients(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_recip_user ON campaign_recipients(user_id)")

    # Migration: add campaign_id to candidates table for source tracking
    c40_migrations = [
        ("candidates", "campaign_id", "TEXT"),
        ("candidates", "campaign_recipient_id", "TEXT"),
    ]
    for table, col, coltype in c40_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # ======================== CYCLE 41: JOBS TAB — SEPARATE JOBS FROM INTERVIEWS ========================

    # Jobs table — the position/role an RSC is hiring for (separate from how they evaluate candidates)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        department TEXT,
        position TEXT,
        location TEXT,
        job_type TEXT DEFAULT 'full_time',
        salary_range TEXT,
        application_deadline TIMESTAMP,
        status TEXT DEFAULT 'active',
        job_board_enabled INTEGER DEFAULT 0,
        public_apply_enabled INTEGER DEFAULT 1,
        auto_engage_mode TEXT DEFAULT 'hold',
        apply_instructions TEXT,
        apply_fields_json TEXT DEFAULT '[]',
        prep_video_url TEXT,
        prep_instructions TEXT,
        estimated_duration_min INTEGER DEFAULT 15,
        generated_description TEXT,
        interview_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (interview_id) REFERENCES interviews(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_interview ON jobs(interview_id)")

    # Migrations: add job_id to candidates, campaigns for the new linkage
    c41_migrations = [
        ("candidates", "job_id", "TEXT"),
        ("campaigns", "job_id", "TEXT"),
    ]
    for table, col, coltype in c41_migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except:
            pass

    # Auto-migration: create a Job record for every existing Interview that doesn't already have one
    try:
        existing_interviews = conn.execute("""
            SELECT i.id, i.user_id, i.title, i.description, i.department, i.position, i.status,
                   i.created_at, i.updated_at
            FROM interviews i
            WHERE i.id NOT IN (SELECT interview_id FROM jobs WHERE interview_id IS NOT NULL)
        """).fetchall()
        for iv in existing_interviews:
            iv = dict(iv)
            job_id = uuid.uuid4().hex
            # Pull job metadata from interview migration columns (may not exist on all rows)
            location = None
            job_type = 'full_time'
            salary_range = None
            application_deadline = None
            job_board_enabled = 0
            public_apply_enabled = 0
            auto_engage_mode = 'hold'
            apply_instructions = None
            estimated_duration_min = 15
            generated_description = None
            try:
                extra = conn.execute("""SELECT location, job_type, salary_range, application_deadline,
                    job_board_enabled, public_apply_enabled, auto_engage_mode, apply_instructions,
                    estimated_duration_min, generated_description
                    FROM interviews WHERE id=?""", (iv['id'],)).fetchone()
                if extra:
                    extra = dict(extra)
                    location = extra.get('location')
                    job_type = extra.get('job_type') or 'full_time'
                    salary_range = extra.get('salary_range')
                    application_deadline = extra.get('application_deadline')
                    job_board_enabled = extra.get('job_board_enabled') or 0
                    public_apply_enabled = extra.get('public_apply_enabled') or 0
                    auto_engage_mode = extra.get('auto_engage_mode') or 'hold'
                    apply_instructions = extra.get('apply_instructions')
                    estimated_duration_min = extra.get('estimated_duration_min') or 15
                    generated_description = extra.get('generated_description')
            except:
                pass
            conn.execute("""INSERT INTO jobs (id, user_id, title, description, department, position,
                location, job_type, salary_range, application_deadline, status, job_board_enabled,
                public_apply_enabled, auto_engage_mode, apply_instructions, estimated_duration_min,
                generated_description, interview_id, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, iv['user_id'], iv['title'], iv.get('description'), iv.get('department'),
                 iv.get('position'), location, job_type, salary_range, application_deadline,
                 iv.get('status', 'active'), job_board_enabled, public_apply_enabled,
                 auto_engage_mode, apply_instructions, estimated_duration_min,
                 generated_description, iv['id'], iv.get('created_at'), iv.get('updated_at')))
            # Backfill job_id on candidates that belong to this interview
            conn.execute("UPDATE candidates SET job_id=? WHERE interview_id=? AND (job_id IS NULL OR job_id='')", (job_id, iv['id']))
            # Backfill job_id on campaigns that reference this interview
            conn.execute("UPDATE campaigns SET job_id=? WHERE interview_id=? AND (job_id IS NULL OR job_id='')", (job_id, iv['id']))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except:
            pass

    # ======================== CYCLE 45: PIPELINE STAGE MIGRATION ========================
    # Migrate old pipeline_stage values to new Cycle 45 stages
    # Old: new, in_review, shortlisted, interview_scheduled, offered, hired, rejected
    # New: new, invited, in_progress, completed, shortlisted, interview_scheduled, offered, hired, rejected
    # 'in_review' maps to 'completed' (interview done, RSC hasn't acted)
    try:
        migrated = conn.execute(
            "UPDATE candidates SET pipeline_stage='completed' WHERE pipeline_stage='in_review'"
        ).rowcount
        if migrated:
            conn.commit()
    except Exception:
        pass

    # ======================== CYCLE 40A: OUTREACH SEQUENCE ENGINE + TERRITORY ========================

    # RSC Territories — defines the geographic area an RSC recruits in
    conn.execute("""
    CREATE TABLE IF NOT EXISTS rsc_territories (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT 'My Territory',
        center_zip TEXT,
        radius_miles INTEGER DEFAULT 25,
        zip_codes TEXT DEFAULT '[]',
        states TEXT DEFAULT '[]',
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_territories_user ON rsc_territories(user_id)")

    # Outreach Sequences — reusable multi-step outreach templates
    conn.execute("""
    CREATE TABLE IF NOT EXISTS outreach_sequences (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        name TEXT NOT NULL,
        description TEXT,
        sequence_type TEXT NOT NULL DEFAULT 'recruiting',
        is_system INTEGER DEFAULT 0,
        step_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sequences_user ON outreach_sequences(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sequences_type ON outreach_sequences(sequence_type)")

    # Outreach Sequence Steps — ordered actions within a sequence
    conn.execute("""
    CREATE TABLE IF NOT EXISTS outreach_sequence_steps (
        id TEXT PRIMARY KEY,
        sequence_id TEXT NOT NULL,
        step_order INTEGER NOT NULL DEFAULT 1,
        channel TEXT NOT NULL DEFAULT 'email',
        delay_days INTEGER NOT NULL DEFAULT 0,
        template_subject TEXT,
        template_content TEXT NOT NULL,
        voice_script_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (sequence_id) REFERENCES outreach_sequences(id),
        FOREIGN KEY (voice_script_id) REFERENCES voice_scripts(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seq_steps_sequence ON outreach_sequence_steps(sequence_id)")

    # Outreach Campaigns — an active run of a sequence against a set of contacts
    conn.execute("""
    CREATE TABLE IF NOT EXISTS outreach_campaigns (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        campaign_type TEXT NOT NULL DEFAULT 'recruiting',
        sequence_id TEXT NOT NULL,
        territory_id TEXT,
        job_id TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        send_window_start TEXT DEFAULT '09:00',
        send_window_end TEXT DEFAULT '18:00',
        contact_count INTEGER DEFAULT 0,
        responded_count INTEGER DEFAULT 0,
        converted_count INTEGER DEFAULT 0,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (sequence_id) REFERENCES outreach_sequences(id),
        FOREIGN KEY (territory_id) REFERENCES rsc_territories(id),
        FOREIGN KEY (job_id) REFERENCES jobs(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_user ON outreach_campaigns(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_status ON outreach_campaigns(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_type ON outreach_campaigns(campaign_type)")

    # Outreach Campaign Contacts — people enrolled in a campaign with per-step tracking
    conn.execute("""
    CREATE TABLE IF NOT EXISTS outreach_contacts (
        id TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL,
        contact_type TEXT NOT NULL DEFAULT 'lead',
        contact_id TEXT,
        first_name TEXT,
        last_name TEXT,
        email TEXT,
        phone TEXT,
        zip_code TEXT,
        current_step INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        enrolled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_step_at TIMESTAMP,
        next_step_due TIMESTAMP,
        converted_at TIMESTAMP,
        FOREIGN KEY (campaign_id) REFERENCES outreach_campaigns(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_contacts_campaign ON outreach_contacts(campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_contacts_status ON outreach_contacts(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_contacts_next ON outreach_contacts(next_step_due)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oc_contacts_email ON outreach_contacts(email)")

    # Outreach Campaign Events — granular log of every action
    conn.execute("""
    CREATE TABLE IF NOT EXISTS outreach_events (
        id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL,
        step_id TEXT,
        event_type TEXT NOT NULL,
        channel TEXT,
        metadata TEXT DEFAULT '{}',
        occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (contact_id) REFERENCES outreach_contacts(id),
        FOREIGN KEY (step_id) REFERENCES outreach_sequence_steps(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oe_contact ON outreach_events(contact_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oe_type ON outreach_events(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oe_occurred ON outreach_events(occurred_at)")

    # ======================== CYCLE 46: GUIDED CONVERSATION WALKTHROUGHS ========================
    # Tracks per-user progress through each guided-conversation flow (territory, first_job,
    # notifications, brand — and future flows for campaign launch, pipeline, AI agent rules).
    # One row per (user_id, flow_key). answers is a JSON object keyed by step_key.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS walkthrough_progress (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        flow_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'in_progress',
        step_index INTEGER DEFAULT 0,
        answers TEXT DEFAULT '{}',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, flow_key)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walkthrough_user ON walkthrough_progress(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_walkthrough_flow ON walkthrough_progress(flow_key)")

    # Migration: ensure users.notification_prefs exists (JSON, written by the notifications flow)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN notification_prefs TEXT DEFAULT '{}'")
    except Exception:
        pass
    # Migration: ensure users.outreach_tone exists (written by the brand flow)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN outreach_tone TEXT DEFAULT 'warm'")
    except Exception:
        pass

    # Seed system outreach sequences (pre-built campaign templates)
    try:
        conn.commit()
        row = conn.execute("SELECT COUNT(*) as cnt FROM outreach_sequences WHERE is_system=1").fetchone()
        seq_count = dict(row).get('cnt', 0) if hasattr(row, 'keys') else row[0]
        if seq_count == 0:
            import json
            # Template 1: Cold Producer Outreach
            seq1_id = 'seq_cold_outreach'
            conn.execute("""INSERT INTO outreach_sequences (id, name, description, sequence_type, is_system, step_count)
                VALUES (?, ?, ?, ?, 1, 4)""",
                (seq1_id, 'Cold Producer Outreach',
                 'Email intro, SMS nudge, AI voice call, follow-up email. Best for new lead lists and purchased data.',
                 'recruiting'))
            cold_steps = [
                ('cs1_email', seq1_id, 1, 'email', 0,
                 'An opportunity worth exploring',
                 'Hi {{first_name}},\n\nI came across your profile and wanted to reach out. We have an opportunity at {{agency_name}} that I think could be a great fit for someone with your background.\n\nWe offer flexibility, competitive compensation, and a team that actually supports your growth.\n\nWould you be open to a quick conversation? You can learn more and share a bit about yourself here:\n{{interview_link}}\n\nLooking forward to connecting,\n{{recruiter_name}}\n{{agency_name}}'),
                ('cs1_sms', seq1_id, 2, 'sms', 3,
                 None,
                 'Hi {{first_name}}, this is {{recruiter_name}} from {{agency_name}}. I sent you an email a few days ago about a career opportunity. Would love to connect — any interest? Reply here or check it out: {{interview_link}}'),
                ('cs1_voice', seq1_id, 3, 'voice', 5,
                 None,
                 'cold_outreach_script'),
                ('cs1_followup', seq1_id, 4, 'email', 7,
                 'Following up — still interested?',
                 'Hi {{first_name}},\n\nI wanted to follow up on my earlier message. I know your inbox is busy, so I\'ll keep this brief.\n\nWe\'re looking for people who want more control over their career and income. If that sounds like you, I\'d love to chat.\n\nHere\'s the link whenever you\'re ready:\n{{interview_link}}\n\nNo pressure either way.\n\nBest,\n{{recruiter_name}}'),
            ]
            for sid, seqid, order, channel, delay, subject, content in cold_steps:
                conn.execute("""INSERT INTO outreach_sequence_steps (id, sequence_id, step_order, channel, delay_days, template_subject, template_content)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (sid, seqid, order, channel, delay, subject, content))

            # Template 2: Warm Lead Nurture
            seq2_id = 'seq_warm_nurture'
            conn.execute("""INSERT INTO outreach_sequences (id, name, description, sequence_type, is_system, step_count)
                VALUES (?, ?, ?, ?, 1, 3)""",
                (seq2_id, 'Warm Lead Nurture',
                 'Personal SMS, detailed email, check-in text. Best for referrals and event contacts.',
                 'recruiting'))
            warm_steps = [
                ('ws1_sms', seq2_id, 1, 'sms', 0,
                 None,
                 'Hi {{first_name}}, this is {{recruiter_name}} from {{agency_name}}. {{referral_source}} mentioned you might be interested in learning about what we do. Would love to chat — what does your week look like?'),
                ('ws1_email', seq2_id, 2, 'email', 2,
                 'More about the opportunity at {{agency_name}}',
                 'Hi {{first_name}},\n\nThanks for your interest in {{agency_name}}. I wanted to share a bit more about what we offer:\n\n- Flexible schedule — you control your calendar\n- Uncapped earning potential with competitive commissions\n- Full training and mentorship from day one\n- A team that actually has your back\n\nIf you\'d like to learn more, you can complete a quick intro at your convenience:\n{{interview_link}}\n\nHappy to answer any questions.\n\nBest,\n{{recruiter_name}}\n{{agency_name}}'),
                ('ws1_checkin', seq2_id, 3, 'sms', 5,
                 None,
                 'Hey {{first_name}}, just checking in. Did you get a chance to look at the opportunity? Happy to answer any questions — just reply here. {{recruiter_name}}'),
            ]
            for sid, seqid, order, channel, delay, subject, content in warm_steps:
                conn.execute("""INSERT INTO outreach_sequence_steps (id, sequence_id, step_order, channel, delay_days, template_subject, template_content)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (sid, seqid, order, channel, delay, subject, content))

            # Template 3: Re-Engagement
            seq3_id = 'seq_re_engagement'
            conn.execute("""INSERT INTO outreach_sequences (id, name, description, sequence_type, is_system, step_count)
                VALUES (?, ?, ?, ?, 1, 3)""",
                (seq3_id, 'Re-Engagement',
                 'Reconnect email, SMS nudge, final email. Best for old candidates and stale leads.',
                 'recruiting'))
            re_steps = [
                ('rs1_email', seq3_id, 1, 'email', 0,
                 "We'd love to reconnect",
                 "Hi {{first_name}},\n\nIt's been a while since we last connected, and I wanted to reach out again. Things at {{agency_name}} have been growing, and we're looking for great people to join the team.\n\nIf your situation has changed or you're open to exploring something new, I'd love to catch up.\n\nHere's a quick way to get started:\n{{interview_link}}\n\nHope to hear from you,\n{{recruiter_name}}"),
                ('rs1_sms', seq3_id, 2, 'sms', 4,
                 None,
                 'Hi {{first_name}}, {{recruiter_name}} here from {{agency_name}}. Sent you an email recently about reconnecting. Any interest in catching up? {{interview_link}}'),
                ('rs1_final', seq3_id, 3, 'email', 8,
                 'Last check-in from {{agency_name}}',
                 "Hi {{first_name}},\n\nI'll keep this short \u2014 I don't want to fill your inbox if the timing isn't right.\n\nIf you're ever interested in exploring what {{agency_name}} has to offer, the door is always open:\n{{interview_link}}\n\nWishing you all the best either way.\n\n{{recruiter_name}}"),
            ]
            for sid, seqid, order, channel, delay, subject, content in re_steps:
                conn.execute("""INSERT INTO outreach_sequence_steps (id, sequence_id, step_order, channel, delay_days, template_subject, template_content)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (sid, seqid, order, channel, delay, subject, content))

            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except:
            pass

    # ======================== CYCLE 47: ZIP ROUTING ENGINE + INBOUND APPLY ========================
    # Master zip->lat/lng table used by route_zip_to_rsc() for Haversine distance filtering.
    # Seeded lazily with the 38 US metro centers covering all 5 RSC regions; production can
    # later swap in a full GeoNames/USPS dataset of ~41k US zips without schema changes.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS zip_geo (
        zip TEXT PRIMARY KEY,
        lat REAL NOT NULL,
        lng REAL NOT NULL,
        city TEXT,
        state TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zip_geo_state ON zip_geo(state)")

    # Unassigned candidates: inbound apply-inbound requests that couldn't be routed to any RSC.
    # Acts as a demand-driven sales queue ("47 candidates in zip X are waiting for a subscriber").
    conn.execute("""
    CREATE TABLE IF NOT EXISTS unassigned_candidates (
        id TEXT PRIMARY KEY,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT,
        zip TEXT NOT NULL,
        source TEXT DEFAULT 'channelcareers',
        utm_source TEXT,
        utm_medium TEXT,
        utm_campaign TEXT,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        claimed_by_user_id TEXT,
        claimed_at TIMESTAMP,
        FOREIGN KEY (claimed_by_user_id) REFERENCES users(id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_unassigned_zip ON unassigned_candidates(zip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_unassigned_claimed ON unassigned_candidates(claimed_by_user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_unassigned_applied ON unassigned_candidates(applied_at)")

    # Routing columns on candidates (idempotent ALTERs)
    for alter in [
        "ALTER TABLE candidates ADD COLUMN territory_id TEXT",
        "ALTER TABLE candidates ADD COLUMN routed_at TIMESTAMP",
        "ALTER TABLE candidates ADD COLUMN utm_source TEXT",
        "ALTER TABLE candidates ADD COLUMN utm_medium TEXT",
        "ALTER TABLE candidates ADD COLUMN utm_campaign TEXT",
        "ALTER TABLE candidates ADD COLUMN zip_code TEXT",
    ]:
        try:
            conn.execute(alter)
        except Exception:
            pass
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_candidates_territory ON candidates(territory_id)",
        "CREATE INDEX IF NOT EXISTS idx_candidates_utm_source ON candidates(utm_source)",
        "CREATE INDEX IF NOT EXISTS idx_candidates_zip ON candidates(zip_code)",
    ]:
        try:
            conn.execute(idx)
        except Exception:
            pass

    # Rate limiting: per-IP apply-inbound throttle
    conn.execute("""
    CREATE TABLE IF NOT EXISTS apply_inbound_rate_limit (
        ip TEXT PRIMARY KEY,
        last_apply_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        apply_count_today INTEGER DEFAULT 1,
        window_started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Seed zip_geo with ~38 US metro centers covering all 5 RSC regions + low-density fallbacks
    try:
        have_any = conn.execute("SELECT COUNT(*) FROM zip_geo").fetchone()[0]
        if have_any < 20:
            seed_zips = [
                # Northwest region
                ('98101', 47.6062, -122.3321, 'Seattle', 'WA'),
                ('97201', 45.5152, -122.6784, 'Portland', 'OR'),
                ('83702', 43.6150, -116.2023, 'Boise', 'ID'),
                ('59101', 45.7833, -108.5007, 'Billings', 'MT'),
                ('99501', 61.2181, -149.9003, 'Anchorage', 'AK'),
                ('99701', 64.8378, -147.7164, 'Fairbanks', 'AK'),
                # Southwest region
                ('75201', 32.7767, -96.7970, 'Dallas', 'TX'),
                ('77002', 29.7604, -95.3698, 'Houston', 'TX'),
                ('78701', 30.2672, -97.7431, 'Austin', 'TX'),
                ('78205', 29.4241, -98.4936, 'San Antonio', 'TX'),
                ('85001', 33.4484, -112.0740, 'Phoenix', 'AZ'),
                ('85701', 32.2226, -110.9747, 'Tucson', 'AZ'),
                ('87501', 35.6870, -105.9378, 'Santa Fe', 'NM'),
                ('89101', 36.1699, -115.1398, 'Las Vegas', 'NV'),
                # California (Southwest-adjacent)
                ('90001', 33.9731, -118.2479, 'Los Angeles', 'CA'),
                ('94102', 37.7749, -122.4194, 'San Francisco', 'CA'),
                ('92101', 32.7157, -117.1611, 'San Diego', 'CA'),
                ('95814', 38.5816, -121.4944, 'Sacramento', 'CA'),
                # Northeast region
                ('10001', 40.7128, -74.0060, 'New York', 'NY'),
                ('02108', 42.3601, -71.0589, 'Boston', 'MA'),
                ('19103', 39.9526, -75.1652, 'Philadelphia', 'PA'),
                ('15222', 40.4406, -79.9959, 'Pittsburgh', 'PA'),
                ('21201', 39.2904, -76.6122, 'Baltimore', 'MD'),
                ('20001', 38.9072, -77.0369, 'Washington', 'DC'),
                # Southeast region
                ('33101', 25.7617, -80.1918, 'Miami', 'FL'),
                ('32801', 28.5383, -81.3792, 'Orlando', 'FL'),
                ('32202', 30.3322, -81.6557, 'Jacksonville', 'FL'),
                ('30303', 33.7490, -84.3880, 'Atlanta', 'GA'),
                ('28202', 35.2271, -80.8431, 'Charlotte', 'NC'),
                ('37201', 36.1627, -86.7816, 'Nashville', 'TN'),
                ('70112', 29.9511, -90.0715, 'New Orleans', 'LA'),
                # North region
                ('60601', 41.8781, -87.6298, 'Chicago', 'IL'),
                ('55101', 44.9537, -93.0900, 'St Paul', 'MN'),
                ('55401', 44.9778, -93.2650, 'Minneapolis', 'MN'),
                ('48201', 42.3314, -83.0458, 'Detroit', 'MI'),
                ('53202', 43.0389, -87.9065, 'Milwaukee', 'WI'),
                ('64101', 39.0997, -94.5786, 'Kansas City', 'MO'),
                ('80202', 39.7392, -104.9903, 'Denver', 'CO'),
                # Low-density / fallback zips for diagnostics
                ('59718', 45.6770, -111.0429, 'Bozeman', 'MT'),
                ('82001', 41.1400, -104.8202, 'Cheyenne', 'WY'),
                ('58501', 46.8083, -100.7837, 'Bismarck', 'ND'),
                ('57501', 44.3683, -100.3509, 'Pierre', 'SD'),
            ]
            for z, la, lg, city, st in seed_zips:
                conn.execute(
                    "INSERT OR IGNORE INTO zip_geo (zip, lat, lng, city, state) VALUES (?, ?, ?, ?, ?)",
                    (z, la, lg, city, st),
                )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    # ======================== CYCLE 48: RESUME UPLOAD ON INBOUND APPLY ========================
    # resume_url on unassigned_candidates so resumes attached to applicants in
    # no-coverage zips are preserved and forwarded when an RSC claims them.
    try:
        conn.execute("ALTER TABLE unassigned_candidates ADD COLUMN resume_url TEXT")
    except Exception:
        pass

    try:
        conn.commit()
    except:
        pass  # autocommit mode doesn't need explicit commit
    conn.close()

if __name__ == '__main__':
    init_db()
    print("Database initialized successfully.")
