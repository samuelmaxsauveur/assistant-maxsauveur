"""PDF documents of repair files: workshops' UPS labels and credit notes.

Stored on the Railway volume next to the database. A document found in a
workshop email that the assistant cannot tie to a file waits in an inbox
until Samuel picks the file on the Réparations page.
"""
import os
import re
import shutil
import time

import database

KINDS = {"label": "Étiquette UPS", "credit": "Avoir", "document": "Document"}


def _base():
    return os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)), "repair_docs")


def _case_dir(case_id):
    d = os.path.join(_base(), str(int(case_id)))
    os.makedirs(d, exist_ok=True)
    return d


def _inbox_dir():
    d = os.path.join(_base(), "inbox")
    os.makedirs(d, exist_ok=True)
    return d


def _safe(filename):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "document.pdf").strip("._") or "document.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def save_to_case(case_id, kind, filename, data):
    name = f"{kind}_{int(time.time() * 1000)}_{_safe(filename)}"
    with open(os.path.join(_case_dir(case_id), name), "wb") as f:
        f.write(data)
    return name


def save_to_inbox(filename, data):
    name = f"{int(time.time() * 1000)}_{_safe(filename)}"
    with open(os.path.join(_inbox_dir(), name), "wb") as f:
        f.write(data)
    return name


def inbox_path(stored_as):
    p = os.path.join(_inbox_dir(), os.path.basename(stored_as))
    return p if os.path.exists(p) else None


def file_inbox_item(item, case_ids):
    """Copy an inbox document into the chosen files."""
    src = inbox_path(item["stored_as"])
    if not src:
        raise ValueError("Document introuvable")
    for cid in case_ids:
        name = f"{item['kind']}_{int(time.time() * 1000)}_{_safe(item['filename'])}"
        shutil.copyfile(src, os.path.join(_case_dir(cid), name))
    database.set_repair_inbox_cases(item["id"], ",".join(str(c) for c in case_ids))


def names(case_id):
    return sorted(n for n in os.listdir(_case_dir(case_id)) if n.endswith(".pdf"))


def describe(name):
    """(kind label, original file name) for a stored document name."""
    kind, _, rest = name.partition("_")
    return KINDS.get(kind, "Document"), rest.partition("_")[2] or name


def path(case_id, name):
    if name not in names(case_id):
        return None
    return os.path.join(_case_dir(case_id), name)


def save_upload(case_id, kind, filename, data):
    if kind not in KINDS:
        raise ValueError("Type de document inconnu")
    if not data.startswith(b"%PDF"):
        raise ValueError("Ce fichier n'est pas un PDF")
    return save_to_case(case_id, kind, filename, data)
