import base64
import itertools
import os
import random
import string
import threading
import time
from functools import wraps
from io import BytesIO

import qrcode
from authlib.integrations.flask_client import OAuth
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from bakong_khqr import KHQR

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")

PAYMENT_TIMEOUT_SECONDS = 300  # 5 minutes

# =============================
# BAKONG / KHQR CONFIG
# Real secrets only ever come from environment variables -- never hardcode
# them here, and never commit a .env file with real values into source control.
# =============================
BAKONG_API_TOKEN = os.getenv("BAKONG_API_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJkYXRhIjp7ImlkIjoiOTg4YTA0ZGVhMTYyNGY1MCJ9LCJpYXQiOjE3ODY4Njg4NjQsImV4cCI6MTc5NDY0NDg2NH0.WdwQrVmeaEjyZmsH2p7Uf-XMAXmd3aXMVNBAIk5LoDk")
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "chhira_ly@aclb")
MERCHANT_NAME = os.getenv("MERCHANT_NAME", "CAMLOCAL-T")
MERCHANT_CITY = os.getenv("MERCHANT_CITY", "Phnom Penh")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+855882000544")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8819268338:AAEGzZx_P02nVRS8fx4mXsLVM5QxASNhm2g")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1003924082723")

khqr = KHQR(BAKONG_API_TOKEN) if BAKONG_API_TOKEN else None

# =============================
# GOOGLE SIGN-IN CONFIG
# Create OAuth credentials at https://console.cloud.google.com/apis/credentials
# and set the authorized redirect URI to <your-domain>/auth/callback
# (use http://127.0.0.1:5000/auth/callback for local testing).
# =============================
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "21315854645-734qaqm9lp49n0ahc21gsfq4i8tmhd68.apps.googleusercontent.com")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "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")

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# =============================
# IN-MEMORY STATE ONLY -- NO DATABASE, NO FILES.
# Everything below lives in plain Python variables for the lifetime of the
# running process. Restarting the server clears all of it: carts, orders,
# and payment status are not saved anywhere.
# =============================
payment_status = {}      # md5 -> "PENDING" | "PAID" | "EXPIRED"
ORDERS = {}               # order_id -> order dict
_order_id_counter = itertools.count(1)

# =============================
# PRODUCT CATALOG
# =============================
PRODUCTS = [
    {"id": 1, "name": "Krama Weave Tee", "price": 0.01, "desc": "Krama checkerweave print."},
    {"id": 2, "name": "Angkor Line Tee", "price": 16.00, "desc": "Minimal temple line art."},
    {"id": 3, "name": "Script Stack Tee", "price": 17.00, "desc": "Khmer + English wordmark."},
    {"id": 4, "name": "Moto-Taxi Tee", "price": 18.00, "desc": "Tribute to the city's moto-dops."},
    {"id": 5, "name": "Night Market Tee", "price": 16.00, "desc": "String-light scatter print."},
    {"id": 6, "name": "Rice Field Tee", "price": 17.00, "desc": "Thread-stitch horizon print."},
]


def get_product(product_id):
    return next((p for p in PRODUCTS if p["id"] == product_id), None)


# =============================
# ORDER HELPERS (in-memory only)
# =============================
def create_order(user_id, md5, bill_number, name, email, phone, address, lat, lng, items, total):
    order_id = next(_order_id_counter)
    ORDERS[order_id] = {
        "id": order_id,
        "user_id": user_id,
        "md5": md5,
        "bill_number": bill_number,
        "customer_name": name,
        "customer_email": email,
        "customer_phone": phone,
        "customer_address": address,
        "customer_lat": lat,
        "customer_lng": lng,
        "items": items,
        "total": total,
        "status": "PENDING",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return order_id


def get_order_by_md5(md5):
    return next((o for o in ORDERS.values() if o["md5"] == md5), None)


def mark_order_paid(md5):
    order = get_order_by_md5(md5)
    if order:
        order["status"] = "PAID"


def mark_order_expired(md5):
    order = get_order_by_md5(md5)
    if order and order["status"] == "PENDING":
        order["status"] = "EXPIRED"


def get_orders_for_user(user_id):
    return sorted(
        (o for o in ORDERS.values() if o["user_id"] == user_id),
        key=lambda o: o["id"],
        reverse=True,
    )


# =============================
# AUTH HELPERS
# =============================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_user():
    cart = session.get("cart", {})
    return {
        "current_user": session.get("user"),
        "cart_count": sum(cart.values()) if session.get("user") else 0,
    }


# =============================
# LOGIN / LOGOUT (real Google Sign-In, nothing written to disk)
# =============================
@app.route("/login")
def login_page():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/login/google")
def login_google():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return "Google sign-in isn't configured yet: set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.", 500
    redirect_uri = url_for("auth_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google.authorize_access_token()
    userinfo = token.get("userinfo")
    if not userinfo:
        return redirect(url_for("login_page"))

    # No database: the Google account info is kept only in this browser's
    # signed session cookie, not written anywhere on the server.
    session["user"] = {
        "id": userinfo["sub"],
        "email": userinfo["email"],
        "name": userinfo.get("name", userinfo["email"].split("@")[0]),
        "picture": userinfo.get("picture", ""),
    }

    next_url = request.args.get("next") or url_for("index")
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# =============================
# HELPERS
# =============================
def generate_bill_number(length=8):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def notify_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except Exception:
        pass


def poll_bakong_payment(md5, timeout=PAYMENT_TIMEOUT_SECONDS):
    """
    Background worker that checks whether a KHQR bill has been paid.
    Uses ONLY the bakong_khqr library's own check_payment(md5) method --
    no direct HTTP calls to any API URL, and nothing written to disk.
    """
    payment_status[md5] = "PENDING"
    start = time.time()

    while time.time() - start < timeout:
        try:
            if khqr and hasattr(khqr, "check_payment"):
                result = khqr.check_payment(md5)
                if result is True or str(result).upper() in ("PAID", "SUCCESS"):
                    payment_status[md5] = "PAID"
                    mark_order_paid(md5)
                    order = get_order_by_md5(md5)
                    if order:
                        notify_telegram(
                            f"New order PAID\n"
                            f"Order #{order['id']} | ${order['total']:.2f}\n"
                            f"Customer: {order['customer_name']} ({order['customer_phone']})\n"
                            f"Deliver to: {order['customer_address']}"
                        )
                    return
        except Exception:
            pass

        time.sleep(2)

    payment_status[md5] = "EXPIRED"
    mark_order_expired(md5)


# =============================
# STOREFRONT (all require Google sign-in)
# =============================
@app.route("/")
@login_required
def index():
    return render_template("index.html", products=PRODUCTS)


@app.route("/cart/add", methods=["POST"])
@login_required
def cart_add():
    product_id = int(request.form["product_id"])
    qty = max(1, int(request.form.get("qty", 1)))

    if not get_product(product_id):
        return redirect(url_for("index"))

    cart = session.get("cart", {})
    key = str(product_id)
    cart[key] = cart.get(key, 0) + qty
    session["cart"] = cart

    return redirect(request.referrer or url_for("index"))


@app.route("/cart/remove", methods=["POST"])
@login_required
def cart_remove():
    product_id = str(int(request.form["product_id"]))
    cart = session.get("cart", {})
    cart.pop(product_id, None)
    session["cart"] = cart
    return redirect(url_for("cart_view"))


@app.route("/cart")
@login_required
def cart_view():
    cart = session.get("cart", {})
    items = []
    total = 0.0
    for pid, qty in cart.items():
        product = get_product(int(pid))
        if not product:
            continue
        line_total = product["price"] * qty
        total += line_total
        items.append({**product, "qty": qty, "line_total": line_total})

    return render_template("cart.html", items=items, total=total)


# =============================
# CHECKOUT -> GENERATE KHQR
# =============================
@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    if not khqr:
        return "Payment isn't configured yet: set BAKONG_API_TOKEN on the server.", 500

    cart = session.get("cart", {})
    if not cart:
        return redirect(url_for("cart_view"))

    user = session["user"]
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    address = request.form.get("address", "").strip()
    lat = request.form.get("lat", "").strip()
    lng = request.form.get("lng", "").strip()

    if not name or not phone or not address:
        return redirect(url_for("cart_view"))

    items, total = [], 0.0
    for pid, qty in cart.items():
        product = get_product(int(pid))
        if not product:
            continue
        items.append({"name": product["name"], "qty": qty, "price": product["price"]})
        total += product["price"] * qty

    if total <= 0:
        return redirect(url_for("cart_view"))

    bill_number = generate_bill_number()
    expires_at = int(time.time()) + PAYMENT_TIMEOUT_SECONDS

    qr_string = khqr.create_qr(
        bank_account=BANK_ACCOUNT,
        merchant_name=MERCHANT_NAME,
        merchant_city=MERCHANT_CITY,
        amount=round(total, 2),
        currency="USD",
        store_label="CAMLOCAL-T",
        phone_number=PHONE_NUMBER,
        bill_number=bill_number,
        terminal_label="WEB-01",
        static=False,
    )
    md5 = khqr.generate_md5(qr_string)

    img = qrcode.make(qr_string)
    buf = BytesIO()
    img.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode()

    order_id = create_order(
        user["id"], md5, bill_number, name, user["email"], phone, address,
        float(lat) if lat else None, float(lng) if lng else None, items, total,
    )

    threading.Thread(target=poll_bakong_payment, args=(md5,), daemon=True).start()

    session["cart"] = {}

    return render_template(
        "checkout_qr.html",
        qr_data=qr_base64,
        amount=round(total, 2),
        items=items,
        md5=md5,
        order_id=order_id,
        expires_at=expires_at,
    )


@app.route("/check_payment_status")
@login_required
def check_payment_status():
    md5 = request.args.get("md5", "")
    return jsonify({"status": payment_status.get(md5, "PENDING")})


@app.route("/account")
@login_required
def account_page():
    orders = get_orders_for_user(session["user"]["id"])
    return render_template("account.html", orders=orders)


@app.route("/order/<int:order_id>/success")
@login_required
def order_success(order_id):
    order = ORDERS.get(order_id)
    if not order or order["user_id"] != session["user"]["id"] or order["status"] != "PAID":
        return redirect(url_for("index"))
    return render_template("success.html", order=order)


if __name__ == "__main__":
    app.run(debug=True)
