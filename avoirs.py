"""Avoirs after a customer return: one Shopify code per returned item, then the
email to the customer.

The amount is the catalogue price of the item before any discount, shipping
excluded, so the customer can exchange it for another pair at no cost. The code
is reserved to the customer's account, single use, valid six months, on the men's
shoes and belts collections, with no minimum purchase.
"""
import calendar
import difflib
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone

import requests

SHOPIFY_API = "2025-01"
COLLECTIONS = ("chaussures", "ceintures")   # Chaussures Homme, Ceintures hommes
VALIDITY_MONTHS = 6
TITLE_PREFIX = "Avoir"
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I


def _gql(query, variables=None):
    shop, token = os.getenv("SHOPIFY_SHOP"), os.getenv("SHOPIFY_TOKEN")
    r = requests.post(f"https://{shop}/admin/api/{SHOPIFY_API}/graphql.json", timeout=60,
                      headers={"X-Shopify-Access-Token": token},
                      json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify : {body['errors']}")
    return body["data"]


def _norm(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def get_order(order_number):
    number = str(order_number).lstrip("#").strip()
    data = _gql("""
        query($q: String!) {
          orders(first: 1, query: $q) { nodes {
            id name email customerLocale
            customer { id email firstName lastName }
            billingAddress { firstName name }
            discountCodes
            lineItems(first: 50) { nodes {
              title variantTitle quantity
              originalUnitPriceSet { shopMoney { amount } }
              variant { price compareAtPrice }
              product { id }
            } }
          } }
        }""", {"q": f"name:#{number}"})
    nodes = data["orders"]["nodes"]
    if not nodes or nodes[0]["name"].lstrip("#") != number:
        raise RuntimeError(f"Commande #{number} introuvable dans Shopify.")
    return nodes[0]


def match_item(order, model):
    """The line item the dictated model name refers to. Dictation mangles names,
    so this is fuzzy, and it refuses to guess between two close candidates."""
    items = order["lineItems"]["nodes"]
    wanted = _norm(model)

    def score(item):
        label = _norm(f"{item['title']} {item['variantTitle'] or ''}")
        words = wanted.split()
        hits = sum(1 for w in words if w in label.split()) / max(len(words), 1)
        return max(hits, difflib.SequenceMatcher(None, wanted, label).ratio())

    ranked = sorted(((score(i), i) for i in items), key=lambda p: p[0], reverse=True)
    listing = "\n".join(f"  - {i['title']} ({i['variantTitle']})" for i in items)
    if not ranked or ranked[0][0] < 0.4:
        raise RuntimeError(f"Aucun article ne correspond à « {model} ». Articles de la commande :\n{listing}")
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.1 and ranked[0][1]["title"] != ranked[1][1]["title"]:
        raise RuntimeError(f"« {model} » est ambigu. Articles de la commande :\n{listing}")
    return ranked[0][1]


def catalogue_price(item):
    """Price shown in the catalogue when the order was placed, before any code
    or automatic discount. Shipping is never part of a line item."""
    return float(item["originalUnitPriceSet"]["shopMoney"]["amount"])


def existing_avoirs(order_name):
    data = _gql("""
        query($q: String!) {
          codeDiscountNodes(first: 20, query: $q) { nodes { codeDiscount {
            ... on DiscountCodeBasic {
              title status endsAt codes(first: 1) { nodes { code } }
              customerGets { value { ... on DiscountAmount { amount { amount } } } }
            }
          } } }
        }""", {"q": f'title:"{TITLE_PREFIX} {order_name}"'})
    found = []
    for n in data["codeDiscountNodes"]["nodes"]:
        d = n.get("codeDiscount") or {}
        if not d.get("title", "").startswith(f"{TITLE_PREFIX} {order_name} "):
            continue
        found.append({"title": d["title"], "status": d["status"],
                      "code": (d["codes"]["nodes"] or [{}])[0].get("code"),
                      "amount": float(((d.get("customerGets") or {}).get("value") or {}).get("amount", {}).get("amount", 0)),
                      "ends_at": datetime.fromisoformat(d["endsAt"].replace("Z", "+00:00")) if d.get("endsAt") else None})
    return found


def _add_months(when, months):
    month = when.month - 1 + months
    year, month = when.year + month // 12, month % 12 + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def prepare(order_number, model, quantity=1, amount=None, model_only=False):
    """Everything needed to show Samuel the avoir before anything is created."""
    order = get_order(order_number)
    item = match_item(order, model)
    customer = order.get("customer")
    if not customer:
        raise RuntimeError(f"La commande {order['name']} n'a pas de fiche client : impossible de réserver le code.")
    unit = catalogue_price(item)
    first_name = customer.get("firstName") or (order.get("billingAddress") or {}).get("firstName") or ""
    ends = _add_months(datetime.now(timezone.utc), VALIDITY_MONTHS)
    return {
        "order": order["name"],
        "email": order.get("email") or customer.get("email"),
        "customer_id": customer["id"],
        "first_name": first_name.strip().title(),
        "full_name": (order.get("billingAddress") or {}).get("name")
                     or f"{customer.get('firstName') or ''} {customer.get('lastName') or ''}".strip(),
        "codes_used": order.get("discountCodes") or [],
        "english": (order.get("customerLocale") or "fr").lower().startswith("en"),
        "item": item["title"],
        "variant": item["variantTitle"],
        "unit_price": unit,
        "current_price": float(item["variant"]["price"]) if item.get("variant") else None,
        "quantity": quantity,
        "amount": round(amount if amount is not None else unit * quantity, 2),
        "ends_at": ends,
        "already": existing_avoirs(order["name"]),
        "product_id": (item.get("product") or {}).get("id"),
        "model_only": model_only,
    }


def _collection_ids():
    ids = []
    for handle in COLLECTIONS:
        coll = _gql("query($h: String!) { collectionByHandle(handle: $h) { id } }",
                    {"h": handle})["collectionByHandle"]
        if not coll:
            raise RuntimeError(f"Collection Shopify « {handle} » introuvable.")
        ids.append(coll["id"])
    return ids


def _items(avoir):
    """Either the returned product only (same model and colour), or the men's
    shoes and belts collections."""
    if avoir["model_only"]:
        if not avoir["product_id"]:
            raise RuntimeError("Produit introuvable dans Shopify : impossible de limiter l'avoir au modèle.")
        return {"products": {"productsToAdd": [avoir["product_id"]]}}
    return {"collections": {"add": _collection_ids()}}


def create_code(avoir):
    """The avoir code. With avoir["free_shipping"], it combines with the
    free-shipping code from create_shipping_code (Shopify cannot put shipping
    in an amount code)."""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    data = _gql("""
        mutation($d: DiscountCodeBasicInput!) {
          discountCodeBasicCreate(basicCodeDiscount: $d) {
            codeDiscountNode { id }
            userErrors { field message }
          }
        }""", {"d": {
            "title": f"{TITLE_PREFIX} {avoir['order']} · {avoir['item']} · {avoir['email']}",
            "code": code,
            "startsAt": datetime.now(timezone.utc).isoformat(),
            "endsAt": avoir["ends_at"].isoformat(),
            "customerSelection": {"customers": {"add": [avoir["customer_id"]]}},
            "customerGets": {
                "value": {"discountAmount": {"amount": f"{avoir['amount']:.2f}", "appliesOnEachItem": False}},
                "items": _items(avoir),
            },
            "usageLimit": 1,
            "appliesOncePerCustomer": True,
            "combinesWith": {"orderDiscounts": False, "productDiscounts": False,
                             "shippingDiscounts": bool(avoir.get("free_shipping"))},
        }})
    res = data["discountCodeBasicCreate"]
    if res["userErrors"]:
        raise RuntimeError(f"Shopify a refusé l'avoir : {res['userErrors']}")
    return code


def create_shipping_code(avoir):
    """Free shipping on the order paid with the avoir: same customer, same end
    date, single use, combinable with the avoir code only."""
    code = "LIVRAISON" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
    data = _gql("""
        mutation($d: DiscountCodeFreeShippingInput!) {
          discountCodeFreeShippingCreate(freeShippingCodeDiscount: $d) {
            codeDiscountNode { id }
            userErrors { field message }
          }
        }""", {"d": {
            "title": f"Livraison offerte {avoir['order']} · {avoir['email']}",
            "code": code,
            "startsAt": datetime.now(timezone.utc).isoformat(),
            "endsAt": avoir["ends_at"].isoformat(),
            "customerSelection": {"customers": {"add": [avoir["customer_id"]]}},
            "destination": {"all": True},
            "usageLimit": 1,
            "appliesOncePerCustomer": True,
            "combinesWith": {"orderDiscounts": False, "productDiscounts": True,
                             "shippingDiscounts": False},
        }})
    res = data["discountCodeFreeShippingCreate"]
    if res["userErrors"]:
        raise RuntimeError(f"Shopify a refusé le code livraison : {res['userErrors']}")
    return code


def _euros(amount):
    return f"{amount:.2f}".replace(".", ",").replace(",00", "") + " €"


def email(avoir, code="XXXXXXXXXXXX"):
    """Subject and body, from the « MAIL TYPE AVOIR » template."""
    name = avoir["first_name"]
    if avoir["english"]:
        ends = avoir["ends_at"].strftime("%d/%m/%Y")
        return (f"Your store credit for order {avoir['order']}",
                f"Hello {name},\n\n"
                f"We have received your {avoir['item']} and your store credit of {_euros(avoir['amount'])} is ready.\n\n"
                f"Here is your code, to enter in your basket before completing your order:\n\n{code}\n\n"
                f"Terms of use:\n"
                f"- valid until {ends},\n"
                + (f"- valid only on the {avoir['item']} model, in a single order,\n" if avoir["model_only"]
                   else "- valid on our men's shoes and belts, in a single order,\n")
                + f"- reserved for your email address ({avoir['email']}): please order with this address,\n"
                f"- cannot be combined with any other code or promotion.\n\n"
                f"If you have any questions, I remain at your disposal.\n\n"
                f"John from Max Sauveur")
    ends = avoir["ends_at"].strftime("%d/%m/%Y")
    return (f"Votre avoir pour la commande {avoir['order']}",
            f"Bonjour {name},\n\n"
            f"Nous avons bien reçu votre retour ({avoir['item']}) et venons de générer votre avoir "
            f"d'un montant de {_euros(avoir['amount'])}.\n\n"
            f"Voici votre code à saisir dans votre panier avant de finaliser la commande :\n\n{code}\n\n"
            f"Conditions d'utilisation :\n"
            f"- valable jusqu'au {ends},\n"
            + (f"- utilisable uniquement sur le modèle {avoir['item']}, en une seule commande,\n" if avoir["model_only"]
               else "- utilisable sur nos chaussures et ceintures homme, en une seule commande,\n")
            + f"- réservé à votre adresse e-mail ({avoir['email']}) : pensez à commander avec cette adresse,\n"
            f"- non cumulable avec d'autres codes ou promotions.\n\n"
            f"Si vous avez la moindre question, je reste disponible.\n\n"
            f"John de Max Sauveur")
