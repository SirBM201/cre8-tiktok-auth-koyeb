import os
import time
import json
import secrets
import urllib.parse
import requests
import boto3
from flask import Flask, request, jsonify, redirect

app = Flask(__name__)

# -----------------------------
# AWS / S3
# -----------------------------
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "fair-video-source")

s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
)

def presign_s3_get_url(bucket: str, key: str, expires_seconds: int = 3600) -> str:
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=int(expires_seconds),
    )

def head_s3_object(bucket: str, key: str):
    return s3.head_object(Bucket=bucket, Key=key)

# -----------------------------
# TikTok OAuth + API
# -----------------------------
TIKTOK_BASE = os.getenv("TIKTOK_BASE", "https://open.tiktokapis.com")

TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")

# MUST match TikTok developer portal redirect URI exactly
# Example: https://available-doro-bmsconcept-5df3438a.koyeb.app/api/tiktok/callback
TIKTOK_REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI", "")

# Requested scopes
TIKTOK_SCOPES = os.getenv("TIKTOK_SCOPES", "user.info.basic,video.publish")

AUTHORIZE_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = f"{TIKTOK_BASE}/v2/oauth/token/"

TIKTOK_VIDEO_INIT_ENDPOINT = os.getenv(
    "TIKTOK_VIDEO_INIT_ENDPOINT",
    f"{TIKTOK_BASE}/v2/post/publish/video/init/"
)

TIKTOK_STATUS_ENDPOINT = os.getenv(
    "TIKTOK_STATUS_ENDPOINT",
    f"{TIKTOK_BASE}/v2/post/publish/status/fetch/"
)

def tiktok_headers(access_token: str):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=utf-8",
    }

def require_env_or_error():
    missing = []
    if not TIKTOK_CLIENT_KEY: missing.append("TIKTOK_CLIENT_KEY")
    if not TIKTOK_CLIENT_SECRET: missing.append("TIKTOK_CLIENT_SECRET")
    if not TIKTOK_REDIRECT_URI: missing.append("TIKTOK_REDIRECT_URI")
    if missing:
        return jsonify({
            "status": "error",
            "message": "Missing required env vars",
            "missing": missing
        }), 500
    return None

def build_authorize_url(state: str) -> str:
    # Properly URL-encode redirect URI and build a clean authorize link
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "scope": TIKTOK_SCOPES,
        "response_type": "code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
    }
    return AUTHORIZE_ENDPOINT + "?" + urllib.parse.urlencode(params, safe=",:/")

def exchange_code_for_token(code: str) -> dict:
    # TikTok expects x-www-form-urlencoded
    payload = {
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
    }
    r = requests.post(
        TOKEN_ENDPOINT,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
        timeout=60,
    )
    try:
        out = r.json()
    except Exception:
        raise RuntimeError(f"TikTok token endpoint returned non-JSON: {r.text[:2000]}")
    out["_http"] = r.status_code
    return out

def refresh_access_token(refresh_token: str) -> dict:
    payload = {
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(
        TOKEN_ENDPOINT,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
        timeout=60,
    )
    try:
        out = r.json()
    except Exception:
        raise RuntimeError(f"TikTok refresh endpoint returned non-JSON: {r.text[:2000]}")
    out["_http"] = r.status_code
    return out

# -----------------------------
# Health check
# -----------------------------
@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

# -----------------------------
# S3 presign (debug helper)
# -----------------------------
@app.post("/api/s3/presign")
def api_s3_presign():
    data = request.get_json(force=True, silent=False)
    s3_key = data.get("s3_key")
    expires = int(data.get("expires_seconds", 3600))

    if not s3_key:
        return jsonify({"status": "error", "message": "Missing s3_key"}), 400

    meta = head_s3_object(S3_BUCKET, s3_key)
    url = presign_s3_get_url(S3_BUCKET, s3_key, expires_seconds=expires)

    return jsonify({
        "status": "ok",
        "bucket": S3_BUCKET,
        "s3_key": s3_key,
        "size": meta.get("ContentLength"),
        "content_type": meta.get("ContentType"),
        "expires_seconds": expires,
        "url": url
    }), 200

# -----------------------------
# TikTok OAuth: start login
# -----------------------------
@app.get("/api/tiktok/login")
def tiktok_login():
    err = require_env_or_error()
    if err: return err

    # generate a random state (helps protect against CSRF)
    state = request.args.get("state") or f"cre8_{secrets.token_urlsafe(12)}"
    auth_url = build_authorize_url(state)

    # Return URL (so you can copy it) or redirect automatically
    mode = request.args.get("mode", "redirect")  # redirect | json
    if mode == "json":
        return jsonify({"status": "ok", "authorize_url": auth_url, "state": state}), 200
    return redirect(auth_url)

# -----------------------------
# TikTok OAuth: callback (THIS FIXES YOUR 404)
# -----------------------------
@app.get("/api/tiktok/callback")
def tiktok_callback():
    err = require_env_or_error()
    if err: return err

    code = request.args.get("code")
    state = request.args.get("state")

    if not code:
        return jsonify({"status": "error", "message": "Missing code in callback"}), 400

    try:
        out = exchange_code_for_token(code)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 502

    http = out.get("_http", 500)

    # TikTok typically returns {"data": {...}} on success
    if http >= 400 or out.get("error"):
        return jsonify({
            "status": "error",
            "step": "exchange_code",
            "http": http,
            "tiktok": out,
            "state": state
        }), 400

    return jsonify({
        "status": "ok",
        "step": "oauth_complete",
        "state": state,
        "tiktok": out
    }), 200

# -----------------------------
# TikTok OAuth: refresh helper
# -----------------------------
@app.post("/api/tiktok/refresh")
def api_tiktok_refresh():
    err = require_env_or_error()
    if err: return err

    data = request.get_json(force=True, silent=False)
    refresh_token_in = data.get("refresh_token")
    if not refresh_token_in:
        return jsonify({"status": "error", "message": "Missing refresh_token"}), 400

    try:
        out = refresh_access_token(refresh_token_in)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 502

    http = out.get("_http", 500)
    if http >= 400 or out.get("error"):
        return jsonify({"status": "error", "http": http, "tiktok": out}), 400

    return jsonify({"status": "ok", "tiktok": out}), 200

# -----------------------------
# TikTok publish using PULL_FROM_URL (Option A)
# -----------------------------
@app.post("/api/tiktok/publish_from_s3_pull")
def publish_from_s3_pull():
    """
    Body JSON:
    {
      "open_id": "xxx",                 (optional if access token belongs to user; but keep it)
      "access_token": "xxx",            (optional if you provide refresh_token)
      "refresh_token": "xxx",           (optional; if provided, we refresh automatically)
      "s3_key": "posted/.../file.mp4",
      "title": "Cre8 Studio test post",
      "privacy_level": "SELF_ONLY"
    }
    """
    data = request.get_json(force=True, silent=False)

    open_id = data.get("open_id")
    access_token = data.get("access_token")
    refresh_token_in = data.get("refresh_token")

    s3_key = data.get("s3_key")
    title = data.get("title", "Cre8 Studio post")
    privacy_level = data.get("privacy_level", "SELF_ONLY")

    if not s3_key:
        return jsonify({"status": "error", "message": "Missing s3_key"}), 400

    # If access token missing/expired, refresh automatically (if refresh_token provided)
    if (not access_token) and refresh_token_in:
        err = require_env_or_error()
        if err: return err
        try:
            ref = refresh_access_token(refresh_token_in)
        except Exception as e:
            return jsonify({"status": "error", "step": "refresh_token", "message": str(e)}), 502

        if ref.get("_http", 500) >= 400 or ref.get("error"):
            return jsonify({"status": "error", "step": "refresh_token", "tiktok": ref}), 400

        ref_data = ref.get("data", {})
        access_token = ref_data.get("access_token") or ref_data.get("accessToken")
        open_id = open_id or ref_data.get("open_id") or ref_data.get("openId")

    if not access_token:
        return jsonify({"status": "error", "message": "Missing access_token (or provide refresh_token)"}), 400
    if not open_id:
        return jsonify({"status": "error", "message": "Missing open_id"}), 400

    # 1) confirm S3 object exists + get size
    head = head_s3_object(S3_BUCKET, s3_key)
    video_size = int(head.get("ContentLength", 0))
    content_type = head.get("ContentType", "")

    if video_size <= 0:
        return jsonify({"status": "error", "message": "S3 object has invalid size"}), 400

    # 2) generate pre-signed URL (2 hours)
    presigned_url = presign_s3_get_url(S3_BUCKET, s3_key, expires_seconds=7200)

    # 3) TikTok init payload for pull-from-url
    payload = {
        "open_id": open_id,
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": presigned_url,
        }
    }

    r = requests.post(
        TIKTOK_VIDEO_INIT_ENDPOINT,
        headers=tiktok_headers(access_token),
        data=json.dumps(payload),
        timeout=60
    )

    try:
        out = r.json()
    except Exception:
        return jsonify({
            "status": "error",
            "http": r.status_code,
            "message": "TikTok returned non-JSON response",
            "raw": r.text[:2000]
        }), 502

    if r.status_code >= 400:
        return jsonify({
            "status": "error",
            "http": r.status_code,
            "step": "tiktok_init_pull",
            "tiktok": out,
            "debug": {
                "bucket": S3_BUCKET,
                "s3_key": s3_key,
                "video_size": video_size,
                "content_type": content_type,
                "init_endpoint": TIKTOK_VIDEO_INIT_ENDPOINT
            }
        }), 400

    data_node = out.get("data", {}) if isinstance(out, dict) else {}
    publish_id = data_node.get("publish_id") or data_node.get("publishId")
    video_id = data_node.get("video_id") or data_node.get("videoId")

    return jsonify({
        "status": "ok",
        "step": "tiktok_init_pull",
        "bucket": S3_BUCKET,
        "s3_key": s3_key,
        "video_size": video_size,
        "presigned_url_expires_seconds": 7200,
        "publish_id": publish_id,
        "video_id": video_id,
        "tiktok": out
    }), 200

# -----------------------------
# TikTok status/poll endpoint
# -----------------------------
@app.get("/api/tiktok/status")
def tiktok_status():
    access_token = request.args.get("access_token")
    publish_id = request.args.get("publish_id")
    video_id = request.args.get("video_id")

    if not access_token:
        return jsonify({"status": "error", "message": "Missing access_token"}), 400
    if not publish_id and not video_id:
        return jsonify({"status": "error", "message": "Missing publish_id or video_id"}), 400

    payload = {}
    if publish_id:
        payload["publish_id"] = publish_id
    if video_id:
        payload["video_id"] = video_id

    r = requests.post(
        TIKTOK_STATUS_ENDPOINT,
        headers=tiktok_headers(access_token),
        data=json.dumps(payload),
        timeout=60
    )

    try:
        out = r.json()
    except Exception:
        return jsonify({
            "status": "error",
            "http": r.status_code,
            "message": "TikTok returned non-JSON response",
            "raw": r.text[:2000]
        }), 502

    return jsonify({
        "status": "ok" if r.status_code < 400 else "error",
        "http": r.status_code,
        "endpoint": TIKTOK_STATUS_ENDPOINT,
        "tiktok": out
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
