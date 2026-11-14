# app/attendance.py

import os
import ssl
import time
import hmac
import uuid
import smtplib
import logging

from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine

load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/demo/attendance",
    tags=["Attendance Demo"]
)


class CheckInRequest(BaseModel):
    employee_code: str = Field(min_length=1, max_length=100)
    manually_confirmed: bool


class RecognizedCheckInRequest(BaseModel):
    employee_code: str = Field(min_length=1, max_length=100)
    match_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    confirmed_frames: int = Field(ge=1)
    video_time_seconds: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    source_name: str | None = Field(default=None, max_length=255)


def verify_token(token):
    expected = os.getenv("COLAB_DEMO_TOKEN")

    if not expected or not token:
        raise HTTPException(401, "Unauthorized")

    if not hmac.compare_digest(expected, token):
        raise HTTPException(401, "Unauthorized")


def send_attendance_email(
    employee,
    checked_in_at,
    verification_method="manual_demo"
):
    smtp_user = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    sender = os.environ["SMTP_FROM_EMAIL"]
    sender_name = os.getenv(
        "SMTP_FROM_NAME",
        "Attendance System"
    )

    if sender.lower() != smtp_user.lower():
        raise ValueError("SMTP sender configuration mismatch")

    message = EmailMessage()

    message["Subject"] = "Employee Check-in Confirmation"
    message["From"] = f"{sender_name} <{sender}>"
    message["To"] = employee["email"]

    local_time = checked_in_at.astimezone(ZoneInfo("Asia/Kolkata"))

    if verification_method == "face_recognition":
        method_line = (
            "Your attendance was recorded after the face recognition "
            "system identified you.\n\n"
        )
    else:
        method_line = (
            "This is a test notification from the "
            "Face Recognition Attendance System.\n\n"
        )

    message.set_content(
        f"Hello {employee['full_name']},\n\n"
        "Your attendance check-in has been recorded.\n\n"
        f"Employee Code: {employee['employee_code']}\n"
        f"Check-in Time: {local_time:%d-%m-%Y %I:%M:%S %p} IST\n\n"
        f"{method_line}"
        "If this was not you, please contact your administrator.\n\n"
        "Regards,\nAttendance Management System"
    )

    with smtplib.SMTP_SSL(
        smtp_host,
        smtp_port,
        context=ssl.create_default_context(),
        timeout=30
    ) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(message)


def dedupe_clause() -> tuple[str, dict]:
    """SQL fragment that finds a check-in that already counts for today.

    ATTENDANCE_DEDUPE=window        (default) a check-in within the last
                                    ATTENDANCE_WINDOW_HOURS hours (default 12)
    ATTENDANCE_DEDUPE=calendar_day  a check-in since midnight India time
    The fragment is built only from constants, never from request data.
    """
    mode = os.getenv("ATTENDANCE_DEDUPE", "window").strip().lower()

    if mode == "calendar_day":
        return (
            "verified_at >= (date_trunc('day', now() AT TIME ZONE "
            "'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata')",
            {}
        )

    try:
        hours = int(os.getenv("ATTENDANCE_WINDOW_HOURS", "12"))
    except ValueError:
        hours = 12

    if not 1 <= hours <= 72:
        hours = 12

    return "verified_at > now() - make_interval(hours => :hours)", {"hours": hours}


EMAIL_RETRY_DELAYS = (0, 2, 5)   # seconds before attempt 1, 2 and 3


def send_with_retry(employee, checked_in_at, verification_method):
    """Try the email up to three times. Returns (sent, error_name)."""
    last_error = None

    for attempt, delay in enumerate(EMAIL_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)

        try:
            send_attendance_email(employee, checked_in_at, verification_method)
            return True, None
        except Exception as exc:
            last_error = type(exc).__name__
            logger.warning(
                "Attendance email attempt %d/%d failed (%s)",
                attempt, len(EMAIL_RETRY_DELAYS), last_error
            )

    return False, last_error


def record_check_in(employee_code: str, verification_method: str) -> dict:
    """Shared core for manual and face-recognition check-ins.

    Looks up the active employee, blocks a second check-in within
    12 hours, writes the attendance and notification rows, then sends
    the email and records the delivery status.
    """
    employee_code = employee_code.strip()

    attendance_id = uuid.uuid4()
    notification_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    try:
        with engine.begin() as conn:

            # Serialize concurrent check-ins for this employee.
            conn.execute(
                text("""
                    SELECT pg_advisory_xact_lock(
                        hashtext(:employee_code)
                    )
                """),
                {"employee_code": employee_code}
            )

            employee = conn.execute(
                text("""
                    SELECT id, employee_code, full_name, email
                    FROM public.employees
                    WHERE employee_code = :code
                      AND status = 'active'
                    LIMIT 1
                """),
                {"code": employee_code}
            ).mappings().first()

            if employee is None:
                raise HTTPException(
                    404,
                    "Active employee not found"
                )

            if not employee["email"]:
                raise HTTPException(
                    400,
                    "Employee email is missing"
                )

            # Prevent duplicate attendance records and duplicate emails.
            dedupe_sql, dedupe_params = dedupe_clause()

            existing = conn.execute(
                text(f"""
                    SELECT id
                    FROM public.attendance_events
                    WHERE employee_id = :employee_id
                      AND event_type = 'check_in'
                      AND {dedupe_sql}
                    ORDER BY verified_at DESC
                    LIMIT 1
                """),
                {"employee_id": employee["id"], **dedupe_params}
            ).scalar_one_or_none()

            if existing:
                return {
                    "status": "already_checked_in",
                    "attendance_id": str(existing),
                    "email_sent": False,
                    "message": "Check-in already recorded for this period"
                }

            conn.execute(
                text("""
                    INSERT INTO public.attendance_events (
                        id,
                        employee_id,
                        event_type,
                        verification_method,
                        verified_at,
                        created_at
                    )
                    VALUES (
                        :id,
                        :employee_id,
                        'check_in',
                        :verification_method,
                        :verified_at,
                        :created_at
                    )
                """),
                {
                    "id": attendance_id,
                    "employee_id": employee["id"],
                    "verification_method": verification_method,
                    "verified_at": now,
                    "created_at": now
                }
            )

            conn.execute(
                text("""
                    INSERT INTO public.notifications (
                        id,
                        employee_id,
                        attendance_event_id,
                        notification_type,
                        recipient_email,
                        subject,
                        status,
                        created_at
                    )
                    VALUES (
                        :id,
                        :employee_id,
                        :attendance_id,
                        'check_in_confirmation',
                        :recipient_email,
                        'Employee Check-in Confirmation',
                        'pending',
                        :created_at
                    )
                """),
                {
                    "id": notification_id,
                    "employee_id": employee["id"],
                    "attendance_id": attendance_id,
                    "recipient_email": employee["email"],
                    "created_at": now
                }
            )

    except HTTPException:
        raise
    except SQLAlchemyError:
        logger.exception("Failed to record attendance")
        raise HTTPException(
            503,
            "Attendance database operation failed"
        )

    # The attendance record has been committed.
    # Email delivery is handled separately.

    email_sent, email_error = send_with_retry(
        employee, now, verification_method
    )

    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE public.notifications
                    SET status = :status,
                        sent_at = :sent_at,
                        error_message = :error_message
                    WHERE id = :id
                """),
                {
                    "id": notification_id,
                    "status": "sent" if email_sent else "failed",
                    "sent_at": (
                        datetime.now(timezone.utc)
                        if email_sent else None
                    ),
                    "error_message": (
                        None if email_sent
                        else f"SMTP failed after "
                             f"{len(EMAIL_RETRY_DELAYS)} attempts "
                             f"({email_error})"
                    )
                }
            )
    except SQLAlchemyError:
        logger.exception("Failed to update notification status")
        return {
            "status": "recorded_notification_status_unknown",
            "attendance_id": str(attendance_id),
            "notification_id": str(notification_id),
            "email_submitted": email_sent
        }

    return {
        "status": "checked_in",
        "attendance_id": str(attendance_id),
        "notification_id": str(notification_id),
        "employee_code": employee["employee_code"],
        "email_submitted": email_sent,
        "notification_status": (
            "sent" if email_sent else "failed"
        ),
        "check_in_time": now.isoformat()
    }


@router.post("/check-in")
def demo_check_in(
    request: CheckInRequest,
    x_demo_token: str | None = Header(default=None)
):
    """Original manual demo check-in (behaviour unchanged)."""
    verify_token(x_demo_token)

    if request.manually_confirmed is not True:
        raise HTTPException(
            400,
            "Explicit manual confirmation is required"
        )

    return record_check_in(request.employee_code, "manual_demo")


@router.post("/recognized-check-in")
def recognized_check_in(
    request: RecognizedCheckInRequest,
    x_demo_token: str | None = Header(default=None)
):
    """Check-in triggered by the Colab face-recognition notebook."""
    verify_token(x_demo_token)

    # Safety floor on the server. The notebook applies its own
    # (stricter) policy; these values are only a backstop.
    min_score = float(os.getenv("ATTENDANCE_MIN_SCORE", "0.40"))
    min_frames = int(os.getenv("ATTENDANCE_MIN_CONFIRMED_FRAMES", "3"))

    if (
        request.match_score < min_score
        or request.confirmed_frames < min_frames
    ):
        return {
            "status": "rejected_low_confidence",
            "email_sent": False,
            "message": (
                f"Requires match_score >= {min_score} and "
                f"confirmed_frames >= {min_frames}"
            )
        }

    logger.info(
        "Recognition check-in: employee=%s score=%.3f frames=%d "
        "video_time=%s source=%s",
        request.employee_code.strip(),
        request.match_score,
        request.confirmed_frames,
        request.video_time_seconds,
        request.source_name
    )

    return record_check_in(request.employee_code, "face_recognition")


@router.post("/retry-failed-emails")
def retry_failed_emails(
    limit: int = 20,
    x_demo_token: str | None = Header(default=None)
):
    """Re-send check-in emails whose delivery failed earlier."""
    verify_token(x_demo_token)

    limit = max(1, min(limit, 50))

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT n.id AS notification_id,
                           n.recipient_email,
                           e.employee_code,
                           e.full_name,
                           a.verified_at,
                           a.verification_method
                    FROM public.notifications n
                    JOIN public.employees e
                      ON e.id = n.employee_id
                    JOIN public.attendance_events a
                      ON a.id = n.attendance_event_id
                    WHERE n.status = 'failed'
                      AND n.notification_type = 'check_in_confirmation'
                    ORDER BY n.created_at
                    LIMIT :limit
                """),
                {"limit": limit}
            ).mappings().all()
    except SQLAlchemyError:
        logger.exception("Failed to load failed notifications")
        raise HTTPException(503, "Unable to load failed notifications")

    sent = 0
    failed = 0

    for row in rows:
        # Claim the row so two simultaneous retries cannot both send it.
        try:
            with engine.begin() as conn:
                claimed = conn.execute(
                    text("""
                        UPDATE public.notifications
                        SET status = 'sending'
                        WHERE id = :id AND status = 'failed'
                        RETURNING id
                    """),
                    {"id": row["notification_id"]}
                ).first()
        except SQLAlchemyError:
            logger.exception("Failed to claim notification")
            failed += 1
            continue

        if claimed is None:
            continue

        employee = {
            "email": row["recipient_email"],
            "full_name": row["full_name"],
            "employee_code": row["employee_code"]
        }

        ok, error = send_with_retry(
            employee, row["verified_at"], row["verification_method"]
        )

        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        UPDATE public.notifications
                        SET status = :status,
                            sent_at = :sent_at,
                            error_message = :error_message
                        WHERE id = :id
                    """),
                    {
                        "id": row["notification_id"],
                        "status": "sent" if ok else "failed",
                        "sent_at": datetime.now(timezone.utc) if ok else None,
                        "error_message": (
                            None if ok
                            else f"SMTP failed on retry ({error})"
                        )
                    }
                )
        except SQLAlchemyError:
            logger.exception("Failed to update notification after retry")

        if ok:
            sent += 1
        else:
            failed += 1

    return {"retried": sent + failed, "sent": sent, "failed": failed}