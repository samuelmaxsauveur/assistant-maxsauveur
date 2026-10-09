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
