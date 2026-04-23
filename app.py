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
from routes_client import client_bp
app.register_blueprint(validation)
app.register_blueprint(sav)
app.register_blueprint(client_bp)
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
    sender_email = data.get('sender_email', '')

    history = []
    if sender_email:
        try:
            service = get_service()
            raw = gmail_helper.get_customer_history(service, sender_email, max_results=10)
            history = [
                {'date': h.get('date', '')[:16], 'subject': h.get('subject', ''),
                 'body': h.get('body', '')[:600], 'direction': h.get('direction', 'received')}
                for h in raw
            ]
        except Exception:
            pass

    suggested = claude_ai.generate_response(data['body'], data['subject'], customer_name, order_info, history)

    # Check if Claude returned a needs_info JSON instead of a draft
    try:
        m = re.search(r'\{[\s\S]*\}', suggested)
        if m:
            import json as _json
            parsed = _json.loads(m.group())
            if parsed.get('needs_info'):
                return jsonify({'questions': parsed.get('questions', [])})
    except Exception:
        pass

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
    question = data.get('question', '')

    # Extract order number once (used by multiple Wing lookups below)
    def _extract_order_num():
        if order_info:
            n = str(order_info.get('number', '')).replace('#', '').strip()
            if n:
                return n
        m = re.search(r'#?(\d{4,6})', question)
        return m.group(1) if m else None

    wing_extra = ''
    q_lower = question.lower()

    # Auto-fetch relay point from Wing
    relay_keywords = ['point relais', 'point-relais', 'relais', 'relay', 'adresse de livraison',
                      'adresse relais', 'où livrer', 'adresse du client', 'cherche dans wing']
    if any(kw in q_lower for kw in relay_keywords):
        order_num = _extract_order_num()
        if order_num:
            try:
                relay_text = wing_api.get_relay_point_from_wing(order_num)
                if relay_text:
                    wing_extra += f"\n\n--- Point relais récupéré depuis Wing (commande #{order_num}) ---\n{relay_text[:800]}"
            except Exception:
                pass

    # Auto-fetch repair tracking from Wing — only if question explicitly mentions Wing
    tracking_keywords = ['wing', 'suivi wing', 'numéro de suivi wing', 'cherche dans wing', 'statut wing']
    if any(kw in q_lower for kw in tracking_keywords):
        order_num = _extract_order_num()
        if order_num:
            try:
                # Try repair order first (order_REPARATION)
                repair_status = wing_api.check_repair_status(order_num)
                if repair_status:
                    wing_extra += f"\n\n--- Statut récupéré depuis Wing pour la réparation #{order_num} ---\nStatut : {repair_status}"
            except Exception:
                pass
            # Also try getting the relay/tracking from the repair order in Wing
            try:
                repair_relay = wing_api.get_relay_point_from_wing(f"{order_num}_bis")
                if repair_relay:
                    wing_extra += f"\n\n--- Détails Wing commande réparation #{order_num}_bis ---\n{repair_relay[:800]}"
            except Exception:
                pass

    full_question = question + wing_extra
    result = claude_ai.answer_question(
        data['body'],
        data['subject'],
        customer_name,
        order_info,
        full_question,
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

import tempfile, base64

_label_store = {}  # temp storage: label_id -> {bytes, filename}

@app.route('/prepare-return-label', methods=['POST'])
def prepare_return_label():
    """Fetch Wing return label and return draft preview — does NOT send yet."""
    import uuid
    data = request.json or {}
    customer_name = data.get('customer_name', '')
    order_number = data.get('order_number', '').replace('#', '')
    email_body = data.get('email_body', '')

    # Generate return label from Wing — returns URL string
    label_url = None
    if order_number:
        try:
            print(f"[Label] API returned None, trying Playwright...")
            label_url = wing_automation.generate_return_label(order_number)
            print(f"[Label] Got URL: {label_url}")
        except Exception as e:
            print(f"Wing label error: {e}")

    # Generate SAV draft (include label URL in email if available)
    draft = claude_ai.generate_sav_approval_email(customer_name, order_number, email_body, label_url=label_url)
    return jsonify({'success': True, 'label_attached': bool(label_url), 'label_url': label_url, 'draft': draft})


@app.route('/send-return-label', methods=['POST'])
def send_return_label():
    """Send the prepared email with the stored label."""
    data = request.json or {}
    customer_email = data.get('customer_email', '')
    subject = data.get('subject', 'Prise en charge de votre réparation')
    thread_id = data.get('thread_id')
    body = data.get('body', '')
    label_id = data.get('label_id')
    order_number = data.get('order_number', '').replace('#', '')

    if not customer_email or not body:
        return jsonify({'success': False, 'error': 'Email ou message manquant'})

    service = get_service()
    gmail_helper.send_email(
        service, customer_email, f"Re: {subject}", body,
        thread_id=thread_id,
    )
    return jsonify({'success': True})


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


@app.route('/wing/debug-screenshot')
def wing_debug_screenshot():
    """Take a screenshot of Wing after login to diagnose automation issues."""
    import base64
    import tempfile
    from playwright.sync_api import sync_playwright
    import os, time
    results = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            # Login
            email = os.getenv('WING_EMAIL', '')
            password = os.getenv('WING_PASSWORD', '')
            results['wing_email_configured'] = bool(email)
            results['wing_password_configured'] = bool(password)
            page.goto('https://my.wing.eu/login')
            page.wait_for_load_state('networkidle')
            time.sleep(2)
            results['login_page_title'] = page.title()
            page.fill('input[name="email"]', email)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')
            page.wait_for_load_state('networkidle')
            time.sleep(4)
            results['after_login_url'] = page.url
            results['after_login_title'] = page.title()
            # Screenshot
            tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            page.screenshot(path=tmp.name, full_page=False)
            with open(tmp.name, 'rb') as f:
                results['screenshot_b64'] = base64.b64encode(f.read()).decode()
            # Try searching
            page.goto('https://my.wing.eu/orders')
            page.wait_for_load_state('networkidle')
            time.sleep(3)
            results['orders_page_url'] = page.url
            results['orders_page_text'] = page.inner_text('body')[:500]
            # Test search for 11768
            inp = page.locator('input[type="search"]').first
            inp.click(); inp.fill(''); inp.type('11768', delay=50)
            page.keyboard.press('Enter')
            import time; time.sleep(3)
            results['search_11768_text'] = page.inner_text('body')[:800]
            browser.close()
    except Exception as e:
        import traceback
        results['error'] = traceback.format_exc()
    # Return without screenshot in JSON (too large), just text info
    screenshot = results.pop('screenshot_b64', None)
    html = f'<pre>{results}</pre>'
    if screenshot:
        html += f'<img src="data:image/png;base64,{screenshot}" style="max-width:100%">'
    return html


@app.route('/wing/debug-search/<order_number>')
def wing_debug_search(order_number):
    """Search Wing for a specific order and return screenshot + page text."""
    import base64, tempfile, os, time
    from playwright.sync_api import sync_playwright
    html_parts = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            email = os.getenv('WING_EMAIL', '')
            password = os.getenv('WING_PASSWORD', '')
            page.goto('https://my.wing.eu/login')
            page.wait_for_selector('input[name="email"]')
            page.fill('input[name="email"]', email)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')
            page.wait_for_url(lambda url: '/login' not in url, timeout=15000)
            html_parts.append(f'<p>Logged in. URL: {page.url}</p>')

            # Search
            page.goto('https://my.wing.eu/orders')
            page.wait_for_selector('input[type="search"]', timeout=10000)
            inp = page.locator('input[type="search"]').first
            inp.click(); inp.fill(''); inp.type(order_number, delay=50)
            page.keyboard.press('Enter')
            time.sleep(2)
            # Screenshot after search (before Toutes)
            tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            page.screenshot(path=tmp.name)
            with open(tmp.name, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            html_parts.append(f'<h3>Après recherche "{order_number}" (avant Toutes)</h3>')
            html_parts.append(f'<img src="data:image/png;base64,{b64}" style="max-width:100%">')
            html_parts.append(f'<pre>Texte: {page.inner_text("body")[:600]}</pre>')

            # Click Toutes
            try:
                page.locator('text=Toutes').first.click()
                time.sleep(1)
            except Exception:
                html_parts.append('<p>Toutes tab not found</p>')
            # Screenshot after Toutes
            tmp2 = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            page.screenshot(path=tmp2.name)
            with open(tmp2.name, 'rb') as f:
                b64b = base64.b64encode(f.read()).decode()
            html_parts.append(f'<h3>Après clic Toutes</h3>')
            html_parts.append(f'<img src="data:image/png;base64,{b64b}" style="max-width:100%">')
            html_parts.append(f'<pre>Texte: {page.inner_text("body")[:600]}</pre>')
            # DOM first row
            row_info = page.evaluate("""() => {
                const rows = document.querySelectorAll('tbody tr');
                return 'Rows: ' + rows.length + ' | First: ' + (rows[0] ? rows[0].innerHTML.substring(0,300) : 'NONE');
            }""")
            html_parts.append(f'<pre>DOM: {row_info}</pre>')
            browser.close()
    except Exception as e:
        import traceback
        html_parts.append(f'<pre>ERROR: {traceback.format_exc()}</pre>')
    return ''.join(html_parts)


@app.route('/wing/lookup', methods=['POST'])
def wing_lookup():
    """Manually fetch Wing info for an order number — relay point + repair status."""
    data = request.json or {}
    order_num = str(data.get('order_number', '')).replace('#', '').strip()
    if not order_num:
        return jsonify({'success': False, 'error': 'Numéro de commande requis'})
    results = {}
    # Try repair order (_bis and variants)
    try:
        repair_info = wing_api.check_repair_status(order_num)
        results['repair'] = repair_info
    except Exception as e:
        results['repair_error'] = str(e)
    # Try relay point on base order
    try:
        relay_info = wing_api.get_relay_point_from_wing(order_num)
        results['relay'] = relay_info
    except Exception as e:
        results['relay_error'] = str(e)
    if not results.get('repair') and not results.get('relay'):
        return jsonify({'success': False, 'error': 'Aucune info trouvée dans Wing', 'details': results})
    return jsonify({'success': True, 'data': results})


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
