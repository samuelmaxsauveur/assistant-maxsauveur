"""Grouped shipment of repair pairs to a workshop.

Samuel picks the pairs (they may be at the office or at SSL, so nothing is
automatic here). The assistant computes the cartons and weight the workshop
needs for its prepaid UPS label, drafts the request in French with the
customer's photos attached, and sends it once Samuel has read it.
"""
import math

WORKSHOPS = {
    "penha": {"name": "Penha", "emails": ["emilia@penha.pt", "estela@penha.pt"]},
    "gomes": {"name": "Gomes e Leite", "emails": ["geral@gomeseleite.pt", "elisabete@gomeseleite.pt"]},
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


def _carton_line(i, carton):
    parts = []
    if carton["low"]:
        parts.append(f"{carton['low']} paire{'s' if carton['low'] > 1 else ''} basse{'s' if carton['low'] > 1 else ''}")
    if carton["boots"]:
        parts.append(f"{carton['boots']} paire{'s' if carton['boots'] > 1 else ''} de boots")
    return f"Carton {i} : {carton['format']}, {carton['weight_kg']} kg ({' et '.join(parts)})"


def _request_line(case, photos):
    problem = (case.get("problem") or "").strip() or ("voir photos" if photos else "à préciser")
    if case.get("decision") == "credit":
        return f"Demande d'avoir, paire défectueuse : {problem}"
    return f"Réparation : {problem}"


def draft_email(workshop_key, cases, photo_counts):
    workshop = WORKSHOPS[workshop_key]
    cartons = pack(cases)
    total_weight = sum(c["weight_kg"] for c in cartons)
    n = len(cases)

    lines = [
        "Bonjour,",
        "",
        f"Nous avons {n} paire{'s' if n > 1 else ''} à vous renvoyer. "
        "Pourriez-vous nous transmettre une étiquette UPS pour l'expédition ?",
        "",
        f"Colis ({len(cartons)} carton{'s' if len(cartons) > 1 else ''}, {total_weight} kg au total) :",
        *_numbered([_carton_line(i, c) for i, c in enumerate(cartons, 1)]),
        "",
        "Détail des paires :",
    ]
    for i, case in enumerate(cases, 1):
        ref = (case.get("workshop_ref") or "").strip()
        photos = photo_counts.get(case["id"], 0)
        lines += [
            "",
            f"{i}. {case.get('product') or 'Paire'}",
            f"Référence atelier : {ref or 'à compléter'}",
            _request_line(case, photos),
            f"Notre référence : {case['wing_ref']}"
            + (f" (photo{'s' if photos > 1 else ''} jointe{'s' if photos > 1 else ''} : {case['wing_ref'].lstrip('#')})" if photos else ""),
        ]
    lines += ["", "Merci beaucoup,", "", "Samuel Sfez", "Max Sauveur"]

    subject = f"Max Sauveur : demande d'étiquette UPS pour {n} paire{'s' if n > 1 else ''}"
    return {
        "to": ", ".join(workshop["emails"]),
        "subject": subject,
        "body": "\n".join(lines),
        "cartons": cartons,
    }


def _numbered(items):
    return [f"  {item}" for item in items]
