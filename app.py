import os
import time
import json
import base64
import secrets
import requests
import boto3
from urllib.parse import urlencode

from flask import Flask, request, redirect, jsonify
from google.cloud import firestore

app = Flask(__name__)

# =========================
# ENV
# =========================
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI", "")
TIKTOK_SCOPES = os.getenv("TIKTOK_SCOPES", "user.info.basic,video.publish")
TIKTOK_FIRESTORE_COLLECTION = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")

# Cron protection secret (YOU set this in Koyeb env vars)
CRON_SECRET = os.getenv("CRON_SECRET", "")

# AWS (for presigned URL)
AWS_REGION = os.getenv("AWS_REGION", "")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
S3_BUCKET = os.getenv("S3_BUCKET", "fair-video-source")

# =========================
# Firestore
# =========================
db = firestore.Client(project=FIREBASE_PROJECT_ID)
col = db.collection(TIKTOK_FIRESTORE_COLLECTION)

# =========================
# AWS S3
# =========================
s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)

# =========================
# Helpers
# =========================
def now_ts() -> int:
    return int(time.time())

def require_cron_secret():
    if not CRON_SECRET:
        return (jsonify({"error": "CRON_SECRET not set on server"}), 500)

    provided = request.headers.get("X-CRON-SECRET", "")
    if not provided or provided != CRON_SECRET:
        return (jsonify({"error": "Unauthorized: invalid X-CRON-SECRET"}), 401)
    return None

def tiktok_exchange_code_for_token(code: str):
    # TikTok OAuth token endpoint
    url = "https://open.tiktokapis.com/v2/oauth/token/"
    payload = {
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
    }
    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def tiktok_refresh_token(refresh_token: str):
    url = "https://open.tiktokapis.com/v2/oauth/token/"
    payload = {
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def presign_s3_url(bucket: str, key: str, expires_seconds: int = 3600) -> str:
    # Generates a temporary HTTPS URL TikTok can fetch
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_seconds,
    )

def token_is_expiring_soon(obtained_at: int, expires_in: int, buffer_seconds: int = 600) -> bool:
    # refresh if within 10 minutes of expiry
    return (obtained_at + expires_in) <= (now_ts() + buffer_seconds)

# =========================
# Routes
# =========================

@app.get("/")
def home():
    return "Cre8 Studio TikTok API server OK", 200

@app.get("/api/tiktok/connect")
def tiktok_connect():
    # Creates TikTok auth URL
    state = secrets.token_urlsafe(16)
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "scope": TIKTOK_SCOPES,
        "response_type": "code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
    }
    auth_url = "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)
    return redirect(auth_url, code=302)

@app.get("/api/tiktok/callback")
def tiktok_callback():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing code"}), 400

    data = tiktok_exchange_code_for_token(code)

    # Typical TikTok response shape includes: access_token, refresh_token, expires_in, open_id, scope, token_type
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = int(data.get("expires_in", 0))
    open_id = data.get("open_id")
    scope = data.get("scope", "")
    token_type = data.get("token_type", "Bearer")

    if not (access_token and refresh_token and open_id):
        return jsonify({"error": "Token exchange returned incomplete data", "raw": data}), 500

    doc = {
        "provider": "tiktok",
        "open_id": open_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "scope": scope,
        "token_type": token_type,
        "obtained_at": now_ts(),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    col.document(open_id).set(doc, merge=True)

    return jsonify({"collection": TIKTOK_FIRESTORE_COLLECTION, "open_id": open_id, "status": "ok"}), 200

@app.post("/api/tiktok/refresh")
def refresh_one():
    # Accepts open_id OR refresh_token in JSON
    body = request.get_json(silent=True) or {}
    open_id = body.get("open_id")
    refresh_token = body.get("refresh_token")

    if not open_id and not refresh_token:
        return jsonify({"error": "Provide open_id or refresh_token"}), 400

    if open_id:
        snap = col.document(open_id).get()
        if not snap.exists:
            return jsonify({"error": "open_id not found", "open_id": open_id}), 404
        doc = snap.to_dict()
        refresh_token = doc.get("refresh_token")
        obtained_at = int(doc.get("obtained_at", 0))
        expires_in = int(doc.get("expires_in", 0))

        if not token_is_expiring_soon(obtained_at, expires_in):
            return jsonify({"open_id": open_id, "reason": "not_expiring_soon", "status": "skipped"}), 200

    data = tiktok_refresh_token(refresh_token)

    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token", refresh_token)
    new_expires = int(data.get("expires_in", 0))
    new_open_id = data.get("open_id", open_id)

    if not (new_access and new_open_id):
        return jsonify({"error": "Refresh returned incomplete data", "raw": data}), 500

    col.document(new_open_id).set({
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expires_in": new_expires,
        "obtained_at": now_ts(),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)

    return jsonify({"open_id": new_open_id, "status": "refreshed"}), 200

@app.post("/api/tiktok/refresh_all")
def refresh_all():
    # Protected endpoint for cron
    auth_err = require_cron_secret()
    if auth_err:
        return auth_err

    processed = 0
    refreshed = 0
    skipped = 0
    errors = 0

    for snap in col.stream():
        processed += 1
        doc = snap.to_dict() or {}
        open_id = doc.get("open_id", snap.id)
        obtained_at = int(doc.get("obtained_at", 0))
        expires_in = int(doc.get("expires_in", 0))
        refresh_token = doc.get("refresh_token", "")

        try:
            if not token_is_expiring_soon(obtained_at, expires_in):
                skipped += 1
                continue

            data = tiktok_refresh_token(refresh_token)
            new_access = data.get("access_token")
            new_refresh = data.get("refresh_token", refresh_token)
            new_expires = int(data.get("expires_in", 0))

            if not new_access:
                raise RuntimeError(f"Missing access_token in refresh response: {data}")

            col.document(open_id).set({
                "access_token": new_access,
                "refresh_token": new_refresh,
                "expires_in": new_expires,
                "obtained_at": now_ts(),
                "updated_at": firestore.SERVER_TIMESTAMP,
            }, merge=True)

            refreshed += 1

        except Exception as e:
            errors += 1

    return jsonify({
        "collection": TIKTOK_FIRESTORE_COLLECTION,
        "processed": processed,
        "refreshed": refreshed,
        "skipped": skipped,
        "errors": errors,
        "status": "ok",
    }), 200

@app.post("/api/tiktok/publish_init")
def publish_init():
    """
    Body JSON:
    {
      "open_id": "...",
      "s3_key": "posted/reels n shorts/9am content/2025-12-14 Faceless Coronation.mp4",
      "title": "Cre8 Studio test post",
      "privacy_level": "SELF_ONLY"
    }
    """
    body = request.get_json(silent=True) or {}
    open_id = body.get("open_id")
    s3_key = body.get("s3_key")
    title = body.get("title", "")
    privacy_level = body.get("privacy_level", "SELF_ONLY")

    if not open_id or not s3_key:
        return jsonify({"error": "open_id and s3_key are required"}), 400

    snap = col.document(open_id).get()
    if not snap.exists:
        return jsonify({"error": "open_id not found", "open_id": open_id}), 404

    acct = snap.to_dict() or {}
    access_token = acct.get("access_token")
    if not access_token:
        return jsonify({"error": "Missing access_token for open_id", "open_id": open_id}), 500

    # Generate presigned URL (TikTok can fetch this)
    video_url = presign_s3_url(S3_BUCKET, s3_key, expires_seconds=3600)

    # TikTok init endpoint
    url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url,
        }
    }

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    # Return TikTok response even when not 200, to help debugging
    try:
        data = r.json()
    except Exception:
        data = {"raw_text": r.text}

    if r.status_code >= 400:
        return jsonify({"status": "error", "http": r.status_code, "tiktok": data}), 400

    return jsonify({"status": "ok", "open_id": open_id, "video_url": video_url, "tiktok": data}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
