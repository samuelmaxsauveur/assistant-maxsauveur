import os
import requests

def get_order_by_number(order_number):
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    order_number = str(order_number).replace('#', '')
    url = f"https://{shop}/admin/api/2024-01/orders.json"
    headers = {'X-Shopify-Access-Token': token}
    params = {'name': f'#{order_number}', 'status': 'any'}
    response = requests.get(url, headers=headers, params=params)
    orders = response.json().get('orders', [])
    if orders:
        return format_order(orders[0])
    return None

def get_orders_by_email(email):
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    url = f"https://{shop}/admin/api/2024-01/orders.json"
    headers = {'X-Shopify-Access-Token': token}
    params = {'email': email, 'status': 'any', 'limit': 20}
    response = requests.get(url, headers=headers, params=params)
    orders = response.json().get('orders', [])
    return [format_order(o) for o in orders]

def search_customers_by_name(name):
    """Search Shopify customers by name, return list of {email, name, orders}."""
    shop = os.getenv('SHOPIFY_SHOP')
    token = os.getenv('SHOPIFY_TOKEN')
    url = f"https://{shop}/admin/api/2024-01/customers/search.json"
    headers = {'X-Shopify-Access-Token': token}
    params = {'query': name, 'limit': 5}
    response = requests.get(url, headers=headers, params=params)
    customers = response.json().get('customers', [])
    return [
        {
            'email': c.get('email', ''),
            'name': f"{c.get('first_name', '')} {c.get('last_name', '')}".strip(),
            'orders_count': c.get('orders_count', 0),
        }
        for c in customers
    ]


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
