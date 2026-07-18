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

def _dismiss_notifications(page):
    """Close any Wing notification banners that block clicks."""
    import time
    try:
        page.keyboard.press('Escape')
        time.sleep(0.3)
    except Exception:
        pass
    # Click any close/dismiss button on notifications
    for selector in ['button[aria-label="Close"]', 'button[aria-label="Fermer"]',
                     '[class*="close"]', '[class*="dismiss"]', 'button svg']:
        try:
            els = page.locator(selector).all()
            for el in els[:3]:
                if el.is_visible():
                    el.click()
                    time.sleep(0.2)
        except Exception:
            pass


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
            // Find button whose exact text matches "Toutes (N)" — the tab button
            const buttons = Array.from(document.querySelectorAll('button'));
            const toutesBtn = buttons.find(btn => /^Toutes \(\d+\)$/.test(btn.textContent.trim()));
            if (toutesBtn) toutesBtn.click();
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


def get_order_tracking(order_number):
    """
    Search Wing for an order and return the tracking link + status.
    Uses the same search pattern as generate_return_label (which works).
    """
    import time
    order_number = str(order_number).lstrip('#')
    p, browser, page = _launch()
    try:
        _login(page)
        print(f"[Wing] Searching order #{order_number}...")
        page.goto(f"{WING_URL}/orders")
        page.wait_for_selector('input[type="search"]', timeout=10000)

        inp = page.locator('input[type="search"]').first
        inp.click()
        inp.fill('')
        inp.type(order_number, delay=50)
        page.keyboard.press('Enter')
        time.sleep(2)

        # Click "Toutes" tab via data-testid (reliable method used in generate_return_label)
        try:
            result = page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="order-tab-all"]');
                if (btn) { btn.click(); return 'clicked'; }
                return 'not found';
            }""")
            print(f"[Wing] Toutes tab: {result}")
            time.sleep(3)
            page.wait_for_selector('tbody tr', timeout=8000)
            time.sleep(1)
        except Exception as e:
            print(f"[Wing] Toutes tab error: {e}")

        # Log page state for debugging
        page_text = page.inner_text('body')
        print(f"[Wing] Page text (first 400): {page_text[:400]}")

        # Find the row (try exact match first, then first available row)
        tracking_number = ''
        tracking_url = ''
        row_text = ''
        try:
            rows = page.locator('tbody tr').all()
            print(f"[Wing] Found {len(rows)} rows")
            row = None
            for r in rows:
                txt = r.inner_text().strip()
                if order_number in txt:
                    row = r
                    row_text = txt
                    break
            if not row and rows:
                row = rows[0]
                row_text = rows[0].inner_text().strip()
                print(f"[Wing] No exact match, using first row: {row_text[:100]}")

            if row:
                # Extract all external links from the row
                for link in row.locator('a').all():
                    href = link.get_attribute('href') or ''
                    txt = link.inner_text().strip()
                    if href.startswith('http') and 'wing' not in href.lower():
                        tracking_url = href
                        tracking_number = txt
                        print(f"[Wing] Tracking link found: {href}")
                        break
        except Exception as e:
            print(f"[Wing] Row extraction error: {e}")

        # Open detail panel for status info
        panel = ''
        try:
            if page.locator('tbody tr').count() > 0:
                page.locator('tbody tr').first.click()
                time.sleep(2)
                panel = _get_panel_text(page)
        except Exception as e:
            print(f"[Wing] Panel error: {e}")

        browser.close()
        p.stop()

        if not row_text and not tracking_url and not panel:
            print(f"[Wing] Nothing found for #{order_number}")
            return None

        result = f"Commande : #{order_number}\n"
        if tracking_number:
            result += f"Numéro de suivi : {tracking_number}\n"
        if tracking_url:
            result += f"Lien de suivi : {tracking_url}\n"
        if row_text:
            result += f"Ligne Wing : {row_text}\n"
        if panel and len(panel) > 20:
            result += f"Détail : {panel[:400]}"
        return result.strip()

    except Exception as e:
        print(f"[Wing] get_order_tracking error: {e}")
        try:
            browser.close()
            p.stop()
        except Exception:
            pass
        return None


def generate_return_label(order_number):
    """Generate a return label in Wing and return PDF bytes, or None on failure."""
    import time
    order_number = str(order_number).lstrip('#')
    p, browser, page = _launch()
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    try:
        print(f"[Wing] Login...")
        _login(page)
        print(f"[Wing] Going to orders page...")
        page.goto(f"https://my.wing.eu/orders")
        page.wait_for_selector('input[type="search"]', timeout=10000)

        # Search for the order
        inp = page.locator('input[type="search"]').first
        inp.click()
        inp.fill('')
        inp.type(order_number, delay=50)
        page.keyboard.press('Enter')
        time.sleep(2)

        # Click the "Toutes" tab using JS (bypass overlay that intercepts pointer events)
        print(f"[Wing] Clicking Toutes tab via JS...")
        try:
            result = page.evaluate("""() => {
                const btn = document.querySelector('[data-testid="order-tab-all"]');
                if (btn) { btn.click(); return 'clicked'; }
                return 'not found';
            }""")
            print(f"[Wing] Toutes tab JS click: {result}")
            time.sleep(3)
            # Wait for table to populate
            page.wait_for_selector('tbody tr', timeout=8000)
            time.sleep(1)
        except Exception as e:
            print(f"[Wing] Toutes tab click failed: {e}")

        # Wait for order row
        print(f"[Wing] Waiting for order row '{order_number}'...")
        order_row = page.locator(f'tr:has-text("{order_number}")').first
        try:
            order_row.wait_for(timeout=12000)
            print(f"[Wing] Order row found!")
        except Exception as e:
            print(f"[Wing] Order row not found: {e}")
            page.screenshot(path="/tmp/wing_label_debug.png")
            raise Exception(f"Order {order_number} not found in Wing table")

        # Intercept S3 label URLs from all network responses
        s3_url_holder = []

        def on_s3_response(response):
            try:
                url = response.url
                ct = response.headers.get('content-type', '')
                if ('wing-labelling-system.s3' in url or
                        ('s3' in url and 'LABEL' in url.upper()) or
                        'pdf' in ct.lower() or url.lower().endswith('.pdf')):
                    print(f"[Wing] S3/PDF intercepted: {url}")
                    s3_url_holder.append(url)
            except Exception:
                pass

        page.on('response', on_s3_response)

        # Step 1: click checkbox
        print(f"[Wing] Clicking checkbox...")
        order_row.hover()
        time.sleep(0.5)
        checkbox = order_row.locator('input.checkbox-cell')
        try:
            checkbox.wait_for(timeout=3000)
            checkbox.click(force=True)
            print(f"[Wing] Checkbox clicked")
        except Exception as e:
            print(f"[Wing] Checkbox click failed: {e}, trying bbox...")
            first_td = order_row.locator('td').first
            bbox = first_td.bounding_box()
            if bbox:
                page.mouse.click(bbox['x'] + 12, bbox['y'] + bbox['height'] / 2)
            else:
                first_td.click(force=True)
        time.sleep(1)

        # Step 2: Affranchissement → sous-menu → Créer et générer l'étiquette retour
        label_url = None
        print(f"[Wing] Waiting for Affranchissement...")
        try:
            page.wait_for_selector('text=Affranchissement', timeout=6000)
        except Exception:
            page.screenshot(path="/tmp/wing_label_debug.png")
            raise Exception("Affranchissement not visible after checkbox click")
        page.locator('text=Affranchissement').click()
        time.sleep(1)

        # Step 3: click the label option in the submenu
        option_to_click = None
        for opt in ["Créer et générer l'étiquette retour", 'Créer et générer', 'Réimprimer']:
            if page.locator(f'text={opt}').count() > 0:
                option_to_click = opt
                break
        if not option_to_click:
            page.screenshot(path="/tmp/wing_label_debug.png")
            print(f"[Wing] Submenu body: {page.inner_text('body')[:600]}")
            raise Exception("Aucune option d'étiquette trouvée dans Affranchissement")
        print(f"[Wing] Clicking '{option_to_click}' (no_wait_after)...")
        page.locator(f'text={option_to_click}').first.click(no_wait_after=True)
        time.sleep(2)

        # Step 4: select pickup/depot if Wing shows a location submenu
        # Wing requires selecting a depot (e.g. "Aix-Pickup", "Paris-Commines") before generating the label
        depot_keywords = ['Pickup', 'Commines', 'Aix', 'Paris', 'Lyon', 'Bordeaux', 'Marseille', 'Entrepôt', 'Dépôt']
        depot_clicked = False
        for kw in depot_keywords:
            loc = page.locator(f'text={kw}')
            if loc.count() > 0:
                print(f"[Wing] Selecting depot: '{kw}'...")
                loc.first.click(no_wait_after=True)
                depot_clicked = True
                time.sleep(2)
                break
        if depot_clicked:
            # After depot selection, click confirm/generate if a button appears
            for confirm_text in ['Générer', 'Valider', 'Confirmer', 'OK']:
                if page.locator(f'text={confirm_text}').count() > 0:
                    print(f"[Wing] Confirming with '{confirm_text}'...")
                    page.locator(f'text={confirm_text}').first.click(no_wait_after=True)
                    time.sleep(2)
                    break

        # Step 5: wait for S3 URL from network interception
        print(f"[Wing] Waiting for S3 URL from network...")
        time.sleep(10)  # give Wing enough time to generate and return the label URL

        # Step 4: search for S3 URL in intercepted responses or page HTML
        if not s3_url_holder:
            import re as _re
            page_html = page.content()
            matches = _re.findall(r'https://wing-labelling-system\.s3[^\s"\'<>]+', page_html)
            if matches:
                s3_url_holder.extend(matches)
                print(f"[Wing] S3 URLs in HTML: {matches}")

        if s3_url_holder:
            label_url = s3_url_holder[0]
            print(f"[Wing] Label URL: {label_url}")

        if not label_url:
            page.screenshot(path="/tmp/wing_label_debug.png")
            print(f"[Wing] Page state: {page.inner_text('body')[:800]}")
            raise Exception("URL étiquette non trouvée")

        context.close()
        browser.close()
        p.stop()
        return label_url

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
