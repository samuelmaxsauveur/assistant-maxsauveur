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
    """Fetch Shopify orders for a single email. Returns all orders for the customer."""
    order_number = claude_ai.extract_order_number(email['subject']) or \
                   claude_ai.extract_order_number(email['body'])
    sender_email, customer_name = resolve_sender(email['sender'], email['body'])

    # Fetch complete order history: by email + by name (handles multiple Shopify accounts)
    all_orders = []
    try:
        all_orders = shopify_api.get_full_order_history(sender_email, customer_name) or []
    except Exception:
        pass

    # If a specific order number was mentioned in the email, make sure it's first
    if order_number:
        mentioned = next(
            (o for o in all_orders if str(o.get('number', '')).lstrip('#') == str(order_number)),
            None
        )
        if mentioned:
            all_orders = [mentioned] + [o for o in all_orders if o is not mentioned]
        elif not all_orders:
            try:
                specific = shopify_api.get_order_by_number(order_number)
                if specific:
                    all_orders = [specific]
            except Exception:
                pass

    # Last resort — search by name patterns in body
    if not all_orders:
        all_orders = _lookup_orders_by_name(customer_name, email['body'])

    return {
        'email': email,
        'order': all_orders[0] if all_orders else None,
        'orders': all_orders,
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

def _lookup_orders_by_name(name, body=''):
    """
    Last-resort: search Shopify using any useful info found in the email body.
    1. Email addresses explicitly written in the body
    2. Customer name from sender field
    3. Name patterns extracted from the body signature
    Returns list of orders (may be empty).
    """
    # --- Step 1: find emails explicitly labeled by the customer in the body ---
    # Only look for "Mails: x@x.com", "Email: x", "E-mail: x", "Contact: x" etc.
    if body:
        labeled_emails = re.findall(
            r'(?:mails?|e-?mail|contact|courriel)\s*[:\-]\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
            body, re.IGNORECASE
        )
        for found_email in labeled_emails:
            if 'maxsauveur' in found_email.lower():
                continue
            try:
                orders = shopify_api.get_orders_by_email(found_email)
                if orders:
                    return orders
            except Exception:
                pass

    # --- Step 2: build name candidates ---
    name_candidates = []
    # Sender name (if it looks like a real name, not just an email)
    if name and '@' not in name and len(name.split()) >= 2:
        name_candidates.append(name)

    # Extract name from body: look for patterns in the last 400 chars
    if body:
        tail = body[-400:]
        # Pattern 1: after a closing word (Cordialement, Merci, Best, etc.)
        sig_after_close = re.search(
            r'(?:cordialement|sincèrement|bonne journée|merci|regards|best|cdlt)[,.\s]+([A-ZÀ-Ýa-zà-ý][a-zà-ý]+\s+[A-ZÀ-Ý][A-ZÀ-Ýa-zà-ý]+)',
            tail, re.IGNORECASE
        )
        if sig_after_close:
            name_candidates.append(sig_after_close.group(1).strip())

        # Pattern 2: standalone "Prénom Nom" line on its own line
        # Handles: "Antoine FAU", "Antoine Fau", "ANTOINE FAU", "Jean-Pierre Dupont"
        standalone = re.findall(
            r'^([A-ZÀ-Ýa-zà-ý][a-zA-Zà-ÿÀ-Ÿ\-]{1,20}\s+[a-zA-ZÀ-Ÿ\-]{2,25})$',
            tail, re.MULTILINE
        )
        # Filter out obvious non-names (lines with common words)
        skip_words = {'téléphone', 'telephone', 'mobile', 'adresse', 'address', 'mails',
                      'email', 'bonjour', 'bonsoir', 'merci', 'cordialement', 'cdlt',
                      'service', 'client', 'contact', 'france', 'paris', 'lyon'}
        for s in standalone:
            if s.lower().split()[0] not in skip_words and s.lower().split()[-1] not in skip_words:
                name_candidates.append(s)

        # Pattern 3: "Nom : Antoine FAU" or "Name: Antoine FAU"
        labeled = re.search(
            r'(?:nom|name|prénom|prenom)\s*[:\-]\s*([A-ZÀ-Ýa-zà-ý][a-zà-ý]+\s+[A-ZÀ-Ý][A-ZÀ-Ýa-zà-ý]+)',
            tail, re.IGNORECASE
        )
        if labeled:
            name_candidates.append(labeled.group(1).strip())

    # Internal domains/emails to never use as customer
    internal_domains = {'maxsauveur.com', 'maxsauveur.fr'}

    # Build search terms: full name + each individual word (firstname / lastname separately)
    search_terms = list(name_candidates)
    for candidate in name_candidates:
        parts = candidate.split()
        if len(parts) >= 2:
            search_terms.extend(parts)  # try "Antoine" and "FAU" separately too

    seen_emails = set()

    # --- Step 3: search Shopify by each name/word candidate ---
    for term in search_terms:
        if len(term) < 3:
            continue
        try:
            customers = shopify_api.search_customers_by_name(term)
            if not customers:
                continue
            for found in customers[:3]:  # check top 3 results
                found_email = found.get('email', '')
                found_name = found.get('name', '')

                if not found_email or found_email in seen_emails:
                    continue
                # Skip internal emails
                if any(d in found_email.lower() for d in internal_domains):
                    continue

                # Verify at least one part of the original name appears in the found name
                all_parts = set(w.lower() for c in name_candidates for w in c.split() if len(w) > 2)
                found_parts = set(w.lower() for w in found_name.split() if len(w) > 2)
                if not all_parts & found_parts:
                    continue

                seen_emails.add(found_email)
                orders = shopify_api.get_orders_by_email(found_email)
                if orders:
                    return orders
        except Exception:
            pass
    return []


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

@app.route('/debug/orders')
def debug_orders():
    """Debug: show all orders found for a given email + name."""
    email = request.args.get('email', '')
    name = request.args.get('name', '')
    results = {}
    try:
        by_email = shopify_api.get_orders_by_email(email) if email else []
        results['by_email'] = [{'number': o['number'], 'products': [p['name'] for p in o['products']], 'date': o['created_at']} for o in by_email]
    except Exception as e:
        results['by_email_error'] = str(e)
    try:
        customers = shopify_api.search_customers_by_name(name) if name else []
        results['customers_found'] = customers
    except Exception as e:
        results['customers_error'] = str(e)
    try:
        by_name = shopify_api.get_all_orders_by_customer_name(name) if name else []
        results['by_name'] = [{'number': o['number'], 'products': [p['name'] for p in o['products']], 'date': o['created_at']} for o in by_name]
    except Exception as e:
        results['by_name_error'] = str(e)
    try:
        full = shopify_api.get_full_order_history(email, name)
        results['full_history'] = [{'number': o['number'], 'products': [p['name'] for p in o['products']], 'date': o['created_at']} for o in full]
    except Exception as e:
        results['full_error'] = str(e)
    return jsonify(results)


@app.route('/debug/raw-order')
def debug_raw_order():
    """Fetch raw Shopify order data to inspect email fields."""
    number = request.args.get('number', '')
    if not number:
        return jsonify({'error': 'Pass ?number=11734'})
    shop = os.environ.get('SHOPIFY_SHOP')
    token = os.environ.get('SHOPIFY_TOKEN')
    import requests as _req
    results = {}
    # REST variants
    for variant in [number, f'#{number}', f'SS{number}', f'#SS{number}']:
        resp = _req.get(
            f"https://{shop}/admin/api/2024-01/orders.json",
            headers={'X-Shopify-Access-Token': token},
            params={'name': variant, 'status': 'any'}
        )
        orders = resp.json().get('orders', [])
        if orders:
            o = orders[0]
            results[f'REST:{variant}'] = {
                'found': True,
                'order_number': o.get('order_number'),
                'name': o.get('name'),
                'email': o.get('email'),
                'contact_email': o.get('contact_email'),
                'customer_id': (o.get('customer') or {}).get('id'),
                'customer_email': (o.get('customer') or {}).get('email'),
                'billing_name': f"{(o.get('billing_address') or {}).get('first_name','')} {(o.get('billing_address') or {}).get('last_name','')}".strip(),
            }
        else:
            results[f'REST:{variant}'] = {'found': False}
    # GraphQL by name
    gql_url = f"https://{shop}/admin/api/2024-01/graphql.json"
    gql_headers = {'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'}
    for variant in [number, f'#{number}']:
        gql_query = '{ orders(first:1, query: "name:%s") { edges { node { name email contactEmail customer { id email } billingAddress { firstName lastName } } } } }' % variant
        gr = _req.post(gql_url, headers=gql_headers, json={'query': gql_query}, timeout=10)
        edges = gr.json().get('data', {}).get('orders', {}).get('edges', [])
        if edges:
            node = edges[0]['node']
            results[f'GQL:{variant}'] = {
                'found': True,
                'name': node.get('name'),
                'email': node.get('email'),
                'contactEmail': node.get('contactEmail'),
                'customer': node.get('customer'),
                'billing': node.get('billingAddress'),
            }
        else:
            results[f'GQL:{variant}'] = {'found': False, 'raw': gr.json()}
    return jsonify(results)


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

    # Always fetch full order history (email + name, handles multiple Shopify accounts)
    import sys as _sys
    _cn = extract_customer_name(data.get('sender', ''))
    print(f"[GENERATE] sender_email={sender_email!r} name={_cn!r}", file=_sys.stderr, flush=True)
    try:
        all_orders = shopify_api.get_full_order_history(sender_email, _cn) or []
    except Exception as _e:
        print(f"[GENERATE_ERROR] {_e}", file=_sys.stderr, flush=True)
        all_orders = []
    print(f"[GENERATE] all_orders after lookup: {[o['number'] for o in all_orders]}", file=_sys.stderr, flush=True)

    # Step: also search by name found in email body (e.g. signature differs from sender name)
    _body_name = claude_ai.extract_name_from_body(data.get('body', ''))
    if _body_name and _body_name.lower() not in (_cn or '').lower():
        print(f"[GENERATE] body_name found: {_body_name!r}", file=_sys.stderr, flush=True)
        try:
            _body_orders = shopify_api.get_full_order_history(None, _body_name) or []
            _seen = {o['number'] for o in all_orders}
            for o in _body_orders:
                if o['number'] not in _seen:
                    all_orders.append(o)
                    _seen.add(o['number'])
        except Exception as _e:
            print(f"[GENERATE] body_name lookup error: {_e}", file=_sys.stderr, flush=True)

    # If a specific order number was mentioned, put it first
    order_num_fallback = claude_ai.extract_order_number(data.get('subject', '')) or \
                         claude_ai.extract_order_number(data.get('body', ''))
    if order_num_fallback and all_orders:
        mentioned = next(
            (o for o in all_orders if str(o.get('number', '')).lstrip('#') == str(order_num_fallback)),
            None
        )
        if mentioned:
            all_orders = [mentioned] + [o for o in all_orders if o is not mentioned]
    if order_num_fallback and not all_orders:
        try:
            specific = shopify_api.get_order_by_number(order_num_fallback)
            if specific:
                all_orders = [specific]
        except Exception:
            pass

    # Final fallback: use single order passed from frontend
    if not all_orders and order_info:
        all_orders = [order_info]
    order_info = all_orders[0] if all_orders else None

    # Auto-fetch Wing tracking for expedition-related emails
    wing_context = ''
    order_num = str((order_info or {}).get('number', '')).replace('#', '').strip()
    if not order_num:
        order_num = claude_ai.extract_order_number(data.get('subject', '')) or \
                    claude_ai.extract_order_number(data.get('body', '')) or ''
    email_text_lower = (data.get('body', '') + ' ' + data.get('subject', '')).lower()
    is_fulfilled = (order_info or {}).get('fulfillment_status') == 'fulfilled'
    expedition_triggers = ['suivi', 'livraison', 'colis', 'reçu', 'recu', 'expédié', 'expedie',
                           'où est', 'ou est', 'tracking', 'retard', 'transporteur',
                           'expédition', 'expedition', 'arrivée', 'arrivee', 'délai', 'delai',
                           'pas encore reçu', 'pas reçu', 'non reçu', 'toujours pas']
    if order_num and (is_fulfilled or any(kw in email_text_lower for kw in expedition_triggers)):
        try:
            tracking = wing_automation.get_order_tracking(order_num)
            if tracking:
                wing_context = f"\n\n--- Suivi Wing pour la commande #{order_num} ---\n{tracking}"
        except Exception:
            pass

    suggested = claude_ai.generate_response(data['body'], data['subject'], customer_name, order_info, history,
                                            wing_context=wing_context, orders=all_orders)

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
    sender_email_ask = extract_email_address(sender)

    # Always fetch full order history (email + name, handles multiple Shopify accounts)
    try:
        all_orders_ask = shopify_api.get_full_order_history(
            sender_email_ask,
            extract_customer_name(sender)
        ) or []
    except Exception:
        all_orders_ask = []

    # Also search by name found in email body (signature may differ from sender)
    _ask_body_name = claude_ai.extract_name_from_body(data.get('body', ''))
    _ask_cn = extract_customer_name(sender)
    if _ask_body_name and _ask_body_name.lower() not in (_ask_cn or '').lower():
        try:
            _extra = shopify_api.get_full_order_history(None, _ask_body_name) or []
            _seen_ask = {o['number'] for o in all_orders_ask}
            for o in _extra:
                if o['number'] not in _seen_ask:
                    all_orders_ask.append(o)
                    _seen_ask.add(o['number'])
        except Exception:
            pass

    # If a specific order number was mentioned, put it first
    order_num_fallback = claude_ai.extract_order_number(data.get('subject', '')) or \
                         claude_ai.extract_order_number(data.get('body', ''))
    if order_num_fallback and all_orders_ask:
        mentioned = next(
            (o for o in all_orders_ask if str(o.get('number', '')).lstrip('#') == str(order_num_fallback)),
            None
        )
        if mentioned:
            all_orders_ask = [mentioned] + [o for o in all_orders_ask if o is not mentioned]
    if order_num_fallback and not all_orders_ask:
        try:
            specific = shopify_api.get_order_by_number(order_num_fallback)
            if specific:
                all_orders_ask = [specific]
        except Exception:
            pass

    # Detect manual name search in question: "cherche Antoine FAU", "regarde pour X", etc.
    _manual_name_match = re.search(
        r'(?:cherche[z]?|recherche[z]?|trouve[z]?|regarde[z]?\s+pour|look\s+up|search\s+for)\s+["\']?([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zà-ÿ\-]+)+)["\']?',
        question, re.IGNORECASE
    )
    if _manual_name_match:
        _manual_name = _manual_name_match.group(1)
        try:
            _manual_orders = shopify_api.get_full_order_history(None, _manual_name) or []
            _seen_manual = {o['number'] for o in all_orders_ask}
            for o in _manual_orders:
                if o['number'] not in _seen_manual:
                    all_orders_ask.append(o)
                    _seen_manual.add(o['number'])
        except Exception:
            pass

    if not all_orders_ask and order_info:
        all_orders_ask = [order_info]
    order_info = all_orders_ask[0] if all_orders_ask else order_info

    # Extract order number once (used by multiple Wing lookups below)
    def _extract_order_num():
        if order_info:
            n = str(order_info.get('number', '')).replace('#', '').strip()
            if n:
                return n
        raw = data.get('subject', '') + ' ' + data.get('body', '') + ' ' + question
        m = re.search(r'#?(\d{4,6})', raw)
        return m.group(1) if m else None

    wing_extra = ''
    q_lower = question.lower()
    email_body_lower = (data.get('body', '') + ' ' + data.get('subject', '')).lower()
    is_fulfilled_ask = (order_info or {}).get('fulfillment_status') == 'fulfilled'

    # Auto-fetch Wing whenever the email OR question involves expedition/tracking/relay
    expedition_keywords = ['wing', 'suivi', 'livraison', 'colis', 'expédié', 'expedie',
                           'tracking', 'retard', 'transporteur', 'expédition', 'expedition',
                           'reçu', 'recu', 'où est', 'ou est', 'arrivée', 'arrivee',
                           'pas encore reçu', 'pas reçu', 'point relais', 'relais', 'adresse']
    needs_wing = (
        is_fulfilled_ask or
        any(kw in email_body_lower for kw in expedition_keywords) or
        any(kw in q_lower for kw in expedition_keywords)
    )
    if needs_wing:
        order_num = _extract_order_num()
        if order_num:
            try:
                tracking = wing_automation.get_order_tracking(order_num)
                if tracking:
                    wing_extra += f"\n\n--- Données Wing pour la commande #{order_num} ---\n{tracking}"
            except Exception:
                pass
            if not wing_extra:
                try:
                    repair = wing_automation.check_repair_status(order_num)
                    if repair:
                        wing_extra += f"\n\n--- Données Wing réparation #{order_num} ---\n{repair}"
                except Exception:
                    pass

    full_question = question + wing_extra
    result = claude_ai.answer_question(
        data['body'],
        data['subject'],
        customer_name,
        order_info,
        full_question,
        previous_exchanges=previous_exchanges,
        orders=all_orders_ask
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

@app.route('/send-ajax', methods=['POST'])
def send_ajax():
    data = request.json or {}
    service = get_service()
    gmail_helper.send_email(service, data['to'], f"Re: {data['subject']}", data['body'], data.get('thread_id'))
    if data.get('email_id'):
        gmail_helper.mark_as_read(service, data['email_id'])
    database.log_sent_email(
        to_email=data['to'],
        subject=f"Re: {data['subject']}",
        body=data['body'],
        source='reply',
        thread_id=data.get('thread_id')
    )
    return jsonify({'success': True})

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
    """Fetch Wing info for an order via browser automation: tracking link + repair status."""
    data = request.json or {}
    order_num = str(data.get('order_number', '')).replace('#', '').strip()
    if not order_num:
        return jsonify({'success': False, 'error': 'Numéro de commande requis'})
    # Try base order first (tracking colis)
    tracking = wing_automation.get_order_tracking(order_num)
    if tracking:
        return jsonify({'success': True, 'data': {'relay': tracking, 'repair': None}})
    # Fallback: try repair variants
    repair = wing_automation.check_repair_status(order_num)
    if repair:
        return jsonify({'success': True, 'data': {'relay': None, 'repair': repair}})
    return jsonify({'success': False, 'error': 'Aucune info trouvée dans Wing', 'details': {'relay': None, 'repair': None}})


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
