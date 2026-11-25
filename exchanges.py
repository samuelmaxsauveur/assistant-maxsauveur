"""Exchanges from a return: a new order in Wing for what the customer wants instead.

Samuel (25/11/2026): « je veux pouvoir créer une nouvelle commande pour les demandes
d'échange après avoir contacté les clients ... dès que j'ai son accord, avec l'option de
créer sa nouvelle commande dans Wing et l'ajouter dans la prochaine collecte. Mais je
dois aussi pouvoir choisir le produit de son choix, même quand il est différent de sa
commande initiale. Il faut aussi garder le mode avoir. »

The order goes on the main Wing account (samuel@maxsauveur.com) as #13672_ECHANGE,
with the customer of the original order, to the same place (same relay point or same
address) unless Samuel picks another relay. It exists only in Wing, like the dictated
orders. Nothing is created without Samuel's click; the credit flow is untouched.
"""
import re
from datetime import datetime, timezone

import avoirs
import database
import repair_catalog
import retours
import returns
import shopify_api
import wing_repair

SUFFIXES = ("_ECHANGE", "_ECHANGE_BIS", "_ECHANGE_TER")
RELAY = returns.DELIVERY_OPTIONS["chrono_relay"]
HOME = returns.DELIVERY_OPTIONS["home"]
RECIPIENT_KEYS = ("firstName", "lastName", "company", "phone", "email", "line1", "line2",
                  "additional", "city", "zip", "countryCode")


# ---------------------------------------------------------------- products

def catalogue():
    """Products on sale, [{id, title}] (6 h cache shared with the SAV investigation)."""
    import enquete
    return sorted(({"id": p["id"], "title": p["title"]} for p in enquete._catalog_items()),
                  key=lambda p: p["title"].lower())


def product(product_id):
    """A product with every size, its price, stock and barcode."""
    p = avoirs._gql("""query($id: ID!) { product(id: $id) { id title status variants(first: 60) {
        nodes { id title price barcode inventoryQuantity availableForSale } } } }""", {"id": product_id})["product"]
    if not p:
        raise ValueError("Produit introuvable dans Shopify.")
    return {"id": p["id"], "title": p["title"], "active": p.get("status") == "ACTIVE",
            "variants": [{"id": v["id"], "size": v["title"], "price": float(v.get("price") or 0),
                          "barcode": v.get("barcode"), "qty": v.get("inventoryQuantity"),
                          "sellable": bool(v.get("availableForSale"))} for v in p["variants"]["nodes"]]}


def _variant(variant_id):
    v = avoirs._gql("""query($id: ID!) { productVariant(id: $id) { id title price barcode inventoryQuantity
        product { id title } } }""", {"id": variant_id})["productVariant"]
    if not v:
        raise ValueError("Article introuvable dans Shopify.")
    return {"id": v["id"], "size": v["title"], "price": float(v.get("price") or 0), "barcode": v.get("barcode"),
            "qty": v.get("inventoryQuantity"), "product_id": v["product"]["id"], "title": v["product"]["title"]}


def wish(data):
    """What the customer asked for, in his words: his answer, else the slip."""
    slip = data.get("slip") or {}
    comment = slip.get("comment") or ""
    return (data.get("exchange_wish") or slip.get("exchange_wish")
            or (comment if re.search(r"[ée]change", comment, re.I) else "") or "").strip()


def _size_in(text, sizes):
    """The size the customer wrote (« en 43 », « T42,5 », « 95 cm »), if the product has it."""
    for raw in re.findall(r"(?<![\d])(\d{2,3}(?:[.,]5)?)(?![\d])", text or ""):
        wanted = raw.replace(",", ".")
        for s in sizes:
            if re.sub(r"[^\d.]", "", s.replace(",", ".")) == wanted:
                return s
    return None


def suggestion(case):
    """The product and size to preselect: what he asks for if it names a model, else the returned one."""
    import enquete
    data = case["data"]
    order = data.get("order") or {}
    text = wish(data)
    returned = retours.analyse(data)["returned"]
    named = enquete._mentioned_products([text], limit=1) if text else []
    line = (returned or order.get("lines") or [{}])[0]
    pid = named[0] if named else line.get("product_id")
    if not pid:
        return {"wish": text, "product": None, "variant_id": None}
    p = product(pid)
    sizes = [v["size"] for v in p["variants"]]
    size = _size_in(text, sizes) or (line.get("variant") if not named and not text else None)
    picked = next((v["id"] for v in p["variants"] if v["size"] == size), None)
    return {"wish": text, "product": p, "variant_id": picked,
            "returned_paid": round(sum(l["paid"] * l["qty"] for l in returned), 2) if returned else None}


# ---------------------------------------------------------------- where it goes, and when

def _original(token, number):
    return wing_repair._find_order(token, f"#{number}") or wing_repair._find_order(token, str(number))


def delivery(case):
    """Where the original order went, as Wing has it (the exchange goes to the same place by default)."""
    order = case["data"].get("order") or {}
    token = wing_repair._get_token("main")
    original = _original(token, order.get("number"))
    if original:
        r = original["recipient"]
        fo = (original.get("fulfillmentOrders") or [{}])[0]
        if fo.get("carrierPickupId"):
            return {"relay": True, "where": f"point relais {fo.get('carrierPickupName') or ''}".strip(), "source": "wing"}
        return {"relay": False, "where": f"{r.get('line1', '')}, {r.get('zip', '')} {r.get('city', '')}", "source": "wing"}
    d = shopify_api.get_order_delivery(order.get("number")) or {}
    if not d.get("line1"):
        return {"relay": False, "where": None, "source": None}
    return {"relay": False, "where": f"{d['line1']}, {d['zip']} {d['city']} (adresse Shopify, à domicile)", "source": "shopify"}


def collects():
    """The collects still to come on the main Wing account, soonest first."""
    token = wing_repair._get_token("main")
    r = wing_repair._gql("{ collects(input: { limit: 30 }) { id status shouldStartAt parcelsCount "
                         "organizationPickup { friendlyName } } }", token=token)
    if "errors" in r:
        raise RuntimeError(f"Collectes Wing illisibles : {r['errors'][0].get('message')}")
    now = datetime.now(timezone.utc)
    upcoming = []
    for c in r["data"]["collects"]:
        at = datetime.fromisoformat(c["shouldStartAt"].replace("Z", "+00:00")) if c.get("shouldStartAt") else None
        if c.get("status") == "INITIATED" and at and at > now:
            local = at.astimezone(retours.PARIS)
            upcoming.append({"id": c["id"], "at": at.isoformat(), "parcels": c.get("parcelsCount") or 0,
                             "label": f"{retours.fr_date(local.date())} à {local.strftime('%Hh%M').replace('h00', 'h')}"
                                      f" ({c.get('parcelsCount') or 0} colis)"})
    return sorted(upcoming, key=lambda c: c["at"])[:6]


def _add_to_collect(token, collect_id, fo_ids):
    r = wing_repair._gql("""mutation($input: AddFulfillmentOrdersToCollectInput!) {
      addFulfillmentOrdersToCollect(input: $input) {
        collect { id status shouldStartAt parcelsCount }
        error { archivedIds notFoundIds notNewIds notLabelledIds statusNotAllowedIds }
        fulfillmentOrders { id status } } }""", {"input": {"collectId": collect_id, "fulfillmentOrderIds": fo_ids}}, token)
    if "errors" in r:
        raise RuntimeError(r["errors"][0].get("message", r["errors"]))
    res = r["data"]["addFulfillmentOrdersToCollect"]
    refused = {k: v for k, v in (res.get("error") or {}).items() if v}
    if refused:
        raise RuntimeError(f"Wing refuse l'ajout ({', '.join(refused)})")
    return res["collect"]


# ---------------------------------------------------------------- the order

def _free_ref(token, number, case_id):
    """#13672_ECHANGE, or _BIS... when another file already used it. A reference that exists in Wing
    without any file holding it comes from an attempt that went wrong: Samuel checks it first."""
    held = {(c["data"].get("exchange") or {}).get("ref") for c in database.get_client_returns() if c["id"] != case_id}
    for suffix in SUFFIXES:
        ref = f"#{number}{suffix}"
        found = wing_repair._find_order(token, ref) or wing_repair._find_order(token, ref.lstrip("#"))
        if not found:
            return ref
        if found.get("ref") not in held and f"#{found.get('ref', '').lstrip('#')}" not in held:
            raise RuntimeError(f"La commande {found['ref']} existe déjà dans Wing sans dossier qui la porte "
                               f"(une création précédente ?) : vérifie-la dans Wing avant d'en créer une autre.")
    raise RuntimeError(f"Trop d'échanges déjà créés pour la commande #{number}.")


def create(case_id, variant_id, quantity=1, relay_id=None, relay_name=None, collect_id=None):
    """The exchange order in Wing, added to the collect Samuel chose. Only from his click."""
    case = database.get_client_return(case_id)
    data = case["data"]
    order = data.get("order")
    if (data.get("exchange") or {}).get("ref"):
        raise RuntimeError(f"L'échange {data['exchange']['ref']} existe déjà dans Wing.")
    pending = data.get("exchange_pending")
    if pending and (datetime.now(retours.PARIS) - datetime.fromisoformat(pending)).total_seconds() < 180:
        # A restart can cut a creation short: after 3 minutes, _free_ref() finds what it left in Wing.
        raise RuntimeError("Création déjà en cours : attends quelques secondes puis recharge la fiche.")
    if not order:
        raise RuntimeError("Commande introuvable : saisis d'abord le numéro de commande.")
    quantity = max(1, int(quantity or 1))
    v = _variant(variant_id)
    database.update_client_return(case_id, data={"exchange_pending": datetime.now(retours.PARIS).isoformat()})
    try:
        token = wing_repair._get_token("main")
        original = _original(token, order["number"])
        fo = (original.get("fulfillmentOrders") or [{}])[0] if original else {}
        if original:
            r = original["recipient"]
            recipient = {k: r[k] for k in RECIPIENT_KEYS if r.get(k)}
        else:
            d = shopify_api.get_order_delivery(order["number"]) or {}
            if not d.get("line1"):
                raise RuntimeError(f"Commande #{order['number']} introuvable dans Wing, et pas d'adresse dans Shopify.")
            recipient = {k: val for k, val in {
                "firstName": d["first_name"], "lastName": d["last_name"], "company": d.get("company"),
                "phone": returns._phone(d.get("phone"), d.get("country_code") or "FR"), "email": d.get("email"),
                "line1": d["line1"], "line2": d.get("line2"), "city": d["city"], "zip": d["zip"],
                "countryCode": d.get("country_code") or "FR"}.items() if val}
        if relay_id:
            fulfillment = {"service": RELAY["service"], "carrierPickupId": relay_id, "carrierPickupName": relay_name or ""}
            where, to_relay = f"point relais {relay_name}", True
        elif fo.get("carrierPickupId"):
            fulfillment = {"service": fo.get("service") or RELAY["service"], "carrierPickupId": fo["carrierPickupId"],
                           "carrierPickupName": fo.get("carrierPickupName") or ""}
            where, to_relay = f"point relais {fo.get('carrierPickupName') or ''}".strip(), True
        else:
            to_relay = False
            fulfillment = {"service": (fo.get("service") or (original or {}).get("service") or HOME["service"])}
            where = f"{recipient.get('line1', '')}, {recipient.get('zip', '')} {recipient.get('city', '')}"
        designation = f"{v['title']} - {v['size']}"
        weight = repair_catalog.PARCEL_TYPES[repair_catalog.parcel_type(v["title"])]["weight"]
        fulfillment["products"] = [{"designation": designation, "price": v["price"] or 100, "weight": weight,
                                    "quantity": quantity, **({"ean": v["barcode"]} if v.get("barcode") else {})}]
        ref = _free_ref(token, order["number"], case_id)
        result = wing_repair._gql("""
        mutation($input: OrderToCreateInput!) { createOrder(input: $input) { id ref fulfillmentOrders { id status } } }
        """, {"input": {"ref": ref, "orderedAt": datetime.now(timezone.utc).isoformat(), "recipient": recipient,
                        "fulfillmentOrders": [fulfillment]}}, token)
        if "errors" in result:
            raise RuntimeError(f"Wing a refusé la commande {ref} : {result['errors'][0].get('message', result['errors'])}")
    except Exception:
        database.update_client_return(case_id, data={"exchange_pending": None})
        raise
    created = result["data"]["createOrder"]
    print(f"[Retours] Exchange {ref} created in Wing for file {case_id}: {quantity} x {designation}")
    exchange = {"ref": ref, "wing_id": created["id"], "item": designation, "title": v["title"], "size": v["size"],
                "quantity": quantity, "price": v["price"], "where": where, "relay": to_relay,
                "stock_before": v["qty"], "created_at": datetime.now(retours.PARIS).isoformat()}
    log = f"échange créé dans Wing : {ref}, {quantity} × {designation}, vers {where}"
    if collect_id:
        try:
            collect = _add_to_collect(token, collect_id, [f["id"] for f in created["fulfillmentOrders"]])
            at = datetime.fromisoformat(collect["shouldStartAt"].replace("Z", "+00:00")).astimezone(retours.PARIS)
            exchange["collect_at"] = at.isoformat()
            log += f" · ajouté à la collecte du {retours.fr_date(at.date())}"
        except Exception as e:
            print(f"[Retours] {ref} not added to the collect: {e}")
            exchange["collect_error"] = str(e)
            log += f" · pas ajouté à la collecte ({e})"
    if data.get("scheduled"):
        # Written before the exchange: it must not leave on its own now.
        retours.cancel_scheduled(case_id, "envoi programmé annulé : échange créé")
    case = database.update_client_return(case_id, status="to_reship", data={
        "exchange": exchange, "exchange_pending": None, "reship_ref": ref, "reship_relay": where,
        "log": retours._log(data, log)})
    subject, body = retours.draft(case, "exchange_sent")
    return database.update_client_return(case_id, data={"template": "exchange_sent", "mail_subject": subject,
                                                        "mail_body": body, "mail_edited": False})
