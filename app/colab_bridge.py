# app/colab_bridge.py
#
# Serves the private employee reference images to the Colab notebook.
# (The old processing-job and face-detection-event endpoints were removed
# together with their tables.)

import hmac
import json
import logging
import os
import urllib.error
import urllib.request

from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import engine

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/demo",
    tags=["Colab Demo"]
)


def verify_demo_token(token: str | None):
    expected = os.getenv("COLAB_DEMO_TOKEN")

    if not expected or not token:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    if not hmac.compare_digest(expected, token):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )


def _sign_reference_paths(paths: list[str]) -> dict[str, str]:
    """Ask Supabase Storage for short-lived signed URLs (private bucket).

    The service-role key stays on this server; Colab only receives
    URLs that expire after REFERENCE_URL_SECONDS.
    """
    base = settings.SUPABASE_URL.rstrip("/")
    bucket = settings.SUPABASE_REFERENCE_BUCKET
    key = settings.SUPABASE_SERVICE_ROLE_KEY

    request = urllib.request.Request(
        f"{base}/storage/v1/object/sign/{bucket}",
        data=json.dumps({
            "expiresIn": settings.REFERENCE_URL_SECONDS,
            "paths": paths
        }).encode("utf-8"),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        items = json.loads(response.read().decode("utf-8"))

    signed: dict[str, str] = {}

    for item in items:
        if item.get("error") or not item.get("signedURL"):
            logger.warning(
                "Could not sign reference image %s: %s",
                item.get("path"), item.get("error")
            )
            continue

        url = item["signedURL"]

        if not url.startswith("http"):
            if not url.startswith("/"):
                url = "/" + url
            url = f"{base}/storage/v1{url}"

        signed[item["path"]] = url

    return signed


@router.get("/reference-gallery")
def reference_gallery(
    x_demo_token: str | None = Header(default=None)
):
    """Active employees with signed URLs of their reference images."""
    verify_demo_token(x_demo_token)

    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=503,
            detail="Supabase Storage is not configured on the backend"
        )

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT e.employee_code,
                           e.full_name,
                           r.storage_path
                    FROM public.employees e
                    JOIN public.employee_reference_images r
                      ON r.employee_id = e.id
                    WHERE e.status = 'active'
                    ORDER BY e.employee_code, r.created_at
                """)
            ).mappings().all()
    except SQLAlchemyError:
        logger.exception("Failed to load reference image records")
        raise HTTPException(
            status_code=503,
            detail="Unable to load reference images"
        )

    if not rows:
        return {"employees": []}

    try:
        signed = _sign_reference_paths(
            [row["storage_path"] for row in rows]
        )
    except (urllib.error.URLError, ValueError, KeyError):
        logger.exception("Supabase Storage signing request failed")
        raise HTTPException(
            status_code=502,
            detail="Unable to create reference image URLs"
        )

    employees: dict[str, dict[str, Any]] = {}

    for row in rows:
        url = signed.get(row["storage_path"])

        if url is None:
            continue

        entry = employees.setdefault(
            row["employee_code"],
            {
                "employee_code": row["employee_code"],
                "full_name": row["full_name"],
                "image_urls": []
            }
        )
        entry["image_urls"].append(url)

    return {"employees": list(employees.values())}