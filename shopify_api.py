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
    params = {'email': email, 'status': 'any', 'limit': 3}
    response = requests.get(url, headers=headers, params=params)
    orders = response.json().get('orders', [])
    return [format_order(o) for o in orders]

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
        'shipping': float(order.get('total_shipping_price_set', {}).get('shop_money', {}).get('amount', 0)),
        'discount_total': float(order.get('total_discounts', 0)),
        'products': [
            {
                'name': item['name'],
                'qty': item['quantity'],
                'price': float(item.get('price', 0)),
                'total': float(item.get('price', 0)) * item['quantity'],
                'discount': sum(float(d.get('amount', 0)) for d in item.get('discount_allocations', [])),
            }
            for item in order.get('line_items', [])
        ],
    }
