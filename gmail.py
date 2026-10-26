import os
import re
import html as html_lib
import quopri
import json
import unicodedata
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import email.mime.text
import email.mime.multipart
import email.mime.base
import email.encoders
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


# Samuel's own mailbox: sends to the workshops and reads their replies
# (UPS labels, credit notes). The SAV box stays the one customers see.
SAMUEL_SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
SAMUEL_TOKEN_FILE = 'token_samuel.json'


def _load_samuel_token():
    token_env = os.getenv('GMAIL_SAMUEL_TOKEN_JSON')
    if token_env:
        return json.loads(base64.b64decode(token_env.strip()).decode('utf-8'))
    if os.path.exists(SAMUEL_TOKEN_FILE):
        with open(SAMUEL_TOKEN_FILE) as f:
            return json.load(f)
    return None


def get_samuel_service():
    """Gmail service for samuel@maxsauveur.com, or None if not connected yet."""
    token_data = _load_samuel_token()
    if not token_data:
        return None
    return build('gmail', 'v1', credentials=_manual_refresh(token_data))


def authorize_samuel():
    """One-time, on the Mac: sign in as samuel@maxsauveur.com in the browser."""
    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SAMUEL_SCOPES)
    creds = flow.run_local_server(port=0, login_hint='samuel@maxsauveur.com', prompt='consent')
    with open(SAMUEL_TOKEN_FILE, 'w') as f:
        f.write(creds.to_json())
    return creds


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
        'body': body,
        'internal_date': int(email_data.get('internalDate') or 0),
    }


def last_reply_at(service, thread_id):
    """ms epoch of the last message we sent on a thread, 0 if none.

    Uses Gmail's SENT label and internalDate: the Date header sorts as a
    string, which puts "Fri" before "Mon" whatever the actual dates.
    """
    thread = service.users().threads().get(
        userId='me', id=thread_id, format='minimal'
    ).execute()
    return max(
        (int(m.get('internalDate') or 0) for m in thread.get('messages', [])
         if 'SENT' in m.get('labelIds', [])),
        default=0,
    )


def needs_reply(service, email, scheduled_threads):
    """'new', 'answered' or 'scheduled' for an email still unread in the inbox.

    An email counts as answered when we wrote on its thread after it arrived
    (from the dashboard or straight from Gmail), and as scheduled when a reply
    was programmed after it arrived. A customer message arriving later is new.
    """
    received = email.get('internal_date') or 0
    if scheduled_threads.get(email['thread_id'], 0) > received:
        return 'scheduled'
    if last_reply_at(service, email['thread_id']) > received:
        return 'answered'
    return 'new'


def _strip_quoted(body):
    """Our own words only: drop the quoted customer message below them."""
    kept = []
    for line in (body or '').splitlines():
        if line.startswith('>') or re.match(r'^(Le .+ a écrit|On .+ wrote)\s*:?\s*$', line.strip()):
            break
        kept.append(line)
    return '\n'.join(kept).strip()


def product_replies_context(service, orders, max_per_product=3):
    """What Samuel recently told other customers about the products ordered.

    Pre-orders, announced delivery dates and restocks are only ever written in
    replies, and the customer history only covers this one customer. Searching
    the sent folder by model and colour ("Blucher" "Noir") brings them back.
    """
    seen, blocks = set(), []
    for order in (orders or [])[:2]:
        for product in order.get('products', []):
            parts = [p.strip() for p in (product.get('name') or '').split(' - ')]
            if not parts or not parts[0]:
                continue
            model = parts[0]
            # "Blucher - Chaussures Derby Cuir Noir - 41.5": the colour ends the
            # middle part. "Embauchoirs bois de cèdre - 43" has no middle part:
            # its second part is the size, which no reply ever quotes.
            colour = parts[1].split()[-1] if len(parts) > 2 and parts[1].split() else ''
            key = (model.lower(), colour.lower())
            if key in seen:
                continue
            seen.add(key)
            q = (f'in:sent "{model}" {colour} newer_than:90d -to:samuel@maxsauveur.com '
                 f'-subject:"en attente" -subject:"Quelques informations utiles"')
            try:
                found = service.users().messages().list(
                    userId='me', q=q, maxResults=max_per_product).execute().get('messages', [])
                for ref in found:
                    msg = parse_email(service.users().messages().get(
                        userId='me', id=ref['id'], format='full').execute())
                    text = _strip_quoted(msg['body'])[:1200]
                    if text:
                        blocks.append(f"[{msg['date'][:16]}] {text}")
            except Exception as e:
                print(f"product_replies_context failed for {model}: {e}")
    if not blocks:
        return ''
    return ("\n\n--- RÉPONSES RÉCENTES DE SAMUEL SUR CE PRODUIT (à d'autres clients) ---\n"
            + "\n\n".join(blocks))


def get_emails_to_answer(service, max_results=10):
    """Unread inbox emails that have neither been answered nor scheduled.

    Answered ones are marked read so they stop taking a slot. Scheduled ones are
    left unread: if the scheduled reply is cancelled, the email comes back.
    """
    import database
    scheduled_threads = database.get_scheduled_threads()
    result = []
    for email in get_unread_emails(service, max_results=max_results * 3):
        try:
            state = needs_reply(service, email, scheduled_threads)
        except Exception as e:
            print(f"needs_reply failed for {email['id']}: {e}")
            state = 'new'
        if state == 'answered':
            try:
                mark_as_read(service, email['id'])
            except Exception:
                pass
            database.close_drafts_for_thread(email['thread_id'])
            continue
        if state == 'scheduled':
            continue
        result.append(email)
        if len(result) >= max_results:
            break
    return result


def _clean_text(text):
    """Decode HTML entities, quoted-printable, and collapse whitespace."""
    if not text:
        return ''
    # Decode quoted-printable soft line breaks (=\n) and encoded chars (=XX)
    try:
        text = quopri.decodestring(text.encode()).decode('utf-8', errors='ignore')
    except Exception:
        pass
    # Decode HTML entities (&lt; &gt; &amp; &#13; etc.)
    text = html_lib.unescape(text)
    # Collapse excessive blank lines (keep max 2 newlines in a row)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def get_email_body(payload):
    # Recursive search: prioritize text/plain, fallback to text/html stripped
    plain = _find_body(payload, 'text/plain')
    if plain:
        return _clean_text(plain)
    html = _find_body(payload, 'text/html')
    if html:
        # Remove tags, then decode remaining entities
        stripped = re.sub(r'<[^>]+>', ' ', html)
        stripped = re.sub(r'[ \t]+', ' ', stripped)
        return _clean_text(stripped)
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


def extract_email_address(sender):
    match = re.search(r'<(.+?)>', sender)
    return match.group(1) if match else sender


def extract_customer_name(sender):
    match = re.search(r'^(.+?)\s*<', sender)
    if match:
        return match.group(1).strip().strip('"')
    return sender


def resolve_sender(sender, body):
    """Real customer address and name behind a noreply form address.

    Shogun contact forms arrive from a noreply address, with the customer's own
    address written in the body. Anything keyed on the raw sender (order lookup,
    history, the reply-to) would then target the noreply address and find
    nothing. Lives here so app.py and scheduler.py resolve senders identically.
    """
    raw_email = extract_email_address(sender)
    customer_name = extract_customer_name(sender)

    if re.search(r'noreply|no-reply|donotreply', raw_email, re.IGNORECASE):
        email_match = re.search(
            r'e-?mail\s*:\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
            body, re.IGNORECASE)
        if email_match:
            raw_email = email_match.group(1).strip()
        # Stop at the next form label. get_email_body collapses the form onto a
        # single line, so a plain (.+) swallows the address, the subject and the
        # whole message into the customer's name.
        name_match = re.search(
            r'nom\s*:\s*(.+?)\s*(?=(?:e-?mail|courriel|sujet|objet|message|t[ée]l[ée]phone|tel)\s*:|$)',
            body, re.IGNORECASE)
        if name_match and name_match.group(1).strip():
            customer_name = name_match.group(1).strip()

    return raw_email, customer_name


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _date_sort_key(msg):
    """Real datetime from the RFC 2822 Date header.

    Sorting the raw header string instead sorts on the weekday name, so
    "Wed, 9 Apr 2025" lands ahead of "Mon, 28 Sep 2026" and the most recent
    exchanges get dropped from the history sent to Claude.
    """
    try:
        d = parsedate_to_datetime(msg.get('date', ''))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return _EPOCH


def get_customer_history(service, sender_email, max_results=10):
    """Fetch recent sent+received emails with a given customer address."""
    history = []
    # Emails received from this customer
    results = service.users().messages().list(
        userId='me',
        q=f'from:{sender_email}',
        maxResults=max_results
    ).execute()
    for msg in results.get('messages', []):
        data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        parsed = parse_email(data)
        parsed['direction'] = 'received'
        history.append(parsed)
    # Emails sent to this customer
    results = service.users().messages().list(
        userId='me',
        q=f'to:{sender_email}',
        maxResults=max_results
    ).execute()
    for msg in results.get('messages', []):
        data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        parsed = parse_email(data)
        parsed['direction'] = 'sent'
        history.append(parsed)
    # Keep the 25 most recent exchanges, oldest first so the conversation reads
    # in the order it happened. Six was thin on an active support thread: the
    # start of the conversation fell out of the context sent to Claude.
    history.sort(key=_date_sort_key, reverse=True)
    return list(reversed(history[:25]))


def get_thread_messages(service, thread_id):
    """Fetch all messages in a Gmail thread, sorted chronologically."""
    thread = service.users().threads().get(
        userId='me',
        id=thread_id,
        format='full'
    ).execute()
    messages = []
    for msg in thread.get('messages', []):
        parsed = parse_email(msg)
        # Determine direction: if From contains our address, it's sent
        sender = parsed.get('sender', '')
        parsed['direction'] = 'sent' if 'maxsauveur.com' in sender.lower() else 'received'
        messages.append(parsed)
    # Sort chronologically by date header
    messages.sort(key=lambda x: x.get('date', ''))
    return messages


# Les seuls types d'image acceptés par l'API Anthropic.
_SUPPORTED_IMAGE_MIMES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _normalise_image_data(data):
    """Gmail encodes attachments in URL-safe base64, the API wants standard base64.

    Passing Gmail's string straight through gets rejected with
    "invalid base64 data", and that error used to kill the processing of the
    whole email, photo and text alike. Returns None for anything unusable.
    """
    cleaned = re.sub(r'\s+', '', data or '')
    # Reject anything outside the url-safe alphabet up front: the decoder drops
    # stray characters silently, so garbage would decode into garbage instead
    # of being refused.
    if not cleaned or re.search(r'[^A-Za-z0-9\-_=]', cleaned):
        return None
    try:
        raw = base64.urlsafe_b64decode(cleaned + '=' * (-len(cleaned) % 4))
    except Exception:
        return None
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        return None
    return base64.b64encode(raw).decode('ascii')


def get_image_attachments(service, email_data):
    """Extract image attachments from an email as base64 strings (max 5)."""
    images = []
    _extract_images(service, email_data.get('id', ''), email_data.get('payload', {}), images)
    return images[:5]


def _extract_images(service, message_id, payload, result):
    mime = (payload.get('mimeType') or '').lower()
    body = payload.get('body', {})
    if mime in _SUPPORTED_IMAGE_MIMES:
        data = body.get('data')
        if not data and body.get('attachmentId'):
            try:
                att = service.users().messages().attachments().get(
                    userId='me', messageId=message_id, id=body['attachmentId']
                ).execute()
                data = att.get('data')
            except Exception:
                data = None
        if data:
            clean = _normalise_image_data(data)
            if clean:
                result.append({'data': clean, 'mime': mime})
    for part in payload.get('parts', []):
        _extract_images(service, message_id, part, result)


def send_email(service, to, subject, body, thread_id=None,
               attachment_bytes=None, attachment_filename='etiquette_retour.pdf'):
    if attachment_bytes:
        message = email.mime.multipart.MIMEMultipart()
        message.attach(email.mime.text.MIMEText(body, 'plain', 'utf-8'))
        part = email.mime.base.MIMEBase('application', 'pdf')
        part.set_payload(attachment_bytes)
        email.encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=attachment_filename)
        message.attach(part)
    else:
        message = email.mime.text.MIMEText(body, 'plain', 'utf-8')
    message['to'] = to
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body_data = {'raw': raw}
    if thread_id:
        body_data['threadId'] = thread_id
    service.users().messages().send(userId='me', body=body_data).execute()


def send_email_with_files(service, to, subject, body, files):
    """Send a new email with several attachments, given as (bytes, filename, mime) tuples."""
    message = email.mime.multipart.MIMEMultipart()
    message.attach(email.mime.text.MIMEText(body, 'plain', 'utf-8'))
    for data, filename, mime in files:
        maintype, _, subtype = (mime or 'application/octet-stream').partition('/')
        part = email.mime.base.MIMEBase(maintype, subtype or 'octet-stream')
        part.set_payload(data)
        email.encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=filename)
        message.attach(part)
    message['to'] = to
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()


def mark_as_read(service, message_id):
    service.users().messages().modify(
        userId='me',
        id=message_id,
        body={'removeLabelIds': ['UNREAD']}
    ).execute()
