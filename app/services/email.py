import boto3
from botocore.exceptions import ClientError, BotoCoreError
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


class EmailService:
    """Service for sending transactional account emails (invitation, password
    reset) via AWS SES.

    Céluma 1.3 Phase 3, Block E, Story E1: this class used to read its sender
    and its region through defaulted attribute lookups against fields that did
    not exist on ``Settings``. A defaulted lookup cannot fail, so the sender
    was *always* the hardcoded ``noreply@celuma.com`` — in every environment —
    and since that address is not a verified SES identity, every send was
    rejected by SES and swallowed by the ``return False`` below. Both
    fallbacks are gone: the settings are real fields now, and an unset sender
    is reported rather than papered over.

    ``email_ses_region`` rather than ``aws_region`` because Céluma runs in
    ``mx-central-1``, where SES is not offered — see ``Settings``.

    This class predates Block E and is **not** the notification delivery path.
    Notification email goes through ``app/services/email_provider.py`` and the
    delivery worker, which has a provider abstraction, sanitized error codes,
    a retry lifecycle and tests. This one is left in place, with its
    configuration corrected, because rewriting the invitation/reset flows is
    not Block E's scope.
    """

    def __init__(self):
        self.client = boto3.client(
            'ses',
            region_name=settings.effective_email_ses_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        self.sender_email = settings.email_sender

    def _sender_or_none(self) -> str | None:
        """The configured sender, or None with one log line explaining why no
        email will be sent.

        Cheaper and far more diagnosable than handing ``Source=None`` to SES
        and reading the resulting ``ParamValidationError`` — which is what the
        old silent default effectively produced, one network round trip later.
        """
        if not (self.sender_email or "").strip():
            logger.error(
                "Email sender is not configured; no email was sent",
                extra={
                    "event": "email.not_configured",
                    "error_code": "email_sender_not_configured",
                },
            )
            return None
        return self.sender_email

    def send_invitation_email(
        self,
        recipient_email: str,
        recipient_name: str,
        lab_name: str,
        invitation_url: str,
    ) -> bool:
        """Send invitation email to new user"""
        subject = f"Invitación a {lab_name} en Céluma"
        
        html_body = f"""
        <html>
        <head></head>
        <body>
            <h2>Hola {recipient_name},</h2>
            <p>Has sido invitado a unirte a <strong>{lab_name}</strong> en Céluma.</p>
            <p>Para aceptar la invitación y crear tu cuenta, haz clic en el siguiente enlace:</p>
            <p><a href="{invitation_url}">Aceptar Invitación</a></p>
            <p>Este enlace expirará en 7 días.</p>
            <p>Si no esperabas esta invitación, puedes ignorar este correo.</p>
            <br>
            <p>Saludos,<br>Equipo Céluma</p>
        </body>
        </html>
        """
        
        text_body = f"""
        Hola {recipient_name},
        
        Has sido invitado a unirte a {lab_name} en Céluma.
        
        Para aceptar la invitación, visita: {invitation_url}
        
        Este enlace expirará en 7 días.
        
        Saludos,
        Equipo Céluma
        """

        sender = self._sender_or_none()
        if sender is None:
            return False

        try:
            response = self.client.send_email(
                Source=sender,
                Destination={'ToAddresses': [recipient_email]},
                Message={
                    'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                    'Body': {
                        'Text': {'Data': text_body, 'Charset': 'UTF-8'},
                        'Html': {'Data': html_body, 'Charset': 'UTF-8'}
                    }
                }
            )

            logger.info(
                f"Invitation email sent to {recipient_email}",
                extra={
                    "event": "email.invitation_sent",
                    "recipient": recipient_email,
                    "message_id": response['MessageId'],
                },
            )
            return True
            
        except (ClientError, BotoCoreError) as e:
            error_msg = str(e)
            if hasattr(e, 'response') and 'Error' in e.response:
                error_msg = e.response['Error']['Message']
                
            logger.error(
                f"Failed to send invitation email to {recipient_email}: {error_msg}",
                extra={
                    "event": "email.invitation_failed",
                    "recipient": recipient_email,
                    "error": error_msg,
                },
            )
            return False
        except Exception as e:
            logger.exception(f"Unexpected error sending invitation email to {recipient_email}")
            return False
    
    def send_password_reset_email(
        self,
        recipient_email: str,
        recipient_name: str,
        reset_url: str,
    ) -> bool:
        """Send password reset email"""
        subject = "Recuperación de Contraseña - Céluma"
        
        html_body = f"""
        <html>
        <head></head>
        <body>
            <h2>Hola {recipient_name},</h2>
            <p>Has solicitado restablecer tu contraseña en Céluma.</p>
            <p>Para crear una nueva contraseña, haz clic en el siguiente enlace:</p>
            <p><a href="{reset_url}">Restablecer Contraseña</a></p>
            <p>Este enlace expirará en 1 hora.</p>
            <p>Si no solicitaste este cambio, puedes ignorar este correo de forma segura.</p>
            <br>
            <p>Saludos,<br>Equipo Céluma</p>
        </body>
        </html>
        """
        
        text_body = f"""
        Hola {recipient_name},
        
        Has solicitado restablecer tu contraseña en Céluma.
        
        Para crear una nueva contraseña, visita: {reset_url}
        
        Este enlace expirará en 1 hora.
        
        Saludos,
        Equipo Céluma
        """

        sender = self._sender_or_none()
        if sender is None:
            return False

        try:
            response = self.client.send_email(
                Source=sender,
                Destination={'ToAddresses': [recipient_email]},
                Message={
                    'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                    'Body': {
                        'Text': {'Data': text_body, 'Charset': 'UTF-8'},
                        'Html': {'Data': html_body, 'Charset': 'UTF-8'}
                    }
                }
            )

            logger.info(
                f"Password reset email sent to {recipient_email}",
                extra={
                    "event": "email.password_reset_sent",
                    "recipient": recipient_email,
                    "message_id": response['MessageId'],
                },
            )
            return True
            
        except (ClientError, BotoCoreError) as e:
            error_msg = str(e)
            if hasattr(e, 'response') and 'Error' in e.response:
                error_msg = e.response['Error']['Message']
                
            logger.error(
                f"Failed to send password reset email to {recipient_email}: {error_msg}",
                extra={
                    "event": "email.password_reset_failed",
                    "recipient": recipient_email,
                    "error": error_msg,
                },
            )
            return False
        except Exception as e:
            logger.exception(f"Unexpected error sending password reset email to {recipient_email}")
            return False

