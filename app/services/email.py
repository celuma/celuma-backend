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

    def __init__(self, client=None):
        # Céluma 1.3 Phase 5, Block G-B CI remediation.
        #
        # The client used to be built here. Constructing a boto3 client
        # resolves a region, credentials and an endpoint, so an eager one made
        # *instantiating* this service an operation that can fail — and
        # `create_invitation` instantiates it on every invitation, whether or
        # not an email will actually be sent.
        #
        # In GitHub Actions, where no AWS variable exists, that raised
        # `botocore.exceptions.NoRegionError` out of `__init__` and turned a
        # seat-accounting test into a 500. Locally it passed only because a
        # gitignored `.env` supplies `AWS_REGION`, which
        # `effective_email_ses_region` falls back to. The test was never about
        # email.
        #
        # `SesEmailProvider` already solved this — it builds nothing until a
        # send happens, locked by
        # `test_no_client_is_built_until_a_send_happens`. This is the same
        # pattern applied to the class `block-e-dependencies.md` recorded as
        # untestable precisely because it built its client in `__init__`.
        #
        # `client` is injectable for the same reason it is on
        # `SesEmailProvider`: so the mapping logic can be exercised without
        # credentials, a network or a region that offers SES.
        self._client = client
        self.sender_email = settings.email_sender

    @property
    def client(self):
        """The SES client, built on first use.

        A property rather than an attribute so existing call sites
        (`self.client.send_email(...)`) are unchanged, while construction
        moves to the point where a send is genuinely about to happen.
        """
        if self._client is None:
            self._client = boto3.client(
                'ses',
                region_name=settings.effective_email_ses_region,
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
            )
        return self._client

    def _sending_is_enabled(self) -> bool:
        """Whether this environment may send account email at all.

        Céluma 1.3 Phase 5, Block G-B CI remediation.

        `EMAIL_ENABLED` is documented as the single guard between Céluma and a
        real inbox, and since SES production access was granted the sandbox no
        longer backs that claim up. This class did not honour it: it predates
        the flag, so invitation and password-reset mail was gated only by
        whether `EMAIL_SENDER` happened to be set.

        Production is unaffected today — the task definition sets no `EMAIL_*`
        variable, so `email_sender` is `None` and `_sender_or_none()` already
        refuses. The gap is what happens next: configuring `EMAIL_SENDER`
        while deliberately leaving `EMAIL_ENABLED=false` would have started
        sending real mail to real people with no other change. Checking the
        flag here makes the documented invariant true rather than incidental.
        """
        if not settings.email_enabled:
            logger.info(
                "Email delivery is disabled; no account email was sent",
                extra={
                    "event": "email.delivery_disabled",
                    "error_code": "email_delivery_disabled",
                },
            )
            return False
        return True

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

        if not self._sending_is_enabled():
            return False

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

        if not self._sending_is_enabled():
            return False

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

