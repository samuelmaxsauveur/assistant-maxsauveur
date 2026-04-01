import os
import re
import json
import time
import anthropic
from knowledge_base import KNOWLEDGE_BASE

BASE_SYSTEM_PROMPT = f"""Tu es John, du service client de Max Sauveur.

Ton style :
- Friendly et professionnel, mais naturel et humain. Jamais pompeux ni administratif.
- Concis et direct. Zéro redondance.
- Tu utilises "vous" sauf si le client utilise "tu" en premier.
- Tu réponds dans la même langue que le client (français ou anglais).
- Tu signes toujours : John - Service Client - Max Sauveur

Ne mentionne jamais que tu es une IA.
Si tu as des infos de commande disponibles, utilise-les pour personnaliser ta réponse.

{KNOWLEDGE_BASE}"""


def get_system_prompt():
    """Build SYSTEM_PROMPT dynamically, injecting summaries and saved processes."""
    try:
        import database
        extra = ""
        summaries = database.get_recent_summaries(days=14)
        if summaries:
            extra += "\n\n--- RÉSUMÉS DES JOURS PRÉCÉDENTS (référence) ---\n"
            for s in reversed(summaries):
                extra += f"\n[{s['date']}]\n{s['summary']}\n"
        processes = database.get_all_processes()
        if processes:
            extra += "\n\n--- PROCESSUS MANUELS ENREGISTRÉS ---\n"
            for p in processes:
                extra += f"\n[{p['name']}] (déclencheur : {p['trigger']})\n{p['steps']}\n"
        if extra:
            return BASE_SYSTEM_PROMPT + extra
    except Exception:
        pass
    return BASE_SYSTEM_PROMPT


def _build_context(email_body, email_subject, customer_name, order_info=None, history=None):
    context = f"Email de : {customer_name}\nSujet : {email_subject}\n\nContenu :\n{email_body}"
    if order_info:
        context += f"\n\n--- Infos commande ---"
        context += f"\nNuméro : {order_info['number']}"
        context += f"\nStatut paiement : {order_info['status']}"
        context += f"\nStatut livraison : {order_info['fulfillment_status']}"
        context += f"\nDate : {order_info['created_at']}"
        context += f"\nTotal : {order_info['total']}"
        if order_info.get('tracking_number'):
            context += f"\nN° suivi : {order_info['tracking_number']}"
        if order_info.get('tracking_url'):
            context += f"\nLien suivi : {order_info['tracking_url']}"
        items_str = ', '.join([f"{i['name']} x{i['qty']}" for i in order_info['products']])
        context += f"\nArticles : {items_str}"
    if history:
        context += "\n\n--- Historique des échanges avec ce client ---"
        for h in reversed(history):
            direction = "Client →" if h['direction'] == 'received' else "Nous →"
            context += f"\n[{h['date']}] {direction} {h['subject']}\n{h['body'][:300]}...\n"
    return context


def _call_claude(system, messages, max_tokens=1024):
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    for attempt in range(3):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=max_tokens,
                system=system,
                messages=messages
            )
            return message.content[0].text
        except anthropic.APIStatusError as e:
            if attempt < 2:
                time.sleep(3)
                continue
            raise e


def generate_response(email_body, email_subject, customer_name, order_info=None, history=None):
    context = _build_context(email_body, email_subject, customer_name, order_info, history)
    return _call_claude(
        system=get_system_prompt(),
        messages=[{"role": "user", "content": f"Rédige une réponse à cet email de support :\n\n{context}"}]
    )


def answer_question(email_body, email_subject, customer_name, order_info, question, previous_exchanges=None):
    """Samuel asks a question about how to handle this email. Returns dict with samuel_answer and updated_draft."""
    context = _build_context(email_body, email_subject, customer_name, order_info)

    # If question is about price, search current catalog prices in Shopify
    price_catalog = ""
    price_keywords = ['prix', 'price', 'tarif', 'coût', 'combien', 'remise', 'réduction', 'promo', 'discount', 'payer', 'payé']
    if any(kw in question.lower() for kw in price_keywords):
        try:
            import shopify_api
            # Search by product names from order, or from email body keywords
            search_terms = []
            if order_info:
                for p in order_info.get('products', []):
                    search_terms.append(p['name'].split(' ')[0])
            if not search_terms:
                # Extract potential product names from email body
                words = [w for w in email_body.split() if len(w) > 4 and w[0].isupper()]
                search_terms = words[:2]
            catalog_results = []
            seen = set()
            for term in search_terms[:2]:
                for item in shopify_api.search_product_price(term):
                    key = f"{item['product']}|{item['variant']}"
                    if key not in seen:
                        seen.add(key)
                        catalog_results.append(item)
            if catalog_results:
                price_catalog = "\n--- Prix actuels dans le catalogue Shopify ---\n"
                for item in catalog_results:
                    line = f"  {item['product']}"
                    if item['variant'] and item['variant'] != 'Default Title':
                        line += f" — {item['variant']}"
                    line += f" : {item['price']:.2f} EUR"
                    if item['compare_at_price'] > item['price']:
                        line += f" (prix barré : {item['compare_at_price']:.2f} EUR)"
                    line += " ✓" if item['available'] else " (épuisé)"
                    price_catalog += line + "\n"
        except Exception:
            pass

    # Build payment detail block for price-related questions
    payment_detail = ""
    if order_info:
        currency = order_info.get('currency', 'EUR')
        lines = []
        for p in order_info.get('products', []):
            line = f"  - {p['name']} ×{p['qty']} : {p['total']:.2f} {currency}"
            if p.get('discount', 0) > 0:
                line += f" (remise : -{p['discount']:.2f} {currency})"
            lines.append(line)
        shipping = order_info.get('shipping', 0)
        lines.append(f"  - Livraison : {'offerte' if shipping == 0 else f'{shipping:.2f} {currency}'}")
        discount_total = order_info.get('discount_total', 0)
        if discount_total > 0:
            lines.append(f"  - Remise totale : -{discount_total:.2f} {currency}")
        lines.append(f"  - TOTAL PAYÉ : {order_info.get('total', '?')}")
        payment_detail = "\n--- Détail paiement Shopify ---\n" + "\n".join(lines)

    # Build previous exchanges block
    history_block = ""
    if previous_exchanges:
        history_block = "\n--- Historique de notre discussion sur cet email ---\n"
        for ex in previous_exchanges:
            history_block += f"\nSamuel : {ex['question']}\nClaude : {ex['claude_answer']}\n"

    prompt = f"""Voici un email de support client :

{context}{price_catalog}{payment_detail}{history_block}

---

Samuel (le responsable) te demande maintenant :
{question}

Utilise tous les éléments ci-dessus (historique client, détail commande, paiement, échanges précédents) pour répondre.

Réponds en JSON strict avec exactement ces deux champs :
{{
  "samuel_answer": "ta réponse à Samuel en 2-3 phrases (explication, conseil)",
  "updated_draft": "la réponse COMPLÈTE à envoyer au client, tenant compte de tout le contexte"
}}

Réponds UNIQUEMENT avec le JSON, rien d'autre."""

    raw = _call_claude(
        system=get_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500
    )
    try:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            return json.loads(match.group())
    except Exception:
        pass
    return {"samuel_answer": "", "updated_draft": raw}


def generate_daily_summary(drafts_today, questions_today=None, rejections_today=None):
    """Generate a summary of the day's emails, questions and corrections for future reference."""
    if not drafts_today and not questions_today and not rejections_today:
        return None

    drafts_text = ""
    for d in drafts_today:
        intent = d.get('intent', 'other')
        drafts_text += f"\n- Sujet: {d['subject']} | Intent: {intent} | Statut: {d['status']}\n  Réponse: {d['draft_response'][:200]}...\n"

    questions_text = ""
    if questions_today:
        questions_text = "\n\n--- CORRECTIONS ET QUESTIONS DE SAMUEL ---\n"
        for q in questions_today:
            questions_text += f"\nEmail: {q.get('subject', '')} ({q.get('customer_name', '')})\nQuestion de Samuel: {q['question']}\nRéponse Claude: {q['claude_answer'][:300]}\n"

    rejections_text = ""
    if rejections_today:
        rejections_text = "\n\n--- BROUILLONS REJETÉS AVEC COMMENTAIRES ---\n"
        for r in rejections_today:
            rejections_text += f"\nEmail: {r.get('subject', '')} ({r.get('customer_name', '')})\nCommentaire: {r['rejection_comment']}\n"

    prompt = f"""Voici les emails traités aujourd'hui par le service client Max Sauveur :

{drafts_text}{questions_text}{rejections_text}

Génère un résumé TRÈS concis (max 400 mots) structuré ainsi :
1. Types de questions reçues aujourd'hui
2. Réponses types données (ce qui a bien marché)
3. Corrections apportées par Samuel (ce qu'il faut améliorer)
4. Points d'attention à retenir pour les prochains jours

Ce résumé servira de référence pour répondre aux futurs emails."""

    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


def detect_intent(email_body, email_subject):
    """Détecte l'intention principale de l'email."""
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Analyse cet email et réponds UNIQUEMENT avec un JSON comme ceci :
{{"intent": "relay_change", "address": "adresse complète ou null", "has_full_address": true}}

Les intents possibles :
- "relay_change" : le client veut changer son point relais ou adresse de livraison
- "order_status" : le client demande où est sa commande
- "return_repair" : le client veut retourner ou faire réparer un article
- "product_question" : question sur un produit (taille, stock, etc.)
- "other" : autre

Email :
Sujet: {email_subject}
Corps: {email_body}

Réponds UNIQUEMENT avec le JSON, rien d'autre."""
        }]
    )
    try:
        return json.loads(message.content[0].text.strip())
    except:
        return {"intent": "other", "address": None, "has_full_address": False}


SAV_APPROVAL_TEMPLATE = """Bonjour {customer_name},

Merci de nous avoir contacté.

Nous prenons en charge la réparation de votre article. Voici la marche à suivre :

1. Emballez soigneusement votre paire (idéalement dans sa boîte d'origine).
2. Glissez dans le colis un petit mot avec votre nom, votre numéro de commande et la mention "réparation".
3. {label_line}
4. Déposez le colis dans n'importe quel bureau de La Poste ou point relais.

Adresse de retour (déjà renseignée sur l'étiquette) :
SSL – Solutions & Services Logistiques
14 avenue Lamartine — 13170 Les Pennes-Mirabeau

Délai estimé en atelier : 6 à 8 semaines.

On reste disponible si vous avez la moindre question.

John – Service Client – Max Sauveur"""


def generate_sav_approval_email(customer_name, order_number, email_body):
    """Generate the approval/repair email using the fixed SAV template."""
    label_line = "Collez l'étiquette de retour ci-jointe sur votre colis — le port est entièrement pris en charge."
    return SAV_APPROVAL_TEMPLATE.format(
        customer_name=customer_name,
        order_number=order_number or '(votre commande)',
        label_line=label_line
    )


def generate_sav_rejection_email(customer_name, order_number, email_body, reason):
    """Generate a polished rejection email based on Samuel's reason."""
    prompt = f"""Email client reçu :
{email_body}

Instruction du responsable : {reason}

Rédige une réponse professionnelle, courtoise et pédagogique pour refuser ou expliquer la situation.
Sois clair mais humain, jamais froid. Utilise "vous".
Signe : John – Service Client – Max Sauveur
Réponds uniquement avec le corps du mail, rien d'autre."""
    return _call_claude(
        system=get_system_prompt(),
        messages=[{"role": "user", "content": prompt}]
    )


def extract_order_number(text):
    patterns = [
        r'commande[s]?\s+n[°o]?\s*#?\s*(\d{4,6})',
        r'order\s+#?\s*(\d{4,6})',
        r'#(\d{4,6})',
        r'\b(\d{4,6})\b'
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None
