"""Comptage de stock à l'oral : vocaux -> tableau modèle/couleur x pointures.

Chaque vocal (ou transcription) devient UNE ligne du tableau. Un modèle déjà
enregistré n'est jamais additionné : il apparaît sur une nouvelle ligne,
signalée comme doublon, pour que l'oubli d'un vocal déjà envoyé reste visible.

Usage :
  python stock_comptage.py transcribe <audio.m4a> [...]   # texte du vocal (faster-whisper)
  python stock_comptage.py add "<modèle couleur>" "<pointures>" [--source F] [--texte T]
  python stock_comptage.py parse "<pointures dictées>"     # aperçu du découpage
  python stock_comptage.py match "<nom dicté>"             # modèles Shopify proches
  python stock_comptage.py catalog                         # rafraîchit le catalogue Shopify
  python stock_comptage.py list | table [--md] | remove <n> | reset

Pointures : "41 42 42 43,5" compte une paire par mention (42 cité deux fois = 2).
"42 deux paires", "42 x2", "2 fois 42", "trois 43" donnent la quantité indiquée.
"""
import argparse
import difflib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent / "stock_comptage"
DATA = BASE / "comptage.json"
CATALOG = BASE / "catalogue_shopify.json"
TRANSCRIPTS = BASE / "transcriptions"

NOMBRES = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6,
    "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
}
POINTURE_MIN, POINTURE_MAX = 34, 50


# ---------- stockage ----------

def load():
    if DATA.exists():
        return json.loads(DATA.read_text(encoding="utf-8"))
    return {"entrees": []}


def save(data):
    BASE.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


# ---------- découpage des pointures ----------

def _num(tok):
    if tok in NOMBRES:
        return NOMBRES[tok]
    return int(tok) if tok.isdigit() and int(tok) < POINTURE_MIN else None


def parse_pointures(texte):
    """Renvoie {pointure: quantité} à partir du texte dicté."""
    t = texte.lower()
    t = re.sub(r"(\d{2})\s*(?:,|\.|et demi|et demie|demi)\s*(5)?(?!\d)",
               lambda m: f"{m.group(1)}.5" if (m.group(2) or "demi" in m.group(0)) else m.group(0), t)
    t = re.sub(r"(\d{2})\s+et\s+demie?", r"\1.5", t)
    t = re.sub(r"\b(x|fois|paires?|pair|exemplaires?)\b", r" \1 ", t)
    t = re.sub(r"(\d)x\b", r"\1 x", t)
    toks = re.findall(r"\d{2}\.5|\d+|[a-zéèêàù]+", t)

    res = {}
    i = 0
    while i < len(toks):
        tok = toks[i]
        is_size = re.fullmatch(r"\d{2}(\.5)?", tok) and POINTURE_MIN <= float(tok) <= POINTURE_MAX
        if not is_size:
            # "2 fois 42" / "trois 43" : quantité avant la pointure
            q = _num(tok)
            j = i + 1
            if q is not None and j < len(toks) and toks[j] == "fois":
                j += 1
            if q is not None and j < len(toks) and re.fullmatch(r"\d{2}(\.5)?", toks[j]) \
                    and POINTURE_MIN <= float(toks[j]) <= POINTURE_MAX:
                taille = toks[j]
                res[taille] = res.get(taille, 0) + q
                i = j + 1
                continue
            i += 1
            continue
        qty = 1
        j = i + 1
        # "42 deux paires" / "42 x2" / "42 x 2"
        if j < len(toks) and toks[j] == "x":
            j += 1
        if j < len(toks) and _num(toks[j]) is not None and (
                toks[j - 1] == "x" or (j + 1 < len(toks) and toks[j + 1] in ("paire", "paires", "pair", "fois", "exemplaire", "exemplaires"))):
            qty = _num(toks[j])
            j += 1
            if j < len(toks) and toks[j] in ("paire", "paires", "pair", "fois", "exemplaire", "exemplaires"):
                j += 1
        res[tok] = res.get(tok, 0) + qty
        i = j
    return dict(sorted(res.items(), key=lambda kv: float(kv[0])))


def fmt_taille(t):
    return t.replace(".5", ",5")


# ---------- catalogue Shopify ----------

def fetch_catalog():
    import requests
    shop, token = os.getenv("SHOPIFY_SHOP"), os.getenv("SHOPIFY_TOKEN")
    if not shop or not token:
        sys.exit("SHOPIFY_SHOP / SHOPIFY_TOKEN absents : impossible de lire le catalogue.")
    url = f"https://{shop}/admin/api/2024-01/products.json"
    params = {"limit": 250, "fields": "id,title,options,variants,status"}
    headers = {"X-Shopify-Access-Token": token}
    produits = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        for p in r.json().get("products", []):
            couleurs = set()
            for o in p.get("options", []):
                if norm(o.get("name", "")) in ("couleur", "color", "coloris", "colour"):
                    couleurs.update(o.get("values", []))
            produits.append({"titre": p["title"], "couleurs": sorted(couleurs), "statut": p.get("status")})
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
        url, params = (nxt.group(1), None) if nxt else (None, None)
    BASE.mkdir(exist_ok=True)
    CATALOG.write_text(json.dumps(produits, ensure_ascii=False, indent=2), encoding="utf-8")
    return produits


def candidats():
    if not CATALOG.exists():
        return []
    noms = []
    for p in json.loads(CATALOG.read_text(encoding="utf-8")):
        noms.append(p["titre"])
        noms += [f"{p['titre']} - {c}" for c in p["couleurs"]]
    return noms


def match(nom, n=5):
    noms = candidats()
    if not noms:
        return []
    cible = norm(nom)
    mots = set(cible.split())

    def score(c):
        nc = norm(c)
        ratio = difflib.SequenceMatcher(None, cible, nc).ratio()
        commun = len(mots & set(nc.split())) / max(len(mots), 1)
        return 0.5 * ratio + 0.5 * commun

    return sorted(((round(score(c), 2), c) for c in noms), reverse=True)[:n]


# ---------- transcription ----------

def transcribe(paths):
    from faster_whisper import WhisperModel
    model = WhisperModel(os.getenv("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    for p in paths:
        segs, _ = model.transcribe(p, language="fr", vad_filter=True,
                                   initial_prompt="Comptage de stock de chaussures : modèle, couleur, pointures 41, 41,5, 42, 42,5, 43, 43,5, 44, 44,5, 45.")
        texte = " ".join(s.text.strip() for s in segs).strip()
        (TRANSCRIPTS / (Path(p).stem + ".txt")).write_text(texte, encoding="utf-8")
        print(f"=== {Path(p).name}\n{texte}\n")


# ---------- tableau ----------

def table(data, md=False):
    entrees = data["entrees"]
    if not entrees:
        return "Aucun comptage enregistré."
    tailles = sorted({t for e in entrees for t in e["pointures"]}, key=float)
    vus = {}
    rows = []
    for k, e in enumerate(entrees, 1):
        cle = norm(e["modele"])
        doublon = cle in vus
        vus.setdefault(cle, k)
        cells = [str(e["pointures"].get(t, "")) for t in tailles]
        rows.append([str(k), e["modele"] + (f"  ⚠ doublon de #{vus[cle]}" if doublon else ""),
                     *cells, str(sum(e["pointures"].values()))])
    head = ["#", "Modèle / couleur", *map(fmt_taille, tailles), "Total"]
    total_row = ["", "TOTAL", *[str(sum(e["pointures"].get(t, 0) for e in entrees) or "") for t in tailles],
                 str(sum(sum(e["pointures"].values()) for e in entrees))]
    if md:
        lines = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in rows + [total_row]]
        return "\n".join(lines)
    allr = [head] + rows + [total_row]
    w = [max(len(r[i]) for r in allr) for i in range(len(head))]
    fmt = lambda r: "  ".join(c.ljust(w[i]) if i == 1 else c.rjust(w[i]) for i, c in enumerate(r))
    sep = "-" * len(fmt(head))
    return "\n".join([fmt(head), sep, *map(fmt, rows), sep, fmt(total_row)])


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("transcribe"); s.add_argument("fichiers", nargs="+")
    s = sub.add_parser("add"); s.add_argument("modele"); s.add_argument("pointures")
    s.add_argument("--source", default=""); s.add_argument("--texte", default="")
    s = sub.add_parser("parse"); s.add_argument("texte")
    s = sub.add_parser("match"); s.add_argument("nom")
    sub.add_parser("catalog"); sub.add_parser("list"); sub.add_parser("reset")
    s = sub.add_parser("table"); s.add_argument("--md", action="store_true")
    s = sub.add_parser("remove"); s.add_argument("numero", type=int)
    a = ap.parse_args()

    if a.cmd == "transcribe":
        transcribe(a.fichiers)
    elif a.cmd == "parse":
        print({fmt_taille(k): v for k, v in parse_pointures(a.texte).items()})
    elif a.cmd == "match":
        for sc, nom in match(a.nom) or [(0, "(catalogue absent : lancer `catalog`)")]:
            print(f"{sc:.2f}  {nom}")
    elif a.cmd == "catalog":
        print(f"{len(fetch_catalog())} produits enregistrés dans {CATALOG}")
    elif a.cmd == "add":
        p = parse_pointures(a.pointures)
        if not p:
            sys.exit("Aucune pointure reconnue.")
        data = load()
        data["entrees"].append({"modele": a.modele.strip(), "pointures": p, "source": a.source,
                                "texte": a.texte or a.pointures,
                                "ajoute_le": datetime.now().isoformat(timespec="seconds")})
        save(data)
        print(f"#{len(data['entrees'])} {a.modele} : " + ", ".join(f"{fmt_taille(k)}×{v}" for k, v in p.items()))
    elif a.cmd == "list":
        for k, e in enumerate(load()["entrees"], 1):
            print(f"#{k} {e['modele']} [{e['source']}] : {e['texte']}")
    elif a.cmd == "remove":
        data = load()
        e = data["entrees"].pop(a.numero - 1)
        save(data)
        print(f"Supprimé : {e['modele']}")
    elif a.cmd == "reset":
        save({"entrees": []})
        print("Comptage remis à zéro.")
    elif a.cmd == "table":
        print(table(load(), md=a.md))


if __name__ == "__main__":
    main()
