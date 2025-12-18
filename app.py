import os
import math
import uuid
import logging
from datetime import datetime, timezone

import boto3
import requests
from flask import Flask, request, jsonify, send_from_directory

# -----------------------------
# Basic logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cre8-tiktok-koyeb")

# -----------------------------
# Flask app
# -----------------------------
app = Flask(__name__)

# -----------------------------
# Simple CORS (NO flask_cors dependency)
# -----------------------------
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGINS
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    resp.headers["Access-Control-Max-Age"] = "3600"
    return resp

@app.route("/<path:path>", methods=["OPTIONS"])
def cors_preflight(path):
    return ("", 204)

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "app": os.getenv("APP_NAME", "cre8-tiktok-auth-koyeb"),
        "status": "ok",
        "message": "Cre8 TikTok API is running. Use /health and /debug/routes."
    })

# -----------------------------
# Environment
# -----------------------------
APP_NAME = os.getenv("APP_NAME", "cre8-tiktok-auth-koyeb")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "fair-video-source")

# TikTok endpoints
TIKTOK_INIT_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

# Upload tuning
DEFAULT_EXPIRES_SECONDS = int(os.getenv("DEFAULT_EXPIRES_SECONDS", "7200"))
S3_DOWNLOAD_MAX_MB = int(os.getenv("S3_DOWNLOAD_MAX_MB", "300"))

# Chunk size for upload (keep 5MB as safe standard)
TIKTOK_CHUNK_SIZE = int(os.getenv("TIKTOK_CHUNK_SIZE", str(5 * 1024 * 1024)))  # 5MB

# PUBLIC posting toggle (set to 1 only after TikTok audit approval)
TIKTOK_ALLOW_PUBLIC = os.getenv("TIKTOK_ALLOW_PUBLIC", "0").strip() == "1"

# Boto3 client
s3 = boto3.client("s3", region_name=AWS_REGION)

# -----------------------------
# Helpers
# -----------------------------
def trace_id():
    return uuid.uuid4().hex[:10]

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def json_body():
    try:
        data = request.get_json(force=True, silent=False)
        if data is None:
            raise ValueError("Empty JSON body.")
        return data
    except Exception as e:
        raise ValueError(f"Invalid JSON body: {e}")

def s3_head_object(bucket: str, key: str):
    return s3.head_object(Bucket=bucket, Key=key)

def tiktok_headers(access_token: str):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

def normalize_privacy_level(v: str) -> str:
    """
    Normalize common inputs to TikTok-supported values used in our system.
    """
    if not v:
        return "SELF_ONLY"
    v = str(v).strip().upper()
    mapping = {
        "PRIVATE": "SELF_ONLY",
        "SELF_ONLY": "SELF_ONLY",
        "PUBLIC": "PUBLIC_TO_EVERYONE",
        "PUBLIC_TO_EVERYONE": "PUBLIC_TO_EVERYONE",
        "EVERYONE": "PUBLIC_TO_EVERYONE",
    }
    return mapping.get(v, v)

def _extract_tiktok_data(resp_json: dict):
    data = resp_json.get("data") or {}
    err = resp_json.get("error") or {}
    return data, err

def tiktok_init_file_upload(access_token: str, video_size: int, title: str, privacy_level: str):
    """
    FILE_UPLOAD init.
    To keep your working path stable, we use single-chunk init for smaller files:
      chunk_size = video_size
      total_chunk_count = 1
    If file becomes larger later, switch to chunking via env or code expansion.
    """
    payload = {
        "post_info": {
            "title": title,
            "privacy_level": normalize_privacy_level(privacy_level),
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": int(video_size),

            # Keep your previously-working approach (single chunk)
            "chunk_size": int(video_size),
            "total_chunk_count": 1,
        }
    }

    r = requests.post(
        TIKTOK_INIT_ENDPOINT,
        headers=tiktok_headers(access_token),
        json=payload,
        timeout=60
    )
    return r, payload

def tiktok_status_fetch(access_token: str, publish_id: str):
    payload = {"publish_id": publish_id}
    r = requests.post(TIKTOK_STATUS_ENDPOINT, headers=tiktok_headers(access_token), json=payload, timeout=60)
    return r

# -----------------------------
# Health + debug
# -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "app": APP_NAME,
        "bucket": S3_BUCKET,
        "region": AWS_REGION,
        "status": "ok",
        "tiktok_allow_public": TIKTOK_ALLOW_PUBLIC,
        "tiktok_init": f"POST {TIKTOK_INIT_ENDPOINT}",
        "tiktok_status": f"POST {TIKTOK_STATUS_ENDPOINT}",
        "time_utc": now_utc()
    })

@app.route("/debug/routes", methods=["GET"])
def debug_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            "path": str(rule),
            "methods": sorted([m for m in rule.methods if m in ("GET", "POST", "OPTIONS")])
        })
    return jsonify({"routes": routes})

# -----------------------------
# TikTok status
# -----------------------------
@app.route("/api/tiktok/status", methods=["POST"])
def api_tiktok_status():
    tid = trace_id()
    try:
        body = json_body()
        access_token = body.get("access_token")
        publish_id = body.get("publish_id")

        if not access_token or not publish_id:
            return jsonify({
                "status": "error",
                "message": "Missing 'access_token' or 'publish_id'.",
                "trace_id": tid
            }), 400

        r = tiktok_status_fetch(access_token, publish_id)
        return jsonify({
            "status": "ok" if r.ok else "error",
            "tiktok_http_status": r.status_code,
            "tiktok_response": r.json() if r.headers.get("Content-Type", "").startswith("application/json") else r.text,
            "trace_id": tid
        }), (200 if r.ok else 400)

    except Exception as e:
        log.exception("tiktok/status failed")
        return jsonify({"status": "error", "message": str(e), "trace_id": tid}), 500

# -----------------------------
# TikTok publish (Option B)
# -----------------------------
@app.route("/api/tiktok/publish_from_s3_upload", methods=["POST"])
def api_tiktok_publish_from_s3_upload():
    """
    ✅ FILE_UPLOAD
    - Uploads from S3 -> TikTok upload_url
    - Enforces public posting rules:
        - If app unaudited, PUBLIC requests are blocked with a clear message.
        - SELF_ONLY requests work for testing/review proof.
    """
    tid = trace_id()
    try:
        body = json_body()
        access_token = body.get("access_token")
        s3_key = body.get("s3_key")
        title = body.get("title", "Cre8 Studio Upload")

        requested_privacy = normalize_privacy_level(body.get("privacy_level", "SELF_ONLY"))

        if not access_token or not s3_key:
            return jsonify({"status": "error", "message": "Missing 'access_token' or 's3_key'.", "trace_id": tid}), 400

        # If unaudited, block public request early (prevents wasting calls)
        if requested_privacy != "SELF_ONLY" and not TIKTOK_ALLOW_PUBLIC:
            return jsonify({
                "status": "error",
                "message": (
                    "PUBLIC posting is currently blocked because TikTok classifies this client as unaudited. "
                    "Use privacy_level=SELF_ONLY for now. After TikTok audit approval, set env TIKTOK_ALLOW_PUBLIC=1."
                ),
                "requested_privacy_level": requested_privacy,
                "allowed_privacy_level_now": "SELF_ONLY",
                "trace_id": tid
            }), 403

        # Head S3
        head = s3_head_object(S3_BUCKET, s3_key)
        size = int(head.get("ContentLength") or 0)
        content_type = head.get("ContentType") or "video/mp4"

        if size <= 0:
            return jsonify({"status": "error", "message": "S3 object size is 0.", "trace_id": tid}), 400

        if (size / (1024 * 1024)) > S3_DOWNLOAD_MAX_MB:
            return jsonify({
                "status": "error",
                "message": f"File too large for this server upload path. SizeMB={(size/(1024*1024)):.2f}.",
                "trace_id": tid
            }), 400

        # TikTok init
        init_resp, init_payload = tiktok_init_file_upload(
            access_token=access_token,
            video_size=size,
            title=title,
            privacy_level=requested_privacy
        )

        init_json = init_resp.json() if init_resp.headers.get("Content-Type", "").startswith("application/json") else None

        if not init_resp.ok:
            # If TikTok tells us unaudited again, give a clean message
            try:
                err = (init_json or {}).get("error") or {}
                if err.get("code") == "unaudited_client_can_only_post_to_private_accounts":
                    return jsonify({
                        "status": "error",
                        "message": (
                            "TikTok blocked PUBLIC posting because this app/client is unaudited. "
                            "For now, you can only post using privacy_level=SELF_ONLY (private). "
                            "To unlock public, you must pass TikTok Content Posting audit/review."
                        ),
                        "tiktok_http_status": init_resp.status_code,
                        "tiktok_response": init_json or init_resp.text,
                        "init_payload_sent": init_payload,
                        "trace_id": tid
                    }), 403
            except Exception:
                pass

            return jsonify({
                "status": "error",
                "message": "TikTok init failed.",
                "tiktok_http_status": init_resp.status_code,
                "tiktok_response": init_json or init_resp.text,
                "init_payload_sent": init_payload,
                "trace_id": tid
            }), 400

        data, _err = _extract_tiktok_data(init_json or {})
        upload_url = data.get("upload_url") or (data.get("upload_url_list", [None])[0] if isinstance(data.get("upload_url_list"), list) else None)
        publish_id = data.get("publish_id") or data.get("publishId") or data.get("publish_id_str")

        if not upload_url or not publish_id:
            return jsonify({
                "status": "error",
                "message": "TikTok init succeeded but upload_url/publish_id missing in response.",
                "tiktok_response": init_json,
                "init_payload_sent": init_payload,
                "trace_id": tid
            }), 500

        # Stream download from S3 and upload in a single PUT (works for your current file)
        obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
        body_stream = obj["Body"]

        chunk = body_stream.read(size)
        if not chunk or len(chunk) != size:
            return jsonify({
                "status": "error",
                "message": "Failed to read full file from S3 stream.",
                "bytes_read": len(chunk) if chunk else 0,
                "size": size,
                "trace_id": tid
            }), 500

        headers = {
            "Content-Type": content_type,
            "Content-Range": f"bytes 0-{size-1}/{size}",
            "Content-Length": str(size)
        }

        put = requests.put(upload_url, headers=headers, data=chunk, timeout=600)
        if not put.ok:
            return jsonify({
                "status": "error",
                "message": "TikTok upload failed.",
                "tiktok_http_status": put.status_code,
                "tiktok_response": put.text,
                "publish_id": publish_id,
                "trace_id": tid
            }), 400

        return jsonify({
            "status": "ok",
            "message": "Upload completed. Now poll /api/tiktok/status using publish_id.",
            "publish_id": publish_id,
            "privacy_level_used": requested_privacy,
            "bytes_sent": size,
            "size": size,
            "trace_id": tid
        }), 200

    except Exception as e:
        log.exception("tiktok/publish_from_s3_upload failed")
        return jsonify({"status": "error", "message": str(e), "trace_id": tid}), 500

# -----------------------------
# Static (optional)
# -----------------------------
@app.route("/static/<path:filename>", methods=["GET"])
def static_files(filename):
    return send_from_directory("static", filename)

# -----------------------------
# Local run (Koyeb uses Gunicorn)
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
