import os
import time
import math
import secrets
import requests
import boto3
from urllib.parse import urlencode
from flask import Flask, request, redirect, jsonify, abort
from google.cloud import firestore

app = Flask(__name__)

# -----------------------------
# ENV
# -----------------------------
# TikTok
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"
PUBLISH_INIT_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
PUBLISH_STATUS_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

# Firestore
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")

db = firestore.Client(project=PROJECT_ID or None)

# AWS S3
AWS_REGION = os.getenv("AWS_REGION", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")  # MUST be set in Koyeb env vars
s3 = boto3.client("s3", region_name=AWS_REGION or None)

# Cron protection
CRON_SECRET = os.getenv("CRON_SECRET", "")  # set in Koyeb env vars
CRON_HEADER = "X-CRON-SECRET"

# Optional behavior
# If true, we try PULL_FROM_URL first; if TikTok says url_ownership_unverified, we fallback to FILE_UPLOAD
TRY_PULL_FROM_URL_FIRST = os.getenv("TRY_PULL_FROM_URL_FIRST", "false").lower() == "true"


# -----------------------------
# Helpers
# -----------------------------
def require_env():
    missing = []
    for k in ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REDIRECT_URI"]:
        if not os.getenv(k):
            missing.append(k)
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


def check_cron_secret():
    if not CRON_SECRET:
        return False
    got = request.headers.get(CRON_HEADER, "")
    return secrets.compare_digest(got, CRON_SECRET)


def build_auth_url(state: str) -> str:
    params = {
        "client_key": CLIENT_KEY,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


def tiktok_token_request(payload: dict) -> dict:
    r = requests.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def exchange_code_for_token(code: str) -> dict:
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }
    return tiktok_token_request(payload)


def refresh_access_token(refresh_token: str) -> dict:
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    return tiktok_token_request(payload)


def get_account(open_id: str) -> dict:
    snap = db.collection(COLL).document(open_id).get()
    if not snap.exists:
        raise KeyError("open_id not found in Firestore")
    return snap.to_dict() or {}


def store_account(open_id: str, token_json: dict):
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


def token_expiring_soon(acct: dict, buffer_seconds: int = 300) -> bool:
    obtained_at = int(acct.get("obtained_at", 0) or 0)
    expires_in = int(acct.get("expires_in", 0) or 0)
    if not obtained_at or not expires_in:
        return True
    return (obtained_at + expires_in - buffer_seconds) <= int(time.time())


def ensure_fresh_token(open_id: str) -> dict:
    acct = get_account(open_id)
    if not acct.get("access_token"):
        raise RuntimeError("Account missing access_token")
    if not acct.get("refresh_token"):
        raise RuntimeError("Account missing refresh_token")

    if not token_expiring_soon(acct):
        return acct

    token_json = refresh_access_token(acct["refresh_token"])
    # TikTok returns open_id again (or sometimes not); keep original open_id
    store_account(open_id, token_json)
    return get_account(open_id)


def s3_head(key: str) -> dict:
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    return s3.head_object(Bucket=S3_BUCKET, Key=key)


def presign_s3_url(key: str, expires_seconds: int = 3600) -> str:
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires_seconds,
    )


def tiktok_publish_init(access_token: str, payload: dict) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}
    r = requests.post(PUBLISH_INIT_ENDPOINT, headers=headers, json=payload, timeout=60)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return r.status_code, data


def tiktok_upload_file(upload_url: str, key: str, total_size: int, content_type: str, chunk_size: int = 10_000_000):
    """
    Upload S3 object to TikTok upload_url in chunks with Content-Range.
    """
    # Stream from S3
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    body = obj["Body"]

    sent = 0
    part_index = 0

    while sent < total_size:
        to_read = min(chunk_size, total_size - sent)
        chunk = body.read(to_read)
        if not chunk:
            break

        start = sent
        end = sent + len(chunk) - 1

        headers = {
            "Content-Type": content_type or "video/mp4",
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{total_size}",
        }

        # TikTok requires PUT to the full upload_url (including querystring)
        resp = requests.put(upload_url, headers=headers, data=chunk, timeout=120)
        if resp.status_code >= 400:
            raise RuntimeError(f"TikTok upload failed at chunk {part_index}: {resp.status_code} {resp.text[:300]}")

        sent += len(chunk)
        part_index += 1


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def home():
    return jsonify({"status": "ok", "service": "cre8-tiktok-auth-koyeb"})


@app.get("/api/tiktok/connect")
def tiktok_connect():
    require_env()
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

    store_account(open_id, token_json)
    return jsonify({"status": "connected", "open_id": open_id, "collection": COLL})


# ---- Refresh one token ----
@app.post("/api/tiktok/refresh_one")
def api_refresh_one():
    body = request.get_json(force=True) or {}
    open_id = body.get("open_id")
    if not open_id:
        return jsonify({"error": "Missing open_id"}), 400

    acct = get_account(open_id)
    if not acct.get("refresh_token"):
        return jsonify({"error": "Missing refresh_token in Firestore", "open_id": open_id}), 500

    token_json = refresh_access_token(acct["refresh_token"])
    store_account(open_id, token_json)
    return jsonify({"status": "ok", "open_id": open_id, "refreshed": True})


# ---- Refresh all tokens (protected) ----
@app.post("/api/tiktok/refresh_all")
def api_refresh_all():
    if not check_cron_secret():
        return jsonify({"error": "Forbidden"}), 403

    processed = 0
    refreshed = 0
    skipped = 0
    errors = 0

    for doc in db.collection(COLL).stream():
        processed += 1
        open_id = doc.id
        acct = doc.to_dict() or {}
        try:
            if not token_expiring_soon(acct):
                skipped += 1
                continue
            if not acct.get("refresh_token"):
                errors += 1
                continue
            token_json = refresh_access_token(acct["refresh_token"])
            store_account(open_id, token_json)
            refreshed += 1
        except Exception:
            errors += 1

    return jsonify({"status": "ok", "processed": processed, "refreshed": refreshed, "skipped": skipped, "errors": errors})


# ---- S3 check (confirm object exists + readable by server) ----
@app.post("/api/s3/check")
def api_s3_check():
    body = request.get_json(force=True) or {}
    key = body.get("s3_key")
    if not key:
        return jsonify({"error": "Missing s3_key"}), 400
    try:
        meta = s3_head(key)
        return jsonify({
            "status": "ok",
            "bucket": S3_BUCKET,
            "s3_key": key,
            "size": meta.get("ContentLength"),
            "content_type": meta.get("ContentType"),
            "last_modified": str(meta.get("LastModified")),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ---- S3 presign (if you still want to test a URL in browser) ----
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


# ---- Publish from S3 (best-practice: FILE_UPLOAD) ----
@app.post("/api/tiktok/publish_from_s3")
def api_publish_from_s3():
    body = request.get_json(force=True) or {}

    open_id = body.get("open_id")
    s3_key = body.get("s3_key")
    title = body.get("title", "Cre8 Studio post")
    privacy_level = body.get("privacy_level", "SELF_ONLY")

    if not open_id:
        return jsonify({"error": "Missing open_id"}), 400
    if not s3_key:
        return jsonify({"error": "Missing s3_key"}), 400

    # Ensure token is fresh (refresh if expiring)
    acct = ensure_fresh_token(open_id)
    access_token = acct.get("access_token")

    # Confirm S3 object exists and get size/type
    meta = s3_head(s3_key)
    total_size = int(meta.get("ContentLength", 0) or 0)
    content_type = meta.get("ContentType") or "video/mp4"

    if total_size <= 0:
        return jsonify({"error": "S3 file size invalid", "s3_key": s3_key}), 400

    # Optional: try PULL_FROM_URL first (will fail unless you verify URL ownership in TikTok)
    if TRY_PULL_FROM_URL_FIRST:
        pull_url = presign_s3_url(s3_key, expires_seconds=3600)
        pull_payload = {
            "post_info": {
                "title": title,
                "privacy_level": privacy_level,
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": {"source": "PULL_FROM_URL", "video_url": pull_url},
        }
        code, data = tiktok_publish_init(access_token, pull_payload)
        if code < 400 and (data.get("error", {}) or {}).get("code") == "ok":
            return jsonify({"status": "ok", "method": "PULL_FROM_URL", "tiktok": data, "video_url_used": pull_url})

        # If it failed because of ownership, fall back to FILE_UPLOAD
        err_code = ((data.get("error") or {}).get("code")) or (((data.get("error") or {}).get("code")) if isinstance(data.get("error"), dict) else None)
        # some responses nest error differently; keep simple:
        if "url_ownership_unverified" not in str(data):
            return jsonify({"status": "error", "http": code, "method": "PULL_FROM_URL", "tiktok": data, "video_url_test": pull_url}), 400

    # FILE_UPLOAD init
    chunk_size = int(os.getenv("TIKTOK_CHUNK_SIZE", "10000000"))  # 10MB default
    total_chunks = int(math.ceil(total_size / chunk_size))

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
            "video_size": total_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }

    code, data = tiktok_publish_init(access_token, init_payload)

    # If token invalid, refresh and retry once
    if code == 401 or "access_token_invalid" in str(data):
        acct = ensure_fresh_token(open_id)  # forces refresh if needed
        access_token = acct.get("access_token")
        code, data = tiktok_publish_init(access_token, init_payload)

    if code >= 400:
        return jsonify({"status": "error", "http": code, "step": "tiktok_init", "tiktok": data}), 400

    tiktok_data = data.get("data") or {}
    upload_url = tiktok_data.get("upload_url")
    publish_id = tiktok_data.get("publish_id")

    if not upload_url or not publish_id:
        return jsonify({"status": "error", "step": "tiktok_init", "tiktok": data, "message": "Missing upload_url or publish_id"}), 400

    # Upload bytes to TikTok
    try:
        tiktok_upload_file(
            upload_url=upload_url,
            key=s3_key,
            total_size=total_size,
            content_type=content_type,
            chunk_size=chunk_size,
        )
    except Exception as e:
        return jsonify({"status": "error", "step": "upload", "publish_id": publish_id, "error": str(e)}), 500

    return jsonify({
        "status": "ok",
        "method": "FILE_UPLOAD",
        "publish_id": publish_id,
        "uploaded": True,
        "s3_key": s3_key,
        "size": total_size,
        "content_type": content_type,
        "chunks": total_chunks
    })


# ---- Check publish status by publish_id ----
@app.post("/api/tiktok/publish_status")
def api_publish_status():
    body = request.get_json(force=True) or {}
    open_id = body.get("open_id")
    publish_id = body.get("publish_id")
    if not open_id or not publish_id:
        return jsonify({"error": "Missing open_id or publish_id"}), 400

    acct = ensure_fresh_token(open_id)
    access_token = acct.get("access_token")
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}
    payload = {"publish_id": publish_id}

    r = requests.post(PUBLISH_STATUS_ENDPOINT, headers=headers, json=payload, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    return jsonify({"http": r.status_code, "tiktok": data})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
