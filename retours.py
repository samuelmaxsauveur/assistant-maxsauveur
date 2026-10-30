"""Customer returns (refund or credit) from the paper return slip.

Samuel photographs the slip at the logistics centre, after writing on it the
date the parcel arrived there. The assistant reads it, finds the Shopify
order, runs the checks (deadline, returned items, offers tied to the order,
credit already used, past refunds) and drafts the email Samuel picks. It
creates the credit codes and reads the customers' answers. Refunds stay with
Samuel, in Shopify: the assistant only computes them and notices when they
are done. No email leaves without Samuel's click.
"""
import base64
import difflib
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import anthropic
import pytz

import avoirs
import claude_ai
import database
import gmail as gmail_helper
import repair_photos

PARIS = pytz.timezone("Europe/Paris")

# Added to the catalogue price of each returned piece when the customer takes
# the credit instead of the refund (Samuel, 30/10/2026).
GESTURES = {"shoes": 35, "sandal_boat": 25, "belt": 15, "other": 0}
CATEGORY_LABELS = {"shoes": "chaussures", "sandal_boat": "sandales et bateaux",
                   "belt": "ceinture", "other": "accessoire"}
DEADLINE_WORKDAYS = 15          # delivery of the order -> arrival at SSL
REFUND_WITHIN_WORKDAYS = 10     # promised on the slip once the parcel is in
REMINDER_AFTER_DAYS = 3
REFUND_AFTER_DAYS = 7           # no answer to the offer: refund as asked on the slip

REASONS = {"10": "Trop grand", "20": "Trop petit", "30": "Mauvaise pointure reçue",
           "40": "Mauvaise couleur reçue", "50": "Mauvais article reçu",
           "60": "Article défectueux", "70": "Le modèle ne me plaît plus",
           "80": "J'ai changé d'avis"}
CHOICES = {"refund": "Remboursement", "credit": "Avoir"}

STATUS_LABELS = {
    "to_check": "À traiter",
    "waiting_client": "Proposition envoyée",
    "reply_to_read": "Réponse à lire",
    "credit_to_send": "Avoir à envoyer",
    "to_refund": "À rembourser",
    "done": "Terminé",
    "refused": "Refusé",
}
STAGES = [
    ("a-traiter", "À traiter", ("to_check",)),
    ("attente", "Attente client", ("waiting_client",)),
    ("reponse", "Réponse à lire", ("reply_to_read",)),
    ("avoir", "Avoir à envoyer", ("credit_to_send",)),
    ("rembourser", "À rembourser", ("to_refund",)),
    ("termines", "Terminés", ("done",)),
    ("refuses", "Refusés", ("refused",)),
]

TEMPLATES = {
    "offer": "Proposition d'avoir bonifié",
    "credit": "Avoir direct (avec le code)",
    "credit_bonus": "Avoir bonifié accepté (avec les codes)",
    "refund": "Remboursement en cours",
    "late": "Délai dépassé : avoir à la place du remboursement",
    "partial": "Retour partiel : offre liée à la commande",
    "condition": "Refus : paire marquée ou abîmée",
    "reminder": "Relance de la proposition",
    "free": "Message libre (d'après tes consignes)",
}
# Where the file goes once the email is sent.
AFTER_SEND = {"offer": "waiting_client", "reminder": "waiting_client", "late": "waiting_client",
              "partial": "waiting_client", "free": "waiting_client", "credit": "done",
              "credit_bonus": "done", "refund": "to_refund", "condition": "refused"}
CREDIT_TEMPLATES = ("credit", "credit_bonus")
# The offer each email makes, so the customer's "yes" gets the right credit.
OFFER_KIND = {"offer": "bonus", "reminder": "bonus", "late": "plain", "partial": "plain"}

READ_PROMPT = """Photo d'un bon de retour Max Sauveur (chaussures et ceintures pour homme), rempli à la main par le client.
La logistique a ajouté au stylo la date d'arrivée du colis chez elle : c'est une date manuscrite en plus des champs du client, souvent en haut, dans une marge ou en travers du bon. Ne la confonds pas avec la « Date d'expédition du colis retour » remplie par le client.
Réponds UNIQUEMENT avec ce JSON :
{"name": "nom du client ou null",
 "order_number": "numéro de commande, chiffres seulement, ou null",
 "client_shipping_date": "date d'expédition écrite par le client (AAAA-MM-JJ) ou null",
 "arrival_date": "date d'arrivée ajoutée par la logistique (AAAA-MM-JJ) ou null",
 "items": [{"reference": "modèle ou référence tel qu'écrit", "size": "pointure ou taille, ou null",
            "quantity": 1, "reason_code": "10 à 80, ou null", "choice": "refund" | "credit" | null}],
 "comment": "autre texte manuscrit utile (défaut précisé, demande particulière), ou null",
 "unreadable": "ce que tu n'arrives pas à lire, ou null"}
Codes retour : 10 Trop grand, 20 Trop petit, 30 Mauvaise pointure reçue, 40 Mauvaise couleur reçue, 50 Mauvais article reçu, 60 Article défectueux (précisez le défaut), 70 Le modèle ne me plaît plus, 80 J'ai changé d'avis.
choice : "refund" si la colonne « Remboursement ou Avoir » indique le remboursement, "credit" si elle indique l'avoir (ou un échange).
Les dates sont de {year}, ou de la fin de {last_year} : écris toujours l'année complète."""

REPLY_PROMPT = """Un client de Max Sauveur a renvoyé un article. Nous lui avons proposé : {offer}.
Voici sa réponse :
---
{reply}
---
Réponds UNIQUEMENT avec ce JSON : {{"choice": "credit" | "refund" | "unclear", "summary": "sa réponse en une phrase"}}
"credit" : il accepte clairement l'avoir. "refund" : il demande clairement le remboursement. "unclear" : une question, une hésitation, une autre demande, ou rien de clair."""


# ---------------------------------------------------------------- dates

def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _holidays(year):
    e = _easter(year)
    return {date(year, 1, 1), e + timedelta(1), date(year, 5, 1), date(year, 5, 8),
            e + timedelta(39), e + timedelta(50), date(year, 7, 14), date(year, 8, 15),
            date(year, 11, 1), date(year, 11, 11), date(year, 12, 25)}


def _workday(d):
    return d.weekday() < 5 and d not in _holidays(d.year)


def workdays_between(start, end):
    """Working days after `start`, up to and including `end` (French holidays off)."""
    n, d = 0, start
    while d < end:
        d += timedelta(1)
        n += _workday(d)
    return n


def add_workdays(start, count):
    d = start
    while count > 0:
        d += timedelta(1)
        count -= _workday(d)
    return d


def today():
    return datetime.now(PARIS).date()


def _parse_date(value):
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _recent(d):
    """A slip date is a few days old: when the year was not written (or misread),
    take the year that puts it closest before today."""
    if not d:
        return None
    now = today()
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(d.replace(year=year))
        except ValueError:      # 29 February
            pass
    past = [c for c in candidates if c <= now + timedelta(2)]
    return max(past) if past else d


_DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre")


def fr_date(d, weekday=True):
    day = "1er" if d.day == 1 else str(d.day)
    text = f"{day} {_MONTHS[d.month - 1]}"
    return f"{_DAYS[d.weekday()]} {text}" if weekday else text


def short_date(value):
    d = _parse_date(value) if isinstance(value, str) else value
    return d.strftime("%d/%m/%Y") if d else ""


def euros(amount):
    return f"{amount:.2f}".replace(".", ",").replace(",00", "") + " €"


# ---------------------------------------------------------------- photos

def _dir(case_id):
    base = os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)), "client_returns", str(int(case_id)))
    os.makedirs(base, exist_ok=True)
    return base


def save_photo(case_id, raw):
    data = repair_photos._jpeg(raw)
    name = f"bon_{int(time.time() * 1000)}.jpg"
    with open(os.path.join(_dir(case_id), name), "wb") as f:
        f.write(data)
    return name, data


def photo_path(case_id, name):
    """Absolute path of one photo of the file, or None (names are checked, never trusted)."""
    if not re.fullmatch(r"bon_\d+\.jpg", name or ""):
        return None
    p = os.path.join(_dir(case_id), name)
    return p if os.path.exists(p) else None


def delete_photos(case_id):
    folder = _dir(case_id)
    for n in os.listdir(folder):
        os.remove(os.path.join(folder, n))
    os.rmdir(folder)


# ---------------------------------------------------------------- reading the slip

def read_slip(jpeg):
    """What Claude reads on the photo of the slip."""
    year = today().year
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(jpeg).decode()}},
            {"type": "text", "text": READ_PROMPT.replace("{year}", str(year)).replace("{last_year}", str(year - 1))},
        ]}])
    text = next(b.text for b in msg.content if b.type == "text")
    start = text.find("{")
    if start < 0:
        raise RuntimeError("Claude n'a pas su lire le bon.")
    slip, _ = json.JSONDecoder().raw_decode(text[start:])
    slip["order_number"] = re.sub(r"\D", "", str(slip.get("order_number") or "")) or None
    for key in ("arrival_date", "client_shipping_date"):
        d = _recent(_parse_date(slip.get(key)))
        slip[key] = d.isoformat() if d else None
    for item in slip.get("items") or []:
        code = re.sub(r"\D", "", str(item.get("reason_code") or ""))
        item["reason_code"] = code if code in REASONS else None
        if item.get("choice") not in CHOICES:
            item["choice"] = None
    return slip


# ---------------------------------------------------------------- the Shopify order

_ORDER_FIELDS = """
  id name createdAt email customerLocale displayFinancialStatus cancelledAt
  customer { id firstName lastName }
  billingAddress { firstName name }
  discountApplications(first: 20) { nodes { __typename
     ... on DiscountCodeApplication { code } ... on AutomaticDiscountApplication { title }
     ... on ManualDiscountApplication { title } ... on ScriptDiscountApplication { title } } }
  totalShippingPriceSet { shopMoney { amount } }
  transactions(first: 20) { kind status gateway amountSet { shopMoney { amount } } }
  refunds(first: 10) { createdAt totalRefundedSet { shopMoney { amount } } }
  fulfillments(first: 5) { createdAt deliveredAt displayStatus trackingInfo { number company } }
  lineItems(first: 50) { nodes { id title variantTitle quantity
     originalUnitPriceSet { shopMoney { amount } }
     discountedUnitPriceAfterAllDiscountsSet { shopMoney { amount } }
     discountAllocations { allocatedAmountSet { shopMoney { amount } } discountApplication { index } }
     product { id productType } } }
"""


def _amount(money_set):
    return float(((money_set or {}).get("shopMoney") or {}).get("amount") or 0)


def category(title):
    t = avoirs._norm(title)
    if "ceinture" in t:
        return "belt"
    if "sandale" in t or "bateau" in t:
        return "sandal_boat"
    return "shoes"


def _discount_title(code):
    """Title of a discount code, to tell an avoir apart from a promotion."""
    try:
        node = avoirs._gql("""query($c: String!) { codeDiscountNodeByCode(code: $c) { codeDiscount {
            ... on DiscountCodeBasic { title } ... on DiscountCodeFreeShipping { title } } } }""",
                           {"c": code})["codeDiscountNodeByCode"]
        return ((node or {}).get("codeDiscount") or {}).get("title") or ""
    except Exception as e:
        print(f"[Retours] Discount {code} unreadable: {e}")
        return ""


def _normalise(node):
    discounts = []
    for app in node["discountApplications"]["nodes"]:
        label = app.get("code") or app.get("title") or "remise"
        title = _discount_title(app["code"]) if app.get("code") else label
        low = f"{label} {title}".lower()
        discounts.append({"label": label, "title": title, "is_code": bool(app.get("code")),
                          "is_credit": "avoir" in low,
                          "is_madmax": "madmax" in low.replace(" ", ""),
                          "is_bebelt": "bebelt" in low})
    lines = []
    for item in node["lineItems"]["nodes"]:
        title = item["title"]
        product_type = ((item.get("product") or {}).get("productType") or "")
        allocations = []
        for a in item.get("discountAllocations") or []:
            d = discounts[a["discountApplication"]["index"]] if a.get("discountApplication") else {}
            allocations.append({"label": d.get("label", "remise"), "amount": _amount(a["allocatedAmountSet"]),
                                "is_credit": d.get("is_credit", False), "is_bebelt": d.get("is_bebelt", False)})
        lines.append({
            "id": item["id"].rsplit("/", 1)[-1], "title": title, "variant": item.get("variantTitle") or "",
            "quantity": item["quantity"],
            "catalogue": _amount(item["originalUnitPriceSet"]),
            "paid": _amount(item["discountedUnitPriceAfterAllDiscountsSet"]),
            "discounts": allocations,
            "category": "other" if product_type == "Accessoires" and "ceinture" not in avoirs._norm(title)
                        else category(title),
            "product_id": (item.get("product") or {}).get("id"),
        })
    fulfilled = [f for f in node.get("fulfillments") or []]
    delivered = [f["deliveredAt"] for f in fulfilled if f.get("deliveredAt")]
    tracking = next((t for f in fulfilled for t in f.get("trackingInfo") or []), {})
    customer = node.get("customer") or {}
    billing = node.get("billingAddress") or {}
    first = customer.get("firstName") or billing.get("firstName") or ""
    return {
        "gid": node["id"], "legacy_id": node["id"].rsplit("/", 1)[-1],
        "name": node["name"], "number": node["name"].lstrip("#"),
        "created_at": node["createdAt"][:10], "email": node.get("email") or "",
        "english": (node.get("customerLocale") or "fr").lower().startswith("en"),
        "financial_status": node.get("displayFinancialStatus"), "cancelled": bool(node.get("cancelledAt")),
        "customer_id": customer.get("id"), "first_name": first.strip().title(),
        "full_name": billing.get("name") or f"{customer.get('firstName') or ''} {customer.get('lastName') or ''}".strip(),
        "shipping": _amount(node.get("totalShippingPriceSet")),
        "gift_card": sum(_amount(t["amountSet"]) for t in node.get("transactions") or []
                         if t.get("gateway") == "gift_card" and t.get("kind") in ("SALE", "CAPTURE")
                         and t.get("status") == "SUCCESS"),
        "refunded": sum(_amount(r["totalRefundedSet"]) for r in node.get("refunds") or []),
        "refund_dates": [r["createdAt"][:10] for r in node.get("refunds") or []],
        "delivered_at": (datetime.fromisoformat(min(delivered).replace("Z", "+00:00"))
                         .astimezone(PARIS).date().isoformat() if delivered else None),
        "shipped_at": min(f["createdAt"] for f in fulfilled)[:10] if fulfilled else None,
        "carrier": tracking.get("company") or "",
        "discounts": discounts,
        "lines": lines,
    }


def fetch_order(number):
    number = re.sub(r"\D", "", str(number or ""))
    if not number:
        return None
    nodes = avoirs._gql(f"query($q: String!) {{ orders(first: 1, query: $q) {{ nodes {{ {_ORDER_FIELDS} }} }} }}",
                        {"q": f"name:#{number}"})["orders"]["nodes"]
    if not nodes or nodes[0]["name"].lstrip("#") != number:
        return None
    return _normalise(nodes[0])


def search_orders_by_name(name):
    """Most recent delivered orders of a customer, when the slip has no usable number."""
    words = [w for w in avoirs._norm(name).split() if len(w) > 2]
    if not words:
        return []
    nodes = avoirs._gql(f"query($q: String!) {{ orders(first: 5, query: $q, sortKey: CREATED_AT, reverse: true) "
                        f"{{ nodes {{ {_ORDER_FIELDS} }} }} }}", {"q": " ".join(words)})["orders"]["nodes"]
    found = [_normalise(n) for n in nodes]
    return [o for o in found if all(w in avoirs._norm(o["full_name"] + " " + o["email"]) for w in words[-1:])]


def _names_match(slip_name, order):
    if not slip_name:
        return True
    target = avoirs._norm(order["full_name"] + " " + order["email"])
    return any(w in target for w in avoirs._norm(slip_name).split() if len(w) > 2)


# ---------------------------------------------------------------- matching the slip to the order

def _match_lines(slip, order):
    """{line id: quantity returned}, from what the customer wrote on the slip."""
    lines = order["lines"]
    items = slip.get("items") or []
    returned = {}
    for item in items:
        ref = avoirs._norm(f"{item.get('reference') or ''}")
        size = avoirs._norm(str(item.get("size") or ""))

        def score(line):
            label = avoirs._norm(f"{line['title']} {line['variant']}")
            words = [w for w in ref.split() if len(w) > 2]
            hits = sum(1 for w in words if w in label.split()) / max(len(words), 1)
            s = max(hits, difflib.SequenceMatcher(None, ref, label).ratio())
            if size and size == avoirs._norm(line["variant"]):
                s += 0.3
            return s

        ranked = sorted(lines, key=score, reverse=True)
        if ranked and (score(ranked[0]) >= 0.35 or len(lines) == 1):
            qty = int(item.get("quantity") or 1)
            returned[ranked[0]["id"]] = min(ranked[0]["quantity"], returned.get(ranked[0]["id"], 0) + qty)
    if not returned and not items:
        main = [l for l in lines if l["category"] != "other"]
        if len(main) == 1:
            returned[main[0]["id"]] = main[0]["quantity"]
    return returned


def _slip_choice(slip):
    choices = {i.get("choice") for i in slip.get("items") or []} - {None}
    return choices.pop() if len(choices) == 1 else None


def _slip_reason(slip):
    codes = [i.get("reason_code") for i in slip.get("items") or [] if i.get("reason_code")]
    return codes[0] if codes else None


# ---------------------------------------------------------------- analysis

def analyse(data):
    """Checks and amounts for the file, from the order, the slip and Samuel's corrections."""
    order = data.get("order")
    checks = []

    def add(level, text):
        checks.append({"level": level, "text": text})

    if not order:
        add("bad", "Commande introuvable : saisis le numéro de commande puis « Recalculer ».")
        return {"checks": checks, "amounts": {}, "returned": [], "kept": []}

    slip = data.get("slip") or {}
    if data.get("found_by"):
        add("warn", data["found_by"])
    elif slip.get("name") and not _names_match(slip["name"], order):
        add("warn", f"Le nom du bon (« {slip['name']} ») ne correspond pas à la commande ({order['full_name']}).")
    if order["cancelled"]:
        add("bad", "Commande annulée dans Shopify.")

    qty = {k: int(v) for k, v in (data.get("returned") or {}).items() if int(v) > 0}
    returned = [dict(l, qty=qty[l["id"]]) for l in order["lines"] if l["id"] in qty]
    kept = [dict(l, qty=l["quantity"] - qty.get(l["id"], 0)) for l in order["lines"]
            if l["quantity"] - qty.get(l["id"], 0) > 0]

    # Deadline: delivery of the order to the customer -> arrival at SSL.
    arrival = _parse_date(data.get("arrival_date"))
    if not data.get("arrival_from_slip"):
        add("warn", "Date d'arrivée chez SSL non lue sur le bon : j'ai mis la date de la photo, corrige-la si besoin.")
    delivered = _parse_date(order.get("delivered_at"))
    if arrival and delivered and arrival < delivered:
        add("bad", f"Arrivée chez SSL ({short_date(arrival)}) avant la livraison de la commande "
                   f"({short_date(delivered)}) : corrige la date d'arrivée.")
    elif arrival and delivered:
        n = workdays_between(delivered, arrival)
        if n > DEADLINE_WORKDAYS:
            add("bad", f"Délai dépassé : livrée le {short_date(delivered)}, arrivée chez SSL le {short_date(arrival)}, "
                       f"soit {n} jours ouvrés (maximum {DEADLINE_WORKDAYS}).")
        else:
            add("ok", f"Délai : livrée le {short_date(delivered)}, arrivée chez SSL le {short_date(arrival)}, "
                      f"soit {n} jours ouvrés sur {DEADLINE_WORKDAYS}.")
    elif arrival:
        shipped = order.get("shipped_at")
        add("warn", "Date de livraison inconnue dans Shopify" + (f" (expédiée le {short_date(shipped)})" if shipped else "")
                    + " : vérifie le délai à la main.")

    # What came back.
    if not returned:
        add("bad", "Aucun article retourné n'est coché.")
    slip_items = slip.get("items") or []
    for line in returned:
        size = next((str(i.get("size") or "") for i in slip_items
                     if avoirs._norm(str(i.get("size") or "")) == avoirs._norm(line["variant"])), None)
        written = [str(i.get("size")) for i in slip_items if i.get("size")]
        if written and size is None and len(returned) == 1:
            add("warn", f"Pointure écrite sur le bon : {', '.join(written)}. Commande : {line['variant']}. Vérifie la paire.")
        else:
            add("ok", f"Article : {line['title']} ({line['variant']}) × {line['qty']}, présent dans la commande.")
    for code in {i.get("reason_code") for i in slip_items} & {"40", "50"}:
        add("warn", f"Code {code} « {REASONS[code]} » : erreur de préparation possible, vérifie la paire reçue.")
    if "60" in {i.get("reason_code") for i in slip_items}:
        add("warn", "Code 60 « Article défectueux » : à traiter plutôt dans le circuit réparations ?")

    # Offers tied to what the customer keeps.
    if returned and kept:
        add("warn", "Retour partiel : le client garde " + ", ".join(f"{l['title']} ({l['variant']})" for l in kept) + ".")
        for line in kept:
            if any(d["is_bebelt"] for d in line["discounts"]) or (line["category"] == "belt" and line["paid"] < line["catalogue"]
                                                                  and any(l["category"] != "belt" for l in returned)):
                loss = (line["catalogue"] - line["paid"]) * line["qty"]
                refundable = sum(l["paid"] * l["qty"] for l in returned) - loss
                add("bad", f"Il garde {line['title']} obtenue à prix réduit ({euros(line['paid'])} au lieu de "
                           f"{euros(line['catalogue'])}) grâce à l'achat de la paire retournée. Remboursement "
                           f"seulement en déduisant la remise de {euros(loss)} : {euros(max(refundable, 0))}.")
        if any(d["is_madmax"] for d in order["discounts"]):
            add("warn", "Remise Mad Max sur la commande : le palier se recalcule sur les articles gardés.")

    # How the returned pieces were paid.
    credit_paid = sum(d["amount"] * l["qty"] / l["quantity"] for l in returned for d in l["discounts"] if d["is_credit"])
    if credit_paid:
        add("bad", f"Payé avec un avoir ({euros(credit_paid)}) : cette part ne se rembourse pas, l'avoir est à réactiver.")
    if order["gift_card"]:
        add("warn", f"Commande payée en partie par carte cadeau ({euros(order['gift_card'])}).")
    for line in returned:
        promos = [d for d in line["discounts"] if not d["is_credit"] and d["amount"] > 0]
        if promos and line["paid"] < line["catalogue"]:
            add("ok", f"{line['title']} payée {euros(line['paid'])} au lieu de {euros(line['catalogue'])} "
                      f"({', '.join(d['label'] for d in promos)}).")
    if order["refunded"]:
        add("warn", f"Déjà remboursé sur cette commande : {euros(order['refunded'])} "
                    f"(le {', '.join(short_date(d) for d in order['refund_dates'])}).")
    for a in data.get("existing_avoirs") or []:
        add("warn", f"Avoir déjà créé pour cette commande : {a['code']} ({euros(a['amount'])}, {a['status'].lower()}).")

    choice = data.get("choice")
    add("ok" if choice else "warn", f"Demande du client : {CHOICES[choice]}." if choice
        else "Remboursement ou avoir : non lu sur le bon, choisis-le.")

    catalogue = sum(l["catalogue"] * l["qty"] for l in returned)
    gesture = sum(GESTURES[l["category"]] * l["qty"] for l in returned)
    paid = sum(l["paid"] * l["qty"] for l in returned)
    whole = bool(returned) and not kept
    refund = round(paid + (order["shipping"] if whole else 0), 2)
    amounts = {"catalogue": round(catalogue, 2), "gesture": gesture, "bonus": round(catalogue + gesture, 2),
               "paid": round(paid, 2), "refund": refund, "credit_paid": round(credit_paid, 2),
               "shipping_refunded": whole and order["shipping"] > 0,
               "discounted": paid + credit_paid < catalogue}
    return {"checks": checks, "amounts": amounts, "returned": returned, "kept": kept}


def default_template(data, result):
    late = any(c["level"] == "bad" and c["text"].startswith("Délai dépassé") for c in result["checks"])
    if late and data.get("choice") != "credit":
        return "late"
    if any(c["level"] == "bad" and c["text"].startswith("Il garde") for c in result["checks"]):
        return "partial"
    return "credit" if data.get("choice") == "credit" else "offer"


# ---------------------------------------------------------------- emails

def _items_text(returned, english=False):
    parts = []
    for l in returned:
        unit = ("size" if english else "taille") if l["category"] == "belt" else ("size" if english else "pointure")
        label = f"{l['title']}, {unit} {l['variant']}" if l["variant"] else l["title"]
        parts.append(f"{l['qty']} × {label}" if l["qty"] > 1 else label)
    return " et ".join(parts) if not english else " and ".join(parts)


def _curiosity(reason):
    if reason in ("10", "20", "30"):
        return ("au-delà du problème de taille, est-ce qu'il y a un détail sur le modèle (le cuir, la forme "
                "ou le style) qui ne vous a pas totalement convaincu ?")
    return "est-ce qu'il y a un détail sur le modèle (le cuir, la forme ou le style) qui ne vous a pas totalement convaincu ?"


def _avoir(case, amount, free_shipping):
    order = case["data"]["order"]
    result = analyse(case["data"])
    if not order.get("customer_id"):
        raise RuntimeError(f"La commande {order['name']} n'a pas de fiche client : impossible de réserver l'avoir.")
    return {"order": order["name"], "email": order["email"], "customer_id": order["customer_id"],
            "first_name": order["first_name"], "english": order["english"],
            "item": " + ".join(l["title"] for l in result["returned"]) or "article retourné",
            "amount": round(float(amount), 2), "model_only": False, "product_id": None,
            "free_shipping": free_shipping,
            "ends_at": avoirs._add_months(datetime.now(timezone.utc), avoirs.VALIDITY_MONTHS)}


def draft(case, template, amount=None):
    """Subject and body of one email, from the fixed texts below."""
    data = case["data"]
    order = data.get("order") or {}
    result = analyse(data)
    a = result["amounts"]
    first = order.get("first_name") or ""
    items = _items_text(result["returned"]) or "votre article"
    reason = _slip_reason(data.get("slip") or {})
    one = sum(l["qty"] for l in result["returned"]) <= 1
    paire = "votre paire" if one and all(l["category"] != "belt" for l in result["returned"]) else \
            "votre ceinture" if one else "vos articles"
    sign = "\n\nJohn de Max Sauveur"
    if not a:
        return "Votre retour", f"Bonjour,\n\n(commande introuvable : saisis le numéro de commande){sign}"
    bonus = float(amount) if amount not in (None, "") else a["bonus"]
    gesture = round(bonus - a["catalogue"], 2)
    answer_by = fr_date(today() + timedelta(REFUND_AFTER_DAYS))
    subject = f"Votre retour pour la commande {order.get('name', '')}".strip()

    if template == "offer":
        advantage = (f"Autrement dit, vous gardez l'avantage de votre commande initiale et vous repartez avec "
                     f"{euros(gesture)} de plus." if a.get("discounted") else
                     f"Autrement dit, vous repartez avec une nouvelle pièce sans rien débourser, et {euros(gesture)} "
                     f"de plus en poche.")
        body = (f"Bonjour {first},\n\n"
                f"Nous avons bien reçu votre retour ({items}) et votre demande de remboursement est enregistrée. "
                f"On s'en occupe.\n\n"
                f"Par simple curiosité, {_curiosity(reason)}\n\n"
                f"On essaie de comprendre si une autre pièce de la collection pourrait mieux vous correspondre, que "
                f"vous soyez plutôt dans une recherche d'esthétique précise ou de durabilité.\n\n"
                f"=> Si une autre pointure ou un autre modèle vous tente, voici ce qu'on peut faire : nous vous "
                f"envoyons un avoir de {euros(bonus)}, soit le prix de {paire} ({euros(a['catalogue'])}) et "
                f"{euros(gesture)} offerts par la maison. Il est valable 6 mois sur toute la collection, chaussures "
                f"et ceintures, et la livraison de votre nouvelle commande est offerte, à l'adresse de votre choix.\n\n"
                f"{advantage}\n\n"
                f"Une précision : une fois émis, l'avoir remplace le remboursement.\n\n"
                f"Dites-moi simplement si vous préférez cette option ou si nous validons le remboursement initial "
                f"sur votre compte. Sans réponse de votre part d'ici le {answer_by}, nous procéderons au "
                f"remboursement.{sign}")
    elif template == "reminder":
        subject = f"Re: {subject}"
        body = (f"Bonjour {first},\n\n"
                f"Je reviens vers vous au sujet de votre retour ({items}).\n\n"
                f"Notre proposition tient toujours : un avoir de {euros(bonus)} ({euros(a['catalogue'])} et "
                f"{euros(gesture)} offerts par la maison), valable 6 mois sur toute la collection, avec la livraison "
                f"offerte sur votre nouvelle commande.\n\n"
                f"Sans réponse de votre part d'ici le {fr_date(_offer_deadline(data))}, nous procéderons au "
                f"remboursement.{sign}")
    elif template in CREDIT_TEMPLATES:
        code = data.get("credit_code") or "XXXXXXXXXXXX"
        ends = _parse_date(data.get("credit_ends_at")) or avoirs._add_months(today(), avoirs.VALIDITY_MONTHS)
        if data.get("credit_amount"):
            value = float(data["credit_amount"])
        elif template == "credit_bonus":
            value = bonus
        else:
            value = float(amount) if amount not in (None, "") else a.get("catalogue", 0)
        if template == "credit_bonus":
            ship = data.get("shipping_code") or "LIVRAISONXXXXXX"
            subject = f"Re: {subject}"
            body = (f"Bonjour {first},\n\n"
                    f"Merci pour votre retour. Votre avoir de {euros(value)} est prêt.\n\n"
                    f"Voici vos deux codes, à saisir tous les deux dans votre panier avant de finaliser la "
                    f"commande :\n\n"
                    f"{code} : votre avoir de {euros(value)}\n"
                    f"{ship} : la livraison offerte\n\n"
                    f"Conditions d'utilisation :\n"
                    f"- valable jusqu'au {short_date(ends)},\n"
                    f"- utilisable sur nos chaussures et ceintures homme, en une seule commande,\n"
                    f"- réservé à votre adresse e-mail ({order.get('email')}) : pensez à commander avec cette "
                    f"adresse,\n"
                    f"- en dehors de ces deux codes, non cumulable avec d'autres codes ou promotions.\n\n"
                    f"Si vous avez la moindre question, je reste disponible.{sign}")
        else:
            av = _avoir(case, value, False) if order.get("customer_id") else None
            if av:
                av["ends_at"] = datetime.combine(ends, datetime.min.time())
                subject, body = avoirs.email(av, code)
            else:
                body = f"Bonjour {first},\n\n(avoir impossible : la commande n'a pas de fiche client){sign}"
    elif template == "refund":
        refund = float(amount) if amount not in (None, "") else a.get("refund", 0)
        body = (f"Bonjour {first},\n\n"
                f"Nous avons bien reçu votre retour ({items}). Le remboursement de {euros(refund)} est en cours sur "
                f"votre moyen de paiement d'origine : il apparaîtra sur votre compte d'ici quelques jours.\n\n"
                f"Si vous avez la moindre question, je reste disponible.{sign}")
    elif template == "late":
        value = float(amount) if amount not in (None, "") else a.get("catalogue", 0)
        body = (f"Bonjour {first},\n\n"
                f"Nous avons bien reçu votre retour ({items}).\n\n"
                f"Le délai de retour prévu par nos conditions de vente étant dépassé, nous ne pouvons pas procéder "
                f"au remboursement. En revanche, nous pouvons vous proposer un avoir de {euros(value)}, du montant "
                f"de {paire}, valable 6 mois sur toute la collection, chaussures et ceintures.\n\n"
                f"Dites-moi simplement si cette solution vous convient et je vous l'envoie dans la foulée.{sign}")
    elif template == "partial":
        kept = [l for l in result["kept"] if l["paid"] < l["catalogue"]] or result["kept"]
        kept_text = ", ".join(l["title"] for l in kept) or "un autre article"
        value = float(amount) if amount not in (None, "") else a.get("catalogue", 0)
        body = (f"Bonjour {first},\n\n"
                f"Nous avons bien reçu votre retour ({items}).\n\n"
                f"Votre commande comprenait aussi {kept_text}, obtenue à prix réduit grâce à l'achat de {paire}. "
                f"En l'état, nous ne pouvons donc pas procéder au remboursement : il faudrait nous retourner aussi "
                f"{kept_text}.\n\n"
                f"Il existe une solution plus simple : nous vous envoyons un avoir de {euros(value)}, du montant de "
                f"{paire}, pour commander une autre pointure ou un autre modèle. Vous gardez ainsi votre offre.\n\n"
                f"Dites-moi simplement ce que vous préférez.{sign}")
    elif template == "condition":
        body = (f"Bonjour {first},\n\n"
                f"Nous avons bien reçu votre retour ({items}).\n\n"
                f"Lors du contrôle, notre équipe a constaté que {paire} présente des marques d'usage qui ne "
                f"permettent pas de la remettre en vente. Nous ne pouvons donc pas accepter ce retour.\n\n"
                f"Si vous le souhaitez, nous pouvons vous la renvoyer : dites-moi simplement à quelle adresse.{sign}")
    else:
        body = f"Bonjour {first},\n\n{sign.strip()}"
    return subject, body


def _facts(case):
    """The figures Claude must use when it rewrites an email."""
    data = case["data"]
    order = data.get("order") or {}
    result = analyse(data)
    a = result["amounts"]
    lines = [f"Commande {order.get('name')} du {short_date(order.get('created_at'))}, client {order.get('full_name')} "
             f"({order.get('email')}), prénom {order.get('first_name')}."]
    for l in result["returned"]:
        lines.append(f"Retourné : {l['title']} ({l['variant']}) × {l['qty']}, prix catalogue {euros(l['catalogue'])}, "
                     f"payé {euros(l['paid'])}, catégorie {CATEGORY_LABELS[l['category']]}.")
    for l in result["kept"]:
        lines.append(f"Gardé par le client : {l['title']} ({l['variant']}), payé {euros(l['paid'])} au lieu de "
                     f"{euros(l['catalogue'])}.")
    if a:
        lines.append(f"Avoir simple : {euros(a['catalogue'])}. Geste : {euros(a['gesture'])}. Avoir bonifié : "
                     f"{euros(a['bonus'])}. Remboursement estimé : {euros(a['refund'])}.")
    reason = _slip_reason(data.get("slip") or {})
    if reason:
        lines.append(f"Code retour du bon : {reason} ({REASONS[reason]}).")
    if data.get("choice"):
        lines.append(f"Demande écrite sur le bon : {CHOICES[data['choice']]}.")
    # The checks stay off: they are notes for Samuel, never for the customer.
    if data.get("reply_text"):
        lines.append(f"Dernière réponse du client : {data['reply_text'][:1500]}")
    return "\n".join(lines)


def rewrite(case, template, consignes, amount=None):
    """The email for this file: the fixed text, then Samuel's instructions applied by Claude."""
    data = case["data"]
    order = data.get("order") or {}
    if template.startswith("tpl:"):
        body = claude_ai.generate_outbound_email(
            customer_name=order.get("full_name", ""), customer_email=order.get("email", ""),
            subject_type=template[4:],
            user_draft=f"{consignes}\n\nDonnées du retour (montants à utiliser tels quels) :\n{_facts(case)}",
            order_info=None)
        return f"Votre retour pour la commande {order.get('name', '')}", body
    subject, body = draft(case, template, amount)
    if template == "free" or consignes.strip():
        base = "" if template == "free" else f"\n\nMODÈLE VALIDÉ, À MODIFIER LE MOINS POSSIBLE :\n---\n{body}\n---"
        prompt = (f"Tu prépares un e-mail à un client Max Sauveur au sujet de son retour.\n\n"
                  f"Données du retour :\n{_facts(case)}{base}\n\n"
                  f"Consignes de Samuel pour ce cas précis :\n{consignes or '(aucune)'}\n\n"
                  + ("Applique les consignes au modèle : garde ses formulations, sa structure et sa signature, et "
                     "change seulement ce que les consignes demandent (recalcule les montants si elles le demandent). "
                     if base else "Rédige l'e-mail d'après les consignes, signé « John de Max Sauveur ». ")
                  + "N'invente aucune information. Si le client écrit en anglais, réponds en anglais.\n"
                    "Réponds uniquement avec le corps de l'e-mail.")
        body = claude_ai._call_claude(system=claude_ai.get_system_prompt(email_context=consignes),
                                      messages=[{"role": "user", "content": prompt}], max_tokens=1200).strip()
    return subject, body


# ---------------------------------------------------------------- the file

def _log(data, text):
    stamp = datetime.now(PARIS).strftime("%d/%m %H:%M")
    return (data.get("log") or []) + [f"{stamp} : {text}"]


def _summary(result):
    returned = result["returned"]
    return {"items": ", ".join(f"{l['title'].split(' - ')[0]} {l['variant']}".strip() for l in returned) or "?",
            "alert": "bad" if any(c["level"] == "bad" for c in result["checks"])
                     else "warn" if any(c["level"] == "warn" for c in result["checks"]) else "ok"}


def refresh(case_id, order_number=None):
    """Re-read the order in Shopify and recompute the checks and the draft."""
    case = database.get_client_return(case_id)
    data = dict(case["data"])
    number = order_number or case.get("order_number") or (data.get("slip") or {}).get("order_number")
    order = fetch_order(number) if number else None
    data["found_by"] = None
    if not order and (data.get("slip") or {}).get("name"):
        candidates = search_orders_by_name(data["slip"]["name"])
        if candidates:
            order = candidates[0]
            data["found_by"] = (f"Numéro de commande illisible ou inconnu : commande trouvée par le nom "
                                f"(la plus récente sur {len(candidates)}). Vérifie que c'est la bonne.")
    if order and (not data.get("order") or data["order"]["name"] != order["name"]):
        data["returned"] = _match_lines(data.get("slip") or {}, order)
    data["order"] = order
    data["existing_avoirs"] = []
    if order:
        try:
            data["existing_avoirs"] = [{"code": a["code"], "amount": a["amount"], "status": a["status"]}
                                       for a in avoirs.existing_avoirs(order["name"])]
        except Exception as e:
            print(f"[Retours] Existing avoirs unreadable for {order['name']}: {e}")
    result = analyse(data)
    data["summary"] = _summary(result)
    if case["status"] == "to_check" and not data.get("template_chosen"):
        data["template"] = default_template(data, result)
    case = database.update_client_return(
        case_id, data=data, order_number=order["number"] if order else number,
        customer_name=order["full_name"] if order else (data.get("slip") or {}).get("name"),
        customer_email=order["email"] if order else None)
    if order and case["status"] == "to_check" and not data.get("mail_edited"):
        subject, body = rewrite(case, data["template"], data.get("consignes") or "", data.get("amount_override"))
        case = database.update_client_return(case_id, data={"mail_subject": subject, "mail_body": body})
    return case


def prepare_mail(case_id, template, consignes, amount):
    """The email Samuel asked for on the card: chosen template, his instructions, his amount."""
    if template not in TEMPLATES and not template.startswith("tpl:"):
        raise ValueError("Mail inconnu")
    case = database.update_client_return(case_id, data={
        "template": template, "template_chosen": True, "consignes": consignes,
        "amount_override": amount if amount not in (None, "") else None})
    subject, body = rewrite(case, template, consignes, amount)
    return database.update_client_return(case_id, data={"mail_subject": subject, "mail_body": body,
                                                        "mail_edited": False})


def create_from_photo(raw):
    """A new file from the photo of a slip: read, matched to its order, drafted."""
    case_id = database.save_client_return(status="to_check", data={})
    name, jpeg = save_photo(case_id, raw)
    data = {"photos": [name], "log": _log({}, "bon photographié")}
    try:
        slip = read_slip(jpeg)
        data["slip"] = slip
        data["choice"] = _slip_choice(slip)
        arrival = _parse_date(slip.get("arrival_date"))
        data["arrival_from_slip"] = bool(arrival)
        data["arrival_date"] = (arrival or today()).isoformat()
    except Exception as e:
        data["error"] = f"Lecture du bon impossible : {e}"
        data["arrival_date"] = today().isoformat()
    database.update_client_return(case_id, data=data,
                                  order_number=(data.get("slip") or {}).get("order_number"),
                                  customer_name=(data.get("slip") or {}).get("name"))
    try:
        return refresh(case_id)
    except Exception as e:
        print(f"[Retours] Analysis failed for file {case_id}: {e}")
        return database.update_client_return(case_id, data={"error": f"Analyse impossible : {e}"})


def update(case_id, fields):
    """Samuel's corrections on the card: order number, arrival date, choice, pieces returned."""
    case = database.get_client_return(case_id)
    data = {}
    if "arrival_date" in fields and _parse_date(fields["arrival_date"]):
        data["arrival_date"] = fields["arrival_date"]
        data["arrival_from_slip"] = True
    if "choice" in fields:
        data["choice"] = fields["choice"] if fields["choice"] in CHOICES else None
    if "returned" in fields:
        data["returned"] = {str(k): int(v) for k, v in (fields["returned"] or {}).items() if int(v) > 0}
    number = re.sub(r"\D", "", str(fields.get("order_number") or "")) or None
    if number and number != case.get("order_number"):
        data["returned"] = None
    data["mail_edited"] = False
    database.update_client_return(case_id, data=data, order_number=number or case.get("order_number"))
    if number and number != case.get("order_number"):
        # A new order: forget the old one so the pieces are matched again.
        database.update_client_return(case_id, data={"order": None})
    return refresh(case_id, number)


def create_credit(case_id, bonus, amount=None):
    """Create the credit code(s) in Shopify and put them in the email to send."""
    case = database.get_client_return(case_id)
    data = case["data"]
    if data.get("credit_code"):
        raise RuntimeError(f"L'avoir existe déjà : {data['credit_code']}.")
    a = analyse(data)["amounts"]
    if amount not in (None, ""):
        value = float(amount)
    elif data.get("offer_amount") and (data.get("offer_kind") or "bonus") == ("bonus" if bonus else "plain"):
        value = float(data["offer_amount"])     # what the customer was promised
    else:
        value = a["bonus"] if bonus else a["catalogue"]
    if value <= 0:
        raise RuntimeError("Montant de l'avoir nul : vérifie les articles retournés.")
    av = _avoir(case, value, free_shipping=bonus)
    code = avoirs.create_code(av)
    ship = avoirs.create_shipping_code(av) if bonus else None
    log = f"avoir créé dans Shopify : {code} ({euros(value)})" + (f" + livraison offerte {ship}" if ship else "")
    template = "credit_bonus" if bonus else "credit"
    case = database.update_client_return(case_id, status="credit_to_send", data={
        "credit_code": code, "shipping_code": ship, "credit_amount": value,
        "credit_ends_at": av["ends_at"].date().isoformat(), "template": template,
        "mail_edited": False, "log": _log(data, log)})
    subject, body = draft(case, template)
    return database.update_client_return(case_id, data={"mail_subject": subject, "mail_body": body})


def send(case_id, template, subject, body):
    """Send the email Samuel validated, then move the file to its next step."""
    case = database.get_client_return(case_id)
    data = case["data"]
    order = data.get("order") or {}
    to = order.get("email") or case.get("customer_email")
    if not to:
        raise RuntimeError("Adresse du client inconnue.")
    if template in CREDIT_TEMPLATES and not data.get("credit_code"):
        raise RuntimeError("Crée d'abord l'avoir dans Shopify : le mail doit contenir le vrai code.")
    if template in CREDIT_TEMPLATES and data["credit_code"] not in body:
        raise RuntimeError(f"Le code {data['credit_code']} n'est plus dans le mail.")
    service = gmail_helper.get_gmail_service()
    sent = gmail_helper.send_email(service, to, subject, body, thread_id=case.get("thread_id"))
    thread_id = (sent or {}).get("threadId") or case.get("thread_id")
    database.log_sent_email(to_email=to, subject=subject, body=body, source="retour", thread_id=thread_id)
    if template.startswith("tpl:") or template == "free":
        status = "waiting_client" if case["status"] in ("to_check", "reply_to_read") else case["status"]
    else:
        status = AFTER_SEND.get(template, case["status"])
    now = datetime.now(PARIS).isoformat()
    changes = {"log": _log(data, f"mail envoyé : {TEMPLATES.get(template, 'mail pré-rédigé')}"),
               "mail_edited": False, "last_sent_at": now,
               "seen_messages": (data.get("seen_messages") or []) + ([sent["id"]] if sent and sent.get("id") else [])}
    if template in OFFER_KIND or template.startswith("tpl:") or template == "free":
        changes["offer_kind"] = OFFER_KIND.get(template, data.get("offer_kind") or "bonus")
    if template in ("offer", "late", "partial") or (template.startswith("tpl:") and not data.get("offer_sent_at")):
        changes["offer_sent_at"] = now
        a = analyse(data)["amounts"]
        override = data.get("amount_override")
        changes["offer_amount"] = float(override) if override not in (None, "") else \
            (a.get("bonus") if changes["offer_kind"] == "bonus" else a.get("catalogue"))
        # Instructions or a hand-edited email may promise another amount than
        # the one computed: the credit then waits for Samuel instead of being created.
        changes["offer_custom"] = euros(changes["offer_amount"] or 0) not in body
    if template == "reminder":
        changes["reminder_sent_at"] = now
        changes["reminder_due"] = False
    if status == "to_refund":
        changes["refunded_before"] = order.get("refunded", 0)
    if status in ("done", "refused"):
        changes["closed_at"] = now
    if status == "waiting_client" and thread_id:
        try:
            changes["seen_messages"] = list({m["id"] for m in gmail_helper.get_thread_messages(service, thread_id)}
                                            | set(changes["seen_messages"]))
        except Exception as e:
            print(f"[Retours] Thread {thread_id} unreadable after sending: {e}")
    return database.update_client_return(case_id, status=status, thread_id=thread_id, data=changes)


def set_status(case_id, status, note=None, extra=None):
    case = database.get_client_return(case_id)
    changes = dict(extra or {}, log=_log(case["data"], note or f"passé en « {STATUS_LABELS[status]} »"))
    if status == "to_refund":
        changes["refunded_before"] = (case["data"].get("order") or {}).get("refunded", 0)
    if status in ("done", "refused"):
        changes["closed_at"] = datetime.now(PARIS).isoformat()
    case = database.update_client_return(case_id, status=status, data=changes)
    if status == "to_refund" and case["data"].get("order"):
        # The "refund on its way" email is ready, for when Samuel wants to send one.
        subject, body = draft(case, "refund")
        case = database.update_client_return(case_id, data={"template": "refund", "mail_subject": subject,
                                                            "mail_body": body, "mail_edited": False})
    return case


def delete(case_id):
    database.delete_client_return(case_id)
    try:
        delete_photos(case_id)
    except OSError:
        pass


# ---------------------------------------------------------------- following the files (scheduler)

def _offer_deadline(data):
    sent = data.get("offer_sent_at")
    start = datetime.fromisoformat(sent).date() if sent else today()
    return start + timedelta(REFUND_AFTER_DAYS)


def refund_deadline(data):
    arrival = _parse_date(data.get("arrival_date"))
    return add_workdays(arrival, REFUND_WITHIN_WORKDAYS) if arrival else None


def _classify(reply, offer_kind, data):
    a = analyse(data)["amounts"]
    offer = (f"un avoir bonifié de {euros(a.get('bonus', 0))} avec la livraison offerte, ou le remboursement"
             if offer_kind == "bonus" else f"un avoir de {euros(a.get('catalogue', 0))}")
    text = claude_ai._call_haiku([{"role": "user", "content": REPLY_PROMPT.format(offer=offer, reply=reply[:3000])}],
                                 max_tokens=200)
    start = text.find("{")
    if start < 0:
        return {"choice": "unclear", "summary": ""}
    info, _ = json.JSONDecoder().raw_decode(text[start:])
    if info.get("choice") not in ("credit", "refund", "unclear"):
        info["choice"] = "unclear"
    return info


def check_replies():
    """Customers' answers to the offers, reminders, and the files left without an answer."""
    waiting = database.get_client_returns(["waiting_client"])
    if not waiting:
        return
    try:
        service = gmail_helper.get_gmail_service()
    except Exception as e:
        print(f"[Retours] Gmail unavailable: {e}")
        return
    for case in waiting:
        data = case["data"]
        try:
            new = []
            if case.get("thread_id"):
                seen = set(data.get("seen_messages") or [])
                new = [m for m in gmail_helper.get_thread_messages(service, case["thread_id"])
                       if m["id"] not in seen and m["direction"] == "received"]
            if new:
                reply = "\n\n".join(gmail_helper._strip_quoted(m["body"]) for m in new).strip()
                info = _classify(reply, data.get("offer_kind") or "bonus", data)
                changes = {"seen_messages": list(seen | {m["id"] for m in new}), "reply_text": reply,
                           "reply_summary": info.get("summary") or "", "reply_choice": info["choice"],
                           "reply_at": datetime.now(PARIS).isoformat()}
                # The SAV dashboard must not draft a second answer to the same email.
                for m in new:
                    database.mark_processed(m["id"])
                database.close_drafts_for_thread(case["thread_id"])
                if info["choice"] == "credit" and data.get("offer_custom"):
                    # The email promised a hand-set amount: Samuel creates the code himself.
                    database.update_client_return(case["id"], status="credit_to_send", data=dict(
                        changes, template="credit_bonus" if (data.get("offer_kind") or "bonus") == "bonus" else "credit",
                        log=_log(data, "le client choisit l'avoir (montant particulier : code à créer)")))
                elif info["choice"] == "credit":
                    database.update_client_return(case["id"], data=dict(changes, log=_log(data, "le client choisit l'avoir")))
                    try:
                        create_credit(case["id"], bonus=(data.get("offer_kind") or "bonus") == "bonus")
                    except Exception as e:
                        print(f"[Retours] Credit for file {case['id']} failed: {e}")
                        database.update_client_return(case["id"], status="reply_to_read",
                                                      data={"error": f"Avoir non créé : {e}"})
                elif info["choice"] == "refund" and (data.get("offer_kind") or "bonus") == "bonus":
                    set_status(case["id"], "to_refund", "le client choisit le remboursement", extra=changes)
                else:
                    database.update_client_return(case["id"], status="reply_to_read",
                                                  data=dict(changes, log=_log(data, "réponse du client à lire")))
                continue
            sent = data.get("offer_sent_at")
            if not sent or (data.get("offer_kind") or "bonus") != "bonus":
                continue
            age = (datetime.now(PARIS) - datetime.fromisoformat(sent)).days
            if age >= REFUND_AFTER_DAYS and data.get("choice") != "credit":
                set_status(case["id"], "to_refund",
                           f"sans réponse après {REFUND_AFTER_DAYS} jours : remboursement selon le bon")
            elif age >= REMINDER_AFTER_DAYS and not data.get("reminder_sent_at") and not data.get("reminder_due"):
                subject, body = draft(case, "reminder")
                database.update_client_return(case["id"], data={
                    "reminder_due": True, "template": "reminder", "mail_subject": subject, "mail_body": body,
                    "log": _log(data, "relance à envoyer")})
        except Exception as e:
            print(f"[Retours] Reply check failed for file {case['id']}: {e}")


def check_refunds():
    """Close the files Samuel has refunded in Shopify."""
    for case in database.get_client_returns(["to_refund"]):
        data = case["data"]
        try:
            order = fetch_order(case.get("order_number"))
            if order and order["refunded"] > float(data.get("refunded_before") or 0) + 0.001:
                done = order["refunded"] - float(data.get("refunded_before") or 0)
                database.update_client_return(case["id"], status="done", data={
                    "order": order, "closed_at": datetime.now(PARIS).isoformat(),
                    "log": _log(data, f"remboursement vu dans Shopify : {euros(done)}")})
        except Exception as e:
            print(f"[Retours] Refund check failed for file {case['id']}: {e}")


def tick():
    check_replies()
    check_refunds()


def counts_for_report():
    """Files waiting for Samuel, for the morning report."""
    cases = database.get_client_returns(["to_check", "reply_to_read", "credit_to_send", "to_refund", "waiting_client"])
    return {
        "À traiter": sum(c["status"] == "to_check" for c in cases),
        "Réponse à lire": sum(c["status"] == "reply_to_read" for c in cases),
        "Avoir à envoyer": sum(c["status"] == "credit_to_send" for c in cases),
        "À rembourser": sum(c["status"] == "to_refund" for c in cases),
        "Relance à envoyer": sum(c["status"] == "waiting_client" and bool(c["data"].get("reminder_due")) for c in cases),
    }
