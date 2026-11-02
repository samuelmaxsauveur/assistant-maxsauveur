import os
import secrets

from flask import Blueprint, Response, jsonify, render_template, request

import avis
import database

avis_bp = Blueprint('avis', __name__)


@avis_bp.before_request
def require_password():
    # This page sends mails under Samuel's name: closed until a password is set.
    expected = os.getenv('AVIS_PASSWORD', '')
    if not expected:
        return "Page fermée : AVIS_PASSWORD n'est pas défini.", 503
    supplied = (request.authorization.password if request.authorization else '') or ''
    if not secrets.compare_digest(supplied.encode(), expected.encode()):
        return Response("Mot de passe requis", 401, {'WWW-Authenticate': 'Basic realm="Avis", charset="UTF-8"'})


def _fail(e, code=400):
    return jsonify({'success': False, 'error': str(e)}), code


@avis_bp.route('/avis')
def avis_page():
    all_invites = database.get_review_invites([s for _, _, statuses in avis.STAGES for s in statuses])
    counts = {key: sum(i['status'] in statuses for i in all_invites) for key, _, statuses in avis.STAGES}
    tab = request.args.get('onglet')
    if tab not in counts:
        tab = 'probleme' if counts['probleme'] and not counts['a-valider'] else 'a-valider'
    statuses = dict((key, s) for key, _, s in avis.STAGES)[tab]
    return render_template('avis.html', stages=avis.STAGES, counts=counts, tab=tab,
                           invites=[i for i in all_invites if i['status'] in statuses],
                           sendable=avis.SENDABLE, configured=avis.configured(), review_url=avis.review_url(),
                           seeded=database.review_invites_seeded())


@avis_bp.route('/avis/envoyer', methods=['POST'])
def send_mails():
    ids = [str(i) for i in (request.get_json().get('ids') or [])]
    try:
        return jsonify({'success': True, **avis.send(ids)})
    except Exception as e:
        print(f"[Avis] Sending stopped: {e}")
        return _fail(e)


@avis_bp.route('/avis/<review_id>/mail', methods=['POST'])
def save_mail(review_id):
    data = request.get_json()
    try:
        avis.edit(review_id, (data.get('subject') or '').strip(), data.get('body') or '')
    except Exception as e:
        return _fail(e)
    return jsonify({'success': True})


@avis_bp.route('/avis/<review_id>/retirer', methods=['POST'])
def remove_mail(review_id):
    request.get_json()
    try:
        avis.remove(review_id)
    except Exception as e:
        return _fail(e)
    return jsonify({'success': True})


@avis_bp.route('/avis/<review_id>/remettre', methods=['POST'])
def restore_mail(review_id):
    request.get_json()
    try:
        avis.restore(review_id)
    except Exception as e:
        return _fail(e)
    return jsonify({'success': True})
