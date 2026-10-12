"""
routes_popup.py — Borne iPad du tirage au sort pop-up store, et son tableau de bord
"""

import csv
import io
import secrets
import time

from flask import (Blueprint, Response, jsonify, make_response, redirect, render_template,
                   request, url_for)

import popup_store

popup_bp = Blueprint("popup", __name__)

# Tickets ouverts par le PIN vendeur : un ticket = une participation.
# Gardés en mémoire, ce qui suffit avec un seul worker gunicorn.
_tickets = {}
TICKET_TTL = 15 * 60

# Anti force brute sur le PIN : 5 essais ratés bloquent la borne 5 minutes.
_pin_failures = []


def _admin_ok():
    key = request.args.get("key") or request.cookies.get("popup_admin")
    return bool(popup_store.ADMIN_KEY) and secrets.compare_digest(key or "", popup_store.ADMIN_KEY)


# ── Borne ─────────────────────────────────────────────────────────────────────

@popup_bp.route("/popup")
def kiosk():
    return render_template("popup.html", ends_label=popup_store.ENDS_LABEL,
                           min_purchase=popup_store.MIN_PURCHASE)


@popup_bp.route("/popup/reglement")
def rules():
    return render_template("popup_reglement.html", ends_label=popup_store.ENDS_LABEL,
                           min_purchase=popup_store.MIN_PURCHASE,
                           counts=popup_store.tier_counts(500), address=popup_store.EVENT_ADDRESS)


@popup_bp.route("/popup/manifest.webmanifest")
def manifest():
    resp = jsonify({
        "name": "Tirage Max Sauveur",
        "short_name": "Max Sauveur",
        "start_url": "/popup",
        "scope": "/popup",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#1f2021",
        "theme_color": "#1f2021",
        "icons": [{"src": "/static/popup/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/popup/icon-192.png", "sizes": "192x192", "type": "image/png"}],
    })
    resp.mimetype = "application/manifest+json"
    return resp


@popup_bp.route("/popup/api/unlock", methods=["POST"])
def unlock():
    now = time.time()
    _pin_failures[:] = [t for t in _pin_failures if now - t < 300]
    if len(_pin_failures) >= 5:
        return jsonify(error="Trop d'essais. Réessayez dans quelques minutes."), 429
    pin = str((request.get_json(silent=True) or {}).get("pin", ""))
    if not popup_store.PIN or not secrets.compare_digest(pin, popup_store.PIN):
        _pin_failures.append(now)
        return jsonify(error="Code incorrect."), 403
    for t, exp in list(_tickets.items()):
        if exp < now:
            del _tickets[t]
    ticket = secrets.token_urlsafe(16)
    _tickets[ticket] = now + TICKET_TTL
    return jsonify(ticket=ticket)


@popup_bp.route("/popup/api/register", methods=["POST"])
def register():
    form = request.get_json(silent=True) or {}
    ticket = form.get("ticket", "")
    if _tickets.get(ticket, 0) < time.time():
        return jsonify(error="Session expirée. Demandez au vendeur de relancer la borne.",
                       expired=True), 403
    data, errors = popup_store.validate(form)
    if errors:
        return jsonify(errors=errors), 400
    participant, error = popup_store.register(data)
    if error:
        return jsonify(error=error), 409
    _tickets.pop(ticket, None)
    return jsonify(token=participant["token"], first_name=participant["first_name"])


@popup_bp.route("/popup/api/draw", methods=["POST"])
def draw():
    token = (request.get_json(silent=True) or {}).get("token", "")
    participant = popup_store.get_participant(token)
    if not participant:
        return jsonify(error="Participation introuvable."), 404
    participant, error = popup_store.draw(participant)
    if error:
        return jsonify(error=error), 409
    return jsonify(amount=participant["amount"], code=participant["code"],
                   email=participant["email"], first_name=participant["first_name"])


# ── Tableau de bord ───────────────────────────────────────────────────────────

@popup_bp.route("/popup/admin")
def admin():
    if not _admin_ok():
        return "Accès réservé.", 403
    tiers, participants = popup_store.stats()
    real = [p for p in participants if not p["is_test"]]
    resp = make_response(render_template(
        "popup_admin.html", tiers=tiers, participants=participants,
        n_real=len(real), n_drawn=sum(1 for p in real if p["code"]),
        n_optin=sum(1 for p in real if p["optin_email"]),
        distributed=sum(p["amount"] or 0 for p in real),
        planned=popup_store.tier_counts(500), percents=dict(popup_store.TIERS),
        msg=request.args.get("msg")))
    if request.args.get("key"):
        resp.set_cookie("popup_admin", request.args["key"], max_age=60 * 60 * 24 * 60,
                        httponly=True, secure=request.is_secure, samesite="Lax")
    return resp


@popup_bp.route("/popup/admin/generate", methods=["POST"])
def admin_generate():
    if not _admin_ok():
        return "Accès réservé.", 403
    try:
        total = int(request.form.get("total", "0"))
        if not 1 <= total <= 5000:
            raise ValueError("Indiquez un nombre entre 1 et 5000.")
        created = popup_store.generate_codes(total)
        msg = "Codes créés : " + ", ".join(f"{n} × {a} €" for a, n in created.items())
    except Exception as e:
        msg = f"Erreur : {e}"
    return redirect(url_for("popup.admin", msg=msg))


@popup_bp.route("/popup/admin/resend/<int:pid>", methods=["POST"])
def admin_resend(pid):
    if not _admin_ok():
        return "Accès réservé.", 403
    popup_store.send_code_email(pid)
    p = popup_store.get_participant_by_id(pid)
    msg = (f"E-mail renvoyé à {p['email']}" if p["email_status"] == "sent"
           else f"Échec de l'envoi : {p['email_error']}")
    return redirect(url_for("popup.admin", msg=msg))


@popup_bp.route("/popup/admin/resync/<int:pid>", methods=["POST"])
def admin_resync(pid):
    if not _admin_ok():
        return "Accès réservé.", 403
    popup_store.sync_klaviyo(pid)
    popup_store.klaviyo_track_draw(pid)
    p = popup_store.get_participant_by_id(pid)
    msg = (f"{p['email']} synchronisé dans Klaviyo" if p["klaviyo_status"] == "synced"
           else f"Échec Klaviyo : {p['klaviyo_error']}")
    return redirect(url_for("popup.admin", msg=msg))


@popup_bp.route("/popup/admin/export.csv")
def admin_export():
    if not _admin_ok():
        return "Accès réservé.", 403
    _, participants = popup_store.stats()
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Date", "Prénom", "Nom", "E-mail", "Téléphone", "Newsletter", "SMS",
                "Montant", "Code", "E-mail envoyé", "Klaviyo", "Test"])
    for p in participants:
        w.writerow([p["created_at"], p["first_name"], p["last_name"], p["email"], p["phone"],
                    "oui" if p["optin_email"] else "non", "oui" if p["optin_sms"] else "non",
                    p["amount"] or "", p["code"] or "", p["email_status"], p["klaviyo_status"],
                    "oui" if p["is_test"] else ""])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=popup-participants.csv"})
