import os
import time
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

# TikTok
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI")
SCOPES = os.getenv("TIKTOK_SCOPES", "video.publish,user.info.basic")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"
PUBLISH_INIT_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/video/init/"

# Firestore
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")  # e.g. cre8-studio
COLL = os.getenv("TIKTOK_FIRESTORE_COLLECTION", "tiktok_accounts")
db = firestore.Client(project=PROJECT_ID or None)

# AWS
AWS_REGION = os.getenv("AWS_REGION", "")
S3_BUCKET = os.getenv("S3_BUCKET", "")  # IMPORTANT: must be set in Koyeb env
s3 = boto3.client("s3", region_name=AWS_REGION or None)


# =========================
# HELPERS
# =========================

def _require_env():
    missing = []
    for k in ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REDIRECT_URI"]:
        if not os.getenv(k):
            missing.append(k)
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

def refresh_token_call(refresh_token: str) -> dict:
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def presign_s3_url(key: str, expires_seconds: int = 3600) -> str:
    if not S3_BUCKET:
        raise RuntimeError("Missing S3_BUCKET env var")
    if not AWS_REGION:
        # Not always required by boto3, but good to enforce
        raise RuntimeError("Missing AWS_REGION env var")

    # Key must be exact (spaces allowed in S3)
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires_seconds,
    )

def get_firestore_doc(open_id: str) -> dict:
    snap = db.collection(COLL).document(open_id).get()
    if not snap.exists:
        raise RuntimeError(f"No TikTok account found for open_id: {open_id}")
    return snap.to_dict() or {}

def refresh_access_token_if_needed(open_id: str) -> str:
    """
    Returns a valid access_token.
    Refreshes using refresh_token if expired or close to expiry.
    Updates Firestore on refresh.
    """
    acct = get_firestore_doc(open_id)

    now = int(time.time())
    access_token = acct.get("access_token")
    refresh_token = acct.get("refresh_token")

    expires_in = int(acct.get("expires_in", 0) or 0)
    obtained_at = int(acct.get("obtained_at", 0) or 0)

    # If we have a token and it is NOT expiring within 5 minutes, use it
    if access_token and expires_in and obtained_at:
        if (obtained_at + expires_in - 300) > now:
            return access_token

    # Otherwise refresh
    if not refresh_token:
        raise RuntimeError("Missing refresh_token. Please reconnect TikTok (/api/tiktok/connect).")

    data = refresh_token_call(refresh_token)

    new_access = data.get("access_token")
    if not new_access:
        raise RuntimeError(f"Refresh response missing access_token: {data}")

    updated = {
        "access_token": new_access,
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_in": int(data.get("expires_in", 0) or 0),
        "refresh_expires_in": int(data.get("refresh_expires_in", 0) or 0),
        "obtained_at": int(time.time()),
        "updated_at": firestore.SERVER_TIMESTAMP,
        "scope": data.get("scope", acct.get("scope")),
        "token_type": data.get("token_type", acct.get("token_type", "Bearer")),
    }

    db.collection(COLL).document(open_id).set(updated, merge=True)
    return new_access


# =========================
# ROUTES
# =========================

@app.get("/")
def home():
    return jsonify({"status": "ok", "service": "cre8-tiktok-auth-koyeb"})


# ---- TikTok OAuth ----

@app.get("/api/tiktok/connect")
def tiktok_connect():
    try:
        _require_env()
        state = secrets.token_urlsafe(24)
        auth_url = build_auth_url(state)

        resp = redirect(auth_url, code=302)
        resp.set_cookie(
            "tiktok_oauth_state",
            state,
            httponly=True,
            samesite="Lax",
            max_age=600
        )
        return resp
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.get("/api/tiktok/callback")
def tiktok_callback():
    try:
        code = request.args.get("code")
        state = request.args.get("state")
        saved_state = request.cookies.get("tiktok_oauth_state")

        if not code:
            return jsonify({"status": "error", "error": "Missing code"}), 400
        if not state or not saved_state or state != saved_state:
            return jsonify({"status": "error", "error": "Invalid state"}), 400

        token_json = exchange_code_for_token(code)
        open_id = token_json.get("open_id")
        if not open_id:
            return jsonify({"status": "error", "error": "Token response missing open_id", "raw": token_json}), 500

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

        return jsonify({
            "status": "connected",
            "open_id": open_id,
            "stored_in_firestore": True,
            "collection": COLL
        })

    except requests.HTTPError as e:
        return jsonify({"status": "error", "error": "TikTok token exchange failed", "details": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ---- Token refresh endpoints (manual / cron use) ----

@app.post("/api/tiktok/refresh")
def tiktok_refresh():
    try:
        body = request.get_json(force=True) or {}
        open_id = body.get("open_id")
        if not open_id:
            return jsonify({"status": "error", "error": "Missing open_id"}), 400

        new_access = refresh_access_token_if_needed(open_id)
        return jsonify({"status": "ok", "open_id": open_id, "access_token_refreshed": True, "access_token": new_access})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400


@app.post("/api/tiktok/refresh_all")
def tiktok_refresh_all():
    """
    Refreshes all accounts in the collection. Useful for cron jobs.
    Optional header protection: X-CRON-SECRET must match CRON_SECRET env var if set.
    """
    try:
        cron_secret = os.getenv("CRON_SECRET", "")
        if cron_secret:
            incoming = request.headers.get("X-CRON-SECRET", "")
            if incoming != cron_secret:
                return jsonify({"status": "error", "error": "Unauthorized"}), 401

        processed = 0
        refreshed = 0
        skipped = 0
        errors = 0

        docs = db.collection(COLL).stream()
        for snap in docs:
            processed += 1
            try:
                open_id = snap.id
                acct = snap.to_dict() or {}
                now = int(time.time())
                expires_in = int(acct.get("expires_in", 0) or 0)
                obtained_at = int(acct.get("obtained_at", 0) or 0)

                # If not near expiry, skip
                if acct.get("access_token") and expires_in and obtained_at and (obtained_at + expires_in - 300) > now:
                    skipped += 1
                    continue

                refresh_access_token_if_needed(open_id)
                refreshed += 1

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

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ---- S3 Test endpoint (confirms AWS access and presigning works) ----

@app.post("/api/s3/presign")
def api_s3_presign():
    try:
        body = request.get_json(force=True) or {}
        key = body.get("s3_key")
        if not key:
            return jsonify({"status": "error", "error": "Missing s3_key"}), 400

        url = presign_s3_url(key, expires_seconds=3600)
        return jsonify({"status": "ok", "bucket": S3_BUCKET, "s3_key": key, "video_url": url})

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ---- TikTok publish init (AUTO-REFRESH token + S3 presigned URL) ----

@app.post("/api/tiktok/publish_init")
def tiktok_publish_init():
    body = request.get_json(force=True) or {}

    open_id = body.get("open_id")
    s3_key = body.get("s3_key")
    title = body.get("title", "Cre8 Studio post")
    privacy_level = body.get("privacy_level", "SELF_ONLY")

    if not open_id:
        return jsonify({"status": "error", "error": "Missing open_id"}), 400
    if not s3_key:
        return jsonify({"status": "error", "error": "Missing s3_key"}), 400

    try:
        # 1) AUTO-REFRESH token (prevents access_token_invalid)
        access_token = refresh_access_token_if_needed(open_id)

        # 2) Presign S3 URL (TikTok pulls the video from this)
        video_url = presign_s3_url(s3_key, expires_seconds=3600)

        # 3) Call TikTok publish init
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

        r = requests.post(PUBLISH_INIT_ENDPOINT, headers=headers, json=payload, timeout=60)

        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        if r.status_code >= 400:
            # return the URL we used so you can test it in browser
            return jsonify({
                "status": "error",
                "http": r.status_code,
                "tiktok": data,
                "video_url_test": video_url
            }), 400

        return jsonify({
            "status": "ok",
            "tiktok": data,
            "video_url_used": video_url
        })

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# =========================
# RUN
# =========================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
