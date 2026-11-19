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
import retours

PENDING = re.compile(r"avoir à valider : ([\d\s.,]+) € · commande #(\d+)(?: · modèle ([^)]+))?", re.I)

READ = """Samuel, le fondateur de Max Sauveur, écrit une consigne à l'assistant de son service client, à propos d'un client.
Commandes du client : {orders}
Proposition d'avoir faite à Samuel et en attente de sa validation : {pending}
Consigne de Samuel :
---
{question}
---
Samuel DEMANDE-t-il de créer maintenant un avoir (un code de réduction en euros) pour ce client ? Une question (« peut-on lui faire un avoir ? »), une hypothèse, un avis ou une simple mention n'est pas une demande.
Réponds uniquement avec ce JSON :
{{"confirm": true s'il valide la proposition en attente telle quelle (« oui », « ok », « vas-y », « valide »), sinon false,
 "create": true ou false,
 "amount": montant de l'avoir en euros, ou null,
 "order": "numéro de la commande concernée (chiffres seulement), ou null",
 "model": "modèle de l'article concerné s'il le cite, ou null",
 "model_only": true si l'avoir doit être limité à ce modèle, sinon false,
 "months": durée de validité en mois s'il la précise, sinon null,
 "free_shipping": true s'il offre aussi la livraison de la prochaine commande, sinon false}}"""


def _read(question, orders, pending=None):
    import claude_ai
    listing = "; ".join(f"{o.get('number')} ({', '.join(p.get('name', '') for p in o.get('products', []))})"
                        for o in orders or []) or "aucune"
    waiting = (f"{pending['amount']} € sur la commande #{pending['order']}" if pending else "aucune")
    text = claude_ai._call_haiku([{"role": "user", "content": READ.format(orders=listing, question=question[:2000],
                                                                          pending=waiting)}], max_tokens=250)
    start = text.find("{")
    return json.loads(text[start:text.rfind("}") + 1]) if start >= 0 else {}


def _pending(previous):
    """The amount proposed in the last answer of the discussion, still waiting for Samuel's « oui »."""
    last = (previous or [{}])[-1].get("claude_answer") or "" if previous else ""
    found = PENDING.search(last)
    if not found:
        return None
    amount = float(found.group(1).replace(" ", "").replace("\u202f", "").replace(",", "."))
    return {"amount": amount, "order": found.group(2), "model": (found.group(3) or "").strip() or None}


def _proposal(number, model):
    """What the customer paid for his order (or for the model named), shipping excluded, line by line."""
    order = retours.fetch_order(number)
    if not order:
        raise RuntimeError(f"Commande #{number} introuvable dans Shopify")
    lines = order["lines"]
    if model:
        wanted = [w for w in avoirs._norm(model).split() if len(w) > 2]
        picked = [l for l in lines if wanted and all(w in avoirs._norm(f"{l['title']} {l['variant']}") for w in wanted)]
        lines = picked or lines
    amount = round(sum(l["paid"] * l["quantity"] for l in lines), 2)
    detail = ", ".join(f"{l['title']}" + (f" {l['variant']}" if l["variant"] else "")
                       + f" : {retours.euros(l['paid'] * l['quantity'])}" for l in lines)
    return amount, detail


def maybe_create(question, orders, previous=None):
    """(context block for Claude, note for Samuel, waiting): waiting is True when Samuel must first validate an amount."""
    pending = _pending(previous)
    if not pending and not re.search(r"\bavoir|\bcode\b", question or "", re.I):
        return "", None, False
    info = _read(question, orders, pending)
    try:
        amount = float(info.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if pending and info.get("confirm") and amount <= 0:
        amount, info["order"], info["model"] = pending["amount"], pending["order"], pending["model"]
        info["create"] = True
    if not info.get("create"):
        return "", None, False
    number = re.sub(r"\D", "", str(info.get("order") or "")) or \
        next((str(o.get("number") or "").lstrip("#") for o in orders or [] if o.get("number")), "")
    if not number:
        return "", "Avoir non créé : je ne trouve aucune commande Shopify pour ce client.", True
    if amount <= 0:
        # No amount given: propose what he paid, and wait for Samuel's « oui » (or another amount).
        proposed, detail = _proposal(number, info.get("model"))
        model = f" · modèle {info['model']}" if info.get("model") else ""
        return "", (f"Avant de créer l'avoir, je te propose {retours.euros(proposed)} : le montant payé pour la commande "
                    f"#{number}, frais de port non compris ({detail}). Je le crée ? Réponds « oui », ou donne-moi un "
                    f"autre montant. (avoir à valider : {retours.euros(proposed)} · commande #{number}{model})"), True
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
    return block, note, False
