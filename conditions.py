"""The return conditions published on maxsauveur.com, read once a day for the drafts.

Samuel (14/11/2026): « À chaque fois que tu poses des questions sur les conditions
de retour, regarde sur le site les conditions. » The drafts asked him whether a
return was free, when the « Livraison et retour » page and article 6 of the CGV
say it. Those two texts now sit in every draft's context.

Reads public pages only. A failed read keeps the last copy (memory, then the
file beside the database); with no copy at all, the block is simply left out.
"""
import html
import json
import os
import re
import time

import requests

import database

PAGES = [
    ("Page « Livraison et retour »", "https://maxsauveur.com/pages/livraison-et-retour", None, None),
    ("Conditions générales de vente, article 6 (droit de rétractation, retours, remboursements)",
     "https://maxsauveur.com/policies/refund-policy", r"6\s*\.\s*Droit de rétractation", r"7\s*\.\s*Responsabilité"),
    ("Règlement de la carte cadeau Mad Max (commandes du 5 novembre 2026 : ce que devient la carte après un retour)",
     "https://maxsauveur.com/pages/reglement-carte-cadeau-mad-max", None, None),
]
TTL = 24 * 3600
MAX_CHARS = 16000
_cache = {"at": 0, "text": ""}


def _cache_file():
    return os.path.join(os.path.dirname(os.path.abspath(database.DB_PATH)), "site_conditions.json")


def _page_text(url, start=None, end=None):
    raw = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (assistant Max Sauveur)"}).text
    raw = re.sub(r"(?s)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", raw)
    main = re.search(r"(?s)<main[^>]*>(.*)</main>", raw)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", main.group(1) if main else raw))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if start:
        s = re.search(start, text)
        e = re.search(end, text[s.end():]) if s and end else None
        if s:
            text = text[s.start(): s.end() + e.start()] if e else text[s.start():]
    return text


def _fetch():
    parts = []
    for title, url, start, end in PAGES:
        try:
            parts.append(f"[{title}] ({url})\n{_page_text(url, start, end)}")
        except Exception as e:
            print(f"[Conditions] {url} unreadable: {e}")
    return "\n\n".join(parts)[:MAX_CHARS]


def published_conditions():
    """The block for the system prompt, fresh within a day."""
    if _cache["text"] and time.time() - _cache["at"] < TTL:
        return _cache["text"]
    text = _fetch()
    if text:
        _cache.update(at=time.time(), text=text)
        try:
            with open(_cache_file(), "w") as f:
                json.dump({"at": _cache["at"], "text": text}, f, ensure_ascii=False)
        except Exception as e:
            print(f"[Conditions] Copy not saved: {e}")
        return text
    try:
        with open(_cache_file()) as f:
            saved = json.load(f)
        _cache.update(at=time.time() - TTL + 3600, text=saved.get("text", ""))   # retry in an hour
    except Exception:
        pass
    return _cache["text"]


# Where a published page is out of date, Samuel's word wins; said right under the page text.
CORRECTIONS = [
    "Délai de livraison en point relais en France : le tableau ci-dessus indique encore « Mondial Relay, 7-9 jours », "
    "c'est périmé. Le bon délai est de 3 à 5 jours ouvrés après l'expédition, en Chrono2Shop (Samuel, 19/11/2026).",
]


def block():
    text = published_conditions()
    if not text:
        return ""
    return ("\n\n--- CONDITIONS PUBLIÉES SUR MAXSAUVEUR.COM (relues chaque jour : elles font foi pour les retours, "
            "les échanges, les remboursements et leurs frais) ---\n" + text
            + "\n\nCORRECTIONS DE SAMUEL (elles l'emportent sur le texte du site) :\n"
            + "\n".join("- " + c for c in CORRECTIONS) + "\n")
