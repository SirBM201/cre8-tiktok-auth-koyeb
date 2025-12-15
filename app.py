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

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

db = firestore.Client(project=FIREBASE_PROJECT_ID or None)

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

    # Store securely in Firestore (document id = open_id)
    now = int(time.time())
    doc = {
        "open_id": open_id,
        "scope": token_json.get("scope"),
        "token_type": token_json.get("token_type", "Bearer"),
        "access_token": token_json.get("access_token"),
        "refresh_token": token_json.get("refresh_token"),
        "expires_in": int(token_json.get("expires_in", 0) or 0),
        "refresh_expires_in": int(token_json.get("refresh_expires_in", 0) or 0),
        "obtained_at": now,
        "updated_at": firestore.SERVER_TIMESTAMP,
        "provider": "tiktok",
        # later we will add: app_user_id, platform_account_label, etc.
    }

    db.collection(COLL).document(open_id).set(doc, merge=True)

    # Return minimal info (do not leak tokens)
    return jsonify({
        "status": "connected",
        "open_id": open_id,
        "scope": doc["scope"],
        "stored_in_firestore": True
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
