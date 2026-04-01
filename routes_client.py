from flask import Blueprint, request, jsonify, render_template
import gmail as gmail_helper
import shopify_api
import database

client_bp = Blueprint('client_bp', __name__)


@client_bp.route('/client')
def client_page():
    return render_template('client.html')


@client_bp.route('/client/search', methods=['POST'])
def client_search():
    data = request.json
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'found': False})

    customer_email = None
    customer_name = None
    orders = []
    sav_cases = []
    gmail_history = []

    # 1. Search Shopify by email or order number
    try:
        if '@' in query:
            customer_email = query
            shopify_orders = shopify_api.get_orders_by_email(query)
        else:
            shopify_orders = shopify_api.get_orders_by_email(query)  # may return nothing
            # Try by order number
            order = shopify_api.get_order_by_number(query.replace('#', ''))
            if order:
                shopify_orders = [order]
                customer_email = order.get('customer_email')
        orders = shopify_orders or []
        if orders and not customer_email:
            customer_email = orders[0].get('customer_email')
        if orders and not customer_name:
            customer_name = orders[0].get('customer_name')
    except Exception:
        pass

    # 2. Search SAV cases by email or name
    try:
        all_cases = database.get_sav_cases()
        q_lower = query.lower()
        sav_cases = [
            c for c in all_cases
            if q_lower in c.get('customer_email', '').lower()
            or q_lower in c.get('customer_name', '').lower()
            or q_lower in (c.get('order_number') or '').lower()
        ]
        if sav_cases and not customer_email:
            customer_email = sav_cases[0]['customer_email']
        if sav_cases and not customer_name:
            customer_name = sav_cases[0]['customer_name']
    except Exception:
        pass

    # 3. Search pending drafts by email or name
    try:
        all_drafts = database.get_all_drafts()
        drafts = [
            d for d in all_drafts
            if q_lower in d.get('customer_email', '').lower()
            or q_lower in d.get('customer_name', '').lower()
        ]
        if drafts and not customer_email:
            customer_email = drafts[0]['customer_email']
        if drafts and not customer_name:
            customer_name = drafts[0]['customer_name']
    except Exception:
        drafts = []

    if not customer_email and not sav_cases and not orders:
        return jsonify({'found': False, 'query': query})

    # 4. Fetch Gmail history if we have an email
    if customer_email:
        try:
            service = gmail_helper.get_gmail_service()
            raw_history = gmail_helper.get_customer_history(service, customer_email, max_results=15)
            gmail_history = [
                {
                    'date': h.get('date', '')[:16],
                    'subject': h.get('subject', ''),
                    'body': h.get('body', '')[:400],
                    'direction': h.get('direction', 'received'),
                }
                for h in raw_history
            ]
        except Exception:
            pass

    return jsonify({
        'found': True,
        'customer_email': customer_email,
        'customer_name': customer_name,
        'orders': orders[:5],
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
    data = request.json
    customer_email = data['customer_email']
    subject = data.get('subject', 'Max Sauveur — Service Client')
    body = data['body']
    thread_id = data.get('thread_id')
    try:
        service = gmail_helper.get_gmail_service()
        gmail_helper.send_email(service, customer_email, subject, body, thread_id=thread_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
