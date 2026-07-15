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

RÈGLES DE RÉDACTION ABSOLUES :
- N'utilise jamais le tiret long "—" ni le tiret "-" comme ponctuation dans les emails. Remplace-les par des virgules ou reformule la phrase.
- Ne commence jamais un email par "Bonne nouvelle".
- N'invente JAMAIS un numéro de commande. Tu ne peux citer un numéro de commande QUE s'il apparaît explicitement dans les données Shopify fournies dans ce contexte. Si aucune commande n'est fournie, ne mentionne aucun numéro.
- N'invente aucune information sur la commande, le stock ou les délais.

POSITION PAR RAPPORT AU CLIENT :
Tu es l'avocat de Max Sauveur, pas du client. Ton rôle est de défendre les intérêts de la marque tout en restant honnête et professionnel.

RÈGLES DE DÉFENSE DE LA MARQUE :
- Ne valide jamais automatiquement ce que dit un client. Un client qui affirme quelque chose n'a pas forcément raison.
- Avant d'accepter une réclamation, demande-toi : est-ce que nous sommes réellement en tort ? Si ce n'est pas évident, ne le concède pas.
- Les coûts comptent : un remboursement, un renvoi, un geste commercial ont un coût réel. Ne les accorde jamais par défaut ou par politesse.
- Ne t'excuse pas si on n'a pas fait d'erreur. "Désolé pour la gêne" sans faute de notre côté est interdit.
- Si le client se plaint d'un délai normal, d'une politique standard ou d'une décision commerciale légitime : explique-la calmement, sans culpabilité.
- Si le client affirme avoir payé, reçu, ou renvoyé quelque chose sans preuve : ne le prends pas pour acquis. Demande les preuves (photo, suivi, numéro de commande).
- Si une demande est abusive ou hors politique : refuse-la clairement, poliment, sans tourner autour du pot.
- Les gestes commerciaux (remboursements partiels, avoirs, frais offerts) ne s'accordent QUE si la faute est clairement de notre côté, ou si Samuel le décide explicitement.

RÈGLES ABSOLUES SUR LES FRAIS DE RETOUR :
- Les frais de retour sont TOUJOURS à la charge du client, sauf deux cas précis : défaut de fabrication avéré OU erreur de préparation (mauvais produit envoyé).
- Un conseil pointure donné par nos soins N'EST PAS une erreur de notre part. Le client reste responsable de choisir sa taille.
- Ne propose JAMAIS de prendre en charge les frais de retour pour : mauvaise taille, modèle qui ne convient pas, préférence esthétique, confort insuffisant.
- Ne remonte JAMAIS à Samuel pour un geste commercial sur les frais de retour dans ces cas — la réponse est non, et tu le dis directement sans créer d'attente.
- Ne propose jamais spontanément un geste commercial que Samuel n'a pas autorisé. Si tu n'es pas sûr qu'un geste est justifié, ne le propose pas — attends que Samuel le valide.
- NE MENTIONNE JAMAIS dans un email que "les frais de retour sont à votre charge" ou toute formulation similaire. C'est dans les CGV, le client le sait ou le découvrira — le dire proactivement crée des négociations inutiles. Donne juste la procédure de retour, sans commentaire sur qui paie.

ÉQUILIBRE :
- Rester honnête : si on a clairement fait une erreur (retard de notre fait, produit défectueux confirmé, mauvaise info donnée), on le reconnaît et on propose une solution juste.
- Rester humain : le ton reste courtois et direct, jamais agressif ni condescendant.
- En cas de doute sur la position à tenir : remonte à Samuel avec un résumé des faits plutôt que de concéder par défaut.

Ne mentionne jamais que tu es une IA.
Si tu as des infos de commande disponibles, utilise-les pour personnaliser ta réponse.

ACCÈS AUX OUTILS :
- Les données Wing sont automatiquement injectées dans le contexte quand disponibles, sous forme de bloc "--- Suivi Wing ---" ou "--- Données Wing ---".
- Si un tel bloc est présent, utilise-le comme source de vérité pour le numéro de suivi et le lien.
- Si tu n'as pas de données Wing : génère la réponse avec les infos disponibles (Shopify). Ne demande jamais à Samuel de cliquer un bouton ou de te copier-coller quoi que ce soit.

{KNOWLEDGE_BASE}"""


def _select_relevant_patterns(email_text, patterns, top_n=12):
    """Select the most relevant patterns based on keyword overlap with the email."""
    if not email_text or not patterns:
        return patterns[:top_n]
    email_words = set(re.findall(r'\w+', email_text.lower()))
    scored = []
    for p in patterns:
        pattern_text = f"{p['topic_label']} {p['situation']} {p['response_template']}".lower()
        pattern_words = set(re.findall(r'\w+', pattern_text))
        score = len(email_words & pattern_words)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:top_n]]


def get_system_prompt(email_context=''):
    """Build SYSTEM_PROMPT dynamically, injecting summaries, processes and relevant patterns."""
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
        patterns = database.get_response_patterns()
        if patterns:
            # Inject daily doc first if it exists
            daily_doc = next((p for p in patterns if p.get('topic') == '_daily_doc'), None)
            if daily_doc:
                extra += f"\n\n--- DOCUMENT DES RÉPONSES TYPES (mis à jour chaque soir) ---\n{daily_doc['response_template']}\n"
            # Then inject top relevant individual patterns
            non_meta = [p for p in patterns if not p.get('topic', '').startswith('_')]
            selected = _select_relevant_patterns(email_context, non_meta, top_n=10)
            if selected:
                extra += "\n\n--- FICHES RÉPONSES INDIVIDUELLES (les plus pertinentes) ---\n"
                for p in selected:
                    extra += f"\n[{p['topic_label']}]\nSituation : {p['situation'][:300]}\nRéponse type :\n{p['response_template'][:600]}\n"
        if extra:
            return BASE_SYSTEM_PROMPT + extra
    except Exception:
        pass
    return BASE_SYSTEM_PROMPT


def _build_context(email_body, email_subject, customer_name, order_info=None, history=None, orders=None):
    context = f"Email de : {customer_name}\nSujet : {email_subject}\n\nContenu :\n{email_body}"
    # History FIRST — highest priority context
    if history:
        context += "\n\n--- HISTORIQUE DES ÉCHANGES AVEC CE CLIENT (priorité absolue) ---"
        for h in history:
            direction = "Client →" if h['direction'] == 'received' else "Nous →"
            context += f"\n[{h['date']}] {direction} {h['subject']}\n{h['body'][:600]}\n"

    # Build the list of orders to display
    orders_to_show = orders if orders else ([] if not order_info else [order_info])

    if orders_to_show:
        if len(orders_to_show) == 1:
            o = orders_to_show[0]
            context += "\n\n--- Infos commande ---"
            context += f"\nNuméro : {o['number']}"
            context += f"\nStatut paiement : {o['status']}"
            context += f"\nStatut livraison : {o['fulfillment_status']}"
            context += f"\nDate : {o['created_at']}"
            context += f"\nTotal : {o['total']}"
            if o.get('tracking_number'):
                context += f"\nN° suivi : {o['tracking_number']}"
            if o.get('tracking_url'):
                context += f"\nLien suivi : {o['tracking_url']}"
            items_str = ', '.join([f"{p['name']} x{p['qty']}" for p in o['products']])
            context += f"\nArticles : {items_str}"
        else:
            context += f"\n\n--- Historique commandes client ({len(orders_to_show)} commandes) — lis l'email pour identifier laquelle est concernée ---"
            for idx, o in enumerate(orders_to_show):
                label = " (la plus récente)" if idx == 0 else ""
                context += f"\n\n[Commande {idx + 1}{label}]"
                context += f"\nNuméro : {o['number']}"
                context += f"\nDate : {o['created_at']}"
                context += f"\nStatut livraison : {o['fulfillment_status']}"
                context += f"\nTotal : {o['total']}"
                if o.get('tracking_number'):
                    context += f"\nN° suivi : {o['tracking_number']}"
                if o.get('tracking_url'):
                    context += f"\nLien suivi : {o['tracking_url']}"
                items_str = ', '.join([f"{p['name']} x{p['qty']}" for p in o['products']])
                context += f"\nArticles : {items_str}"

    return context


def _call_claude(system, messages, max_tokens=1024, model="claude-sonnet-4-6"):
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    for attempt in range(3):
        try:
            message = client.messages.create(
                model=model,
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


def _call_haiku(messages, max_tokens=512, system=None):
    """Appel rapide et économique pour les tâches simples."""
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    kwargs = dict(model="claude-haiku-4-5-20251001", max_tokens=max_tokens, messages=messages)
    if system:
        kwargs["system"] = system
    message = client.messages.create(**kwargs)
    return message.content[0].text


def generate_response(email_body, email_subject, customer_name, order_info=None, history=None, wing_context='', orders=None):
    context = _build_context(email_body, email_subject, customer_name, order_info, history, orders=orders)
    if wing_context:
        context += wing_context
    prompt = f"""Voici un email de support client à traiter :

{context}

---

INSTRUCTIONS :
1. Utilise EN PRIORITÉ l'historique des échanges pour comprendre le contexte et éviter de répéter des choses déjà dites.
2. Si le contexte contient PLUSIEURS commandes client : cherche dans TOUS les articles de TOUTES les commandes celui qui correspond au produit mentionné dans l'email (matching souple : "Jagger" = "Moc Jagger" = "Mocassin Jagger"). Si tu trouves une correspondance même partielle, utilise cette commande. Si aucune correspondance, dis-le clairement en listant les produits trouvés dans chaque commande.
3. Génère TOUJOURS une réponse directement. N'utilise JAMAIS le JSON needs_info pour des questions de suivi de commande, de livraison, de statut, de point relais ou de numéro de suivi — utilise les données Shopify et/ou Wing déjà dans le contexte.
4. Le JSON needs_info est réservé UNIQUEMENT aux cas où une décision commerciale est impossible sans l'avis de Samuel (ex : accorder un geste commercial exceptionnel, savoir si une garantie s'applique dans un cas limite). Utilise-le avec parcimonie.
5. Si le statut de livraison Shopify est disponible dans le contexte, utilise-le directement sans poser de question."""
    return _call_claude(
        system=get_system_prompt(email_context=f"{email_subject} {email_body}"),
        messages=[{"role": "user", "content": prompt}]
    )


def answer_question(email_body, email_subject, customer_name, order_info, question, previous_exchanges=None, orders=None):
    """Samuel asks a question about how to handle this email. Returns dict with samuel_answer and updated_draft."""
    context = _build_context(email_body, email_subject, customer_name, order_info, orders=orders)

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

Utilise tous les éléments ci-dessus pour répondre.
Si le contexte contient PLUSIEURS commandes client, identifie laquelle est concernée par l'email (produit mentionné, numéro, description) et n'utilise que celle-là.
Si le contexte contient un "Numéro de suivi" et un "Lien de suivi" Wing, intègre-les DIRECTEMENT dans le updated_draft (ne les omets pas, ne demande pas de confirmation).

Réponds en JSON strict avec exactement ces deux champs :
{{
  "samuel_answer": "ta réponse à Samuel en 2-3 phrases (explication, conseil)",
  "updated_draft": "la réponse COMPLÈTE à envoyer au client, tenant compte de tout le contexte"
}}

Réponds UNIQUEMENT avec le JSON, rien d'autre."""

    raw = _call_claude(
        system=get_system_prompt(email_context=f"{email_subject} {email_body}"),
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


def generate_daily_summary(drafts_today, questions_today=None, rejections_today=None, sent_today=None):
    """Generate a summary of the day's emails, questions and corrections for future reference."""
    if not drafts_today and not questions_today and not rejections_today and not sent_today:
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

    sent_text = ""
    if sent_today:
        sent_text = "\n\n--- EMAILS RÉELLEMENT ENVOYÉS AUJOURD'HUI ---\n"
        for s in sent_today:
            source_label = "Réponse client" if s.get('source') == 'reply' else "Email sortant"
            sent_text += f"\n[{source_label}] À: {s['to_email']} | Sujet: {s['subject']}\n{s['body'][:300]}{'...' if len(s['body']) > 300 else ''}\n"

    prompt = f"""Voici les emails traités aujourd'hui par le service client Max Sauveur :

{drafts_text}{questions_text}{rejections_text}{sent_text}

Génère un résumé TRÈS concis (max 400 mots) structuré ainsi :
1. Types de questions reçues aujourd'hui
2. Réponses types données (ce qui a bien marché) — basées sur les emails réellement envoyés
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


def generate_daily_patterns_document(all_patterns):
    """
    At end of day, generate a consolidated structured document of ALL response types
    accumulated to date. Saved as _daily_doc pattern for use the next day.
    """
    if not all_patterns:
        return None
    patterns_text = ""
    for p in all_patterns:
        if p.get('topic', '').startswith('_'):
            continue
        patterns_text += f"\n[{p['topic_label']}]\nSituation : {p['situation'][:200]}\nRéponse type :\n{p['response_template'][:500]}\n---\n"

    prompt = f"""Tu es l'assistant service client de Max Sauveur (lunettes de soleil).
Voici toutes les fiches de réponses types accumulées à ce jour :
{patterns_text}

Génère un DOCUMENT DE RÉFÉRENCE structuré qui regroupe et organise ces fiches.
Format attendu (markdown simple) :

# Document des réponses types — Max Sauveur

## [Catégorie 1] (ex: Livraison & Suivi)
### [Type de situation]
**Quand :** description de la situation
**Réponse type :** le modèle de réponse

## [Catégorie 2] (ex: Retours & SAV)
...

Règles :
- Regroupe les fiches similaires en catégories logiques
- Conserve les formulations clés validées
- Remplace les noms propres par [Prénom], les numéros par [N° commande]
- Maximum 800 mots
- Réponds UNIQUEMENT avec le document, aucun texte autour"""

    return _call_haiku(messages=[{"role": "user", "content": prompt}], max_tokens=1200)


def extract_response_patterns(sent_emails, existing_patterns):
    """
    Analyze today's sent emails and return a list of patterns to upsert.
    Each pattern has: topic, topic_label, situation, response_template, key_points.
    Only returns patterns where the response brings something new or confirms an existing approach.
    """
    if not sent_emails:
        return []

    existing_summary = ""
    if existing_patterns:
        existing_summary = "\n\nFICHES EXISTANTES (topics déjà connus) :\n"
        for p in existing_patterns:
            existing_summary += f"- {p['topic']} : {p['topic_label']} — {p['situation'][:120]}\n"

    emails_text = ""
    for s in sent_emails:
        source_label = "Réponse à un client" if s.get('source') == 'reply' else "Email sortant"
        emails_text += f"\n[{source_label}]\nÀ : {s['to_email']}\nSujet : {s['subject']}\nContenu :\n{s['body']}\n---\n"

    prompt = f"""Tu es l'assistant de Max Sauveur (marque de lunettes de soleil).
Voici les emails envoyés aux clients aujourd'hui :{emails_text}{existing_summary}

Pour chaque email, identifie le type de situation client traité et extrait la réponse validée.
Regroupe les emails similaires ensemble.

Retourne UNIQUEMENT un JSON valide (liste) comme ceci :
[
  {{
    "topic": "slug_snake_case_unique",
    "topic_label": "Titre court lisible (ex: Retour produit défectueux)",
    "situation": "Description en 2-3 phrases de quand cette situation se présente",
    "response_template": "Modèle de réponse extrait de l'email validé (adapté pour être réutilisé, avec [Prénom] pour les variables)",
    "key_points": "Points clés à retenir : ton, engagements, formulations importantes"
  }}
]

Règles :
- Ne crée pas de fiche si l'email est trop vague ou hors sujet
- Si le topic existe déjà dans les fiches existantes, retourne quand même la fiche avec le contenu MIS À JOUR si la réponse apporte une nuance nouvelle — sinon ne l'inclus pas
- Maximum 5 fiches par appel
- Réponds UNIQUEMENT avec le JSON, aucun texte autour"""

    raw = _call_haiku(messages=[{"role": "user", "content": prompt}], max_tokens=2000).strip()
    # Extract JSON array from response
    import re as _re
    match = _re.search(r'\[.*\]', raw, _re.DOTALL)
    if not match:
        return []
    try:
        import json as _json
        return _json.loads(match.group(0))
    except Exception:
        return []


def detect_intent(email_body, email_subject):
    """Détecte l'intention principale de l'email."""
    raw = _call_haiku(
        messages=[{"role": "user", "content": f"""Analyse cet email et réponds UNIQUEMENT avec un JSON :
{{"intent": "relay_change", "address": "adresse complète ou null", "has_full_address": true}}

Intents possibles : relay_change, order_status, return_repair, product_question, other

Sujet: {email_subject}
Corps: {email_body[:500]}

JSON uniquement."""}],
        max_tokens=150
    )
    try:
        return json.loads(raw.strip())
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
14 avenue Lamartine, 13170 Les Pennes-Mirabeau

Délai estimé en atelier : 6 à 8 semaines.

On reste disponible si vous avez la moindre question.

John – Service Client – Max Sauveur"""


def analyze_sav_email(email_body, email_subject, order_info=None, history=None):
    """Analyze a SAV email and return missing info questions + intent summary."""
    order_context = ""
    if order_info:
        order_context = f"\nCommande trouvée : {order_info.get('number')} — {order_info.get('fulfillment_status')} — {order_info.get('total')}"
    history_context = ""
    if history:
        history_context = "\n\n--- HISTORIQUE COMPLET DES ÉCHANGES AVEC CE CLIENT ---"
        for h in history:
            direction = "Client →" if h['direction'] == 'received' else "Nous →"
            history_context += f"\n[{h['date']}] {direction} {h['subject']}\n{h['body'][:600]}\n"
    prompt = f"""Analyse cet email SAV client et réponds en JSON strict :

Sujet : {email_subject}
Corps : {email_body}{order_context}{history_context}

En tenant compte de TOUT l'historique des échanges, identifie :
1. Le problème exact décrit
2. Les informations manquantes pour traiter la demande (ex: pas de photo, pas de numéro de commande, problème flou, etc.)
3. Les questions à poser à Samuel (le responsable) avant de répondre — uniquement si l'historique ne répond pas déjà à ces questions

Réponds UNIQUEMENT avec ce JSON :
{{
  "problem_summary": "résumé en 1 phrase du problème",
  "missing_info": ["info manquante 1", "info manquante 2"],
  "questions_for_samuel": ["question 1 ?", "question 2 ?"],
  "can_respond_now": true
}}

Si tu as assez d'info pour répondre, mets can_respond_now à true et questions_for_samuel vide."""
    raw = _call_haiku(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400
    )
    try:
        import re as _re
        match = _re.search(r'\{[\s\S]*\}', raw)
        if match:
            return json.loads(match.group())
    except Exception:
        pass
    return {"problem_summary": email_subject, "missing_info": [], "questions_for_samuel": [], "can_respond_now": True}


def generate_sav_approval_email(customer_name, order_number, email_body, label_url=None):
    """Generate the approval/repair email using the fixed SAV template."""
    if label_url:
        label_line = f"Voici votre étiquette de retour prépayée (port entièrement pris en charge) :\n{label_url}\n\nImprimez-la et collez-la sur votre colis avant de le déposer en point relais."
    else:
        label_line = "Collez l'étiquette de retour ci-jointe sur votre colis — le port est entièrement pris en charge."
    return SAV_APPROVAL_TEMPLATE.format(
        customer_name=customer_name,
        order_number=order_number or '(votre commande)',
        label_line=label_line
    )


SAV_STATUS_MESSAGES = {
    'received_warehouse': "Votre colis a bien été réceptionné dans notre entrepôt. Nous allons le transmettre à notre atelier de réparation dans les prochains jours.",
    'sent_repair': "Votre paire a été envoyée à notre atelier de réparation. Le délai estimé est de 6 à 8 semaines à compter de la réception à l'atelier.",
    'in_repair': "Votre paire est actuellement en cours de réparation dans notre atelier. Nous vous tiendrons informé dès qu'elle sera prête.",
    'repaired_available': "Votre paire a été réparée et est prête à être renvoyée. Nous allons procéder à son expédition dans les prochaines 48h.",
    'returned_to_client': "Votre paire réparée est en route ! Elle a été expédiée aujourd'hui. Vous recevrez bientôt vos informations de suivi.",
}


def generate_sav_status_notification(customer_name, order_number, status):
    """Generate a customer notification email for a given repair status."""
    message = SAV_STATUS_MESSAGES.get(status, "Votre demande SAV a été mise à jour.")
    order_ref = f" (commande {order_number})" if order_number else ""
    return f"""Bonjour {customer_name},

Nous vous contactons concernant votre demande de réparation{order_ref}.

{message}

N'hésitez pas à nous contacter si vous avez la moindre question.

John – Service Client – Max Sauveur"""


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
        system=get_system_prompt(email_context=f"{reason} {email_body}"),
        messages=[{"role": "user", "content": prompt}]
    )


OUTBOUND_SUBJECT_TEMPLATES = {
    'stock_issue': {
        'label': 'Rupture de stock / substitution produit',
        'subject': 'Votre commande Max Sauveur — information importante',
        'context': """Le client a passé une commande mais suite à une mauvaise synchronisation des stocks, nous ne sommes pas en mesure de lui envoyer sa commande complète. Tu dois lui proposer un produit de substitution ou un remboursement partiel, en t'excusant sincèrement pour la gêne occasionnée.""",
    },
    'return_refund': {
        'label': 'Confirmation retour reçu — remboursement',
        'subject': 'Retour reçu — remboursement en cours',
        'context': '',
        'template': """Bonjour [Prénom],

Nous avons bien reçu votre [produit] et votre demande de remboursement est enregistrée. On s'en occupe.

Par simple curiosité, au-delà du problème de [raison], est-ce qu'il y a un détail sur le modèle (le cuir, la forme ou le style) qui ne vous a pas totalement convaincu ?

On essaie de comprendre si une autre pièce de la collection pourrait mieux répondre à vos attentes, que vous soyez plutôt dans une recherche d'esthétique précise ou de durabilité.

=> Si un autre modèle vous fait de l'œil, on peut vous proposer une solution plus simple : nous vous envoyons un avoir d'une valeur de [montant + 35]€ (soit 35€ offerts par la maison) et nous prenons en charge les frais de livraison pour cet échange. Cela vous permet de trouver la pièce idéale sans aucun frais supplémentaire.

Dites-moi simplement si vous préférez cette option ou si nous validons le remboursement initial sur votre compte.

John de Max Sauveur""",
    },
    'return_exchange': {
        'label': 'Confirmation retour reçu — échange',
        'subject': 'Retour reçu — échange en cours',
        'context': '',
        'template': """Bonjour [Prénom],

Nous avons bien reçu votre colis retour. Votre échange est enregistré et en cours de traitement.

[Détails de l'échange : nouveau modèle / taille / délai estimé]

N'hésitez pas si vous avez la moindre question.

John de Max Sauveur""",
    },
    'custom': {
        'label': 'Autre (message libre)',
        'subject': 'Max Sauveur — Service Client',
        'context': '',
    },
}


def generate_outbound_email(customer_name, customer_email, subject_type, user_draft, order_info=None, feedback=None, previous_draft=None):
    """Generate a polished outbound email from Samuel's rough draft."""
    template = OUTBOUND_SUBJECT_TEMPLATES.get(subject_type, OUTBOUND_SUBJECT_TEMPLATES['custom'])
    order_context = ""
    if order_info:
        items_str = ', '.join([f"{i['name']} x{i['qty']}" for i in order_info.get('products', [])])
        order_context = f"\nCommande : {order_info.get('number')} — {order_info.get('total')} — {order_info.get('fulfillment_status')}"
        if items_str:
            order_context += f"\nArticles : {items_str}"
    type_context = f"\nContexte : {template['context']}" if template.get('context') else ""
    draft_block = f"\n\nInstructions spécifiques de Samuel :\n{user_draft}" if user_draft.strip() else ""
    feedback_block = ""
    if feedback and previous_draft:
        feedback_block = f"\n\nVersion précédente générée :\n{previous_draft}\n\nCorrection demandée par Samuel : {feedback}\nRéécris l'email en tenant compte de cette correction."
    elif feedback:
        feedback_block = f"\n\nInstruction : {feedback}"
    # 1. Check DB/env saved template, 2. Fall back to code default template
    template_block = ""
    has_saved_template = False
    try:
        import database as _db
        saved = _db.get_outbound_template(subject_type)
        if saved:
            has_saved_template = True
            template_block = f"\n\nMODÈLE VALIDÉ À SUIVRE OBLIGATOIREMENT :\n---\n{saved}\n---"
    except Exception:
        pass
    if not has_saved_template and template.get('template'):
        has_saved_template = True
        template_block = f"\n\nMODÈLE DE BASE À SUIVRE OBLIGATOIREMENT :\n---\n{template['template']}\n---"

    if has_saved_template:
        instruction = """INSTRUCTION PRINCIPALE : Un modèle est fourni ci-dessus. Tu DOIS partir de ce modèle exact.
Adapte uniquement : le prénom client, le produit, le numéro de commande, les montants, et intègre les instructions spécifiques de Samuel si présentes.
Ne réécris pas l'email from scratch. Garde la structure, le ton et les formulations du modèle.
Si Samuel donne des instructions spécifiques, applique-les comme des ajustements au modèle, pas comme une réécriture complète."""
    else:
        instruction = """Rédige un email complet, professionnel et humain dans le style de John (service client Max Sauveur).
Respecte les règles habituelles : pas de tiret long, pas de "Bonne nouvelle", signature John de Max Sauveur.
Si des notes sont fournies, utilise-les comme base mais reformule et complète."""

    prompt = f"""Tu dois rédiger un email sortant à envoyer à un client Max Sauveur.

Client : {customer_name} ({customer_email}){order_context}{type_context}{template_block}{draft_block}{feedback_block}

{instruction}
Réponds uniquement avec le corps de l'email, rien d'autre."""
    return _call_claude(
        system=get_system_prompt(email_context=f"{customer_name} {user_draft}"),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800
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


def extract_name_from_body(email_body):
    """
    Extract a person's name mentioned anywhere in the email body.
    Returns "Prénom NOM" style string or None.
    """
    if not email_body:
        return None

    skip_words = {
        'Cordialement', 'Merci', 'Bonjour', 'Bonsoir', 'Bien', 'Sincères',
        'Salutations', 'Respectueuses', 'Amicalement', 'Bonne', 'Journée',
        'Service', 'Client', 'Max', 'Sauveur', 'John', 'Contact', 'Cdlt',
        'Regards', 'Best', 'Hello', 'Cher', 'Chère', 'Madame', 'Monsieur',
        'Votre', 'Notre', 'Cette', 'Vous', 'Nous', 'Pour', 'Avec', 'Sans',
    }

    # Priority 1: "nom de X", "au nom de X", "sous le nom X" patterns
    name_intro = re.search(
        r'(?:nom\s+de|au\s+nom\s+de?|sous\s+le\s+nom(?:\s+de?)?|sous\s+nom\s+de?|commande\s+(?:de|au\s+nom\s+de?))\s+([A-ZÀ-Ÿ][a-zà-ÿ\-]+\s+[A-ZÀ-Ÿ][A-Za-zà-ÿ\-]+)',
        email_body, re.IGNORECASE
    )
    if name_intro:
        return name_intro.group(1)

    # Priority 2: standalone line in last 8 lines (signature)
    lines = [l.strip() for l in email_body.strip().split('\n') if l.strip()]
    candidates = lines[-8:] if len(lines) > 8 else lines
    line_pattern = re.compile(r'^([A-ZÀ-Ÿ][A-Za-zà-ÿ\-]+\s+[A-ZÀ-Ÿ][A-Za-zà-ÿ\-]*)$')
    for line in reversed(candidates):
        m = line_pattern.match(line)
        if m:
            name = m.group(1)
            words = name.split()
            if all(w not in skip_words for w in words) and len(name) >= 5:
                return name

    # Priority 3: any "Prénom NOM" (all-caps surname) anywhere in body
    all_caps_pattern = re.compile(r'\b([A-ZÀ-Ÿ][a-zà-ÿ\-]+\s+[A-ZÀ-Ÿ]{2,})\b')
    for m in all_caps_pattern.finditer(email_body):
        name = m.group(1)
        words = name.split()
        if all(w not in skip_words for w in words) and len(name) >= 5:
            return name

    return None
