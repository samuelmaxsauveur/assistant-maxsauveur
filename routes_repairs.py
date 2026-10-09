import base64
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template

import database
import gmail as gmail_helper
import repairs
import workshop

repairs_bp = Blueprint('repairs', __name__)


@repairs_bp.route('/reparations')
def repairs_page():
    cases = database.get_repair_cases()
    show_closed = request.args.get('tout') == '1'
    if not show_closed:
        cases = [c for c in cases if c['status'] not in ('delivered_customer', 'cancelled')]
    # Pairs received and not yet shipped to a workshop.
    ready = [c for c in database.get_repair_cases() if c['status'] == 'received']
    return render_template('repairs.html', cases=cases, statuses=repairs.STATUSES,
                           status_labels=repairs.STATUS_LABELS, decisions=repairs.DECISIONS,
                           parcel_labels=repairs.PARCEL_LABELS, show_closed=show_closed,
                           ready=ready, workshops=workshop.WORKSHOPS)


def _selected_cases(data):
    key = data.get('workshop')
    if key not in workshop.WORKSHOPS:
        raise ValueError("Atelier inconnu")
    ids = [int(i) for i in data.get('case_ids') or []]
    cases = [c for c in (database.get_repair_case(i) for i in ids) if c]
    if not cases:
        raise ValueError("Sélectionne au moins une paire")
    return key, cases


@repairs_bp.route('/reparations/atelier/apercu', methods=['POST'])
def workshop_preview():
    """Draft of the UPS label request, with the customer photos counted per pair."""
    try:
        key, cases = _selected_cases(request.json or {})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    service = gmail_helper.get_gmail_service()
    counts = {c['id']: len(workshop.customer_photos(service, c)) for c in cases}
    draft = workshop.draft_email(key, cases, counts)
    missing_ref = [c['wing_ref'] for c in cases if not (c.get('workshop_ref') or '').strip()]
    missing_problem = [c['wing_ref'] for c in cases if not (c.get('problem') or '').strip()]
    return jsonify({'success': True, **draft, 'photos': sum(counts.values()),
                    'missing_ref': missing_ref, 'missing_problem': missing_problem})


@repairs_bp.route('/reparations/atelier/envoyer', methods=['POST'])
def workshop_send():
    data = request.json or {}
    try:
        key, cases = _selected_cases(data)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    body = (data.get('body') or '').strip()
    subject = (data.get('subject') or '').strip()
    if not body or not subject:
        return jsonify({'success': False, 'error': "Objet ou message vide"}), 400

    service = gmail_helper.get_gmail_service()
    files = []
    for case in cases:
        name = case['wing_ref'].lstrip('#')
        for n, photo in enumerate(workshop.customer_photos(service, case), 1):
            ext = photo['mime'].split('/')[-1].replace('jpeg', 'jpg')
            files.append((base64.b64decode(photo['data']), f"{name}_{n}.{ext}", photo['mime']))
    to = ", ".join(workshop.WORKSHOPS[key]['emails'])
    gmail_helper.send_email_with_files(service, to, subject, body, files)

    name = workshop.WORKSHOPS[key]['name']
    stamp = datetime.now().strftime('%d/%m/%Y')
    for case in cases:
        notes = (case.get('notes') or '').strip()
        line = f"{stamp} : demande d'étiquette UPS envoyée à {name}"
        repairs.update_case(case['id'], workshop=name, notes=f"{notes}\n{line}".strip())
    return jsonify({'success': True, 'photos': len(files), 'to': to})


@repairs_bp.route('/reparations/statut-groupe', methods=['POST'])
def batch_status():
    """Move several pairs at once, e.g. the carton just handed to UPS."""
    data = request.json or {}
    status = data.get('status')
    if status not in repairs.STATUS_LABELS:
        return jsonify({'success': False, 'error': 'Statut inconnu'}), 400
    ids = [int(i) for i in data.get('case_ids') or []]
    for case_id in ids:
        repairs.update_case(case_id, status=status)
    return jsonify({'success': True, 'count': len(ids)})


@repairs_bp.route('/reparations/<int:case_id>', methods=['POST'])
def update_repair(case_id):
    try:
        case, synced = repairs.update_case(case_id, **(request.json or {}))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'synced': synced,
                    'status_label': repairs.STATUS_LABELS[case['status']]})


@repairs_bp.route('/reparations/refresh', methods=['POST'])
def refresh_repairs():
    """Run the hourly Wing check now, then mirror every file to the sheet."""
    try:
        moved = repairs.refresh_from_wing()
    except Exception as e:
        return jsonify({'success': False, 'error': f"Wing : {e}"}), 502
    synced = sum(repairs.push_to_sheet(c) for c in database.get_repair_cases())
    return jsonify({'success': True, 'moved': moved, 'synced': synced})
