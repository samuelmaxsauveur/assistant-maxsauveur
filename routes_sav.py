import re
from flask import Blueprint, request, jsonify, render_template
from concurrent.futures import ThreadPoolExecutor
import gmail as gmail_helper
import shopify_api
import claude_ai
import database
import wing_automation

sav = Blueprint('sav', __name__)

STATUS_LABELS = {
    'pending':             'En attente',
    'approved':            'Approuvé — email envoyé',
    'received_warehouse':  'Reçu à l\'entrepôt',
    'sent_repair':         'Envoyé en réparation',
    'in_repair':           'En cours de réparation',
    'repaired_available':  'Réparé — disponible au dépôt',
    'returned_to_client':  'Retourné au client',
    'rejected':            'Refusé',
    'ignored':             'Mis de côté',
}


def _get_service():
    return gmail_helper.get_gmail_service()


@sav.route('/sav')
def sav_page():
    from concurrent.futures import ThreadPoolExecutor
    import re

    try:
        service = _get_service()
        emails = gmail_helper.get_unread_emails(service, max_results=10)

        def enrich(email):
            order_number = claude_ai.extract_order_number(email['body'] + ' ' + email['subject'])
            order_info = None
            sender = email['sender']
            match = re.search(r'<(.+?)>', sender)
            sender_email = match.group(1) if match else sender
            try:
                if order_number:
                    order_info = shopify_api.get_order_by_number(order_number)
                # If no order found by number, try by sender email
                if not order_info and sender_email and '@' in sender_email:
                    orders = shopify_api.get_orders_by_email(sender_email)
                    if orders:
                        order_info = orders[0]
                        if not order_number:
                            order_number = order_info.get('number', '')
            except Exception:
                pass
            return {**email, 'order_number': order_number, 'order': order_info, 'sender_email': sender_email}

        with ThreadPoolExecutor(max_workers=5) as ex:
            enriched = list(ex.map(enrich, emails))
    except Exception as e:
        enriched = []

    cases = database.get_sav_cases()
    return render_template('sav.html', emails=enriched, cases=cases, status_labels=STATUS_LABELS)


@sav.route('/sav/save-case', methods=['POST'])
def save_case():
    data = request.json
    case_id = database.save_sav_case(
        email_id=data['email_id'],
        thread_id=data['thread_id'],
        customer_email=data['customer_email'],
        customer_name=data['customer_name'],
        subject=data['subject'],
        email_body=data['email_body'],
        order_number=data.get('order_number')
    )
    return jsonify({'success': True, 'case_id': case_id})


@sav.route('/sav/approve', methods=['POST'])
def approve():
    data = request.json
    case_id = data['case_id']
    customer_email = data['customer_email']
    customer_name = data['customer_name']
    order_number = data.get('order_number', '')
    email_body = data['email_body']
    subject = data['subject']
    thread_id = data.get('thread_id')

    # Generate return label via Wing (may take 20-40s)
    label_bytes = None
    if order_number:
        try:
            label_bytes = wing_automation.generate_return_label(order_number)
        except Exception as e:
            print(f"Wing label error: {e}")

    # Generate email from fixed template
    draft = claude_ai.generate_sav_approval_email(customer_name, order_number, email_body)

    # Send email (with or without attachment)
    service = _get_service()
    gmail_helper.send_email(
        service, customer_email, f"Re: {subject}", draft,
        thread_id=thread_id,
        attachment_bytes=label_bytes,
        attachment_filename=f"etiquette_retour_{order_number}.pdf" if order_number else "etiquette_retour.pdf"
    )

    database.update_sav_case_status(case_id, 'approved')
    return jsonify({'success': True, 'label_attached': label_bytes is not None, 'draft': draft})


@sav.route('/sav/generate-rejection', methods=['POST'])
def generate_rejection():
    data = request.json
    draft = claude_ai.generate_sav_rejection_email(
        customer_name=data['customer_name'],
        order_number=data.get('order_number', ''),
        email_body=data['email_body'],
        reason=data['reason']
    )
    return jsonify({'draft': draft})


@sav.route('/sav/send-rejection', methods=['POST'])
def send_rejection():
    data = request.json
    service = _get_service()
    gmail_helper.send_email(
        service,
        data['customer_email'],
        f"Re: {data['subject']}",
        data['body'],
        thread_id=data.get('thread_id')
    )
    database.update_sav_case_status(data['case_id'], 'rejected')
    return jsonify({'success': True})


@sav.route('/sav/analyze', methods=['POST'])
def analyze():
    data = request.json
    # Fetch full thread history for richer context
    history = []
    customer_email = data.get('customer_email', '')
    thread_id = data.get('thread_id', '')
    try:
        if customer_email and '@' in customer_email:
            service = _get_service()
            raw = gmail_helper.get_customer_history(service, customer_email, max_results=30)
            history = [
                {
                    'date': h.get('date', '')[:16],
                    'subject': h.get('subject', ''),
                    'body': h.get('body', '')[:800],
                    'direction': h.get('direction', 'received'),
                }
                for h in raw
            ]
    except Exception:
        pass
    result = claude_ai.analyze_sav_email(
        data['email_body'], data['subject'],
        order_info=data.get('order'),
        history=history
    )
    return jsonify(result)


@sav.route('/sav/outbound/search', methods=['POST'])
def outbound_search():
    query = (request.json or {}).get('query', '').strip()
    if not query:
        return jsonify({'found': False})
    customer_email = None
    customer_name = None
    orders = []
    order_num = re.sub(r'[^0-9]', '', query) if not '@' in query and not ' ' in query else None
    if '@' in query:
        customer_email = query
        try:
            orders = shopify_api.get_orders_by_email(query) or []
            if orders:
                customer_name = orders[0].get('customer_name')
        except Exception:
            pass
    elif order_num:
        try:
            order = shopify_api.get_order_by_number(order_num)
            if order:
                orders = [order]
                customer_email = order.get('customer_email')
                customer_name = order.get('customer_name')
        except Exception:
            pass
    else:
        try:
            customers = shopify_api.search_customers_by_name(query)
            if customers:
                customer_email = customers[0]['email']
                customer_name = customers[0]['name']
        except Exception:
            pass
    if customer_email and not orders:
        try:
            orders = shopify_api.get_orders_by_email(customer_email) or []
        except Exception:
            pass
    if not customer_email and not orders:
        return jsonify({'found': False})
    return jsonify({
        'found': True,
        'customer_email': customer_email or '',
        'customer_name': customer_name or '',
        'orders': [
            {
                'number': o.get('number', ''),
                'created_at': o.get('created_at', ''),
                'total': o.get('total', ''),
                'fulfillment_status': o.get('fulfillment_status', ''),
                'products': o.get('products', []),
            }
            for o in orders[:10]
        ],
    })


@sav.route('/sav/outbound/generate', methods=['POST'])
def outbound_generate():
    data = request.json or {}
    draft = claude_ai.generate_outbound_email(
        customer_name=data.get('customer_name', ''),
        customer_email=data.get('customer_email', ''),
        subject_type=data.get('subject_type', 'custom'),
        user_draft=data.get('user_draft', ''),
        order_info=data.get('order_info'),
        feedback=data.get('feedback', ''),
        previous_draft=data.get('previous_draft', ''),
    )
    return jsonify({'draft': draft})


@sav.route('/sav/outbound/save-template', methods=['POST'])
def outbound_save_template():
    data = request.json or {}
    subject_type = data.get('subject_type', '')
    body = data.get('body', '')
    if not subject_type or not body:
        return jsonify({'success': False, 'error': 'Données manquantes'})
    database.save_outbound_template(subject_type, body)
    # Return the env var name/value so the user can persist it in Railway
    env_key = f"TEMPLATE_{subject_type.upper()}"
    return jsonify({'success': True, 'env_key': env_key, 'env_value': body})


@sav.route('/sav/outbound/templates', methods=['GET'])
def outbound_templates():
    types = ['stock_issue', 'return_refund', 'return_exchange', 'custom']
    result = {}
    for t in types:
        result[t] = database.get_outbound_template(t) is not None
    return jsonify(result)


@sav.route('/sav/outbound/send', methods=['POST'])
def outbound_send():
    data = request.json or {}
    customer_email = data.get('customer_email', '')
    subject = data.get('subject', 'Max Sauveur — Service Client')
    body = data.get('body', '')
    if not customer_email or not body:
        return jsonify({'success': False, 'error': 'Email et message requis'})
    try:
        service = _get_service()
        gmail_helper.send_email(service, customer_email, subject, body)
        database.log_sent_email(
            to_email=customer_email,
            subject=subject,
            body=body,
            source='outbound'
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@sav.route('/sav/ignore', methods=['POST'])
def ignore_case():
    data = request.json
    database.update_sav_case_status(data['case_id'], 'ignored')
    # Do NOT mark email as read, do NOT send anything
    return jsonify({'success': True})


@sav.route('/sav/restore', methods=['POST'])
def restore_case():
    data = request.json
    database.update_sav_case_status(data['case_id'], 'pending')
    return jsonify({'success': True})


@sav.route('/sav/check-status', methods=['POST'])
def check_status():
    data = request.json
    order_number = data.get('order_number', '')
    if not order_number:
        return jsonify({'found': False, 'status': None})
    status = wing_automation.check_repair_status(order_number)
    return jsonify({'found': status is not None, 'status': status})


@sav.route('/sav/update-status', methods=['POST'])
def update_status():
    data = request.json
    case_id = data['case_id']
    new_status = data['new_status']
    note = data.get('note', '')
    notify = data.get('notify', False)

    # Update case status and log history
    database.update_sav_case_status(case_id, new_status)

    # Send notification email if requested
    notified = False
    if notify:
        case = next((c for c in database.get_sav_cases() if c['id'] == case_id), None)
        if case:
            body = claude_ai.generate_sav_status_notification(
                case['customer_name'], case['order_number'], new_status
            )
            service = _get_service()
            gmail_helper.send_email(
                service,
                case['customer_email'],
                f"Mise à jour de votre réparation Max Sauveur",
                body,
                thread_id=case['thread_id']
            )
            notified = True

    database.add_sav_status_history(case_id, new_status, note=note or None, notified=notified)
    return jsonify({'success': True, 'notified': notified})


@sav.route('/sav/knowledge')
def knowledge_base():
    patterns = database.get_response_patterns()
    return render_template('knowledge.html', patterns=patterns)


@sav.route('/sav/knowledge/update', methods=['POST'])
def knowledge_update():
    """Manually trigger a knowledge base update from recent sent emails."""
    import claude_ai as ai
    try:
        sent = database.get_today_sent_emails()
        if not sent:
            return jsonify({'success': False, 'message': 'Aucun email envoyé aujourd\'hui'})
        existing = database.get_response_patterns()
        patterns = ai.extract_response_patterns(sent, existing)
        for p in patterns:
            database.upsert_response_pattern(
                topic=p['topic'],
                topic_label=p['topic_label'],
                situation=p['situation'],
                response_template=p['response_template'],
                key_points=p.get('key_points', '')
            )
        return jsonify({'success': True, 'updated': len(patterns)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@sav.route('/sav/knowledge/delete/<int:pattern_id>', methods=['POST'])
def knowledge_delete(pattern_id):
    conn = database.get_connection()
    try:
        conn.execute("DELETE FROM response_patterns WHERE id = ?", (pattern_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'success': True})


@sav.route('/sav/case/<int:case_id>')
def case_detail(case_id):
    cases = database.get_sav_cases()
    case = next((c for c in cases if c['id'] == case_id), None)
    if not case:
        return "Cas introuvable", 404
    history = database.get_sav_status_history(case_id)
    return render_template('sav_case.html', case=case, history=history, status_labels=STATUS_LABELS)
