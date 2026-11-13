
import os
import sys
import ssl
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv
from sqlalchemy import text

from app.database import engine

load_dotenv()


def send_test_email(employee_code):
    with engine.connect() as connection:
        employee = connection.execute(
            text("""
                SELECT employee_code, full_name, email
                FROM public.employees
                WHERE employee_code = :employee_code
                  AND status = 'active'
                LIMIT 1
            """),
            {"employee_code": employee_code}
        ).mappings().first()

    if employee is None:
        print("Employee not found or inactive.")
        return False

    if not employee["email"]:
        print("Employee has no email address.")
        return False

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_user = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ["SMTP_FROM_EMAIL"]
    from_name = os.environ["SMTP_FROM_NAME"]

    message = EmailMessage()
    message["Subject"] = "Face Recognition Attendance - Test Email"
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = employee["email"]

    message.set_content(
        f"Hello {employee['full_name']},\n\n"
        "This is a test email from the Face Recognition "
        "Attendance System.\n\n"
        "Your employee details were retrieved successfully "
        "from the database.\n\n"
        "This message is only a system test. "
        "It does not confirm your attendance.\n\n"
        "Regards,\n"
        "Face Recognition Attendance System"
    )

    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(
        smtp_host,
        smtp_port,
        context=context,
        timeout=30,
    ) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(message)

    print("EMAIL SUBMITTED SUCCESSFULLY")
    print("Employee:", employee["full_name"])
    print("Recipient:", employee["email"])

    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python send_test_email.py EMP001")
        sys.exit(1)

    try:
        successful = send_test_email(sys.argv[1])
        sys.exit(0 if successful else 1)
    except Exception as error:
        print("EMAIL SENDING FAILED")
        print("Error type:", type(error).__name__)
        print(
            "Check SMTP credentials, network connectivity, "
            "and the selected employee."
        )
        sys.exit(1)
