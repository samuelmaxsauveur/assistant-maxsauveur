import json
import re
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template, send_file

import database
import gmail as gmail_helper
import intake
import repair_catalog
import repair_docs
import repair_photos
import repairs
import returns
import ups_tracking
import workshop
import workshop_mail


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
    all_cases = database.get_repair_cases()
    counts = {key: sum(1 for c in all_cases if c['status'] in statuses) for key, _, statuses in repairs.STAGES}
    tab = request.args.get('onglet')
    if tab not in counts:
        # Open on the first stage that needs something from Samuel.
        tab = next((k for k in ('a-traiter', 'etiquette', 'retour', 'atelier', 'client', 'attente') if counts[k]), 'a-traiter')
    statuses = dict((k, s) for k, _, s in repairs.STAGES)[tab]
    cases = [c for c in all_cases if c['status'] in statuses]
    for c in cases:
        c['delivery'] = json.loads(c['return_delivery']) if c.get('return_delivery') else None
        c['photos'] = {s: repair_photos.names(c['id'], s) for s in repair_photos.SOURCES}
        c['docs'] = [(n,) + repair_docs.describe(n) for n in repair_docs.names(c['id'])]
        c['size'] = (c.get('product') or '').rsplit(' - ', 1)[-1] if ' - ' in (c.get('product') or '') else ''
        c['model'] = repair_catalog.model_name(c.get('product'))
    by_workshop = {}
    for c in cases:
        by_workshop.setdefault(c.get('workshop') or 'Sans atelier', []).append(c)
    open_cases = [c for c in all_cases if c['status'] not in ('delivered_customer', 'credit_received', 'cancelled')]
    return render_template('repairs.html', tab=tab, stages=repairs.STAGES, counts=counts, cases=cases,
                           by_workshop=by_workshop, statuses=repairs.STATUSES,
                           status_labels=repairs.STATUS_LABELS, decisions=repairs.DECISIONS,
                           parcel_labels=repairs.PARCEL_LABELS, workshops=workshop.WORKSHOPS,
                           delivery_options=returns.DELIVERY_OPTIONS, inbox=database.get_repair_inbox(),
                           open_cases=open_cases, ups_link=ups_tracking.tracking_link,
                           ups_auto=ups_tracking.enabled(), samuel_connected=gmail_helper.get_samuel_service() is not None)


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
    counts = {}
    for c in cases:
        if not repair_photos.names(c['id'], 'client'):
            try:
                repair_photos.import_customer_photos(c)
            except Exception as e:
                print(f"[Workshop] Customer photos unavailable for {c['wing_ref']}: {e}")
        counts[c['id']] = len(repair_photos.names(c['id']))
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

    # Workshops get Samuel's own address, customers the SAV one (hello@).
    service = gmail_helper.get_samuel_service()
    if service is None:
        return jsonify({'success': False, 'error': "La boîte samuel@maxsauveur.com n'est pas encore connectée à l'assistant"}), 400
    files = []
    for case in cases:
        name = case['wing_ref'].lstrip('#')
        for n, (data, source) in enumerate(repair_photos.load_all(case['id']), 1):
            files.append((data, f"{name}_{source}_{n}.jpg", 'image/jpeg'))
    to = ", ".join(workshop.WORKSHOPS[key]['emails'])
    gmail_helper.send_email_with_files(service, to, subject, body, files)

    name = workshop.WORKSHOPS[key]['name']
    stamp = datetime.now().strftime('%d/%m/%Y')
    for case in cases:
        notes = (case.get('notes') or '').strip()
        line = f"{stamp} : demande d'étiquette UPS envoyée à {name}"
        repairs.update_case(case['id'], workshop=name, status='label_requested', notes=f"{notes}\n{line}".strip())
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


@repairs_bp.route('/reparations/<int:case_id>/photos', methods=['POST'])
def upload_photos(case_id):
    """Reception photos taken by Samuel, from the phone or the computer."""
    if not database.get_repair_case(case_id):
        return jsonify({'success': False, 'error': 'Dossier introuvable'}), 404
    saved, errors = [], []
    for f in request.files.getlist('photos'):
        try:
            saved.append(repair_photos.save(case_id, f.read(), f.mimetype, source='reception'))
        except ValueError as e:
            errors.append(f"{f.filename} : {e}")
    return jsonify({'success': bool(saved), 'saved': len(saved), 'errors': errors})


@repairs_bp.route('/reparations/<int:case_id>/photos/client', methods=['POST'])
def import_client_photos(case_id):
    """Fetch (again) the photos the customer emailed."""
    case = database.get_repair_case(case_id)
    if not case:
        return jsonify({'success': False, 'error': 'Dossier introuvable'}), 404
    try:
        added = repair_photos.import_customer_photos(case)
    except Exception as e:
        return jsonify({'success': False, 'error': f"Gmail : {e}"}), 502
    return jsonify({'success': True, 'added': added})


@repairs_bp.route('/reparations/<int:case_id>/photos/<name>')
def photo_file(case_id, name):
    p = repair_photos.path(case_id, name)
    if not p:
        return 'Photo introuvable', 404
    return send_file(p, mimetype='image/jpeg')


@repairs_bp.route('/reparations/<int:case_id>/photos/<name>/supprimer', methods=['POST'])
def photo_delete(case_id, name):
    return jsonify({'success': repair_photos.delete(case_id, name)})


@repairs_bp.route('/reparations/atelier/colis-parti', methods=['POST'])
def carton_shipped():
    """The carton left with the workshop's UPS label: pairs are on their way."""
    data = request.json or {}
    tracking = re.sub(r'\s', '', data.get('tracking') or '').upper()
    if tracking and not workshop_mail.UPS_TRACKING.fullmatch(tracking):
        return jsonify({'success': False, 'error': "Numéro UPS invalide (il commence par 1Z, 18 caractères)"}), 400
    ids = [int(i) for i in data.get('case_ids') or []]
    if not ids:
        return jsonify({'success': False, 'error': 'Sélectionne au moins une paire'}), 400
    now = datetime.utcnow().isoformat()
    for case_id in ids:
        database.update_repair_case(case_id, {'workshop_tracking': tracking or None, 'workshop_shipped_at': now})
        repairs.update_case(case_id, status='sent_workshop')
    return jsonify({'success': True, 'count': len(ids)})


@repairs_bp.route('/reparations/<int:case_id>/documents', methods=['POST'])
def upload_document(case_id):
    if not database.get_repair_case(case_id):
        return jsonify({'success': False, 'error': 'Dossier introuvable'}), 404
    f = request.files.get('document')
    try:
        repair_docs.save_upload(case_id, request.form.get('kind', 'document'), f.filename if f else '', f.read() if f else b'')
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True})


@repairs_bp.route('/reparations/<int:case_id>/documents/<name>')
def document_file(case_id, name):
    p = repair_docs.path(case_id, name)
    return send_file(p, mimetype='application/pdf') if p else ('Document introuvable', 404)


@repairs_bp.route('/reparations/a-classer/<int:item_id>')
def inbox_file(item_id):
    item = next((i for i in database.get_repair_inbox(pending_only=False) if i['id'] == item_id), None)
    p = repair_docs.inbox_path(item['stored_as']) if item else None
    return send_file(p, mimetype='application/pdf') if p else ('Document introuvable', 404)


@repairs_bp.route('/reparations/a-classer/<int:item_id>', methods=['POST'])
def inbox_assign(item_id):
    """File a workshop document the assistant could not place on its own."""
    item = next((i for i in database.get_repair_inbox() if i['id'] == item_id), None)
    if not item:
        return jsonify({'success': False, 'error': 'Document déjà classé ou introuvable'}), 404
    ids = [int(i) for i in (request.json or {}).get('case_ids') or []]
    if not ids:
        return jsonify({'success': False, 'error': 'Choisis au moins un dossier'}), 400
    repair_docs.file_inbox_item(item, ids)
    if item['kind'] == 'credit':
        for case_id in ids:
            repairs.update_case(case_id, status='credit_received')
    return jsonify({'success': True})


@repairs_bp.route('/reparations/courriels', methods=['POST'])
def check_workshop_mail():
    """Read the workshops' emails now instead of waiting for the hourly run."""
    try:
        done = workshop_mail.check_mailboxes(days=30)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 502
    return jsonify({'success': True, 'done': done})


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
