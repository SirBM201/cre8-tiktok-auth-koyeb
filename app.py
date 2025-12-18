import os
import time
import json
import requests
import boto3
from flask import Flask, request, jsonify

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
    # URL TikTok can fetch directly
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=int(expires_seconds),
    )

def head_s3_object(bucket: str, key: str):
    return s3.head_object(Bucket=bucket, Key=key)

# -----------------------------
# TikTok API (configurable)
# -----------------------------
TIKTOK_BASE = os.getenv("TIKTOK_BASE", "https://open.tiktokapis.com")

# Set these to match your project’s working endpoints:
# If your current code uses another init endpoint, put it here.
TIKTOK_VIDEO_INIT_ENDPOINT = os.getenv(
    "TIKTOK_VIDEO_INIT_ENDPOINT",
    f"{TIKTOK_BASE}/v2/post/publish/video/init/"
)

# Status endpoint can differ too; set it via env if needed.
TIKTOK_STATUS_ENDPOINT = os.getenv(
    "TIKTOK_STATUS_ENDPOINT",
    f"{TIKTOK_BASE}/v2/post/publish/status/fetch/"
)

def tiktok_headers(access_token: str):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=utf-8",
    }

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

    # confirm exists
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
# TikTok publish using PULL_FROM_URL
# -----------------------------
@app.post("/api/tiktok/publish_from_s3_pull")
def publish_from_s3_pull():
    """
    Body JSON:
    {
      "open_id": "xxx",
      "access_token": "xxx",
      "s3_key": "posted/.../file.mp4",
      "title": "Cre8 Studio test post",
      "privacy_level": "SELF_ONLY"
    }
    """
    data = request.get_json(force=True, silent=False)

    open_id = data.get("open_id")
    access_token = data.get("access_token")
    s3_key = data.get("s3_key")
    title = data.get("title", "Cre8 Studio post")
    privacy_level = data.get("privacy_level", "SELF_ONLY")

    if not open_id:
        return jsonify({"status": "error", "message": "Missing open_id"}), 400
    if not access_token:
        return jsonify({"status": "error", "message": "Missing access_token"}), 400
    if not s3_key:
        return jsonify({"status": "error", "message": "Missing s3_key"}), 400

    # 1) confirm S3 object exists + get size
    head = head_s3_object(S3_BUCKET, s3_key)
    video_size = int(head.get("ContentLength", 0))
    content_type = head.get("ContentType", "")

    if video_size <= 0:
        return jsonify({"status": "error", "message": "S3 object has invalid size"}), 400

    # 2) generate pre-signed GET URL long enough for TikTok to fetch
    # Use 2 hours by default (safe for slower pulls)
    presigned_url = presign_s3_get_url(S3_BUCKET, s3_key, expires_seconds=7200)

    # 3) TikTok init payload for pull-from-url
    # NOTE: If your TikTok init endpoint expects different keys,
    # adjust here (but keep source = PULL_FROM_URL + video_url).
    payload = {
        "open_id": open_id,
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
            # optional fields if you need later:
            # "disable_comment": False,
            # "disable_duet": False,
            # "disable_stitch": False,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": presigned_url,
        }
    }

    # 4) Call TikTok init
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

    # Common fields returned by TikTok depending on API variant:
    # Some return data.publish_id, some return data.video_id.
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
    """
    Query:
      /api/tiktok/status?access_token=xxx&publish_id=yyy
    """
    access_token = request.args.get("access_token")
    publish_id = request.args.get("publish_id")
    video_id = request.args.get("video_id")

    if not access_token:
        return jsonify({"status": "error", "message": "Missing access_token"}), 400
    if not publish_id and not video_id:
        return jsonify({"status": "error", "message": "Missing publish_id or video_id"}), 400

    # Different TikTok variants use different key names. We send both if provided.
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
