import re
from flask import Blueprint, request, jsonify, render_template
import gmail as gmail_helper
import shopify_api
import database

client_bp = Blueprint('client_bp', __name__)


def _extract_order_number(query):
    """Extract a pure order number from queries like #1161, 1161_REPARATION, #1161_réparation."""
    q = query.strip().lstrip('#')
    # Take only the leading digits
    m = re.match(r'^(\d+)', q)
    return m.group(1) if m else None


@client_bp.route('/client')
def client_page():
    return render_template('client.html')


@client_bp.route('/client/debug-shopify')
def debug_shopify():
    """Test Shopify connection — remove after debugging."""
    import os
    import requests as req
    import traceback
    shop = os.getenv('SHOPIFY_SHOP', '')
    token = os.getenv('SHOPIFY_TOKEN', '')
    results = {
        'shop_configured': bool(shop),
        'token_configured': bool(token),
        'shop_value': shop[:40],
    }
    # Raw API call to see exact response
    try:
        url = f"https://{shop}/admin/api/2024-01/orders.json"
        resp = req.get(url, headers={'X-Shopify-Access-Token': token},
                       params={'name': '#11161', 'status': 'any'}, timeout=10)
        results['shopify_status_code'] = resp.status_code
        results['shopify_response'] = resp.json()
    except Exception:
        results['shopify_raw_error'] = traceback.format_exc()
    try:
        sav = database.get_sav_cases()
        results['sav_count'] = len(sav)
    except Exception as e:
        results['sav_error'] = str(e)
    return jsonify(results)


@client_bp.route('/client/search', methods=['POST'])
def client_search():
    query = (request.json or {}).get('query', '').strip()
    if not query:
        return jsonify({'found': False})

    q = query.lower()
    customer_email = None
    customer_name = None
    orders = []
    sav_cases = []
    gmail_history = []

    # --- 1. Search our database by name / email / order number ---
    order_num = _extract_order_number(query)
    try:
        for case in database.get_sav_cases():
            case_order = (case.get('order_number') or '').lower()
            if (q in (case.get('customer_email') or '').lower()
                    or q in (case.get('customer_name') or '').lower()
                    or (order_num and order_num in case_order)
                    or q.lstrip('#') in case_order):
                sav_cases.append(case)
                if not customer_email:
                    customer_email = case.get('customer_email')
                if not customer_name:
                    customer_name = case.get('customer_name')
    except Exception:
        pass

    try:
        for draft in database.get_all_drafts():
            if (q in (draft.get('customer_email') or '').lower()
                    or q in (draft.get('customer_name') or '').lower()):
                if not customer_email:
                    customer_email = draft.get('customer_email')
                if not customer_name:
                    customer_name = draft.get('customer_name')
    except Exception:
        pass

    # --- 2. Shopify search ---
    if '@' in query:
        customer_email = query
        try:
            orders = shopify_api.get_orders_by_email(query) or []
            if orders and not customer_name:
                customer_name = orders[0].get('customer_name')
        except Exception:
            pass

    elif order_num:
        # Order number (handles #1161, 1161_REPARATION, etc.)
        try:
            order = shopify_api.get_order_by_number(order_num)
            if order:
                orders = [order]
                if not customer_email:
                    customer_email = order.get('customer_email')
                if not customer_name:
                    customer_name = order.get('customer_name')
        except Exception:
            pass

    else:
        # Name search → try Shopify customer search
        try:
            customers = shopify_api.search_customers_by_name(query)
            if customers and not customer_email:
                customer_email = customers[0]['email']
                customer_name = customers[0]['name']
        except Exception:
            pass

    # Fetch Shopify orders if we have an email but no orders yet
    if customer_email and not orders:
        try:
            orders = shopify_api.get_orders_by_email(customer_email) or []
        except Exception:
            pass

    if not customer_email and not sav_cases and not orders:
        return jsonify({'found': False, 'debug': {
            'query': query,
            'order_num_extracted': order_num,
            'sav_cases_count': len(sav_cases),
            'orders_count': len(orders),
            'customer_email': customer_email,
        }})

    # --- 3. Gmail history ---
    if customer_email:
        try:
            service = gmail_helper.get_gmail_service()
            raw = gmail_helper.get_customer_history(service, customer_email, max_results=20)
            gmail_history = [
                {
                    'date': h.get('date', '')[:16],
                    'subject': h.get('subject', ''),
                    'body': h.get('body', '')[:500],
                    'direction': h.get('direction', 'received'),
                }
                for h in raw
            ]
        except Exception:
            pass

    return jsonify({
        'found': True,
        'customer_email': customer_email or '',
        'customer_name': customer_name or '',
        'orders': [
            {
                'number': o.get('number', ''),
                'created_at': o.get('created_at', ''),
                'total': o.get('total', ''),
                'fulfillment_status': o.get('fulfillment_status', ''),
                'tracking_url': o.get('tracking_url'),
            }
            for o in orders[:20]
        ],
        'sav_cases': [
            {
                'id': c['id'],
                'subject': c['subject'],
                'status': c['status'],
                'created_at': c['created_at'][:10],
                'order_number': c.get('order_number'),
            }
            for c in sav_cases
        ],
        'gmail_history': gmail_history,
    })


@client_bp.route('/client/send', methods=['POST'])
def client_send():
    data = request.json or {}
    customer_email = data.get('customer_email', '')
    subject = data.get('subject') or 'Max Sauveur — Service Client'
    body = data.get('body', '')
    if not customer_email or not body:
        return jsonify({'success': False, 'error': 'Email et message requis'})
    try:
        service = gmail_helper.get_gmail_service()
        gmail_helper.send_email(service, customer_email, subject, body)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
