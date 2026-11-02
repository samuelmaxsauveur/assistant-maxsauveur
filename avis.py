"""Customer reviews: a review Samuel publishes on Yotpo becomes a mail asking to post it on Google.

Nothing happens for a review until Samuel publishes it on Yotpo. The check runs
every Monday before the 6 am report (Samuel's choice: once a week). It only
prepares the mail: sending always takes his click on the Avis page.

The mail never promises anything in return and never asks for a rating: Google
forbids rewards for reviews, so no code or gift may ever be tied to this flow.
It goes out whatever the score of the published review.

A published review shows up in the normal Yotpo list (`deleted` false). Its
`updated_at` also moves for reasons that have nothing to do with moderation, so
new reviews are found by id: the first run records everything already published
as `known`, and those customers never get a mail.
"""
import html
import os
import re
import time
from datetime import datetime, timedelta

import requests

import database
import gmail as gmail_helper

API = "https://api.yotpo.com"
PAGE_SIZE = 100
MAX_PAGES = 1000
WINDOW_DAYS = 21  # A missed Monday must not lose the reviews of that week.
LINK_PLACEHOLDER = "[LIEN GOOGLE À AJOUTER]"
OWN_DOMAIN = "@maxsauveur.com"

# Tabs of the Avis page: key, title, statuses.
STAGES = [
    ("a-valider", "À valider", ["to_validate"]),
    ("probleme", "À vérifier", ["failed", "sending"]),
    ("envoyes", "Envoyés", ["sent"]),
    ("ecartes", "Écartés", ["removed", "skipped"]),
]
SENDABLE = ("to_validate", "failed")

FRENCH = set("le la les un une des et est très je mon ma mes pour avec pas de du que qui sont bien "
             "chaussures qualité paire".split())
ENGLISH = set("the and is very i my for with not of that are good shoes quality pair great they it this".split())
NOT_A_NAME = {"anonymous", "anonyme", "client", "customer"}

_token = None


def configured():
    return bool(os.getenv("YOTPO_APP_KEY") and os.getenv("YOTPO_SECRET_KEY"))


def review_url():
    return (os.getenv("GOOGLE_REVIEW_URL") or "").strip()


def _new_token():
    global _token
    resp = requests.post(f"{API}/oauth/token", timeout=30, json={
        "client_id": os.getenv("YOTPO_APP_KEY"),
        "client_secret": os.getenv("YOTPO_SECRET_KEY"),
        "grant_type": "client_credentials",
    })
    resp.raise_for_status()
    _token = resp.json()["access_token"]
    return _token


def _get_reviews(params):
    url = f"{API}/v1/apps/{os.getenv('YOTPO_APP_KEY')}/reviews"
    resp = requests.get(url, params=params, timeout=30, headers={"X-Yotpo-Token": _token or _new_token()})
    if resp.status_code == 401:
        resp = requests.get(url, params=params, timeout=30, headers={"X-Yotpo-Token": _new_token()})
    resp.raise_for_status()
    return resp.json().get("reviews", [])


def published_reviews(since=None):
    """Every published review, or an error: a partial list must never pass for a complete one."""
    params = {"count": PAGE_SIZE}
    if since:
        params["since_updated_at"] = since
    reviews = []
    for page in range(1, MAX_PAGES + 1):
        batch = _get_reviews({**params, "page": page})
        reviews += [r for r in batch if not r.get("deleted") and not r.get("archived")]
        if len(batch) < PAGE_SIZE:
            return reviews
    raise RuntimeError("Trop de pages d'avis Yotpo")


def first_name(name):
    word = (name or "").strip().split(" ")[0].strip(",.")
    if len(word) < 2 or "@" in word or re.search(r"\d", word) or word.lower() in NOT_A_NAME:
        return ""
    return word.title()


def language(content):
    words = re.findall(r"[a-zà-ÿ']+", (content or "").lower())
    french = sum(w in FRENCH for w in words)
    english = sum(w in ENGLISH for w in words)
    return "en" if english > french and english >= 2 else "fr"


def _mail_fr(name, score, quote, link):
    hello = f"Bonjour {name}," if name else "Bonjour,"
    subject = f"{name}, j'ai lu votre avis" if name else "J'ai lu votre avis"
    if score >= 4:
        return subject, f"""{hello}

C'est Sam, le fondateur de Max Sauveur. Je lis tous les avis qu'on reçoit (c'est plus fort que moi), et je suis tombé sur le vôtre :

« {quote} »

Franchement, ça m'a fait plaisir. Merci d'avoir pris ce temps-là.

Du coup, j'ai un petit service à vous demander...

Le truc, c'est que la plupart des gens qui aiment leur paire n'en parlent jamais sur internet. Ils n'ont pas le temps (et je les comprends), ou alors ils reçoivent leurs chaussures, ils sont contents, et ça leur suffit.

Sauf que pour une petite marque comme la nôtre, chaque avis sur Google nous aide à nous faire connaître un peu plus.

Si vous avez deux minutes, vous pourriez raconter ce que vous en pensez sur notre page Google ? Vous pouvez reprendre votre texte tel quel, ou le changer comme vous voulez.

{link}

Ça m'aiderait vraiment. Et si vous n'avez pas le temps, aucun souci, merci déjà pour votre message.

Sam

P.S. Si vous avez la moindre question sur votre paire, répondez à ce mail, ça arrive directement chez moi. (Et si vous préférez que je ne vous écrive plus, dites-le-moi aussi.)
"""
    return subject, f"""{hello}

C'est Sam, le fondateur de Max Sauveur. Je lis tous les avis qu'on reçoit, et j'ai lu le vôtre :

« {quote} »

Merci de l'avoir écrit, ce genre de retour nous sert vraiment. Si quelque chose vous pose problème avec votre paire, répondez à ce mail, ça arrive directement chez moi et on regarde ça ensemble.

J'ai aussi un petit service à vous demander...

Pour une petite marque comme la nôtre, chaque avis sur Google nous aide à nous faire connaître, et les gens qui hésitent y lisent ce que les clients pensent vraiment. Si vous avez deux minutes, vous pourriez raconter votre expérience sur notre page Google, telle qu'elle est ?

{link}

Et si vous n'avez pas le temps, aucun souci, merci déjà pour votre message.

Sam

P.S. Si vous préférez que je ne vous écrive plus, dites-le-moi simplement.
"""


def _mail_en(name, score, quote, link):
    hello = f"Hi {name}," if name else "Hi,"
    subject = f"{name}, I read your review" if name else "I read your review"
    opening = ("Honestly, it made my day. Thanks for taking the time."
               if score >= 4 else
               "Thanks for writing it, this kind of feedback really helps us. If anything is wrong with your pair, "
               "just reply to this email, it comes straight to me and we'll look at it together.")
    ask = ("If you have two minutes, could you share what you think on our Google page? "
           "You can reuse your review as it is, or change it however you like."
           if score >= 4 else
           "If you have two minutes, could you share your experience on our Google page, just as it was?")
    return subject, f"""{hello}

It's Sam, the founder of Max Sauveur. I read every review we get, and I came across yours:

"{quote}"

{opening}

I have a small favour to ask...

Most people who like their shoes never talk about them online. They don't have the time (and I get it), or they get their pair, they're happy, and that's enough.

For a small brand like ours though, every Google review helps a few more people hear about us.

{ask}

{link}

It would really help. And if you don't have the time, no problem at all, thanks for your message already.

Sam

P.S. If you'd rather I didn't write to you again, just let me know.
"""


def compose(name, score, content):
    """The mail for a review: its own words, no reward and no rating asked."""
    quote = html.unescape(content or "").strip()
    write = _mail_en if language(quote) == "en" else _mail_fr
    return write(first_name(name), score or 0, quote, review_url() or LINK_PLACEHOLDER)


def _prepare(review):
    review_id = str(review["id"])
    email = (review.get("email") or "").strip().lower()
    fields = {"email": email, "name": review.get("name"), "score": review.get("score"),
              "content": html.unescape(review.get("content") or "").strip()}
    if not email:
        database.add_review_invite(review_id, "skipped", note="Pas d'adresse e-mail", **fields)
    elif email.endswith(OWN_DOMAIN):
        database.add_review_invite(review_id, "skipped", note="Adresse Max Sauveur", **fields)
    elif database.review_email_invited(email):
        database.add_review_invite(review_id, "skipped", note="Client déjà invité", **fields)
    else:
        subject, body = compose(fields["name"], fields["score"], fields["content"])
        database.add_review_invite(review_id, "to_validate", subject=subject, body=body, **fields)


def seed():
    """First run, at startup: what is already published gets no mail. Returns True if it ran."""
    if not configured() or database.review_invites_seeded():
        return False
    ids = [str(r["id"]) for r in published_reviews()]
    if ids:
        database.add_known_reviews(ids)
        print(f"[Avis] {len(ids)} published reviews recorded, no mail for those.")
    return True


def tick():
    """Find the reviews published since the last check. Prepares mails, never sends one."""
    if not configured() or seed():
        return
    since = (datetime.utcnow() - timedelta(days=WINDOW_DAYS)).date().isoformat()
    reviews = published_reviews(since)
    known = database.known_review_ids([str(r["id"]) for r in reviews])
    fresh = sorted((r for r in reviews if str(r["id"]) not in known), key=lambda r: r.get("created_at") or "")
    for review in fresh:
        _prepare(review)
    if fresh:
        print(f"[Avis] {len(fresh)} newly published review(s).")


def send(ids):
    """Send the mails Samuel validated, from samuel@. Returns the outcome of each."""
    link = review_url()
    if not link:
        raise ValueError("Le lien Google n'est pas configuré, rien n'est parti.")
    service = gmail_helper.get_samuel_service()
    if not service:
        raise ValueError("La boîte samuel@maxsauveur.com n'est pas connectée, rien n'est parti.")
    # A review unpublished since the mail was prepared must not get one.
    published = {str(r["id"]) for r in published_reviews()}
    outcome = {"sent": 0, "failed": 0, "unpublished": 0}
    for review_id in ids:
        invite = database.get_review_invite(review_id)
        if not invite or invite["status"] not in SENDABLE:
            continue
        if review_id not in published:
            database.update_review_invite(review_id, status="removed", note="Dépublié sur Yotpo")
            outcome["unpublished"] += 1
            continue
        body = (invite["body"] or "").replace(LINK_PLACEHOLDER, link)
        if link not in body:
            database.update_review_invite(review_id, status="failed", note="Le lien Google manque dans ce mail")
            outcome["failed"] += 1
            continue
        if not database.claim_review_invite(review_id):
            continue
        try:
            sent = gmail_helper.send_email(service, invite["email"], invite["subject"], body)
            database.update_review_invite(review_id, status="sent", body=body, note=None,
                                          thread_id=sent.get("threadId"), sent_at=datetime.utcnow().isoformat())
            outcome["sent"] += 1
        except Exception as e:
            print(f"[Avis] Sending failed for review {review_id}: {e}")
            database.update_review_invite(review_id, status="failed", note=f"Échec de l'envoi : {e}")
            outcome["failed"] += 1
        time.sleep(1)
    return outcome


def remove(review_id):
    invite = database.get_review_invite(review_id)
    if not invite or invite["status"] not in SENDABLE:
        raise ValueError("Ce mail ne peut plus être retiré")
    database.update_review_invite(review_id, status="removed", note="Retiré par Sam")


def restore(review_id):
    invite = database.get_review_invite(review_id)
    if not invite or invite["status"] not in ("removed", "skipped", "failed", "sending"):
        raise ValueError("Ce mail ne peut pas être remis à valider")
    fields = {"status": "to_validate", "note": None}
    if not invite["body"]:
        fields["subject"], fields["body"] = compose(invite["name"], invite["score"], invite["content"])
    database.update_review_invite(review_id, **fields)


def edit(review_id, subject, body):
    invite = database.get_review_invite(review_id)
    if not invite or invite["status"] not in SENDABLE:
        raise ValueError("Ce mail ne peut plus être modifié")
    database.update_review_invite(review_id, subject=subject, body=body)


def counts_for_report():
    invites = database.get_review_invites(["to_validate", "failed", "sending"])
    return {
        "Mails à valider": sum(i["status"] == "to_validate" for i in invites),
        "À vérifier": sum(i["status"] != "to_validate" for i in invites),
    }
