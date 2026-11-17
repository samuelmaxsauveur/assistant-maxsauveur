"""What Shopify and Wing really say about a customer, gathered before Claude drafts a reply.

Samuel's complaint (14/11/2026): the drafts asked him "has the pair been shipped?",
"is size 42 in stock?" when the answer was one search away. This module does
those searches for every draft and hands Claude the facts:

  - every Shopify order of the customer, by email and by name (another address);
  - the credit codes quoted in the emails: still unused, or used on which order;
  - every Wing order tied to the customer, on both accounts (reshipments, repairs,
    returns, orders created by hand under his name), with their parcels;
  - the stock of every size of the models he ordered;
  - his files in the Retours and Erreurs pages.

Reads only. Each search fails on its own without stopping the others.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor

import avoirs
import database
import retours
import wing_repair

CODE_RE = re.compile(r"\b(MADMAX-[A-Z0-9]{6}|LIVRAISON[A-Z0-9]{4,10}|[A-Z0-9]{12})\b")
# Only numbers written as order numbers: postcodes like 13170 (SSL) sit in our own emails.
NUMBER_RE = re.compile(r"(?:#|commande\s*(?:n°|no|numéro|numero)?\s*:?\s*#?|order\s*#?)\s*(1\d{4})(?!\d)", re.I)
MAX_ORDERS = 6
MAX_CODES = 4
MAX_PRODUCTS = 3

STATUS_FR = {"DELIVERED": "livré", "SHIPPED": "expédié", "DISPATCHED": "en transit", "PENDING": "en attente",
             "DELETED": "annulé", "CANCELED": "annulé", "CANCELLED": "annulé", "TO_FULFILL": "à préparer",
             "FULFILLED": "préparé", "LABELLED": "étiquette créée", "READY": "prêt",
             "CREATED": "étiquette créée, pas encore déposé", "IN_TRANSIT": "en transit",
             "OUT_FOR_DELIVERY": "en cours de livraison", "RETURNED": "retourné à l'expéditeur",
             "EXCEPTION": "incident de livraison", "AVAILABLE_FOR_PICKUP": "disponible au relais"}


def _fr(status):
    return STATUS_FR.get((status or "").upper(), (status or "?").lower())


def _date(value):
    return retours.short_date((value or "")[:10]) if value else "?"


def _codes(texts):
    found = []
    for code in CODE_RE.findall(" ".join(texts)):
        if code.startswith(("MADMAX-", "LIVRAISON")) or (re.search(r"\d", code) and re.search(r"[A-Z]", code)):
            if code not in found:
                found.append(code)
    return found[:MAX_CODES]


def _code_line(code):
    node = avoirs._gql("""query($c: String!) { codeDiscountNodeByCode(code: $c) { codeDiscount {
        ... on DiscountCodeBasic { title status asyncUsageCount startsAt endsAt }
        ... on DiscountCodeFreeShipping { title status asyncUsageCount startsAt endsAt } } } }""", {"c": code})
    discount = ((node.get("codeDiscountNodeByCode") or {}).get("codeDiscount")) or {}
    if not discount:
        return f"{code} : introuvable dans Shopify."
    used = discount.get("asyncUsageCount") or 0
    line = (f"{code} ({discount.get('title') or 'code'}) : {str(discount.get('status') or '').lower()}, créé le "
            f"{_date(discount.get('startsAt'))}" + (f", fin le {_date(discount.get('endsAt'))}" if discount.get("endsAt") else "")
            + f", utilisé {used} fois")
    if used:
        orders = avoirs._gql('query($q: String!) { orders(first: 5, query: $q) { nodes { name createdAt } } }',
                             {"q": f"discount_code:{code}"})["orders"]["nodes"]
        line += " : " + (", ".join(f"commande {o['name']} du {_date(o['createdAt'])}" for o in orders) or "commande non retrouvée")
    else:
        line += " : aucune commande n'a été passée avec"
    return line + "."


def _wing_orders(numbers, names):
    """{ref: (account, order)} for every Wing order tied to these order numbers or full names
    (first and last name both on the order, so a namesake never comes up)."""
    found = {}
    for account in ("main", "rep"):
        try:
            token = wing_repair._get_token(account)
        except Exception as e:
            print(f"[Enquête] Wing {account} unavailable: {e}")
            continue
        for term in list(numbers) + [n.split()[-1] for n in names]:
            try:
                data = wing_repair._gql("""query($s: String!) { orders(input: { filter: { search: $s }, limit: 20 }) {
                    ref status orderedAt recipient { firstName lastName }
                    orderProducts { quantity product { designation } }
                    fulfillmentOrders { service carrierPickupName parcels { trackingId status } } } }""",
                                        {"s": term}, token)
            except Exception as e:
                print(f"[Enquête] Wing search '{term}' failed: {e}")
                continue
            for o in (data.get("data") or {}).get("orders") or []:
                ref = o.get("ref") or ""
                bare = ref.lstrip("#")
                names_of = avoirs._norm(f"{(o.get('recipient') or {}).get('firstName') or ''} {(o.get('recipient') or {}).get('lastName') or ''}")
                words = set(names_of.split()) | set(re.split(r"[_\W]+", avoirs._norm(bare)))
                ref_words = set(re.split(r"[_\W]+", avoirs._norm(bare)))
                tied = any(bare == n or bare.startswith(n + "_") for n in numbers) or \
                    any(all(w in words for w in name.split()) for name in names) or \
                    ("_" in bare and any(name.split()[-1] in ref_words for name in names))   # our NOM_CEINTURE, NOM_..._RETOUR
                # The repair account also mirrors Shopify orders, incompletely: only our own references count there.
                if not tied or (account == "rep" and "_" not in bare):
                    continue
                found.setdefault(ref, (account, o))
    return found


def _wing_line(ref, account, o):
    products = ", ".join(f"{p['product']['designation']}" + (f" × {p['quantity']}" if p["quantity"] > 1 else "")
                         for p in o.get("orderProducts") or [])
    parcels = [p for fo in o.get("fulfillmentOrders") or [] for p in fo.get("parcels") or []]
    relay = next((fo.get("carrierPickupName") for fo in o.get("fulfillmentOrders") or [] if fo.get("carrierPickupName")), None)
    colis = "; ".join(f"colis {p['trackingId'] or 'sans numéro'} {_fr(p['status'])}" for p in parcels) or "aucun colis créé"
    return (f"{ref} (compte {'principal' if account == 'main' else 'réparation'}) du {_date(o.get('orderedAt'))} : "
            f"{_fr(o.get('status'))} · {products or 'sans article'}" + (f" · relais {relay}" if relay else "") + f" · {colis}.")


def _stock_line(product_id):
    product = avoirs._gql("""query($id: ID!) { product(id: $id) { title variants(first: 60) {
        nodes { title inventoryQuantity } } } }""", {"id": product_id})["product"]
    sizes = ", ".join(f"{v['title']} : {v['inventoryQuantity']}" for v in product["variants"]["nodes"]
                      if v.get("inventoryQuantity") is not None)
    return f"{product['title']} : {sizes or 'stock inconnu'}."


OFFER_LABELS = {"bonus": "un avoir bonifié", "plain": "un avoir", "exchange": "un échange Mad Max gratuit"}


def _return_line(c):
    """A Retours file in plain facts: what came back, what we already wrote and to which address, his answer."""
    d = c["data"]
    order = d.get("order") or {}
    qty = {k: int(v) for k, v in (d.get("returned") or {}).items() if str(v).isdigit() and int(v) > 0}
    items = ", ".join(f"{l['title']}" + (f" {l['variant']}" if l.get("variant") else "")
                      for l in order.get("lines") or [] if l.get("id") in qty)
    parts = [retours.STATUS_LABELS.get(c["status"], c["status"])]
    if items:
        parts.append(f"article retourné : {items}")
    if d.get("arrival_date"):
        parts.append(f"colis arrivé chez SSL le {_date(d['arrival_date'])}")
    to = c.get("customer_email") or order.get("email") or "?"
    if d.get("offer_sent_at"):
        offer = OFFER_LABELS.get(d.get("offer_kind") or "bonus", "une proposition")
        amount = f" de {retours.euros(float(d['offer_amount']))}" if d.get("offer_amount") else ""
        parts.append(f"mail envoyé le {_date(d['offer_sent_at'])} à {to} (« Votre retour pour la commande "
                     f"{order.get('name') or '#' + str(c.get('order_number'))} ») : réception du colis confirmée et "
                     f"proposition d'{offer[3:] if offer.startswith('un ') else offer}{amount} au lieu du remboursement")
    elif d.get("last_sent_at"):
        parts.append(f"dernier mail envoyé le {_date(d['last_sent_at'])} à {to}")
    if d.get("reply_at"):
        choice = {"credit": "il choisit l'avoir", "refund": "il choisit le remboursement",
                  "exchange": "il accepte l'échange"}.get(d.get("reply_choice"), "réponse à lire")
        parts.append(f"réponse du client le {_date(d['reply_at'])} : {choice}"
                     + (f" ({d['reply_summary']})" if d.get("reply_summary") else ""))
    if c["status"] == "waiting_client" and d.get("offer_sent_at") and not d.get("reply_at"):
        parts.append("pas encore de réponse à cette proposition : s'il écrit maintenant qu'il veut le remboursement, "
                     "c'est son choix, on prend acte et on rembourse sans lui refaire la proposition")
    if d.get("credit_code"):
        parts.append(f"avoir créé : {d['credit_code']}" + (f" ({retours.euros(float(d['credit_amount']))})" if d.get("credit_amount") else ""))
    if c["status"] in ("to_refund", "waiting_client", "reply_to_read", "to_decide"):
        try:
            refund = retours.analyse(d)["amounts"].get("refund")
            if refund is not None:
                parts.append(f"remboursement prévu s'il est remboursé : {retours.euros(refund)}")
        except Exception as e:
            print(f"[Enquête] Return file {c['id']} amounts unavailable: {e}")
    return f"Dossier Retours n°{c['id']} (commande #{c.get('order_number')}) : " + " · ".join(parts) + "."


def _files(numbers, email):
    lines = []
    email = (email or "").lower()
    for c in database.get_client_returns():
        if c.get("order_number") in numbers or (email and (c.get("customer_email") or "").lower() == email):
            lines.append(_return_line(c))
    try:
        import erreurs
        for c in database.get_logistics_errors():
            if c.get("order_number") in numbers or (email and (c.get("customer_email") or "").lower() == email):
                sent = ", ".join(f"{erreurs.MAILS.get(k, k)} le {_date(v)}" for k, v in (c["data"].get("sent_mails") or {}).items()
                                 if v and v != "skipped")
                lines.append(f"Dossier Erreurs logistique n°{c['id']} (commande #{c.get('order_number')}, "
                             f"{erreurs.KINDS[c['data']['kind']]}) : {erreurs.STATUS_LABELS[c['status']]}"
                             + (f" · mails envoyés à {c.get('customer_email')} : {sent}" if sent else "") + ".")
    except Exception as e:
        print(f"[Enquête] Error files unavailable: {e}")
    return lines


TIERS_TEXT = ("paliers Mad Max, sur le montant des articles gardés avant remise : moins de 150 € : aucune remise ; "
              "de 150 à 449,99 € : 15 % ; de 450 à 799,99 € : 20 % ; 800 € et plus : 30 %")
RETURN_WORDS = re.compile(r"retour|renvo|rembours|échang|echang|garde", re.I)
MADMAX_READ = """Une commande Max Sauveur :
{lines}

Messages échangés avec le client (les siens et les nôtres) :
{texts}

Quels articles de cette commande le client veut-il renvoyer pour être remboursé, et lesquels garde-t-il ou échange-t-il contre une autre pointure ?
Réponds uniquement avec ce JSON :
{{"returned": [{{"line": numéro de ligne, "qty": quantité renvoyée}}], "exchanged": [numéros des lignes échangées contre une autre pointure]}}
Si ce n'est pas clair, réponds {{"returned": [], "exchanged": []}}."""


def _madmax_calc(o, texts):
    """The Mad Max discount of a refund, computed by the code from what the customer says he sends back."""
    import claude_ai
    lines = o["lines"]
    prompt = MADMAX_READ.format(
        lines="\n".join(f"{i}. {l['title']}" + (f" {l['variant']}" if l["variant"] else "") + f" × {l['quantity']}"
                        for i, l in enumerate(lines)),
        texts="\n\n---\n\n".join(t[:1500] for t in texts if t)[:5000])
    text = claude_ai._call_haiku([{"role": "user", "content": prompt}], max_tokens=200)
    start = text.find("{")
    found = json.loads(text[start:text.rfind("}") + 1]) if start >= 0 else {}
    qty = {}
    for r in found.get("returned") or []:
        i, n = r.get("line"), r.get("qty") or 1
        if isinstance(i, int) and 0 <= i < len(lines) and isinstance(n, int) and n > 0:
            qty[i] = min(n, lines[i]["quantity"])
    if not qty:
        return None
    returned = [dict(lines[i], qty=n) for i, n in qty.items()]
    kept = [dict(l, qty=l["quantity"] - qty.get(i, 0)) for i, l in enumerate(lines) if l["quantity"] - qty.get(i, 0) > 0]
    mm = retours.madmax_terms(o, returned, kept)
    if not mm:
        return None
    paid = round(sum(l["paid"] * l["qty"] for l in returned) + (o["shipping"] if not kept else 0), 2)
    refund = round(max(paid - mm["deduction"], 0), 2)
    back = ", ".join(f"{l['title']} {l['variant']}".strip() + (f" × {l['qty']}" if l["qty"] > 1 else "") for l in returned)
    tier = f"{mm['new_rate']} %" if mm["new_rate"] else "aucune remise"
    line = (f"CALCUL MAD MAX fait par le code pour #{o['number']} (seuls ces chiffres font foi : ceux de nos messages "
            f"précédents peuvent être faux ; reprends-les tels quels, ne refais aucun calcul). S'il renvoie {back} :\n"
            f"  · articles gardés avant remise : {retours.euros(mm['kept_base'])}, donc palier {tier} au lieu de {mm['rate']} %\n"
            f"  · remise à {tier} sur ces articles : {retours.euros(mm['new_discount'])}, soit {retours.euros(mm['kept_after'])} après remise\n"
            f"  · payé pour ces articles avec la remise de {mm['rate']} % : {retours.euros(mm['kept_paid'])}\n"
            f"  · différence à déduire du remboursement : {retours.euros(mm['deduction'])}\n"
            f"  · payé pour ce qu'il renvoie : {retours.euros(paid)}\n"
            f"  · remboursement : {retours.euros(paid)} - {retours.euros(mm['deduction'])} = {retours.euros(refund)}")
    if mm.get("card"):
        line += (" Sa carte cadeau Mad Max reste valable." if mm["card_kept_on_refund"]
                 else " Sa carte cadeau Mad Max est annulée (ce qu'il garde passe sous 150 €).")
    if found.get("exchanged"):
        line += " Un échange de pointure ne change pas la remise : l'article échangé compte dans ce qu'il garde."
    return line


def investigate(sender_email, customer_name="", orders=(), texts=()):
    """The ENQUÊTE block for Claude, or '' when nothing could be read."""
    texts = [t for t in texts if t]
    numbers, trusted = [], set()
    for o in orders or []:
        n = str(o.get("number") or "").lstrip("#")
        if n and n not in numbers:
            numbers.append(n)
            trusted.add(n)
    for n in NUMBER_RE.findall(" ".join(texts)):
        if n not in numbers:
            numbers.append(n)
    numbers = numbers[:MAX_ORDERS]
    sender = (sender_email or "").lower()
    own_words = [w for w in avoirs._norm(customer_name or "").split() if len(w) > 2 and "@" not in w]

    pool = ThreadPoolExecutor(max_workers=4)
    codes_job = pool.submit(lambda: [_code_line(c) for c in _codes(texts)])

    # Shopify: the orders we know, then the same name under another address.
    shopify, by_name, names = [], [], set()
    for n in list(numbers):
        try:
            o = retours.fetch_order(n)
        except Exception as e:
            print(f"[Enquête] Shopify order {n} unreadable: {e}")
            o = None
        if o and n not in trusted:
            # A number read in the emails must be this customer's order, never someone else's.
            mine = (o.get("email") or "").lower() == sender or \
                (own_words and all(w in retours._order_names(o) for w in own_words[-1:]))
            if not mine:
                numbers.remove(n)
                o = None
        if o:
            shopify.append(o)
            for full in (o.get("full_name"), o.get("shipping_name")):
                words = list(dict.fromkeys(w for w in avoirs._norm(full or "").split() if len(w) > 2))
                if len(words) >= 2:
                    names.add(" ".join(words))
    if not shopify and customer_name and "@" not in customer_name:
        # The display name can be a nickname ("Jean Jean"): only a real first and last name is searched.
        words = list(dict.fromkeys(w for w in avoirs._norm(customer_name).split() if len(w) > 2))
        if len(words) >= 2:
            names.add(" ".join(words))
    for name in list(names)[:2]:
        try:
            for o in retours.search_orders_by_name(name):
                if o["number"] not in numbers and all(w in retours._order_names(o) for w in name.split()):
                    by_name.append(o)
                    numbers.append(o["number"])
        except Exception as e:
            print(f"[Enquête] Name search '{name}' failed: {e}")
    wing_job = pool.submit(_wing_orders, list(numbers), sorted(names)) if numbers or names else None

    product_ids = []
    for o in shopify + by_name:
        for l in o["lines"]:
            if l.get("product_id") and l.get("category") != "other" and l["product_id"] not in product_ids:
                product_ids.append(l["product_id"])
    stock_job = pool.submit(lambda: [_stock_line(p) for p in product_ids[:MAX_PRODUCTS]])

    out = []

    def order_line(o):
        items = ", ".join(f"{l['title']}" + (f" {l['variant']}" if l["variant"] else "") for l in o["lines"])
        state = "annulée" if o.get("cancelled") else (f"livrée le {_date(o['delivered_at'])}" if o.get("delivered_at")
                                                       else f"expédiée le {_date(o['shipped_at'])}" if o.get("shipped_at")
                                                       else "pas encore expédiée")
        refund = f", remboursée {retours.euros(o['refunded'])}" if o.get("refunded") else ""
        return f"{o['name']} du {_date(o['created_at'])} ({o.get('email') or 'sans e-mail'}) : {items} · {state}{refund}."

    if shopify:
        out.append("Commandes Shopify connues :\n" + "\n".join("- " + order_line(o) for o in shopify))
    for o in shopify + by_name:
        if not any(d.get("is_madmax") for d in o.get("discounts") or []):
            continue
        rate = retours.madmax_terms(o, [], o["lines"] and [dict(l, qty=l["quantity"]) for l in o["lines"]])
        block = f"Commande Mad Max #{o['number']}" + (f" (remise de {rate['rate']} %)" if rate else "") + f" : {TIERS_TEXT}."
        if any(RETURN_WORDS.search(t or "") for t in texts[:2]):
            try:
                calc = _madmax_calc(o, texts[:6])
                if calc:
                    block += "\n" + calc
            except Exception as e:
                print(f"[Enquête] Mad Max calculation failed for #{o['number']}: {e}")
        out.append(block)
    if names:
        out.append("Autres commandes Shopify au même nom : " +
                   ("\n" + "\n".join("- " + order_line(o) for o in by_name) if by_name else "aucune."))
    try:
        codes = codes_job.result(timeout=40)
        if codes:
            out.append("Codes d'avoir ou de réduction cités dans les échanges :\n" + "\n".join("- " + c for c in codes))
    except Exception as e:
        print(f"[Enquête] Codes unreadable: {e}")
    try:
        wing = wing_job.result(timeout=60) if wing_job else None
        if wing is None:
            raise LookupError("rien à chercher dans Wing")
        out.append("Wing, toutes les commandes liées à ce client :\n" +
                   ("\n".join("- " + _wing_line(ref, acc, o) for ref, (acc, o) in sorted(wing.items()))
                    if wing else "- aucune."))
    except LookupError:
        pass
    except Exception as e:
        print(f"[Enquête] Wing unreadable: {e}")
    try:
        stock = stock_job.result(timeout=40)
        if stock:
            out.append("Stock Shopify actuel par pointure :\n" + "\n".join("- " + s for s in stock))
    except Exception as e:
        print(f"[Enquête] Stock unreadable: {e}")
    try:
        files = _files(set(numbers), sender_email)
        if files:
            out.append("Dossiers dans l'assistant :\n" + "\n".join("- " + f for f in files))
    except Exception as e:
        print(f"[Enquête] Files unreadable: {e}")
    pool.shutdown(wait=False)
    if not out:
        return ""
    return ("\n\n--- ENQUÊTE (recherches faites à l'instant dans Shopify et Wing : ce sont les faits) ---\n"
            + "\n\n".join(out) + "\n")
