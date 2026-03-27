from flask import Blueprint, request, jsonify
import gmail as gmail_helper
import database
import scheduler

validation = Blueprint('validation', __name__)

HTML_WRAPPER_START = """<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
body{font-family:-apple-system,sans-serif;max-width:600px;margin:80px auto;padding:20px;text-align:center;}
h1{font-size:48px;margin-bottom:16px;}
p{color:#555;font-size:18px;}
a{color:#2563eb;}
</style></head><body>"""

HTML_WRAPPER_END = "</body></html>"


def render_page(title_emoji, title_text, message):
    return f"""{HTML_WRAPPER_START}
<h1>{title_emoji}</h1>
<h2>{title_text}</h2>
<p>{message}</p>
{HTML_WRAPPER_END}"""


@validation.route('/validate/<token>')
def validate(token):
    draft = database.get_draft_by_token(token)

    if not draft:
        return render_page("⚠️", "Lien invalide ou déjà utilisé", "Ce lien de validation n'existe pas ou a déjà été utilisé."), 404

    if draft['status'] != 'pending':
        return render_page(
            "ℹ️",
            f"Déjà traité ({draft['status']})",
            "Cet email a déjà été traité et ne peut pas être envoyé à nouveau."
        ), 200

    service = gmail_helper.get_gmail_service()

    gmail_helper.send_email(
        service,
        draft['customer_email'],
        f"Re: {draft['subject']}",
        draft['draft_response'],
        draft['thread_id']
    )

    gmail_helper.mark_as_read(service, draft['email_id'])

    database.validate_draft(token)

    customer_name = draft.get('customer_name', draft['customer_email'])
    customer_email = draft['customer_email']

    return render_page(
        "✅",
        "Email envoyé",
        f"Email envoyé à {customer_name} ({customer_email})"
    ), 200


@validation.route('/reject/<token>')
def reject(token):
    draft = database.get_draft_by_token(token)

    if not draft:
        return render_page("⚠️", "Lien invalide", "Ce lien de rejet n'existe pas."), 404

    database.reject_draft(token)

    return render_page(
        "❌",
        "Draft rejeté",
        "L'email n'a pas été envoyé."
    ), 200


@validation.route('/dashboard')
def dashboard():
    drafts = database.get_all_drafts()

    rows = ""
    for draft in drafts:
        token = draft.get('token', '')
        status = draft.get('status', '')
        date = draft.get('created_at', '')
        client = draft.get('customer_name', draft.get('customer_email', ''))
        subject = draft.get('subject', '')
        intent = draft.get('intent', '')

        if status == 'pending':
            actions = f"""<a href="/validate/{token}" style="color:#16a34a;margin-right:12px;">Valider</a>
                          <a href="/reject/{token}" style="color:#dc2626;">Rejeter</a>"""
        else:
            actions = f"<span style='color:#888;'>{status}</span>"

        rows += f"""<tr>
            <td>{date}</td>
            <td>{client}</td>
            <td>{subject}</td>
            <td>{intent}</td>
            <td>{status}</td>
            <td>{actions}</td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
body{{font-family:-apple-system,sans-serif;max-width:1000px;margin:40px auto;padding:20px;}}
h1{{font-size:28px;margin-bottom:24px;}}
table{{width:100%;border-collapse:collapse;margin-top:16px;}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #e5e7eb;font-size:14px;}}
th{{background:#f9fafb;font-weight:600;color:#374151;}}
tr:hover{{background:#f9fafb;}}
.btn{{display:inline-block;margin-bottom:20px;padding:10px 20px;background:#2563eb;color:#fff;
      border:none;border-radius:6px;font-size:14px;cursor:pointer;text-decoration:none;}}
.btn:hover{{background:#1d4ed8;}}
</style></head><body>
<h1>📋 Dashboard — Emails en attente</h1>
<form method="POST" action="/trigger-check" style="display:inline;">
    <button type="submit" class="btn">🔄 Forcer vérification emails maintenant</button>
</form>
<table>
    <thead>
        <tr>
            <th>Date</th>
            <th>Client</th>
            <th>Sujet</th>
            <th>Intent</th>
            <th>Statut</th>
            <th>Actions</th>
        </tr>
    </thead>
    <tbody>
        {rows if rows else '<tr><td colspan="6" style="text-align:center;color:#888;padding:32px;">Aucun draft en attente</td></tr>'}
    </tbody>
</table>
</body></html>"""

    return html, 200


