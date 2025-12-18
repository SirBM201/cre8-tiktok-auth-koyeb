import os
import json
import uuid
import logging
from datetime import datetime, timezone

import boto3
import requests
from botocore.exceptions import ClientError
from flask import Flask, request, jsonify

APP_NAME = os.getenv("APP_NAME", "cre8-tiktok-auth-koyeb")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)

# ---- Simple CORS (no flask_cors needed) ----
ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "*")

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGINS
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp

@app.route("/<path:_path>", methods=["OPTIONS"])
def cors_preflight(_path):
    return ("", 204)

# -----------------------------
# Environment (S3)
# -----------------------------
AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
S3_BUCKET = os.getenv("S3_BUCKET", os.getenv("AWS_S3_BUCKET", "fair-video-source"))

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
HAS_AWS_KEYS = bool(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY)

s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)

# -----------------------------
# TikTok Endpoints (v2)
# -----------------------------
TIKTOK_INIT_ENDPOINT = os.getenv(
    "TIKTOK_INIT_ENDPOINT",
    "https://open.tiktokapis.com/v2/post/publish/video/init/",
)
TIKTOK_STATUS_ENDPOINT = os.getenv(
    "TIKTOK_STATUS_ENDPOINT",
    "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
)
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT_SECONDS", "25"))

# -----------------------------
# Helpers
# -----------------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def make_trace_id() -> str:
    return uuid.uuid4().hex[:12]

def json_error(message: str, trace_id: str, status_code: int = 400, extra: dict | None = None):
    payload = {
        "status": "error",
        "message": message,
        "trace_id": trace_id,
        "time_utc": now_utc_iso(),
    }
    if extra:
        payload["extra"] = extra
    return jsonify(payload), status_code

def require_fields(data: dict, fields: list[str], trace_id: str):
    missing = [f for f in fields if not data.get(f)]
    if missing:
        return json_error("Missing required field(s).", trace_id, 400, {"missing": missing})
    return None

def s3_head_object(s3_key: str) -> dict:
    return s3.head_object(Bucket=S3_BUCKET, Key=s3_key)

def s3_presign_get_url(s3_key: str, expires_seconds: int = 7200) -> str:
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=expires_seconds,
    )

def tiktok_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

def safe_log_json(label: str, obj: dict, max_len: int = 4000):
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) > max_len:
        s = s[:max_len] + "...(truncated)"
    logging.info("%s: %s", label, s)

# -----------------------------
# Global error handler
# -----------------------------
@app.errorhandler(Exception)
def handle_unexpected_error(e):
    trace_id = make_trace_id()
    logging.exception("Unhandled exception trace_id=%s: %s", trace_id, str(e))
    return json_error(
        "Internal server error in app. Check logs using trace_id.",
        trace_id,
        500,
        {"error_type": type(e).__name__},
    )

# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return jsonify({
        "app": APP_NAME,
        "status": "ok",
        "time_utc": now_utc_iso(),
        "bucket": S3_BUCKET,
        "region": AWS_REGION,
        "has_aws_keys": HAS_AWS_KEYS,
        "tiktok_init": f"POST {TIKTOK_INIT_ENDPOINT}",
        "tiktok_status": f"POST {TIKTOK_STATUS_ENDPOINT}",
    })

@app.get("/debug/routes")
def debug_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        methods = sorted([m for m in rule.methods if m not in ("HEAD", "OPTIONS")])
        routes.append({"path": str(rule), "methods": methods})
    return jsonify({"routes": routes})

@app.post("/api/s3/check")
def api_s3_check():
    trace_id = make_trace_id()
    data = request.get_json(silent=True) or {}
    err = require_fields(data, ["s3_key"], trace_id)
    if err:
        return err

    s3_key = data["s3_key"]

    try:
        meta = s3_head_object(s3_key)
        return jsonify({
            "status": "ok",
            "trace_id": trace_id,
            "bucket": S3_BUCKET,
            "s3_key": s3_key,
            "size": meta.get("ContentLength"),
            "content_type": meta.get("ContentType"),
            "etag": meta.get("ETag"),
        })
    except ClientError as ce:
        code = ce.response.get("Error", {}).get("Code", "Unknown")
        logging.warning("S3 check failed trace_id=%s code=%s key=%s", trace_id, code, s3_key)
        return json_error("S3 object not found or not accessible.", trace_id, 404, {"s3_key": s3_key, "aws_error_code": code})

@app.post("/api/s3/presign")
def api_s3_presign():
    trace_id = make_trace_id()
    data = request.get_json(silent=True) or {}
    err = require_fields(data, ["s3_key"], trace_id)
    if err:
        return err

    s3_key = data["s3_key"]
    expires_seconds = int(data.get("expires_seconds", 7200))

    meta = s3_head_object(s3_key)
    url = s3_presign_get_url(s3_key, expires_seconds=expires_seconds)

    return jsonify({
        "status": "ok",
        "trace_id": trace_id,
        "bucket": S3_BUCKET,
        "s3_key": s3_key,
        "expires_seconds": expires_seconds,
        "size": meta.get("ContentLength"),
        "content_type": meta.get("ContentType"),
        "url": url,
    })

@app.post("/api/tiktok/publish_from_s3_pull")
def api_tiktok_publish_from_s3_pull():
    trace_id = make_trace_id()
    data = request.get_json(silent=True) or {}

    err = require_fields(data, ["access_token", "s3_key", "title"], trace_id)
    if err:
        return err

    access_token = data["access_token"].strip()
    s3_key = data["s3_key"]
    title = data["title"]
    privacy_level = data.get("privacy_level", "SELF_ONLY")
    expires_seconds = int(data.get("expires_seconds", 7200))

    meta = s3_head_object(s3_key)
    size = int(meta.get("ContentLength", 0))
    content_type = meta.get("ContentType") or "video/mp4"

    video_url = s3_presign_get_url(s3_key, expires_seconds=expires_seconds)

    payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url,
            "video_size": size,
            "content_type": content_type,
        },
    }

    safe_log_json(f"[{trace_id}] TikTok init payload", {
        "post_info": payload["post_info"],
        "source_info": {
            "source": payload["source_info"]["source"],
            "video_url": "(presigned url hidden)",
            "video_size": payload["source_info"].get("video_size"),
            "content_type": payload["source_info"].get("content_type"),
        }
    })

    resp = requests.post(
        TIKTOK_INIT_ENDPOINT,
        headers=tiktok_headers(access_token),
        json=payload,
        timeout=HTTP_TIMEOUT,
    )

    try:
        resp_json = resp.json()
    except Exception:
        resp_json = {"raw_text": resp.text}

    safe_log_json(f"[{trace_id}] TikTok init response", {
        "status_code": resp.status_code,
        "body": resp_json
    })

    if resp.status_code >= 400:
        return jsonify({
            "status": "error",
            "trace_id": trace_id,
            "message": "TikTok init failed.",
            "tiktok_http_status": resp.status_code,
            "tiktok_response": resp_json,
        }), 400

    return jsonify({
        "status": "ok",
        "trace_id": trace_id,
        "s3_key": s3_key,
        "size": size,
        "privacy_level": privacy_level,
        "tiktok_http_status": resp.status_code,
        "tiktok_response": resp_json,
    })

@app.post("/api/tiktok/status")
def api_tiktok_status():
    trace_id = make_trace_id()
    data = request.get_json(silent=True) or {}

    err = require_fields(data, ["access_token", "publish_id"], trace_id)
    if err:
        return err

    access_token = data["access_token"].strip()
    publish_id = data["publish_id"].strip()

    payload = {"publish_id": publish_id}

    resp = requests.post(
        TIKTOK_STATUS_ENDPOINT,
        headers=tiktok_headers(access_token),
        json=payload,
        timeout=HTTP_TIMEOUT,
    )

    try:
        resp_json = resp.json()
    except Exception:
        resp_json = {"raw_text": resp.text}

    safe_log_json(f"[{trace_id}] TikTok status response", {
        "status_code": resp.status_code,
        "body": resp_json
    })

    if resp.status_code >= 400:
        return jsonify({
            "status": "error",
            "trace_id": trace_id,
            "message": "TikTok status fetch failed.",
            "tiktok_http_status": resp.status_code,
            "tiktok_response": resp_json,
        }), 400

    return jsonify({
        "status": "ok",
        "trace_id": trace_id,
        "tiktok_http_status": resp.status_code,
        "tiktok_response": resp_json,
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
