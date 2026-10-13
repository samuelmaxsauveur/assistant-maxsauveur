"""Read the workshops' emails: UPS labels and credit notes go to the right files.

Both mailboxes are watched (samuel@ gets the workshops' replies, hello@
sometimes too). Workshop PDFs are scans, so Claude reads them: document
type, UPS tracking number, the pairs a credit note covers. A label goes to
the pairs waiting for one at that workshop; a credit note to the credit
files it names. Anything the assistant cannot place waits on the
Réparations page for Samuel to file it in one click.
"""
import base64
import json
import os
import re
from datetime import datetime

import anthropic

import database
import gmail as gmail_helper
import repair_catalog
import repair_docs
import repairs

# Sender domain, or full address for a workshop writing from a Gmail account.
WORKSHOP_DOMAINS = {"penha.pt": "Penha", "gomeseleite.pt": "Gomes e Leite",
                    "commerciale.santatrinita@gmail.com": "Santa Trinita"}
LABEL_NAME = re.compile(r"\b(ups|upa)\b.*label|label.*\b(ups|upa)\b", re.I)
CREDIT_NAME = re.compile(r"n\.?\s*cred|nota[ _-]d[ei][ _-]cr[eé]dito|credit|cr[ée]dit|avoir", re.I)
UPS_TRACKING = re.compile(r"\b1Z[0-9A-Z]{16}\b")

READ_PROMPT = """Ce PDF vient d'un atelier de chaussures qui travaille pour la marque Max Sauveur.
Réponds UNIQUEMENT avec ce JSON :
{"type": "label" | "credit" | "other",
 "tracking": "numéro de suivi UPS (commence par 1Z) ou null",
 "number": "numéro du document ou null",
 "total": "montant total avec la devise, ou null",
 "items": [{"designation": "texte de la ligne", "size": "pointure ou null", "quantity": 1}]}
"label" = étiquette d'expédition UPS. "credit" = avoir / nota de crédito / note de crédit.
items : seulement pour un avoir, une entrée par ligne d'article."""


def read_pdf(data):
    """What Claude reads in a workshop PDF (scans included)."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": READ_PROMPT},
        ]}])
    text = msg.content[0].text
    start = text.find("{")
    if start < 0:
        return {"type": "other"}
    # Multi-page labels can come back as one JSON per page: keep the first.
    info, _ = json.JSONDecoder().raw_decode(text[start:])
    # Scans get read with stray spaces inside the number.
    tracking = re.sub(r"\s", "", info.get("tracking") or "").upper()
    info["tracking"] = tracking if UPS_TRACKING.fullmatch(tracking) else None
    return info


def _workshop_of(sender):
    return next((name for domain, name in WORKSHOP_DOMAINS.items() if domain in (sender or "").lower()), None)


def _pdf_parts(service, msg):
    def walk(part):
        if (part.get("filename") or "").lower().endswith(".pdf") and part.get("body", {}).get("attachmentId"):
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=part["body"]["attachmentId"]).execute()
            yield part["filename"], base64.urlsafe_b64decode(att["data"])
        for sub in part.get("parts", []):
            yield from walk(sub)
    yield from walk(msg["payload"])


COLOUR_WORDS = {
    "noir": ("noir", "preto", "black", "nero"),
    "marron": ("marron", "castanho", "brown", "chocolat", "caffe", "moka"),
    "bordeaux": ("bordeaux", "bordo", "burgundy"),
    "cognac": ("cognac", "camel"),
}


def _match_credit(workshop, info):
    """Open credit files of this workshop that the credit note's lines point to.

    Credit notes name the model and leather, rarely the size: model and
    colour must both match, the size only decides between two pairs.
    """
    candidates = [c for c in database.get_repair_cases()
                  if c.get("decision") == "credit" and c.get("workshop") == workshop
                  and c["status"] not in ("credit_received", "cancelled")]
    matched = []
    for item in info.get("items") or []:
        text = (item.get("designation") or "").lower()
        hits = []
        for c in candidates:
            product = (c.get("product") or "").lower()
            model = repair_catalog.model_name(product).split(" ")[0]
            colours = [w for key, words in COLOUR_WORDS.items() if key in product for w in words]
            if model and model in text and (not colours or any(w in text for w in colours)):
                hits.append(c)
        size = str(item.get("size") or "").replace(",", ".")
        if size and len(hits) > 1:
            hits = [c for c in hits if (c.get("product") or "").replace(",", ".").rstrip().endswith(size)] or hits
        for c in hits[:int(item.get("quantity") or 1)]:
            if c not in matched:
                matched.append(c)
    return matched


def _note(case, line):
    notes = (case.get("notes") or "").strip()
    return f"{notes}\n{datetime.now().strftime('%d/%m/%Y')} : {line}".strip()


def _handle_pdf(workshop, filename, data, sender, subject, date):
    by_name = "label" if LABEL_NAME.search(filename) else "credit" if CREDIT_NAME.search(filename) else None
    awaiting_credit = any(c["status"] == "credit_requested" and c.get("workshop") == workshop
                          for c in database.get_repair_cases())
    if by_name is None and not awaiting_credit:
        return None  # invoices, receipts, packing lists: not repair business
    info = read_pdf(data)
    kind = info.get("type") if info.get("type") in ("label", "credit") else by_name
    if kind is None:
        return None  # a PDF read only because a credit note is awaited, and it is not one

    if kind == "label":
        waiting = [c for c in database.get_repair_cases()
                   if c["status"] == "label_requested" and c.get("workshop") == workshop]
        if waiting:
            tracking = info.get("tracking") or ""
            for c in waiting:
                repair_docs.save_to_case(c["id"], "label", filename, data)
                fields = {"notes": _note(c, f"étiquette UPS reçue de {workshop}" + (f" (suivi {tracking})" if tracking else ""))}
                if tracking:
                    fields["workshop_tracking"] = tracking
                database.update_repair_case(c["id"], fields)
                repairs.push_to_sheet(database.get_repair_case(c["id"]))
            return f"étiquette UPS {workshop} → {len(waiting)} paire(s)"
    else:
        matched = _match_credit(workshop, info)
        if matched:
            for c in matched:
                repair_docs.save_to_case(c["id"], "credit", filename, data)
                line = f"avoir {info.get('number') or filename} reçu de {workshop}" + (f", {info['total']}" if info.get("total") else "")
                repairs.update_case(c["id"], status="credit_received", notes=_note(c, line))
            return f"avoir {workshop} → {', '.join(c['wing_ref'] for c in matched)}"

    stored = repair_docs.save_to_inbox(filename, data)
    database.add_repair_inbox(kind, filename, stored, workshop, sender, subject, date)
    return f"{repair_docs.KINDS[kind]} {workshop} à classer"


def check_mailboxes(days=14):
    """Scheduler job: new workshop emails with PDFs, in both mailboxes."""
    accounts = [("hello", gmail_helper.get_gmail_service())]
    samuel = gmail_helper.get_samuel_service()
    if samuel:
        accounts.append(("samuel", samuel))
    domains = " OR ".join(WORKSHOP_DOMAINS)
    done = []
    for account, service in accounts:
        res = service.users().messages().list(
            userId="me", q=f"from:({domains}) has:attachment newer_than:{days}d", maxResults=30).execute()
        for ref in res.get("messages", []):
            key = f"{account}:{ref['id']}"
            if database.repair_mail_seen(key):
                continue
            msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            workshop = _workshop_of(headers.get("from"))
            try:
                for filename, data in _pdf_parts(service, msg):
                    result = _handle_pdf(workshop, filename, data, headers.get("from"),
                                         headers.get("subject"), headers.get("date"))
                    if result:
                        done.append(result)
                database.mark_repair_mail_seen(key)
            except Exception as e:
                print(f"[WorkshopMail] {key} failed, retried next run: {e}")
    if done:
        print(f"[WorkshopMail] {done}")
    return done
