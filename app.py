import os
import time
import secrets
import requests
from urllib.parse import urlencode

from flask import Flask, request, redirect, jsonify
from google.cloud import firestore

# -------------------------------------------------
# App setup
# -------------------------------------------------
app = Flask(__name__)

CRON_SECRET = os.getenv("CRON_SECRET", "")

def require_cron_secret():
    """
    Protect internal endpoints (cron/manual admin calls) from being triggered by random people.
    Client must send header: X-CRON-SECRET: <CRON_SECRET>
    """
    if not CRON_SECRET:
        return False, (jsonify({"error": "Server misconfigured: CRON_SECRET not set"}), 500)

    got = request.headers.get("X-CRON-SECRET", "")
    if not got or got != CRON_SECRET:
        return False, (jsonify({"error": "Unauthorized"}), 401)

    return True, None

# -------------------------------------------------
# Environment variables
# -------------------------------------------------
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")

CRON_SECRET = os.getenv("CRON_SECRET")  # 🔐 used by GitHub Actions

# -------------------------------------------------
# TikTok endpoints
# -------------------------------------------------
AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

# -------------------------------------------------
# Firestore client
# -------------------------------------------------
db = firestore.Client(project=PROJECT_ID or None)

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def build_auth_url(state: str) -> str:
    params = {
        "client_key": CLIENT_KEY,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_token(code: str) -> dict:
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }
    r = requests.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def refresh_access_token(refresh_token: str) -> dict:
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def is_expiring_soon(doc: dict, margin_seconds: int = 6 * 3600) -> bool:
    obtained_at = int(doc.get("obtained_at") or 0)
    expires_in = int(doc.get("expires_in") or 0)

    if not obtained_at or not expires_in:
        return True  # missing data → force refresh

    expires_at = obtained_at + expires_in
    return (expires_at - int(time.time())) <= margin_seconds

# -------------------------------------------------
# Routes
# -------------------------------------------------
@app.get("/")
def home():
    return jsonify({"status": "ok", "service": "cre8-tiktok-auth"})


@app.get("/api/tiktok/connect")
def tiktok_connect():
    if not (CLIENT_KEY and CLIENT_SECRET and REDIRECT_URI):
        return jsonify({"error": "Missing env vars"}), 500

    state = secrets.token_urlsafe(24)
    auth_url = build_auth_url(state)

    resp = redirect(auth_url, code=302)
    resp.set_cookie(
        "tiktok_oauth_state",
        state,
        httponly=True,
        samesite="Lax",
        max_age=600
    )
    return resp


@app.get("/api/tiktok/callback")
def tiktok_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    saved_state = request.cookies.get("tiktok_oauth_state")

    if not code:
        return jsonify({"error": "Missing code"}), 400
    if not state or not saved_state or state != saved_state:
        return jsonify({"error": "Invalid state"}), 400

    token_json = exchange_code_for_token(code)
    open_id = token_json.get("open_id")

    if not open_id:
        return jsonify({"error": "Token response missing open_id"}), 500

    now = int(time.time())

    doc = {
        "open_id": open_id,
        "provider": "tiktok",
        "scope": token_json.get("scope"),
        "token_type": token_json.get("token_type", "Bearer"),

        "access_token": token_json.get("access_token"),
        "refresh_token": token_json.get("refresh_token"),

        "expires_in": int(token_json.get("expires_in", 0) or 0),
        "refresh_expires_in": int(token_json.get("refresh_expires_in", 0) or 0),

        "obtained_at": now,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }

    db.collection(COLL).document(open_id).set(doc, merge=True)

    return jsonify({
        "status": "connected",
        "open_id": open_id,
        "collection": COLL
    })


# -------------------------------------------------
# 🔁 AUTO REFRESH ENDPOINT (CRON)
# -------------------------------------------------
@app.post("/api/tiktok/refresh_due")
def refresh_due():
    # 🔐 Protect endpoint
    if not CRON_SECRET or request.headers.get("X-CRON-SECRET") != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    margin = int(request.args.get("margin", str(6 * 3600)))  # 6 hours
    limit = int(request.args.get("limit", "50"))

    refreshed = 0
    skipped = 0
    errors = 0

    docs = db.collection(COLL).limit(limit).stream()

    for snap in docs:
        data = snap.to_dict() or {}

        if not is_expiring_soon(data, margin):
            skipped += 1
            continue

        refresh_token = data.get("refresh_token")
        if not refresh_token:
            errors += 1
            continue

        try:
            token_json = refresh_access_token(refresh_token)
            now = int(time.time())

            update = {
                "access_token": token_json.get("access_token"),
                "refresh_token": token_json.get("refresh_token") or refresh_token,
                "expires_in": int(token_json.get("expires_in", 0) or 0),
                "refresh_expires_in": int(token_json.get("refresh_expires_in", 0) or 0),
                "scope": token_json.get("scope") or data.get("scope"),
                "token_type": token_json.get("token_type", "Bearer"),
                "obtained_at": now,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "last_refresh_status": "refreshed",
            }

            db.collection(COLL).document(snap.id).set(update, merge=True)
            refreshed += 1

        except Exception as e:
            errors += 1
            db.collection(COLL).document(snap.id).set({
                "last_refresh_status": "error",
                "last_refresh_error": str(e)[:300],
                "updated_at": firestore.SERVER_TIMESTAMP,
            }, merge=True)

    return jsonify({
        "status": "ok",
        "processed": refreshed + skipped + errors,
        "refreshed": refreshed,
        "skipped": skipped,
        "errors": errors
    })


# -------------------------------------------------
# Local run
# -------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)

