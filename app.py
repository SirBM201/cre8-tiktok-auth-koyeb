import os
import time
import secrets
import logging
from urllib.parse import urlencode

import requests
from flask import Flask, request, redirect, jsonify
from google.cloud import firestore

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cre8-tiktok-auth")

# -----------------------------
# App
# -----------------------------
app = Flask(__name__)

# -----------------------------
# ENV
# -----------------------------
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")  # e.g. cre8-studio
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")

# Protect refresh endpoints
CRON_SECRET = os.getenv("CRON_SECRET", "")

# TikTok endpoints
AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

# Content posting (pull from URL) endpoint
PUBLISH_INIT_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/video/init/"

# -----------------------------
# Firestore client
# -----------------------------
# GOOGLE_APPLICATION_CREDENTIALS should point to your service account json file
db = firestore.Client(project=PROJECT_ID or None)

# -----------------------------
# Helpers
# -----------------------------
def now_ts() -> int:
    return int(time.time())

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
    Refresh using TikTok refresh token.
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

def require_cron_secret():
    """
    Require header: X-CRON-SECRET: <CRON_SECRET>
    """
    if not CRON_SECRET:
        return False, (jsonify({"error": "Server misconfigured: CRON_SECRET not set"}), 500)

    got = request.headers.get("X-CRON-SECRET", "")
    if not got or got != CRON_SECRET:
        return False, (jsonify({"error": "Unauthorized"}), 401)

    return True, None

def get_account_doc(open_id: str):
    ref = db.collection(COLL).document(open_id)
    snap = ref.get()
    if not snap.exists:
        return None, None
    return ref, snap.to_dict()

def token_expires_soon(doc: dict, safety_window_seconds: int = 6 * 3600) -> bool:
    """
    Refresh only when access token is close to expiring.
    Default: if token expires within next 6 hours.
    """
    obtained_at = int(doc.get("obtained_at", 0) or 0)
    expires_in = int(doc.get("expires_in", 0) or 0)

    if obtained_at <= 0 or expires_in <= 0:
        # if unknown, treat as expiring soon
        return True

    expires_at = obtained_at + expires_in
    return (expires_at - now_ts()) <= safety_window_seconds

def safe_env_check():
    missing = []
    if not CLIENT_KEY: missing.append("TIKTOK_CLIENT_KEY")
    if not CLIENT_SECRET: missing.append("TIKTOK_CLIENT_SECRET")
    if not REDIRECT_URI: missing.append("TIKTOK_REDIRECT_URI")
    return missing

# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def home():
    return jsonify({"status": "ok", "service": "cre8-tiktok-auth"})

@app.get("/api/tiktok/connect")
def tiktok_connect():
    missing = safe_env_check()
    if missing:
        return jsonify({"error": "Missing env vars", "missing": missing}), 500

    state = secrets.token_urlsafe(24)
    auth_url = build_auth_url(state)

    resp = redirect(auth_url, code=302)
    resp.set_cookie(
        "tiktok_oauth_state",
        state,
        httponly=True,
        samesite="Lax",
        max_age=600,
        secure=True,
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
        return jsonify({"error": "Token response missing open_id", "raw": token_json}), 500

    now = now_ts()

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

# -----------------------------
# Refresh endpoints
# -----------------------------
@app.post("/api/tiktok/refresh")
def refresh_one():
    ok, resp = require_cron_secret()
    if not ok:
        return resp

    payload = request.get_json(silent=True) or {}
    open_id = payload.get("open_id")
    refresh_token_input = payload.get("refresh_token")

    if not open_id and not refresh_token_input:
        return jsonify({"error": "Provide open_id or refresh_token"}), 400

    # If caller provides refresh_token directly, refresh immediately (no firestore read needed)
    if refresh_token_input:
        try:
            token_json = refresh_access_token(refresh_token_input)
            return jsonify({"status": "refreshed", "token_response": token_json})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)}), 500

    # Otherwise, load from Firestore by open_id
    ref, doc = get_account_doc(open_id)
    if not doc:
        return jsonify({"error": "open_id not found in Firestore", "open_id": open_id}), 404

    if not token_expires_soon(doc):
        return jsonify({"status": "skipped", "open_id": open_id, "reason": "not_expiring_soon"})

    rt = doc.get("refresh_token")
    if not rt:
        return jsonify({"status": "error", "open_id": open_id, "error": "Missing refresh_token in Firestore"}), 500

    try:
        token_json = refresh_access_token(rt)
        new_access = token_json.get("access_token")
        new_refresh = token_json.get("refresh_token")

        # Update Firestore (keep open_id doc id)
        update_doc = {
            "access_token": new_access or doc.get("access_token"),
            "refresh_token": new_refresh or doc.get("refresh_token"),
            "expires_in": int(token_json.get("expires_in", doc.get("expires_in", 0)) or 0),
            "refresh_expires_in": int(token_json.get("refresh_expires_in", doc.get("refresh_expires_in", 0)) or 0),
            "obtained_at": now_ts(),
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        ref.set(update_doc, merge=True)

        return jsonify({
            "status": "refreshed",
            "open_id": open_id,
            "updated": True,
        })
    except Exception as e:
        return jsonify({"status": "error", "open_id": open_id, "error": str(e)}), 500


@app.post("/api/tiktok/refresh_all")
def refresh_all():
    ok, resp = require_cron_secret()
    if not ok:
        return resp

    processed = 0
    refreshed = 0
    skipped = 0
    errors = 0

    # Stream all docs in collection
    for snap in db.collection(COLL).stream():
        processed += 1
        open_id = snap.id
        doc = snap.to_dict() or {}

        try:
            if not token_expires_soon(doc):
                skipped += 1
                continue

            rt = doc.get("refresh_token")
            if not rt:
                errors += 1
                log.warning("Missing refresh_token for open_id=%s", open_id)
                continue

            token_json = refresh_access_token(rt)

            update_doc = {
                "access_token": token_json.get("access_token", doc.get("access_token")),
                "refresh_token": token_json.get("refresh_token", doc.get("refresh_token")),
                "expires_in": int(token_json.get("expires_in", doc.get("expires_in", 0)) or 0),
                "refresh_expires_in": int(token_json.get("refresh_expires_in", doc.get("refresh_expires_in", 0)) or 0),
                "obtained_at": now_ts(),
                "updated_at": firestore.SERVER_TIMESTAMP,
            }

            db.collection(COLL).document(open_id).set(update_doc, merge=True)
            refreshed += 1

        except Exception as e:
            errors += 1
            log.exception("Refresh failed for open_id=%s error=%s", open_id, str(e))

    return jsonify({
        "status": "ok",
        "processed": processed,
        "refreshed": refreshed,
        "skipped": skipped,
        "errors": errors,
        "collection": COLL
    })

# -----------------------------
# Publish: PULL_FROM_URL (starter)
# -----------------------------
@app.post("/api/tiktok/publish/pull")
def tiktok_publish_pull():
    """
    Publish video to TikTok by letting TikTok pull from a public URL.
    Body JSON:
    {
      "open_id": "...",
      "video_url": "https://.../video.mp4",
      "title": "caption text",
      "privacy_level": "SELF_ONLY"  # optional
    }
    """
    # Protect this too (so random users can't publish from your server)
    ok, resp = require_cron_secret()
    if not ok:
        return resp

    data = request.get_json(silent=True) or {}
    open_id = data.get("open_id")
    video_url = data.get("video_url")
    title = data.get("title", "Cre8 Studio")
    privacy_level = data.get("privacy_level", "SELF_ONLY")

    if not open_id or not video_url:
        return jsonify({"error": "open_id and video_url are required"}), 400

    _, doc = get_account_doc(open_id)
    if not doc:
        return jsonify({"error": "open_id not found in Firestore", "open_id": open_id}), 404

    access_token = doc.get("access_token")
    if not access_token:
        return jsonify({"error": "Missing access_token in Firestore", "open_id": open_id}), 500

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url,
        }
    }

    try:
        r = requests.post(PUBLISH_INIT_ENDPOINT, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        out = r.json()

        # store publish attempt (optional but recommended)
        publish_id = None
        if isinstance(out, dict):
            publish_id = (out.get("data") or {}).get("publish_id")

        if publish_id:
            db.collection(COLL).document(open_id).collection("publishes").document(publish_id).set({
                "publish_id": publish_id,
                "video_url": video_url,
                "title": title,
                "privacy_level": privacy_level,
                "created_at": firestore.SERVER_TIMESTAMP,
                "raw_response": out,
            }, merge=True)

        return jsonify({"status": "ok", "open_id": open_id, "response": out})
    except Exception as e:
        return jsonify({"status": "error", "open_id": open_id, "error": str(e)}), 500


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)

