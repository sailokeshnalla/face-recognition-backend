import logging
import os

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

from app.config import settings
from app.database import check_database_connection
from app.colab_bridge import router as colab_router
from app.attendance import router as attendance_router

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("startup")

REQUIRED_ENV = (
    "COLAB_DEMO_TOKEN",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_FROM_EMAIL",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warn at startup about settings that would fail later at run time."""
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]

    if missing:
        logger.warning(
            "Missing environment variables: %s", ", ".join(missing)
        )

    token = os.getenv("COLAB_DEMO_TOKEN", "")

    if token and len(token) < 24:
        logger.warning(
            "COLAB_DEMO_TOKEN is short; use at least 24 random characters"
        )

    if not (settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY):
        logger.warning(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set: "
            "the reference-gallery endpoint will return 503"
        )

    try:
        check_database_connection()
        logger.info("Database connection OK")
    except Exception:
        logger.exception("Database connection failed at startup")

    yield


# The API docs are switched off unless ENABLE_DOCS=1, because the backend is
# reachable from the internet through the tunnel.
docs_enabled = os.getenv("ENABLE_DOCS", "0").strip().lower() in (
    "1", "true", "yes"
)

app = FastAPI(
    title="Face Recognition Backend",
    version="0.3.0",
    lifespan=lifespan,
    docs_url="/docs" if docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if docs_enabled else None,
)

app.include_router(colab_router)
app.include_router(attendance_router)


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/health/database")
def database_health():
    try:
        check_database_connection()
        return {
            "status": "connected",
            "database": "postgresql"
        }
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database connection unavailable"
        )