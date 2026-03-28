import os
import re
import json
import unicodedata
import base64
import email.mime.text
import tempfile
import requests as requests_lib
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']


def _load_token_data():
    token_env = os.getenv('GMAIL_TOKEN_JSON')
    if token_env:
        try:
            # Try base64 decode first (new format, avoids Railway encoding issues)
            decoded = base64.b64decode(token_env.strip()).decode('utf-8')
            return json.loads(decoded)
        except Exception:
            pass
        # Fallback: try raw JSON with aggressive cleaning
        cleaned = ''.join(c for c in token_env if unicodedata.category(c)[0] != 'C')
        return json.loads(cleaned)
    if os.path.exists('token.json'):
        with open('token.json') as f:
            return json.load(f)
    return None


def _manual_refresh(token_data):
    """Rafraîchit le token via requests (identique à curl, plus fiable sur Railway)."""
    resp = requests_lib.post('https://oauth2.googleapis.com/token', data={
        'client_id': token_data['client_id'],
        'client_secret': token_data['client_secret'],
        'refresh_token': token_data['refresh_token'],
        'grant_type': 'refresh_token'
    })
    resp.raise_for_status()
    new_data = resp.json()
    return Credentials(
        token=new_data['access_token'],
        refresh_token=token_data['refresh_token'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=token_data['client_id'],
        client_secret=token_data['client_secret'],
        scopes=SCOPES
    )


def get_gmail_service():
    token_data = _load_token_data()

    if token_data:
        # On tente le refresh manuel directement (contourne les bugs de google-auth sur Railway)
        try:
            creds = _manual_refresh(token_data)
            return build('gmail', 'v1', credentials=creds)
        except Exception:
            pass

    # Fallback : OAuth interactif (uniquement en local)
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
    # Recursive search: prioritize text/plain, fallback to text/html stripped
    plain = _find_body(payload, 'text/plain')
    if plain:
        return plain
    html = _find_body(payload, 'text/html')
    if html:
        return re.sub(r'<[^>]+>', ' ', html).strip()
    return ''


def _find_body(payload, mime_type):
    if payload.get('mimeType') == mime_type:
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
    for part in payload.get('parts', []):
        result = _find_body(part, mime_type)
        if result:
            return result
    return ''


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
