"""
routes_newsletter.py — Interface web pour pousser un Google Doc en brouillon Klaviyo
"""

import os
import re
import requests
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from flask import Blueprint, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

newsletter = Blueprint("newsletter", __name__)

# ── Config ────────────────────────────────────────────────────────────────────

KLAVIYO_API_KEY  = os.getenv("KLAVIYO_API_KEY", "")
FROM_EMAIL       = os.getenv("KLAVIYO_FROM_EMAIL", "")
FROM_LABEL       = os.getenv("KLAVIYO_FROM_LABEL", "Max Sauveur")
REPLY_TO         = os.getenv("KLAVIYO_REPLY_TO", "")
KLAVIYO_REVISION = "2026-04-15"
KLAVIYO_BASE     = "https://a.klaviyo.com/api"
TARGET_LIST      = "test mail"

SECTION_STYLES = {
    "border_color": "#000000", "border_style": "solid", "border_width": 1,
    "content_color_type": "template",
    "inner_padding_bottom": 0, "inner_padding_left": 0,
    "inner_padding_right": 0, "inner_padding_top": 0,
    "stack_on_mobile": True
}

TEXT_STYLES = {
    "block_padding_bottom": 0, "block_padding_left": 0,
    "block_padding_right": 0, "block_padding_top": 0,
    "inner_padding_bottom": 10, "inner_padding_left": 18,
    "inner_padding_right": 18, "inner_padding_top": 10,
    "mobile_stretch_content": False, "text_table_layout": "fixed"
}

BUTTON_STYLES = {
    "background_color": "#F72424", "border_radius": 0,
    "color": "#FFFFFF", "font_family": "Arial",
    "font_size": 16, "font_weight": "700", "letter_spacing": 0
}

GLOBAL_STYLES = [
    {"style_type": "base-styles",
     "properties": {"currency": "en-GB-u-nu-latn_EUR_EU_EUR", "currency_set_on_template": False,
                    "disable_websafe_fonts": False, "mobile_optimizations": True},
     "styles": {"background_format": "auto", "background_position": "left-top",
                "background_repeat": True, "border_color": "#aaaaaa", "border_radius": 0,
                "content_background_color": "#ffffff", "margin_top": 10}},
    {"style_type": "text-styles",
     "styles": {"color": "#444444", "font_family": "Arial", "font_size": 14,
                "line_height": 1.3, "mobile_font_size": 14, "mobile_line_height": 1.3,
                "text_align": "left"}},
    {"style_type": "link-styles",
     "styles": {"color": "#1155cc", "font_weight": "normal", "text_decoration": "underline"}},
    {"style_type": "heading-1-styles",
     "styles": {"color": "#222222", "font_family": "Georgia", "font_size": 40,
                "font_style": "normal", "font_weight": "normal", "line_height": 1.3,
                "margin_bottom": 20, "mobile_font_size": 40, "mobile_line_height": 1.3, "text_align": "left"}},
    {"style_type": "heading-2-styles",
     "styles": {"color": "#222222", "font_family": "Georgia", "font_size": 32,
                "font_style": "normal", "font_weight": "bold", "line_height": 1.1,
                "margin_bottom": 16, "mobile_font_size": 32, "mobile_line_height": 1.3, "text_align": "left"}},
    {"style_type": "heading-3-styles",
     "styles": {"color": "#222222", "font_family": "Georgia", "font_size": 24,
                "font_style": "normal", "font_weight": "bold", "line_height": 1.1,
                "margin_bottom": 12, "mobile_font_size": 24, "mobile_line_height": 1.3, "text_align": "left"}},
    {"style_type": "heading-4-styles",
     "styles": {"color": "#222222", "font_family": "Georgia", "font_size": 18,
                "font_style": "normal", "line_height": 1.1,
                "margin_bottom": 9, "mobile_font_size": 18, "mobile_line_height": 1.3, "text_align": "left"}},
    {"style_type": "mobile-styles", "properties": {}, "styles": {"mobile_margin": 10}}
]


# ── Google Docs ───────────────────────────────────────────────────────────────

def extract_doc_id(url_or_id):
    m = re.search(r'/document/d/([a-zA-Z0-9_-]+)', url_or_id)
    return m.group(1) if m else url_or_id.strip()


def fetch_doc_html(doc_id):
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=html"
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise ValueError(f"Impossible de lire le Google Doc ({r.status_code}). "
                         "Vérifie qu'il est partagé en 'Tout le monde avec le lien'.")
    return r.text


# ── Parser HTML ───────────────────────────────────────────────────────────────

class DocParser(HTMLParser):
    BLOCK_TAGS = {"h1", "h2", "h3", "h4", "p", "li"}

    def __init__(self):
        super().__init__()
        self.blocks      = []
        self.subject     = None
        self._tag        = None
        self._buf        = ""
        self._bold       = 0
        self._italic     = 0
        self._color      = []
        self._href       = None
        self._span_stack = []
        self._pending_p  = []

    def _flush_pending(self):
        if self._pending_p:
            self.blocks.append(_make_text_block("".join(self._pending_p)))
            self._pending_p = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.BLOCK_TAGS:
            self._tag = tag
            self._buf = ""
        if tag in ("b", "strong"):
            self._bold += 1
        if tag in ("i", "em"):
            self._italic += 1
        if tag == "span":
            style = attrs.get("style", "")
            opened = []
            if "font-weight:700" in style or "font-weight: 700" in style:
                self._bold += 1
                opened.append("bold")
            if "font-style:italic" in style or "font-style: italic" in style:
                self._italic += 1
                opened.append("italic")
            cm = re.search(r'color\s*:\s*(#[0-9a-fA-F]{3,6}|rgb\([^)]+\))', style)
            if cm:
                self._color.append(cm.group(1))
                opened.append(("color", cm.group(1)))
            self._span_stack.append(opened)
        if tag == "a":
            self._href = attrs.get("href", "")
            if self._href:
                m = re.search(r'[?&]q=([^&]+)', self._href)
                if m:
                    from urllib.parse import unquote
                    self._href = unquote(m.group(1))

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS and self._tag == tag:
            self._flush_block()
            self._tag = None
        if tag in ("b", "strong"):
            self._bold = max(0, self._bold - 1)
        if tag in ("i", "em"):
            self._italic = max(0, self._italic - 1)
        if tag == "span" and self._span_stack:
            opened = self._span_stack.pop()
            if "bold" in opened:
                self._bold = max(0, self._bold - 1)
            if "italic" in opened:
                self._italic = max(0, self._italic - 1)
            for item in opened:
                if isinstance(item, tuple) and item[0] == "color" and self._color:
                    self._color.pop()
        if tag == "a":
            self._href = None

    def handle_data(self, data):
        if self._tag is None:
            return
        text = data
        if self._href:
            text = f'<a href="{self._href}" style="color:#1155cc;text-decoration:underline;">{text}</a>'
        if self._color and not self._href:
            text = f'<span style="color:{self._color[-1]};">{text}</span>'
        if self._bold:
            text = f'<strong style="font-weight: 700;">{text}</strong>'
        if self._italic:
            text = f"<em>{text}</em>"
        self._buf += text

    def _flush_block(self):
        raw = re.sub(r'<[^>]+>', '', self._buf).strip()
        if not raw:
            return
        tag = self._tag

        m = re.match(r'^\[Objet\]?\s*:?\s+(.+)$', raw, re.IGNORECASE)
        if m:
            self.subject = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            return

        raw_clean = re.sub(r'[\[\]]', '', raw).strip().upper()
        if raw_clean in ("IMAGE", "PHOTO"):
            self._flush_pending()
            self.blocks.append(_make_image_block())
            return

        m = re.match(r'^\[BOUTON\s*:\s*(.+?)\s*:\s*(https?://[^\]]+)\]\s*$', raw, re.IGNORECASE)
        if m:
            label, href = m.group(1).strip(), m.group(2).strip()
            self._flush_pending()
            self.blocks.append(_make_button_block(label, href))
            self.blocks.append(_make_url_text_block(href))
            return

        m = re.match(r'^\[BOUTON\s*:\s*([^\]]+)\]\(([^)]+)\)\s*$', raw, re.IGNORECASE)
        if m:
            label, href = m.group(1).strip(), m.group(2).strip()
            self._flush_pending()
            self.blocks.append(_make_button_block(label, href))
            self.blocks.append(_make_url_text_block(href))
            return

        if re.match(r'^---+$', raw):
            self._flush_pending()
            self.blocks.append(_make_divider_block())
            return

        if tag == "h1":
            self._flush_pending()
            self.blocks.append(_make_text_block(f"<h1>{self._buf}</h1>"))
        elif tag == "h2":
            self._flush_pending()
            self.blocks.append(_make_text_block(
                f'<h3><span style="font-weight: 400;">{self._buf}</span></h3>'))
        elif tag == "h3":
            self._flush_pending()
            self.blocks.append(_make_text_block(f"<h3>{self._buf}</h3>"))
        else:
            if raw:
                self._pending_p.append(f"<p>{self._buf}</p>")


def parse_html(html):
    parser = DocParser()
    parser.feed(html)
    parser._flush_pending()
    m = re.search(r'<title>([^<]+)</title>', html)
    titre = m.group(1).strip() if m else "Newsletter"
    return parser.blocks, parser.subject, titre


# ── Blocs Klaviyo ─────────────────────────────────────────────────────────────

def _make_text_block(html):
    return {"content_type": "block", "type": "text",
            "data": {"content": html, "display_options": {"show_on": "all"},
                     "styles": TEXT_STYLES.copy()}}

def _make_image_block():
    return {"content_type": "block", "type": "image",
            "data": {"properties": {"src": "", "alt_text": "", "href": "", "dynamic": False},
                     "display_options": {"show_on": "all"}, "styles": {}}}

def _make_button_block(label, href):
    return {"content_type": "block", "type": "button",
            "data": {"content": label, "properties": {"href": href},
                     "display_options": {}, "styles": BUTTON_STYLES.copy()}}

def _make_url_text_block(href):
    return {"content_type": "block", "type": "text",
            "data": {"content": f'<p><a href="{href}" style="color:#1155cc;text-decoration:underline;">{href}</a></p>',
                     "display_options": {"show_on": "all"}, "styles": TEXT_STYLES.copy()}}

def _make_divider_block():
    return {"content_type": "block", "type": "divider",
            "data": {"styles": {"border_color": "#e0e0e0", "border_style": "solid", "border_width": "1"},
                     "display_options": {}}}

def _make_footer_block():
    return {"content_type": "block", "type": "text",
            "data": {"content": ("Vous ne voulez plus recevoir nos e-mails ? {% unsubscribe %}.<br>\n"
                                 "<span style=\"color:#D3D3D3;\">{{ organization.name }}</span>"),
                     "display_options": {},
                     "styles": {**TEXT_STYLES, "background_color": "#f7f7f7", "color": "#222222",
                                "font_size": 11, "text_align": "center"}}}

def _blocks_to_definition(blocks):
    all_blocks = blocks + [_make_footer_block()]
    rows = [{"data": {"styles": {"column_layout": "1-column-full-width"}},
             "columns": [{"data": {}, "blocks": [b]}]} for b in all_blocks]
    return {
        "body": {"properties": {"id": "bodyTable", "css_class": "root-container"},
                 "styles": {"background_color": "#eeeeee", "width": 600},
                 "sections": [{"content_type": "section", "type": "section",
                               "data": {"properties": {}, "display_options": {}, "styles": SECTION_STYLES},
                               "rows": rows}]},
        "styles": GLOBAL_STYLES
    }


# ── API Klaviyo ───────────────────────────────────────────────────────────────

def _kl_headers():
    return {"Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
            "revision": KLAVIYO_REVISION, "Content-Type": "application/json",
            "Accept": "application/json"}

def _trouver_liste():
    url, params = f"{KLAVIYO_BASE}/lists/", {"fields[list]": "name", "page[size]": 10}
    while url:
        r = requests.get(url, headers=_kl_headers(), params=params)
        data = r.json()
        for l in data.get("data", []):
            if TARGET_LIST.lower() in l["attributes"]["name"].lower():
                return l["id"]
        url, params = data.get("links", {}).get("next"), {}
    raise ValueError(f"Liste '{TARGET_LIST}' introuvable dans Klaviyo.")

def _creer_template(nom, definition):
    r = requests.post(f"{KLAVIYO_BASE}/templates/", headers=_kl_headers(),
                      json={"data": {"type": "template", "attributes": {
                          "name": nom, "editor_type": "SYSTEM_DRAGGABLE", "definition": definition}}})
    if r.status_code not in (200, 201):
        raise ValueError(r.json().get("errors", [{}])[0].get("detail", r.text))
    return r.json()["data"]["id"]

def _creer_campagne(nom, list_id, sujet, preheader):
    r = requests.post(f"{KLAVIYO_BASE}/campaigns/", headers=_kl_headers(),
                      json={"data": {"type": "campaign", "attributes": {
                          "name": nom,
                          "audiences": {"included": [list_id], "excluded": []},
                          "send_strategy": {"method": "static",
                              "datetime": (datetime.now(timezone.utc) + timedelta(days=30))
                                           .strftime("%Y-%m-%dT10:00:00+00:00")},
                          "campaign-messages": {"data": [{"type": "campaign-message", "attributes": {
                              "definition": {"channel": "email", "label": sujet, "content": {
                                  "subject": sujet, "preview_text": preheader,
                                  "from_email": FROM_EMAIL, "from_label": FROM_LABEL,
                                  "reply_to_email": REPLY_TO or FROM_EMAIL}}}}]}}}})
    if r.status_code not in (200, 201):
        raise ValueError(r.json().get("errors", [{}])[0].get("detail", r.text))
    data = r.json()["data"]
    cid  = data["id"]
    msgs = data.get("relationships", {}).get("campaign-messages", {}).get("data", [])
    if not msgs:
        r2   = requests.get(f"{KLAVIYO_BASE}/campaigns/{cid}/campaign-messages/", headers=_kl_headers())
        msgs = r2.json().get("data", []) if r2.status_code == 200 else []
    return cid, msgs[0]["id"] if msgs else None

def _assigner_template(mid, tid):
    r = requests.post(f"{KLAVIYO_BASE}/campaign-message-assign-template/", headers=_kl_headers(),
                      json={"data": {"type": "campaign-message", "id": mid,
                                     "relationships": {"template": {"data": {"type": "template", "id": tid}}}}})
    if r.status_code not in (200, 201):
        raise ValueError(r.json().get("errors", [{}])[0].get("detail", r.text))


# ── Routes ────────────────────────────────────────────────────────────────────

@newsletter.route("/newsletter")
def newsletter_page():
    return render_template("newsletter.html")


@newsletter.route("/newsletter/preview", methods=["POST"])
def newsletter_preview():
    """Lit le doc et retourne sujet + nb blocs pour préremplir le formulaire."""
    doc_url = request.json.get("doc_url", "").strip()
    if not doc_url:
        return jsonify({"error": "URL manquante"}), 400
    try:
        doc_id = extract_doc_id(doc_url)
        html   = fetch_doc_html(doc_id)
        blocks, subject, titre = parse_html(html)
        return jsonify({
            "titre": titre,
            "subject": subject or titre,
            "nb_blocs": len(blocks),
            "types": [b["type"] for b in blocks]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@newsletter.route("/newsletter/push", methods=["POST"])
def newsletter_push():
    """Crée le brouillon Klaviyo."""
    data      = request.json
    doc_url   = data.get("doc_url", "").strip()
    sujet     = data.get("sujet", "").strip()
    preheader = data.get("preheader", "").strip()
    nom       = data.get("nom", "").strip()

    if not doc_url or not sujet:
        return jsonify({"error": "URL et sujet requis"}), 400

    try:
        doc_id = extract_doc_id(doc_url)
        html   = fetch_doc_html(doc_id)
        blocks, _, titre = parse_html(html)
        definition = _blocks_to_definition(blocks)
        nom = nom or f"[DRAFT] {sujet}"

        list_id  = _trouver_liste()
        tid      = _creer_template(nom, definition)
        cid, mid = _creer_campagne(nom, list_id, sujet, preheader)
        if mid:
            _assigner_template(mid, tid)

        return jsonify({
            "campaign_url": f"https://www.klaviyo.com/campaign/{cid}/edit",
            "nb_blocs": len(blocks)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
