import boto3
from botocore.exceptions import ClientError, BotoCoreError
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


class EmailService:
    """Service for sending emails via AWS SES"""
    
    def __init__(self):
        self.client = boto3.client(
            'ses',
            region_name=getattr(settings, 'aws_region', 'us-east-1'),
            aws_access_key_id=getattr(settings, 'aws_access_key_id', None),
            aws_secret_access_key=getattr(settings, 'aws_secret_access_key', None),
        )
        self.sender_email = getattr(settings, 'email_sender', 'noreply@celuma.com')
    
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
        
        try:
            response = self.client.send_email(
                Source=self.sender_email,
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
        
        try:
            response = self.client.send_email(
                Source=self.sender_email,
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

