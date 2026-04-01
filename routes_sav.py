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
            try:
                if order_number:
                    order_info = shopify_api.get_order_by_number(order_number)
            except Exception:
                pass
            sender = email['sender']
            match = re.search(r'<(.+?)>', sender)
            sender_email = match.group(1) if match else sender
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


@sav.route('/sav/case/<int:case_id>')
def case_detail(case_id):
    cases = database.get_sav_cases()
    case = next((c for c in cases if c['id'] == case_id), None)
    if not case:
        return "Cas introuvable", 404
    history = database.get_sav_status_history(case_id)
    return render_template('sav_case.html', case=case, history=history, status_labels=STATUS_LABELS)
