"""
routes_madmax.py — Page de tirage de la carte cadeau Mad Max, et son tableau de bord
"""

import csv
import io
import secrets

from flask import Blueprint, Response, jsonify, make_response, redirect, render_template, request, url_for

import madmax

madmax_bp = Blueprint("madmax", __name__)


def _admin_ok():
    key = request.args.get("key") or request.cookies.get("madmax_admin")
    return bool(madmax.admin_key()) and secrets.compare_digest(key or "", madmax.admin_key())


def _page_ctx():
    return dict(starts_label=madmax.STARTS_LABEL, ends_label=madmax.ENDS_LABEL,
                conditions=madmax.CONDITIONS, rules_url=madmax.RULES_URL)


# ── Page client ───────────────────────────────────────────────────────────────

@madmax_bp.route("/cadeau/conditions")
def conditions():
    return render_template("madmax_reglement.html", counts=madmax.tier_counts(500), keep_min=madmax.CARD_KEEP_MIN,
                           min_purchase=madmax.MIN_PURCHASE, **_page_ctx())


@madmax_bp.route("/cadeau/<token>")
def gift_page(token):
    o = madmax.get_order(token)
    if not o:
        return render_template("madmax.html", invalid=True, o=None, **_page_ctx()), 404
    return render_template("madmax.html", invalid=False, o=o, **_page_ctx())


@madmax_bp.route("/cadeau/<token>/stop", methods=["GET", "POST"])
def gift_stop(token):
    # La page demande une confirmation : les antivirus de messagerie ouvrent
    # parfois les liens tout seuls, un simple clic ne doit rien désinscrire.
    o = madmax.get_order(token)
    if not o:
        return render_template("madmax_stop.html", o=None), 404
    done = bool(o["reminders_off"])
    if request.method == "POST" and not done:
        madmax._update(o["id"], reminders_off=1)
        done = True
    return render_template("madmax_stop.html", o=o, done=done)


@madmax_bp.route("/cadeau/<token>/tirage", methods=["POST"])
def gift_draw(token):
    o = madmax.get_order(token)
    if not o:
        return jsonify(error="Ce lien n'est pas valide."), 404
    o, error = madmax.draw(o)
    if error:
        return jsonify(error=error), 409
    return jsonify(amount=o["amount"], code=o["code"], email=o["email"])


# ── Tableau de bord ───────────────────────────────────────────────────────────

def _back(msg):
    return redirect(url_for("madmax.admin", msg=msg))


@madmax_bp.route("/madmax/admin")
def admin():
    if not _admin_ok():
        return "Accès réservé.", 403
    tiers, orders, reminders = madmax.stats()
    real = [o for o in orders if not o["is_test"]]
    eligible = [o for o in real if o["status"] == "ok"]
    to_check = [o for o in real if o["review"] == "to_check"]
    resp = make_response(render_template(
        "madmax_admin.html", tiers=tiers, orders=orders, reminders=reminders, to_check=to_check,
        keep_min=madmax.CARD_KEEP_MIN,
        n_orders=len(eligible), n_links=sum(o["link_status"] == "sent" for o in real),
        n_drawn=sum(bool(o["code"]) for o in real), n_used=sum(o["used"] for o in real),
        drawn_value=sum(o["amount"] or 0 for o in real),
        used_value=sum(o["amount"] or 0 for o in real if o["used"]),
        planned=madmax.tier_counts(500), percents=dict(madmax.TIERS),
        sending=madmax.sending_enabled(), months=madmax.REMINDER_MONTHS,
        links_from=madmax.LINKS_FROM, match=madmax.DISCOUNT_MATCH,
        msg=request.args.get("msg")))
    if request.args.get("key"):
        resp.set_cookie("madmax_admin", request.args["key"], max_age=60 * 60 * 24 * 90,
                        httponly=True, secure=request.is_secure, samesite="Lax")
    return resp


@madmax_bp.route("/madmax/admin/generate", methods=["POST"])
def admin_generate():
    if not _admin_ok():
        return "Accès réservé.", 403
    try:
        total = int(request.form.get("total", "0"))
        if not 1 <= total <= 5000:
            raise ValueError("Indiquez un nombre entre 1 et 5000.")
        created = madmax.generate_codes(total)
        return _back("Codes créés : " + ", ".join(f"{n} × {a} €" for a, n in created.items()))
    except Exception as e:
        return _back(f"Erreur : {e}")


@madmax_bp.route("/madmax/admin/sync", methods=["POST"])
def admin_sync():
    if not _admin_ok():
        return "Accès réservé.", 403
    try:
        _, added, removed = madmax.sync_collection()
        return _back(f"Collection à jour : {added} produit(s) ajouté(s), {removed} retiré(s).")
    except Exception as e:
        return _back(f"Erreur : {e}")


@madmax_bp.route("/madmax/admin/review/<int:oid>", methods=["POST"])
def admin_review(oid):
    if not _admin_ok():
        return "Accès réservé.", 403
    try:
        if request.form.get("decision") == "cancel":
            return _back(madmax.cancel_card(oid))
        madmax.keep_card(oid)
        return _back("Carte conservée.")
    except Exception as e:
        return _back(f"Erreur : {e}")


@madmax_bp.route("/madmax/admin/import", methods=["POST"])
def admin_import():
    if not _admin_ok():
        return "Accès réservé.", 403
    try:
        return _back(f"{madmax.import_orders()} nouvelle(s) commande(s) importée(s). Aucun e-mail envoyé.")
    except Exception as e:
        return _back(f"Erreur : {e}")


@madmax_bp.route("/madmax/admin/test", methods=["POST"])
def admin_test():
    if not _admin_ok():
        return "Accès réservé.", 403
    try:
        o = madmax.create_test_order(request.form.get("email", ""), request.form.get("first_name", ""))
        return _back(f"Lien de test envoyé à {o['email']}" if o["link_status"] == "sent"
                     else f"Échec de l'envoi : {o['link_error']}")
    except Exception as e:
        return _back(f"Erreur : {e}")


@madmax_bp.route("/madmax/admin/resend/<kind>/<int:oid>", methods=["POST"])
def admin_resend(kind, oid):
    if not _admin_ok():
        return "Accès réservé.", 403
    o = madmax.get_order_by_id(oid)
    if kind == "link":
        madmax.send_link_email(oid)
        o = madmax.get_order_by_id(oid)
        return _back(f"Lien renvoyé à {o['email']}" if o["link_status"] == "sent" else f"Échec : {o['link_error']}")
    if kind == "code" and o["code"]:
        madmax.send_code_email(oid)
        o = madmax.get_order_by_id(oid)
        return _back(f"Code renvoyé à {o['email']}" if o["code_status"] == "sent" else f"Échec : {o['code_error']}")
    if kind == "reminder" and o["is_test"]:
        # Uniquement pour les tests : montre la relance du mois choisi dans une vraie boîte mail.
        month = request.form.get("month", madmax.REMINDER_MONTHS[0])
        madmax._send(o, madmax.build_email("reminder_code" if o["code"] else "reminder_link", o, month))
        return _back(f"Relance de test ({month}) envoyée à {o['email']}")
    return _back("Action impossible.")


@madmax_bp.route("/madmax/admin/apercu/<kind>")
def admin_preview(kind):
    if not _admin_ok():
        return "Accès réservé.", 403
    sample = {"id": 0, "first_name": "Camille", "email": "camille@exemple.fr", "order_name": "#14012",
              "token": "exemple", "amount": 40, "code": "MADMAX-K7Q4ZP"}
    month = request.args.get("mois", madmax.REMINDER_MONTHS[0])
    try:
        subject, body = madmax.preview(kind, sample, month)
    except ValueError:
        return "Aperçu inconnu.", 404
    banner = (f'<div style="font:14px Helvetica,Arial;padding:12px 18px;background:#1f2021;color:#fff">'
              f'Objet : <b>{subject}</b></div>')
    return banner + body


@madmax_bp.route("/madmax/admin/export.csv")
def admin_export():
    if not _admin_ok():
        return "Accès réservé.", 403
    _, orders, reminders = madmax.stats()
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Commande", "Date", "Prénom", "E-mail", "Statut", "Lien", "Montant", "Code",
                "E-mail du code", "Utilisé", "Relances", "Test"])
    for o in orders:
        w.writerow([o["order_name"], o["ordered_at"], o["first_name"], o["email"], o["status"], o["link_status"],
                    o["amount"] or "", o["code"] or "", o["code_status"], "oui" if o["used"] else "non",
                    " ".join(r["month"] for r in reminders.get(o["id"], [])), "oui" if o["is_test"] else ""])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=madmax-cartes-cadeaux.csv"})
