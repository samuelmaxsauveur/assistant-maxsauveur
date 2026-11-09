"""Requests to a workshop: UPS label for a shipment, a credit note on photos,
or a reminder for a credit note that never came.

Samuel picks the pairs (they may be at the office or at SSL, so nothing is
automatic here). For a shipment the assistant computes the cartons and
weight the workshop needs for its prepaid UPS label; for a credit note
without return it simply lists the defects. Drafts are in each workshop's
language, with the photos attached (not for a reminder), and leave only
once Samuel has read them.
"""
import math
import re
from datetime import datetime

WORKSHOPS = {
    "penha": {"name": "Penha", "emails": ["emilia@penha.pt", "estela@penha.pt"], "lang": "fr"},
    "gomes": {"name": "Gomes e Leite", "emails": ["geral@gomeseleite.pt", "elisabete@gomeseleite.pt"], "lang": "fr"},
    # Belts, Florence; Samuel writes to Nicolò Greco in English.
    "santa": {"name": "Santa Trinita", "emails": ["commerciale.santatrinita@gmail.com"], "lang": "en"},
}

# Shoe boxes, Samuel's measures: low 33 x 20 x 12 cm, boots 33 x 22 x 12 cm.
# Belts and accessories travel as a low box.
BOX_LENGTH, BOX_HEIGHT = 33, 12
BOX_WIDTH = {"low": 20, "boots": 22}
# Samuel's shipping carton: 2 boxes side by side, 3 layers, any mix.
FULL_CARTON = "48 × 34 × 38 cm"
CARTON_CAPACITY = 6
# A smaller load goes in a carton cut to its stacked boxes, 2 cm for the walls.
MARGIN = 2


def _carton_format(n, boots):
    """Outer size for n boxes: stacked up to three high, four as two stacks of two."""
    if n > 4:
        return FULL_CARTON
    width = BOX_WIDTH["boots" if boots else "low"]
    across, layers = (2, 2) if n == 4 else (1, n)
    return f"{BOX_LENGTH + MARGIN} × {width * across + MARGIN} × {BOX_HEIGHT * layers + MARGIN} cm"


def pack(cases):
    """Cartons for the selected pairs, each sized to the shoe boxes it holds.

    Boots go first so they share the full cartons. Six boxes fill Samuel's
    48 x 34 x 38 carton; a two-pair parcel no longer asks for a carton built
    for six. Each carton's weight is the sum of the declared pair weights,
    rounded up to the kilo for the UPS label.
    """
    ordered = sorted(cases, key=lambda c: c.get("parcel_type") != "boots")
    result = []
    for start in range(0, len(ordered), CARTON_CAPACITY):
        content = ordered[start:start + CARTON_CAPACITY]
        n_boots = sum(1 for c in content if c.get("parcel_type") == "boots")
        n_acc = sum(1 for c in content if c.get("parcel_type") == "accessoire")
        weight = sum(float(c.get("weight_kg") or 1.5) for c in content)
        result.append({
            "format": _carton_format(len(content), n_boots > 0),
            "pairs": len(content),
            "boots": n_boots,
            "accessory": n_acc,
            "low": len(content) - n_boots - n_acc,
            "weight_kg": math.ceil(weight),
        })
    return result


TEXT = {
    "fr": {
        "low": ("paire basse", "paires basses"), "boots": ("paire de boots", "paires de boots"),
        "accessory": ("ceinture ou accessoire", "ceintures ou accessoires"), "and": "et",
        "carton": "Carton {i} : {fmt}, {kg} kg ({content})",
        "repair": "Réparation : {pb}", "credit": "Demande d'avoir, paire défectueuse : {pb}",
        "no_problem": ("voir photos", "à préciser"),
        "ref": "Référence atelier : {ref}", "ref_missing": "à compléter",
        "ours": "Notre référence : {ref}", "photos": " (photos jointes : {name})",
        "label_subject": "Max Sauveur : demande d'étiquette UPS pour {n} paire{s}",
        "label_intro": "Nous avons {n} paire{s} à vous renvoyer. Pourriez-vous nous transmettre une étiquette UPS pour l'expédition ?",
        "parcel": "Colis ({c} carton{cs}, {kg} kg au total) :",
        "credit_subject": "Max Sauveur : demande d'avoir pour {n} article{s}",
        "credit_intro": "Nous avons constaté un défaut de fabrication sur {n} article{s}, photos jointes. Pourriez-vous nous établir un avoir ?",
        "reminder_subject": "Max Sauveur : relance pour l'avoir de {n} article{s}",
        "reminder_intro": ("Je me permets de vous relancer : nous n'avons pas encore reçu l'avoir pour l'article ci-dessous. Pourriez-vous nous l'envoyer ?",
                           "Je me permets de vous relancer : nous n'avons pas encore reçu l'avoir pour les {n} articles ci-dessous. Pourriez-vous nous l'envoyer ?"),
        "sent_on": "Article envoyé chez vous le {date}", "asked_on": "Avoir demandé le {date}",
        "see_request": "voir notre demande",
        "details": "Détail :", "hello": "Bonjour,", "end": ["Merci beaucoup,", "", "Samuel Sfez", "Max Sauveur"],
    },
    "en": {
        "low": ("low pair", "low pairs"), "boots": ("pair of boots", "pairs of boots"),
        "accessory": ("belt or accessory", "belts or accessories"), "and": "and",
        "carton": "Carton {i}: {fmt}, {kg} kg ({content})",
        "repair": "Repair: {pb}", "credit": "Credit note, defective item: {pb}",
        "no_problem": ("see photos", "to be specified"),
        "ref": "Your reference: {ref}", "ref_missing": "to be completed",
        "ours": "Our reference: {ref}", "photos": " (photos attached: {name})",
        "label_subject": "Max Sauveur: UPS label request for {n} item{s}",
        "label_intro": "We have {n} item{s} to send back to you. Could you please send us a UPS label for the shipment?",
        "parcel": "Parcel ({c} carton{cs}, {kg} kg in total):",
        "credit_subject": "Max Sauveur: credit note request for {n} item{s}",
        "credit_intro": "We found a manufacturing defect on {n} item{s}, photos attached. Could you please issue a credit note?",
        "reminder_subject": "Max Sauveur: reminder, credit note for {n} item{s}",
        "reminder_intro": ("Just a quick follow-up: we have not received the credit note for the item below yet. Could you please send it to us?",
                           "Just a quick follow-up: we have not received the credit note for the {n} items below yet. Could you please send it to us?"),
        "sent_on": "Item sent to you on {date}", "asked_on": "Credit note requested on {date}",
        "see_request": "see our request",
        "details": "Details:", "hello": "Hello,", "end": ["Thanks,", "", "Samuel"],
    },
}


def _carton_line(t, i, carton):
    parts = []
    for kind in ("low", "boots", "accessory"):
        if carton[kind]:
            parts.append(f"{carton[kind]} {t[kind][carton[kind] > 1]}")
    return t["carton"].format(i=i, fmt=carton["format"], kg=carton["weight_kg"], content=f" {t['and']} ".join(parts))


def draft_email(workshop_key, cases, photo_counts, mode="label"):
    """mode "label": the items go back, ask for a UPS label with the cartons.
    mode "credit": nothing is shipped, ask for a credit note on the photos.
    mode "reminder": chase a credit note already asked for."""
    if mode == "reminder":
        return draft_reminder(workshop_key, cases)
    workshop = WORKSHOPS[workshop_key]
    t = TEXT[workshop.get("lang", "fr")]
    n = len(cases)
    plural = "s" if n > 1 else ""
    lines = [t["hello"], ""]
    cartons = []
    if mode == "credit":
        lines += [t["credit_intro"].format(n=n, s=plural)]
        subject = t["credit_subject"].format(n=n, s=plural)
    else:
        cartons = pack(cases)
        lines += [
            t["label_intro"].format(n=n, s=plural), "",
            t["parcel"].format(c=len(cartons), cs="s" if len(cartons) > 1 else "", kg=sum(c["weight_kg"] for c in cartons)),
            *_numbered([_carton_line(t, i, c) for i, c in enumerate(cartons, 1)]),
        ]
        subject = t["label_subject"].format(n=n, s=plural)
    lines += ["", t["details"]]
    for i, case in enumerate(cases, 1):
        photos = photo_counts.get(case["id"], 0)
        problem = (case.get("problem") or "").strip() or t["no_problem"][0 if photos else 1]
        lines += [
            "",
            f"{i}. {case.get('product') or ''}",
            t["ref"].format(ref=(case.get("workshop_ref") or "").strip() or t["ref_missing"]),
            t["credit" if mode == "credit" or case.get("decision") == "credit" else "repair"].format(pb=problem),
            t["ours"].format(ref=case["wing_ref"]) + (t["photos"].format(name=case["wing_ref"].lstrip("#")) if photos else ""),
        ]
    lines += [""] + t["end"]
    return {"to": ", ".join(workshop["emails"]), "subject": subject, "body": "\n".join(lines), "cartons": cartons}


def _asked_on(case):
    """Date of the first request to the workshop, as stamped in the file's notes."""
    m = re.search(r"(\d{2}/\d{2}/\d{4}) : demande d.(?:avoir|étiquette)", case.get("notes") or "")
    return m.group(1) if m else ""


def draft_reminder(workshop_key, cases):
    """Chase the credit notes a workshop has not sent.

    The items were shipped to the workshop with a credit request, or shown on
    photos only; either way the credit note never came. No photos: the
    workshop already has them with the first request.
    """
    workshop = WORKSHOPS[workshop_key]
    t = TEXT[workshop.get("lang", "fr")]
    n = len(cases)
    lines = [t["hello"], "", t["reminder_intro"][n > 1].format(n=n), "", t["details"]]
    for i, case in enumerate(cases, 1):
        lines += [
            "",
            f"{i}. {case.get('product') or ''}",
            t["ref"].format(ref=(case.get("workshop_ref") or "").strip() or t["ref_missing"]),
            t["credit"].format(pb=(case.get("problem") or "").strip() or t["see_request"]),
            t["ours"].format(ref=case["wing_ref"]),
        ]
        if case.get("workshop_shipped_at"):
            shipped = datetime.fromisoformat(case["workshop_shipped_at"]).strftime("%d/%m/%Y")
            tracking = f" (UPS {case['workshop_tracking']})" if case.get("workshop_tracking") else ""
            lines.append(t["sent_on"].format(date=shipped) + tracking)
        elif _asked_on(case):
            lines.append(t["asked_on"].format(date=_asked_on(case)))
    lines += [""] + t["end"]
    return {"to": ", ".join(workshop["emails"]), "subject": t["reminder_subject"].format(n=n, s="s" if n > 1 else ""),
            "body": "\n".join(lines), "cartons": []}


def _numbered(items):
    return [f"  {item}" for item in items]
