"""
Pizza Home - Backend Logic (Flask)

Features implemented in this single-file prototype:
- WhatsApp incoming webhook receiver (/webhook/whatsapp)
- Create order endpoint (/order/create)
- Payment webhook (/webhook/payment) - for payment gateways (optional)
- Screenshot upload endpoint (/upload/screenshot)
- Manual verification endpoint (/order/verify) - you will verify payment screenshot
- Rider notification (/rider/notify)
- Menu upload / load (JSON)
- Delivery charges load (JSON)
- Simple menu text matching using difflib (for 'image-menu' typed selections)
- Simple in-memory session/cart store (replace with Redis in production)

How to use:
1) Install dependencies: pip install flask sqlite3
2) Configure environment variables for WhatsApp provider (placeholders in send_whatsapp function)
3) Run: python pizza_home_backend.py
4) Expose the server to the internet (ngrok) and configure your WhatsApp provider webhook to /webhook/whatsapp

WARNING: This is a prototype to be extended. Replace dummy send_whatsapp() with your provider (Twilio / 360dialog / Meta Cloud API).

"""
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
import os
import json
import sqlite3
import uuid
import difflib
from datetime import datetime, timedelta
import threading

# ---------------------------- Configuration ---------------------------------
DATA_DIR = 'data'
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
MENU_FILE = os.path.join(DATA_DIR, 'menu.json')
DELIVERY_FILE = os.path.join(DATA_DIR, 'delivery_charges.json')
DB_FILE = os.path.join(DATA_DIR, 'orders.db')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Basic Flask app
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB uploads

# In-memory session/cart store (use Redis in production)
sessions = {}

# ---------------------------- Utilities -----------------------------------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        customer_phone TEXT,
        customer_name TEXT,
        items_json TEXT,
        subtotal INTEGER,
        delivery_charges INTEGER,
        total INTEGER,
        payment_method TEXT,
        payment_status TEXT,
        status TEXT,
        address TEXT,
        lat REAL,
        lng REAL,
        created_at TEXT,
        verified_at TEXT,
        screenshot_path TEXT
    )''')
    conn.commit()
    conn.close()


def load_json_file(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


menu = load_json_file(MENU_FILE, {
    "items": [
        {"name": "Chicken Tikka Pizza", "prices": {"Small": 350, "Medium": 650, "Large": 950}},
        {"name": "Pepperoni Pizza", "prices": {"Small": 400, "Medium": 700, "Large": 1000}},
        {"name": "Margherita Pizza", "prices": {"Small": 300, "Medium": 550, "Large": 800}},
        {"name": "Fries", "prices": {"OneSize": 120}},
        {"name": "Pepsi 1.5L", "prices": {"OneSize": 250}}
    ]
})

delivery_charges = load_json_file(DELIVERY_FILE, {
    "zones": {
        "City Center": 80,
        "Fauji Colony": 100,
        "Near DHQ": 120,
        "Outskirts": 150
    }
})


def save_menu():
    with open(MENU_FILE, 'w', encoding='utf-8') as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)


def save_delivery():
    with open(DELIVERY_FILE, 'w', encoding='utf-8') as f:
        json.dump(delivery_charges, f, ensure_ascii=False, indent=2)


# very simple whatsapp send stub - replace with provider SDK
def send_whatsapp(to, text, template_name=None, template_components=None):
    # Replace this function with Twilio / 360dialog / Meta API integration
    print(f"[WHATSAPP -> {to}] {text}")


# Basic menu text matching: returns (item_obj, price, size_key) or (None, None, None)
def match_menu_item(text):
    # normalize
    text = text.strip()
    # try to find explicit size words
    size = None
    for s in ['Large', 'large', 'L', 'l', 'Medium', 'medium', 'M', 'm', 'Small', 'small', 'S', 's']:
        if s in text:
            size = s.capitalize() if len(s) > 1 else s.upper()
            break

    # build candidate list
    names = [it['name'] for it in menu['items']]
    match = difflib.get_close_matches(text, names, n=1, cutoff=0.5)
    if match:
        # find item
        for it in menu['items']:
            if it['name'] == match[0]:
                # determine price
                prices = it.get('prices', {})
                # choose size
                if size:
                    # try to map small/medium/large variants
                    if size in prices:
                        return it, prices[size], size
                    # handle single size
                    if 'OneSize' in prices:
                        return it, prices['OneSize'], 'OneSize'
                    # fallback to medium or first
                    if 'Medium' in prices:
                        return it, prices['Medium'], 'Medium'
                    first_price = next(iter(prices.values()))
                    return it, first_price, list(prices.keys())[0]
                else:
                    # no size mentioned -> pick default (Medium or OneSize)
                    if 'Medium' in prices:
                        return it, prices['Medium'], 'Medium'
                    if 'OneSize' in prices:
                        return it, prices['OneSize'], 'OneSize'
                    first_price = next(iter(prices.values()))
                    return it, first_price, list(prices.keys())[0]
    # fallback: attempt rough extraction like "Large Chicken Tikka"
    for it in menu['items']:
        if it['name'].lower() in text.lower():
            prices = it.get('prices', {})
            if 'Medium' in prices:
                return it, prices['Medium'], 'Medium'
            if 'OneSize' in prices:
                return it, prices['OneSize'], 'OneSize'
            first_price = next(iter(prices.values()))
            return it, first_price, list(prices.keys())[0]
    return None, None, None


def calculate_delivery_charge(address_text):
    # naive zone detection using keywords
    addr = address_text.lower()
    for zone, charge in delivery_charges.get('zones', {}).items():
        if zone.lower() in addr:
            return charge, zone
    # default fallback
    return list(delivery_charges.get('zones', {}).values())[0], list(delivery_charges.get('zones', {}).keys())[0]


def persist_order_to_db(order):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO orders (order_id, customer_phone, customer_name, items_json, subtotal, delivery_charges, total, payment_method, payment_status, status, address, lat, lng, created_at, screenshot_path)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
        order['order_id'], order['customer_phone'], order.get('customer_name'), json.dumps(order['items']), order['subtotal'], order['delivery_charges'], order['total'], order['payment_method'], order.get('payment_status','pending'), order.get('status','initiated'), order.get('address'), order.get('lat'), order.get('lng'), order.get('created_at'), order.get('screenshot_path')
    ))
    conn.commit()
    conn.close()


def update_order_payment_status(order_id, payment_status, screenshot_path=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if screenshot_path:
        c.execute('UPDATE orders SET payment_status=?, status=?, screenshot_path=?, verified_at=? WHERE order_id=?', (payment_status, 'awaiting_verification' if payment_status=='pending' else 'confirmed', screenshot_path, datetime.utcnow().isoformat(), order_id))
    else:
        c.execute('UPDATE orders SET payment_status=?, status=? WHERE order_id=?', (payment_status, 'confirmed' if payment_status=='paid' else 'pending', order_id))
    conn.commit()
    conn.close()


# ---------------------------- Flask Endpoints ------------------------------

@app.route('/webhook/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Receive incoming WhatsApp messages from provider (Twilio/360dialog). This is a minimal parser.
    Expect JSON with at least: from, type, text or location or media
    """
    payload = request.get_json(force=True)
    # Basic structure depends on provider - adapt mapping as needed
    sender = payload.get('from') or payload.get('wa_id') or payload.get('contact')
    message_type = payload.get('type', 'text')
    print('[WHATSAPP INCOMING]', payload)

    # For demo: extract text or location
    text = None
    if message_type == 'text' or 'text' in payload:
        text = payload.get('text', {}).get('body') if isinstance(payload.get('text'), dict) else payload.get('text')
    elif message_type == 'location' or 'location' in payload:
        loc = payload.get('location') or payload.get('loc')
        # handle location - store in session
        if sender:
            sessions.setdefault(sender, {})['last_location'] = loc
            send_whatsapp(sender, 'Got your location. Please confirm your address or type the full address.')
            return jsonify({'status':'ok'})

    if not sender:
        return jsonify({'error':'no sender id'}), 400

    # Simple intent routing by keywords
    lower = (text or '').lower()
    if any(k in lower for k in ['order', 'menu', 'pizza', 'place order', 'order karna', 'order krna']):
        # Start order flow: ask what they want or accept direct selection
        send_whatsapp(sender, 'Great — send me the item name (e.g., "Large Chicken Tikka") or type MENU to see options.')
    elif lower == 'menu' or 'show menu' in lower:
        # Reply with menu (text version)
        lines = ['Menu:']
        for it in menu['items']:
            prices = it['prices']
            price_str = ', '.join([f"{k}: Rs {v}" for k, v in prices.items()])
            lines.append(f"- {it['name']} ({price_str})")
        send_whatsapp(sender, '\n'.join(lines))
    elif any(k in lower for k in ['large', 'medium', 'small', 'fries', 'pepsi']):
        # User likely selected an item from image menu by typing
        item, price, size = match_menu_item(text)
        if item:
            # add to cart
            sess = sessions.setdefault(sender, {'cart': []})
            sess['cart'].append({'name': item['name'], 'size': size, 'price': price, 'qty': 1})
            subtotal = sum(i['price'] * i.get('qty',1) for i in sess['cart'])
            send_whatsapp(sender, f"Added to cart: {size} {item['name']} = Rs {price}.\nCurrent total: Rs {subtotal}.\nReply CHECKOUT to proceed or add more items.")
        else:
            send_whatsapp(sender, "Sorry, I couldn't find that item in the menu. Please type the exact name or type MENU to view options.")
    elif lower == 'checkout':
        sess = sessions.get(sender)
        if not sess or not sess.get('cart'):
            send_whatsapp(sender, 'Your cart is empty. Send an item name to add.')
        else:
            subtotal = sum(i['price']*i.get('qty',1) for i in sess['cart'])
            sess['subtotal'] = subtotal
            send_whatsapp(sender, f"Your subtotal is Rs {subtotal}. Please share delivery address or type PICKUP.\nPayment options: 1) Cash on Delivery 2) Online Payment (send screenshot after transfer). Reply COD or ONLINE.")
    elif lower == 'cod' or 'cash' in lower:
        sess = sessions.get(sender)
        if not sess or not sess.get('cart'):
            send_whatsapp(sender, 'Your cart is empty. Add items before choosing payment.')
        else:
            send_whatsapp(sender, 'Please share your delivery address (text) or tap Share Location.')
            sess['awaiting_address'] = True
    elif lower == 'pickup':
        sess = sessions.get(sender)
        if not sess or not sess.get('cart'):
            send_whatsapp(sender, 'Your cart is empty. Add items before choosing pickup.')
        else:
            # place order as pickup
            subtotal = sess['subtotal']
            order_id = f"PH-{uuid.uuid4().hex[:8].upper()}"
            order = {
                'order_id': order_id,
                'customer_phone': sender,
                'customer_name': sess.get('name'),
                'items': sess['cart'],
                'subtotal': subtotal,
                'delivery_charges': 0,
                'total': subtotal,
                'payment_method': 'cod',
                'payment_status': 'pending',
                'status': 'confirmed',
                'address': 'PICKUP',
                'created_at': datetime.utcnow().isoformat()
            }
            persist_order_to_db(order)
            send_whatsapp(sender, f"✅ Order {order_id} confirmed for pickup. ETA ~45 minutes. We have notified the kitchen and rider.")
            # notify rider
            threading.Thread(target=notify_rider, args=(order,)).start()
            sessions.pop(sender, None)
    elif 'confirm address' in lower or 'address' in lower or sessions.get(sender, {}).get('awaiting_address'):
        # treat text as address
        addr = text
        sess = sessions.setdefault(sender, {})
        sess['address'] = addr
        sess['awaiting_address'] = False
        # calculate delivery charge
        dc, zone = calculate_delivery_charge(addr)
        sess['delivery_charges'] = dc
        subtotal = sess.get('subtotal', sum(i['price'] for i in sess.get('cart', [])))
        total = subtotal + dc
        sess['total'] = total
        order_preview = f"Delivery Charges: Rs {dc} (Zone: {zone})\nSubtotal: Rs {subtotal}\nGrand Total: Rs {total}\nPayment: COD or ONLINE? Reply COD or ONLINE."
        send_whatsapp(sender, order_preview)
    elif lower == 'online':
        sess = sessions.get(sender)
        if not sess or not sess.get('cart'):
            send_whatsapp(sender, 'Your cart is empty. Add items before choosing payment.')
        else:
            # compute totals
            subtotal = sess.get('subtotal', sum(i['price'] for i in sess['cart']))
            if not sess.get('delivery_charges'):
                # ask for address first
                send_whatsapp(sender, 'Please share your delivery address first so I can calculate delivery charges.')
            else:
                total = subtotal + sess['delivery_charges']
                sess['total'] = total
                # generate order id and send bank details
                order_id = f"PH-{uuid.uuid4().hex[:8].upper()}"
                sess['pending_order_id'] = order_id
                bank_msg = f"Please make payment to:\nBank: XYZ Bank\nAccount: Pizza Home\nAccount Number: 1234-5678-9012\nAccount Title: Pizza Home\nAmount: Rs {total}\nAfter payment, please send a screenshot using UPLOAD SCREENSHOT button or by sending the image here.\nYour Order ID: {order_id}"
                send_whatsapp(sender, bank_msg)
                # create order in DB with payment pending
                order = {
                    'order_id': order_id,
                    'customer_phone': sender,
                    'customer_name': sess.get('name'),
                    'items': sess['cart'],
                    'subtotal': subtotal,
                    'delivery_charges': sess['delivery_charges'],
                    'total': total,
                    'payment_method': 'online_manual',
                    'payment_status': 'pending',
                    'status': 'awaiting_payment',
                    'address': sess.get('address'),
                    'created_at': datetime.utcnow().isoformat()
                }
                persist_order_to_db(order)
    elif lower == 'upload screenshot' or lower.startswith('upload') or 'screenshot' in lower:
        send_whatsapp(sender, 'Please use the file upload option in WhatsApp to send your payment screenshot. Use the endpoint /upload/screenshot with your Order-ID in the form data if using REST client.')
    elif lower == 'track' or 'track order' in lower:
        send_whatsapp(sender, 'Please send your Order ID to track (e.g., PH-12345).')
    elif text and text.startswith('PH-'):
        # lookup order
        order_id = text.strip()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT status, payment_status, total, created_at FROM orders WHERE order_id=?', (order_id,))
        row = c.fetchone()
        conn.close()
        if row:
            status, payment_status, total, created_at = row
            send_whatsapp(sender, f"Order {order_id} status: {status}. Payment: {payment_status}. Total: Rs {total}. Placed at {created_at} UTC.")
        else:
            send_whatsapp(sender, 'Order not found. Please check the Order ID.')
    else:
        send_whatsapp(sender, "Sorry, I didn't understand that. You can: 1) Type MENU 2) Type an item name (e.g., 'Large Pepperoni') 3) Type CHECKOUT to pay or COD 4) Type TRACK and Order ID to track")

    return jsonify({'status':'ok'})


@app.route('/order/create', methods=['POST'])
def http_create_order():
    """External API to create an order (used by dashboard or POS). Accepts JSON similar to earlier examples."""
    data = request.get_json(force=True)
    # validate minimal fields
    required = ['customer_phone', 'items', 'payment_method']
    for r in required:
        if r not in data:
            return jsonify({'error': f'Missing {r}'}), 400
    order_id = f"PH-{uuid.uuid4().hex[:8].upper()}"
    subtotal = sum(it.get('price',0) * it.get('qty',1) for it in data['items'])
    dc = data.get('delivery_charges', 0)
    total = subtotal + dc
    order = {
        'order_id': order_id,
        'customer_phone': data['customer_phone'],
        'customer_name': data.get('customer_name'),
        'items': data['items'],
        'subtotal': subtotal,
        'delivery_charges': dc,
        'total': total,
        'payment_method': data['payment_method'],
        'payment_status': 'pending' if data['payment_method']!='online_manual' else 'pending',
        'status': 'confirmed' if data.get('payment_method')=='cod' else 'awaiting_payment',
        'address': data.get('address'),
        'lat': data.get('lat'),
        'lng': data.get('lng'),
        'created_at': datetime.utcnow().isoformat()
    }
    persist_order_to_db(order)
    # notify customer and rider
    send_whatsapp(order['customer_phone'], f"✅ Order {order_id} received. Total Rs {total}. ETA ~45 minutes.")
    threading.Thread(target=notify_rider, args=(order,)).start()
    return jsonify({'order_id': order_id, 'total': total}), 201


@app.route('/upload/screenshot', methods=['POST'])
def upload_screenshot():
    """Endpoint to receive user uploaded screenshot (WhatsApp provider will usually post media URL to webhook instead)
    Accept form-data: order_id, phone, file
    """
    order_id = request.form.get('order_id')
    phone = request.form.get('phone')
    if 'file' not in request.files:
        return jsonify({'error':'file missing'}), 400
    f = request.files['file']
    filename = secure_filename(f.filename)
    save_path = os.path.join(UPLOAD_DIR, f"{order_id}_{filename}")
    f.save(save_path)
    # mark order awaiting verification
    update_order_payment_status(order_id, 'pending', screenshot_path=save_path)
    # notify admin (you) - replace with your admin WhatsApp number
    admin_phone = "+923001234567"
    send_whatsapp(admin_phone, f"Payment screenshot uploaded for {order_id} by {phone}. Please verify. Screenshot path: {save_path}")
    send_whatsapp(phone, 'Thanks — we received your screenshot. We will verify and confirm your order shortly.')
    return jsonify({'status':'uploaded'})


@app.route('/order/verify', methods=['POST'])
def verify_order():
    """Manual verification endpoint for admin to confirm a payment. POST JSON: {order_id: 'PH-...', verified: true}
    If verified=true -> mark order as paid and send confirmation to customer and rider
    """
    data = request.get_json(force=True)
    order_id = data.get('order_id')
    verified = data.get('verified', False)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT customer_phone, total FROM orders WHERE order_id=?', (order_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error':'order not found'}), 404
    customer_phone, total = row
    if verified:
        update_order_payment_status(order_id, 'paid')
        send_whatsapp(customer_phone, f"✅ Payment verified for {order_id}. Your order will be delivered in ~45 minutes.")
        # update order status and notify rider/kitchen
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('UPDATE orders SET status=? WHERE order_id=?', ('confirmed', order_id))
        conn.commit()
        conn.close()
        # fetch order to notify rider
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT items_json, address, total FROM orders WHERE order_id=?', (order_id,))
        row = c.fetchone()
        conn.close()
        items_json, address, total = row
        order = {'order_id': order_id, 'customer_phone': customer_phone, 'items': json.loads(items_json), 'address': address, 'total': total}
        threading.Thread(target=notify_rider, args=(order,)).start()
        return jsonify({'status':'verified'})
    else:
        update_order_payment_status(order_id, 'failed')
        send_whatsapp(customer_phone, f"Payment for {order_id} could not be verified. Please try again or contact support.")
        return jsonify({'status':'failed'})


@app.route('/webhook/payment', methods=['POST'])
def payment_webhook():
    """Optional: handle payment gateway webhooks where provider notifies auto-paid transactions.
    Example payload: {order_id, transaction_id, status}
    """
    data = request.get_json(force=True)
    order_id = data.get('order_id')
    status = data.get('status')
    if status == 'paid':
        update_order_payment_status(order_id, 'paid')
        # notify customer
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT customer_phone FROM orders WHERE order_id=?', (order_id,))
        row = c.fetchone()
        conn.close()
        if row:
            send_whatsapp(row[0], f"✅ Payment received for {order_id}. Your order will be delivered in ~45 minutes.")
        # notify rider
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT items_json, address, total, customer_phone FROM orders WHERE order_id=?', (order_id,))
        row = c.fetchone()
        conn.close()
        if row:
            items_json, address, total, customer_phone = row
            order = {'order_id': order_id, 'customer_phone': customer_phone, 'items': json.loads(items_json), 'address': address, 'total': total}
            threading.Thread(target=notify_rider, args=(order,)).start()
    return jsonify({'status':'ok'})


@app.route('/rider/notify', methods=['POST'])
def http_notify_rider():
    data = request.get_json(force=True)
    order = data.get('order')
    if not order:
        return jsonify({'error':'missing order'}), 400
    threading.Thread(target=notify_rider, args=(order,)).start()
    return jsonify({'status':'notifying'})


def notify_rider(order):
    # Example: forward message to rider number or call Rider API
    rider_number = os.getenv('RIDER_NUMBER', '+923001234567')
    items = order.get('items')
    items_text = '\n'.join([f"- {i.get('qty',1)}x {i.get('size','')} {i.get('name')} = Rs {i.get('price')}" for i in items])
    text = f"📦 New Order: {order.get('order_id')}\nCustomer: {order.get('customer_phone')}\nAddress: {order.get('address')}\nItems:\n{items_text}\nTotal: Rs {order.get('total')}\nDelivery Time: approx {45} minutes"
    send_whatsapp(rider_number, text)


@app.route('/menu/upload', methods=['POST'])
def upload_menu():
    """Admin endpoint to upload menu JSON (overwrites existing menu)
    Expects JSON body matching saved structure.
    """
    data = request.get_json(force=True)
    if not data:
        return jsonify({'error':'missing menu body'}), 400
    global menu
    menu = data
    save_menu()
    return jsonify({'status':'menu saved'})


@app.route('/delivery/upload', methods=['POST'])
def upload_delivery_charges():
    data = request.get_json(force=True)
    if not data:
        return jsonify({'error':'missing body'}), 400
    global delivery_charges
    delivery_charges = data
    save_delivery()
    return jsonify({'status':'delivery saved'})


@app.route('/uploads/<path:filename>', methods=['GET'])
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------------------- Start ---------------------------------------
if __name__ == '__main__':
    init_db()
    save_menu()
    save_delivery()
    app.run(host='0.0.0.0', port=5000, debug=True)
