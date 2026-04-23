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
    # Clear then type
    inp = page.locator('input[type="search"]').first
    inp.click()
    inp.fill('')
    inp.type(search_term, delay=50)
    page.keyboard.press('Enter')
    import time; time.sleep(2)
    # Wing defaults to "À traiter" tab — click "Toutes" to see all orders
    try:
        page.evaluate("""() => {
            const spans = Array.from(document.querySelectorAll('span'));
            const toutesTab = spans.find(s =>
                s.className && s.className.includes('justify-center') &&
                s.textContent.trim().startsWith('Toutes')
            );
            if (toutesTab) {
                const btn = toutesTab.closest('button') || toutesTab.closest('a') || toutesTab.parentElement;
                if (btn) btn.click(); else toutesTab.click();
            }
        }""")
        time.sleep(1)
    except Exception:
        pass
    # Wait for matching row
    try:
        page.wait_for_selector(f'tr:has-text("{search_term}")', timeout=6000)
    except Exception:
        pass

def _click_order_row(page, search_term):
    try:
        row = page.locator(f'tr:has-text("{search_term}")').first
        row.wait_for(timeout=3000)
        row.click()
    except Exception:
        page.locator('tbody tr').first.click()
    # Wait for detail panel title
    try:
        page.wait_for_selector('text=Détails de l\'expédition', timeout=6000)
    except Exception:
        pass

def _get_panel_text(page):
    # Panel opens as a side overlay — grab everything after "Détails de l'expédition"
    try:
        panel = page.locator('text=Détails de l\'expédition').locator('xpath=ancestor::*[4]')
        t = panel.text_content(timeout=3000).strip()
        if t and len(t) > 30:
            return t
    except Exception:
        pass
    for selector in ['[class*="detail"]', '[class*="panel"]', '[class*="drawer"]', '[class*="sidebar"]', '[class*="sheet"]']:
        try:
            el = page.locator(selector).last  # last = foreground panel
            t = el.text_content(timeout=2000).strip()
            if t and len(t) > 30:
                return t
        except Exception:
            continue
    return ''

def _no_results(page):
    body = page.inner_text('body').lower()
    return 'aucune donnée' in body or 'no result' in body or 'introuvable' in body


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
            # Extract row data (tracking number + URL from SUIVI ALLER column)
            row_text = ''
            tracking_number = ''
            tracking_url = ''
            try:
                row = page.locator(f'tr:has-text("{search_term}")').first
                row.wait_for(timeout=3000)
                row_text = row.inner_text().strip()
                # Extract tracking link from the row
                links = row.locator('a').all()
                for link in links:
                    href = link.get_attribute('href') or ''
                    txt = link.inner_text().strip()
                    if href and ('track' in href.lower() or 'suivi' in href.lower() or 'colissimo' in href.lower() or 'chronopost' in href.lower() or 'laposte' in href.lower()):
                        tracking_url = href
                        tracking_number = txt
                        break
                # Fallback: any external link in the row
                if not tracking_url:
                    for link in links:
                        href = link.get_attribute('href') or ''
                        if href.startswith('http') and 'wing' not in href.lower():
                            tracking_url = href
                            tracking_number = link.inner_text().strip()
                            break
            except Exception:
                pass
            _click_order_row(page, search_term)
            panel = _get_panel_text(page)
            browser.close()
            p.stop()
            result = f"Référence Wing : {search_term}\n"
            if tracking_number:
                result += f"Numéro de suivi : {tracking_number}\n"
            if tracking_url:
                result += f"Lien de suivi : {tracking_url}\n"
            if row_text:
                result += f"Ligne tableau Wing : {row_text}\n"
            if panel and len(panel) > 30:
                result += f"Panneau détail : {panel[:400]}"
            return result.strip()
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
    import time
    p, browser, page = _launch()
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    try:
        print(f"[Wing] Login...")
        _login(page)
        print(f"[Wing] Going to orders page...")
        page.goto(f"https://my.wing.eu/orders")
        page.wait_for_selector('input[type="search"]', timeout=10000)
        # Type search term
        inp = page.locator('input[type="search"]').first
        inp.click(); inp.fill(''); inp.type(str(order_number), delay=50)
        page.keyboard.press('Enter')
        print(f"[Wing] Typed search, waiting 3s...")
        time.sleep(3)
        print(f"[Wing] Page text after search: {page.inner_text('body')[:300]}")
        # Click Toutes tab explicitly
        print(f"[Wing] Looking for Toutes tab...")
        toutes_count = page.locator('text=Toutes').count()
        print(f"[Wing] Toutes elements found: {toutes_count}")
        try:
            # Target the tab specifically: span with class "flex justify-center flex-1" containing Toutes
            result = page.evaluate("""() => {
                // Find the tab span (class="flex justify-center flex-1") containing "Toutes"
                const spans = Array.from(document.querySelectorAll('span'));
                const toutesTab = spans.find(s =>
                    s.className && s.className.includes('justify-center') &&
                    s.textContent.trim().startsWith('Toutes')
                );
                if (toutesTab) {
                    // Click the parent button/link element
                    const btn = toutesTab.closest('button') || toutesTab.closest('a') || toutesTab.parentElement;
                    if (btn) { btn.click(); return 'clicked parent: ' + btn.tagName + ' ' + btn.className.substring(0, 50); }
                    toutesTab.click();
                    return 'clicked span directly';
                }
                return 'Toutes tab not found';
            }""")
            print(f"[Wing] JS Toutes click: {result}")
            time.sleep(2)
        except Exception as e:
            print(f"[Wing] JS Toutes click failed: {e}")
        print(f"[Wing] Page text after Toutes: {page.inner_text('body')[:300]}")
        # Wait for the specific order row
        print(f"[Wing] Waiting for order row with '{order_number}'...")
        order_row = page.locator(f'tr:has-text("{order_number}")').first
        try:
            order_row.wait_for(timeout=8000)
            print(f"[Wing] Order row found!")
        except Exception as e:
            print(f"[Wing] Order row not found after Toutes: {e}")
            page.screenshot(path="/tmp/wing_label_debug.png")
            raise Exception(f"Order {order_number} not found in Wing table")

        # Log the row's DOM to understand its structure
        try:
            row_info = page.evaluate(f"""() => {{
                const rows = document.querySelectorAll('tbody tr');
                for (const row of rows) {{
                    if (row.textContent.includes('{order_number}')) {{
                        return 'children: ' + Array.from(row.children).map(c =>
                            c.tagName + '[' + (c.className||'').substring(0,50) + '] children:' + c.children.length
                        ).join(' | ');
                    }}
                }}
                return 'row not found in JS';
            }}""")
            print(f"[Wing] Row structure: {row_info}")
        except Exception as e:
            print(f"[Wing] Row inspect failed: {e}")

        # Hover over the row to reveal the checkbox (Wing uses CSS :hover)
        order_row.hover()
        time.sleep(0.5)

        # Try clicking the first td (leftmost cell = where checkbox is)
        checked = False
        try:
            first_td = order_row.locator('td').first
            first_td.click()
            print(f"[Wing] Clicked first td of order row")
            time.sleep(0.8)
            aff_count = page.locator('text=Affranchissement').count()
            print(f"[Wing] After td click — Affranchissement: {aff_count}")
            if aff_count > 0:
                checked = True
        except Exception as e:
            print(f"[Wing] First td click failed: {e}")

        # Fallback: JS mouseover + click on first cell
        if not checked:
            try:
                result = page.evaluate(f"""() => {{
                    const rows = document.querySelectorAll('tbody tr');
                    for (const row of rows) {{
                        if (row.textContent.includes('{order_number}')) {{
                            const td = row.querySelector('td');
                            if (!td) return 'no td';
                            td.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));
                            td.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                            td.click();
                            return 'js clicked td';
                        }}
                    }}
                    return 'row not found';
                }}""")
                print(f"[Wing] JS mouseover+click: {result}")
                time.sleep(0.8)
                aff_count = page.locator('text=Affranchissement').count()
                print(f"[Wing] After JS click — Affranchissement: {aff_count}")
                if aff_count > 0:
                    checked = True
            except Exception as e:
                print(f"[Wing] JS click failed: {e}")
        # Open Affranchissement dropdown
        print(f"[Wing] Waiting for Affranchissement button (appears after checkbox)...")
        try:
            page.wait_for_selector('text=Affranchissement', timeout=8000)
            print(f"[Wing] Affranchissement found, clicking...")
        except Exception:
            print(f"[Wing] Affranchissement not found after 8s — taking screenshot")
            page.screenshot(path="/tmp/wing_label_debug.png")
            raise Exception("Affranchissement button not visible — checkbox may not be checked")
        page.click('text=Affranchissement')
        time.sleep(0.5)
        # Click specifically "Créer et générer l'étiquette retour"
        print(f"[Wing] Waiting for dropdown option...")
        try:
            page.wait_for_selector('text=Créer et générer', timeout=5000)
        except Exception:
            print(f"[Wing] 'Créer et générer' not found — taking screenshot")
            page.screenshot(path="/tmp/wing_label_debug.png")
            raise
        print(f"[Wing] Clicking 'Créer et générer'...")
        with page.expect_download(timeout=30000) as download_info:
            page.locator('text=Créer et générer').first.click()
        print(f"[Wing] Download triggered, reading PDF...")
        pdf_bytes = open(download_info.value.path(), 'rb').read()
        print(f"[Wing] PDF ready: {len(pdf_bytes)} bytes")
        context.close()
        browser.close()
        p.stop()
        return pdf_bytes
    except Exception as e:
        print(f"[Wing] Erreur génération étiquette: {e}")
        try:
            page.screenshot(path="/tmp/wing_label_debug.png")
        except Exception:
            pass
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
