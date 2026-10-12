"""
popup_store.py — Tirage au sort du pop-up store Paris (6, 7 et 8 novembre 2026)

Le client remplit le formulaire sur l'iPad, tire un bon d'achat au hasard et le
reçoit par e-mail depuis hello@maxsauveur.com. Chaque participant est envoyé
dans Klaviyo.

Les codes sont créés à l'avance dans Shopify, un code unique à usage unique par
bon. Le tirage pioche au hasard dans les codes encore libres : sur l'ensemble
des codes, la répartition fixée dans TIERS est donc respectée exactement.
"""

import base64
import email.mime.multipart
import email.mime.text
import email.utils
import os
import re
import secrets
import threading
import time
from datetime import datetime

import requests

import database
import gmail as gmail_helper

# ── Réglages ──────────────────────────────────────────────────────────────────

# Part de chaque montant dans le lot de codes, en pourcentage (total : 100).
TIERS = [
    (15, 7.5),
    (20, 10),
    (30, 57.5),
    (40, 10),
    (50, 5),
    (60, 5),
    (70, 5),
]

MIN_PURCHASE    = 150
STARTS_AT       = os.getenv("POPUP_STARTS_AT", "2026-11-04T00:00:00+01:00")
STARTS_LABEL    = os.getenv("POPUP_STARTS_LABEL", "4 novembre 2026")
ENDS_AT         = os.getenv("POPUP_ENDS_AT", "2027-05-29T23:59:59+02:00")
ENDS_LABEL      = os.getenv("POPUP_ENDS_LABEL", "29 mai 2027")
COLLECTION      = os.getenv("POPUP_COLLECTION_HANDLE", "popup-eligibles")
CODE_PREFIX     = "MSPARIS-"
TITLE_PREFIX    = "Pop-up Paris nov. 2026"
KLAVIYO_LIST    = "Pop-up Paris novembre 2026"
EVENT_ADDRESS   = os.getenv("POPUP_ADDRESS", "2 rue des Commines, 75003 Paris")

PIN             = os.getenv("POPUP_PIN", "")
ADMIN_KEY       = os.getenv("POPUP_ADMIN_KEY", "")

# Adresses internes : elles jouent en mode test, sans consommer de vrai code
# ni partir dans Klaviyo, et peuvent rejouer autant qu'elles veulent.
TEST_DOMAIN     = "@maxsauveur.com"

SHOPIFY_API     = "2025-01"
KLAVIYO_BASE    = "https://a.klaviyo.com/api"
KLAVIYO_REV     = "2026-04-15"
LOGO_URL        = "https://maxsauveur.com/cdn/shop/files/Max-Sauveur-Blanc_260x@2x.png"
SHOP_URL        = "https://maxsauveur.com"

_CODE_ALPHABET  = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # sans 0/O, 1/I/L


def tier_counts(total):
    """Répartit `total` codes entre les montants, au plus près des pourcentages."""
    exact = [(amount, total * pct / 100) for amount, pct in TIERS]
    counts = {amount: int(x) for amount, x in exact}
    missing = total - sum(counts.values())
    by_rest = sorted(exact, key=lambda t: t[1] - int(t[1]), reverse=True)
    for amount, _ in by_rest[:missing]:
        counts[amount] += 1
    return counts


# ── Base de données ───────────────────────────────────────────────────────────

def init_db():
    conn = database.get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS popup_codes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                code           TEXT UNIQUE NOT NULL,
                amount         INTEGER NOT NULL,
                discount_id    TEXT,
                participant_id INTEGER,
                drawn_at       TEXT,
                created_at     TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS popup_participants (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                token           TEXT UNIQUE NOT NULL,
                first_name      TEXT NOT NULL,
                last_name       TEXT NOT NULL,
                email           TEXT NOT NULL,
                phone           TEXT NOT NULL,
                optin_email     INTEGER NOT NULL DEFAULT 0,
                optin_sms       INTEGER NOT NULL DEFAULT 0,
                accepted_at     TEXT NOT NULL,
                is_test         INTEGER NOT NULL DEFAULT 0,
                code            TEXT,
                amount          INTEGER,
                drawn_at        TEXT,
                email_status    TEXT NOT NULL DEFAULT 'pending',
                email_error     TEXT,
                klaviyo_status  TEXT NOT NULL DEFAULT 'pending',
                klaviyo_error   TEXT,
                created_at      TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now().isoformat(timespec="seconds")


def get_participant(token):
    conn = database.get_connection()
    try:
        row = conn.execute("SELECT * FROM popup_participants WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_participant_by_id(pid):
    conn = database.get_connection()
    try:
        row = conn.execute("SELECT * FROM popup_participants WHERE id = ?", (pid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _update_participant(pid, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = database.get_connection()
    try:
        conn.execute(f"UPDATE popup_participants SET {cols} WHERE id = ?", (*fields.values(), pid))
        conn.commit()
    finally:
        conn.close()


def stats():
    conn = database.get_connection()
    try:
        tiers = [dict(r) for r in conn.execute("""
            SELECT amount, COUNT(*) AS total,
                   SUM(participant_id IS NOT NULL) AS drawn
            FROM popup_codes GROUP BY amount ORDER BY amount
        """)]
        participants = [dict(r) for r in conn.execute(
            "SELECT * FROM popup_participants ORDER BY id DESC")]
    finally:
        conn.close()
    for t in tiers:
        t["remaining"] = t["total"] - (t["drawn"] or 0)
    return tiers, participants


# ── Validation du formulaire ──────────────────────────────────────────────────

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def normalize_phone(raw):
    """Numéro au format international (+33612345678), ou None s'il est invalide."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if re.fullmatch(r"0[1-9]\d{8}", digits):
        digits = "+33" + digits[1:]
    if re.fullmatch(r"\+\d{8,15}", digits):
        return digits
    return None


def validate(form):
    """Renvoie (données propres, erreurs par champ)."""
    data = {
        "first_name": (form.get("first_name") or "").strip()[:60],
        "last_name":  (form.get("last_name") or "").strip()[:60],
        "email":      (form.get("email") or "").strip().lower()[:120],
        "phone":      normalize_phone(form.get("phone")),
        "accept":     bool(form.get("accept_rules")),
        "optin_email": bool(form.get("optin_email")),
        "optin_sms":   bool(form.get("optin_sms")),
    }
    errors = {}
    if not data["first_name"]:
        errors["first_name"] = "Indiquez votre prénom."
    if not data["last_name"]:
        errors["last_name"] = "Indiquez votre nom."
    if not EMAIL_RE.match(data["email"]):
        errors["email"] = "Cette adresse e-mail ne semble pas valide."
    if not data["phone"]:
        errors["phone"] = "Ce numéro ne semble pas valide."
    if not data["accept"]:
        errors["accept_rules"] = "Vous devez accepter le règlement pour participer."
    return data, errors


def register(data):
    """Enregistre le participant. Renvoie (participant, message d'erreur)."""
    is_test = data["email"].endswith(TEST_DOMAIN)
    conn = database.get_connection()
    try:
        if not is_test:
            dup = conn.execute(
                "SELECT id FROM popup_participants WHERE is_test = 0 AND (email = ? OR phone = ?)",
                (data["email"], data["phone"])).fetchone()
            if dup:
                return None, ("Vous avez déjà participé au tirage avec cette adresse e-mail "
                              "ou ce numéro. Une seule participation par personne.")
        token = secrets.token_urlsafe(16)
        cur = conn.execute("""
            INSERT INTO popup_participants
                (token, first_name, last_name, email, phone, optin_email, optin_sms,
                 accepted_at, is_test, klaviyo_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (token, data["first_name"], data["last_name"], data["email"], data["phone"],
              int(data["optin_email"]), int(data["optin_sms"]), _now(), int(is_test),
              "skipped" if is_test else "pending", _now()))
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()
    participant = get_participant_by_id(pid)
    if not is_test:
        _in_background(sync_klaviyo, pid)
    return participant, None


# ── Tirage ────────────────────────────────────────────────────────────────────

_draw_lock = threading.Lock()


def draw(participant):
    """Attribue un bon au participant. Rejouer renvoie le même bon.

    Renvoie (participant à jour, message d'erreur).
    """
    if participant["code"]:
        return participant, None

    with _draw_lock:
        if participant["is_test"]:
            amount = _random_test_amount()
            code = "TEST-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        else:
            conn = database.get_connection()
            try:
                free = conn.execute(
                    "SELECT id, code, amount FROM popup_codes WHERE participant_id IS NULL").fetchall()
                if not free:
                    return participant, "Tous les bons ont été distribués. Merci pour votre visite !"
                picked = secrets.choice(free)
                conn.execute("UPDATE popup_codes SET participant_id = ?, drawn_at = ? WHERE id = ?",
                             (participant["id"], _now(), picked["id"]))
                conn.commit()
                code, amount = picked["code"], picked["amount"]
            finally:
                conn.close()
        _update_participant(participant["id"], code=code, amount=amount, drawn_at=_now())

    _in_background(send_code_email, participant["id"])
    if not participant["is_test"]:
        _in_background(klaviyo_track_draw, participant["id"])
    return get_participant_by_id(participant["id"]), None


def _random_test_amount():
    weights = [pct for _, pct in TIERS]
    roll = secrets.randbelow(int(sum(weights) * 10)) / 10
    for amount, pct in TIERS:
        roll -= pct
        if roll < 0:
            return amount
    return TIERS[-1][0]


def _in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ── E-mail du bon (hello@maxsauveur.com) ──────────────────────────────────────

def _email_html(p):
    link = f"{SHOP_URL}/discount/{p['code']}?redirect=/collections/all"
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Votre bon d'achat Max Sauveur</title></head>
<body style="margin:0;padding:0;background:#f6f6f6;font-family:Helvetica,Arial,sans-serif;color:#1f2021">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f6f6">
<tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff">
  <tr><td align="center" style="background:#1f2021;padding:28px 24px">
    <img src="{LOGO_URL}" width="180" alt="Max Sauveur" style="display:block;width:180px;height:auto;border:0">
  </td></tr>
  <tr><td align="center" style="padding:40px 32px 8px">
    <p style="margin:0 0 6px;font-size:12px;letter-spacing:3px;text-transform:uppercase;color:#70644a">Pop-up store Paris</p>
    <h1 style="margin:0;font-family:Georgia,'Times New Roman',serif;font-weight:normal;font-size:28px;line-height:1.3">
      Merci {p['first_name']}, voici votre bon d'achat</h1>
  </td></tr>
  <tr><td align="center" style="padding:24px 32px 8px">
    <p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:64px;line-height:1;color:#1f2021">{p['amount']}&nbsp;€</p>
  </td></tr>
  <tr><td align="center" style="padding:20px 32px">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="border:1px dashed #70644a;padding:14px 28px;font-size:22px;letter-spacing:3px;font-weight:bold;font-family:'Courier New',monospace">{p['code']}</td>
    </tr></table>
  </td></tr>
  <tr><td align="center" style="padding:8px 32px 32px">
    <a href="{link}" style="display:inline-block;background:#1f2021;color:#ffffff;text-decoration:none;padding:15px 34px;font-size:13px;letter-spacing:2px;text-transform:uppercase">Utiliser mon bon</a>
  </td></tr>
  <tr><td style="padding:0 32px 36px;font-size:13px;line-height:1.6;color:#555555">
    <p style="margin:0 0 8px"><strong style="color:#1f2021">Conditions d'utilisation</strong></p>
    <p style="margin:0">Valable du {STARTS_LABEL} au {ENDS_LABEL} sur maxsauveur.com, pour un achat d'au moins {MIN_PURCHASE}&nbsp;€
    hors accessoires et produits en précommande. Utilisable une seule fois, non cumulable avec une autre offre,
    non remboursable et non échangeable contre des espèces. Si vous avez déjà ouvert le lien ci-dessus, le code
    s'applique tout seul à votre panier.</p>
  </td></tr>
  <tr><td align="center" style="background:#f6f6f6;padding:20px 32px;font-size:11px;line-height:1.6;color:#888888">
    Max Sauveur SAS · 9b rue Jeanne d'Arc, 94500 Champigny-sur-Marne · RCS Paris 327 017 802<br>
    Une question ? Répondez simplement à cet e-mail.
  </td></tr>
</table></td></tr></table></body></html>"""


def _email_text(p):
    return (
        f"Merci {p['first_name']},\n\n"
        f"Voici votre bon d'achat de {p['amount']} € gagné au pop-up store Max Sauveur à Paris.\n\n"
        f"Votre code : {p['code']}\n"
        f"Pour l'utiliser : {SHOP_URL}/discount/{p['code']}?redirect=/collections/all\n\n"
        f"Valable du {STARTS_LABEL} au {ENDS_LABEL} sur maxsauveur.com, pour un achat d'au moins "
        f"{MIN_PURCHASE} € hors accessoires et produits en précommande. Utilisable une seule fois, "
        f"non cumulable avec une autre offre, non remboursable et non échangeable contre des espèces.\n\n"
        f"À bientôt,\nMax Sauveur\n"
    )


def send_code_email(pid):
    p = get_participant_by_id(pid)
    if not p or not p["code"]:
        return
    try:
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg.attach(email.mime.text.MIMEText(_email_text(p), "plain", "utf-8"))
        msg.attach(email.mime.text.MIMEText(_email_html(p), "html", "utf-8"))
        sender = os.getenv("GMAIL_ADDRESS", "hello@maxsauveur.com")
        msg["From"] = email.utils.formataddr(("Max Sauveur", sender))
        msg["To"] = p["email"]
        msg["Subject"] = f"Votre bon d'achat de {p['amount']} € Max Sauveur"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service = gmail_helper.get_gmail_service()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        _update_participant(pid, email_status="sent", email_error=None)
    except Exception as e:
        print(f"[Popup] Email failed for participant {pid}: {e}", flush=True)
        _update_participant(pid, email_status="failed", email_error=str(e)[:500])


# ── Klaviyo ───────────────────────────────────────────────────────────────────

def _kl_headers():
    key = os.getenv("KLAVIYO_POPUP_API_KEY") or os.getenv("KLAVIYO_API_KEY", "")
    return {"Authorization": f"Klaviyo-API-Key {key}", "revision": KLAVIYO_REV,
            "accept": "application/json", "content-type": "application/json"}


_list_id = None


def _klaviyo_list_id():
    """Identifiant de la liste du pop-up, créée au premier passage."""
    global _list_id
    if _list_id:
        return _list_id
    r = requests.get(f"{KLAVIYO_BASE}/lists/", headers=_kl_headers(),
                     params={"filter": f'equals(name,"{KLAVIYO_LIST}")'}, timeout=20)
    r.raise_for_status()
    found = r.json().get("data") or []
    if found:
        _list_id = found[0]["id"]
    else:
        r = requests.post(f"{KLAVIYO_BASE}/lists/", headers=_kl_headers(), timeout=20,
                          json={"data": {"type": "list", "attributes": {"name": KLAVIYO_LIST}}})
        r.raise_for_status()
        _list_id = r.json()["data"]["id"]
    return _list_id


def sync_klaviyo(pid):
    """Crée ou met à jour le profil, l'ajoute à la liste du pop-up et enregistre
    les consentements cochés. Sans case cochée, le profil n'est pas abonné."""
    p = get_participant_by_id(pid)
    if not p:
        return
    try:
        props = {"popup_paris_2026": True, "popup_source": "Pop-up store Paris nov. 2026",
                 "popup_participation": p["created_at"]}
        if p["code"]:
            props.update({"popup_bon_montant": p["amount"], "popup_bon_code": p["code"]})
        r = requests.post(f"{KLAVIYO_BASE}/profile-import/", headers=_kl_headers(), timeout=20, json={
            "data": {"type": "profile", "attributes": {
                "email": p["email"], "phone_number": p["phone"],
                "first_name": p["first_name"], "last_name": p["last_name"],
                "properties": props}}})
        if r.status_code >= 400 and "phone_number" in r.text:
            # Klaviyo refuse certains numéros étrangers : on garde le profil sans le téléphone.
            attrs = {"email": p["email"], "first_name": p["first_name"],
                     "last_name": p["last_name"], "properties": {**props, "popup_telephone": p["phone"]}}
            r = requests.post(f"{KLAVIYO_BASE}/profile-import/", headers=_kl_headers(), timeout=20,
                              json={"data": {"type": "profile", "attributes": attrs}})
        r.raise_for_status()
        profile_id = r.json()["data"]["id"]

        list_id = _klaviyo_list_id()
        r = requests.post(f"{KLAVIYO_BASE}/lists/{list_id}/relationships/profiles/",
                          headers=_kl_headers(), timeout=20,
                          json={"data": [{"type": "profile", "id": profile_id}]})
        r.raise_for_status()

        notes = []
        if p["optin_email"] or p["optin_sms"]:
            subs = {}
            if p["optin_email"]:
                subs["email"] = {"marketing": {"consent": "SUBSCRIBED"}}
            if p["optin_sms"]:
                subs["sms"] = {"marketing": {"consent": "SUBSCRIBED"}}
            attrs = {"email": p["email"], "subscriptions": subs}
            if p["optin_sms"]:
                attrs["phone_number"] = p["phone"]
            r = requests.post(f"{KLAVIYO_BASE}/profile-subscription-bulk-create-jobs/",
                              headers=_kl_headers(), timeout=20, json={"data": {
                                  "type": "profile-subscription-bulk-create-job",
                                  "attributes": {"custom_source": "Pop-up store Paris nov. 2026",
                                                 "profiles": {"data": [{"type": "profile", "attributes": attrs}]}},
                                  "relationships": {"list": {"data": {"type": "list", "id": list_id}}}}})
            if r.status_code >= 400:
                notes.append(f"abonnement refusé : {r.text[:300]}")
        _update_participant(pid, klaviyo_status="failed" if notes else "synced",
                            klaviyo_error="; ".join(notes) or None)
    except Exception as e:
        detail = getattr(getattr(e, "response", None), "text", "") or str(e)
        print(f"[Popup] Klaviyo sync failed for participant {pid}: {detail[:500]}", flush=True)
        _update_participant(pid, klaviyo_status="failed", klaviyo_error=detail[:500])


def klaviyo_track_draw(pid):
    """Ajoute le bon gagné au profil et enregistre l'événement « Pop-up tirage »,
    utilisable ensuite dans les flows Klaviyo (relance avant expiration, etc.)."""
    p = get_participant_by_id(pid)
    if not p or not p["code"]:
        return
    try:
        r = requests.post(f"{KLAVIYO_BASE}/events/", headers=_kl_headers(), timeout=20, json={
            "data": {"type": "event", "attributes": {
                "properties": {"montant": p["amount"], "code": p["code"], "expire_le": ENDS_LABEL},
                "value": p["amount"], "unique_id": f"popup-{p['code']}",
                "metric": {"data": {"type": "metric", "attributes": {"name": "Pop-up tirage"}}},
                "profile": {"data": {"type": "profile", "attributes": {
                    "email": p["email"],
                    "properties": {"popup_bon_montant": p["amount"], "popup_bon_code": p["code"]}}}}}}})
        r.raise_for_status()
    except Exception as e:
        detail = getattr(getattr(e, "response", None), "text", "") or str(e)
        print(f"[Popup] Klaviyo event failed for participant {pid}: {detail[:500]}", flush=True)
        _update_participant(pid, klaviyo_status="failed", klaviyo_error=detail[:500])


# ── Shopify : création des codes ──────────────────────────────────────────────

def _gql(query, variables=None):
    shop, token = os.getenv("SHOPIFY_SHOP"), os.getenv("SHOPIFY_TOKEN")
    r = requests.post(f"https://{shop}/admin/api/{SHOPIFY_API}/graphql.json", timeout=60,
                      headers={"X-Shopify-Access-Token": token},
                      json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify : {body['errors']}")
    return body["data"]


def _collection_id():
    data = _gql("query($h: String!) { collectionByHandle(handle: $h) { id title } }", {"h": COLLECTION})
    coll = data["collectionByHandle"]
    if not coll:
        raise RuntimeError(f"Collection Shopify « {COLLECTION} » introuvable.")
    return coll["id"]


def _new_code(taken):
    while True:
        code = CODE_PREFIX + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if code not in taken:
            taken.add(code)
            return code


def generate_codes(total, log=print):
    """Crée `total` codes dans Shopify, une remise par montant, et les range dans
    l'urne. Peut être relancé : chaque appel ajoute un nouveau lot."""
    collection_id = _collection_id()
    conn = database.get_connection()
    try:
        taken = {r["code"] for r in conn.execute("SELECT code FROM popup_codes")}
    finally:
        conn.close()

    created = {}
    for amount, count in tier_counts(total).items():
        if not count:
            continue
        codes = [_new_code(taken) for _ in range(count)]
        title = f"{TITLE_PREFIX} · {amount} € · lot {datetime.now():%d/%m %H:%M}"
        data = _gql("""
            mutation($d: DiscountCodeBasicInput!) {
              discountCodeBasicCreate(basicCodeDiscount: $d) {
                codeDiscountNode { id }
                userErrors { field message }
              }
            }""", {"d": {
                "title": title,
                "code": codes[0],
                "startsAt": STARTS_AT,
                "endsAt": ENDS_AT,
                "customerSelection": {"all": True},
                "customerGets": {
                    "value": {"discountAmount": {"amount": f"{amount}.00", "appliesOnEachItem": False}},
                    "items": {"collections": {"add": [collection_id]}},
                },
                "minimumRequirement": {"subtotal": {"greaterThanOrEqualToSubtotal": f"{MIN_PURCHASE}.00"}},
                "usageLimit": 1,
                "appliesOncePerCustomer": True,
                "combinesWith": {"orderDiscounts": False, "productDiscounts": False,
                                 "shippingDiscounts": True},
            }})
        res = data["discountCodeBasicCreate"]
        if res["userErrors"]:
            raise RuntimeError(f"Remise {amount} € refusée : {res['userErrors']}")
        discount_id = res["codeDiscountNode"]["id"]
        ok = [codes[0]]
        for i in range(1, len(codes), 250):
            ok += _bulk_add(discount_id, codes[i:i + 250])

        conn = database.get_connection()
        try:
            conn.executemany(
                "INSERT INTO popup_codes (code, amount, discount_id, created_at) VALUES (?, ?, ?, ?)",
                [(c, amount, discount_id, _now()) for c in ok])
            conn.commit()
        finally:
            conn.close()
        created[amount] = len(ok)
        log(f"{amount} € : {len(ok)} codes créés")
    return created


def _bulk_add(discount_id, codes):
    """Ajoute des codes à une remise (250 au plus) et renvoie ceux acceptés."""
    data = _gql("""
        mutation($id: ID!, $codes: [DiscountRedeemCodeInput!]!) {
          discountRedeemCodeBulkAdd(discountId: $id, codes: $codes) {
            bulkCreation { id }
            userErrors { field message }
          }
        }""", {"id": discount_id, "codes": [{"code": c} for c in codes]})
    res = data["discountRedeemCodeBulkAdd"]
    if res["userErrors"]:
        raise RuntimeError(f"Ajout de codes refusé : {res['userErrors']}")
    job = res["bulkCreation"]["id"]
    for _ in range(60):
        time.sleep(2)
        st = _gql("""
            query($id: ID!) { discountRedeemCodeBulkCreation(id: $id) {
              done failedCount
              codes(first: 250) { nodes { code errors { message } } }
            } }""", {"id": job})["discountRedeemCodeBulkCreation"]
        if st["done"]:
            return [n["code"] for n in st["codes"]["nodes"] if not n["errors"]]
    raise RuntimeError("Shopify n'a pas fini de créer les codes à temps.")
