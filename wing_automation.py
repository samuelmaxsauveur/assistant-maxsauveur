import os
from playwright.sync_api import sync_playwright

WING_URL = "https://my.wing.eu"

def _launch():
    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
    )
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    return p, browser, page

def _login(page):
    email = os.getenv('WING_EMAIL')
    password = os.getenv('WING_PASSWORD')
    page.goto(f"{WING_URL}/login")
    page.wait_for_selector('input[name="email"]')
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    # Wait for redirect away from login
    page.wait_for_url(lambda url: '/login' not in url, timeout=15000)

def _search_order(page, search_term):
    page.goto(f"{WING_URL}/orders")
    page.wait_for_selector('input[type="search"]', timeout=10000)
    page.fill('input[type="search"]', search_term)
    page.keyboard.press('Enter')
    # Wait for table to update
    try:
        page.wait_for_selector('tbody tr', timeout=8000)
    except Exception:
        pass

def _click_order_row(page, search_term):
    try:
        row = page.locator(f'tr:has-text("{search_term}")').first
        row.wait_for(timeout=3000)
        row.click()
    except Exception:
        page.locator('tbody tr').first.click()
    # Wait for detail panel
    try:
        page.wait_for_selector('text=Détails de l\'expédition', timeout=5000)
    except Exception:
        pass

def _get_panel_text(page):
    for selector in ['[class*="detail"]', '[class*="panel"]', '[class*="drawer"]', '[class*="sidebar"]']:
        try:
            el = page.locator(selector).first
            t = el.text_content(timeout=2000).strip()
            if t and len(t) > 20:
                return t
        except Exception:
            continue
    return page.inner_text('body')[:1500]

def _no_results(page):
    body = page.inner_text('body').lower()
    return any(x in body for x in ['aucun', 'no result', 'introuvable', '0 résultat'])


def check_repair_status(order_number):
    """Search Wing for repair order and return status + details."""
    order_number = str(order_number).lstrip('#')
    variants = [
        f"{order_number}_REPARATION",
        f"{order_number}_bis",
        f"{order_number}_reparation",
        f"{order_number}_réparation",
    ]
    p, browser, page = _launch()
    try:
        _login(page)
        for search_term in variants:
            _search_order(page, search_term)
            if _no_results(page):
                continue
            _click_order_row(page, search_term)
            panel = _get_panel_text(page)
            browser.close()
            p.stop()
            return f"[Trouvé : '{search_term}']\n{panel[:1000]}"
        browser.close()
        p.stop()
        return None
    except Exception as e:
        print(f"Erreur check_repair_status: {e}")
        try:
            browser.close()
            p.stop()
        except Exception:
            pass
        return None


def get_relay_point_from_wing(order_number):
    """Open a Wing order and extract the relay point from the detail panel."""
    order_number = str(order_number).lstrip('#')
    p, browser, page = _launch()
    try:
        _login(page)
        _search_order(page, order_number)
        if _no_results(page):
            browser.close()
            p.stop()
            return None
        _click_order_row(page, order_number)

        # Look for relay name (ALL CAPS bold element)
        try:
            for el in page.locator('strong').all():
                t = el.text_content(timeout=1000).strip()
                if t and t.isupper() and len(t) > 5:
                    parent = el.locator('xpath=..').text_content(timeout=1000).strip()
                    browser.close()
                    p.stop()
                    return f"{t}\n{parent}".strip()
        except Exception:
            pass

        panel = _get_panel_text(page)
        browser.close()
        p.stop()
        return panel[:800]

    except Exception as e:
        print(f"Erreur get_relay_point: {e}")
        try:
            browser.close()
            p.stop()
        except Exception:
            pass
        return None


def generate_return_label(order_number):
    """Generate a return label in Wing and return PDF bytes, or None on failure."""
    p, browser, page = _launch()
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    try:
        _login(page)
        _search_order(page, str(order_number))
        page.locator('input[type="checkbox"]').first.click()
        page.click('text=Affranchissement')
        page.wait_for_selector('text=Créer', timeout=5000)
        page.click('text=Créer')
        with page.expect_download(timeout=30000) as download_info:
            page.locator('text=Télécharger').first.click()
        pdf_bytes = open(download_info.value.path(), 'rb').read()
        context.close()
        browser.close()
        p.stop()
        return pdf_bytes
    except Exception as e:
        print(f"Erreur génération étiquette Wing: {e}")
        try:
            context.close()
            browser.close()
            p.stop()
        except Exception:
            pass
        return None


def change_relay_point(order_number, new_address):
    """Change le point relais d'une commande Wing. Retourne True si succès."""
    p, browser, page = _launch()
    try:
        _login(page)
        _search_order(page, str(order_number))
        _click_order_row(page, str(order_number))
        page.click('text=Modifier')
        page.wait_for_selector('text=Sélectionner le point relais', timeout=5000)
        page.click('text=Sélectionner le point relais')
        page.wait_for_selector('input[placeholder="Saisissez votre adresse"]', timeout=5000)
        page.fill('input[placeholder="Saisissez votre adresse"]', new_address)
        page.wait_for_selector('[class*="suggestion"], [class*="autocomplete"] li', timeout=5000)
        page.locator('[class*="suggestion"], [class*="autocomplete"] li').first.click()

        relay_name = next((k for k in ['SUPER U', 'Carrefour', 'Relay', 'Pickup', 'Chronopost']
                           if k.upper() in new_address.upper()), None)
        if relay_name:
            page.click(f'text={relay_name}')
        else:
            page.locator('[class*="relay"], [class*="point-relais"], [class*="pickup"]').first.click()

        page.click('text=Me livrer à cette adresse')
        page.wait_for_selector('text=Valider', timeout=5000)
        page.click('text=Valider')
        page.wait_for_load_state('networkidle', timeout=10000)
        browser.close()
        p.stop()
        return True
    except Exception as e:
        print(f"Erreur change_relay_point: {e}")
        try:
            browser.close()
            p.stop()
        except Exception:
            pass
        return False
