"""Repair return labels, on the second Wing account (WING_REP_*).

Reproduces Samuel's manual routine: duplicate the template order
#10452_REPARATION (addressed to SSL, the logistics centre), rename it after the
customer's Shopify order, then generate the Colissimo label. The customer uses
that label to send the pair back to SSL.

This account is also synced with Shopify, but its Shopify orders are
incomplete: never look customer orders up here. The only orders read are the
template and our own *_REPARATION references.
"""
import os
import time
from datetime import datetime, timezone

import requests

WING_API_URL = "https://api-developer.wing.eu/v3"
TEMPLATE_REF = "#10452_REPARATION"
REF_SUFFIXES = ["_REPARATION", "_REPARATION_BIS", "_REPARATION_TER",
                "_REPARATION_QUATER", "_REPARATION_5", "_REPARATION_6"]


def _gql(query, variables=None, token=None):
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(WING_API_URL, json=payload, headers=headers, timeout=30)
    if not resp.ok:
        print(f"[WingRepair] HTTP {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()


_token_cache = {"value": None, "at": 0.0}
TOKEN_TTL = 20 * 60


def _get_token():
    # Wing answers "too-many-attempts" when logins come close together, so one
    # token is reused for a while instead of logging in on every call.
    if _token_cache["value"] and time.time() - _token_cache["at"] < TOKEN_TTL:
        return _token_cache["value"]
    query = """
    mutation($email: EmailAddress!, $password: String!) {
      createAccessToken(email: $email, password: $password) { accessToken }
    }
    """
    variables = {"email": os.getenv('WING_REP_EMAIL'), "password": os.getenv('WING_REP_PASSWORD')}
    for attempt in range(3):
        data = _gql(query, variables)
        token = ((data.get('data') or {}).get('createAccessToken') or {}).get('accessToken')
        if token:
            _token_cache.update(value=token, at=time.time())
            return token
        print(f"[WingRepair] Auth attempt {attempt + 1} failed: {data.get('errors')}")
        time.sleep(5)
    raise Exception("Connexion impossible au compte Wing réparation")


ORDER_FIELDS = """
  id ref status service
  recipient { firstName lastName company phone email line1 line2 additional city zip countryCode }
  orderProducts { quantity product { designation price weight } }
  fulfillmentOrders {
    id status service
    parcels { id trackingId labelURL trackingURL status organizationExpeditorId }
  }
"""


def _find_order(token, ref):
    """Exact match on the reference; Wing's search is fuzzy."""
    query = """
    query($search: String!) {
      orders(input: { filter: { search: $search }, limit: 20 }) { """ + ORDER_FIELDS + """ }
    }
    """
    data = _gql(query, {"search": ref.lstrip('#')}, token)
    if 'errors' in data:
        raise Exception(f"Recherche Wing '{ref}' : {data['errors']}")
    orders = (data.get('data') or {}).get('orders') or []
    return next((o for o in orders if (o.get('ref') or '').upper() == ref.upper()), None)


def _pickup_id(token, expeditor_id):
    """The pickup address the template's label was issued from (SSL)."""
    data = _gql("""
    { organizationPickups(input: { limit: 50 }) { id organizationExpeditorId deactivatedAt isLastVersion } }
    """, token=token)
    pickups = (data.get('data') or {}).get('organizationPickups') or []
    for p in pickups:
        if p['organizationExpeditorId'] == expeditor_id and not p['deactivatedAt'] and p['isLastVersion']:
            return p['id']
    raise Exception("Adresse d'enlèvement SSL introuvable sur le compte Wing réparation")


def next_free_ref(order_number, token=None):
    """#13617_REPARATION, or _BIS, _TER... if that order already came back for repair."""
    token = token or _get_token()
    number = str(order_number).strip().lstrip('#')
    for suffix in REF_SUFFIXES:
        ref = f"#{number}{suffix}"
        if not _find_order(token, ref):
            return ref
    raise Exception(f"Trop de réparations déjà ouvertes sur la commande #{number}")


def cancel_label(ref, token=None):
    """Void the label of one of our repair orders; the order itself stays in Wing.

    Refuses anything but our own references, so a typo can never touch a
    customer's Shopify order on this account.
    """
    if not any(ref.upper().endswith(s) for s in REF_SUFFIXES) and not ref.upper().endswith('_REP'):
        raise Exception(f"{ref} n'est pas une référence de réparation, annulation refusée")
    token = token or _get_token()
    order = _find_order(token, ref)
    if not order:
        raise Exception(f"Commande {ref} introuvable")
    result = _gql("""
    mutation($input: CancelFulfillmentOrderParcelsInput!) {
      cancelFulfillmentOrderParcels(input: $input) {
        fulfillmentOrders { id status parcels { trackingId } }
      }
    }
    """, {"input": {"fulfillmentOrderIds": [fo['id'] for fo in order['fulfillmentOrders']]}}, token)
    if 'errors' in result:
        raise Exception(f"Annulation de {ref} refusée : {result['errors']}")
    fos = result['data']['cancelFulfillmentOrderParcels']['fulfillmentOrders']
    return {"ref": ref, "remaining_parcels": sum(len(fo['parcels']) for fo in fos)}


def create_repair_label(order_number, weight_kg=None, designation=None):
    """Duplicate the template, rename it, generate the label.

    Returns {ref, order_id, tracking_id, label_url, tracking_url}.
    Every call creates a real, billed Colissimo label.
    """
    token = _get_token()
    template = _find_order(token, TEMPLATE_REF)
    if not template:
        raise Exception(f"Commande modèle {TEMPLATE_REF} introuvable sur le compte Wing réparation")

    ref = next_free_ref(order_number, token)

    r = template['recipient']
    recipient = {k: r[k] for k in ('firstName', 'lastName', 'company', 'phone', 'email',
                                   'line1', 'line2', 'additional', 'city', 'zip', 'countryCode') if r.get(k)}
    products = []
    for op in template['orderProducts']:
        p = op['product']
        products.append({
            "designation": designation or p['designation'],
            "price": p['price'],
            "quantity": op['quantity'],
            "weight": weight_kg if weight_kg is not None else p['weight'],
        })
    service = template['fulfillmentOrders'][0]['service']
    expeditor_id = template['fulfillmentOrders'][0]['parcels'][0]['organizationExpeditorId']

    created = _gql("""
    mutation($input: OrderToCreateInput!) {
      createOrder(input: $input) { id ref fulfillmentOrders { id } }
    }
    """, {"input": {
        "ref": ref,
        "orderedAt": datetime.now(timezone.utc).isoformat(),
        "recipient": recipient,
        "fulfillmentOrders": [{"products": products, "service": service}],
    }}, token)
    if 'errors' in created:
        raise Exception(f"Création de la commande {ref} refusée : {created['errors']}")
    order = created['data']['createOrder']
    fo_id = order['fulfillmentOrders'][0]['id']
    print(f"[WingRepair] Order {ref} created ({order['id']})")

    labelled = _gql("""
    mutation($input: CreateFulfillmentParcelInput!) {
      createFulfillmentParcel(input: $input) {
        id status parcels { trackingId labelURL trackingURL }
      }
    }
    """, {"input": {"fulfillmentOrderId": fo_id,
                    "organizationPickupId": _pickup_id(token, expeditor_id)}}, token)
    if 'errors' in labelled:
        # The order exists but has no label: Samuel can finish it by hand in Wing.
        raise Exception(f"Commande {ref} créée mais étiquette refusée : {labelled['errors']}")
    parcel = labelled['data']['createFulfillmentParcel']['parcels'][-1]
    print(f"[WingRepair] Label for {ref}: {parcel['trackingId']}")

    return {
        "ref": ref,
        "order_id": order['id'],
        "tracking_id": parcel['trackingId'],
        "label_url": parcel['labelURL'],
        "tracking_url": parcel.get('trackingURL'),
    }
