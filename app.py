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

# ---------------------------
# TikTok OAuth / API
# ---------------------------
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

# Content Posting API (Direct Post)
PUBLISH_INIT_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/video/init/"


# ---------------------------
# Firestore
# ---------------------------
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")
db = firestore.Client(project=PROJECT_ID or None)


# ---------------------------
# AWS S3
# ---------------------------
AWS_REGION = os.getenv("AWS_REGION", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")
s3 = boto3.client("s3", region_name=AWS_REGION or None)

# Upload chunk size (bytes)
# TikTok example uses 10,000,000. We'll default to that. :contentReference[oaicite:2]{index=2}
TIKTOK_CHUNK_SIZE = int(os.getenv("TIKTOK_CHUNK_SIZE", "10000000"))

# Cron protection
CRON_SECRET = os.getenv("CRON_SECRET", "")


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


def is_token_expiring(acct: dict, skew_seconds: int = 120) -> bool:
    obtained_at = int(acct.get("obtained_at") or 0)
    expires_in = int(acct.get("expires_in") or 0)
    if not obtained_at or not expires_in:
        return True
    return (time.time() >= (obtained_at + expires_in - skew_seconds))


def get_valid_access_token(open_id: str) -> str:
    snap = db.collection(COLL).document(open_id).get()
    if not snap.exists:
        raise RuntimeError(f"No TikTok account found for open_id={open_id}")

    acct = snap.to_dict() or {}
    access_token = acct.get("access_token")
    refresh_token = acct.get("refresh_token")

    if not access_token:
        raise RuntimeError("Missing access_token in Firestore for this open_id")

    # Auto-refresh if expiring/expired
    if is_token_expiring(acct):
        if not refresh_token:
            raise RuntimeError("Access token expired and no refresh_token available")
        new_tokens = refresh_access_token(refresh_token)
        now = int(time.time())

        updated = {
            "access_token": new_tokens.get("access_token"),
            "refresh_token": new_tokens.get("refresh_token", refresh_token),
            "expires_in": int(new_tokens.get("expires_in", 0) or 0),
            "refresh_expires_in": int(new_tokens.get("refresh_expires_in", 0) or 0),
            "scope": new_tokens.get("scope", acct.get("scope")),
            "token_type": new_tokens.get("token_type", acct.get("token_type", "Bearer")),
            "obtained_at": now,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        db.collection(COLL).document(open_id).set(updated, merge=True)
        access_token = updated["access_token"]

        if not access_token:
            raise RuntimeError("Token refresh succeeded but access_token missing in response")

    return access_token


def s3_head(key: str) -> dict:
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    return s3.head_object(Bucket=S3_BUCKET, Key=key)


@app.get("/")
def home():
    return jsonify({"status": "ok", "service": "cre8-tiktok-auth-koyeb"})


@app.get("/api/tiktok/connect")
def tiktok_connect():
    if not (CLIENT_KEY and CLIENT_SECRET and REDIRECT_URI):
        return jsonify({"error": "Missing TikTok env vars"}), 500

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

    return jsonify({"status": "connected", "open_id": open_id, "collection": COLL})


# ---------------------------
# S3: check that file exists
# ---------------------------
@app.post("/api/s3/check")
def api_s3_check():
    body = request.get_json(force=True) or {}
    key = body.get("s3_key")
    if not key:
        return jsonify({"status": "error", "error": "Missing s3_key"}), 400
    try:
        meta = s3_head(key)
        return jsonify({
            "status": "ok",
            "bucket": S3_BUCKET,
            "s3_key": key,
            "size": int(meta.get("ContentLength") or 0),
            "content_type": meta.get("ContentType") or "",
            "last_modified": str(meta.get("LastModified") or ""),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ---------------------------
# TikTok: FILE_UPLOAD from S3
# ---------------------------
@app.post("/api/tiktok/publish_from_s3")
def tiktok_publish_from_s3():
    body = request.get_json(force=True) or {}

    open_id = body.get("open_id")
    s3_key = body.get("s3_key")
    title = body.get("title", "Cre8 Studio test post")
    privacy_level = body.get("privacy_level", "SELF_ONLY")

    if not open_id:
        return jsonify({"status": "error", "error": "Missing open_id"}), 400
    if not s3_key:
        return jsonify({"status": "error", "error": "Missing s3_key"}), 400

    try:
        # 1) Ensure token valid (auto refresh)
        access_token = get_valid_access_token(open_id)

        # 2) Read S3 metadata
        meta = s3_head(s3_key)
        video_size = int(meta.get("ContentLength") or 0)
        if video_size <= 0:
            return jsonify({"status": "error", "error": "S3 file size is 0"}), 400

        # TikTok requires chunk metadata for FILE_UPLOAD :contentReference[oaicite:3]{index=3}
        chunk_size = min(TIKTOK_CHUNK_SIZE, video_size)
        total_chunks = int(math.ceil(video_size / chunk_size))
        if total_chunks < 1:
            total_chunks = 1

        # 3) Init publish with FILE_UPLOAD
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}
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

        init_res = requests.post(PUBLISH_INIT_ENDPOINT, headers=headers, json=init_payload, timeout=60)
        init_json = init_res.json() if init_res.headers.get("content-type", "").startswith("application/json") else {"raw": init_res.text}

        if init_res.status_code >= 400:
            return jsonify({
                "status": "error",
                "step": "tiktok_init",
                "http": init_res.status_code,
                "tiktok": init_json,
                "debug": {"video_size": video_size, "chunk_size": chunk_size, "total_chunks": total_chunks},
            }), 400

        data = init_json.get("data") or {}
        publish_id = data.get("publish_id")
        upload_url = data.get("upload_url")
        if not upload_url:
            return jsonify({"status": "error", "step": "tiktok_init", "error": "upload_url missing", "tiktok": init_json}), 400

        # 4) Stream S3 -> TikTok upload_url in chunks
        obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
        stream = obj["Body"]

        sent = 0
        chunk_index = 0

        while sent < video_size:
            chunk = stream.read(chunk_size)
            if not chunk:
                break

            start = sent
            end = sent + len(chunk) - 1

            put_headers = {
                "Content-Type": meta.get("ContentType") or "video/mp4",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{video_size}",
            }

            put_res = requests.put(upload_url, headers=put_headers, data=chunk, timeout=120)
            if put_res.status_code >= 400:
                return jsonify({
                    "status": "error",
                    "step": "tiktok_upload",
                    "http": put_res.status_code,
                    "tiktok_upload_response": put_res.text[:500],
                    "debug": {"start": start, "end": end, "video_size": video_size, "chunk_index": chunk_index},
                }), 400

            sent += len(chunk)
            chunk_index += 1

        if sent != video_size:
            return jsonify({
                "status": "error",
                "step": "tiktok_upload",
                "error": "Upload incomplete",
                "debug": {"sent": sent, "video_size": video_size, "chunks_sent": chunk_index},
            }), 400

        return jsonify({
            "status": "ok",
            "publish_id": publish_id,
            "uploaded_bytes": sent,
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "note": "Upload complete. TikTok will process/publish asynchronously.",
        })

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ---------------------------
# Optional: cron-protected refresh_all placeholder
# ---------------------------
@app.post("/api/tiktok/refresh_all")
def tiktok_refresh_all():
    got = request.headers.get("X-CRON-SECRET", "")
    if not CRON_SECRET or got != CRON_SECRET:
        return jsonify({"status": "error", "error": "Forbidden"}), 403
    # Keep your earlier refresh_all logic here if you want.
    return jsonify({"status": "ok", "message": "refresh_all stub - add your logic"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
