import os
import re
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, redirect, url_for, jsonify
from dotenv import load_dotenv

load_dotenv()

import gmail as gmail_helper
import shopify_api
import claude_ai
import wing_api
import wing_automation
import database
import scheduler as scheduler_module

app = Flask(__name__)
from routes_validation import validation
from routes_sav import sav
app.register_blueprint(validation)
app.register_blueprint(sav)
gmail_service = None

def _fetch_order_for_email(email):
    """Fetch Shopify order for a single email. Runs in thread pool."""
    order_number = claude_ai.extract_order_number(email['body'] + ' ' + email['subject'])
    order_info = None
    try:
        if order_number:
            order_info = shopify_api.get_order_by_number(order_number)
    except Exception:
        pass
    sender_email, customer_name = resolve_sender(email['sender'], email['body'])
    try:
        if not order_info:
            orders = shopify_api.get_orders_by_email(sender_email)
            if orders:
                order_info = orders[0]
    except Exception:
        pass
    return {
        'email': email,
        'order': order_info,
        'sender_email': sender_email,
        'intent': {"intent": "other", "address": None, "has_full_address": False},
        'draft_response': ''
    }


def get_service():
    global gmail_service
    if not gmail_service:
        gmail_service = gmail_helper.get_gmail_service()
    return gmail_service

def extract_email_address(sender):
    match = re.search(r'<(.+?)>', sender)
    return match.group(1) if match else sender

def extract_customer_name(sender):
    match = re.search(r'^(.+?)\s*<', sender)
    if match:
        return match.group(1).strip().strip('"')
    return sender

def resolve_sender(sender, body):
    """If sender is a noreply/form address, extract real customer email and name from the body."""
    raw_email = extract_email_address(sender)
    customer_name = extract_customer_name(sender)

    if re.search(r'noreply|no-reply|donotreply', raw_email, re.IGNORECASE):
        # Extract email from form body (e.g. "E-mail : kevin@example.com")
        email_match = re.search(r'e-?mail\s*:\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', body, re.IGNORECASE)
        if email_match:
            raw_email = email_match.group(1).strip()
        # Extract name from form body (e.g. "Nom : Kevin Peschot")
        name_match = re.search(r'nom\s*:\s*(.+)', body, re.IGNORECASE)
        if name_match:
            customer_name = name_match.group(1).strip()

    return raw_email, customer_name

@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200

@app.route('/debug')
def debug():
    import traceback
    results = {}
    try:
        import database
        results['database'] = 'OK'
        database.get_pending_drafts()
        results['db_query'] = 'OK'
    except Exception as e:
        results['database'] = traceback.format_exc()
    try:
        service = get_service()
        results['gmail'] = 'OK'
    except Exception as e:
        results['gmail'] = str(e)
    try:
        import shopify_api
        results['shopify_import'] = 'OK'
    except Exception as e:
        results['shopify_import'] = str(e)
    try:
        import claude_ai
        results['claude_import'] = 'OK'
    except Exception as e:
        results['claude_import'] = str(e)
    try:
        import scheduler as s
        results['scheduler_import'] = 'OK'
    except Exception as e:
        results['scheduler_import'] = str(e)
    return jsonify(results), 200

@app.route('/')
def index():
    import traceback as tb
    try:
        processed = []
        pending_count = len(database.get_pending_drafts())
        gmail_error = None
        try:
            service = get_service()
            emails = gmail_helper.get_unread_emails(service, max_results=10)
        except Exception as e:
            gmail_error = str(e)
            return render_template('index.html', emails=[], pending_count=pending_count, gmail_error=gmail_error)

        with ThreadPoolExecutor(max_workers=5) as executor:
            processed = list(executor.map(_fetch_order_for_email, emails))
        return render_template('index.html', emails=processed, pending_count=pending_count)
    except Exception:
        return f"<pre style='padding:20px'>{tb.format_exc()}</pre>", 500

@app.route('/trigger-check', methods=['POST'])
def trigger_check():
    try:
        scheduler_module.process_new_emails()
        return jsonify({'status': 'done', 'message': 'Vérification effectuée'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/generate', methods=['POST'])
def generate():
    data = request.json
    customer_name = extract_customer_name(data['sender'])
    order_info = data.get('order')
    suggested = claude_ai.generate_response(
        data['body'],
        data['subject'],
        customer_name,
        order_info
    )
    return jsonify({'response': suggested})

@app.route('/ask', methods=['POST'])
def ask():
    data = request.json
    sender = data.get('sender', '')
    customer_name, _ = resolve_sender(sender, data.get('body', ''))
    if customer_name == sender:
        customer_name = extract_customer_name(sender)
    order_info = data.get('order')
    previous_exchanges = data.get('previous_exchanges', [])
    result = claude_ai.answer_question(
        data['body'],
        data['subject'],
        customer_name,
        order_info,
        data['question'],
        previous_exchanges=previous_exchanges
    )
    samuel_answer = result.get('samuel_answer', '')
    updated_draft = result.get('updated_draft', '')

    # Save this exchange for daily learning
    try:
        database.save_question_log(
            email_id=data.get('email_id', ''),
            subject=data.get('subject', ''),
            customer_name=customer_name,
            question=data['question'],
            claude_answer=samuel_answer or result,
            updated_draft=updated_draft
        )
    except Exception:
        pass

    return jsonify({'samuel_answer': samuel_answer, 'updated_draft': updated_draft})


@app.route('/send', methods=['POST'])
def send():
    service = get_service()
    to = request.form['to']
    subject = request.form['subject']
    body = request.form['body']
    thread_id = request.form.get('thread_id')
    email_id = request.form.get('email_id')
    gmail_helper.send_email(service, to, f"Re: {subject}", body, thread_id)
    if email_id:
        gmail_helper.mark_as_read(service, email_id)
    return redirect(url_for('index'))

@app.route('/change-relay', methods=['POST'])
def change_relay():
    data = request.json
    order_number = data['order_number']
    new_address = data['address']
    customer_email = data['customer_email']
    customer_name = data['customer_name']

    # Changer le point relais dans Wing via Playwright
    success = wing_automation.change_relay_point(order_number, new_address)

    # Envoyer email de notification à Samuel
    service = get_service()
    status = "✅ SUCCÈS" if success else "❌ ÉCHEC"
    notification_body = f"""Changement de point relais effectué automatiquement.

{status}

Commande : #{order_number}
Client : {customer_name} ({customer_email})
Nouveau point relais : {new_address}

Vérifie sur Wing que le changement est bien effectué avant de valider la réponse client dans l'interface.

http://localhost:8080
"""
    gmail_helper.send_email(
        service,
        "samuel@maxsauveur.com",
        f"[Vérification] Changement relais #{order_number}",
        notification_body
    )

    return jsonify({'success': success})

@app.route('/save-process', methods=['POST'])
def save_process():
    data = request.json
    name = data.get('name', '').strip()
    trigger = data.get('trigger', '').strip()
    steps = data.get('steps', '').strip()
    if not name or not steps:
        return jsonify({'success': False, 'error': 'Nom et étapes requis'}), 400
    database.save_process(name, trigger, steps)
    return jsonify({'success': True})


@app.route('/wing', methods=['GET', 'POST'])
def wing_order():
    result = None
    if request.method == 'POST':
        result = wing_api.create_shipment(
            customer_name=request.form['customer_name'],
            customer_email=request.form['customer_email'],
            customer_phone=request.form['customer_phone'],
            address=request.form['address'],
            city=request.form['city'],
            postal_code=request.form['postal_code'],
            country=request.form.get('country', 'FR')
        )
    return render_template('wing.html', result=result)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    app.run(debug=False, host='0.0.0.0', port=port)
