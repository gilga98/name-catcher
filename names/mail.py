"""Sign-up and password emails go through the job queue, so a slow mail server never holds up a page."""

from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.mail.backends.base import BaseEmailBackend

from . import jobs


class QueuedEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages) -> int:
        for message in email_messages:
            payload = {
                "subject": message.subject,
                "body": message.body,
                "from_email": message.from_email,
                "to": list(message.to),
                "cc": list(message.cc),
                "bcc": list(message.bcc),
                "headers": dict(message.extra_headers),
                "alternatives": [list(item) for item in getattr(message, "alternatives", [])],
            }
            jobs.enqueue(jobs.MAIL, "send_email", payload)
        return len(email_messages)


def deliver(payload: dict, backend: str) -> None:
    message = EmailMultiAlternatives(
        subject=payload["subject"],
        body=payload["body"],
        from_email=payload["from_email"],
        to=payload["to"],
        cc=payload["cc"],
        bcc=payload["bcc"],
        headers=payload["headers"],
        connection=get_connection(backend),
    )
    for content, mimetype in payload["alternatives"]:
        message.attach_alternative(content, mimetype)
    message.send()
