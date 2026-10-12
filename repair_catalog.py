"""Parcel type and declared weight for a repair, from the product name.

Weights are Samuel's rule for one pair in its box: 1,5 kg for low shoes, 2 kg
for boots. Model names come from the site catalogue (product_type "Boots" on
Shopify); Cody is a mountain derby but ships in a boots box.
"""
import re

BOOTS_MODELS = [
    "Chelsea Chapman", "Cody", "Dakota", "Logger Boots", "Miller",
    "Plain Toe", "Powell", "Springsteen", "Wayne", "Wico",
]

PARCEL_TYPES = {
    "basse": {"label": "Paire basse", "weight": 1.5},
    "boots": {"label": "Paire boots", "weight": 2.0},
    "accessoire": {"label": "Ceinture / accessoire", "weight": 0.5},
}


# Maker and maker's model reference, from the 2026 order forms (BDC 2026) and
# Samuel's answers of 12/10/2026. An empty reference means the model comes
# from an older order form. Dakota (Darksea) goes to a cobbler for now.
WORKSHOP_BY_MODEL = {
    "paul": ("Penha", "13465"),
    "brogues": ("Penha", "13806"),
    "deck": ("Penha", "14114"),
    "peter a.": ("Penha", "14811"),
    "wayne": ("Penha", "14678-G"),
    "springsteen": ("Penha", "14678-G"),
    "chelsea chapman": ("Penha", "14990A-1"),
    "le moc jagger": ("Penha", "14931 B"),
    "wood": ("Penha", "13446"),
    "blucher": ("Penha", "13705-G"),
    "cody": ("Penha", ""),
    "logger boots": ("Penha", ""),
    "powell": ("Penha", ""),
    "penny loafer": ("Penha", ""),
    "miller": ("Arturo Vintage", ""),
    "plain toe": ("Arturo Vintage", ""),
    "wico": ("Gomes e Leite", "4305"),
    "le boat": ("Gomes e Leite", "4376"),
    "dakota": ("Cordonnier", ""),
}


def workshop_for(product_name):
    """(workshop, reference) for a product name, or ("", "") when unknown."""
    model = model_name(product_name).lower()
    if model in WORKSHOP_BY_MODEL:
        return WORKSHOP_BY_MODEL[model]
    if re.search(r"\bsandales?\b", product_name or "", re.I):
        return ("Gomes e Leite", "4311")
    return ("", "")


def model_name(product_name):
    """'Paul - Chaussures Mocassin Cuir Noir - 42' -> 'Paul'."""
    return (product_name or "").split(" - ")[0].strip()


def parcel_type(product_name):
    name = product_name or ""
    model = model_name(name).lower()
    if any(model == m.lower() for m in BOOTS_MODELS) or re.search(r"\bboots?\b", name, re.I):
        return "boots"
    if re.search(r"ceinture|porte.carte|chaussette|embauchoir|cirage|brosse|kit ", name, re.I):
        return "accessoire"
    return "basse"
