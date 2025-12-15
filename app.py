import os
import time
import secrets
import requests
from urllib.parse import urlencode
from flask import Flask, request, redirect, jsonify

from google.cloud import firestore

app = Flask(__name__)

CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

# IMPORTANT: your Koyeb env screenshot shows FIREBASE_PROJECT_ID
# Your code currently uses FIREBASE_PROJECT_ID, so keep that name consistent.
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")

# refresh behavior
REFRESH_MARGIN_SECONDS = int(os.getenv("TIKTOK_REFRESH_MARGIN_SECONDS", "600"))  # 10 min
REFRESH_LOCK_SECONDS = int(os.getenv("TIKTOK_REFRESH_LOCK_SECONDS", "120"))      # 2 min

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

db = firestore.Client(project=PROJECT_ID or None)


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
    """
    TikTok refresh flow.
    """
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def token_expires_soon(doc: dict) -> bool:
    obtained_at = int(doc.get("obtained_at") or 0)
    expires_in = int(doc.get("expires_in") or 0)
    if obtained_at <= 0 or expires_in <= 0:
        return True  # treat unknown expiry as "refresh needed"
    expiry_ts = obtained_at + expires_in
    return (expiry_ts - int(time.time())) <= REFRESH_MARGIN_SECONDS


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
    resp.set_cookie("tiktok_oauth_state", state, httponly=True, samesite="Lax", max_age=600)
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
        return jsonify({"error": "Token response missing open_id", "raw": token_json}), 500

    now = int(time.time())

    doc = {
        "open_id": open_id,
        "provider": "tiktok",
        "scope": token_json.get("scope"),
        "token_type": token_json.get("token_type", "Bearer"),

        # store tokens (server-side only)
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
        "stored_in_firestore": True,
        "collection": COLL
    })


@app.post("/api/tiktok/refresh")
def api_tiktok_refresh():
    """
    POST /api/tiktok/refresh
    Body:
      - {"open_id":"..."}  OR
      - {"refresh_token":"..."} (rare/manual use)

    Returns:
      - refreshed tokens + firestore updated = true/false
    """
    if not (CLIENT_KEY and CLIENT_SECRET):
        return jsonify({"error": "Missing TIKTOK_CLIENT_KEY/SECRET"}), 500

    body = request.get_json(silent=True) or {}
    open_id = body.get("open_id")
    direct_refresh_token = body.get("refresh_token")

    if not open_id and not direct_refresh_token:
        return jsonify({"error": "Provide open_id or refresh_token"}), 400

    # 1) If open_id is provided, load from Firestore
    if open_id:
        doc_ref = db.collection(COLL).document(open_id)
        snap = doc_ref.get()
        if not snap.exists:
            return jsonify({"error": "open_id not found in firestore", "open_id": open_id}), 404

        doc = snap.to_dict() or {}

        # simple lock to avoid multiple refreshes simultaneously
        now = int(time.time())
        locked_until = int(doc.get("refresh_locked_until") or 0)
        if locked_until > now:
            return jsonify({
                "status": "skipped",
                "reason": "refresh_locked",
                "locked_until": locked_until,
                "open_id": open_id
            }), 200

        if not token_expires_soon(doc):
            return jsonify({
                "status": "skipped",
                "reason": "not_expiring_soon",
                "open_id": open_id
            }), 200

        refresh_token = doc.get("refresh_token")
        if not refresh_token:
            return jsonify({"error": "No refresh_token stored for open_id", "open_id": open_id}), 500

        # set lock (best-effort)
        doc_ref.set({"refresh_locked_until": now + REFRESH_LOCK_SECONDS}, merge=True)

        token_json = refresh_access_token(refresh_token)

        # TikTok may return a *new* refresh_token or reuse existing; handle both
        new_access = token_json.get("access_token")
        new_refresh = token_json.get("refresh_token") or refresh_token

        if not new_access:
            # clear lock so future tries can run
            doc_ref.set({"refresh_locked_until": 0}, merge=True)
            return jsonify({"error": "Refresh response missing access_token", "raw": token_json}), 500

        update = {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": int(token_json.get("expires_in", 0) or 0),
            "refresh_expires_in": int(token_json.get("refresh_expires_in", 0) or 0),
            "obtained_at": int(time.time()),
            "updated_at": firestore.SERVER_TIMESTAMP,
            "refresh_locked_until": 0,
        }
        doc_ref.set(update, merge=True)

        return jsonify({
            "status": "refreshed",
            "open_id": open_id,
            "updated_firestore": True,
            "collection": COLL,
            "expires_in": update["expires_in"]
        }), 200

    # 2) Direct refresh_token call (manual troubleshooting)
    token_json = refresh_access_token(direct_refresh_token)
    if not token_json.get("access_token"):
        return jsonify({"error": "Refresh response missing access_token", "raw": token_json}), 500

    return jsonify({
        "status": "refreshed_manual",
        "token": token_json
    }), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
