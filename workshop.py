"""Requests to a workshop: UPS label for a shipment, or a credit note on photos.

Samuel picks the pairs (they may be at the office or at SSL, so nothing is
automatic here). For a shipment the assistant computes the cartons and
weight the workshop needs for its prepaid UPS label; for a credit note
without return it simply lists the defects. Drafts are in each workshop's
language, with the photos attached, and leave only once Samuel has read them.
"""
import math

WORKSHOPS = {
    "penha": {"name": "Penha", "emails": ["emilia@penha.pt", "estela@penha.pt"], "lang": "fr"},
    "gomes": {"name": "Gomes e Leite", "emails": ["geral@gomeseleite.pt", "elisabete@gomeseleite.pt"], "lang": "fr"},
    # Belts, Florence; Samuel writes to Nicolò Greco in English.
    "santa": {"name": "Santa Trinita", "emails": ["commerciale.santatrinita@gmail.com"], "lang": "en"},
}

# Shoe boxes: low 33 x 20 x 12 cm, boots 33 x 22 x 12 cm.
# Large carton: 2 boxes side by side, 3 layers, any mix.
# Small carton: 6 low pairs only (Samuel's measure).
LARGE = {"label": "48 × 34 × 38 cm", "capacity": 6}
SMALL = {"label": "38 × 38 × 38 cm", "capacity": 6}


def pack(cases):
    """Cartons for the selected pairs: boots first in large cartons, low pairs fill the gaps.

    Belts and accessories travel as a low box. Each carton's weight is the sum
    of the declared pair weights, rounded up to the kilo for the UPS label.
    """
    boots = [c for c in cases if c.get("parcel_type") == "boots"]
    low = [c for c in cases if c.get("parcel_type") != "boots"]
    cartons = []
    while boots:
        content = boots[:LARGE["capacity"]]
        boots = boots[LARGE["capacity"]:]
        room = LARGE["capacity"] - len(content)
        content += low[:room]
        low = low[room:]
        cartons.append((LARGE, content))
    while low:
        cartons.append((SMALL, low[:SMALL["capacity"]]))
        low = low[SMALL["capacity"]:]

    result = []
    for fmt, content in cartons:
        n_boots = sum(1 for c in content if c.get("parcel_type") == "boots")
        weight = sum(float(c.get("weight_kg") or 1.5) for c in content)
        result.append({
            "format": fmt["label"],
            "pairs": len(content),
            "boots": n_boots,
            "low": len(content) - n_boots,
            "weight_kg": math.ceil(weight),
        })
    return result


TEXT = {
    "fr": {
        "low": ("paire basse", "paires basses"), "boots": ("paire de boots", "paires de boots"), "and": "et",
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
        "details": "Détail :", "hello": "Bonjour,", "end": ["Merci beaucoup,", "", "Samuel Sfez", "Max Sauveur"],
    },
    "en": {
        "low": ("low pair", "low pairs"), "boots": ("pair of boots", "pairs of boots"), "and": "and",
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
        "details": "Details:", "hello": "Hello,", "end": ["Thanks,", "", "Samuel"],
    },
}


def _carton_line(t, i, carton):
    parts = []
    for kind in ("low", "boots"):
        if carton[kind]:
            parts.append(f"{carton[kind]} {t[kind][carton[kind] > 1]}")
    return t["carton"].format(i=i, fmt=carton["format"], kg=carton["weight_kg"], content=f" {t['and']} ".join(parts))


def draft_email(workshop_key, cases, photo_counts, mode="label"):
    """mode "label": the items go back, ask for a UPS label with the cartons.
    mode "credit": nothing is shipped, ask for a credit note on the photos."""
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


def _numbered(items):
    return [f"  {item}" for item in items]
