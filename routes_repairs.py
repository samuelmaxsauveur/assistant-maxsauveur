import base64
import json
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template

import database
import gmail as gmail_helper
import intake
import repair_catalog
import repairs
import returns
import workshop


def _after_status_change(case):
    """A pair back at the logistics centre: ask its customer where to send it.

    Returns a short note for the page, or None.
    """
    if case['status'] != 'back_logistics' or case.get('return_requested_at'):
        return None
    try:
        returns.request_return(case)
        return f"e-mail « paire prête » envoyé à {case['customer_email']}"
    except Exception as e:
        print(f"[Returns] Ready email failed for {case['wing_ref']}: {e}")
        return f"⚠️ e-mail « paire prête » non envoyé : {e}"

repairs_bp = Blueprint('repairs', __name__)


@repairs_bp.route('/reparations')
def repairs_page():
    cases = database.get_repair_cases()
    show_closed = request.args.get('tout') == '1'
    if not show_closed:
        cases = [c for c in cases if c['status'] not in ('delivered_customer', 'cancelled')]
    # Pairs received and not yet shipped to a workshop.
    all_cases = database.get_repair_cases()
    ready = [c for c in all_cases if c['status'] == 'received']
    # Pairs at a workshop or on their way back from it.
    at_workshop = [c for c in all_cases if c['status'] in ('sent_workshop', 'in_repair', 'returning')]
    for c in cases:
        c['delivery'] = json.loads(c['return_delivery']) if c.get('return_delivery') else None
    return render_template('repairs.html', cases=cases, statuses=repairs.STATUSES,
                           status_labels=repairs.STATUS_LABELS, decisions=repairs.DECISIONS,
                           parcel_labels=repairs.PARCEL_LABELS, show_closed=show_closed,
                           ready=ready, workshops=workshop.WORKSHOPS, at_workshop=at_workshop,
                           delivery_options=returns.DELIVERY_OPTIONS)


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


@repairs_bp.route('/reparations/ajouter', methods=['POST'])
def add_received():
    """Open a file for a pair that came back without the 🔧 button."""
    data = request.json or {}
    try:
        case = intake.add_received_pair(data.get('order_number', ''), data.get('product', ''),
                                        data.get('problem', ''), data.get('decision', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'wing_ref': case['wing_ref'], 'product': case['product'],
                    'workshop': case.get('workshop') or '', 'workshop_ref': case.get('workshop_ref') or '',
                    'photos_thread': bool(case.get('email_id'))})


@repairs_bp.route('/reparations/statut-groupe', methods=['POST'])
def batch_status():
    """Move several pairs at once, e.g. the carton just handed to UPS."""
    data = request.json or {}
    status = data.get('status')
    if status not in repairs.STATUS_LABELS:
        return jsonify({'success': False, 'error': 'Statut inconnu'}), 400
    ids = [int(i) for i in data.get('case_ids') or []]
    notes = []
    for case_id in ids:
        case, _ = repairs.update_case(case_id, status=status)
        note = _after_status_change(case)
        if note:
            notes.append(f"{case['wing_ref']} : {note}")
    return jsonify({'success': True, 'count': len(ids), 'notes': notes})


@repairs_bp.route('/reparations/<int:case_id>', methods=['POST'])
def update_repair(case_id):
    try:
        case, synced = repairs.update_case(case_id, **(request.json or {}))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'synced': synced, 'note': _after_status_change(case),
                    'status_label': repairs.STATUS_LABELS[case['status']]})


# --- Customer page: confirm where the repaired pair goes -------------------

def _case_for_token(token):
    case = database.get_repair_case_by_token(token)
    if not case or case['status'] == 'cancelled':
        return None
    return case


@repairs_bp.route('/retour/<token>')
def return_page(token):
    case = _case_for_token(token)
    if not case:
        return render_template('return.html', case=None), 404
    confirmed = json.loads(case['return_delivery']) if case.get('return_confirmed_at') else None
    return render_template('return.html', case=case, prefill=None if confirmed else returns.prefill(case),
                           confirmed=confirmed, options=returns.DELIVERY_OPTIONS,
                           model=repair_catalog.model_name(case.get('product')) or 'paire')


@repairs_bp.route('/retour/<token>/relais', methods=['POST'])
def return_relays(token):
    case = _case_for_token(token)
    data = request.json or {}
    if not case or data.get('option') not in ('chrono_relay', 'colissimo_relay'):
        return jsonify({'success': False, 'error': 'Demande invalide'}), 400
    if not (data.get('zip') or '').strip() or not (data.get('city') or '').strip():
        return jsonify({'success': False, 'error': 'Indiquez votre code postal et votre ville'}), 400
    try:
        relays = returns.search_relays(data['option'], data.get('address', ''), data['zip'].strip(),
                                       data['city'].strip(), data.get('country') or 'FR')
    except Exception as e:
        print(f"[Returns] Relay search failed: {e}")
        return jsonify({'success': False, 'error': "Recherche indisponible pour le moment, réessayez dans quelques minutes."}), 502
    return jsonify({'success': True, 'relays': relays})


@repairs_bp.route('/retour/<token>/confirmer', methods=['POST'])
def return_confirm(token):
    case = _case_for_token(token)
    if not case:
        return jsonify({'success': False, 'error': 'Lien invalide'}), 404
    if case.get('return_confirmed_at'):
        return jsonify({'success': False, 'error': "Votre adresse est déjà confirmée. Écrivez-nous pour la modifier."}), 409
    try:
        returns.confirm_return(case, request.json or {})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True})


@repairs_bp.route('/reparations/refresh', methods=['POST'])
def refresh_repairs():
    """Run the hourly Wing check now, then mirror every file to the sheet."""
    try:
        moved = repairs.refresh_from_wing()
    except Exception as e:
        return jsonify({'success': False, 'error': f"Wing : {e}"}), 502
    synced = sum(repairs.push_to_sheet(c) for c in database.get_repair_cases())
    return jsonify({'success': True, 'moved': moved, 'synced': synced})
