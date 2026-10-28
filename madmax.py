"""
madmax.py — Carte cadeau des Mad Max (commandes du jeudi 5 novembre 2026)

Chaque commande passée le jeudi 5 novembre (jour réservé aux inscrits) et qui a
profité d'une remise automatique « Mad Max » donne droit à un tirage au sort.

  1. Le 6 novembre à 10 h, le client reçoit depuis hello@ un e-mail avec un
     lien personnel, propre à sa commande.
  2. Sur la page, il ouvre la boîte : le tirage pioche un code Shopify encore
     libre, qu'il voit à l'écran et reçoit aussitôt par e-mail.
  3. Le 1er de chaque mois, de décembre à avril, une relance part pour les
     codes pas encore utilisés et les boîtes jamais ouvertes.

Rien ne part tant que MADMAX_SENDING ne vaut pas « 1 » : Sam valide d'abord
les textes. Les adresses @maxsauveur.com servent aux tests et ne consomment
aucun vrai code.

Les codes viennent d'une urne créée à l'avance (une remise Shopify par
montant) : sur l'ensemble des codes, la répartition de TIERS est respectée.
"""

import base64
import email.mime.multipart
import email.mime.text
import email.utils
import html
import os
import secrets
import threading
import time
from datetime import datetime

import pytz
import requests

import database
import gmail as gmail_helper

PARIS = pytz.timezone("Europe/Paris")

# ── Réglages ──────────────────────────────────────────────────────────────────

# Part de chaque montant dans l'urne, en pourcentage (total : 100).
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
STARTS_AT       = "2026-11-10T00:00:00+01:00"
STARTS_LABEL    = "10 novembre 2026"
ENDS_AT         = "2027-04-15T23:59:59+02:00"
ENDS_LABEL      = "15 avril 2027"

# Le jeudi 5 novembre, heure de Paris (UTC+1 en novembre).
ORDERS_FROM_UTC = "2026-11-04T23:00:00Z"
ORDERS_TO_UTC   = "2026-11-05T23:00:00Z"
ORDER_DAY_LABEL = "jeudi 5 novembre"

# Les liens partent à partir du 6 novembre à 10 h. Jusqu'au 30, les commandes
# payées en retard (virement, paiement en attente) reçoivent aussi le leur.
IMPORT_FROM     = PARIS.localize(datetime(2026, 11, 6, 0, 5))
LINKS_FROM      = PARIS.localize(datetime(2026, 11, 6, 10, 0))
LINKS_UNTIL     = PARIS.localize(datetime(2026, 11, 30, 23, 59))

# Relances : le 1er du mois, à partir de 10 h. La dernière est celle d'avril.
REMINDER_MONTHS = ["2026-12", "2027-01", "2027-02", "2027-03", "2027-04"]
REMINDER_HOUR   = 10

# Une commande est éligible si l'une de ses remises (MADMAX26-15, MADMAX26-20,
# MADMAX26-30) contient ce texte, qu'elle soit automatique ou saisie en code.
DISCOUNT_MATCH  = os.getenv("MADMAX_DISCOUNT_MATCH", "madmax26").lower()

# Produits sur lesquels le code s'applique : chaussures et ceintures, sauf
# précommandes et fins de série. Recalculé chaque jour à partir de ces collections.
COLLECTION      = "madmax-eligibles"
COLLECTION_NAME = "Mad Max : produits éligibles à la carte cadeau"
BELTS           = "ceintures"
EXCLUDED        = ["precommande", "fin-de-serie"]

CODE_PREFIX     = "MADMAX-"
TITLE_PREFIX    = "Mad Max nov. 2026 · carte cadeau"
OLD_TITLE_PREFIX = "Pop-up Paris nov. 2026"

BASE_URL        = os.getenv("MADMAX_BASE_URL", "https://assistant-maxsauveur-production.up.railway.app")
# Règlement : sur l'app tant que la page n'existe pas sur maxsauveur.com.
RULES_URL       = os.getenv("MADMAX_RULES_URL", f"{BASE_URL}/cadeau/conditions")
TEST_DOMAIN     = "@maxsauveur.com"

SHOPIFY_API     = "2025-01"
LOGO_URL        = "https://maxsauveur.com/cdn/shop/files/Max-Sauveur-Blanc_260x@2x.png"
SHOP_URL        = "https://maxsauveur.com"

_CODE_ALPHABET  = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # sans 0/O, 1/I/L

CONDITIONS = (
    f"Carte cadeau valable du {STARTS_LABEL} au {ENDS_LABEL} sur maxsauveur.com, en une seule commande, "
    f"dès {MIN_PURCHASE} € d'achat de chaussures et de ceintures (hors précommandes et fins de série). "
    "Non cumulable avec un autre code ou une autre remise. Ni remboursable, ni échangeable contre de "
    f"l'argent. Annulée si la commande du {ORDER_DAY_LABEL} est annulée ou remboursée en totalité."
)


def sending_enabled():
    return os.getenv("MADMAX_SENDING", "0") == "1"


def admin_key():
    return os.getenv("MADMAX_ADMIN_KEY", "")


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
            CREATE TABLE IF NOT EXISTS madmax_codes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT UNIQUE NOT NULL,
                amount      INTEGER NOT NULL,
                discount_id TEXT,
                order_row   INTEGER,
                drawn_at    TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS madmax_orders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_gid     TEXT UNIQUE NOT NULL,
                order_name    TEXT NOT NULL,
                email         TEXT NOT NULL,
                first_name    TEXT,
                order_total   TEXT,
                ordered_at    TEXT,
                token         TEXT UNIQUE NOT NULL,
                is_test       INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'ok',
                link_status   TEXT NOT NULL DEFAULT 'pending',
                link_sent_at  TEXT,
                link_error    TEXT,
                code          TEXT,
                amount        INTEGER,
                drawn_at      TEXT,
                code_status   TEXT NOT NULL DEFAULT 'pending',
                code_sent_at  TEXT,
                code_error    TEXT,
                used          INTEGER NOT NULL DEFAULT 0,
                reminders_off INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS madmax_reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                order_row  INTEGER NOT NULL,
                month      TEXT NOT NULL,
                kind       TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                error      TEXT,
                sent_at    TEXT,
                UNIQUE(order_row, month)
            )
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(madmax_orders)")}
        if "reminders_off" not in cols:
            conn.execute("ALTER TABLE madmax_orders ADD COLUMN reminders_off INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(PARIS).isoformat(timespec="seconds")


def _row(sql, args=()):
    conn = database.get_connection()
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _rows(sql, args=()):
    conn = database.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _exec(sql, args=()):
    conn = database.get_connection()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_order(token):
    return _row("SELECT * FROM madmax_orders WHERE token = ?", (token,))


def get_order_by_id(oid):
    return _row("SELECT * FROM madmax_orders WHERE id = ?", (oid,))


def _update(oid, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    _exec(f"UPDATE madmax_orders SET {cols} WHERE id = ?", (*fields.values(), oid))


def link_url(o):
    return f"{BASE_URL}/cadeau/{o['token']}"


def stop_url(o):
    return f"{BASE_URL}/cadeau/{o['token']}/stop"


def stats():
    tiers = _rows("""
        SELECT amount, COUNT(*) AS total, SUM(order_row IS NOT NULL) AS drawn
        FROM madmax_codes GROUP BY amount ORDER BY amount
    """)
    for t in tiers:
        t["drawn"] = t["drawn"] or 0
        t["remaining"] = t["total"] - t["drawn"]
    orders = _rows("SELECT * FROM madmax_orders ORDER BY id DESC")
    reminders = {}
    for r in _rows("SELECT * FROM madmax_reminders ORDER BY month"):
        reminders.setdefault(r["order_row"], []).append(r)
    return tiers, orders, reminders


# ── Shopify ───────────────────────────────────────────────────────────────────

def _gql(query, variables=None):
    shop, token = os.getenv("SHOPIFY_SHOP"), os.getenv("SHOPIFY_TOKEN")
    for attempt in range(4):
        r = requests.post(f"https://{shop}/admin/api/{SHOPIFY_API}/graphql.json", timeout=60,
                          headers={"X-Shopify-Access-Token": token},
                          json={"query": query, "variables": variables or {}})
        r.raise_for_status()
        body = r.json()
        throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED"
                        for e in body.get("errors") or [])
        if throttled:
            time.sleep(2 + attempt * 2)
            continue
        if body.get("errors"):
            raise RuntimeError(f"Shopify : {body['errors']}")
        return body["data"]
    raise RuntimeError("Shopify : trop de requêtes, réessayez dans une minute.")


def _paginate(query, path, variables=None):
    """Toutes les pages d'une connexion GraphQL ; `path` mène à la connexion."""
    after, out = None, []
    while True:
        data = _gql(query, {**(variables or {}), "after": after})
        conn = data
        for key in path:
            conn = conn[key] if conn else None
        if not conn:
            return out
        out += conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return out
        after = conn["pageInfo"]["endCursor"]


ORDERS_QUERY = """
query($q: String!, $after: String) {
  orders(first: 100, after: $after, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name email createdAt cancelledAt test displayFinancialStatus
      customer { firstName }
      billingAddress { firstName }
      totalPriceSet { shopMoney { amount } }
      discountApplications(first: 20) {
        nodes { __typename
                ... on AutomaticDiscountApplication { title }
                ... on DiscountCodeApplication { code } }
      }
    }
  }
}"""

PAID_STATUSES = {"PAID", "PARTIALLY_REFUNDED", "AUTHORIZED", "PARTIALLY_PAID"}


def _order_status(node):
    """'ok', 'cancelled', 'refunded' ou 'unpaid' (paiement pas encore reçu)."""
    if node.get("cancelledAt"):
        return "cancelled"
    fs = node.get("displayFinancialStatus")
    if fs in ("REFUNDED", "VOIDED"):
        return "refunded"
    if fs not in PAID_STATUSES:
        return "unpaid"
    return "ok"


def _has_madmax(node):
    names = [(a.get("title") or a.get("code") or "").lower().replace(" ", "")
             for a in node["discountApplications"]["nodes"]]
    return any(DISCOUNT_MATCH in n for n in names)


def import_orders():
    """Range les commandes Mad Max du 5 novembre et met à jour leur statut.
    Ne fait que lire Shopify. Renvoie le nombre de nouvelles commandes."""
    nodes = _paginate(ORDERS_QUERY, ["orders"],
                      {"q": f"created_at:>={ORDERS_FROM_UTC} AND created_at:<{ORDERS_TO_UTC}"})
    added = 0
    for n in nodes:
        if n["test"] or not n.get("email") or not _has_madmax(n):
            continue
        status = _order_status(n)
        existing = _row("SELECT id, status FROM madmax_orders WHERE order_gid = ?", (n["id"],))
        if existing:
            if existing["status"] != status:
                _update(existing["id"], status=status)
            continue
        first = (n.get("customer") or {}).get("firstName") or (n.get("billingAddress") or {}).get("firstName") or ""
        _exec("""
            INSERT INTO madmax_orders (order_gid, order_name, email, first_name, order_total, ordered_at,
                                       token, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (n["id"], n["name"], n["email"].strip().lower(), first.strip().title(),
              n["totalPriceSet"]["shopMoney"]["amount"], n["createdAt"], secrets.token_urlsafe(16),
              status, _now()))
        added += 1
    return added


def _refresh_statuses(order_rows):
    """Relit dans Shopify le statut (annulée, remboursée) des commandes données."""
    real = [o for o in order_rows if not o["is_test"]]
    for i in range(0, len(real), 100):
        batch = real[i:i + 100]
        data = _gql("""query($ids: [ID!]!) { nodes(ids: $ids) {
                          ... on Order { id cancelledAt displayFinancialStatus } } }""",
                    {"ids": [o["order_gid"] for o in batch]})
        by_id = {n["id"]: n for n in data["nodes"] if n}
        for o in batch:
            n = by_id.get(o["order_gid"])
            if n:
                status = _order_status(n)
                if status != o["status"]:
                    _update(o["id"], status=status)
                    o["status"] = status


def refresh_usage():
    """Marque les codes déjà utilisés sur le site."""
    used = set()
    for row in _rows("SELECT DISTINCT discount_id FROM madmax_codes WHERE discount_id IS NOT NULL"):
        nodes = _paginate("""
            query($id: ID!, $after: String) { codeDiscountNode(id: $id) { codeDiscount {
              ... on DiscountCodeBasic { codes(first: 250, after: $after) {
                pageInfo { hasNextPage endCursor } nodes { code asyncUsageCount } } } } } }""",
            ["codeDiscountNode", "codeDiscount", "codes"], {"id": row["discount_id"]})
        used |= {n["code"] for n in nodes if n["asyncUsageCount"]}
    for o in _rows("SELECT id, code FROM madmax_orders WHERE code IS NOT NULL AND used = 0 AND is_test = 0"):
        if o["code"] in used:
            _update(o["id"], used=1)


# ── Collection des produits éligibles ─────────────────────────────────────────

def _collection_ids(handle):
    data = _gql("query($h: String!) { collectionByHandle(handle: $h) { id } }", {"h": handle})
    coll = data["collectionByHandle"]
    if not coll:
        raise RuntimeError(f"Collection Shopify « {handle} » introuvable.")
    nodes = _paginate("""query($id: ID!, $after: String) { collection(id: $id) {
                           products(first: 250, after: $after) { pageInfo { hasNextPage endCursor } nodes { id } } } }""",
                      ["collection", "products"], {"id": coll["id"]})
    return coll["id"], {n["id"] for n in nodes}


def sync_collection():
    """Met la collection cachée à jour : chaussures et ceintures, moins les
    précommandes et les fins de série. Renvoie (ajoutés, retirés)."""
    products = _paginate("""query($after: String) { products(first: 250, after: $after) {
                              pageInfo { hasNextPage endCursor } nodes { id productType isGiftCard } } }""",
                         ["products"])
    _, belts = _collection_ids(BELTS)
    excluded = set()
    for handle in EXCLUDED:
        excluded |= _collection_ids(handle)[1]
    wanted = {p["id"] for p in products
              if not p["isGiftCard"] and (p["productType"] != "Accessoires" or p["id"] in belts)} - excluded

    data = _gql("query($h: String!) { collectionByHandle(handle: $h) { id } }", {"h": COLLECTION})
    if data["collectionByHandle"]:
        coll_id, current = _collection_ids(COLLECTION)
    else:
        res = _gql("""mutation($i: CollectionInput!) { collectionCreate(input: $i) {
                        collection { id } userErrors { field message } } }""",
                   {"i": {"title": COLLECTION_NAME, "handle": COLLECTION,
                          "descriptionHtml": "<p>Collection technique, mise à jour chaque nuit par l'assistant : "
                                             "produits sur lesquels la carte cadeau Mad Max s'applique. Ne pas publier, "
                                             "ne pas modifier à la main.</p>"}})["collectionCreate"]
        if res["userErrors"]:
            raise RuntimeError(f"Création de la collection refusée : {res['userErrors']}")
        coll_id, current = res["collection"]["id"], set()

    to_add, to_remove = sorted(wanted - current), sorted(current - wanted)
    for i in range(0, len(to_add), 250):
        res = _gql("""mutation($id: ID!, $p: [ID!]!) { collectionAddProducts(id: $id, productIds: $p) {
                        userErrors { field message } } }""", {"id": coll_id, "p": to_add[i:i + 250]})
        if res["collectionAddProducts"]["userErrors"]:
            raise RuntimeError(f"Ajout refusé : {res['collectionAddProducts']['userErrors']}")
    for i in range(0, len(to_remove), 250):
        res = _gql("""mutation($id: ID!, $p: [ID!]!) { collectionRemoveProducts(id: $id, productIds: $p) {
                        userErrors { field message } } }""", {"id": coll_id, "p": to_remove[i:i + 250]})
        if res["collectionRemoveProducts"]["userErrors"]:
            raise RuntimeError(f"Retrait refusé : {res['collectionRemoveProducts']['userErrors']}")
    return coll_id, len(to_add), len(to_remove)


# ── Création des codes ────────────────────────────────────────────────────────

def _new_code(taken):
    while True:
        code = CODE_PREFIX + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if code not in taken:
            taken.add(code)
            return code


def generate_codes(total, log=print):
    """Crée `total` codes dans Shopify, une remise par montant, et les range dans
    l'urne. Chaque appel ajoute un nouveau lot."""
    collection_id, _, _ = sync_collection()
    taken = {r["code"] for r in _rows("SELECT code FROM madmax_codes")}
    created = {}
    for amount, count in tier_counts(total).items():
        if not count:
            continue
        codes = [_new_code(taken) for _ in range(count)]
        title = f"{TITLE_PREFIX} · {amount} € · lot {datetime.now(PARIS):%d/%m %H:%M}"
        res = _gql("""
            mutation($d: DiscountCodeBasicInput!) {
              discountCodeBasicCreate(basicCodeDiscount: $d) {
                codeDiscountNode { id } userErrors { field message } } }""", {"d": {
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
            "combinesWith": {"orderDiscounts": False, "productDiscounts": False, "shippingDiscounts": True},
        }})["discountCodeBasicCreate"]
        if res["userErrors"]:
            raise RuntimeError(f"Remise {amount} € refusée : {res['userErrors']}")
        discount_id = res["codeDiscountNode"]["id"]
        ok = [codes[0]]
        for i in range(1, len(codes), 250):
            ok += _bulk_add(discount_id, codes[i:i + 250])
        conn = database.get_connection()
        try:
            conn.executemany("INSERT INTO madmax_codes (code, amount, discount_id, created_at) VALUES (?, ?, ?, ?)",
                             [(c, amount, discount_id, _now()) for c in ok])
            conn.commit()
        finally:
            conn.close()
        created[amount] = len(ok)
        log(f"{amount} € : {len(ok)} codes créés")
    return created


def _bulk_add(discount_id, codes):
    res = _gql("""
        mutation($id: ID!, $codes: [DiscountRedeemCodeInput!]!) {
          discountRedeemCodeBulkAdd(discountId: $id, codes: $codes) {
            bulkCreation { id } userErrors { field message } } }""",
               {"id": discount_id, "codes": [{"code": c} for c in codes]})["discountRedeemCodeBulkAdd"]
    if res["userErrors"]:
        raise RuntimeError(f"Ajout de codes refusé : {res['userErrors']}")
    job = res["bulkCreation"]["id"]
    for _ in range(60):
        time.sleep(2)
        st = _gql("""query($id: ID!) { discountRedeemCodeBulkCreation(id: $id) {
                       done codes(first: 250) { nodes { code errors { message } } } } }""",
                  {"id": job})["discountRedeemCodeBulkCreation"]
        if st["done"]:
            return [n["code"] for n in st["codes"]["nodes"] if not n["errors"]]
    raise RuntimeError("Shopify n'a pas fini de créer les codes à temps.")


def close_old_popup_codes():
    """Désactive les anciennes remises du pop-up (codes MSPARIS-), sans rien supprimer."""
    nodes = _gql("""{ codeDiscountNodes(first: 100, sortKey: CREATED_AT, reverse: true) {
                      nodes { id codeDiscount { ... on DiscountCodeBasic { title status } } } } }""")
    closed = []
    for n in nodes["codeDiscountNodes"]["nodes"]:
        d = n["codeDiscount"]
        if not d.get("title", "").startswith(OLD_TITLE_PREFIX) or d.get("status") == "EXPIRED":
            continue
        res = _gql("""mutation($id: ID!) { discountCodeDeactivate(id: $id) {
                        userErrors { field message } } }""", {"id": n["id"]})["discountCodeDeactivate"]
        if res["userErrors"]:
            raise RuntimeError(f"{d['title']} : {res['userErrors']}")
        closed.append(d["title"])
    return closed


# ── Tirage ────────────────────────────────────────────────────────────────────

_draw_lock = threading.Lock()


def draw(o):
    """Attribue une carte à la commande. Rouvrir le lien renvoie la même.
    Renvoie (commande à jour, message d'erreur)."""
    if o["code"]:
        return o, None
    if not o["is_test"]:
        _refresh_statuses([o])
    if o["status"] in ("cancelled", "refunded"):
        return o, ("Cette commande a été annulée ou remboursée : elle ne donne plus droit à la carte "
                   "cadeau. Une question ? Écrivez-nous à hello@maxsauveur.com.")
    with _draw_lock:
        o = get_order_by_id(o["id"])
        if o["code"]:
            return o, None
        if o["is_test"]:
            amount = _random_test_amount()
            code = "TEST-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        else:
            free = _rows("SELECT id, code, amount FROM madmax_codes WHERE order_row IS NULL")
            if not free:
                print("[MadMax] Urne vide : plus aucun code disponible", flush=True)
                return o, ("Le tirage est momentanément indisponible. Réessayez dans quelques heures, "
                           "ou écrivez-nous à hello@maxsauveur.com.")
            picked = secrets.choice(free)
            _exec("UPDATE madmax_codes SET order_row = ?, drawn_at = ? WHERE id = ?",
                  (o["id"], _now(), picked["id"]))
            code, amount = picked["code"], picked["amount"]
        _update(o["id"], code=code, amount=amount, drawn_at=_now())
    threading.Thread(target=send_code_email, args=(o["id"],), daemon=True).start()
    return get_order_by_id(o["id"]), None


def _random_test_amount():
    roll = secrets.randbelow(1000) / 10
    for amount, pct in TIERS:
        roll -= pct
        if roll < 0:
            return amount
    return TIERS[-1][0]


# ── E-mails (hello@maxsauveur.com) ────────────────────────────────────────────
#
# Textes validés par Sam avant l'activation des envois. Chaque e-mail est un
# dictionnaire : subject, intro (paragraphes), code (bool), button (libellé,
# lien), outro (paragraphes), ps.

def _hello(o):
    return f"Bonjour {o['first_name']}," if o.get("first_name") else "Bonjour,"


def _discount_link(o):
    return f"{SHOP_URL}/discount/{o['code']}?redirect=/collections/all"


REMINDER_OPENINGS = {
    "2026-12": "Noël arrive dans trois semaines... et votre carte cadeau de {amount} € n'a pas encore servi.",
    "2027-01": "Bonne année ! Petit rappel pour bien la commencer : votre carte cadeau de {amount} € vous attend toujours.",
    "2027-02": "Votre carte cadeau de {amount} € n'a pas encore servi. Il lui reste un peu plus de deux mois.",
    "2027-03": "Les beaux jours approchent, et votre carte cadeau de {amount} € n'a toujours pas servi.",
    "2027-04": "Votre carte cadeau de {amount} € expire le 15 avril. Dans deux semaines.",
}


def build_email(kind, o, month=None):
    """Contenu d'un e-mail. kind : link, code, reminder_code, reminder_link."""
    last = month == REMINDER_MONTHS[-1]
    if kind == "link":
        return {
            "subject": "Votre carte cadeau vous attend (et c'est vous qui tirez)",
            "intro": [
                "Jeudi, vous avez fait partie des premiers à entrer dans les Mad Max.",
                f"Et comme promis, votre commande {o['order_name']} vous rapporte une carte cadeau. "
                "Son montant, entre 15 et 70 €, c'est le tirage au sort qui va le décider.",
                "Vous avez un seul essai... alors choisissez bien votre moment (le café du matin, ça marche très bien).",
            ],
            "button": ("Je tente ma chance", link_url(o)),
            "outro": [
                f"Votre carte sera valable du {STARTS_LABEL} au {ENDS_LABEL} sur maxsauveur.com. "
                "De quoi vous faire un cadeau pour Noël... ou vous offrir une paire de printemps.",
                "Merci encore pour votre confiance,",
            ],
            "ps": "Le lien est personnel, il est rattaché à votre commande. Si vous avez passé deux commandes "
                  "jeudi, vous recevez deux e-mails : deux boîtes à ouvrir.",
        }
    if kind == "code":
        return {
            "subject": f"Votre carte cadeau de {o['amount']} €",
            "intro": [
                f"Le tirage a parlé : {o['amount']} € de carte cadeau, rien que pour vous.",
                "Je vous mets le code ici, pour l'avoir sous la main le jour venu :",
            ],
            "code": True,
            "button": ("Utiliser ma carte", _discount_link(o)),
            "outro": [
                f"Elle est valable du {STARTS_LABEL} au {ENDS_LABEL}, en une seule fois. "
                "En passant par le bouton, le code s'ajoute tout seul à votre panier.",
                "À très vite,",
            ],
            "ps": "Noël tombe pile dans la période... je dis ça, je dis rien.",
        }
    if kind == "reminder_code":
        return {
            "subject": (f"Il vous reste deux semaines pour vos {o['amount']} €" if last
                        else f"Vos {o['amount']} € attendent toujours"),
            "intro": [REMINDER_OPENINGS.get(month, REMINDER_OPENINGS["2027-02"]).format(amount=o["amount"]),
                      "Votre code :"],
            "code": True,
            "button": ("Utiliser ma carte", _discount_link(o)),
            "outro": [
                (f"Après le {ENDS_LABEL}, elle ne marchera plus (Shopify est intraitable sur les dates).") if last else f"Elle reste valable jusqu'au {ENDS_LABEL}.",
                "À bientôt,",
            ],
            "ps": "Si vous l'avez déjà utilisée ces derniers jours, ne tenez pas compte de ce message.",
            "stop": stop_url(o),
        }
    if kind == "reminder_link":
        return {
            "subject": ("Dernier rappel : votre boîte est toujours fermée" if last
                        else "Votre boîte est toujours fermée"),
            "intro": [
                f"Après votre commande {o['order_name']} du {ORDER_DAY_LABEL}, je vous ai envoyé un lien "
                "pour tirer au sort votre carte cadeau, entre 15 et 70 €.",
                "Et la boîte n'a pas encore été ouverte...",
            ],
            "button": ("J'ouvre ma boîte", link_url(o)),
            "outro": [
                (f"Attention, la carte expire le {ENDS_LABEL} : il vous reste deux semaines pour l'ouvrir "
                 "et l'utiliser.") if last else f"Une fois tirée, votre carte sera valable jusqu'au {ENDS_LABEL}.",
                "À bientôt,",
            ],
            "ps": "Il n'y a pas de perdant : chaque boîte contient une carte.",
            "stop": stop_url(o),
        }
    raise ValueError(kind)


def _render_html(e, o):
    esc = html.escape
    stop_html = ""
    if e.get("stop"):
        stop_html = (f'<br>Vous ne souhaitez plus recevoir ces rappels ? '
                     f'<a href="{esc(e["stop"])}" style="color:#888888">Arrêter les rappels</a>')
    paras = lambda ps: "".join(f'<p style="margin:0 0 16px">{esc(p)}</p>' for p in ps)
    code_block = ""
    if e.get("code"):
        code_block = f"""
  <tr><td align="center" style="padding:4px 32px 4px">
    <p style="margin:0 0 6px;font-family:Georgia,'Times New Roman',serif;font-size:46px;line-height:1">{o['amount']}&nbsp;€</p>
  </td></tr>
  <tr><td align="center" style="padding:10px 32px 18px">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="border:1px dashed #70644a;padding:13px 26px;font-size:21px;letter-spacing:3px;font-weight:bold;font-family:'Courier New',monospace">{esc(o['code'])}</td>
    </tr></table>
  </td></tr>"""
    label, href = e["button"]
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{esc(e['subject'])}</title></head>
<body style="margin:0;padding:0;background:#f6f6f6;font-family:Helvetica,Arial,sans-serif;color:#1f2021">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f6f6">
<tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff">
  <tr><td align="center" style="background:#1f2021;padding:26px 24px">
    <img src="{LOGO_URL}" width="170" alt="Max Sauveur" style="display:block;width:170px;height:auto;border:0">
  </td></tr>
  <tr><td style="padding:34px 32px 6px;font-size:16px;line-height:1.6">
    <p style="margin:0 0 16px">{esc(_hello(o))}</p>
    {paras(e['intro'])}
  </td></tr>{code_block}
  <tr><td align="center" style="padding:6px 32px 26px">
    <a href="{esc(href)}" style="display:inline-block;background:#1f2021;color:#ffffff;text-decoration:none;padding:15px 32px;font-size:13px;letter-spacing:2px;text-transform:uppercase">{esc(label)}</a>
  </td></tr>
  <tr><td style="padding:0 32px 8px;font-size:16px;line-height:1.6">
    {paras(e['outro'])}
    <p style="margin:0 0 22px">Sam</p>
    <p style="margin:0 0 26px;color:#555555">P.S. {esc(e['ps'])}</p>
  </td></tr>
  <tr><td style="padding:0 32px 30px;font-size:12px;line-height:1.6;color:#777777;border-top:1px solid #eeeeee">
    <p style="margin:18px 0 0">{esc(CONDITIONS)}</p>
  </td></tr>
  <tr><td align="center" style="background:#f6f6f6;padding:18px 32px;font-size:11px;line-height:1.6;color:#888888">
    Max Sauveur SAS · 9b rue Jeanne d'Arc, 94500 Champigny-sur-Marne · RCS Paris 327 017 802<br>
    Une question ? Répondez simplement à cet e-mail.{stop_html}
  </td></tr>
</table></td></tr></table></body></html>"""


def _render_text(e, o):
    lines = [_hello(o), ""] + [p + "\n" for p in e["intro"]]
    if e.get("code"):
        lines += [f"{o['amount']} € · code : {o['code']}", ""]
    label, href = e["button"]
    lines += [f"{label} : {href}", ""] + [p + "\n" for p in e["outro"]] + ["Sam", "", f"P.S. {e['ps']}", "", "--", CONDITIONS]
    if e.get("stop"):
        lines += ["", f"Ne plus recevoir ces rappels : {e['stop']}"]
    return "\n".join(lines)


def _send(o, e):
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg.attach(email.mime.text.MIMEText(_render_text(e, o), "plain", "utf-8"))
    msg.attach(email.mime.text.MIMEText(_render_html(e, o), "html", "utf-8"))
    sender = os.getenv("GMAIL_ADDRESS", "hello@maxsauveur.com")
    msg["From"] = email.utils.formataddr(("Max Sauveur", sender))
    msg["To"] = o["email"]
    msg["Subject"] = e["subject"]
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_helper.get_gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()


def preview(kind, o, month=None):
    e = build_email(kind, o, month)
    return e["subject"], _render_html(e, o)


def send_link_email(oid):
    o = get_order_by_id(oid)
    try:
        _send(o, build_email("link", o))
        _update(oid, link_status="sent", link_sent_at=_now(), link_error=None)
    except Exception as e:
        print(f"[MadMax] Link email failed for {o['order_name']}: {e}", flush=True)
        _update(oid, link_status="failed", link_error=str(e)[:500])


def send_code_email(oid):
    o = get_order_by_id(oid)
    if not o or not o["code"]:
        return
    try:
        _send(o, build_email("code", o))
        _update(oid, code_status="sent", code_sent_at=_now(), code_error=None)
    except Exception as e:
        print(f"[MadMax] Code email failed for {o['order_name']}: {e}", flush=True)
        _update(oid, code_status="failed", code_error=str(e)[:500])


def send_reminder(o, month, kind):
    """Une relance par commande et par mois, au plus."""
    try:
        rid = _exec("INSERT INTO madmax_reminders (order_row, month, kind) VALUES (?, ?, ?)",
                    (o["id"], month, kind))
    except Exception:
        return False   # déjà relancée ce mois-ci
    try:
        _send(o, build_email(kind, o, month))
        _exec("UPDATE madmax_reminders SET status = 'sent', sent_at = ? WHERE id = ?", (_now(), rid))
    except Exception as e:
        print(f"[MadMax] Reminder failed for {o['order_name']}: {e}", flush=True)
        _exec("UPDATE madmax_reminders SET status = 'failed', error = ? WHERE id = ?", (str(e)[:500], rid))
    return True


# ── Tâches automatiques (horloge de l'app) ────────────────────────────────────

_tick_lock = threading.Lock()


def tick():
    """Toutes les 30 minutes : liens du 6 novembre, puis relances du 1er du mois."""
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        now = datetime.now(PARIS)
        if IMPORT_FROM <= now <= LINKS_UNTIL:
            import_orders()
        if sending_enabled() and LINKS_FROM <= now <= LINKS_UNTIL:
            _send_pending_links()
        month = now.strftime("%Y-%m")
        if sending_enabled() and month in REMINDER_MONTHS and now.day == 1 and now.hour >= REMINDER_HOUR:
            _send_reminders(month)
    except Exception as e:
        print(f"[MadMax] tick failed: {e}", flush=True)
    finally:
        _tick_lock.release()


def _send_pending_links():
    pending = _rows("SELECT * FROM madmax_orders WHERE link_status = 'pending' AND status = 'ok' AND is_test = 0")
    if pending:
        _refresh_statuses(pending)
    for o in pending:
        if o["status"] == "ok":
            send_link_email(o["id"])
            time.sleep(1)


def _send_reminders(month):
    orders = _rows("""SELECT * FROM madmax_orders WHERE is_test = 0 AND status = 'ok' AND used = 0
                      AND link_status = 'sent' AND reminders_off = 0
                      AND id NOT IN (SELECT order_row FROM madmax_reminders WHERE month = ?)""", (month,))
    if not orders:
        return
    refresh_usage()
    _refresh_statuses(orders)
    for o in orders:
        fresh = get_order_by_id(o["id"])
        if fresh["status"] != "ok" or fresh["used"]:
            continue
        if send_reminder(fresh, month, "reminder_code" if fresh["code"] else "reminder_link"):
            time.sleep(1)


# ── Tests (adresses @maxsauveur.com) ──────────────────────────────────────────

def create_test_order(email_addr, first_name):
    email_addr = email_addr.strip().lower()
    if not email_addr.endswith(TEST_DOMAIN):
        raise ValueError(f"Les tests se font avec une adresse {TEST_DOMAIN}.")
    oid = _exec("""
        INSERT INTO madmax_orders (order_gid, order_name, email, first_name, order_total, ordered_at,
                                   token, is_test, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, (f"test-{secrets.token_hex(6)}", "#TEST", email_addr, first_name.strip().title(), "0",
          _now(), secrets.token_urlsafe(16), _now()))
    send_link_email(oid)
    return get_order_by_id(oid)
