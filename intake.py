"""Open a repair file for a pair that arrived without the 🔧 button.

Pairs sent back before the repair circuit existed (or with a label made by
hand) have no file. Samuel gives the Shopify order, which pair, the problem
and the decision; the file starts at "Paire réceptionnée" and joins the
normal circuit. The customer's latest email thread is attached so their
photos go to the workshop.
"""
import re
import unicodedata

import database
import gmail as gmail_helper
import repair_catalog
import repairs
import shopify_api
import wing_repair


def _words(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", text))


def pick_product(products, hint):
    """The order line that best matches Samuel's words ("Paul noir")."""
    shoes = [p for p in products if repair_catalog.parcel_type(p["name"]) != "accessoire"] or products
    if len(shoes) == 1 or not hint:
        return shoes[0] if len(shoes) == 1 else None
    wanted = _words(hint)
    scored = sorted(((len(wanted & _words(p["name"])), p) for p in shoes), key=lambda x: -x[0])
    if scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]


def _existing_label(order_number):
    """The repair account's label for this order, if one was made by hand earlier."""
    token = wing_repair._get_token("rep")
    number = str(order_number).lstrip("#")
    for suffix in wing_repair.REF_SUFFIXES:
        for ref in (f"#{number}{suffix}", f"{number}{suffix}"):
            order = wing_repair._find_order(token, ref)
            if order:
                parcels = [p for fo in order["fulfillmentOrders"] for p in fo["parcels"]]
                return order["ref"], parcels[-1] if parcels else None
    return None, None


def _latest_email_id(customer_email):
    try:
        service = gmail_helper.get_gmail_service()
        res = service.users().messages().list(userId="me", q=f"from:{customer_email}", maxResults=1).execute()
        msgs = res.get("messages", [])
        return msgs[0]["id"] if msgs else None
    except Exception as e:
        print(f"[Intake] Gmail search failed for {customer_email}: {e}")
        return None


def add_received_pair(order_number, product_hint, problem, decision):
    number = str(order_number).replace("#", "").replace(" ", "").strip()
    if not number.isdigit():
        raise ValueError("Numéro de commande invalide")
    if decision not in ("repair", "credit"):
        raise ValueError("Choisis réparation ou avoir")
    order = shopify_api.get_order_by_number(number)
    if not order:
        raise ValueError(f"Commande #{number} introuvable sur Shopify")
    product = pick_product(order["products"], product_hint)
    if not product:
        names = " · ".join(p["name"] for p in order["products"])
        raise ValueError(f"Précise le modèle, la commande contient : {names}")

    ref, parcel = _existing_label(number)
    if ref and database.get_repair_case_by_ref(ref):
        raise ValueError(f"Un dossier {ref} existe déjà")
    if not ref:
        ref = f"#{number}_REPARATION"
        if database.get_repair_case_by_ref(ref):
            raise ValueError(f"Un dossier {ref} existe déjà")

    ptype = repair_catalog.parcel_type(product["name"])
    weight = repair_catalog.PARCEL_TYPES[ptype]["weight"]
    email = order.get("customer_email") or ""
    # The Shopify customer record can lack a first name; the shipping address rarely does.
    delivery = shopify_api.get_order_delivery(number) or {}
    name = f"{delivery.get('first_name', '')} {delivery.get('last_name', '')}".strip()
    if not name:
        name = " ".join(w for w in (order.get("customer_name") or "").split() if w != "None")
    database.save_repair_case(ref, number, name, email, None,
                              _latest_email_id(email) if email else None, product["name"], ptype, weight,
                              (parcel or {}).get("trackingId") or "", (parcel or {}).get("labelURL") or "")
    case = database.get_repair_case_by_ref(ref)
    maker, maker_ref = repair_catalog.workshop_for(product["name"])
    case, _ = repairs.update_case(case["id"], status="received", problem=problem.strip(), decision=decision,
                                  workshop=maker, workshop_ref=maker_ref,
                                  notes="Paire reçue hors bouton 🔧 (dossier créé à la main)")
    if parcel:
        case = database.update_repair_case(case["id"], {"parcel_status": parcel["status"]})
        repairs.push_to_sheet(case)
    return case
