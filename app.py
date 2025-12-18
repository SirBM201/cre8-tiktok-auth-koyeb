import os
import json
import boto3
import requests
from botocore.exceptions import ClientError
from flask import Flask, request, jsonify

app = Flask(__name__)

# -----------------------------
# AWS / S3
# -----------------------------
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "fair-video-source")

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
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
# TikTok endpoints (Option A)
# -----------------------------
TIKTOK_BASE = os.getenv("TIKTOK_BASE", "https://open.tiktokapis.com")

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
        "Content-Type": "application/json",
    }

# -----------------------------
# Health + debug
# -----------------------------
@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bucket": S3_BUCKET,
        "region": AWS_REGION,
        "has_aws_keys": bool(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY),
        "tiktok_init": TIKTOK_VIDEO_INIT_ENDPOINT,
        "tiktok_status": TIKTOK_STATUS_ENDPOINT,
    }), 200

@app.get("/debug/routes")
def debug_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            "path": str(rule),
            "methods": sorted([m for m in rule.methods if m not in ("HEAD", "OPTIONS")])
        })
    routes = sorted(routes, key=lambda x: x["path"])
    return jsonify({"routes": routes}), 200

# -----------------------------
# S3 check (NEW)
# -----------------------------
@app.post("/api/s3/check")
def api_s3_check():
    data = request.get_json(force=True, silent=False)
    s3_key = data.get("s3_key")

    if not s3_key:
        return jsonify({"status": "error", "message": "Missing s3_key"}), 400

    try:
        meta = head_s3_object(S3_BUCKET, s3_key)
        return jsonify({
            "status": "ok",
            "bucket": S3_BUCKET,
            "s3_key": s3_key,
            "size": meta.get("ContentLength"),
            "content_type": meta.get("ContentType"),
            "etag": meta.get("ETag"),
        }), 200
    except ClientError as e:
        return jsonify({
            "status": "error",
            "message": "S3 head_object failed",
            "s3_key": s3_key,
            "aws_error": str(e),
        }), 400

# -----------------------------
# S3 presign (existing)
# -----------------------------
@app.post("/api/s3/presign")
def api_s3_presign():
    data = request.get_json(force=True, silent=False)
    s3_key = data.get("s3_key")
    expires = int(data.get("expires_seconds", 7200))

    if not s3_key:
        return jsonify({"status": "error", "message": "Missing s3_key"}), 400

    try:
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

    except ClientError as e:
        return jsonify({
            "status": "error",
            "message": "S3 presign failed",
            "s3_key": s3_key,
            "aws_error": str(e),
        }), 400

# -----------------------------
# TikTok publish (Option A: PULL_FROM_URL)
# -----------------------------
@app.post("/api/tiktok/publish_from_s3_pull")
def publish_from_s3_pull():
    data = request.get_json(force=True, silent=False)

    open_id = data.get("open_id")
    access_token = data.get("access_token")
    s3_key = data.get("s3_key")
    title = data.get("title", "Cre8 Studio post")
    privacy_level = data.get("privacy_level", "SELF_ONLY")

    if not open_id or not access_token or not s3_key:
        return jsonify({
            "status": "error",
            "message": "Missing one of: open_id, access_token, s3_key"
        }), 400

    # 1) Validate S3 object
    try:
        head = head_s3_object(S3_BUCKET, s3_key)
    except ClientError as e:
        return jsonify({
            "status": "error",
            "step": "s3_head_object",
            "message": "Cannot find/read S3 object with provided s3_key",
            "s3_key": s3_key,
            "aws_error": str(e),
        }), 400

    video_size = int(head.get("ContentLength", 0))
    if video_size <= 0:
        return jsonify({"status": "error", "message": "S3 object has invalid size"}), 400

    # 2) Presigned URL for TikTok pull
    presigned_url = presign_s3_get_url(S3_BUCKET, s3_key, expires_seconds=7200)

    # 3) TikTok init
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
        json=payload,
        timeout=60
    )

    # Always return TikTok response body for visibility
    try:
        out = r.json()
    except Exception:
        out = {"raw": r.text}

    if r.status_code >= 400:
        return jsonify({
            "status": "error",
            "step": "tiktok_init",
            "http": r.status_code,
            "tiktok": out,
            "debug": {
                "init_endpoint": TIKTOK_VIDEO_INIT_ENDPOINT,
                "bucket": S3_BUCKET,
                "s3_key": s3_key,
                "video_size": video_size
            }
        }), 400

    data_node = out.get("data", {}) if isinstance(out, dict) else {}
    publish_id = data_node.get("publish_id") or data_node.get("publishId")
    video_id = data_node.get("video_id") or data_node.get("videoId")

    return jsonify({
        "status": "ok",
        "publish_id": publish_id,
        "video_id": video_id,
        "tiktok": out,
    }), 200

@app.post("/api/tiktok/status")
def tiktok_status():
    data = request.get_json(force=True, silent=False)
    access_token = data.get("access_token")
    publish_id = data.get("publish_id")
    video_id = data.get("video_id")

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
        json=payload,
        timeout=60
    )

    try:
        out = r.json()
    except Exception:
        out = {"raw": r.text}

    return jsonify({
        "status": "ok" if r.status_code < 400 else "error",
        "http": r.status_code,
        "endpoint": TIKTOK_STATUS_ENDPOINT,
        "tiktok": out
    }), (200 if r.status_code < 400 else 400)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
