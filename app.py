import os
import math
import time
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

# TikTok docs show FILE_UPLOAD can be done as SINGLE chunk for <= 64MB:
# chunk_size = video_size, total_chunk_count = 1
TIKTOK_SINGLE_CHUNK_MAX_BYTES = int(os.getenv("TIKTOK_SINGLE_CHUNK_MAX_BYTES", str(64 * 1024 * 1024)))

# Fallback chunk size ONLY if file > 64MB
TIKTOK_FALLBACK_CHUNK_SIZE = int(os.getenv("TIKTOK_FALLBACK_CHUNK_SIZE", str(5 * 1024 * 1024)))  # 5MB

S3_DOWNLOAD_MAX_MB = int(os.getenv("S3_DOWNLOAD_MAX_MB", "300"))  # safety

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
    """
    Safe JSON parsing with clear errors.
    """
    try:
        data = request.get_json(force=True, silent=False)
        if data is None:
            raise ValueError("Empty JSON body.")
        return data
    except Exception as e:
        raise ValueError(f"Invalid JSON body: {e}")

def s3_head_object(bucket: str, key: str):
    return s3.head_object(Bucket=bucket, Key=key)

def s3_presigned_get(bucket: str, key: str, expires: int):
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires
    )

def tiktok_headers(access_token: str):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

def _chunk_plan(video_size: int):
    """
    TikTok allows chunking, but for <= 64MB TikTok explicitly suggests:
      chunk_size = video_size
      total_chunk_count = 1
    This avoids 'invalid chunk size' and 'invalid total chunk count' errors.
    """
    if video_size <= TIKTOK_SINGLE_CHUNK_MAX_BYTES:
        chunk_size = video_size
        total_chunks = 1
        mode = "single"
    else:
        chunk_size = TIKTOK_FALLBACK_CHUNK_SIZE
        total_chunks = math.ceil(video_size / chunk_size)
        mode = "chunked"
    return chunk_size, int(total_chunks), mode

def tiktok_init_pull_from_url(access_token: str, video_url: str, title: str, privacy_level: str):
    payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url
        }
    }
    r = requests.post(TIKTOK_INIT_ENDPOINT, headers=tiktok_headers(access_token), json=payload, timeout=60)
    return r

def tiktok_init_file_upload(access_token: str, video_size: int, title: str, privacy_level: str):
    """
    ✅ OPTION B: FILE_UPLOAD init
    Uses TikTok-recommended single-chunk settings for <=64MB:
      chunk_size = video_size
      total_chunk_count = 1
    """
    chunk_size, total_chunks, mode = _chunk_plan(video_size)

    payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": int(video_size),
            "chunk_size": int(chunk_size),
            "total_chunk_count": int(total_chunks)
        }
    }

    log.info(f"TikTok init FILE_UPLOAD plan: mode={mode} video_size={video_size} chunk_size={chunk_size} total_chunks={total_chunks}")

    r = requests.post(TIKTOK_INIT_ENDPOINT, headers=tiktok_headers(access_token), json=payload, timeout=60)
    return r, payload  # return payload too (for debugging)

def tiktok_status_fetch(access_token: str, publish_id: str):
    payload = {"publish_id": publish_id}
    r = requests.post(TIKTOK_STATUS_ENDPOINT, headers=tiktok_headers(access_token), json=payload, timeout=60)
    return r

def _extract_tiktok_data(resp_json: dict):
    """
    Tries to handle common TikTok response shapes.
    """
    data = resp_json.get("data") or {}
    err = resp_json.get("error") or {}
    return data, err

# -----------------------------
# Health + debug
# -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "app": APP_NAME,
        "bucket": S3_BUCKET,
        "has_aws_keys": bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")),
        "region": AWS_REGION,
        "status": "ok",
        "tiktok_init": f"POST {TIKTOK_INIT_ENDPOINT}",
        "tiktok_status": f"POST {TIKTOK_STATUS_ENDPOINT}",
        "single_chunk_max_bytes": TIKTOK_SINGLE_CHUNK_MAX_BYTES,
        "fallback_chunk_size": TIKTOK_FALLBACK_CHUNK_SIZE,
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
# S3 APIs
# -----------------------------
@app.route("/api/s3/check", methods=["POST"])
def api_s3_check():
    tid = trace_id()
    try:
        body = json_body()
        key = body.get("s3_key")
        if not key:
            return jsonify({"status": "error", "message": "Missing 's3_key'.", "trace_id": tid}), 400

        head = s3_head_object(S3_BUCKET, key)
        content_type = head.get("ContentType") or "application/octet-stream"
        size = int(head.get("ContentLength") or 0)
        etag = head.get("ETag")

        return jsonify({
            "bucket": S3_BUCKET,
            "content_type": content_type,
            "etag": etag,
            "s3_key": key,
            "size": size,
            "status": "ok",
            "trace_id": tid
        })
    except Exception as e:
        log.exception("s3/check failed")
        return jsonify({"status": "error", "message": str(e), "trace_id": tid}), 500

@app.route("/api/s3/presign", methods=["POST"])
def api_s3_presign():
    tid = trace_id()
    try:
        body = json_body()
        key = body.get("s3_key")
        expires = int(body.get("expires_seconds") or DEFAULT_EXPIRES_SECONDS)

        if not key:
            return jsonify({"status": "error", "message": "Missing 's3_key'.", "trace_id": tid}), 400

        head = s3_head_object(S3_BUCKET, key)
        size = int(head.get("ContentLength") or 0)
        content_type = head.get("ContentType") or "application/octet-stream"

        url = s3_presigned_get(S3_BUCKET, key, expires)

        return jsonify({
            "bucket": S3_BUCKET,
            "content_type": content_type,
            "expires_seconds": expires,
            "s3_key": key,
            "size": size,
            "status": "ok",
            "url": url,
            "trace_id": tid
        })
    except Exception as e:
        log.exception("s3/presign failed")
        return jsonify({"status": "error", "message": str(e), "trace_id": tid}), 500

# -----------------------------
# TikTok APIs
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


@app.route("/api/tiktok/publish_from_s3_pull", methods=["POST"])
def api_tiktok_publish_from_s3_pull():
    """
    PULL_FROM_URL (will fail with url_ownership_unverified until TikTok verifies your domain).
    """
    tid = trace_id()
    try:
        body = json_body()
        access_token = body.get("access_token")
        s3_key = body.get("s3_key")
        title = body.get("title", "Cre8 Studio Upload")
        privacy_level = body.get("privacy_level", "SELF_ONLY")
        expires_seconds = int(body.get("expires_seconds") or DEFAULT_EXPIRES_SECONDS)

        if not access_token or not s3_key:
            return jsonify({"status": "error", "message": "Missing 'access_token' or 's3_key'.", "trace_id": tid}), 400

        presigned = s3_presigned_get(S3_BUCKET, s3_key, expires_seconds)

        r = tiktok_init_pull_from_url(access_token, presigned, title, privacy_level)

        out = {
            "status": "ok" if r.ok else "error",
            "message": "TikTok init ok." if r.ok else "TikTok init failed.",
            "tiktok_http_status": r.status_code,
            "tiktok_response": r.json() if r.headers.get("Content-Type", "").startswith("application/json") else r.text,
            "video_url_used": presigned,
            "trace_id": tid
        }

        if not r.ok:
            try:
                j = r.json()
                err = (j.get("error") or {})
                if err.get("code") == "url_ownership_unverified":
                    out["hint"] = (
                        "TikTok rejected pull_from_url because URL ownership is not verified. "
                        "Use /api/tiktok/publish_from_s3_upload (Option B) or complete TikTok URL ownership verification."
                    )
            except Exception:
                pass

        return jsonify(out), (200 if r.ok else 400)

    except Exception as e:
        log.exception("tiktok/publish_from_s3_pull failed")
        return jsonify({"status": "error", "message": str(e), "trace_id": tid}), 500


@app.route("/api/tiktok/publish_from_s3_upload", methods=["POST"])
def api_tiktok_publish_from_s3_upload():
    """
    ✅ OPTION B: FILE_UPLOAD
    - Downloads video from S3 (server side)
    - Calls TikTok init with FILE_UPLOAD (single chunk for <=64MB)
    - Uploads file to TikTok upload_url (single PUT for <=64MB)
    """
    tid = trace_id()
    try:
        body = json_body()
        access_token = body.get("access_token")
        s3_key = body.get("s3_key")
        title = body.get("title", "Cre8 Studio Upload")
        privacy_level = body.get("privacy_level", "SELF_ONLY")

        if not access_token or not s3_key:
            return jsonify({"status": "error", "message": "Missing 'access_token' or 's3_key'.", "trace_id": tid}), 400

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

        # 1) TikTok init FILE_UPLOAD
        init_resp, init_payload = tiktok_init_file_upload(access_token, size, title, privacy_level)
        init_json = init_resp.json() if init_resp.headers.get("Content-Type", "").startswith("application/json") else None

        if not init_resp.ok:
            return jsonify({
                "status": "error",
                "message": "TikTok init failed.",
                "tiktok_http_status": init_resp.status_code,
                "tiktok_response": init_json or init_resp.text,
                "init_payload_sent": init_payload,
                "trace_id": tid
            }), 400

        data, err = _extract_tiktok_data(init_json or {})
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

        # 2) Stream-download from S3 and upload to TikTok
        obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
        body_stream = obj["Body"]

        chunk_size, total_chunks, mode = _chunk_plan(size)

        bytes_sent = 0
        chunk_index = 0

        while True:
            chunk = body_stream.read(chunk_size)
            if not chunk:
                break

            start = bytes_sent
            end = bytes_sent + len(chunk) - 1
            bytes_sent += len(chunk)
            chunk_index += 1

            headers = {
                "Content-Type": content_type,
                "Content-Range": f"bytes {start}-{end}/{size}"
            }

            put = requests.put(upload_url, headers=headers, data=chunk, timeout=300)
            if not put.ok:
                return jsonify({
                    "status": "error",
                    "message": "TikTok upload failed.",
                    "upload_mode": mode,
                    "chunk_index": chunk_index,
                    "total_chunks_expected": total_chunks,
                    "tiktok_http_status": put.status_code,
                    "tiktok_response": put.text,
                    "publish_id": publish_id,
                    "trace_id": tid
                }), 400

        if bytes_sent != size:
            return jsonify({
                "status": "error",
                "message": "Upload finished but bytes_sent != size (incomplete upload).",
                "bytes_sent": bytes_sent,
                "size": size,
                "upload_mode": mode,
                "trace_id": tid
            }), 400

        # 3) Return publish_id so you can poll status
        return jsonify({
            "status": "ok",
            "message": "Upload completed. Now poll /api/tiktok/status using publish_id.",
            "publish_id": publish_id,
            "upload_mode": mode,
            "bytes_sent": bytes_sent,
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
