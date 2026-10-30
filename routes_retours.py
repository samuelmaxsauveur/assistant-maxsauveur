import os

from flask import Blueprint, request, jsonify, render_template, send_file, abort

import database
import retours

retours_bp = Blueprint('retours', __name__)

SHOP_ADMIN = "https://admin.shopify.com/store/{shop}/orders/{id}"


def _admin_link(order):
    if not order:
        return None
    shop = (os.getenv('SHOPIFY_SHOP') or 'maxsauveur').split('.')[0]
    return SHOP_ADMIN.format(shop=shop, id=order['legacy_id'])


@retours_bp.route('/retours')
def retours_page():
    all_cases = database.get_client_returns()
    counts = {key: sum(1 for c in all_cases if c['status'] in statuses) for key, _, statuses in retours.STAGES}
    reminders = sum(1 for c in all_cases if c['status'] == 'waiting_client' and c['data'].get('reminder_due'))
    selected_id = request.args.get('id', type=int)
    selected = next((c for c in all_cases if c['id'] == selected_id), None)
    tab = request.args.get('onglet')
    if selected and tab is None:
        tab = next(k for k, _, s in retours.STAGES if selected['status'] in s)
    if tab not in counts:
        # Open on the first step that needs something from Samuel.
        tab = next((k for k in ('a-traiter', 'reponse', 'avoir', 'rembourser', 'attente') if counts[k]), 'a-traiter')
    statuses = dict((k, s) for k, _, s in retours.STAGES)[tab]
    cases = [c for c in all_cases if c['status'] in statuses]
    if tab == 'attente':
        cases.sort(key=lambda c: not c['data'].get('reminder_due'))
    view = None
    if selected:
        data = selected['data']
        result = retours.analyse(data)
        ids = [c['id'] for c in cases]
        following = ids[ids.index(selected['id']) + 1:] if selected['id'] in ids else []
        view = {
            'case': selected, 'data': data, 'result': result, 'order': data.get('order'),
            'admin_link': _admin_link(data.get('order')),
            'refund_deadline': retours.refund_deadline(data),
            'offer_deadline': retours._offer_deadline(data) if data.get('offer_sent_at') else None,
            'next_id': following[0] if following else None,
            'tab_of': next(k for k, _, s in retours.STAGES if selected['status'] in s),
        }
    custom = sorted(database.get_all_outbound_templates(), key=lambda t: (t.get('label') or '').lower())
    return render_template('retours.html', tab=tab, stages=retours.STAGES, counts=counts, cases=cases,
                           reminders=reminders, view=view, updated=request.args.get('maj') == '1',
                           templates=retours.TEMPLATES, custom_templates=custom,
                           credit_templates=retours.CREDIT_TEMPLATES, status_labels=retours.STATUS_LABELS,
                           reasons=retours.REASONS, choices=retours.CHOICES, categories=retours.CATEGORY_LABELS,
                           euros=retours.euros, short_date=retours.short_date, fr_date=retours.fr_date)


def _ok(case, **extra):
    return jsonify({'success': True, 'id': case['id'], 'status': case['status'], **extra})


def _fail(e, code=400):
    return jsonify({'success': False, 'error': str(e)}), code


@retours_bp.route('/retours/nouveau', methods=['POST'])
def new_return():
    f = request.files.get('photo')
    if not f:
        return _fail("Aucune photo reçue")
    try:
        case = retours.create_from_photo(f.read())
    except Exception as e:
        print(f"[Retours] New file failed: {e}")
        return _fail(f"Photo illisible : {e}")
    return _ok(case, error=case['data'].get('error'))


@retours_bp.route('/retours/<int:case_id>/photo/<name>')
def photo(case_id, name):
    path = retours.photo_path(case_id, name)
    if not path:
        abort(404)
    return send_file(path, mimetype='image/jpeg')


@retours_bp.route('/retours/<int:case_id>/recalculer', methods=['POST'])
def recompute(case_id):
    try:
        case = retours.update(case_id, request.json or {})
    except Exception as e:
        print(f"[Retours] Recompute failed for file {case_id}: {e}")
        return _fail(e)
    return _ok(case)


@retours_bp.route('/retours/<int:case_id>/mail', methods=['POST'])
def prepare_mail(case_id):
    data = request.json or {}
    try:
        case = retours.prepare_mail(case_id, data.get('template') or 'offer',
                                    (data.get('consignes') or '').strip(), data.get('amount'))
    except Exception as e:
        print(f"[Retours] Draft failed for file {case_id}: {e}")
        return _fail(e)
    return _ok(case, subject=case['data'].get('mail_subject'), body=case['data'].get('mail_body'))


@retours_bp.route('/retours/<int:case_id>/avoir', methods=['POST'])
def create_credit(case_id):
    data = request.json or {}
    try:
        case = retours.create_credit(case_id, bonus=bool(data.get('bonus')), amount=data.get('amount'))
    except Exception as e:
        print(f"[Retours] Credit failed for file {case_id}: {e}")
        return _fail(e)
    return _ok(case)


@retours_bp.route('/retours/<int:case_id>/envoyer', methods=['POST'])
def send(case_id):
    data = request.json or {}
    subject, body = (data.get('subject') or '').strip(), (data.get('body') or '').strip()
    if not subject or not body:
        return _fail("Objet ou message vide")
    try:
        case = retours.send(case_id, data.get('template') or '', subject, body)
    except Exception as e:
        print(f"[Retours] Send failed for file {case_id}: {e}")
        return _fail(e)
    return _ok(case)


@retours_bp.route('/retours/<int:case_id>/statut', methods=['POST'])
def set_status(case_id):
    status = (request.json or {}).get('status')
    if status not in retours.STATUS_LABELS:
        return _fail("Statut inconnu")
    return _ok(retours.set_status(case_id, status, (request.json or {}).get('note')))


@retours_bp.route('/retours/<int:case_id>/client', methods=['POST'])
def customer_choice(case_id):
    """Samuel reads the answer himself: the customer takes the credit, or the refund."""
    choice = (request.json or {}).get('choice')
    case = database.get_client_return(case_id)
    if not case:
        return _fail("Dossier introuvable", 404)
    try:
        if choice == 'credit':
            bonus = (case['data'].get('offer_kind') or 'bonus') == 'bonus'
            case = retours.create_credit(case_id, bonus=bonus, amount=(request.json or {}).get('amount'))
        elif choice == 'refund':
            case = retours.set_status(case_id, 'to_refund', "le client choisit le remboursement")
        else:
            return _fail("Choix inconnu")
    except Exception as e:
        return _fail(e)
    return _ok(case)


@retours_bp.route('/retours/<int:case_id>/supprimer', methods=['POST'])
def delete(case_id):
    retours.delete(case_id)
    return jsonify({'success': True})


@retours_bp.route('/retours/verifier', methods=['POST'])
def check_now():
    try:
        retours.tick()
    except Exception as e:
        return _fail(e)
    return jsonify({'success': True})
