import base64
import os
import random
import sqlite3
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

DB_PATH = os.path.join(os.path.dirname(__file__), "orders.db")

payment_status = {}

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
# DATABASE
# =============================
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_id TEXT UNIQUE,
            email TEXT UNIQUE,
            name TEXT,
            picture TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            md5 TEXT UNIQUE,
            bill_number TEXT,
            customer_name TEXT,
            customer_email TEXT,
            customer_phone TEXT,
            customer_address TEXT,
            customer_lat REAL,
            customer_lng REAL,
            items_json TEXT,
            total REAL,
            status TEXT DEFAULT 'PENDING',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()


def upsert_user(google_id, email, name, picture):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        INSERT INTO users (google_id, email, name, picture)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(google_id) DO UPDATE SET
            email=excluded.email, name=excluded.name, picture=excluded.picture
        """,
        (google_id, email, name, picture),
    )
    con.commit()
    row = con.execute("SELECT id FROM users WHERE google_id = ?", (google_id,)).fetchone()
    con.close()
    return row[0]


def create_order(user_id, md5, bill_number, name, email, phone, address, lat, lng, items, total):
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        """
        INSERT INTO orders
            (user_id, md5, bill_number, customer_name, customer_email, customer_phone,
             customer_address, customer_lat, customer_lng, items_json, total, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """,
        (user_id, md5, bill_number, name, email, phone, address, lat, lng, str(items), total),
    )
    con.commit()
    order_id = cur.lastrowid
    con.close()
    return order_id


def mark_order_paid(md5):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE orders SET status = 'PAID' WHERE md5 = ?", (md5,))
    con.commit()
    con.close()


def mark_order_expired(md5):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE orders SET status = 'EXPIRED' WHERE md5 = ? AND status = 'PENDING'", (md5,))
    con.commit()
    con.close()


def get_order_by_md5(md5):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM orders WHERE md5 = ?", (md5,)).fetchone()
    con.close()
    return dict(row) if row else None


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
# LOGIN / LOGOUT (real Google Sign-In via Authlib/OAuth)
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

    google_id = userinfo["sub"]
    email = userinfo["email"]
    name = userinfo.get("name", email.split("@")[0])
    picture = userinfo.get("picture", "")

    user_id = upsert_user(google_id, email, name, picture)
    session["user"] = {"id": user_id, "email": email, "name": name, "picture": picture}

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
    no direct HTTP calls to any API URL are made from this code. The
    library itself is responsible for talking to Bakong.
    """
    payment_status[md5] = "PENDING"
    start = time.time()

    while time.time() - start < timeout:
        try:
            if khqr and hasattr(khqr, "check_payment"):
                result = khqr.check_payment(md5)
                # bakong_khqr returns either a bool-like value or a status
                # string depending on version -- handle both.
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
            # Swallow transient errors from the library and keep polling
            # until timeout -- never crash the background thread.
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

    # Generate the KHQR string and its MD5 purely via the bakong_khqr library.
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
    user = session["user"]
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    orders = con.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()
    con.close()
    return render_template("account.html", orders=[dict(o) for o in orders])


@app.route("/order/<int:order_id>/success")
@login_required
def order_success(order_id):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, session["user"]["id"])
    ).fetchone()
    con.close()
    if not row or row["status"] != "PAID":
        return redirect(url_for("index"))
    return render_template("success.html", order=dict(row))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)