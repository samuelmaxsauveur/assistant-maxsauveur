"""Preparation errors by SSL, the logistics centre: a wrong pair or a missing item.

From the customer's email to the right item in their hands, and what SSL owes
for each confirmed error (deducted from its invoice, claimed at the end of the
month by an email from samuel@ to SSL).

Wrong pair:   photos asked (box label + pair) -> Claude compares them with the
              order -> Samuel confirms -> return label on the second Wing account
              (#<order>_ERREUR) -> pair back at SSL -> the customer confirms a
              Chrono2Shop relay or the home address on /erreur/<token> ->
              #<order>_RENVOI on the main Wing account -> tracking -> done.
Missing item: photo of the parcel content -> Samuel confirms -> #<order>_RENVOI
              to the same relay or address as the order -> tracking -> done.

Every customer email is prepared here and only leaves on Samuel's click.
The label and the Wing orders are created at the step that needs them.
"""
import base64
import json
import os
import re
import secrets
from datetime import datetime, timezone

import anthropic
import pytz

import database
import gmail as gmail_helper
import repair_catalog
import repair_photos
import retours
import returns
import wing_repair

PARIS = pytz.timezone("Europe/Paris")
BASE_URL = returns.BASE_URL
SSL_EMAIL = "jclerc@sslfrance.com"      # sent from samuel@ (Samuel, 10/11/2026)
SAMUEL_EMAIL = "samuel@maxsauveur.com"

WRONG, MISSING = "wrong_item", "missing_item"
KINDS = {WRONG: "Mauvaise paire reçue", MISSING: "Commande incomplète"}

# What SSL owes for one confirmed error (Samuel, 10/11/2026).
COST_RESHIP = 19.30    # wrong pair, right one sent: shipping + preparation + return label
COST_REFUND = 12.00    # wrong pair, customer refunded instead
COST_MISSING = 10.00   # missing item sent: shipping + preparation

RESHIP_SUFFIXES = ("_RENVOI", "_RENVOI_BIS", "_RENVOI_TER")
MAX_PHOTOS = 8

STATUS_LABELS = {
    "new": "À traiter",
    "waiting_photos": "Photos demandées",
    "to_check": "À vérifier",
    "label_ready": "Étiquette à envoyer",
    "awaiting_return": "Retour en cours",
    "received": "Paire reçue chez SSL",
    "waiting_address": "Adresse demandée",
    "reship_created": "Renvoi créé dans Wing",
    "shipped": "Renvoi expédié",
    "to_refund": "À rembourser",
    "done": "Terminé",
    "not_error": "Pas une erreur de SSL",
    "cancelled": "Annulé",
}
CLOSED = ("done", "not_error", "cancelled")
STAGES = [
    ("a-faire", "À faire"),
    ("attente", "En attente du client"),
    ("renvoi", "Renvoi en cours"),
    ("termines", "Terminés"),
]

MAILS = {
    "ask_photos": "Demande de photos",
    "label": "Excuses et étiquette retour",
    "address": "Paire reçue : relais ou adresse à confirmer",
    "missing": "Excuses et renvoi de l'article manquant",
    "tracking": "Numéro de suivi du renvoi",
    "refund": "Remboursement en cours",
    "not_error": "Pas d'erreur de préparation",
    "free": "Réponse libre (question du client)",
}
# The status a sent email moves the file to (unchanged when absent).
AFTER_SEND = {"ask_photos": "waiting_photos", "label": "awaiting_return", "address": "waiting_address"}


# ---------------------------------------------------------------- small helpers

def now_iso():
    return datetime.now(PARIS).isoformat()


def euros(amount):
    return f"{amount:,.2f} €".replace(",", " ").replace(".", ",")


def short_date(value):
    return retours.short_date(value)


def _log(data, text):
    return (data.get("log") or []) + [f"{datetime.now(PARIS).strftime('%d/%m %H:%M')} · {text}"]


def _first_name(case):
    order = case["data"].get("order") or {}
    name = (order.get("first_name") or (case.get("customer_name") or "").split(" ")[0]).strip()
    return name.capitalize() if name.isupper() or name.islower() else name


def _line_label(line):
    return f"{line['title']} - {line['variant']}" if line.get("variant") else line["title"]


def human_label(line):
    """'Le Moc Jagger - Chaussures Mocassin Cuir Pony Marron' + '41' -> 'Le Moc Jagger (mocassin cuir pony marron) en 41'."""
    parts = [p.strip() for p in (line.get("title") or "").split(" - ")]
    model = parts[0]
    descr = re.sub(r"^(chaussures|boots)\s+", "", parts[1], flags=re.I).lower() if len(parts) > 1 else ""
    text = f"{model} ({descr})" if descr else model
    return f"{text} en {line['variant']}" if line.get("variant") else text


def _lines(case, indexes):
    lines = (case["data"].get("order") or {}).get("lines") or []
    return [lines[i] for i in indexes if 0 <= i < len(lines)]


def form_defaults(case):
    """What the decision form proposes: the pair ordered, what came instead, and where it was read."""
    data = case["data"]
    lines = (data.get("order") or {}).get("lines") or []
    verdict, mail = data.get("verdict") or {}, data.get("email_read") or {}
    line = next((x for x in (verdict.get("line"), mail.get("line")) if isinstance(x, int) and 0 <= x < len(lines)), None)
    if line is None:
        pairs = [i for i, l in enumerate(lines) if l.get("category") != "other"]
        line = pairs[0] if len(pairs) == 1 else (0 if len(lines) == 1 else None)
    received, source = verdict.get("received"), "photos"
    if not received:
        received, source = mail.get("received"), "mail"
    return {"line": line, "ordered": human_label(lines[line]) if line is not None else "",
            "received": received or "", "source": source if received else None, "quote": mail.get("quote"),
            "missing": verdict.get("missing") or mail.get("missing") or [],
            "mail_read": bool(data.get("email_read"))}


def stage(case):
    """The tab a file belongs to. Anything waiting for Samuel goes to « À faire »."""
    status, data = case["status"], case["data"]
    if status in CLOSED:
        return "termines"
    if data.get("reply_unread") or data.get("alert") or proposed_mail(case):
        return "a-faire"
    if status in ("new", "to_check", "label_ready", "received", "to_refund"):
        return "a-faire"
    if status in ("reship_created", "shipped"):
        return "renvoi"
    return "attente"


# ---------------------------------------------------------------- creating a file

def _photo_dir(case_id):
    base = os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)), "logistics_errors", str(int(case_id)))
    os.makedirs(base, exist_ok=True)
    return base


def photo_names(case_id):
    return sorted(n for n in os.listdir(_photo_dir(case_id)) if re.fullmatch(r"photo_\d+_[0-9a-f]{8}\.jpg", n))


def photo_path(case_id, name):
    """Absolute path of one photo, or None (names are checked, never trusted)."""
    return os.path.join(_photo_dir(case_id), name) if name in photo_names(case_id) else None


def save_photo(case_id, raw, mime="image/jpeg"):
    """Store one photo as an upright JPEG; the same picture twice is kept once."""
    if (mime or "").lower() not in repair_photos.ALLOWED:
        raise ValueError("Format non pris en charge (JPEG, PNG ou HEIC)")
    tag = repair_photos._tag(raw)
    if any(n.endswith(f"_{tag}.jpg") for n in photo_names(case_id)):
        return None
    if len(photo_names(case_id)) >= MAX_PHOTOS:
        raise ValueError(f"{MAX_PHOTOS} photos au maximum par dossier")
    try:
        data = repair_photos._jpeg(raw)
    except Exception as e:
        raise ValueError(f"Photo illisible : {e}")
    name = f"photo_{int(datetime.now().timestamp() * 1000)}_{tag}.jpg"
    with open(os.path.join(_photo_dir(case_id), name), "wb") as f:
        f.write(data)
    return name


THUMB_SIDE = 360


def thumb_path(case_id, name):
    """A small copy of the photo for the page (made once): the page no longer loads the full photos."""
    p = photo_path(case_id, name)
    if not p:
        return None
    thumb = os.path.join(_photo_dir(case_id), "mini_" + name)
    if not os.path.exists(thumb):
        from PIL import Image
        with Image.open(p) as img:
            img.thumbnail((THUMB_SIDE, THUMB_SIDE))
            img.convert("RGB").save(thumb, "JPEG", quality=75)
    return thumb


def delete_photo(case_id, name):
    p = photo_path(case_id, name)
    if p:
        os.remove(p)
        thumb = os.path.join(_photo_dir(case_id), "mini_" + name)
        if os.path.exists(thumb):
            os.remove(thumb)
    return bool(p)


def _thread(service, thread_id):
    return service.users().threads().get(userId="me", id=thread_id, format="full").execute().get("messages", [])


def _sender(msg):
    return next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower() == "from"), "")


IMAGE_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
             ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif"}
MIN_PHOTO_BYTES = 30 * 1024     # smaller images are signature logos, not photos
MAX_PHOTO_BYTES = 40 * 1024 * 1024


def _message_photos(service, msg):
    """Every photo attached to a message, original bytes, whatever its size.

    Gmail's shared helper drops images over 4 MB (Claude's limit for one image),
    and phone photos often weigh 8 to 12 MB: here they are kept, then shrunk."""
    found = []

    def walk(part):
        mime = (part.get("mimeType") or "").lower()
        ext = os.path.splitext((part.get("filename") or "").lower())[1]
        body = part.get("body") or {}
        if (mime.startswith("image/") or ext in IMAGE_EXT) and (body.get("attachmentId") or body.get("data")) \
                and MIN_PHOTO_BYTES <= int(body.get("size") or 0) <= MAX_PHOTO_BYTES:
            data = body.get("data") or service.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=body["attachmentId"]).execute().get("data", "")
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            found.append((raw, mime if mime.startswith("image/") else IMAGE_EXT.get(ext, "image/jpeg"),
                          part.get("filename") or "photo"))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(msg.get("payload") or {})
    return found


QUOTE_START = re.compile(r"^\s*(>|De ?:|From ?:|Envoyé ?:|Sent ?:|-{2,}\s*(Original|Message)|_{5,}|Le .+ a écrit|On .+ wrote|"
                         r"Envoyé à partir de|Envoyé de mon|Sent from my)", re.I)


def _own_words(body):
    """The customer's text without the quoted messages below it."""
    kept = []
    for line in (body or "").splitlines():
        if QUOTE_START.match(line):
            break
        kept.append(line)
    return "\n".join(kept).strip()


READ_PROMPT = """Un client de Max Sauveur (chaussures et ceintures pour homme) nous écrit au sujet de sa commande #{number}, qu'il dit {problem}.

Sa commande :
{lines}

Ses messages, du plus ancien au plus récent :
{messages}

Réponds uniquement avec ce JSON :
{{"received": "ce que le client dit avoir reçu à la place, en quelques mots et avec ses mots (exemple : Derby bordeaux en cuir grainé, en 40), ou null s'il ne le dit pas clairement",
 "line": numéro de la ligne de commande dont il parle (la paire commandée), ou null,
 "missing": [numéros des lignes qu'il dit ne pas avoir reçues],
 "quote": "la phrase exacte du client qui le dit, ou null"}}
N'invente rien : si ce n'est pas écrit clairement, mets null."""


def read_email(case, messages):
    """What the customer says he received (or misses), read in his own messages by Haiku."""
    data = case["data"]
    texts = [_own_words(gmail_helper.parse_email(m).get("body", "")) for m in messages]
    texts = [t for t in texts if t][-4:]
    if not texts:
        return {"received": None, "line": None, "missing": [], "quote": None}
    lines = (data.get("order") or {}).get("lines") or []
    prompt = READ_PROMPT.format(
        number=case["order_number"], problem=PROBLEMS[data["kind"]],
        lines="\n".join(f"{i}. {_line_label(l)}" for i, l in enumerate(lines)) or "(aucune ligne)",
        messages="\n\n---\n\n".join(t[:2500] for t in texts))
    import claude_ai
    text = claude_ai._call_haiku([{"role": "user", "content": prompt}], max_tokens=400)
    start = text.find("{")
    found, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else ({}, 0)
    line = found.get("line")
    return {"received": (found.get("received") or None) if isinstance(found.get("received"), str) else None,
            "line": line if isinstance(line, int) and 0 <= line < len(lines) else None,
            "missing": [i for i in found.get("missing") or [] if isinstance(i, int) and 0 <= i < len(lines)],
            "quote": found.get("quote") if isinstance(found.get("quote"), str) else None}


def refresh_from_thread(case, service=None, initial=False, full=False):
    """Read the customer's thread: new messages, their photos, what he says he received.

    A new customer message lights the red badge (reply_unread) unless the file is
    just being opened. Photos are scanned again from the start while the file has
    none (or on Samuel's request), so a photo missed once is not lost."""
    if not case.get("thread_id"):
        return case
    service = service or gmail_helper.get_gmail_service()
    data = case["data"]
    messages = _thread(service, case["thread_id"])
    seen = set(data.get("seen_messages") or [])
    customer = [m for m in messages if "maxsauveur.com" not in _sender(m).lower()]
    fresh = [m for m in customer if m["id"] not in seen]
    added, unreadable = 0, []
    # The whole thread is scanned once (and on Samuel's request), then only new messages.
    rescan = full or (not photo_names(case["id"]) and not data.get("photos_scanned"))
    for msg in (customer if rescan else fresh):
        for raw, mime, name in _message_photos(service, msg):
            try:
                if save_photo(case["id"], raw, mime):
                    added += 1
            except ValueError as e:
                print(f"[Erreurs] Photo {name} skipped for file {case['id']}: {e}")
                if "maximum" not in str(e):
                    unreadable.append(name)
    changes = {"seen_messages": [m["id"] for m in messages], "photos_scanned": True}
    if unreadable:
        changes["unreadable_photos"] = sorted(set((data.get("unreadable_photos") or []) + unreadable))
    if fresh and not initial:
        last = gmail_helper.parse_email(fresh[-1]).get("body", "")
        changes.update(reply=_own_words(last)[:1500] or last[:1500], reply_unread=True,
                       log=_log(data, "réponse du client" + (f" ({added} photo{'s' if added > 1 else ''})" if added else "")))
    elif added:
        changes["log"] = _log(data, f"{added} photo{'s' if added > 1 else ''} reprise{'s' if added > 1 else ''} du fil")
    if not data.get("confirmed_at") and (fresh or full or not data.get("email_read")):
        try:
            changes["email_read"] = read_email(case, customer)
        except Exception as e:
            # Kept as read, so a failing call is not paid again every 10 minutes.
            print(f"[Erreurs] Customer email unreadable for file {case['id']}: {e}")
            changes["email_read"] = {"received": None, "line": None, "missing": [], "quote": None, "failed": str(e)[:200]}
    case = database.update_logistics_error(case["id"], data=changes)
    if added and not case["data"].get("confirmed_at"):
        try:
            case = verify(case["id"])
        except Exception as e:
            print(f"[Erreurs] Photo check failed for file {case['id']}: {e}")
            case = database.update_logistics_error(case["id"], status="to_check", data={
                "alert": f"Lecture des photos impossible ({e}). Relance-la ou vérifie à l'œil."})
    return case


def create(kind, order_number, customer_name="", customer_email="", thread_id=None, subject=""):
    """Open a file from a customer's email (or by hand), with the Shopify order and the thread's photos.

    If the customer already sent photos, Claude reads them at once."""
    if kind not in KINDS:
        raise ValueError("Type d'erreur inconnu")
    number = re.sub(r"\D", "", str(order_number or ""))
    order = retours.fetch_order(number) if number else None
    if not order:
        raise ValueError(f"Commande #{number or '?'} introuvable dans Shopify")
    for case in database.get_logistics_errors():
        if case["order_number"] == number and case["status"] not in CLOSED and case["data"].get("kind") == kind:
            raise ValueError(f"Un dossier est déjà ouvert pour la commande #{number} (dossier {case['id']})")
    case_id = database.save_logistics_error(
        status="new", order_number=number,
        customer_name=re.sub(r"\s*<[^>]*>", "", customer_name or "").strip().strip('"') or order.get("full_name") or "",
        customer_email=customer_email or order.get("email") or "",
        thread_id=thread_id or None,
        data={"kind": kind, "order": order, "subject": subject or "", "log": _log({}, "dossier ouvert")})
    case = database.get_logistics_error(case_id)
    try:
        case = refresh_from_thread(case, initial=True)
    except Exception as e:
        print(f"[Erreurs] Thread unreadable for file {case_id}: {e}")
    return database.get_logistics_error(case_id)


# ---------------------------------------------------------------- reading the photos

VERIFY_PROMPT = """Un client de Max Sauveur (chaussures et ceintures pour homme) signale {problem}. Voici ses photos, puis sa commande.

Commande #{number} :
{lines}
{said}
Comment lire les photos :
- L'étiquette collée sur la boîte porte la désignation du produit (modèle, coloris), la pointure et un code-barres. Si plusieurs étiquettes sont superposées, seule celle du dessus compte.
- La photo de la paire montre le modèle et la couleur vraiment reçus.
{task}

Ne devine pas : si une information n'est pas lisible, dis-le.

Réponds uniquement avec ce JSON :
{{"box_label": {{"designation": "...", "size": "...", "ean": "..."}} ou null si aucune étiquette n'est lisible,
 "seen": "ce que montrent les photos, en une phrase",
 "verdict": "error" si le client n'a pas reçu ce qu'il a commandé, "no_error" si les photos correspondent à la commande, "unclear" si les photos ne permettent pas de trancher,
 "line": numéro de la ligne de commande concernée, ou null,
 "received": "ce que le client a reçu à la place, en quelques mots (exemple : Oxford cuir marron en 40)", ou null,
 "missing": [numéros des lignes absentes du colis],
 "explanation": "1 ou 2 phrases pour Samuel : ce qui correspond, ce qui diffère"}}"""

TASKS = {
    WRONG: "Compare la désignation et la pointure de l'étiquette, et la paire photographiée, avec les lignes de la commande. "
           "Un modèle, un coloris ou une pointure différents de la commande, c'est une erreur.",
    MISSING: "Repère quels articles de la commande se voient sur la photo du colis, et lesquels manquent.",
}
PROBLEMS = {WRONG: "avoir reçu une paire différente de sa commande", MISSING: "un colis incomplet"}


def _verify_prompt(case):
    data = case["data"]
    lines = "\n".join(f"{i}. {_line_label(l)} (quantité {l['quantity']})"
                      for i, l in enumerate((data.get("order") or {}).get("lines") or []))
    quote = (data.get("email_read") or {}).get("quote")
    said = f"\nCe que le client écrit : « {quote} »\n" if quote else ""
    return VERIFY_PROMPT.format(problem=PROBLEMS[data["kind"]], number=case["order_number"],
                                lines=lines or "(aucune ligne)", task=TASKS[data["kind"]], said=said)


def verify(case_id):
    """Claude compares the photos with the order; Samuel then confirms or not."""
    case = database.get_logistics_error(case_id)
    names = photo_names(case_id)
    if not names:
        raise ValueError("Aucune photo dans le dossier")
    content = []
    for n in names[-4:]:
        with open(os.path.join(_photo_dir(case_id), n), "rb") as f:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(f.read()).decode()}})
    content.append({"type": "text", "text": _verify_prompt(case)})
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024,
                                 messages=[{"role": "user", "content": content}])
    text = next(b.text for b in msg.content if b.type == "text")
    start = text.find("{")
    if start < 0:
        raise RuntimeError("Claude n'a pas su lire les photos.")
    verdict, _ = json.JSONDecoder().raw_decode(text[start:])
    if verdict.get("verdict") not in ("error", "no_error", "unclear"):
        verdict["verdict"] = "unclear"
    lines = (case["data"].get("order") or {}).get("lines") or []
    if not isinstance(verdict.get("line"), int) or not 0 <= verdict["line"] < len(lines):
        verdict["line"] = 0 if len(lines) == 1 else None
    verdict["missing"] = [i for i in verdict.get("missing") or [] if isinstance(i, int) and 0 <= i < len(lines)]
    verdict["photos"] = names[-4:]
    status = case["status"] if case["data"].get("confirmed_at") else "to_check"
    return database.update_logistics_error(case_id, status=status, data={
        "verdict": verdict, "alert": None,
        "log": _log(case["data"], f"photos lues par Claude : {verdict.get('explanation') or verdict['verdict']}")})


# ---------------------------------------------------------------- Samuel's decision

def confirm(case_id, lines, ordered_text="", received_text="", voluntary=False):
    """Samuel confirms the error. Wrong pair: the return label is created (billed).
    Missing item: the reshipment is created on the main Wing account, same relay or address."""
    case = database.get_logistics_error(case_id)
    data = case["data"]
    if data.get("confirmed_at"):
        raise ValueError("Erreur déjà confirmée")
    chosen = [i for i in lines if isinstance(i, int)]
    picked = _lines(case, chosen)
    if not picked:
        raise ValueError("Coche l'article concerné")
    if data["kind"] == WRONG and len(picked) != 1:
        raise ValueError("Choisis une seule paire commandée")
    if data["kind"] == WRONG and not (received_text or "").strip():
        raise ValueError("Indique ce que le client a reçu")
    ordered = (ordered_text or "").strip() or ", ".join(human_label(l) for l in picked)
    case = database.update_logistics_error(case_id, data={
        "confirmed_at": now_iso(), "lines": chosen, "ordered_text": ordered,
        "received_text": (received_text or "").strip(), "voluntary": bool(voluntary),
        "outcome": "missing" if data["kind"] == MISSING else "reship",
        "log": _log(data, "erreur confirmée par Samuel" + (" (envoi volontaire)" if voluntary else ""))})
    return create_label(case_id) if data["kind"] == WRONG else reship_same_place(case_id)


def not_an_error(case_id, note=""):
    case = database.get_logistics_error(case_id)
    return database.update_logistics_error(case_id, status="not_error", data={
        "closed_at": now_iso(), "alert": None,
        "log": _log(case["data"], "pas une erreur de SSL" + (f" : {note}" if note else ""))})


def create_label(case_id):
    """Return label on the second Wing account, named #<order>_ERREUR. Every call is billed."""
    case = database.get_logistics_error(case_id)
    data = case["data"]
    if data.get("label"):
        raise ValueError(f"L'étiquette {data['label']['ref']} existe déjà")
    line = (_lines(case, data.get("lines") or []) or [{}])[0]
    product = _line_label(line) if line else ""
    weight = repair_catalog.PARCEL_TYPES[repair_catalog.parcel_type(product)]["weight"]
    try:
        label = wing_repair.create_repair_label(
            case["order_number"], weight_kg=weight,
            designation=f"{repair_catalog.model_name(product) or 'Paire'} erreur preparation",
            suffixes=wing_repair.ERROR_SUFFIXES)
    except Exception as e:
        print(f"[Erreurs] Label failed for file {case_id}: {e}")
        return database.update_logistics_error(case_id, data={
            "alert": f"Étiquette non créée ({e}). Vérifie dans Wing (compte réparation) avant de réessayer.",
            "log": _log(data, f"étiquette refusée : {e}")})
    return database.update_logistics_error(case_id, status="label_ready", data={
        "label": label, "alert": None, "log": _log(data, f"étiquette retour créée : {label['ref']}")})


def mark_received(case_id, by="Samuel"):
    """The wrong pair is back at SSL: check the stock of the right one."""
    case = database.get_logistics_error(case_id)
    if case["status"] not in ("label_ready", "awaiting_return"):
        raise ValueError("Ce dossier n'attend pas de retour")
    return database.update_logistics_error(case_id, status="received", data={
        "received_at": now_iso(), "return_token": case["data"].get("return_token") or secrets.token_urlsafe(16),
        "stock": stock_of(case), "mode": original_mode(case),
        "log": _log(case["data"], f"paire reçue chez SSL ({by})")})


def choose_refund(case_id):
    """Samuel decides to refund instead of sending the right pair (12 € owed by SSL)."""
    case = database.get_logistics_error(case_id)
    if not case["data"].get("confirmed_at") or case["data"]["kind"] != WRONG:
        raise ValueError("Seulement pour une mauvaise paire confirmée")
    if case["data"].get("reship"):
        raise ValueError(f"Le renvoi {case['data']['reship']['ref']} existe déjà dans Wing")
    order = retours.fetch_order(case["order_number"]) or {}
    return database.update_logistics_error(case_id, status="to_refund", data={
        "outcome": "refund", "refunded_before": order.get("refunded", 0),
        "log": _log(case["data"], "Samuel choisit le remboursement")})


def mark_refunded(case_id):
    case = database.get_logistics_error(case_id)
    return database.update_logistics_error(case_id, status="done", data={
        "closed_at": now_iso(), "log": _log(case["data"], "remboursé")})


def cancel(case_id):
    case = database.get_logistics_error(case_id)
    return database.update_logistics_error(case_id, status="cancelled", data={
        "closed_at": now_iso(), "alert": None, "log": _log(case["data"], "dossier annulé")})


def delete(case_id):
    folder = _photo_dir(case_id)
    for n in os.listdir(folder):
        os.remove(os.path.join(folder, n))
    os.rmdir(folder)
    database.delete_logistics_error(case_id)


def set_flag(case_id, key, value):
    """Samuel's switches on a file: voluntary (not billed to SSL), deducted from SSL's invoice."""
    case = database.get_logistics_error(case_id)
    if key == "voluntary":
        return database.update_logistics_error(case_id, data={
            "voluntary": bool(value), "log": _log(case["data"], "envoi volontaire" if value else "erreur de SSL")})
    if key == "deducted":
        return database.update_logistics_error(case_id, data={
            "deducted_at": now_iso() if value else None,
            "log": _log(case["data"], "déduit de la facture SSL" if value else "déduction annulée")})
    if key == "reply_read":
        return database.update_logistics_error(case_id, data={"reply_unread": False})
    if key == "alert_read":
        return database.update_logistics_error(case_id, data={"alert": None})
    raise ValueError("Réglage inconnu")


# ---------------------------------------------------------------- stock and Wing orders

def variants_of(case):
    """Every size of the pair to send back, with its stock and barcode, from Shopify."""
    lines = _lines(case, case["data"].get("lines") or [])
    if not lines or not lines[0].get("product_id"):
        return []
    try:
        product = retours.avoirs._gql(
            """query($id: ID!) { product(id: $id) { title variants(first: 60) {
                 nodes { id title barcode inventoryQuantity } } } }""",
            {"id": lines[0]["product_id"]})["product"]
    except Exception as e:
        print(f"[Erreurs] Sizes unavailable for #{case['order_number']}: {e}")
        return []
    return [{"id": v["id"].rsplit("/", 1)[-1], "size": v["title"], "barcode": v.get("barcode"),
             "qty": v.get("inventoryQuantity"), "title": product["title"]} for v in product["variants"]["nodes"]]


def reship_text(case):
    """The pair that goes back to the customer, in words: the ordered one unless Samuel changed the size."""
    data = case["data"]
    item = data.get("reship_item")
    if item:
        return human_label({"title": item["title"], "variant": item["size"]})
    return data.get("ordered_text") or ", ".join(human_label(l) for l in _lines(case, data.get("lines") or []))


def set_reship_item(case_id, variant_id):
    """Samuel sends another size than the one ordered (the customer asked for it)."""
    case = database.get_logistics_error(case_id)
    data = case["data"]
    if data["kind"] != WRONG or not data.get("confirmed_at"):
        raise ValueError("Seulement pour une mauvaise paire confirmée")
    if data.get("reship"):
        raise ValueError(f"Le renvoi {data['reship']['ref']} existe déjà dans Wing : change la pointure là-bas")
    variant = next((v for v in variants_of(case) if v["id"] == str(variant_id)), None)
    if not variant:
        raise ValueError("Pointure introuvable dans Shopify")
    ordered = (_lines(case, data.get("lines") or []) or [{}])[0]
    if ordered.get("variant") == variant["size"]:
        item = None
        text = "paire à renvoyer : celle de la commande"
    else:
        item = {"variant_id": variant["id"], "title": variant["title"], "size": variant["size"],
                "designation": f"{variant['title']} - {variant['size']}", "barcode": variant.get("barcode")}
        text = f"paire à renvoyer : {variant['title']} en {variant['size']} (au lieu de {ordered.get('variant')})"
    return database.update_logistics_error(case_id, data={
        "reship_item": item, "stock": {"qty": variant["qty"], "checked_at": now_iso()}, "log": _log(data, text)})


def stock_of(case):
    """Units in stock of the right item, or None when Shopify does not say."""
    lines = _lines(case, case["data"].get("lines") or [])
    if not lines:
        return None
    item = case["data"].get("reship_item")
    if item:
        v = next((v for v in variants_of(case) if v["id"] == item["variant_id"]), None)
        return {"qty": v["qty"], "checked_at": now_iso()} if v and v["qty"] is not None else None
    try:
        node = retours.avoirs._gql(
            """query($q: String!) { orders(first: 1, query: $q) { nodes { name
                 lineItems(first: 50) { nodes { id variant { inventoryQuantity } } } } } }""",
            {"q": f"name:#{case['order_number']}"})["orders"]["nodes"][0]
        by_id = {i["id"].rsplit("/", 1)[-1]: (i.get("variant") or {}).get("inventoryQuantity")
                 for i in node["lineItems"]["nodes"]}
        qty = by_id.get(lines[0]["id"])
        return {"qty": qty, "checked_at": now_iso()} if qty is not None else None
    except Exception as e:
        print(f"[Erreurs] Stock unavailable for #{case['order_number']}: {e}")
        return None


def _original(token, case):
    order = wing_repair._find_order(token, f"#{case['order_number']}")
    if not order:
        raise RuntimeError(f"Commande #{case['order_number']} introuvable sur le compte Wing principal")
    return order


def _products(original, lines):
    """The Wing products of these order lines (designation, price, weight, EAN), as SSL knows them."""
    by_name = {op["product"]["designation"].lower(): op for op in original["orderProducts"]}
    products = []
    for line in lines:
        label = _line_label(line)
        op = by_name.get(label.lower())
        if op:
            p = op["product"]
            products.append({"designation": p["designation"], "price": p["price"], "weight": p["weight"],
                             "quantity": line["quantity"], **({"ean": p["ean"]} if p.get("ean") else {})})
        else:
            weight = repair_catalog.PARCEL_TYPES[repair_catalog.parcel_type(label)]["weight"]
            products.append({"designation": label, "price": line.get("paid") or line.get("catalogue") or 100,
                             "weight": weight, "quantity": line["quantity"]})
    return products


def _reship_products(original, case):
    """What goes in the reshipment: the ordered line, or the size Samuel chose instead."""
    products = _products(original, _lines(case, case["data"].get("lines") or []))
    item = case["data"].get("reship_item")
    if item and products:
        products[0]["designation"] = item["designation"]
        products[0].pop("ean", None)
        if item.get("barcode"):
            products[0]["ean"] = item["barcode"]
    return products


def _create_reship(token, case, recipient, fulfillment):
    base = f"#{case['order_number']}"
    ref = next((base + s for s in RESHIP_SUFFIXES
                if not wing_repair._find_order(token, base + s) and not wing_repair._find_order(token, (base + s).lstrip("#"))), None)
    if not ref:
        raise RuntimeError(f"Trop de renvois déjà créés pour {base}")
    result = wing_repair._gql("""
    mutation($input: OrderToCreateInput!) { createOrder(input: $input) { id ref } }
    """, {"input": {"ref": ref, "orderedAt": datetime.now(timezone.utc).isoformat(), "recipient": recipient,
                    "fulfillmentOrders": [fulfillment]}}, token)
    if "errors" in result:
        raise RuntimeError(f"Wing a refusé la commande {ref} : {result['errors'][0].get('message', result['errors'])}")
    print(f"[Erreurs] Wing order {ref} created for file {case['id']}")
    return ref


def _notify_samuel(case, ref, where):
    body = f"""{ref} · {case.get('customer_name') or ''}
{KINDS[case['data']['kind']]} · commande #{case['order_number']}
{case['data'].get('ordered_text') or ''}

Livraison : {where}

À ajouter à une collecte dans Wing (compte principal) pour que SSL l'expédie.

{BASE_URL}/erreurs?id={case['id']}"""
    try:
        gmail_helper.send_email(gmail_helper.get_gmail_service(), SAMUEL_EMAIL, f"[Erreur prépa] Renvoi créé {ref}", body)
    except Exception as e:
        print(f"[Erreurs] Notification failed: {e}")


def reship_same_place(case_id):
    """Missing item: a new order on the main Wing account, same recipient, same service and relay."""
    case = database.get_logistics_error(case_id)
    data = case["data"]
    if data.get("reship"):
        raise ValueError(f"Le renvoi {data['reship']['ref']} existe déjà")
    try:
        token = wing_repair._get_token("main")
        original = _original(token, case)
        r = original["recipient"]
        recipient = {k: r[k] for k in ("firstName", "lastName", "company", "phone", "email", "line1", "line2",
                                       "additional", "city", "zip", "countryCode") if r.get(k)}
        fo = (original.get("fulfillmentOrders") or [{}])[0]
        service = fo.get("service") or original.get("service")
        fulfillment = {"service": service, "products": _products(original, _lines(case, data.get("lines") or []))}
        if fo.get("carrierPickupId"):
            fulfillment.update(carrierPickupId=fo["carrierPickupId"], carrierPickupName=fo.get("carrierPickupName") or "")
        ref = _create_reship(token, case, recipient, fulfillment)
    except Exception as e:
        print(f"[Erreurs] Reship failed for file {case_id}: {e}")
        return database.update_logistics_error(case_id, data={
            "alert": f"Renvoi non créé ({e}). Vérifie dans Wing (compte principal) avant de réessayer.",
            "log": _log(data, f"renvoi refusé : {e}")})
    where = fo.get("carrierPickupName") or f"{r.get('line1', '')}, {r.get('zip', '')} {r.get('city', '')}"
    case = database.update_logistics_error(case_id, status="reship_created", data={
        "reship": {"ref": ref, "where": where, "relay": bool(fo.get("carrierPickupId")), "created_at": now_iso()},
        "alert": None, "log": _log(data, f"renvoi créé dans Wing : {ref} ({where})")})
    _notify_samuel(case, ref, where)
    return case


def original_mode(case):
    """'chrono_relay' if the order went to a relay, else 'home' (what the customer page preselects)."""
    try:
        fo = (_original(wing_repair._get_token("main"), case).get("fulfillmentOrders") or [{}])[0]
        return "chrono_relay" if fo.get("carrierPickupId") else "home"
    except Exception as e:
        print(f"[Erreurs] Original order unreadable for #{case['order_number']}: {e}")
        return "chrono_relay"


# ---------------------------------------------------------------- the customer page (/erreur/<token>)

PAGE_OPTIONS = {k: returns.DELIVERY_OPTIONS[k] for k in ("chrono_relay", "home")}


def by_token(token):
    for case in database.get_logistics_errors():
        if case["data"].get("return_token") == token and case["status"] not in ("cancelled", "not_error"):
            return case
    return None


def prefill(case):
    delivery = returns.prefill({"order_number": case["order_number"]})
    delivery["email"] = delivery.get("email") or case.get("customer_email") or ""
    return delivery


def confirm_address(case, d):
    """The customer picked a relay or confirmed home: the right pair is ordered on the main Wing account."""
    if case["status"] != "waiting_address" and case["status"] != "received":
        raise ValueError("Ce lien n'attend plus de réponse. Écrivez-nous pour toute modification.")
    if d.get("option") not in PAGE_OPTIONS:
        raise ValueError("Choisissez un mode de livraison")
    missing = [label for key, label in returns.REQUIRED.items() if not (d.get(key) or "").strip()]
    if PAGE_OPTIONS[d["option"]]["relay"] and not d.get("relay_id"):
        missing.append("point relais")
    if missing:
        raise ValueError("Merci de compléter : " + ", ".join(missing))
    d = {k: (v.strip() if isinstance(v, str) else v) for k, v in d.items()}
    data = case["data"]
    option = PAGE_OPTIONS[d["option"]]
    recipient = {k: v for k, v in {
        "firstName": d["first_name"], "lastName": d["last_name"], "company": d.get("company"),
        "phone": returns._phone(d["phone"], d["country_code"]), "email": d["email"],
        "line1": d["line1"], "line2": d.get("line2"), "city": d["city"], "zip": d["zip"],
        "countryCode": d["country_code"],
    }.items() if v}
    where = d.get("relay_name") or f"{d['line1']}, {d['zip']} {d['city']}"
    fields = {"delivery": d, "address_confirmed_at": now_iso()}
    try:
        token = wing_repair._get_token("main")
        fulfillment = {"service": option["service"], "products": _reship_products(_original(token, case), case)}
        if option["relay"]:
            fulfillment.update(carrierPickupId=d["relay_id"], carrierPickupName=d["relay_name"])
        ref = _create_reship(token, case, recipient, fulfillment)
        fields.update(reship={"ref": ref, "where": where, "relay": bool(option["relay"]), "created_at": now_iso()},
                      log=_log(data, f"le client confirme : {where} · renvoi créé dans Wing : {ref}"))
        case = database.update_logistics_error(case["id"], status="reship_created", data=fields)
        _notify_samuel(case, ref, where)
    except Exception as e:
        # The choice is kept: Samuel creates the order by hand, the customer still sees a confirmation.
        print(f"[Erreurs] Reship failed for file {case['id']}: {e}")
        fields.update(alert=f"Le client a confirmé ({where}) mais le renvoi n'a pas pu être créé dans Wing ({e}). "
                            "À créer à la main sur le compte principal.",
                      log=_log(data, f"le client confirme : {where} · renvoi refusé par Wing : {e}"))
        case = database.update_logistics_error(case["id"], data=fields)
    return case


# ---------------------------------------------------------------- customer emails

def proposed_mail(case):
    """The email the file is waiting for, or None."""
    status, data = case["status"], case["data"]
    sent = data.get("sent_mails") or {}
    key = {"new": "ask_photos", "label_ready": "label", "received": "address",
           "to_refund": "refund", "not_error": "not_error", "shipped": "tracking"}.get(status)
    if status == "reship_created" and data["kind"] == MISSING:
        key = "missing"
    if status == "received" and (data.get("stock") or {}).get("qty") is not None and data["stock"]["qty"] <= 0:
        return None     # right pair out of stock: Samuel decides
    return key if key and key not in sent else None


def draft(case, template):
    """(subject, body) of a prepared email; Samuel can rewrite it before sending."""
    data = case["data"]
    name = _first_name(case)
    number = f"#{case['order_number']}"
    ordered = data.get("ordered_text") or ", ".join(human_label(l) for l in (data.get("order") or {}).get("lines") or [])
    thanks = ("Merci pour les photos." if len(photo_names(case["id"])) > 1 else "Merci pour la photo.") \
        if photo_names(case["id"]) else "Merci pour votre message."
    received = data.get("received_text") or ""
    reship = data.get("reship") or {}
    label = data.get("label") or {}
    if template == "ask_photos" and data["kind"] == WRONG:
        body = f"""Merci pour votre message.

Pour vérifier ce qui s'est passé à la préparation de votre commande {number}, pouvez-vous nous envoyer deux photos en réponse à ce mail :
1. l'étiquette collée sur la boîte (avec le nom du modèle, la pointure et le code-barres) ;
2. la paire reçue.

Dès réception, nous revenons vers vous très rapidement."""
    elif template == "ask_photos":
        body = f"""Merci pour votre message.

Pour vérifier ce qui s'est passé à la préparation de votre commande {number}, pouvez-vous nous envoyer en réponse à ce mail une photo de ce que contenait votre colis ?

Dès réception, nous revenons vers vous très rapidement."""
    elif template == "label":
        body = f"""{thanks} Vous avez bien reçu {received} au lieu de {ordered} : l'erreur vient de notre préparation, et nous nous en excusons.

Voici votre étiquette de retour prépayée :
{label.get('label_url') or '[lien de l étiquette]'}

Il vous suffit de l'imprimer, de la coller sur le colis avec la paire dans sa boîte, puis de le déposer dans un bureau de poste ou un point Colissimo.

Dès que la paire arrive chez nous, nous vous envoyons la bonne. Vous n'avez rien à payer."""
    elif template == "address":
        link = (f"{BASE_URL}/erreur/{data['return_token']}" if data.get("received_at") and data.get("return_token")
                else "[lien : marque d'abord la paire comme reçue chez SSL]")
        if (data.get("mode") or "chrono_relay") == "chrono_relay":
            ask = "choisir le point relais Chronopost (Shop2Shop) où vous souhaitez la recevoir"
        else:
            ask = "confirmer votre adresse de livraison"
        body = f"""Votre colis retour est bien arrivé chez nous, merci.

La bonne paire, {reship_text(case)}, est prête à partir. Pour la recevoir, il vous reste à {ask} sur cette page :
{link}

Elle partira dès votre confirmation."""
    elif template == "missing":
        where = (f"au même point relais ({reship.get('where')})" if reship.get("relay")
                 else "à la même adresse")
        body = f"""{thanks} Il manquait bien {ordered} dans votre colis : l'erreur vient de notre préparation, et nous nous en excusons.

Nous vous l'envoyons dans les prochains jours, {where}. Vous recevrez le numéro de suivi par mail dès son départ."""
    elif template == "tracking":
        tracking = data.get("reship_tracking") or {}
        what = "Votre article est parti" if data["kind"] == MISSING else "Votre paire est partie"
        body = f"""{what} : {reship_text(case) if data["kind"] == WRONG else ordered}.

Vous pouvez suivre le colis ici :
{tracking.get('url') or tracking.get('id') or '[numéro de suivi]'}"""
    elif template == "refund":
        body = f"""Votre colis retour est bien arrivé chez nous, merci.

Comme convenu, nous procédons au remboursement de {ordered}. Il apparaîtra sur votre moyen de paiement sous quelques jours."""
    elif template == "free":
        body = "\n"
    elif template == "not_error":
        body = f"""{thanks}

La boîte et la paire correspondent bien à votre commande {number} : {ordered}.

Si la paire ne vous convient pas, vous pouvez nous la retourner en suivant la procédure indiquée ici :
https://maxsauveur.com/pages/livraison-et-retour"""
    else:
        raise ValueError("Mail inconnu")
    subject = data.get("subject") or f"Votre commande {number}"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return subject, f"Bonjour {name},\n\n{body}\n\nJohn de Max Sauveur"


REPLY_PROMPT = """Tu prépares la réponse de John (service client Max Sauveur) au dernier message d'un client qui a un dossier « erreur de préparation » ouvert chez nous.

DOSSIER
{facts}

COMMANDE #{number}
{lines}

POINTURES DU MODÈLE À RENVOYER (stock Shopify en ce moment)
{sizes}

ÉCHANGES DANS CE FIL, du plus ancien au plus récent
{thread}

CONSIGNE DE SAMUEL (le responsable)
{consigne}

RÈGLES
1. Réponds à ce que demande le client dans son dernier message, en t'appuyant uniquement sur le dossier, la commande, le stock et la consigne.
2. La consigne de Samuel est sa décision : applique-la telle quelle. N'ajoute aucun geste, aucune option, aucune promesse qu'il n'a pas écrits.
3. Si le client demande ce que seul Samuel peut décider (une autre pointure ou un autre modèle, un remboursement, un geste, un délai) et que la consigne ne le tranche pas, ne réponds pas sur le fond : écris une réponse courte qui dit qu'on regarde et qu'on revient vers lui très rapidement. Dans note_for_samuel, donne seulement la décision à prendre et les faits utiles (le stock de la pointure demandée et des pointures voisines). Ne lui propose aucune solution ni aucun geste (avoir, remboursement, réduction, autre modèle) : c'est lui qui décide.
4. Ne cite jamais une pointure, un délai ou une disponibilité absents de ce qui précède. Une pointure à 0 en stock n'est pas disponible. Ne promets rien que la consigne ne dit pas : ni réserver ou mettre de côté une paire, ni un délai, ni une date.
5. Les excuses ont déjà été faites dans le fil : ne t'excuse pas à nouveau. Pas de tiret long, ni dans le mail ni dans la note. Vouvoiement, sauf si le client tutoie. Signe « John de Max Sauveur ».
6. Si ta réponse annonce l'envoi d'une autre pointure que la paire à renvoyer du dossier, rappelle à Samuel dans note_for_samuel de la changer dans le dossier avant le renvoi.

Réponds uniquement avec ce JSON :
{{"note_for_samuel": "1 ou 2 phrases pour Samuel", "draft": "le mail complet, de « Bonjour » à la signature"}}"""


def _facts(case):
    data = case["data"]
    facts = [f"Type : {KINDS[data['kind']]}", f"Étape : {STATUS_LABELS[case['status']]}"]
    if data.get("confirmed_at"):
        if data["kind"] == WRONG:
            facts.append(f"Erreur confirmée : reçu {data.get('received_text') or '?'} au lieu de {data.get('ordered_text') or '?'}")
            facts.append(f"Paire à renvoyer : {reship_text(case)}")
        else:
            facts.append(f"Erreur confirmée : article manquant, {data.get('ordered_text') or '?'}")
    if data.get("label"):
        facts.append(f"Étiquette retour envoyée au client : {data['label']['ref']} (Colissimo)")
    facts.append("Paire retournée reçue chez nous : " + ("oui" if data.get("received_at") else "pas encore"))
    if data.get("reship"):
        facts.append(f"Renvoi créé : {data['reship']['ref']} vers {data['reship']['where']}")
    if data.get("reship_tracking"):
        facts.append(f"Suivi du renvoi : {data['reship_tracking'].get('url') or data['reship_tracking'].get('id')}")
    if data.get("outcome") == "refund":
        facts.append("Samuel a choisi de rembourser le client.")
    return "\n".join(facts)


def draft_reply(case_id, consigne=""):
    """Claude drafts the answer to the customer's last message, from the file, the thread and Samuel's words."""
    import claude_ai
    case = database.get_logistics_error(case_id)
    data = case["data"]
    thread = []
    if case.get("thread_id"):
        for m in _thread(gmail_helper.get_gmail_service(), case["thread_id"])[-8:]:
            who = "Nous" if "maxsauveur.com" in _sender(m).lower() else "Client"
            text = _own_words(gmail_helper.parse_email(m).get("body", ""))
            if text:
                thread.append(f"[{who}]\n{text[:2000]}")
    if not thread and data.get("reply"):
        thread.append(f"[Client]\n{data['reply']}")
    sizes = [f"{v['size']} : {v['qty'] if v['qty'] is not None else '?'} en stock" for v in variants_of(case)] \
        if data["kind"] == WRONG else []
    prompt = REPLY_PROMPT.format(
        facts=_facts(case), number=case["order_number"],
        lines="\n".join(f"- {_line_label(l)}" for l in (data.get("order") or {}).get("lines") or []) or "(aucune)",
        sizes="\n".join(sizes) or "(non disponible)", thread="\n\n".join(thread) or "(aucun message)",
        consigne=(consigne or "").strip() or "(aucune : si la demande demande une décision, applique la règle 3)")
    system = claude_ai.get_system_prompt(email_context=f"{data.get('subject') or ''} {thread[-1] if thread else ''}")
    text = claude_ai._call_claude(system=system, messages=[{"role": "user", "content": prompt}], max_tokens=1500)
    start = text.find("{")
    try:
        result, _ = json.JSONDecoder().raw_decode(text[start:])
    except Exception:
        result = {"note_for_samuel": "", "draft": text.strip()}
    return {"note": (result.get("note_for_samuel") or "").strip(), "draft": (result.get("draft") or "").strip()}


def send(case_id, template, subject, body):
    """Send the email Samuel validated (from hello@, in the customer's thread), then move the file on."""
    case = database.get_logistics_error(case_id)
    if template not in MAILS:
        raise ValueError("Mail inconnu")
    if not case.get("customer_email"):
        raise ValueError("Adresse du client inconnue")
    if not (body or "").strip():
        raise ValueError("Le mail est vide")
    if template == "free":
        words = re.sub(r"(?i)^\s*bonjour[^\n]*|john de max sauveur\s*$", "", body.strip()).strip()
        if not words:
            raise ValueError("Écris ta réponse avant d'envoyer")
    if re.search(r"\[(lien|numéro)[^\]]*\]", body):
        raise ValueError("Le mail contient encore un champ à remplir entre crochets")
    service = gmail_helper.get_gmail_service()
    sent = gmail_helper.send_email(service, case["customer_email"], subject, body, thread_id=case.get("thread_id"))
    thread_id = (sent or {}).get("threadId") or case.get("thread_id")
    database.log_sent_email(to_email=case["customer_email"], subject=subject, body=body, source="erreur",
                            thread_id=thread_id)
    data = case["data"]
    seen = set(data.get("seen_messages") or []) | ({sent["id"]} if sent and sent.get("id") else set())
    if thread_id:
        try:
            seen |= {m["id"] for m in _thread(service, thread_id)}
        except Exception as e:
            print(f"[Erreurs] Thread {thread_id} unreadable after sending: {e}")
    status = AFTER_SEND.get(template, case["status"])
    return database.update_logistics_error(case_id, status=status, thread_id=thread_id, data={
        "sent_mails": {**(data.get("sent_mails") or {}), template: now_iso()}, "seen_messages": list(seen),
        "reply_unread": False,
        "log": _log(data, f"mail envoyé : {MAILS[template]}")})


def skip_mail(case_id, template):
    """Samuel does not want this email: the file moves on as if it had been sent."""
    case = database.get_logistics_error(case_id)
    data = case["data"]
    return database.update_logistics_error(case_id, status=AFTER_SEND.get(template, case["status"]), data={
        "sent_mails": {**(data.get("sent_mails") or {}), template: "skipped"},
        "log": _log(data, f"mail non envoyé : {MAILS.get(template, template)}")})


# ---------------------------------------------------------------- hourly job

def _parcels(order):
    return [p for fo in (order or {}).get("fulfillmentOrders", []) for p in fo["parcels"]]


def _check_return_label(token, case):
    order = wing_repair._find_order(token, case["data"]["label"]["ref"])
    parcels = _parcels(order)
    status = parcels[-1]["status"] if parcels else "DELETED"
    if status == "DELIVERED":
        mark_received(case["id"], by="suivi Wing")
    elif status == "DELETED" and not case["data"].get("label_deleted"):
        database.update_logistics_error(case["id"], data={
            "label_deleted": True, "alert": "L'étiquette retour a été annulée dans Wing : plus rien n'arrivera par elle."})


def _check_reship(token, case):
    order = wing_repair._find_order(token, case["data"]["reship"]["ref"])
    parcels = [p for p in _parcels(order) if p.get("trackingId")]
    if not parcels:
        return
    p = parcels[-1]
    data = case["data"]
    fields = {"reship_tracking": {"id": p["trackingId"], "url": p.get("trackingURL"), "status": p["status"]}}
    if p["status"] == "DELIVERED":
        database.update_logistics_error(case["id"], status="done", data={
            **fields, "closed_at": now_iso(), "log": _log(data, "renvoi livré au client")})
    elif p["status"] in ("SHIPPED", "DISPATCHED") and case["status"] == "reship_created":
        database.update_logistics_error(case["id"], status="shipped", data={
            **fields, "log": _log(data, f"renvoi expédié : {p['trackingId']}")})


def _check_refund(case):
    order = retours.fetch_order(case["order_number"]) or {}
    if order.get("refunded", 0) > (case["data"].get("refunded_before") or 0):
        database.update_logistics_error(case["id"], status="done", data={
            "closed_at": now_iso(), "log": _log(case["data"], "remboursement vu dans Shopify")})


def check_replies():
    """Scheduler job, every 10 minutes: customer replies and their photos (the red badge)."""
    service = None
    for case in database.get_logistics_errors():
        if case["status"] in CLOSED or not case.get("thread_id"):
            continue
        try:
            service = service or gmail_helper.get_gmail_service()
            refresh_from_thread(case, service)
        except Exception as e:
            print(f"[Erreurs] Reply check failed for file {case['id']}: {e}")


def unread_count():
    """Open files where the customer wrote and Samuel has not read it yet (the red badge)."""
    return sum(1 for c in database.get_logistics_errors()
               if c["status"] not in CLOSED and c["data"].get("reply_unread"))


def open_threads():
    """Gmail threads of open files: their new messages are handled here, not in 📬 Emails."""
    return {c["thread_id"] for c in database.get_logistics_errors() if c["status"] not in CLOSED and c.get("thread_id")}


def tick():
    """Scheduler job, every hour: customer replies, return labels at SSL, reshipments, refunds."""
    cases = [c for c in database.get_logistics_errors() if c["status"] not in CLOSED]
    if not cases:
        return 0
    service, rep, main = None, None, None
    for case in cases:
        try:
            if case.get("thread_id"):
                service = service or gmail_helper.get_gmail_service()
                case = refresh_from_thread(case, service)
            if case["status"] == "awaiting_return" and case["data"].get("label"):
                rep = rep or wing_repair._get_token("rep")
                _check_return_label(rep, case)
            elif case["status"] in ("reship_created", "shipped") and case["data"].get("reship"):
                main = main or wing_repair._get_token("main")
                _check_reship(main, case)
            elif case["status"] == "to_refund":
                _check_refund(case)
        except Exception as e:
            print(f"[Erreurs] Check failed for file {case['id']}: {e}")
    return len(cases)


# ---------------------------------------------------------------- what SSL owes

def cost(case):
    """What SSL owes for this file, 0 for a voluntary shipment, None when nothing is owed."""
    data = case["data"]
    if not data.get("confirmed_at") or case["status"] in ("not_error", "cancelled"):
        return None
    if data.get("voluntary"):
        return 0.0
    if data["kind"] == MISSING:
        return COST_MISSING
    return COST_REFUND if data.get("outcome") == "refund" else COST_RESHIP


def billing(case):
    data = case["data"]
    if data.get("voluntary"):
        return "voluntary"
    if data.get("deducted_at"):
        return "deducted"
    if data.get("claimed_at"):
        return "claimed"
    return "to_claim"


BILLING_LABELS = {"voluntary": "Volontaire, non facturé", "to_claim": "À réclamer",
                  "claimed": "Réclamé à SSL", "deducted": "Déduit de la facture"}


def detail(case):
    data = case["data"]
    if data["kind"] == MISSING:
        return f"{data.get('ordered_text') or 'article'} manquant"
    text = f"{data.get('received_text') or '?'} envoyé au lieu de {data.get('ordered_text') or '?'}"
    return text + (", client remboursé" if data.get("outcome") == "refund" else ", bonne paire renvoyée")


def ledger(month=None):
    """Confirmed errors of a month ('2026-11'), oldest first, with what SSL owes."""
    rows = []
    for case in database.get_logistics_errors():
        amount = cost(case)
        if amount is None:
            continue
        if month and case["data"]["confirmed_at"][:7] != month:
            continue
        rows.append({"case": case, "date": case["data"]["confirmed_at"][:10], "amount": amount,
                     "billing": billing(case), "detail": detail(case), "kind": KINDS[case["data"]["kind"]]})
    rows.sort(key=lambda r: r["date"])
    return rows


def months():
    """Months that have confirmed errors, newest first, plus the current one."""
    found = {c["data"]["confirmed_at"][:7] for c in database.get_logistics_errors() if cost(c) is not None}
    found.add(datetime.now(PARIS).strftime("%Y-%m"))
    return sorted(found, reverse=True)


_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre")


def month_label(month):
    year, m = month.split("-")
    return f"{_MONTHS[int(m) - 1]} {year}"


def ssl_mail(month):
    """(subject, body, ids) of the claim for the month's errors not claimed yet."""
    rows = [r for r in ledger(month) if r["billing"] == "to_claim" and r["amount"] > 0]
    total = sum(r["amount"] for r in rows)
    listing = "\n".join(f"- {short_date(r['date'])} · #{r['case']['order_number']} · "
                        f"{r['case'].get('customer_name') or ''} · {r['detail']} : {euros(r['amount'])}"
                        for r in rows)
    subject = f"Max Sauveur · Erreurs de préparation {month_label(month)} · {euros(total)}"
    body = f"""Bonjour,

Voici les erreurs de préparation de {month_label(month)}, à déduire de votre prochaine facture :

{listing or '(aucune)'}

Total : {euros(total)}

Merci de me confirmer la déduction.

Bonne journée,
Samuel Sfez
Max Sauveur"""
    return subject, body, [r["case"]["id"] for r in rows]


def send_ssl_mail(month, subject, body, ids):
    """Send the claim from samuel@ to SSL, then mark those errors as claimed."""
    if not ids:
        raise ValueError("Aucune erreur à réclamer ce mois-ci")
    service = gmail_helper.get_samuel_service()
    if not service:
        raise RuntimeError("La boîte samuel@ n'est pas connectée")
    gmail_helper.send_email(service, SSL_EMAIL, subject, body)
    for case_id in ids:
        case = database.get_logistics_error(case_id)
        if case and billing(case) == "to_claim":
            database.update_logistics_error(case_id, data={
                "claimed_at": now_iso(), "claim_month": month,
                "log": _log(case["data"], f"réclamé à SSL ({month_label(month)})")})
    return len(ids)


def counts_for_report():
    """What the 6 am report says about this page."""
    cases = database.get_logistics_errors()
    open_cases = [c for c in cases if c["status"] not in CLOSED]
    to_claim = sum(r["amount"] for r in ledger() if r["billing"] == "to_claim")
    return {
        "À faire": sum(1 for c in open_cases if stage(c) == "a-faire"),
        "Réponse client à lire": sum(1 for c in open_cases if c["data"].get("reply_unread")),
        "À réclamer à SSL": euros(to_claim) if to_claim else 0,
    }
