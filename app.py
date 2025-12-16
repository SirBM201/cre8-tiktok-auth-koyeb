import os
import math
import time
import requests
import boto3
from flask import Flask, request, jsonify
from google.cloud import firestore

app = Flask(__name__)

# ---------- ENV ----------
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET")  # REQUIRED
CRON_SECRET = os.getenv("CRON_SECRET")  # REQUIRED for cron endpoints

TIKTOK_OPEN_API_BASE = "https://open.tiktokapis.com"

# ---------- AWS ----------
s3 = boto3.client("s3", region_name=AWS_REGION)

# ---------- FIRESTORE ----------
db = firestore.Client()
TIKTOK_COLLECTION = os.getenv("TIKTOK_COLLECTION", "tiktok_accounts")  # your collection

# ---------- HELPERS ----------
def require_cron_secret(req):
    if not CRON_SECRET:
        return False, ("CRON_SECRET env var not set", 500)
    got = req.headers.get("X-CRON-SECRET", "")
    if got != CRON_SECRET:
        return False, ("Invalid cron secret", 401)
    return True, None

def get_tiktok_access_token(open_id: str):
    doc = db.collection(TIKTOK_COLLECTION).document(open_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    return data.get("access_token")

def s3_head_size(bucket: str, key: str) -> int:
    head = s3.head_object(Bucket=bucket, Key=key)
    return int(head["ContentLength"])

def s3_get_range(bucket: str, key: str, start: int, end: int) -> bytes:
    # Range header is inclusive
    resp = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end}")
    return resp["Body"].read()

# ---------- ROUTES ----------
@app.get("/health")
def health():
    return "ok", 200

@app.post("/api/tiktok/refresh_all")
def refresh_all():
    ok, err = require_cron_secret(request)
    if not ok:
        msg, code = err
        return jsonify({"status": "error", "error": msg}), code

    # (Keep your existing refresh logic here)
    return jsonify({"status": "ok", "note": "refresh_all stub - keep your current logic"}), 200

@app.post("/api/tiktok/publish_init")
def publish_init():
    """
    Direct Post (FILE_UPLOAD):
    Body:
    {
      "open_id": "...",
      "s3_key": "posted/reels n shorts/9am content/2025-12-14 Faceless Coronation.mp4",
      "title": "Cre8 Studio test post",
      "privacy_level": "SELF_ONLY"
    }
    """
    if not S3_BUCKET:
        return jsonify({"status": "error", "error": "Missing S3_BUCKET env var"}), 500

    payload = request.get_json(force=True, silent=True) or {}
    open_id = payload.get("open_id")
    s3_key = payload.get("s3_key")
    title = payload.get("title", "")
    privacy_level = payload.get("privacy_level", "SELF_ONLY")

    if not open_id or not s3_key:
        return jsonify({"status": "error", "error": "open_id and s3_key are required"}), 400

    access_token = get_tiktok_access_token(open_id)
    if not access_token:
        return jsonify({"status": "error", "error": f"No access_token found for open_id={open_id}"}), 404

    # 1) Determine file size from S3
    try:
        video_size = s3_head_size(S3_BUCKET, s3_key)
    except Exception as e:
        return jsonify({"status": "error", "error": "S3 head_object failed", "details": str(e)}), 500

    # 2) Init TikTok Direct Post with FILE_UPLOAD
    # TikTok doc: /v2/post/publish/video/init/ with source_info.source=FILE_UPLOAD
    # :contentReference[oaicite:2]{index=2}
    chunk_size = 10_000_000  # 10MB recommended
    total_chunks = int(math.ceil(video_size / chunk_size))

    init_body = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks
        }
    }

    try:
        r = requests.post(
            f"{TIKTOK_OPEN_API_BASE}/v2/post/publish/video/init/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8"
            },
            json=init_body,
            timeout=60
        )
        data = r.json()
    except Exception as e:
        return jsonify({"status": "error", "error": "TikTok init request failed", "details": str(e)}), 502

    if r.status_code != 200 or (data.get("error") or {}).get("code") not in (None, "ok"):
        return jsonify({"status": "error", "http": r.status_code, "tiktok": data}), 400

    publish_id = (data.get("data") or {}).get("publish_id")
    upload_url = (data.get("data") or {}).get("upload_url")
    if not publish_id or not upload_url:
        return jsonify({"status": "error", "error": "Missing publish_id/upload_url from TikTok", "tiktok": data}), 400

    # 3) Upload chunks to TikTok upload_url using PUT with Content-Range
    # TikTok doc: upload_url + PUT chunking with Content-Range/Content-Length
    # :contentReference[oaicite:3]{index=3}
    try:
        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size - 1, video_size - 1)
            chunk = s3_get_range(S3_BUCKET, s3_key, start, end)

            put_headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{video_size}",
            }

            put = requests.put(upload_url, headers=put_headers, data=chunk, timeout=120)
            if put.status_code not in (200, 201, 204):
                return jsonify({
                    "status": "error",
                    "error": "TikTok upload failed",
                    "chunk_index": i,
                    "http": put.status_code,
                    "response_text": put.text[:500]
                }), 400

        return jsonify({
            "status": "ok",
            "open_id": open_id,
            "s3_bucket": S3_BUCKET,
            "s3_key": s3_key,
            "publish_id": publish_id,
            "uploaded_chunks": total_chunks
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "error": "Upload loop failed", "details": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
