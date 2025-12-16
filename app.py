import os
import time
import math
import secrets
import requests
import boto3
from urllib.parse import urlencode
from flask import Flask, request, redirect, jsonify
from google.cloud import firestore

app = Flask(__name__)

# -----------------------------
# TikTok env
# -----------------------------
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_OPEN_API_BASE = "https://open.tiktokapis.com"

# -----------------------------
# Firestore
# -----------------------------
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")  # example: cre8-studio
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")
db = firestore.Client(project=PROJECT_ID or None)

# -----------------------------
# AWS S3
# -----------------------------
AWS_REGION = os.getenv("AWS_REGION", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
s3 = boto3.client("s3", region_name=AWS_REGION or None)

# -----------------------------
# Helpers
# -----------------------------
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
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires_seconds,
    )

def s3_head_size(key: str) -> int:
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    head = s3.head_object(Bucket=S3_BUCKET, Key=key)
    return int(head["ContentLength"])

def s3_get_range(key: str, start: int, end: int) -> bytes:
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key, Range=f"bytes={start}-{end}")
    return resp["Body"].read()

def get_access_token_from_firestore(open_id: str) -> str | None:
    snap = db.collection(COLL).document(open_id).get()
    if not snap.exists:
        return None
    acct = snap.to_dict() or {}
    return acct.get("access_token")

# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return "ok", 200

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

# ---- S3 TEST ENDPOINT (confirm AWS access works) ----
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

# ---------------------------------------------------
# TikTok publish init (UPDATED: FILE_UPLOAD from S3)
# ---------------------------------------------------
@app.post("/api/tiktok/publish_init")
def tiktok_publish_init():
    """
    Body JSON:
    {
      "open_id": "...",
      "s3_key": "posted/reels n shorts/9am content/2025-12-14 Faceless Coronation.mp4",
      "title": "Cre8 Studio test post",
      "privacy_level": "SELF_ONLY"
    }
    """
    body = request.get_json(force=True) or {}

    open_id = body.get("open_id")
    s3_key = body.get("s3_key")
    title = body.get("title", "Cre8 Studio post")
    privacy_level = body.get("privacy_level", "SELF_ONLY")

    if not open_id:
        return jsonify({"status": "error", "error": "Missing open_id"}), 400
    if not s3_key:
        return jsonify({"status": "error", "error": "Missing s3_key"}), 400
    if not S3_BUCKET:
        return jsonify({"status": "error", "error": "Missing S3_BUCKET env var"}), 500

    access_token = get_access_token_from_firestore(open_id)
    if not access_token:
        return jsonify({"status": "error", "error": "No TikTok account found / missing access_token", "open_id": open_id}), 404

    # 1) Read size from S3
    try:
        video_size = s3_head_size(s3_key)
    except Exception as e:
        return jsonify({"status": "error", "error": "S3 head_object failed", "details": str(e)}), 500

    # 2) Init Direct Post with FILE_UPLOAD
    # Chunking
    chunk_size = int(os.getenv("TIKTOK_CHUNK_SIZE", "10000000"))  # 10MB default
    total_chunks = int(math.ceil(video_size / chunk_size))

    init_payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }

    try:
        r = requests.post(
            f"{TIKTOK_OPEN_API_BASE}/v2/post/publish/video/init/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=init_payload,
            timeout=60,
        )
        init_data = r.json()
    except Exception as e:
        return jsonify({"status": "error", "error": "TikTok init request failed", "details": str(e)}), 502

    if r.status_code >= 400:
        return jsonify({"status": "error", "http": r.status_code, "tiktok": init_data}), 400

    data = init_data.get("data") or {}
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")

    if not publish_id or not upload_url:
        return jsonify({"status": "error", "error": "Missing publish_id/upload_url from TikTok", "tiktok": init_data}), 400

    # 3) Upload chunks to TikTok upload_url
    try:
        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size - 1, video_size - 1)

            chunk = s3_get_range(s3_key, start, end)

            put_headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{video_size}",
            }

            put = requests.put(upload_url, headers=put_headers, data=chunk, timeout=180)
            if put.status_code not in (200, 201, 204):
                return jsonify({
                    "status": "error",
                    "error": "TikTok upload failed",
                    "chunk_index": i,
                    "http": put.status_code,
                    "response_text": (put.text or "")[:800],
                    "publish_id": publish_id,
                }), 400

        # If upload succeeds, TikTok will process the publish_id asynchronously.
        return jsonify({
            "status": "ok",
            "open_id": open_id,
            "s3_bucket": S3_BUCKET,
            "s3_key": s3_key,
            "publish_id": publish_id,
            "uploaded_chunks": total_chunks,
            "note": "Upload complete. TikTok will process/publish asynchronously for this publish_id."
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "error": "Upload loop failed", "details": str(e), "publish_id": publish_id}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
