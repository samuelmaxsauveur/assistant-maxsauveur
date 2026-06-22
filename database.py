import sqlite3
import uuid
import json
import os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "assistant.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_drafts (
                token         TEXT PRIMARY KEY,
                email_id      TEXT NOT NULL,
                thread_id     TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                customer_name  TEXT NOT NULL,
                subject        TEXT NOT NULL,
                email_body     TEXT NOT NULL,
                draft_response TEXT NOT NULL,
                order_info     TEXT,
                intent         TEXT,
                status         TEXT NOT NULL DEFAULT 'pending',
                created_at     TIMESTAMP NOT NULL,
                validated_at   TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                email_id     TEXT PRIMARY KEY,
                processed_at TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_summaries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                summary     TEXT NOT NULL,
                created_at  TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                trigger     TEXT NOT NULL,
                steps       TEXT NOT NULL,
                created_at  TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS questions_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id      TEXT,
                subject       TEXT,
                customer_name TEXT,
                question      TEXT NOT NULL,
                claude_answer TEXT NOT NULL,
                updated_draft TEXT,
                created_at    TIMESTAMP NOT NULL
            )
        """)
        # Add rejection_comment column if not exists
        try:
            cursor.execute("ALTER TABLE pending_drafts ADD COLUMN rejection_comment TEXT")
        except Exception:
            pass
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sav_cases (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id      TEXT NOT NULL UNIQUE,
                thread_id     TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                customer_name  TEXT NOT NULL,
                subject        TEXT NOT NULL,
                email_body     TEXT NOT NULL,
                order_number   TEXT,
                status         TEXT NOT NULL DEFAULT 'pending',
                created_at     TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sav_status_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id      INTEGER NOT NULL,
                status       TEXT NOT NULL,
                note         TEXT,
                notified     INTEGER NOT NULL DEFAULT 0,
                created_at   TIMESTAMP NOT NULL,
                FOREIGN KEY (case_id) REFERENCES sav_cases(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS outbound_templates (
                subject_type TEXT PRIMARY KEY,
                body         TEXT NOT NULL,
                updated_at   TIMESTAMP NOT NULL
            )
        """)
        try:
            cursor.execute("ALTER TABLE outbound_templates ADD COLUMN label TEXT")
        except Exception:
            pass
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sent_emails (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                to_email    TEXT NOT NULL,
                subject     TEXT NOT NULL,
                body        TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'reply',
                thread_id   TEXT,
                sent_at     TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS response_patterns (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                topic            TEXT NOT NULL UNIQUE,
                topic_label      TEXT NOT NULL,
                situation        TEXT NOT NULL,
                response_template TEXT NOT NULL,
                key_points       TEXT,
                example_count    INTEGER NOT NULL DEFAULT 1,
                last_updated     TIMESTAMP NOT NULL,
                created_at       TIMESTAMP NOT NULL
            )
        """)
        conn.commit()
        # Seed response_patterns from custom_patterns.json (survives Railway redeploys)
        _seed_patterns_from_json(conn)
    finally:
        conn.close()


def _seed_patterns_from_json(conn):
    """Load patterns from custom_patterns.json into SQLite if not already present."""
    try:
        json_path = os.path.join(os.path.dirname(__file__), 'custom_patterns.json')
        with open(json_path) as f:
            data = json.load(f)
        now = datetime.utcnow().isoformat()
        for p in data.get('patterns', []):
            existing = conn.execute(
                "SELECT id FROM response_patterns WHERE topic = ?", (p['topic'],)
            ).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO response_patterns
                       (topic, topic_label, situation, response_template, key_points, example_count, last_updated, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (p['topic'], p['topic_label'], p.get('situation', ''), p['response_template'],
                     p.get('key_points', ''), p.get('example_count', 1), now, now)
                )
        conn.commit()
    except Exception:
        pass


def save_outbound_template(subject_type, body, label=None):
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO outbound_templates (subject_type, body, label, updated_at) VALUES (?, ?, ?, ?)",
            (subject_type, body, label, now)
        )
        conn.commit()
    finally:
        conn.close()


def get_all_outbound_templates():
    """Return all saved custom templates: from JSON file (permanent) + SQLite (session)."""
    results = {}
    # 1. Load from JSON file (permanent, in git repo)
    try:
        import json as _json
        json_path = os.path.join(os.path.dirname(__file__), 'custom_templates.json')
        with open(json_path) as f:
            data = _json.load(f)
        for t in data.get('templates', []):
            results[t['subject_type']] = t
    except Exception:
        pass
    # 2. Overlay with SQLite (may have newer entries not yet committed)
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT subject_type, label, body FROM outbound_templates WHERE label IS NOT NULL ORDER BY updated_at DESC"
        )
        for row in cursor.fetchall():
            results[row['subject_type']] = dict(row)
    finally:
        conn.close()
    return list(results.values())


def get_outbound_template(subject_type):
    # Check environment variable first (survives Railway deployments)
    env_key = f"TEMPLATE_{subject_type.upper()}"
    env_val = os.environ.get(env_key, '').strip()
    if env_val:
        return env_val
    # Check JSON file (permanent, in git repo)
    try:
        import json as _json
        json_path = os.path.join(os.path.dirname(__file__), 'custom_templates.json')
        with open(json_path) as f:
            data = _json.load(f)
        for t in data.get('templates', []):
            if t['subject_type'] == subject_type:
                return t['body']
    except Exception:
        pass
    # Fallback to database
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT body FROM outbound_templates WHERE subject_type = ?", (subject_type,)
        )
        row = cursor.fetchone()
        return row['body'] if row else None
    finally:
        conn.close()


def log_sent_email(to_email, subject, body, source='reply', thread_id=None):
    """Log an email actually sent to a customer and save it as a response pattern immediately."""
    import re as _re
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO sent_emails (to_email, subject, body, source, thread_id, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (to_email, subject, body, source, thread_id, now)
        )
        conn.commit()
    finally:
        conn.close()
    # Save directly as pattern — no AI analysis, no loss
    clean_subject = _re.sub(r'^Re:\s*', '', subject, flags=_re.IGNORECASE).strip()
    topic = _re.sub(r'[^a-z0-9_]', '_', clean_subject.lower())[:60]
    if topic and body and body.strip():
        upsert_response_pattern(
            topic=topic,
            topic_label=clean_subject,
            situation=clean_subject,
            response_template=body.strip(),
            key_points=''
        )


def get_today_sent_emails():
    """Return all emails sent today (UTC date)."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM sent_emails WHERE sent_at LIKE ? ORDER BY sent_at ASC",
            (f"{today}%",)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_response_patterns():
    """Return all response patterns, most recently updated first."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM response_patterns WHERE topic != '_last_extraction' ORDER BY last_updated DESC"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_last_pattern_extraction_date():
    """Return the date of the last extract_response_patterns run."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT last_updated FROM response_patterns WHERE topic = '_last_extraction'"
        ).fetchone()
        return row['last_updated'][:10] if row else None
    finally:
        conn.close()


def mark_pattern_extraction_done():
    """Record today as the last extraction date."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO response_patterns
               (topic, topic_label, situation, response_template, key_points, example_count, last_updated, created_at)
               VALUES ('_last_extraction', '_last_extraction', '', '', '', 0, ?, ?)""",
            (now, now)
        )
        conn.commit()
    finally:
        conn.close()


def upsert_response_pattern(topic, topic_label, situation, response_template, key_points):
    """Insert or update a response pattern. If topic exists, update content and increment count."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id, example_count FROM response_patterns WHERE topic = ?", (topic,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE response_patterns
                   SET topic_label=?, situation=?, response_template=?, key_points=?,
                       example_count=example_count+1, last_updated=?
                   WHERE topic=?""",
                (topic_label, situation, response_template, key_points, now, topic)
            )
        else:
            conn.execute(
                """INSERT INTO response_patterns
                   (topic, topic_label, situation, response_template, key_points, example_count, last_updated, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (topic, topic_label, situation, response_template, key_points, now, now)
            )
        conn.commit()
    finally:
        conn.close()


def save_draft(
    email_id,
    thread_id,
    customer_email,
    customer_name,
    subject,
    email_body,
    draft_response,
    order_info_json,
    intent,
):
    """
    Persist a new draft with status='pending'.
    order_info_json can be a dict/list (will be serialised) or an already-serialised string.
    Returns the generated UUID token.
    """
    token = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    if order_info_json is not None and not isinstance(order_info_json, str):
        order_info_json = json.dumps(order_info_json, ensure_ascii=False)

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO pending_drafts
                (token, email_id, thread_id, customer_email, customer_name,
                 subject, email_body, draft_response, order_info, intent,
                 status, created_at, validated_at)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
            """,
            (
                token,
                email_id,
                thread_id,
                customer_email,
                customer_name,
                subject,
                email_body,
                draft_response,
                order_info_json,
                intent,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return token


def get_all_drafts():
    """Return all drafts ordered by created_at descending."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM pending_drafts ORDER BY created_at DESC"
        )
        rows = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
    return rows


def get_pending_drafts():
    """Return all drafts with status='pending', ordered by created_at ascending."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM pending_drafts WHERE status = 'pending' ORDER BY created_at ASC"
        )
        rows = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
    return rows


def get_draft_by_token(token):
    """Return a single draft dict for the given token, or None if not found."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM pending_drafts WHERE token = ?", (token,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def validate_draft(token):
    """Set status='validated' and record the current UTC timestamp."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE pending_drafts SET status = 'validated', validated_at = ? WHERE token = ?",
            (now, token),
        )
        conn.commit()
    finally:
        conn.close()


def reject_draft(token, comment=None):
    """Set status='rejected', record timestamp and optional comment."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE pending_drafts SET status = 'rejected', validated_at = ?, rejection_comment = ? WHERE token = ?",
            (now, comment, token),
        )
        conn.commit()
    finally:
        conn.close()


def mark_processed(email_id):
    """Record an email as processed so it is never handled again."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_emails (email_id, processed_at)
            VALUES (?, ?)
            """,
            (email_id, now),
        )
        conn.commit()
    finally:
        conn.close()


def is_processed(email_id):
    """Return True if the email has already been processed."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT 1 FROM processed_emails WHERE email_id = ?", (email_id,)
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def save_daily_summary(date, summary_text):
    """Save a daily summary for a given date (YYYY-MM-DD)."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO daily_summaries (date, summary, created_at) VALUES (?, ?, ?)",
            (date, summary_text, now)
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_summaries(days=14):
    """Return the last N days of daily summaries, most recent first."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT date, summary FROM daily_summaries ORDER BY created_at DESC LIMIT ?",
            (days,)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_today_drafts():
    """Return all drafts created today (UTC date)."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM pending_drafts WHERE created_at LIKE ? ORDER BY created_at ASC",
            (f"{today}%",)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def save_process(name, trigger, steps):
    """Save a manual process for Wing/Shopify actions."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO processes (name, trigger, steps, created_at) VALUES (?, ?, ?, ?)",
            (name, trigger, steps, now)
        )
        conn.commit()
    finally:
        conn.close()


def get_all_processes():
    """Return all saved processes."""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM processes ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def save_question_log(email_id, subject, customer_name, question, claude_answer, updated_draft=None):
    """Save a Samuel→Claude question exchange for learning."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO questions_log
               (email_id, subject, customer_name, question, claude_answer, updated_draft, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (email_id, subject, customer_name, question, claude_answer, updated_draft, now)
        )
        conn.commit()
    finally:
        conn.close()


def get_today_questions():
    """Return all question exchanges from today."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM questions_log WHERE created_at LIKE ? ORDER BY created_at ASC",
            (f"{today}%",)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def save_sav_case(email_id, thread_id, customer_email, customer_name, subject, email_body, order_number=None):
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO sav_cases
               (email_id, thread_id, customer_email, customer_name, subject, email_body, order_number, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (email_id, thread_id, customer_email, customer_name, subject, email_body, order_number, now)
        )
        conn.commit()
        cursor = conn.execute("SELECT id FROM sav_cases WHERE email_id = ?", (email_id,))
        row = cursor.fetchone()
        return row['id'] if row else None
    finally:
        conn.close()


def get_sav_cases():
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT * FROM sav_cases ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def update_sav_case_status(case_id, status):
    conn = get_connection()
    try:
        conn.execute("UPDATE sav_cases SET status = ? WHERE id = ?", (status, case_id))
        conn.commit()
    finally:
        conn.close()


def add_sav_status_history(case_id, status, note=None, notified=False):
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO sav_status_history (case_id, status, note, notified, created_at) VALUES (?, ?, ?, ?, ?)",
            (case_id, status, note, 1 if notified else 0, now)
        )
        conn.commit()
    finally:
        conn.close()


def get_sav_status_history(case_id):
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM sav_status_history WHERE case_id = ? ORDER BY created_at ASC",
            (case_id,)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_today_rejections():
    """Return all rejected drafts from today that have a comment."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cursor = conn.execute(
            """SELECT subject, customer_name, draft_response, rejection_comment
               FROM pending_drafts
               WHERE status = 'rejected' AND rejection_comment IS NOT NULL
               AND validated_at LIKE ?""",
            (f"{today}%",)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


# Initialise the database as soon as the module is imported.
init_db()
