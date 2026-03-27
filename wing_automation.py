import os
import time
from playwright.sync_api import sync_playwright

WING_URL = "https://my.wing.eu"

def _login(page):
    email = os.getenv('WING_EMAIL')
    password = os.getenv('WING_PASSWORD')
    page.goto(f"{WING_URL}/login")
    page.wait_for_load_state('networkidle')
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state('networkidle')
    time.sleep(3)

def change_relay_point(order_number, new_address):
    """Change le point relais d'une commande Wing. Retourne True si succès."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        try:
            _login(page)

            # Aller dans Expéditions
            page.goto(f"{WING_URL}/orders")
            page.wait_for_load_state('networkidle')
            time.sleep(2)

            # Rechercher la commande
            page.fill('input[type="search"]', str(order_number))
            page.keyboard.press('Enter')
            time.sleep(4)

            # Cliquer sur la commande
            page.locator(f'text={order_number[:15]}').first.click()
            time.sleep(3)

            # Cliquer sur Modifier (mode d'expédition)
            page.click('text=Modifier')
            time.sleep(2)

            # Ouvrir la sélection de point relais
            page.click('text=Sélectionner le point relais')
            time.sleep(3)

            # Entrer l'adresse
            page.fill('input[placeholder="Saisissez votre adresse"]', new_address)
            time.sleep(3)

            # Sélectionner la première suggestion
            page.locator('[class*="suggestion"], [class*="autocomplete"] li').first.click()
            time.sleep(4)

            # Cliquer sur SUPER U ou le premier point relais qui correspond
            # Chercher le point relais par nom si mentionné dans l'adresse
            relay_name = None
            for keyword in ['SUPER U', 'Carrefour', 'Relay', 'Pickup', 'Chronopost']:
                if keyword.upper() in new_address.upper():
                    relay_name = keyword
                    break

            if relay_name:
                page.click(f'text={relay_name}')
            else:
                # Prendre le premier point relais listé
                page.locator('[class*="relay"], [class*="point-relais"], [class*="pickup"]').first.click()
            time.sleep(2)

            # Confirmer
            page.click('text=Me livrer à cette adresse')
            time.sleep(2)
            page.click('text=Valider')
            time.sleep(3)

            browser.close()
            return True

        except Exception as e:
            print(f"Erreur Wing: {e}")
            browser.close()
            return False
