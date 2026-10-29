"""Repair files: statuses, Google Sheet mirror, Wing tracking.

The assistant's database is the source of truth. Every change is pushed to
the "Réparations auto" tab of the "Suivis - SAV" sheet through a small Apps
Script web app (REPAIR_SHEET_URL, guarded by REPAIR_SHEET_SECRET), so Samuel
keeps his overview in the sheet without filling it by hand.
"""
import os
from datetime import datetime

import requests

import database
import gmail as gmail_helper
import wing_repair

# Samuel's own steps, in order. The keys are stored; the labels are shown.
STATUSES = [
    ("awaiting_customer", "Paire en attente de réception de la part du client"),
    ("received", "Paire réceptionnée"),
    ("label_requested", "Étiquette demandée à l'atelier"),
    ("credit_requested", "Avoir demandé à l'atelier (sans renvoi)"),
    ("sent_workshop", "Paire expédiée à l'atelier"),
    ("in_repair", "Paire en cours de réparation"),
    ("returning", "Paire en cours de retour de l'atelier"),
    ("back_logistics", "Paire réceptionnée au centre logistique"),
    ("to_send", "Paire à envoyer"),
    ("sent", "Paire envoyée"),
    ("delivered_customer", "Paire reçue par le client"),
    ("credit_received", "Avoir reçu"),
    ("cancelled", "Annulé"),
]
STATUS_LABELS = dict(STATUSES)

# The tabs of the Réparations page, in the order a pair goes through them.
STAGES = [
    ("attente", "En attente du client", ("awaiting_customer",)),
    ("a-traiter", "À traiter", ("received",)),
    ("etiquette", "Demande envoyée", ("label_requested", "credit_requested")),
    ("atelier", "En atelier", ("sent_workshop", "in_repair", "returning")),
    ("retour", "De retour", ("back_logistics",)),
    ("client", "Vers le client", ("to_send", "sent")),
    ("clos", "Clos", ("delivered_customer", "credit_received", "cancelled")),
]
STAGE_OF = {status: key for key, _, statuses in STAGES for status in statuses}

DECISIONS = {"": "", "repair": "Paire à réparer", "credit": "Avoir demandé à l'atelier"}

# Wing status of the label the customer ships the pair with.
PARCEL_LABELS = {
    "CREATED": "Étiquette pas encore utilisée",
    "DISPATCHED": "En route vers SSL",
    "SHIPPED": "En route vers SSL",
    "DELIVERED": "Livré chez SSL",
    "DELETED": "Étiquette annulée",
}

EDITABLE_FIELDS = ("status", "problem", "decision", "workshop", "workshop_ref", "notes")


def _date(iso):
    return datetime.fromisoformat(iso).strftime("%d/%m/%Y") if iso else ""


def sheet_row(case):
    """One line of the sheet, keyed by the column headers the Apps Script writes."""
    return {
        "Réf Wing": case["wing_ref"],
        "N° commande": "#" + str(case["order_number"]).lstrip("#"),
        "Client": case.get("customer_name") or "",
        "E-mail": case.get("customer_email") or "",
        "Paire": case.get("product") or "",
        "Poids (kg)": case.get("weight_kg") or "",
        "Statut": STATUS_LABELS.get(case["status"], case["status"]),
        "Mis à jour": _date(case.get("status_updated_at") or case["created_at"]),
        "Colis client": PARCEL_LABELS.get(case.get("parcel_status") or "", ""),
        "Suivi Colissimo": case.get("tracking_id") or "",
        "Étiquette": case.get("label_url") or "",
        "Problème": case.get("problem") or "",
        "Réparer / Avoir": DECISIONS.get(case.get("decision") or "", ""),
        "Atelier": case.get("workshop") or "",
        "Réf atelier": case.get("workshop_ref") or "",
        "Notes": case.get("notes") or "",
        "Créé le": _date(case["created_at"]),
    }


def push_to_sheet(case):
    """Mirror one file to the sheet. Never raises: the sheet is a view, not the record."""
    url, secret = os.getenv("REPAIR_SHEET_URL"), os.getenv("REPAIR_SHEET_SECRET")
    if not url or not secret:
        print("[Repairs] REPAIR_SHEET_URL/SECRET not set, sheet not updated")
        return False
    try:
        # Apps Script answers a POST with a redirect to the result page.
        resp = requests.post(url, json={"secret": secret, "row": sheet_row(case)},
                             timeout=30, allow_redirects=True)
        ok = resp.ok and '"ok":true' in resp.text.replace(" ", "")
        if not ok:
            print(f"[Repairs] Sheet refused {case['wing_ref']}: {resp.status_code} {resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"[Repairs] Sheet error for {case['wing_ref']}: {e}")
        return False


def customer_context(sender_email, orders=(), sender_name=""):
    """The customer's repair files, as a block for the reply prompt.

    A file is the customer's when it was opened under the address they write
    from, for one of their orders, or under their first and last names (a
    customer writing from a work address). Shopify and the shop's Wing account
    know nothing of a pair sent back for repair, so without this block a
    customer asking about their repair got a request for Samuel's help instead
    of a reply.
    """
    email = (sender_email or "").lower()
    numbers = {str(o.get("number", "")).lstrip("#") for o in orders or () if o} - {""}
    blocks = []
    for c in database.get_repair_cases():
        by_address = email and (c.get("customer_email") or "").lower() == email
        by_order = str(c["order_number"]).lstrip("#") in numbers
        if not (by_address or by_order
                or gmail_helper.same_person(c.get("customer_name"), sender_name, sender_email)):
            continue
        status = STATUS_LABELS.get(c["status"], c["status"])
        if c.get("status_updated_at"):
            status += f" (depuis le {_date(c['status_updated_at'])})"
        lines = [f"\n\n--- DOSSIER RÉPARATION {c['wing_ref']} (onglet Réparations, fait foi) ---",
                 f"Paire : {c.get('product') or '?'}",
                 f"Dossier ouvert le {_date(c['created_at'])}",
                 f"Statut : {status}"]
        if not (by_address or by_order):
            lines.append(f"Rattaché par le nom : dossier ouvert sous l'adresse {c.get('customer_email') or '?'}")
        if c.get("parcel_status"):
            lines.append(f"Colis du client : {PARCEL_LABELS.get(c['parcel_status'], c['parcel_status'])}")
        if c.get("problem"):
            lines.append(f"Problème : {c['problem']}")
        if c.get("decision"):
            lines.append(f"Décision : {DECISIONS.get(c['decision'], c['decision'])}")
        if c.get("workshop"):
            lines.append(f"Atelier : {c['workshop']}")
        if c.get("workshop_shipped_at"):
            lines.append(f"Envoyée à l'atelier le {_date(c['workshop_shipped_at'])}")
        if c.get("return_confirmed_at"):
            lines.append(f"Adresse de renvoi confirmée par le client le {_date(c['return_confirmed_at'])}")
        if c.get("notes"):
            lines.append(f"Notes de Samuel : {c['notes']}")
        blocks.append("\n".join(lines))
    return "".join(blocks)


def update_case(case_id, **fields):
    """Save the edits, stamp the status change, mirror to the sheet."""
    fields = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if "status" in fields and fields["status"] not in STATUS_LABELS:
        raise ValueError(f"Statut inconnu : {fields['status']}")
    if "decision" in fields and fields["decision"] not in DECISIONS:
        raise ValueError(f"Décision inconnue : {fields['decision']}")
    case = database.update_repair_case(case_id, fields)
    synced = push_to_sheet(case)
    return case, synced


def refresh_from_wing():
    """Scheduler job: move a pair to "réceptionnée" once its label is delivered to SSL.

    Only files still waiting for the customer are checked; everything after
    reception is Samuel's call.
    """
    waiting = [c for c in database.get_repair_cases() if c["status"] == "awaiting_customer"]
    if not waiting:
        return 0
    token = wing_repair._get_token()
    moved = 0
    for case in waiting:
        try:
            order = wing_repair._find_order(token, case["wing_ref"])
            parcels = [p for fo in (order or {}).get("fulfillmentOrders", []) for p in fo["parcels"]]
            parcel_status = parcels[-1]["status"] if parcels else "DELETED"
            fields = {}
            if parcel_status != case.get("parcel_status"):
                fields["parcel_status"] = parcel_status
            if parcel_status == "DELIVERED":
                fields["status"] = "received"
                moved += 1
            elif parcel_status == "DELETED":
                # Label voided in Wing (or order deleted): nothing will arrive.
                fields["status"] = "cancelled"
            if fields:
                updated = database.update_repair_case(case["id"], fields)
                push_to_sheet(updated)
                print(f"[Repairs] {case['wing_ref']}: {fields}")
        except Exception as e:
            print(f"[Repairs] Wing check failed for {case['wing_ref']}: {e}")
    return moved
