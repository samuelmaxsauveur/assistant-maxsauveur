"""Credit codes created from the SAV discussion, when Samuel asks for one in his own words.

Samuel (19/11/2026): « je veux juste qu'il puisse créer un code avoir quand je lui
demande ». Haiku reads whether his message is an order (« crée-lui un avoir de
120 € ») and not a question (« peut-on lui faire un avoir ? »); the code is then
created with the avoirs module (customer only, one use, 6 months unless he says
otherwise, men's shoes and belts or one model) and handed to Claude for the mail.
An active credit of the same amount already on the order is reused, never doubled.
"""
import json
import re
from datetime import datetime, timezone

import avoirs

READ = """Samuel, le fondateur de Max Sauveur, écrit une consigne à l'assistant de son service client, à propos d'un client.
Commandes du client : {orders}
Consigne de Samuel :
---
{question}
---
Samuel DEMANDE-t-il de créer maintenant un avoir (un code de réduction en euros) pour ce client ? Une question (« peut-on lui faire un avoir ? »), une hypothèse, un avis ou une simple mention n'est pas une demande.
Réponds uniquement avec ce JSON :
{{"create": true ou false,
 "amount": montant de l'avoir en euros, ou null,
 "order": "numéro de la commande concernée (chiffres seulement), ou null",
 "model": "modèle de l'article concerné s'il le cite, ou null",
 "model_only": true si l'avoir doit être limité à ce modèle, sinon false,
 "months": durée de validité en mois s'il la précise, sinon null,
 "free_shipping": true s'il offre aussi la livraison de la prochaine commande, sinon false}}"""


def _read(question, orders):
    import claude_ai
    listing = "; ".join(f"{o.get('number')} ({', '.join(p.get('name', '') for p in o.get('products', []))})"
                        for o in orders or []) or "aucune"
    text = claude_ai._call_haiku([{"role": "user", "content": READ.format(orders=listing, question=question[:2000])}],
                                 max_tokens=250)
    start = text.find("{")
    return json.loads(text[start:text.rfind("}") + 1]) if start >= 0 else {}


def maybe_create(question, orders):
    """(context block for Claude, note for Samuel). Both empty when nothing was asked."""
    if not re.search(r"\bavoir|\bcode\b", question or "", re.I):
        return "", None
    info = _read(question, orders)
    try:
        amount = float(info.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if not info.get("create") or amount <= 0:
        return "", None
    number = re.sub(r"\D", "", str(info.get("order") or "")) or \
        next((str(o.get("number") or "").lstrip("#") for o in orders or [] if o.get("number")), "")
    if not number:
        return "", "Avoir non créé : je ne trouve aucune commande Shopify pour ce client."
    order = avoirs.get_order(number)
    items = order["lineItems"]["nodes"]
    model = info.get("model") or (items[0]["title"] if items else "")
    av = avoirs.prepare(number, model, amount=round(amount, 2), model_only=bool(info.get("model_only")))
    if isinstance(info.get("months"), int) and 0 < info["months"] <= 24:
        av["ends_at"] = avoirs._add_months(datetime.now(timezone.utc), info["months"])
    same = next((a for a in av["already"] if a["status"] == "ACTIVE" and abs(a["amount"] - av["amount"]) < 0.01), None)
    if same:
        code, ends, reused = same["code"], same["ends_at"] or av["ends_at"], True
    else:
        code, ends, reused = avoirs.create_code(av), av["ends_at"], False
        print(f"[SAV credit] {code} created for {av['order']} ({av['amount']} €) on Samuel's request")
    shipping = avoirs.create_shipping_code(av) if info.get("free_shipping") and not reused else None
    where = (f"uniquement sur le modèle {av['item']}" if av["model_only"]
             else "sur nos chaussures et ceintures homme")
    block = (f"\n\n--- AVOIR CRÉÉ DANS SHOPIFY À LA DEMANDE DE SAMUEL (à mettre dans le mail tel quel) ---\n"
             f"Code : {code}\nMontant : {avoirs._euros(av['amount'])}\n"
             f"Valable jusqu'au {ends.strftime('%d/%m/%Y')}, {where}, en une seule commande.\n"
             f"Réservé à l'adresse {av['email']} (il doit commander avec cette adresse), non cumulable avec d'autres codes ou promotions."
             + (f"\nCode livraison offerte, à saisir en plus dans le même panier : {shipping}" if shipping else "") + "\n")
    note = (f"Avoir {'déjà existant, repris' if reused else 'créé dans Shopify'} : {code}, {avoirs._euros(av['amount'])}, "
            f"commande {av['order']}, {where}, jusqu'au {ends.strftime('%d/%m/%Y')}"
            + (f", livraison offerte {shipping}" if shipping else "") + ".")
    return block, note
