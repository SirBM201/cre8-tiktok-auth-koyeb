import os
import time
import secrets
import requests
import boto3

from flask import Flask, request, redirect, jsonify
from urllib.parse import urlencode
from google.cloud import firestore

app = Flask(__name__)

# -------------------------
# ENV VARS (TikTok)
# -------------------------
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

# -------------------------
# ENV VARS (Firestore)
# -------------------------
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")
db = firestore.Client(project=PROJECT_ID or None)

# -------------------------
# ENV VARS (AWS S3)
# -------------------------
AWS_REGION = os.getenv("AWS_REGION", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")  # IMPORTANT: must be set in Koyeb env vars
s3 = boto3.client("s3", region_name=AWS_REGION or None)

# -------------------------
# ENV VARS (Cron protection)
# -------------------------
CRON_SECRET = os.getenv("CRON_SECRET", "")


# =========================
# Helpers
# =========================
def require_env(*names):
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


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


def token_expiring_soon(acct: dict, buffer_seconds: int = 600) -> bool:
    obtained_at = int(acct.get("obtained_at", 0) or 0)
    expires_in = int(acct.get("expires_in", 0) or 0)
    if not obtained_at or not expires_in:
        return True
    return time.time() >= (obtained_at + expires_in - buffer_seconds)


def get_account(open_id: str) -> dict | None:
    snap = db.collection(COLL).document(open_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


def save_account(open_id: str, doc: dict):
    doc["updated_at"] = firestore.SERVER_TIMESTAMP
    db.collection(COLL).document(open_id).set(doc, merge=True)


def ensure_fresh_access_token(open_id: str) -> str:
    acct = get_account(open_id)
    if not acct:
        raise RuntimeError(f"No TikTok account found for open_id={open_id}")

    access_token = acct.get("access_token")
    refresh_token_val = acct.get("refresh_token")

    if not refresh_token_val:
        raise RuntimeError("Missing refresh_token in Firestore")

    # Refresh if missing or expiring soon
    if (not access_token) or token_expiring_soon(acct):
        fresh = refresh_access_token(refresh_token_val)

        now = int(time.time())
        updated = {
            "open_id": open_id,
            "provider": "tiktok",
            "scope": fresh.get("scope", acct.get("scope")),
            "token_type": fresh.get("token_type", acct.get("token_type", "Bearer")),
            "access_token": fresh.get("access_token"),
            # TikTok may return a new refresh token; keep if provided, else keep old
            "refresh_token": fresh.get("refresh_token") or refresh_token_val,
            "expires_in": int(fresh.get("expires_in", 0) or 0),
            "refresh_expires_in": int(fresh.get("refresh_expires_in", 0) or 0),
            "obtained_at": now,
        }
        save_account(open_id, updated)
        return updated["access_token"]

    return access_token


def s3_head_object(key: str):
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    return s3.head_object(Bucket=S3_BUCKET, Key=key)


def s3_get_stream(key: str):
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    return s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"]


def require_cron_secret(req):
    if not CRON_SECRET:
        return False, ("CRON_SECRET not set on server", 500)
    got = req.headers.get("X-CRON-SECRET", "")
    if not got or got != CRON_SECRET:
        return False, ("Unauthorized", 401)
    return True, None


# =========================
# Routes
# =========================
@app.get("/")
def home():
    return jsonify({"status": "ok", "service": "cre8-tiktok-auth"})


@app.get("/api/tiktok/connect")
def tiktok_connect():
    try:
        require_env("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REDIRECT_URI")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    }
    save_account(open_id, doc)

    return jsonify({"status": "connected", "open_id": open_id, "stored_in_firestore": True, "collection": COLL})


# -------------------------
# Token refresh endpoints
# -------------------------
@app.post("/api/tiktok/refresh")
def api_refresh_one():
    body = request.get_json(force=True) or {}
    open_id = body.get("open_id")
    if not open_id:
        return jsonify({"error": "Provide open_id"}), 400

    acct = get_account(open_id)
    if not acct:
        return jsonify({"error": "Account not found", "open_id": open_id}), 404

    try:
        access = ensure_fresh_access_token(open_id)
        return jsonify({"status": "ok", "open_id": open_id, "refreshed": True, "access_token_present": bool(access)})
    except Exception as e:
        return jsonify({"status": "error", "open_id": open_id, "error": str(e)}), 500


@app.post("/api/tiktok/refresh_all")
def api_refresh_all():
    ok, err = require_cron_secret(request)
    if not ok:
        msg, code = err
        return jsonify({"error": msg}), code

    processed = 0
    refreshed = 0
    skipped = 0
    errors = 0

    for snap in db.collection(COLL).stream():
        processed += 1
        acct = snap.to_dict()
        open_id = acct.get("open_id") or snap.id
        try:
            if token_expiring_soon(acct):
                _ = ensure_fresh_access_token(open_id)
                refreshed += 1
            else:
                skipped += 1
        except Exception:
            errors += 1

    return jsonify({
        "status": "ok",
        "collection": COLL,
        "processed": processed,
        "refreshed": refreshed,
        "skipped": skipped,
        "errors": errors
    })


# -------------------------
# S3 sanity test (HEAD + size)
# -------------------------
@app.post("/api/s3/check")
def api_s3_check():
    body = request.get_json(force=True) or {}
    key = body.get("s3_key")
    if not key:
        return jsonify({"error": "Missing s3_key"}), 400
    try:
        meta = s3_head_object(key)
        return jsonify({
            "status": "ok",
            "bucket": S3_BUCKET,
            "s3_key": key,
            "size": meta.get("ContentLength"),
            "content_type": meta.get("ContentType"),
            "last_modified": str(meta.get("LastModified"))
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# -------------------------
# TikTok publish from S3 (FILE_UPLOAD)
# -------------------------
@app.post("/api/tiktok/publish_from_s3")
def tiktok_publish_from_s3():
    """
    Body JSON:
    {
      "open_id": "...",
      "s3_key": "posted/reels n shorts/9am content/2025-12-16 Black Flame Hunt.mp4",
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
        return jsonify({"error": "Missing open_id"}), 400
    if not s3_key:
        return jsonify({"error": "Missing s3_key"}), 400

    # 1) Ensure token valid (refresh if needed)
    try:
        access_token = ensure_fresh_access_token(open_id)
    except Exception as e:
        return jsonify({"status": "error", "step": "ensure_fresh_access_token", "error": str(e)}), 500

    # 2) Check S3 object exists (helps catch wrong key/spaces)
    try:
        meta = s3_head_object(s3_key)
        file_size = int(meta.get("ContentLength", 0) or 0)
        if file_size <= 0:
            return jsonify({"status": "error", "error": "S3 object size is 0"}), 400
    except Exception as e:
        return jsonify({"status": "error", "step": "s3_head_object", "error": str(e)}), 500

    # 3) TikTok init: FILE_UPLOAD (avoids url_ownership_unverified)
    init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
            "disable_comment": False,
            "disable_duet": False,
            "disable_stitch": False
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
        }
    }

    r = requests.post(init_url, headers=headers, json=payload, timeout=60)
    try:
        init_data = r.json()
    except Exception:
        init_data = {"raw": r.text}

    if r.status_code >= 400:
        return jsonify({"status": "error", "step": "tiktok_init", "http": r.status_code, "tiktok": init_data}), 400

    # Expected: upload_url + publish_id in response data
    data = init_data.get("data", {}) or {}
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")

    if not upload_url or not publish_id:
        return jsonify({"status": "error", "step": "parse_init_response", "tiktok": init_data}), 500

    # 4) Upload bytes to TikTok upload_url (PUT)
    try:
        stream = s3_get_stream(s3_key)
        # TikTok expects raw bytes PUT
        put_headers = {"Content-Type": "video/mp4"}
        put_resp = requests.put(upload_url, data=stream, headers=put_headers, timeout=600)
        if put_resp.status_code >= 400:
            return jsonify({
                "status": "error",
                "step": "upload_put",
                "http": put_resp.status_code,
                "raw": put_resp.text[:500],
                "publish_id": publish_id
            }), 400
    except Exception as e:
        return jsonify({"status": "error", "step": "upload_put_exception", "error": str(e), "publish_id": publish_id}), 500

    # 5) Return publish_id (TikTok processes async)
    return jsonify({
        "status": "ok",
        "publish_id": publish_id,
        "open_id": open_id,
        "s3_key": s3_key,
        "file_size": file_size,
        "note": "Upload complete. TikTok will process asynchronously."
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
