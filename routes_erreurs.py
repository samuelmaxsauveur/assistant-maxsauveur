"""The « Erreurs logistique » page (preparation errors by SSL) and the customer's page /erreur/<token>."""
from flask import Blueprint, request, jsonify, render_template, send_file, abort

import database
import erreurs
import returns

erreurs_bp = Blueprint('erreurs', __name__)


def _ok(case=None, **extra):
    out = {'success': True, **extra}
    if case:
        out.update(id=case['id'], status=case['status'])
    return jsonify(out)


def _fail(e, code=400):
    return jsonify({'success': False, 'error': str(e)}), code


def _case(case_id):
    case = database.get_logistics_error(case_id)
    if not case:
        abort(404)
    return case


@erreurs_bp.route('/erreurs')
def erreurs_page():
    all_cases = database.get_logistics_errors()
    counts = {key: sum(1 for c in all_cases if erreurs.stage(c) == key) for key, _ in erreurs.STAGES}
    selected = next((c for c in all_cases if c['id'] == request.args.get('id', type=int)), None)
    tab = request.args.get('onglet')
    if selected and tab is None:
        tab = erreurs.stage(selected)
    if tab != 'tableau' and tab not in counts:
        tab = next((k for k in ('a-faire', 'attente', 'renvoi') if counts[k]), 'a-faire')
    cases = [c for c in all_cases if erreurs.stage(c) == tab]

    view = None
    if selected:
        data = selected['data']
        mail_key = request.args.get('mail') or erreurs.proposed_mail(selected) or ('free' if data.get('reply_unread') else None)
        subject, body = erreurs.draft(selected, mail_key) if mail_key in erreurs.MAILS else ('', '')
        view = {'case': selected, 'data': data, 'order': data.get('order') or {},
                'photos': erreurs.photo_names(selected['id']), 'mail_key': mail_key,
                'subject': subject, 'body': body, 'cost': erreurs.cost(selected),
                'billing': erreurs.billing(selected), 'proposed': erreurs.proposed_mail(selected),
                'defaults': erreurs.form_defaults(selected), 'reship_text': erreurs.reship_text(selected),
                'variants': erreurs.variants_of(selected) if (data.get('kind') == erreurs.WRONG and data.get('confirmed_at')
                            and not data.get('reship') and selected['status'] not in erreurs.CLOSED + ('to_refund',)) else []}

    month = request.args.get('mois') or erreurs.months()[0]
    rows = erreurs.ledger(month) if tab == 'tableau' else []
    ssl = erreurs.ssl_mail(month) if tab == 'tableau' else None
    return render_template(
        'erreurs.html', tab=tab, stages=erreurs.STAGES, counts=counts, cases=cases, view=view,
        status_labels=erreurs.STATUS_LABELS, kinds=erreurs.KINDS, mails=erreurs.MAILS,
        month=month, months=erreurs.months(), month_label=erreurs.month_label, rows=rows, ssl=ssl,
        ssl_email=erreurs.SSL_EMAIL, billing_labels=erreurs.BILLING_LABELS,
        total=sum(r['amount'] for r in rows), to_claim=sum(r['amount'] for r in rows if r['billing'] == 'to_claim'),
        euros=erreurs.euros, short_date=erreurs.short_date, line_label=erreurs._line_label, human_label=erreurs.human_label,
        costs={'reship': erreurs.COST_RESHIP, 'refund': erreurs.COST_REFUND, 'missing': erreurs.COST_MISSING},
        WRONG=erreurs.WRONG, MISSING=erreurs.MISSING)


@erreurs_bp.route('/erreurs/nouveau', methods=['POST'])
def new_case():
    """From the SAV card (email, thread, sender) or from this page (order number only)."""
    d = request.json or {}
    try:
        case = erreurs.create(d.get('kind'), d.get('order_number'), d.get('customer_name', ''),
                              d.get('customer_email', ''), d.get('thread_id'), d.get('subject', ''))
    except ValueError as e:
        return _fail(e)
    except Exception as e:
        print(f"[Erreurs] New file failed: {e}")
        return _fail(e, 500)
    return _ok(case, url=f"/erreurs?id={case['id']}")


@erreurs_bp.route('/erreurs/<int:case_id>/photos', methods=['POST'])
def upload_photos(case_id):
    _case(case_id)
    added = 0
    try:
        for f in request.files.getlist('photos'):
            if erreurs.save_photo(case_id, f.read(), f.mimetype):
                added += 1
    except ValueError as e:
        return _fail(e)
    return _ok(added=added)


@erreurs_bp.route('/erreurs/<int:case_id>/photo/<name>')
def photo(case_id, name):
    path = erreurs.thumb_path(case_id, name) if request.args.get('mini') else erreurs.photo_path(case_id, name)
    if not path:
        abort(404)
    return send_file(path, mimetype='image/jpeg')


@erreurs_bp.route('/erreurs/<int:case_id>/photo/<name>/supprimer', methods=['POST'])
def photo_delete(case_id, name):
    return _ok(deleted=erreurs.delete_photo(case_id, name))


@erreurs_bp.route('/erreurs/<int:case_id>/verifier', methods=['POST'])
def verify(case_id):
    _case(case_id)
    try:
        return _ok(erreurs.verify(case_id))
    except ValueError as e:
        return _fail(e)
    except Exception as e:
        print(f"[Erreurs] Photo check failed for file {case_id}: {e}")
        return _fail(e, 500)


@erreurs_bp.route('/erreurs/<int:case_id>/confirmer', methods=['POST'])
def confirm(case_id):
    _case(case_id)
    d = request.json or {}
    try:
        lines = [int(i) for i in d.get('lines') or []]
        case = erreurs.confirm(case_id, lines, d.get('ordered_text', ''), d.get('received_text', ''),
                               bool(d.get('voluntary')))
    except ValueError as e:
        return _fail(e)
    return _ok(case, alert=case['data'].get('alert'))


ACTIONS = {
    'pas-erreur': lambda i, d: erreurs.not_an_error(i, d.get('note', '')),
    'etiquette': lambda i, d: erreurs.create_label(i),
    'renvoi': lambda i, d: erreurs.reship_same_place(i),
    'recue': lambda i, d: erreurs.mark_received(i),
    'remboursement': lambda i, d: erreurs.choose_refund(i),
    'rembourse': lambda i, d: erreurs.mark_refunded(i),
    'annuler': lambda i, d: erreurs.cancel(i),
    'reglage': lambda i, d: erreurs.set_flag(i, d.get('key'), d.get('value')),
    'ignorer-mail': lambda i, d: erreurs.skip_mail(i, d.get('template')),
    'relire-fil': lambda i, d: erreurs.refresh_from_thread(database.get_logistics_error(i), full=True),
    'paire': lambda i, d: erreurs.set_reship_item(i, d.get('variant_id')),
}


@erreurs_bp.route('/erreurs/<int:case_id>/<action>', methods=['POST'])
def action(case_id, action):
    if action not in ACTIONS:
        abort(404)
    _case(case_id)
    try:
        case = ACTIONS[action](case_id, request.json or {})
    except ValueError as e:
        return _fail(e)
    except Exception as e:
        print(f"[Erreurs] {action} failed for file {case_id}: {e}")
        return _fail(e, 500)
    return _ok(case, alert=case['data'].get('alert'))


@erreurs_bp.route('/erreurs/<int:case_id>/rediger', methods=['POST'])
def draft_reply(case_id):
    _case(case_id)
    try:
        return _ok(**erreurs.draft_reply(case_id, (request.json or {}).get('consigne', '')))
    except Exception as e:
        print(f"[Erreurs] Draft failed for file {case_id}: {e}")
        return _fail(e, 500)


@erreurs_bp.route('/erreurs/<int:case_id>/envoyer', methods=['POST'])
def send(case_id):
    _case(case_id)
    d = request.json or {}
    try:
        return _ok(erreurs.send(case_id, d.get('template'), d.get('subject', ''), d.get('body', '')))
    except ValueError as e:
        return _fail(e)
    except Exception as e:
        print(f"[Erreurs] Send failed for file {case_id}: {e}")
        return _fail(e, 500)


@erreurs_bp.route('/erreurs/<int:case_id>/supprimer', methods=['POST'])
def delete(case_id):
    case = _case(case_id)
    if case['data'].get('label') or case['data'].get('reship'):
        return _fail("Une étiquette ou un renvoi existe déjà dans Wing : annule le dossier plutôt que de le supprimer.")
    erreurs.delete(case_id)
    return _ok()


@erreurs_bp.route('/erreurs/ssl/envoyer', methods=['POST'])
def send_ssl():
    d = request.json or {}
    month = d.get('month') or ''
    _, _, ids = erreurs.ssl_mail(month)
    try:
        n = erreurs.send_ssl_mail(month, d.get('subject', ''), d.get('body', ''), ids)
    except (ValueError, RuntimeError) as e:
        return _fail(e)
    return _ok(claimed=n)


@erreurs_bp.route('/erreurs/verifier-maintenant', methods=['POST'])
def check_now():
    return _ok(checked=erreurs.tick())


# --- Customer page: where the right pair goes -------------------------------

def _page_case(token):
    case = erreurs.by_token(token)
    return case if case and case['data'].get('received_at') else None


@erreurs_bp.route('/erreur/<token>')
def customer_page(token):
    case = _page_case(token)
    if not case:
        return render_template('return.html', case=None), 404
    data = case['data']
    confirmed = data.get('delivery') if data.get('address_confirmed_at') else None
    ordered = data.get('ordered_text') or 'paire'
    return render_template(
        'return.html', case={**case, 'return_token': token}, confirmed=confirmed,
        prefill=None if confirmed else erreurs.prefill(case), options=erreurs.PAGE_OPTIONS,
        model=ordered, base_path=f"/erreur/{token}", default_option=data.get('mode') or 'chrono_relay',
        heading="Votre bonne paire est prête",
        intro=f"Votre {ordered} est prête à partir. Choisissez le point relais Chronopost (Shop2Shop) "
              f"où vous souhaitez la recevoir, ou confirmez votre adresse, puis validez.")


@erreurs_bp.route('/erreur/<token>/relais', methods=['POST'])
def customer_relays(token):
    case = _page_case(token)
    data = request.json or {}
    if not case or data.get('option') != 'chrono_relay':
        return _fail('Demande invalide')
    if not (data.get('zip') or '').strip() or not (data.get('city') or '').strip():
        return _fail('Indiquez votre code postal et votre ville')
    try:
        relays = returns.search_relays('chrono_relay', data.get('address', ''), data['zip'].strip(),
                                       data['city'].strip(), data.get('country') or 'FR')
    except Exception as e:
        print(f"[Erreurs] Relay search failed: {e}")
        return _fail("Recherche indisponible pour le moment, réessayez dans quelques minutes.", 502)
    return _ok(relays=relays)


@erreurs_bp.route('/erreur/<token>/confirmer', methods=['POST'])
def customer_confirm(token):
    case = _page_case(token)
    if not case:
        return _fail('Lien invalide', 404)
    if case['data'].get('address_confirmed_at'):
        return _fail("Votre adresse est déjà confirmée. Écrivez-nous pour la modifier.", 409)
    try:
        erreurs.confirm_address(case, request.json or {})
    except ValueError as e:
        return _fail(e)
    return _ok()
