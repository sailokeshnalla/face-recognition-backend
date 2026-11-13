from fastapi import FastAPI, HTTPException

from app.database import check_database_connection
from app.colab_bridge import router as colab_router
from app.attendance import router as attendance_router

app = FastAPI(
    title="Face Recognition Backend",
    version="0.2.0"
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