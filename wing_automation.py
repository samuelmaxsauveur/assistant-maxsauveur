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
    """Search Wing for order_number + '_bis' (repair duplicate) and return status + details."""
    order_number = str(order_number).lstrip('#')
    # Try _bis first (standard repair naming), then fallbacks
    variants = [f"{order_number}_bis", f"{order_number}_REPARATION", f"{order_number}_reparation", f"{order_number}_réparation", f"{order_number}_Reparation", order_number]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        try:
            _login(page)

            for search_term in variants:
                page.goto(f"{WING_URL}/orders")
                page.wait_for_load_state('networkidle')
                time.sleep(2)

                search_input = page.locator('input[type="search"], input[placeholder*="Recherche"], input[placeholder*="recherche"]').first
                search_input.fill(search_term)
                page.keyboard.press('Enter')
                time.sleep(4)

                body_text = page.inner_text('body')
                if any(x in body_text.lower() for x in ['aucun', 'no result', 'introuvable', '0 résultat']):
                    continue  # Try next variant

                # Found results — click first row to open detail panel
                try:
                    page.locator('tbody tr').first.click()
                    time.sleep(3)
                except Exception:
                    pass

                # Grab full detail panel text
                panel_text = ''
                for selector in ['[class*="detail"]', '[class*="panel"]', '[class*="drawer"]', '[class*="sidebar"]']:
                    try:
                        el = page.locator(selector).first
                        if el.is_visible(timeout=1500):
                            t = el.text_content().strip()
                            if t and len(t) > 20:
                                panel_text = t
                                break
                    except Exception:
                        continue

                browser.close()
                result = panel_text if panel_text else body_text[:1500]
                return f"[Trouvé sous '{search_term}']\n{result[:1000]}"

            browser.close()
            return None

        except Exception as e:
            print(f"Erreur vérification statut Wing: {e}")
            try:
                browser.close()
            except Exception:
                pass
            return None


def get_relay_point_from_wing(order_number):
    """Open a Wing order and extract the relay point name + address from the side panel."""
    order_number = str(order_number).lstrip('#')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        try:
            _login(page)
            page.goto(f"{WING_URL}/orders")
            page.wait_for_load_state('networkidle')
            time.sleep(2)

            # Search by order reference
            search_input = page.locator('input[type="search"], input[placeholder*="Recherche"], input[placeholder*="recherche"]').first
            search_input.fill(f'#{order_number}')
            page.keyboard.press('Enter')
            time.sleep(4)

            # Click the first order row to open the detail panel
            page.locator('tbody tr').first.click()
            time.sleep(3)

            # Wait for the detail panel "Détails de l'expédition"
            try:
                page.wait_for_selector('text=Détails de l\'expédition', timeout=5000)
            except Exception:
                pass
            time.sleep(1)

            # The relay point name appears as bold text below "Livraison estimée"
            # Structure: Chrono 2Shop > Livraison estimée X > [RELAY NAME in bold] > [code]
            panel_text = ''
            for selector in [
                '[class*="detail"] strong',
                '[class*="panel"] strong',
                '[class*="drawer"] strong',
                '[class*="sidebar"] strong',
                'strong',
            ]:
                try:
                    els = page.locator(selector).all()
                    for el in els:
                        t = el.text_content().strip()
                        # Relay names are typically ALL CAPS and more than 5 chars
                        if t and t.isupper() and len(t) > 5:
                            # Get sibling text (the relay code below)
                            parent_text = el.locator('xpath=..').text_content().strip()
                            browser.close()
                            return f"{t}\n{parent_text}".strip()
                except Exception:
                    continue

            # Fallback: grab full panel text and return it
            for selector in ['[class*="detail"]', '[class*="panel"]', '[class*="drawer"]', '[class*="sidebar"]']:
                try:
                    el = page.locator(selector).first
                    if el.is_visible(timeout=1000):
                        panel_text = el.text_content().strip()
                        if panel_text and len(panel_text) > 20:
                            browser.close()
                            return panel_text[:800]
                except Exception:
                    continue

            # Last fallback: return visible body text
            browser.close()
            return page.inner_text('body')[:1500]

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
