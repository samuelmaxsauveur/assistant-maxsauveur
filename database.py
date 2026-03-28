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
