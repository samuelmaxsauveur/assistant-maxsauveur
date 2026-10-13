"""Photos of a repair pair, kept with its file so they can be compared later.

Two sources, told apart by the file name prefix:
  client_*     what the customer emailed when asking for the repair
  reception_*  what Samuel photographs when the pair arrives

They live on the Railway volume (beside DB_PATH) so they survive redeploys,
are shrunk to a reasonable size for email, and all go to the workshop.
"""
import base64
import hashlib
import io
import os
import time

from PIL import Image, ImageOps

import database
import gmail as gmail_helper

MAX_SIDE = 1600
ALLOWED = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
SOURCES = ("client", "reception")
MAX_CLIENT_PHOTOS = 6


def _dir(case_id):
    base = os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)), "repair_photos", str(int(case_id)))
    os.makedirs(base, exist_ok=True)
    return base


def _jpeg(data):
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


def _tag(data):
    """Short fingerprint of the original bytes, kept in the file name to skip duplicates."""
    return hashlib.md5(data).hexdigest()[:8]


def save(case_id, data, mime, source="reception"):
    """Store one photo as an upright JPEG, at most 1600 px on its long side."""
    if source not in SOURCES:
        raise ValueError("Origine de photo inconnue")
    if (mime or "").lower() not in ALLOWED:
        raise ValueError("Format non pris en charge (JPEG, PNG ou HEIC)")
    tag = _tag(data)
    try:
        data = _jpeg(data)
    except Exception as e:
        raise ValueError(f"Photo illisible : {e}")
    name = f"{source}_{int(time.time() * 1000)}_{tag}.jpg"
    with open(os.path.join(_dir(case_id), name), "wb") as f:
        f.write(data)
    return name


def names(case_id, source=None):
    found = sorted(n for n in os.listdir(_dir(case_id)) if n.endswith(".jpg"))
    return [n for n in found if source is None or n.startswith(source + "_")]


def path(case_id, name):
    """Absolute path of one photo, or None (names are checked, never trusted)."""
    if name not in names(case_id):
        return None
    return os.path.join(_dir(case_id), name)


def delete(case_id, name):
    p = path(case_id, name)
    if p:
        os.remove(p)
    return bool(p)


def load_all(case_id):
    """[(bytes, filename_hint)] for the workshop email: customer photos first."""
    result = []
    for source in SOURCES:
        for n in names(case_id, source):
            with open(os.path.join(_dir(case_id), n), "rb") as f:
                result.append((f.read(), source))
    return result


def _customer_messages(service, case):
    """The repair request thread, then the customer's other emails with attachments."""
    messages, seen = [], set()

    def add(msg):
        if msg["id"] not in seen:
            seen.add(msg["id"])
            messages.append(msg)

    if case.get("email_id"):
        try:
            first = service.users().messages().get(userId="me", id=case["email_id"], format="minimal").execute()
            thread = service.users().threads().get(userId="me", id=first["threadId"], format="full").execute()
            for msg in thread.get("messages", []):
                add(msg)
        except Exception as e:
            print(f"[Photos] Thread unavailable for {case['wing_ref']}: {e}")
    if case.get("customer_email"):
        res = service.users().messages().list(
            userId="me", q=f"from:{case['customer_email']} has:attachment newer_than:1y", maxResults=5).execute()
        for ref in res.get("messages", []):
            add(service.users().messages().get(userId="me", id=ref["id"], format="full").execute())
    return messages


def import_customer_photos(case):
    """Copy the customer's emailed photos into the file (once, duplicates skipped)."""
    service = gmail_helper.get_gmail_service()
    existing = {n.rsplit("_", 1)[-1][:-4] for n in names(case["id"], "client")}
    added = 0
    for msg in _customer_messages(service, case):
        sender = next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower() == "from"), "")
        if "maxsauveur.com" in sender.lower():
            continue
        for photo in gmail_helper.get_image_attachments(service, msg):
            if len(names(case["id"], "client")) >= MAX_CLIENT_PHOTOS:
                return added
            raw = base64.b64decode(photo["data"])
            if _tag(raw) in existing:
                continue
            try:
                save(case["id"], raw, photo["mime"], source="client")
            except ValueError as e:
                print(f"[Photos] Skipped a customer photo for {case['wing_ref']}: {e}")
                continue
            existing.add(_tag(raw))
            added += 1
    return added
