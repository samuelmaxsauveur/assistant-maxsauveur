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

def generate_return_label(order_number):
    """Generate a return label in Wing and return PDF bytes, or None on failure."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_viewport_size({'width': 1280, 'height': 900})
        try:
            _login(page)
            page.goto(f"{WING_URL}/orders")
            page.wait_for_load_state('networkidle')
            time.sleep(2)

            # Search for the order
            page.fill('input[type="search"]', str(order_number))
            page.keyboard.press('Enter')
            time.sleep(4)

            # Select order checkbox (leftmost column)
            page.locator('input[type="checkbox"]').first.click()
            time.sleep(1)

            # Bottom action bar: click Affranchissement then Créer
            page.click('text=Affranchissement')
            time.sleep(2)
            page.click('text=Créer')
            time.sleep(5)

            # Download the generated label
            with page.expect_download(timeout=30000) as download_info:
                page.locator('text=Télécharger').first.click()
            download = download_info.value
            pdf_bytes = open(download.path(), 'rb').read()

            context.close()
            browser.close()
            return pdf_bytes

        except Exception as e:
            print(f"Erreur génération étiquette Wing: {e}")
            try:
                context.close()
                browser.close()
            except Exception:
                pass
            return None


def check_repair_status(order_number):
    """Search Wing for order_number + '_réparation' and return the status string, or None if not found."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_viewport_size({'width': 1280, 'height': 900})
        try:
            _login(page)
            page.goto(f"{WING_URL}/orders")
            page.wait_for_load_state('networkidle')
            time.sleep(2)

            page.fill('input[type="search"]', f"{order_number}_réparation")
            page.keyboard.press('Enter')
            time.sleep(4)

            body_text = page.inner_text('body').lower()
            if 'aucun' in body_text or 'no result' in body_text or 'introuvable' in body_text:
                browser.close()
                return None

            # Try common Wing status selectors
            for selector in ['[class*="status"]', '[class*="statut"]', 'td:nth-child(4)']:
                try:
                    el = page.locator(selector).first
                    if el.is_visible(timeout=2000):
                        status = el.text_content().strip()
                        if status:
                            browser.close()
                            return status
                except Exception:
                    continue

            browser.close()
            return "En cours de traitement"

        except Exception as e:
            print(f"Erreur vérification statut Wing: {e}")
            try:
                browser.close()
            except Exception:
                pass
            return None


def get_relay_point_from_wing(order_number):
    """Open a Wing order and extract the relay point address. Returns a string or None."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        try:
            _login(page)
            page.goto(f"{WING_URL}/orders")
            page.wait_for_load_state('networkidle')
            time.sleep(2)

            # Search for the order
            page.fill('input[type="search"]', str(order_number))
            page.keyboard.press('Enter')
            time.sleep(4)

            # Click the order row to open details
            page.locator('tbody tr').first.click()
            page.wait_for_load_state('networkidle')
            time.sleep(3)

            # Take a screenshot for debugging if needed
            # Try to find relay point address in the page
            full_text = page.inner_text('body')

            # Look for relay-point related elements
            relay_selectors = [
                '[class*="relay"]',
                '[class*="point-relais"]',
                '[class*="pickup"]',
                '[class*="livraison"]',
                '[class*="delivery-address"]',
                '[class*="shipping-address"]',
            ]
            for selector in relay_selectors:
                try:
                    el = page.locator(selector).first
                    if el.is_visible(timeout=1500):
                        text = el.text_content().strip()
                        if text and len(text) > 5:
                            browser.close()
                            return text
                except Exception:
                    continue

            # Fallback: grab all visible address-like blocks
            for selector in ['address', '[class*="address"]', '[class*="adresse"]']:
                try:
                    els = page.locator(selector).all()
                    for el in els:
                        text = el.text_content().strip()
                        if text and len(text) > 10:
                            browser.close()
                            return text
                except Exception:
                    continue

            browser.close()
            # Return raw page dump so Claude can still try to extract it
            return full_text[:2000] if full_text else None

        except Exception as e:
            print(f"Erreur récupération point relais Wing: {e}")
            try:
                browser.close()
            except Exception:
                pass
            return None


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
