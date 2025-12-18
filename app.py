import os
import json
import logging
import traceback
from typing import Any, Dict, Optional

import boto3
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from botocore.exceptions import ClientError

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
APP_NAME = "cre8-tiktok-s3-publisher"

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", os.getenv("AWS_S3_BUCKET", "fair-video-source"))

# TikTok endpoints (Content Posting API)
TIKTOK_INIT_ENDPOINT = os.getenv(
    "TIKTOK_INIT_ENDPOINT",
    "https://open.tiktokapis.com/v2/post/publish/video/init/",
)
TIKTOK_STATUS_ENDPOINT = os.getenv(
    "TIKTOK_STATUS_ENDPOINT",
    "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
)

# Debug (set to "true" on Koyeb if you want stack traces in JSON responses)
DEBUG_JSON_ERRORS = os.getenv("DEBUG_JSON_ERRORS", "false").lower() == "true"

# -----------------------------------------------------------------------------
# App + Logging
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(APP_NAME)

s3 = boto3.client("s3", region_name=AWS_REGION)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def json_error(message: str, status_code: int = 400, extra: Optional[Dict[str, Any]] = None):
    payload = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    return jsonify(payload), status_code


def require_json():
    if not request.is_json:
        return False, json_error("Request must be JSON (Content-Type: application/json).", 415)
    return True, None


def safe_exc() -> Dict[str, Any]:
    if not DEBUG_JSON_ERRORS:
        return {}
    return {"trace": traceback.format_exc()}


def s3_head_object(key: str) -> Dict[str, Any]:
    try:
        resp = s3.head_object(Bucket=S3_BUCKET, Key=key)
        content_type = resp.get("ContentType", "")
        size = resp.get("ContentLength", 0)
        etag = resp.get("ETag", "")
        return {"status": "ok", "content_type": content_type, "size": size, "etag": etag}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        return {"status": "error", "aws_error_code": code, "message": str(e)}


def s3_presign_url(key: str, expires_seconds: int = 7200) -> str:
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires_seconds,
    )


def tiktok_post(url: str, access_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    return {"http_status": r.status_code, "data": data}


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/health")
def health():
    # lightweight health check + configuration visibility
    return jsonify(
        {
            "app": APP_NAME,
            "status": "ok",
            "bucket": S3_BUCKET,
            "region": AWS_REGION,
            "has_aws_keys": bool(os.getenv("AWS_ACCESS_KEY_ID")) and bool(os.getenv("AWS_SECRET_ACCESS_KEY")),
            "tiktok_init": f"POST {TIKTOK_INIT_ENDPOINT}",
            "tiktok_status": f"POST {TIKTOK_STATUS_ENDPOINT}",
        }
    )


@app.get("/debug/routes")
def debug_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        methods = sorted([m for m in rule.methods if m not in ("HEAD", "OPTIONS")])
        routes.append({"path": str(rule), "methods": methods})
    return jsonify({"routes": routes})


@app.post("/api/s3/check")
def api_s3_check():
    ok, err = require_json()
    if not ok:
        return err

    body = request.get_json(silent=True) or {}
    key = (body.get("s3_key") or "").strip()
    if not key:
        return json_error("Missing required field: s3_key")

    meta = s3_head_object(key)
    if meta["status"] != "ok":
        return json_error("S3 object not found or not accessible.", 404, extra=meta)

    return jsonify(
        {
            "ok": True,
            "bucket": S3_BUCKET,
            "s3_key": key,
            "status": "ok",
            "content_type": meta.get("content_type"),
            "size": meta.get("size"),
            "etag": meta.get("etag"),
        }
    )


@app.post("/api/s3/presign")
def api_s3_presign():
    ok, err = require_json()
    if not ok:
        return err

    body = request.get_json(silent=True) or {}
    key = (body.get("s3_key") or "").strip()
    expires = int(body.get("expires_seconds") or 7200)

    if not key:
        return json_error("Missing required field: s3_key")

    meta = s3_head_object(key)
    if meta["status"] != "ok":
        return json_error("S3 object not found or not accessible.", 404, extra=meta)

    url = s3_presign_url(key, expires_seconds=expires)
    return jsonify(
        {
            "ok": True,
            "bucket": S3_BUCKET,
            "s3_key": key,
            "expires_seconds": expires,
            "content_type": meta.get("content_type"),
            "size": meta.get("size"),
            "url": url,
        }
    )


@app.post("/api/tiktok/publish_from_s3_pull")
def api_tiktok_publish_from_s3_pull():
    """
    Flow:
      1) Validate S3 key exists
      2) Presign URL (TikTok will pull from it)
      3) Call TikTok /video/init with PULL_FROM_URL
      4) Return TikTok response (publish_id is usually inside)
    """
    ok, err = require_json()
    if not ok:
        return err

    try:
        body = request.get_json(silent=True) or {}

        open_id = (body.get("open_id") or "").strip()
        access_token = (body.get("access_token") or "").strip()
        s3_key = (body.get("s3_key") or "").strip()

        # Optional fields
        title = (body.get("title") or "Cre8 Studio Upload").strip()
        privacy_level = (body.get("privacy_level") or "SELF_ONLY").strip()
        disable_duet = bool(body.get("disable_duet", False))
        disable_comment = bool(body.get("disable_comment", False))
        disable_stitch = bool(body.get("disable_stitch", False))
        expires_seconds = int(body.get("expires_seconds") or 7200)

        if not open_id:
            return json_error("Missing required field: open_id")
        if not access_token:
            return json_error("Missing required field: access_token")
        if not s3_key:
            return json_error("Missing required field: s3_key")

        # 1) confirm S3 exists
        meta = s3_head_object(s3_key)
        if meta["status"] != "ok":
            return json_error("S3 object not found or not accessible.", 404, extra=meta)

        # 2) presign
        video_url = s3_presign_url(s3_key, expires_seconds=expires_seconds)

        # 3) TikTok init payload (PULL_FROM_URL)
        # NOTE: TikTok may return errors if title/metadata violates limits.
        payload = {
            "open_id": open_id,
            "post_info": {
                "title": title,
                "privacy_level": privacy_level,
                "disable_duet": disable_duet,
                "disable_comment": disable_comment,
                "disable_stitch": disable_stitch,
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": video_url,
            },
        }

        tk = tiktok_post(TIKTOK_INIT_ENDPOINT, access_token, payload)

        # If TikTok rejected the request, return that clearly
        if tk["http_status"] >= 400:
            return json_error(
                "TikTok init failed.",
                502,
                extra={
                    "tiktok_http_status": tk["http_status"],
                    "tiktok_response": tk["data"],
                },
            )

        return jsonify(
            {
                "ok": True,
                "message": "TikTok init successful (video pull started).",
                "bucket": S3_BUCKET,
                "s3_key": s3_key,
                "size": meta.get("size"),
                "content_type": meta.get("content_type"),
                "expires_seconds": expires_seconds,
                "tiktok_http_status": tk["http_status"],
                "tiktok_response": tk["data"],
            }
        )

    except Exception as e:
        log.exception("publish_from_s3_pull crashed")
        return json_error("Server error inside publish_from_s3_pull: " + str(e), 500, extra=safe_exc())


@app.post("/api/tiktok/status")
def api_tiktok_status():
    """
    Use this after publish_from_s3_pull to check processing state.
    You must provide:
      - access_token
      - publish_id
    """
    ok, err = require_json()
    if not ok:
        return err

    try:
        body = request.get_json(silent=True) or {}
        access_token = (body.get("access_token") or "").strip()
        publish_id = (body.get("publish_id") or "").strip()

        if not access_token:
            return json_error("Missing required field: access_token")
        if not publish_id:
            return json_error("Missing required field: publish_id")

        payload = {"publish_id": publish_id}
        tk = tiktok_post(TIKTOK_STATUS_ENDPOINT, access_token, payload)

        if tk["http_status"] >= 400:
            return json_error(
                "TikTok status fetch failed.",
                502,
                extra={
                    "tiktok_http_status": tk["http_status"],
                    "tiktok_response": tk["data"],
                },
            )

        return jsonify(
            {
                "ok": True,
                "tiktok_http_status": tk["http_status"],
                "tiktok_response": tk["data"],
            }
        )

    except Exception as e:
        log.exception("status crashed")
        return json_error("Server error inside status: " + str(e), 500, extra=safe_exc())


# -----------------------------------------------------------------------------
# Entry
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
