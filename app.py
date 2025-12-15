import os
import time
import secrets
import requests
import boto3
from urllib.parse import urlencode
from flask import Flask, request, redirect, jsonify

from google.cloud import firestore

app = Flask(__name__)

# TikTok env
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

# Firestore
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")  # example: cre8-studio
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")
db = firestore.Client(project=PROJECT_ID or None)

# AWS S3
AWS_REGION = os.getenv("AWS_REGION", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
s3 = boto3.client("s3", region_name=AWS_REGION or None)

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

def presign_s3_url(key: str, expires_seconds: int = 3600) -> str:
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    # Key must be exact, including spaces (S3 supports spaces)
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires_seconds,
    )

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
        "access_token": token_json.get("access_token"),
        "refresh_token": token_json.get("refresh_token"),
        "expires_in": int(token_json.get("expires_in", 0) or 0),
        "refresh_expires_in": int(token_json.get("refresh_expires_in", 0) or 0),
        "obtained_at": now,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    db.collection(COLL).document(open_id).set(doc, merge=True)

    return jsonify({"status": "connected", "open_id": open_id, "stored_in_firestore": True, "collection": COLL})

# ---- S3 TEST ENDPOINT (use this to confirm AWS access is correct) ----
@app.post("/api/s3/presign")
def api_s3_presign():
    body = request.get_json(force=True) or {}
    key = body.get("s3_key")
    if not key:
        return jsonify({"error": "Missing s3_key"}), 400
    try:
        url = presign_s3_url(key, expires_seconds=3600)
        return jsonify({"status": "ok", "bucket": S3_BUCKET, "s3_key": key, "video_url": url})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# ---- TikTok publish init (uses S3 presigned URL) ----
@app.post("/api/tiktok/publish_init")
def tiktok_publish_init():
    body = request.get_json(force=True) or {}

    open_id = body.get("open_id")
    s3_key = body.get("s3_key")
    title = body.get("title", "Cre8 Studio post")
    privacy_level = body.get("privacy_level", "SELF_ONLY")

    if not open_id:
        return jsonify({"error": "Missing open_id"}), 400
    if not s3_key:
        return jsonify({"error": "Missing s3_key"}), 400

    snap = db.collection(COLL).document(open_id).get()
    if not snap.exists:
        return jsonify({"error": "No TikTok account found for open_id", "open_id": open_id}), 404

    acct = snap.to_dict()
    access_token = acct.get("access_token")
    if not access_token:
        return jsonify({"error": "Missing access_token in Firestore for open_id", "open_id": open_id}), 500

    try:
        video_url = presign_s3_url(s3_key, expires_seconds=3600)
    except Exception as e:
        return jsonify({"error": "Failed to presign S3 URL", "details": str(e)}), 500

    # TikTok publish init endpoint (Content Posting API)
    publish_endpoint = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

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
        },
    }

    r = requests.post(publish_endpoint, headers=headers, json=payload, timeout=60)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 400:
        return jsonify({"status": "error", "http": r.status_code, "tiktok": data, "video_url_test": video_url}), 400

    return jsonify({"status": "ok", "tiktok": data, "video_url_used": video_url})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
