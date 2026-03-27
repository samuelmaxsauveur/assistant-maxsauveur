import os
import json
import base64
import email.mime.text
import tempfile
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def get_gmail_service():
    creds = None

    # Sur Railway : token stocké dans la variable d'environnement GMAIL_TOKEN_JSON
    token_env = os.getenv('GMAIL_TOKEN_JSON')
    if token_env:
        import re
        token_env = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', token_env)
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
    elif os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Sauvegarder le token rafraîchi localement si possible
            try:
                with open('token.json', 'w') as f:
                    f.write(creds.to_json())
            except Exception:
                pass
        else:
            # OAuth interactif — uniquement possible en local
            creds_env = os.getenv('GMAIL_CREDENTIALS_JSON')
            if creds_env:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                    f.write(creds_env)
                    creds_file = f.name
            else:
                creds_file = 'credentials.json'
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
            with open('token.json', 'w') as f:
                f.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)

def get_unread_emails(service, max_results=10):
    results = service.users().messages().list(
        userId='me',
        labelIds=['INBOX', 'UNREAD'],
        maxResults=max_results
    ).execute()
    messages = results.get('messages', [])
    emails = []
    for msg in messages:
        email_data = service.users().messages().get(
            userId='me',
            id=msg['id'],
            format='full'
        ).execute()
        emails.append(parse_email(email_data))
    return emails

def parse_email(email_data):
    headers = email_data['payload']['headers']
    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
    sender = next((h['value'] for h in headers if h['name'] == 'From'), '')
    date = next((h['value'] for h in headers if h['name'] == 'Date'), '')
    body = get_email_body(email_data['payload'])
    return {
        'id': email_data['id'],
        'thread_id': email_data['threadId'],
        'subject': subject,
        'sender': sender,
        'date': date,
        'body': body
    }

def get_email_body(payload):
    body = ''
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain':
                data = part['body'].get('data', '')
                if data:
                    body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                    break
    if not body and payload['body'].get('data'):
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
    return body

def send_email(service, to, subject, body, thread_id=None):
    message = email.mime.text.MIMEText(body, 'plain', 'utf-8')
    message['to'] = to
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body_data = {'raw': raw}
    if thread_id:
        body_data['threadId'] = thread_id
    service.users().messages().send(userId='me', body=body_data).execute()

def mark_as_read(service, message_id):
    service.users().messages().modify(
        userId='me',
        id=message_id,
        body={'removeLabelIds': ['UNREAD']}
    ).execute()
