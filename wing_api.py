import os
import requests

WING_API_URL = "https://api-developer.wing.eu/v3"


def _get_token():
    email = os.getenv('WING_EMAIL')
    password = os.getenv('WING_PASSWORD')
    # Use inline values (no variables) — some GraphQL APIs don't accept variables for auth
    payload = {
        "query": f"""
        mutation {{
          createAccessToken(email: "{email}", password: "{password}") {{
            accessToken
          }}
        }}
        """
    }
    resp = requests.post(WING_API_URL, json=payload, timeout=15)
    print(f"[WingAPI] Auth response {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
    data = resp.json()
    if 'errors' in data:
        raise Exception(f"Auth error: {data['errors']}")
    return data['data']['createAccessToken']['accessToken']


def _gql(query, variables=None, token=None):
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(WING_API_URL, json=payload, headers=headers, timeout=30)
    if not resp.ok:
        print(f"[WingAPI] _gql error {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()


def _find_fulfillment_order_id(token, order_ref):
    """Find Wing fulfillmentOrder ID by Shopify reference number."""
    order_ref = str(order_ref).lstrip('#')
    # Wing API order() only accepts internal ID — must use orders() with filter.search
    query = f"""
    query {{
      orders(input: {{ filter: {{ search: "{order_ref}" }}, limit: 1 }}) {{
        id
        ref
        status
        fulfillmentOrders {{
          id
          status
          service
        }}
      }}
    }}
    """
    result = _gql(query, token=token)
    if 'errors' in result:
        print(f"[WingAPI] orders query errors: {result['errors']}")
        return None, None
    orders = (result.get('data') or {}).get('orders') or []
    if not orders:
        print(f"[WingAPI] No order found for ref={order_ref}")
        return None, None
    order = orders[0]
    fos = order.get('fulfillmentOrders') or []
    fo_id = fos[0]['id'] if fos else None
    service = fos[0].get('service') if fos else None
    print(f"[WingAPI] Found order {order['id']} fo={fo_id} service={service}")
    return order['id'], fo_id


def _get_parcel_fields(token):
    """Introspect Wing API to find available fields on Parcel type."""
    query = '{ __type(name: "Parcel") { fields { name } } }'
    result = _gql(query, token=token)
    fields = [f['name'] for f in ((result.get('data') or {}).get('__type') or {}).get('fields') or []]
    print(f"[WingAPI] Parcel fields: {fields}")
    return fields


def generate_return_label(order_number):
    """Generate a return label via Wing GraphQL API. Returns PDF bytes or None."""
    order_number = str(order_number).lstrip('#')
    try:
        print(f"[WingAPI] Getting token...")
        token = _get_token()

        print(f"[WingAPI] Finding order {order_number}...")
        order_id, fo_id = _find_fulfillment_order_id(token, order_number)
        if not fo_id:
            return None

        # Introspect to find label field name on Parcel
        parcel_fields = _get_parcel_fields(token)
        label_field = next((f for f in parcel_fields if 'label' in f.lower()), None)
        tracking_field = next((f for f in parcel_fields if 'tracking' in f.lower()), 'trackingNumber')
        print(f"[WingAPI] Using label field: {label_field}")

        parcel_subfields = f"id {tracking_field}"
        if label_field:
            parcel_subfields += f" {label_field}"

        print(f"[WingAPI] Creating return parcel for fo={fo_id}...")
        mutation = """
        mutation($foId: ID!) {
          createFulfillmentReturnParcel(input: { fulfillmentOrderId: $foId }) {
            id
            status
            parcels {
              """ + parcel_subfields + """
            }
          }
        }
        """
        result = _gql(mutation, {"foId": fo_id}, token)
        print(f"[WingAPI] Result: {result}")

        if 'errors' in result:
            print(f"[WingAPI] Mutation errors: {result['errors']}")
            return None

        fo = (result.get('data') or {}).get('createFulfillmentReturnParcel') or {}
        parcels = fo.get('parcels') or []

        for parcel in parcels:
            label_url = parcel.get(label_field) if label_field else None
            if label_url and label_url.startswith('http'):
                print(f"[WingAPI] Downloading label: {label_url}")
                pdf_resp = requests.get(label_url, headers={'Authorization': f'Bearer {token}'}, timeout=30)
                if pdf_resp.status_code == 200:
                    print(f"[WingAPI] PDF: {len(pdf_resp.content)} bytes")
                    return pdf_resp.content
                # Retry without auth
                pdf_resp = requests.get(label_url, timeout=30)
                if pdf_resp.status_code == 200:
                    return pdf_resp.content

        print(f"[WingAPI] No label URL in parcels: {parcels}")
        return None

    except Exception as e:
        print(f"[WingAPI] Error: {e}")
        import traceback
        traceback.print_exc()
        return None


def _search_orders_by_ref(token, search_term, limit=5):
    """Search Wing orders by reference text (Shopify order number or variant)."""
    query = """
    query($search: String!, $limit: Int!) {
      orders(input: { filter: { search: $search }, limit: $limit }) {
        id
        ref
        status
        recipient {
          firstName
          lastName
          address {
            line1
            line2
            city
            zip
            country
          }
        }
        fulfillmentOrders {
          id
          status
          service
          parcels {
            id
            trackingNumber
          }
        }
      }
    }
    """
    result = _gql(query, {"search": search_term, "limit": limit}, token)
    if 'errors' in result:
        print(f"[WingAPI] search errors for '{search_term}': {result['errors']}")
        return []
    return (result.get('data') or {}).get('orders') or []


def _format_order_info(order, search_ref=None):
    """Format a Wing order dict into a readable text."""
    lines = []
    ref = order.get('ref') or search_ref or '?'
    lines.append(f"Référence Wing : {ref}")
    lines.append(f"Statut : {order.get('status', '?')}")
    r = order.get('recipient') or {}
    addr = r.get('address') or {}
    name = f"{r.get('firstName', '')} {r.get('lastName', '')}".strip()
    if name:
        lines.append(f"Destinataire : {name}")
    address_parts = [addr.get('line1', ''), addr.get('line2', ''),
                     f"{addr.get('zip', '')} {addr.get('city', '')}".strip(), addr.get('country', '')]
    address_str = ', '.join(p for p in address_parts if p)
    if address_str:
        lines.append(f"Adresse : {address_str}")
    for fo in order.get('fulfillmentOrders') or []:
        service = fo.get('service', '')
        fo_status = fo.get('status', '?')
        lines.append(f"Expédition ({service}) : {fo_status}")
        for parcel in fo.get('parcels') or []:
            tn = parcel.get('trackingNumber', '')
            if tn:
                lines.append(f"Numéro de suivi : {tn}")
    return '\n'.join(lines)


def check_repair_status(order_number):
    """Get repair order status via Wing API (searches by order number variants)."""
    order_number = str(order_number).lstrip('#')
    suffixes = ['_REPARATION', '_bis', '_reparation', '_réparation', '']
    try:
        token = _get_token()
        for suffix in suffixes:
            ref = f"{order_number}{suffix}"
            orders = _search_orders_by_ref(token, ref)
            if not orders:
                continue
            # Pick the most relevant match
            order = next((o for o in orders if ref.lower() in (o.get('ref') or '').lower()), orders[0])
            return _format_order_info(order, ref)
        return None
    except Exception as e:
        print(f"[WingAPI] check_repair_status error: {e}")
        return None


def get_relay_point_from_wing(order_number):
    """Get delivery address / relay point via Wing API."""
    order_number = str(order_number).lstrip('#')
    try:
        token = _get_token()
        orders = _search_orders_by_ref(token, order_number)
        if not orders:
            return None
        order = next((o for o in orders if order_number in (o.get('ref') or '')), orders[0])
        r = order.get('recipient') or {}
        addr = r.get('address') or {}
        parts = [
            f"{r.get('firstName', '')} {r.get('lastName', '')}".strip(),
            addr.get('line1', ''), addr.get('line2', ''),
            f"{addr.get('zip', '')} {addr.get('city', '')}".strip(),
            addr.get('country', '')
        ]
        return '\n'.join(p for p in parts if p)
    except Exception as e:
        print(f"[WingAPI] get_relay_point error: {e}")
        return None
