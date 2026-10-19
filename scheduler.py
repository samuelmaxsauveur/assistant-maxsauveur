import os
import json
import re
from datetime import datetime
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import gmail as gmail_helper
import shopify_api
import claude_ai
import database
import repairs
import returns
import ups_tracking
import workshop_mail

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "https://assistant-maxsauveur.up.railway.app")


# Definis dans gmail.py, partages avec app.py.
extract_email_address = gmail_helper.extract_email_address
extract_customer_name = gmail_helper.extract_customer_name
resolve_sender = gmail_helper.resolve_sender


def process_new_emails():
    print(f"[{datetime.now()}] Starting email processing...")
    try:
        service = gmail_helper.get_gmail_service()
        emails = gmail_helper.get_emails_to_answer(service, max_results=10)
        print(f"[{datetime.now()}] Fetched {len(emails)} email(s) to answer.")
    except Exception as e:
        print(f"[{datetime.now()}] Error fetching emails: {e}")
        return

    for email in emails:
        email_id = email['id']

        try:
            if database.is_processed(email_id):
                print(f"[{datetime.now()}] Email {email_id} already processed, skipping.")
                continue
        except Exception as e:
            print(f"[{datetime.now()}] Error checking processed status for {email_id}: {e}")
            continue

        print(f"[{datetime.now()}] Processing email {email_id}: {email['subject']}")

        try:
            # Shogun contact forms arrive from a noreply address with the real
            # customer address written in the body. Resolve it once, exactly as
            # app.py does, so the order lookup, the history and the saved draft
            # all key on the customer instead of on noreply@.
            sender_email, customer_name = resolve_sender(email['sender'], email['body'])

            # Extract image attachments (for Claude Vision)
            email_images = []
            try:
                raw_email_data = service.users().messages().get(userId='me', id=email_id, format='full').execute()
                email_images = gmail_helper.get_image_attachments(service, raw_email_data)
            except Exception:
                pass

            # Extract order number from subject + body
            order_number = claude_ai.extract_order_number(email['body'] + ' ' + email['subject'])
            order_info = None

            if order_number:
                order_info = shopify_api.get_order_by_number(order_number)

            if not order_info:
                orders = shopify_api.get_orders_by_email(sender_email)
                if orders:
                    order_info = orders[0]

            # Detect intent
            try:
                intent_data = claude_ai.detect_intent(email['body'], email['subject'])
            except Exception as e:
                print(f"[{datetime.now()}] Error detecting intent for {email_id}: {e}")
                intent_data = {"intent": "other", "address": None, "has_full_address": False}
            # Stored with the intent so the morning report can point out the
            # emails worth answering with Opus from the dashboard.
            try:
                if isinstance(intent_data, dict):
                    intent_data.update(claude_ai.assess_complexity(email['body'], email['subject']))
            except Exception as e:
                print(f"[{datetime.now()}] Error assessing complexity for {email_id}: {e}")

            # Past exchanges with this customer. The web page already sends
            # these to Claude; without them the draft repeats or contradicts
            # what Samuel has already told the customer.
            history = []
            try:
                raw_history = gmail_helper.get_customer_history(
                    service, sender_email, max_results=20
                )
                history = [
                    {'date': h.get('date', '')[:16], 'subject': h.get('subject', ''),
                     'body': h.get('body', '')[:4000], 'direction': h.get('direction', 'received')}
                    for h in raw_history
                ]
            except Exception as e:
                print(f"[{datetime.now()}] Could not fetch history for {email_id}: {e}")

            product_context = ''
            try:
                product_context = gmail_helper.product_replies_context(
                    service, [order_info] if order_info else [])
            except Exception as e:
                print(f"[{datetime.now()}] Could not fetch product replies for {email_id}: {e}")

            # Generate Claude AI draft response
            draft_response = claude_ai.generate_response(
                email['body'],
                email['subject'],
                customer_name,
                order_info,
                history=history,
                wing_context=product_context,
                images=email_images if email_images else None
            )

            # Save draft to database
            database.save_draft(
                email_id=email_id,
                thread_id=email['thread_id'],
                customer_email=sender_email,
                customer_name=customer_name,
                subject=email['subject'],
                email_body=email['body'],
                order_info_json=order_info,
                intent=json.dumps(intent_data) if isinstance(intent_data, dict) else intent_data,
                draft_response=draft_response
            )

            # Mark email as processed
            database.mark_processed(email_id)

            print(f"[{datetime.now()}] Email {email_id} processed and draft saved.")

        except Exception as e:
            print(f"[{datetime.now()}] Error processing email {email_id}: {e}")
            continue

    print(f"[{datetime.now()}] Email processing complete.")


def _drafts_still_to_answer(service, drafts):
    """Keep one draft per thread, only for emails nobody has dealt with yet.

    A draft is closed when its email has been read or archived, answered on the
    thread (dashboard or Gmail), or deleted. Drafts whose thread has a reply
    scheduled are left pending but kept out of the report, so they come back
    if the scheduled reply is cancelled.
    """
    scheduled_threads = database.get_scheduled_threads()
    latest = {}
    for draft in drafts:  # created_at ascending: the last one wins
        previous = latest.get(draft['thread_id'])
        if previous:
            database.close_draft(previous['token'])
        latest[draft['thread_id']] = draft

    kept = []
    for draft in latest.values():
        try:
            msg = service.users().messages().get(
                userId='me', id=draft['email_id'], format='minimal'
            ).execute()
        except Exception as e:
            if getattr(getattr(e, 'resp', None), 'status', None) == 404:
                database.close_draft(draft['token'])
            else:
                print(f"[{datetime.now()}] Could not check {draft['email_id']}: {e}")
            continue
        labels = msg.get('labelIds', [])
        if 'UNREAD' not in labels or 'INBOX' not in labels:
            database.close_draft(draft['token'])
            continue
        email = {'thread_id': draft['thread_id'], 'internal_date': int(msg.get('internalDate') or 0)}
        try:
            state = gmail_helper.needs_reply(service, email, scheduled_threads)
        except Exception as e:
            print(f"[{datetime.now()}] needs_reply failed for {draft['email_id']}: {e}")
            state = 'new'
        if state == 'answered':
            database.close_draft(draft['token'])
            continue
        if state == 'scheduled':
            continue
        kept.append(draft)
    return kept


def send_morning_report():
    print(f"[{datetime.now()}] Sending morning report...")
    try:
        service = gmail_helper.get_gmail_service()
        pending_drafts = _drafts_still_to_answer(service, database.get_pending_drafts())
    except Exception as e:
        print(f"[{datetime.now()}] Error fetching pending drafts: {e}")
        return

    count = len(pending_drafts)
    if count == 0:
        print(f"[{datetime.now()}] No pending drafts, skipping morning report.")
        return

    paris_tz = pytz.timezone("Europe/Paris")
    today_date = datetime.now(paris_tz).strftime("%d/%m/%Y")
    subject = f"[Max Sauveur] {count} email(s) en attente — {today_date}"

    # Build HTML body
    drafts_html = ""
    for draft in pending_drafts:
        token = draft.get("token", "")
        customer_name = draft.get("customer_name") or draft.get("customer_email", "")
        email_subject = draft.get("subject", "(sans objet)")
        email_body = draft.get("email_body", "")
        truncated_body = email_body[:300] + ("..." if len(email_body) > 300 else "")
        draft_response = draft.get("draft_response", "")

        validate_url = f"{BASE_URL}/validate/{token}"
        reject_url = f"{BASE_URL}/reject/{token}"

        # A request for Samuel's opinion is not a reply: no Validate button,
        # it would email the internal note to the customer.
        needs_info = claude_ai.parse_needs_info(draft_response)
        if needs_info:
            questions_html = "".join(f"<li>{q}</li>" for q in needs_info["questions"])
            holding_html = ""
            if needs_info["holding_reply"]:
                holding_html = f"""
            <div style="background: #f0f7ff; border: 1px solid #b3d4f5; border-radius: 4px; padding: 12px; margin-bottom: 16px;">
                <p style="margin: 0 0 4px 0; font-size: 12px; color: #888; font-weight: bold;">RÉPONSE D'ATTENTE AU CLIENT</p>
                <p style="margin: 0; font-size: 13px; color: #333; white-space: pre-wrap;">{needs_info["holding_reply"]}</p>
            </div>
            <a href="{validate_url}" style="display: inline-block; background: #28a745; color: white; padding: 10px 18px; border-radius: 5px; text-decoration: none; font-weight: bold; font-size: 14px; margin-right: 12px;">✅ Envoyer la réponse d'attente</a>"""
            answer_html = f"""
            <div style="background: #fffbea; border: 1px solid #f59e0b; border-radius: 4px; padding: 12px; margin-bottom: 16px;">
                <p style="margin: 0 0 4px 0; font-size: 12px; color: #92400e; font-weight: bold;">🤔 CLAUDE A BESOIN DE TON AVIS AVANT DE RÉPONDRE</p>
                <p style="margin: 0; font-size: 13px; color: #78350f; white-space: pre-wrap;">{needs_info["summary"]}</p>
                <ul style="margin: 8px 0 0 0; font-size: 13px; color: #78350f;">{questions_html}</ul>
            </div>{holding_html}
            <a href="{BASE_URL}" style="display: inline-block; background: #f59e0b; color: white; padding: 10px 18px; border-radius: 5px; text-decoration: none; font-weight: bold; font-size: 14px;">Répondre depuis le tableau de bord</a>"""
        else:
            answer_html = f"""
            <div style="background: #f0f7ff; border: 1px solid #b3d4f5; border-radius: 4px; padding: 12px; margin-bottom: 16px;">
                <p style="margin: 0 0 4px 0; font-size: 12px; color: #888; font-weight: bold;">BROUILLON DE RÉPONSE</p>
                <p style="margin: 0; font-size: 13px; color: #333; white-space: pre-wrap;">{draft_response}</p>
            </div>
            <div style="display: flex; gap: 12px;">
                <a href="{validate_url}" style="display: inline-block; background: #28a745; color: white; padding: 10px 18px; border-radius: 5px; text-decoration: none; font-weight: bold; font-size: 14px;">✅ Valider et envoyer</a>
                <a href="{reject_url}" style="display: inline-block; background: #dc3545; color: white; padding: 10px 18px; border-radius: 5px; text-decoration: none; font-weight: bold; font-size: 14px;">❌ Rejeter</a>
            </div>"""

        complex_html = ""
        try:
            intent = json.loads(draft.get("intent") or "{}")
        except (TypeError, ValueError):
            intent = {}
        if isinstance(intent, dict) and intent.get("complex"):
            complex_html = f"""
            <div style="background: #f5f3ff; border: 1px solid #8b5cf6; border-radius: 4px; padding: 10px 12px; margin-bottom: 12px; font-size: 13px; color: #4c1d95;">
                <strong>⚖️ Demande complexe</strong> : {intent.get("reason", "")}<br>
                Le brouillon ci-dessous vient de Sonnet. Pour une réponse rédigée par Opus 5.5, ouvre ce mail dans le <a href="{BASE_URL}" style="color: #5b21b6;">tableau de bord</a>.
            </div>"""

        drafts_html += f"""
        <div style="border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px; margin-bottom: 24px; background: #fafafa;">
            <h3 style="margin: 0 0 8px 0; color: #333;">{customer_name}</h3>
            <p style="margin: 0 0 8px 0; color: #666; font-size: 14px;"><strong>Sujet :</strong> {email_subject}</p>
            {complex_html}
            <div style="background: #fff; border: 1px solid #ddd; border-radius: 4px; padding: 12px; margin-bottom: 12px;">
                <p style="margin: 0; font-size: 13px; color: #555; white-space: pre-wrap;">{truncated_body}</p>
            </div>
            {answer_html}
        </div>
        """

    html_body = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 700px; margin: 0 auto; padding: 24px; background: #f5f5f5; color: #333;">
    <div style="background: white; border-radius: 10px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.08);">
        <h1 style="margin: 0 0 8px 0; font-size: 22px; color: #1a1a1a;">Max Sauveur — Rapport du matin</h1>
        <p style="margin: 0 0 28px 0; color: #666; font-size: 15px;">
            {count} email(s) en attente de validation pour le {today_date}.
        </p>
        {drafts_html}
        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="margin: 0; font-size: 12px; color: #aaa; text-align: center;">
            Envoyé automatiquement par l'assistant Max Sauveur · <a href="{BASE_URL}" style="color: #aaa;">Accéder au tableau de bord</a>
        </p>
    </div>
</body>
</html>"""

    try:
        import base64
        import email.mime.multipart
        import email.mime.text

        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["to"] = "samuel@maxsauveur.com"
        msg["subject"] = subject
        msg.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()

        print(f"[{datetime.now()}] Morning report sent with {count} draft(s).")
    except Exception as e:
        print(f"[{datetime.now()}] Error sending morning report: {e}")


def generate_and_save_daily_summary():
    print(f"[{datetime.now()}] Generating daily summary...")
    try:
        paris_tz = pytz.timezone("Europe/Paris")
        today_date = datetime.now(paris_tz).strftime("%Y-%m-%d")
        drafts_today = database.get_today_drafts()
        questions_today = database.get_today_questions()
        rejections_today = database.get_today_rejections()
        sent_today = database.get_today_sent_emails()
        if not drafts_today and not questions_today and not rejections_today and not sent_today:
            print(f"[{datetime.now()}] Nothing to summarize today, skipping.")
            return
        summary_text = claude_ai.generate_daily_summary(drafts_today, questions_today, rejections_today, sent_today)
        if summary_text:
            database.save_daily_summary(today_date, summary_text)
            print(f"[{datetime.now()}] Daily summary saved for {today_date}.")

        # Update response patterns knowledge base
        if sent_today:
            try:
                existing_patterns = database.get_response_patterns()
                patterns = claude_ai.extract_response_patterns(sent_today, existing_patterns)
                for p in patterns:
                    database.upsert_response_pattern(
                        topic=p['topic'],
                        topic_label=p['topic_label'],
                        situation=p['situation'],
                        response_template=p['response_template'],
                        key_points=p.get('key_points', '')
                    )
                print(f"[{datetime.now()}] Updated {len(patterns)} response pattern(s).")
            except Exception as e:
                print(f"[{datetime.now()}] Error updating response patterns: {e}")
    except Exception as e:
        print(f"[{datetime.now()}] Error generating daily summary: {e}")


def send_scheduled_emails():
    """Send any scheduled emails that are due."""
    try:
        due = database.get_due_scheduled_emails()
        if not due:
            return
        service = gmail_helper.get_gmail_service()
        for email in due:
            try:
                gmail_helper.send_email(
                    service,
                    email['to_email'],
                    email['subject'],
                    email['body'],
                    thread_id=email.get('thread_id') or None
                )
                database.mark_scheduled_sent(email['id'])
                database.log_sent_email(
                    to_email=email['to_email'],
                    subject=email['subject'],
                    body=email['body'],
                    source='scheduled',
                    thread_id=email.get('thread_id') or None
                )
                print(f"[{datetime.now()}] Scheduled email {email['id']} sent to {email['to_email']}")
            except Exception as e:
                database.mark_scheduled_failed(email['id'], str(e))
                print(f"[{datetime.now()}] Failed to send scheduled email {email['id']}: {e}")
    except Exception as e:
        print(f"[{datetime.now()}] Error in send_scheduled_emails: {e}")


scheduler = None


def start():
    """Register every job and start the background scheduler.

    Called at app startup (app.py) so the jobs run inside the web process.
    A separate Railway service would get its own container, and therefore its
    own empty SQLite file, so it would never see the rows written by the web
    process. Safe to call more than once: only the first call does anything.
    """
    global scheduler
    if scheduler is not None:
        return scheduler

    paris_tz = pytz.timezone("Europe/Paris")

    scheduler = BackgroundScheduler()

    # Process new emails every 60 minutes
    scheduler.add_job(
        process_new_emails,
        "interval",
        minutes=60,
        id="process_emails",
        next_run_time=datetime.now()
    )

    # Send morning report every day at 06:00 Paris time
    scheduler.add_job(
        send_morning_report,
        CronTrigger(hour=6, minute=0, timezone=paris_tz),
        id="morning_report"
    )

    # Generate daily summary every day at 20:00 Paris time
    scheduler.add_job(
        generate_and_save_daily_summary,
        CronTrigger(hour=20, minute=0, timezone=paris_tz),
        id="daily_summary"
    )

    # Send scheduled emails every minute
    scheduler.add_job(
        send_scheduled_emails,
        "interval",
        minutes=1,
        id="send_scheduled"
    )

    # Move repair pairs to "réceptionnée" once their label is delivered to SSL
    scheduler.add_job(
        repairs.refresh_from_wing,
        "interval",
        minutes=60,
        id="repair_tracking"
    )

    # Follow the pairs shipped back to customers from the main Wing account
    scheduler.add_job(
        returns.refresh_outbound,
        "interval",
        minutes=60,
        id="repair_outbound"
    )

    # Remind customers who have not confirmed their address after three days
    scheduler.add_job(
        returns.send_reminders,
        CronTrigger(hour=10, minute=0, timezone=paris_tz),
        id="repair_reminders"
    )

    # Workshops' replies: UPS labels and credit notes filed in the repair files
    scheduler.add_job(
        workshop_mail.check_mailboxes,
        "interval",
        minutes=60,
        id="workshop_mail"
    )

    # Carton delivered at the workshop: pairs move to "en cours de réparation"
    scheduler.add_job(
        ups_tracking.refresh,
        "interval",
        minutes=60,
        id="ups_tracking"
    )

    scheduler.start()
    print(f"[{datetime.now()}] Scheduler started ({len(scheduler.get_jobs())} jobs).")
    return scheduler


if __name__ == "__main__":
    start()

    try:
        while True:
            import time
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print(f"[{datetime.now()}] Scheduler stopped.")
