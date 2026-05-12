import os
import requests

def get_order_by_number(order_number):
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    order_number = str(order_number).replace('#', '')
    url = f"https://{shop}/admin/api/2024-01/orders.json"
    headers = {'X-Shopify-Access-Token': token}
    params = {'name': order_number, 'status': 'any'}
    response = requests.get(url, headers=headers, params=params)
    orders = response.json().get('orders', [])
    if orders:
        return format_order(orders[0])
    return None

def get_orders_by_email(email):
    """Fetch all orders for a given email, expanding via customer_id."""
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    url = f"https://{shop}/admin/api/2024-01/orders.json"
    headers = {'X-Shopify-Access-Token': token}
    params = {'email': email, 'status': 'any', 'limit': 20}
    response = requests.get(url, headers=headers, params=params)
    orders = response.json().get('orders', [])
    if not orders:
        return []
    customer_id = (orders[0].get('customer') or {}).get('id')
    if customer_id:
        return get_all_orders_for_customer(customer_id)
    return [format_order(o) for o in orders]


def get_all_orders_for_customer(customer_id):
    """Fetch all orders for a Shopify customer by their internal customer ID."""
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    url = f"https://{shop}/admin/api/2024-01/orders.json"
    headers = {'X-Shopify-Access-Token': token}
    params = {'customer_id': customer_id, 'status': 'any', 'limit': 50}
    response = requests.get(url, headers=headers, params=params)
    orders = response.json().get('orders', [])
    return [format_order(o) for o in orders]


def get_full_order_history(sender_email, customer_name=None):
    """
    Fetch the COMPLETE order history for a customer.
    Combines: email lookup → customer_id expansion → name search (for multiple Shopify accounts).
    Returns deduplicated list sorted by date desc.
    """
    import sys
    internal_domains = {'maxsauveur.com', 'maxsauveur.fr'}
    seen = {}  # number → order

    print(f"[ORDER_LOOKUP] email={sender_email!r} name={customer_name!r}", file=sys.stderr, flush=True)

    # Step 1: by email (+ customer_id expansion)
    if sender_email and not any(d in sender_email.lower() for d in internal_domains):
        by_email = get_orders_by_email(sender_email)
        print(f"[ORDER_LOOKUP] by_email: {[o['number'] for o in by_email]}", file=sys.stderr, flush=True)
        for o in by_email:
            seen[o['number']] = o
        # Augment customer_name from Shopify if we only have a partial name (e.g. "Alex" → "Alex Frau")
        if by_email and (not customer_name or len(customer_name.split()) < 2):
            for o in by_email:
                cn = o.get('customer_name', '')
                if cn and len(cn.split()) >= 2:
                    print(f"[ORDER_LOOKUP] augmenting name from order: {customer_name!r} → {cn!r}", file=sys.stderr, flush=True)
                    customer_name = cn
                    break

    # Step 2: by customer name — try multiple strategies to find all accounts
    if customer_name:
        parts = [p for p in customer_name.split() if len(p) >= 3 and '@' not in p]
        search_queries = []
        if len(parts) >= 2:
            search_queries.append(customer_name)
            search_queries.append(parts[-1])
            search_queries.append(f"last_name:{parts[-1]}")
        elif len(parts) == 1:
            search_queries.append(parts[0])

        seen_customer_ids = set()
        for query in search_queries:
            customers = search_customers_by_name(query)
            print(f"[ORDER_LOOKUP] query={query!r} → {len(customers)} customers: {[c['name'] for c in customers]}", file=sys.stderr, flush=True)
            for c in customers[:5]:
                if any(d in (c.get('email') or '').lower() for d in internal_domains):
                    continue
                cid = c.get('id')
                if not cid or cid in seen_customer_ids:
                    continue
                seen_customer_ids.add(cid)
                orders_for_c = get_all_orders_for_customer(cid)
                print(f"[ORDER_LOOKUP] customer {c['name']} ({c['email']}) → {[o['number'] for o in orders_for_c]}", file=sys.stderr, flush=True)
                for o in orders_for_c:
                    if o['number'] not in seen:
                        seen[o['number']] = o

        # Step 2b: GraphQL search by billing last name — finds guest orders too
        if len(parts) >= 2:
            last_name = parts[-1]
            graphql_orders = search_orders_by_billing_name(last_name)
            print(f"[ORDER_LOOKUP] graphql billing_name={last_name!r} → {[o['number'] for o in graphql_orders]}", file=sys.stderr, flush=True)
            for o in graphql_orders:
                if any(d in (o.get('customer_email') or '').lower() for d in internal_domains):
                    continue
                if o['number'] not in seen:
                    seen[o['number']] = o

    result = list(seen.values())
    result.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    print(f"[ORDER_LOOKUP] FINAL: {[o['number'] for o in result]}", file=sys.stderr, flush=True)
    return result

def search_customers_by_name(name):
    """Search Shopify customers by name, return list of {id, email, name, orders_count}."""
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    url = f"https://{shop}/admin/api/2024-01/customers/search.json"
    headers = {'X-Shopify-Access-Token': token}
    params = {'query': name, 'limit': 5}
    response = requests.get(url, headers=headers, params=params)
    customers = response.json().get('customers', [])
    return [
        {
            'id': c.get('id'),
            'email': c.get('email', ''),
            'name': f"{c.get('first_name', '')} {c.get('last_name', '')}".strip(),
            'orders_count': c.get('orders_count', 0),
        }
        for c in customers
    ]


def get_all_orders_by_customer_name(name):
    """Find ALL orders across all Shopify customer accounts matching a name.
    Handles the case where the same person has ordered with multiple email addresses."""
    internal_domains = {'maxsauveur.com', 'maxsauveur.fr'}
    customers = search_customers_by_name(name)
    all_orders = []
    seen_numbers = set()
    for c in customers[:5]:
        if any(d in (c.get('email') or '').lower() for d in internal_domains):
            continue
        customer_id = c.get('id')
        if not customer_id:
            continue
        for o in get_all_orders_for_customer(customer_id):
            if o['number'] not in seen_numbers:
                all_orders.append(o)
                seen_numbers.add(o['number'])
    # Sort by date descending
    all_orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    return all_orders


def search_orders_by_billing_name(last_name):
    """Search Shopify orders by billing last name using GraphQL — finds guest orders too."""
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    url = f"https://{shop}/admin/api/2024-01/graphql.json"
    headers = {'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'}
    query = """
    {
      orders(first: 20, query: "billing_name:%s status:any") {
        edges {
          node {
            name
            createdAt
            email
            financialStatus
            fulfillmentStatus
            totalPriceSet { shopMoney { amount currencyCode } }
            customer { id }
            shippingAddress { firstName lastName }
            lineItems(first: 10) {
              edges {
                node {
                  name
                  quantity
                  originalUnitPriceSet { shopMoney { amount } }
                }
              }
            }
            fulfillments {
              trackingInfo { number url }
            }
          }
        }
      }
    }
    """ % last_name.replace('"', '')
    try:
        resp = requests.post(url, headers=headers, json={'query': query}, timeout=10)
        edges = resp.json().get('data', {}).get('orders', {}).get('edges', [])
        result = []
        for edge in edges:
            node = edge['node']
            tracking_number = None
            tracking_url = None
            for f in node.get('fulfillments', []):
                for t in f.get('trackingInfo', []):
                    tracking_number = t.get('number')
                    tracking_url = t.get('url')
            shipping = node.get('shippingAddress') or {}
            products = []
            for li_edge in node.get('lineItems', {}).get('edges', []):
                li = li_edge['node']
                price = float((li.get('originalUnitPriceSet') or {}).get('shopMoney', {}).get('amount') or 0)
                products.append({'name': li['name'], 'qty': li['quantity'], 'price': price, 'total': price * li['quantity'], 'discount': 0})
            total_money = node.get('totalPriceSet', {}).get('shopMoney', {})
            result.append({
                'number': node['name'],
                'status': (node.get('financialStatus') or '').lower(),
                'fulfillment_status': (node.get('fulfillmentStatus') or 'unfulfilled').lower(),
                'created_at': (node.get('createdAt') or '')[:10],
                'total': f"{total_money.get('amount', '0')} {total_money.get('currencyCode', 'EUR')}",
                'customer_name': f"{shipping.get('firstName', '')} {shipping.get('lastName', '')}".strip(),
                'customer_email': node.get('email', ''),
                'tracking_number': tracking_number,
                'tracking_url': tracking_url,
                'currency': total_money.get('currencyCode', 'EUR'),
                'shipping': 0,
                'discount_total': 0,
                'products': products,
            })
        return result
    except Exception:
        return []


def search_product_price(query):
    """Search products by name and return current prices from Shopify catalog."""
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    url = f"https://{shop}/admin/api/2024-01/products.json"
    headers = {'X-Shopify-Access-Token': token}
    params = {'title': query, 'limit': 5}
    response = requests.get(url, headers=headers, params=params)
    products = response.json().get('products', [])
    result = []
    for p in products:
        for v in p.get('variants', []):
            result.append({
                'product': p['title'],
                'variant': v.get('title', ''),
                'price': float(v.get('price', 0)),
                'compare_at_price': float(v.get('compare_at_price') or 0),
                'sku': v.get('sku', ''),
                'available': (v.get('inventory_quantity') or 0) > 0,
            })
    return result


def format_order(order):
    tracking_number = None
    tracking_url = None
    fulfillments = order.get('fulfillments', [])
    if fulfillments:
        last = fulfillments[-1]
        tracking_number = last.get('tracking_number')
        tracking_url = last.get('tracking_url')
    return {
        'number': order.get('name'),
        'status': order.get('financial_status'),
        'fulfillment_status': order.get('fulfillment_status', 'unfulfilled'),
        'created_at': order.get('created_at', '')[:10],
        'total': f"{order.get('total_price', '0')} {order.get('currency', 'EUR')}",
        'customer_name': f"{order.get('customer', {}).get('first_name', '')} {order.get('customer', {}).get('last_name', '')}".strip(),
        'customer_email': order.get('email', ''),
        'tracking_number': tracking_number,
        'tracking_url': tracking_url,
        'currency': order.get('currency', 'EUR'),
        'shipping': float((order.get('total_shipping_price_set') or {}).get('shop_money', {}).get('amount') or 0),
        'discount_total': float(order.get('total_discounts') or 0),
        'products': [
            {
                'name': item.get('name', ''),
                'qty': item.get('quantity', 1),
                'price': float(item.get('price') or 0),
                'total': float(item.get('price') or 0) * item.get('quantity', 1),
                'discount': sum(float(d.get('amount') or 0) for d in item.get('discount_allocations', [])),
            }
            for item in order.get('line_items', [])
        ],
    }
