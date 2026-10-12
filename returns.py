"""Sending a repaired pair back to its customer (step 6).

When a pair is back at the logistics centre, the customer gets a link to a
page where they confirm home delivery or pick a Chronopost or Colissimo relay
(the address may have changed since the order). Their confirmation creates
the shipping order on the main Wing account, like the "_reparation" orders
Samuel used to create by hand, and SSL ships it with the next collection.
"""
import json
import os
import secrets
from datetime import datetime, timedelta

import database
import gmail as gmail_helper
import repair_catalog
import repairs
import shopify_api
import wing_repair

BASE_URL = os.getenv("BASE_URL", "https://assistant-maxsauveur-production.up.railway.app").rstrip("/")
SAMUEL_EMAIL = "samuel@maxsauveur.com"
REMINDER_AFTER = timedelta(days=3)

DELIVERY_OPTIONS = {
    "home": {"label": "À domicile (Colissimo)", "service": "COLISSIMO", "relay": None},
    "chrono_relay": {"label": "En point relais Chronopost", "service": "SHOP2SHOP", "relay": "SHOP2SHOP"},
    "colissimo_relay": {"label": "En point relais Colissimo", "service": "COLISSIMO_RELAY", "relay": "COLISSIMO_RELAY"},
}

_channel_cache = {}


def _first_name(case, delivery=None):
    name = ((delivery or {}).get("first_name") or (case.get("customer_name") or "").split(" ")[0]).strip()
    return name.capitalize() if name.isupper() or name.islower() else name


def return_link(case):
    return f"{BASE_URL}/retour/{case['return_token']}"


def request_return(case):
    """Email the customer that the pair is ready, with the link to confirm the address.

    Sent once per file; changing the status back and forth does not resend it.
    """
    if case.get("return_requested_at") or not case.get("customer_email"):
        return case
    token = case.get("return_token") or secrets.token_urlsafe(16)
    case = database.update_repair_case(case["id"], {"return_token": token})
    delivery = shopify_api.get_order_delivery(case["order_number"]) or {}
    model = repair_catalog.model_name(case.get("product")) or "paire"
    body = f"""Bonjour {_first_name(case, delivery)},

Votre paire {model} est de retour de notre atelier et prête à vous être renvoyée.

Avant l'expédition, merci de nous indiquer où vous souhaitez la recevoir : à domicile, en point relais Chronopost ou en point relais Colissimo. Votre adresse a peut-être changé depuis votre commande.

Confirmez votre adresse de livraison ici :
{return_link(case)}

Votre paire partira dès votre confirmation.

John de Max Sauveur"""
    gmail_helper.send_email(gmail_helper.get_gmail_service(), case["customer_email"],
                            "Votre paire Max Sauveur est prête", body)
    case = database.update_repair_case(case["id"], {"return_requested_at": datetime.utcnow().isoformat()})
    print(f"[Returns] Ready email sent for {case['wing_ref']}")
    return case


def send_reminders():
    """Scheduler job: one reminder when the customer has not answered after three days."""
    now = datetime.utcnow()
    sent = 0
    for case in database.get_repair_cases():
        if (case["status"] != "back_logistics" or not case.get("return_requested_at")
                or case.get("return_confirmed_at") or case.get("return_reminded_at")):
            continue
        if now - datetime.fromisoformat(case["return_requested_at"]) < REMINDER_AFTER:
            continue
        body = f"""Bonjour {_first_name(case)},

Votre paire réparée vous attend toujours. Pour que nous puissions vous l'envoyer, il nous manque simplement votre confirmation d'adresse :
{return_link(case)}

John de Max Sauveur"""
        try:
            gmail_helper.send_email(gmail_helper.get_gmail_service(), case["customer_email"],
                                    "Rappel : votre paire Max Sauveur est prête", body)
            database.update_repair_case(case["id"], {"return_reminded_at": now.isoformat()})
            sent += 1
        except Exception as e:
            print(f"[Returns] Reminder failed for {case['wing_ref']}: {e}")
    return sent


def prefill(case):
    """Address shown on the customer's page: the Shopify shipping address."""
    delivery = shopify_api.get_order_delivery(case["order_number"]) or {}
    delivery.pop("prices", None)
    return delivery


def _channel_id(token):
    """Shopify channel of the main Wing account; the relay search needs one."""
    if "main" not in _channel_cache:
        data = wing_repair._gql("""
        { orders(input: { limit: 20 }) { channelOrder { channelId } } }
        """, token=token)
        ids = [o["channelOrder"]["channelId"] for o in (data.get("data") or {}).get("orders", []) if o.get("channelOrder")]
        if not ids:
            raise Exception("Canal Shopify introuvable sur le compte Wing principal")
        _channel_cache["main"] = ids[0]
    return _channel_cache["main"]


def search_relays(option, address, zip_code, city, country="FR"):
    relay_service = DELIVERY_OPTIONS[option]["relay"]
    token = wing_repair._get_token("main")
    data = wing_repair._gql("""
    query($service: WingServiceRelay!, $address: RelayPointAddressInput!, $channel: ID!) {
      relayPoints(service: $service, address: $address, channelId: $channel, limit: 8) {
        id name address zip city distance
      }
    }
    """, {"service": relay_service, "channel": _channel_id(token),
          "address": {"address": address or "", "zip": zip_code, "city": city, "country": country}}, token)
    if "errors" in data:
        raise Exception(data["errors"][0].get("message", "Recherche impossible"))
    return data["data"]["relayPoints"]


def _phone(raw, country):
    digits = "".join(c for c in (raw or "") if c.isdigit() or c == "+")
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0") and country == "FR":
        return "+33" + digits[1:]
    return digits


def _create_main_order(case, d):
    """Shipping order on the main Wing account, ready for SSL to pick and label."""
    token = wing_repair._get_token("main")
    base = case["wing_ref"] if case["wing_ref"].startswith("#") else "#" + case["wing_ref"]
    ref = next((r for r in (base, base + "_RETOUR", base + "_RETOUR_BIS")
                if not wing_repair._find_order(token, r) and not wing_repair._find_order(token, r.lstrip("#"))), None)
    if not ref:
        raise Exception(f"{base} existe déjà sur le compte principal")
    option = DELIVERY_OPTIONS[d["option"]]
    price = (shopify_api.get_order_delivery(case["order_number"]) or {}).get("prices", {}).get(case.get("product"), 0) or 100
    fulfillment = {
        "service": option["service"],
        "products": [{"designation": f"{case.get('product') or 'Paire'} reparation", "price": price,
                      "quantity": 1, "weight": float(case.get("weight_kg") or 1.5)}],
    }
    if option["relay"]:
        fulfillment.update(carrierPickupId=d["relay_id"], carrierPickupName=d["relay_name"])
    recipient = {k: v for k, v in {
        "firstName": d["first_name"], "lastName": d["last_name"], "company": d.get("company"),
        "phone": _phone(d["phone"], d["country_code"]), "email": d["email"],
        "line1": d["line1"], "line2": d.get("line2"), "city": d["city"], "zip": d["zip"],
        "countryCode": d["country_code"],
    }.items() if v}
    result = wing_repair._gql("""
    mutation($input: OrderToCreateInput!) { createOrder(input: $input) { id ref } }
    """, {"input": {"ref": ref, "orderedAt": datetime.utcnow().isoformat(), "recipient": recipient,
                    "fulfillmentOrders": [fulfillment]}}, token)
    if "errors" in result:
        raise Exception(result["errors"][0].get("message", str(result["errors"])))
    return ref


REQUIRED = {"first_name": "prénom", "last_name": "nom", "line1": "adresse", "zip": "code postal",
            "city": "ville", "country_code": "pays", "phone": "téléphone", "email": "e-mail"}


def confirm_return(case, d):
    """Record the customer's choice, create the Wing order, tell Samuel.

    The choice is saved even if Wing refuses the order: Samuel is then asked
    to create it by hand, and the customer still sees a confirmation.
    """
    if d.get("option") not in DELIVERY_OPTIONS:
        raise ValueError("Choisissez un mode de livraison")
    missing = [label for key, label in REQUIRED.items() if not (d.get(key) or "").strip()]
    if DELIVERY_OPTIONS[d["option"]]["relay"] and not d.get("relay_id"):
        missing.append("point relais")
    if missing:
        raise ValueError("Merci de compléter : " + ", ".join(missing))
    d = {k: (v.strip() if isinstance(v, str) else v) for k, v in d.items()}

    fields = {"return_confirmed_at": datetime.utcnow().isoformat(),
              "return_delivery": json.dumps(d, ensure_ascii=False), "status": "to_send"}
    try:
        fields["return_order_ref"] = _create_main_order(case, d)
        wing_note = f"Commande {fields['return_order_ref']} créée sur le compte Wing principal, à expédier par SSL."
    except Exception as e:
        print(f"[Returns] Wing order failed for {case['wing_ref']}: {e}")
        wing_note = f"⚠️ La commande Wing n'a pas pu être créée ({e}). À créer à la main sur le compte principal."
    case = database.update_repair_case(case["id"], fields)
    repairs.push_to_sheet(case)

    where = DELIVERY_OPTIONS[d["option"]]["label"]
    if d.get("relay_name"):
        where += f" : {d['relay_name']}"
    body = f"""{case['wing_ref']} · {case.get('customer_name') or ''}
{case.get('product') or ''}

Le client a confirmé : {where}
{d['first_name']} {d['last_name']}, {d['line1']} {d.get('line2') or ''}, {d['zip']} {d['city']} ({d['country_code']})
Tél. {d['phone']}

{wing_note}

{BASE_URL}/reparations"""
    try:
        gmail_helper.send_email(gmail_helper.get_gmail_service(), SAMUEL_EMAIL,
                                f"[Réparation] Adresse confirmée {case['wing_ref']}", body)
    except Exception as e:
        print(f"[Returns] Notification failed: {e}")
    return case


def refresh_outbound():
    """Scheduler job: follow the shipping order on the main account to "envoyée" then "reçue"."""
    cases = [c for c in database.get_repair_cases()
             if c["status"] in ("to_send", "sent") and c.get("return_order_ref")]
    if not cases:
        return 0
    token = wing_repair._get_token("main")
    moved = 0
    for case in cases:
        try:
            order = wing_repair._find_order(token, case["return_order_ref"])
            parcels = [p for fo in (order or {}).get("fulfillmentOrders", []) for p in fo["parcels"]]
            if not parcels:
                continue
            status = parcels[-1]["status"]
            new = {"SHIPPED": "sent", "DISPATCHED": "sent", "DELIVERED": "delivered_customer"}.get(status)
            fields = {"return_parcel_status": status}
            if new and new != case["status"]:
                fields["status"] = new
                moved += 1
            if status != case.get("return_parcel_status"):
                repairs.push_to_sheet(database.update_repair_case(case["id"], fields))
        except Exception as e:
            print(f"[Returns] Outbound check failed for {case['wing_ref']}: {e}")
    return moved
