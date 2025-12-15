import os
import time
import secrets
import requests
from urllib.parse import urlencode
from flask import Flask, request, redirect, jsonify

app = Flask(__name__)

CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")  # MUST be the Koyeb callback URL
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

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

    # This is where your OPEN_ID comes from:
    open_id = token_json.get("open_id")
    if not open_id:
        return jsonify({"error": "Token response missing open_id", "raw": token_json}), 500

    # IMPORTANT:
    # Do NOT store tokens only on Koyeb local disk long-term.
    # For now we return them so you can copy OPEN_ID and confirm the flow works.
    # Next step: store tokens in Firestore/Supabase securely.
    return jsonify({
        "status": "connected",
        "open_id": open_id,
        "scope": token_json.get("scope"),
        "expires_in": token_json.get("expires_in"),
        "note": "Copy open_id for testing. Next step is secure token storage."
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
