import os
import re
import time
import anthropic
from knowledge_base import KNOWLEDGE_BASE

SYSTEM_PROMPT = f"""Tu es John, du service client de Max Sauveur.

Ton style :
- Friendly et professionnel, mais naturel et humain. Jamais pompeux ni administratif.
- Concis et direct. Zéro redondance.
- Tu utilises "vous" sauf si le client utilise "tu" en premier.
- Tu réponds dans la même langue que le client (français ou anglais).
- Tu signes toujours : John - Service Client - Max Sauveur

Ne mentionne jamais que tu es une IA.
Si tu as des infos de commande disponibles, utilise-les pour personnaliser ta réponse.

{KNOWLEDGE_BASE}"""

def generate_response(email_body, email_subject, customer_name, order_info=None):
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

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

    for attempt in range(3):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Rédige une réponse à cet email de support :\n\n{context}"
                }]
            )
            return message.content[0].text
        except anthropic.APIStatusError as e:
            if attempt < 2:
                time.sleep(3)
                continue
            return f"⚠️ Erreur API Claude ({e.status_code}). Rafraîchis la page."

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
        import json
        return json.loads(message.content[0].text.strip())
    except:
        return {"intent": "other", "address": None, "has_full_address": False}

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
