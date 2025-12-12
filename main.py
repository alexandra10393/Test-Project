import os
import time
import re
import json
import requests
import shutil
from datetime import datetime
from playwright.sync_api import sync_playwright
from urllib.parse import unquote
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===============================
# FUNZIONI DI SISTEMA E UTILITY
# ===============================

# Crea sessione con pooling per Telegram
def create_telegram_session():
    """Crea sessione HTTP con retry e connection pooling"""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=['POST', 'GET'],
        respect_retry_after_header=True
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=20,
        pool_maxsize=20,
        pool_block=False
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session

TELEGRAM_SESSION = create_telegram_session()

# File per tracciare fallimenti
FAILURE_FILE = "failure_tracker.json"
PERFORMANCE_FILE = "performance_log.txt"

# ===============================
# FUNZIONI DI TRACKING E MONITORAGGIO
# ===============================

def track_performance(phase, duration):
    """Logga performance per fase"""
    try:
        with open(PERFORMANCE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()}|{phase}|{duration:.2f}\n")
        
        # Mantieni file sotto 1000 righe
        with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        if len(lines) > 1000:
            with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-500:])
    except Exception as e:
        print(f"⚠️ Errore log performance: {e}")

def track_failure(site, status):
    """Traccia fallimenti consecutivi per ogni sito"""
    try:
        # Carica dati esistenti
        if os.path.exists(FAILURE_FILE):
            with open(FAILURE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {
                "failures": {},
                "consecutive_fails": {},
                "last_success": {}
            }
        
        now = datetime.now().isoformat()
        
        # Gestione fallimenti consecutivi
        if status in ["SUCCESS", "NO_STORIES", "SERVER_UNAVAILABLE"]:
            # Reset fallimenti consecutivi
            data["consecutive_fails"][site] = 0
            if status == "SUCCESS":
                data["last_success"][site] = now
        else:
            # Incrementa fallimenti consecutivi
            current_fails = data["consecutive_fails"].get(site, 0)
            data["consecutive_fails"][site] = current_fails + 1
            
            # Logga fallimento
            fail_key = f"{site}_{int(time.time())}"
            data["failures"][fail_key] = {
                "site": site, 
                "status": status, 
                "time": now,
                "consecutive": current_fails + 1
            }
        
        # Salva dati
        with open(FAILURE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        return data["consecutive_fails"].get(site, 0)
        
    except Exception as e:
        print(f"⚠️ Errore tracking fallimenti: {e}")
        return 0

def get_consecutive_fails(site):
    """Ottieni numero di fallimenti consecutivi per un sito"""
    try:
        if os.path.exists(FAILURE_FILE):
            with open(FAILURE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data["consecutive_fails"].get(site, 0)
    except:
        pass
    return 0

def retry_with_backoff(func, max_retries=2, *args, **kwargs):
    """Esegue retry con backoff esponenziale per errori transienti"""
    import time
    
    for attempt in range(max_retries + 1):
        try:
            start_time = time.time()
            result = func(*args, **kwargs)
            elapsed = time.time() - start_time
            
            if attempt > 0:
                print(f"✅ Retry {attempt} riuscito dopo {elapsed:.1f}s")
                
            return result
            
        except Exception as e:
            if attempt == max_retries:
                print(f"❌ Tutti i {max_retries} retry falliti")
                raise e
            
            wait_time = (2 ** attempt) + 1  # 2, 3, 5 secondi...
            print(f"⚠️ Tentativo {attempt + 1}/{max_retries} fallito. "
                  f"Ritento in {wait_time}s... ({str(e)[:80]})")
            time.sleep(wait_time)

def validate_links(links):
    """Valida che i link siano corretti e rimuovi malformati"""
    if not links:
        return []
    
    valid_links = []
    invalid_count = 0
    
    for link in links:
        # Controlli di validità
        if not link or not isinstance(link, str):
            invalid_count += 1
            continue
        
        link = link.strip()
        
        # Controlli stringa
        if len(link) < 20:
            invalid_count += 1
            continue
        
        # Controlla pattern comuni
        instagram_patterns = [
            "cdninstagram.com",
            "scontent.cdninstagram.com",
            "fbcdn.net",
            "instagram.f"
        ]
        
        if not any(pattern in link for pattern in instagram_patterns):
            print(f"⚠️ Link sospetto (non Instagram): {link[:60]}...")
            invalid_count += 1
            continue
        
        # Controlla caratteri strani
        if " " in link or "\n" in link or "\t" in link:
            # Prova a correggere
            link = link.replace("\n", "").replace("\t", "")
            if " " in link:
                invalid_count += 1
                continue
        
        # Controlla formato URL
        if not link.startswith(("http://", "https://")):
            invalid_count += 1
            continue
        
        valid_links.append(link)
    
    if invalid_count > 0:
        print(f"⚠️ Validazione: rimossi {invalid_count} link malformati")
    
    # Rimuovi duplicati mantenendo l'ordine
    seen = set()
    unique_links = []
    for link in valid_links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
    
    return unique_links

def check_disk_space(min_mb=5):
    """Controlla spazio disco disponibile"""
    try:
        total, used, free = shutil.disk_usage(".")
        free_mb = free // (1024 * 1024)
        
        if free_mb < min_mb:
            print(f"⚠️ ATTENZIONE: Poco spazio disco ({free_mb}MB su {total//(1024*1024)}MB)")
            return False
        return True
    except Exception as e:
        print(f"⚠️ Impossibile controllare spazio disco: {e}")
        return True  # Se non può controllare, procedi comunque

# ===============================
# CONFIGURAZIONE
# ===============================

# Usa le variabili d'ambiente (Secrets)
IG_USER = os.environ.get("IG_USER") 

KEYWORD_LIST = [
    os.environ.get("KEYWORD_1"),
    os.environ.get("KEYWORD_2"),
    os.environ.get("KEYWORD_3")
]

PAROLE_CHIAVE = [k for k in KEYWORD_LIST if k is not None and k.strip()] 
if not PAROLE_CHIAVE:
    PAROLE_CHIAVE = []
    
SOGLIA_ALLUVIONE = 150   
MAX_HISTORY = 300      

# RECUPERO CHIAVI
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OCR_KEY = os.environ.get("OCR_KEY", "")

# ===============================
# FUNZIONI CORE
# ===============================

def get_clean_id(url):
    """Estrai ID univoco dal link"""
    try:
        if "filename=" in url:
            return url.split("filename=")[1].split("&")[0]
        if "/media/" in url:
            return url.split("/media/")[1].split("?")[0]
        # Estrai ultima parte dell'URL
        clean = url.split("/")[-1].split("?")[0]
        return clean if len(clean) > 5 else url
    except:
        return url

def send_telegram(text, media_url=None, is_video=False):
    """Invia messaggio a Telegram con connection pooling"""
    api_url = f"https://api.telegram.org/bot{TOKEN}/"
    method = "sendVideo" if is_video else "sendPhoto"
    
    # Limita lunghezza testo per log
    log_text = text[:80] + "..." if len(text) > 80 else text
    print(f"✈️ Invio Telegram: {log_text}")
    
    try:
        if media_url:
            payload = {"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"}
            files_key = 'video' if is_video else 'photo'
            
            # Usa sessione con pooling
            response = TELEGRAM_SESSION.post(
                api_url + method, 
                data=payload, 
                params={files_key: media_url}, 
                timeout=120
            )
            response.raise_for_status()
            print(f"✅ Media inviato: {response.status_code}")
            
        else:
            response = TELEGRAM_SESSION.post(
                api_url + "sendMessage", 
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=30
            )
            response.raise_for_status()
            
    except Exception as e:
        print(f"❌ Errore invio Telegram: {e}")
        
        # Fallback con sessione semplice
        try:
            requests.post(
                api_url + "sendMessage", 
                json={
                    "chat_id": CHAT_ID, 
                    "text": f"{text}\n\n⚠️ Errore invio media\n📎 Link: {media_url if media_url else 'N/A'}",
                    "parse_mode": "HTML"
                },
                timeout=30
            )
            print("✅ Fallback inviato (solo testo)")
        except Exception as e2:
            print(f"❌ Fallback fallito: {e2}")

def ocr_scan(image_url):
    """Esegue OCR su immagine"""
    if not OCR_KEY: 
        return ""
    
    try:
        url = f"https://api.ocr.space/parse/imageurl?apikey={OCR_KEY}&url={image_url}&language=ita&isOverlayRequired=false"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        if data.get("ParsedResults"):
            text = data["ParsedResults"][0]["ParsedText"]
            return text.upper().strip()
            
    except requests.exceptions.Timeout:
        print("⚠️ OCR timeout (15s)")
    except Exception as e:
        print(f"⚠️ Errore OCR: {e}")
    
    return ""

# ===============================
# MOTORI DI SCRAPING
# ===============================

def check_storiesviewer(page):
    """Scarica storie da StoriesViewer.net con timeout dinamici"""
    print(f"⏩ Controllo StoriesViewer.net...")
    
    target_url = "https://storiesviewer.net/it/"
    links = []
    status = "UNKNOWN"
    error_details = ""
    start_time = time.time()
    
    # Calcola timeout dinamico basato su fallimenti consecutivi
    consecutive_fails = get_consecutive_fails("StoriesViewer")
    base_timeout = 60000  # 60 secondi base
    
    if consecutive_fails >= 2:
        # Riduci timeout per fallimenti consecutivi
        adjusted_timeout = max(30000, base_timeout - (consecutive_fails * 10000))
        print(f"⚠️ {consecutive_fails} fallimenti consecutivi, timeout ridotto a {adjusted_timeout/1000:.0f}s")
    else:
        adjusted_timeout = base_timeout
    
    try:
        # 1. Carica Homepage
        response = page.goto(target_url, timeout=adjusted_timeout, wait_until="domcontentloaded")
        
        if response.status != 200:
            status = "HTTP_ERROR"
            error_details = f"Status {response.status}"
            print(f"❌ StoriesViewer HTTP Error: {response.status}")
            track_failure("StoriesViewer", status)
            return links, status, error_details
        
        # Cookie banner
        try:
            page.click("button:has-text('Consent'), .fc-cta-consent", timeout=3000)
        except:
            pass
        
        # 2. Ricerca utente
        try:
            search_input = page.locator('input[name="url"], input[type="text"]').first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            search_input.fill(IG_USER)
            time.sleep(1)
            
            # Clicca lente di ricerca
            search_btn = page.locator('button[type="submit"], button:has(i), button.btn-default').first
            search_btn.wait_for(state="visible", timeout=5000)
            search_btn.click()
            print("🖱️ Lente cliccata!")
            
        except Exception as e:
            status = "INPUT_ERROR"
            error_details = f"Input non trovato: {str(e)[:100]}"
            print(f"⚠️ Errore fase ricerca: {e}")
            track_failure("StoriesViewer", status)
            return links, status, error_details

        # 3. Attesa risultati con gestione avanzata
        try:
            # Attesa caricamento iniziale
            try:
                page.wait_for_selector('text="Caricamento", text="Loading"', state='hidden', timeout=30000)
                print("✅ Caricamento completato.")
            except:
                print("ℹ️ Nessun indicatore di caricamento")
            
            # Controlla errore server
            try:
                page.wait_for_selector('text="Sorry, the server is temporarily unavailable"', timeout=5000)
                status = "SERVER_UNAVAILABLE"
                error_details = "Server temporaneamente non disponibile"
                print("ℹ️ StoriesViewer: Server temporaneamente non disponibile")
                track_failure("StoriesViewer", status)
                return links, status, error_details
            except:
                pass
            
            # Controlla "nessuna storia"
            try:
                page.wait_for_selector('text="No stories found", text="Nessuna storia", text="not found"', timeout=5000)
                status = "NO_STORIES"
                error_details = "Profilo senza storie o privato"
                print("ℹ️ StoriesViewer: Nessuna storia trovata")
                track_failure("StoriesViewer", status)
                return links, status, error_details
            except:
                pass
            
            # Attesa risultati principali
            page.wait_for_selector('a:has-text("Download HD"), .story-item, .stories-container', timeout=30000)
            print("✨ Elementi storie trovati!")
            
        except Exception as e:
            status = "TIMEOUT"
            error_details = f"Timeout caricamento: {str(e)[:100]}"
            print("⚠️ Timeout caricamento storie")
            # Continua comunque per estrazione
        
        # 4. Estrazione link
        raw_elements = page.query_selector_all('a[href*="media.php"]')
        
        for el in raw_elements:
            raw_url = el.get_attribute("href")
            if raw_url and "media=" in raw_url:
                try:
                    encoded_part = raw_url.split("media=")[1].split("&")[0]
                    clean_url = unquote(encoded_part)
                    if "cdninstagram.com" in clean_url:
                        links.append(clean_url)
                except:
                    continue
        
        # Validazione link
        links = validate_links(links)
        
        elapsed = time.time() - start_time
        
        if links:
            status = "SUCCESS"
            print(f"✅ StoriesViewer: {len(links)} link in {elapsed:.1f}s")
            track_failure("StoriesViewer", status)
            track_performance("StoriesViewer", elapsed)
        else:
            if status == "UNKNOWN":
                status = "NO_LINKS"
                print(f"⚠️ StoriesViewer: nessun link in {elapsed:.1f}s")
            track_failure("StoriesViewer", status)
            
        # Warning per lentezza
        if elapsed > 45000:  # 45 secondi
            print(f"⚠️ ATTENZIONE: StoriesViewer molto lento ({elapsed:.1f}s)")
            
        return links, status, error_details
        
    except Exception as e:
        status = "CRASH"
        error_details = f"Errore generale: {str(e)[:150]}"
        print(f"❌ Errore StoriesViewer: {e}")
        track_failure("StoriesViewer", status)
        return links, status, error_details

def check_iqsaved(page):
    """Scarica storie da IQSaved.com"""
    print(f"🔎 Controllo IQSAVED per {IG_USER}...")
    
    target_url = f"https://iqsaved.com/it/viewer/{IG_USER}/"
    links = []
    status = "UNKNOWN"
    error_details = ""
    start_time = time.time()
    
    # Timeout dinamico
    consecutive_fails = get_consecutive_fails("IQSaved")
    base_timeout = 60000
    
    if consecutive_fails >= 2:
        adjusted_timeout = max(30000, base_timeout - (consecutive_fails * 10000))
        print(f"⚠️ {consecutive_fails} fallimenti consecutivi, timeout ridotto a {adjusted_timeout/1000:.0f}s")
    else:
        adjusted_timeout = base_timeout
    
    try:
        response = page.goto(target_url, timeout=adjusted_timeout, wait_until="domcontentloaded")
        
        if response.status != 200:
            status = "HTTP_ERROR"
            error_details = f"Status {response.status}"
            print(f"❌ IQSaved HTTP Error: {response.status}")
            track_failure("IQSaved", status)
            return links, status, error_details
            
        time.sleep(3)
        
        # Cookie banner
        try:
            page.click("button.fc-cta-consent, button.primary-button, .cookie-agree", timeout=3000)
        except:
            pass

        # Scroll per caricare tutto
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        
        # Controlla "nessuna storia"
        page_content = page.content()
        
        if "No stories found" in page_content or "Nessuna storia" in page_content:
            status = "NO_STORIES"
            error_details = "Profilo senza storie o privato"
            print("ℹ️ IQSaved: Nessuna storia trovata")
            track_failure("IQSaved", status)
            return links, status, error_details
            
        # Estrazione link
        raw_links = re.findall(r'https://cdn\.iqsaved\.com/[^"\']+', page_content)
        links = [l.replace('&amp;', '&') for l in raw_links]
        
        # Validazione
        links = validate_links(links)
        
        elapsed = time.time() - start_time
        
        if links:
            status = "SUCCESS"
            print(f"✅ IQSaved: {len(links)} link in {elapsed:.1f}s")
            track_failure("IQSaved", status)
            track_performance("IQSaved", elapsed)
        else:
            status = "NO_LINKS"
            print(f"⚠️ IQSaved: nessun link in {elapsed:.1f}s")
            track_failure("IQSaved", status)
            
        return list(dict.fromkeys(links)), status, error_details
        
    except Exception as e:
        status = "CRASH"
        error_details = f"Errore: {str(e)[:150]}"
        print(f"❌ Errore IQSaved: {e}")
        track_failure("IQSaved", status)
        return links, status, error_details

# ===============================
# FUNZIONI DI RECOVERY
# ===============================

def safe_check_storiesviewer(page):
    """Wrapper con gestione errori robusta"""
    try:
        print("🔒 Esecuzione sicura StoriesViewer...")
        return check_storiesviewer(page)
    except Exception as e:
        error_msg = f"💀 CRASH GRAVE StoriesViewer: {str(e)[:200]}"
        print(error_msg)
        return [], "FATAL_ERROR", f"Crash completo: {str(e)[:100]}"

def safe_check_iqsaved(page):
    """Wrapper con gestione errori robusta"""
    try:
        print("🔒 Esecuzione sicura IQSaved...")
        return check_iqsaved(page)
    except Exception as e:
        error_msg = f"💀 CRASH GRAVE IQSaved: {str(e)[:200]}"
        print(error_msg)
        return [], "FATAL_ERROR", f"Crash completo: {str(e)[:100]}"

def emergency_cleanup(browser=None, context=None):
    """Pulizia di emergenza"""
    print("🆘 Cleanup di emergenza...")
    try:
        if context:
            context.close()
    except:
        pass
    try:
        if browser:
            browser.close()
    except:
        pass
    
    import gc
    gc.collect()
    print("✅ Cleanup completato")

# ===============================
# FUNZIONE PRINCIPALE
# ===============================

def run():
    """Funzione principale del bot"""
    print("🚀 Avvio Bot Ibrido Avanzato...")
    
    # Timer totale
    start_total = time.time()
    phase_timers = {
        "setup": 0,
        "storiesviewer": 0,
        "iqsaved": 0,
        "processing": 0,
        "telegram": 0
    }
    
    # Variabili per cleanup
    browser = None
    context = None
    
    try:
        # FASE 0: Setup
        phase_start = time.time()
        
        # Carica history
        seen_ids = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r", encoding="utf-8") as f:
                seen_ids = [line.strip() for line in f if line.strip()]
        
        updated_history = seen_ids.copy()
        ids_to_add = []
        
        phase_timers["setup"] = time.time() - phase_start
        
        # Avvia Playwright
        with sync_playwright() as p:
            # Browser ottimizzato per performance
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--disable-software-rasterizer',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-sync',
                    '--disable-translate',
                    '--disable-default-apps',
                    '--mute-audio',
                    '--no-first-run',
                    '--single-process',
                    '--disable-features=site-per-process,TranslateUI',
                    '--disable-blink-features=AutomationControlled',
                ]
            )
            
            # Context
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                viewport={'width': 1280, 'height': 800}
            )
            
            page = context.new_page()
            
            # FASE 1: StoriesViewer con retry
            phase_start = time.time()
            try:
                links_viewer, storiesviewer_status, storiesviewer_error = retry_with_backoff(
                    safe_check_storiesviewer, max_retries=1, page=page
                )
                all_links = links_viewer.copy()
                print(f"✅ StoriesViewer: {len(links_viewer)} link")
            except Exception as e:
                print(f"❌ StoriesViewer fallito anche dopo retry: {e}")
                links_viewer, storiesviewer_status, storiesviewer_error = [], "RETRY_FAILED", str(e)
                all_links = []
            
            phase_timers["storiesviewer"] = time.time() - phase_start
            
            # FASE 2: IQSaved (fallback)
            links_iq, iqsaved_status, iqsaved_error = [], "NOT_TESTED", ""
            phase_start = time.time()
            
            if len(all_links) < 5:
                print("\n=== FASE 2: IQSAVED (FALLBACK) ===")
                try:
                    links_iq, iqsaved_status, iqsaved_error = retry_with_backoff(
                        safe_check_iqsaved, max_retries=1, page=page
                    )
                    all_links.extend(links_iq)
                    print(f"✅ IQSaved: {len(links_iq)} link")
                except Exception as e:
                    print(f"❌ IQSaved fallito anche dopo retry: {e}")
                    links_iq, iqsaved_status, iqsaved_error = [], "RETRY_FAILED", str(e)
            
            phase_timers["iqsaved"] = time.time() - phase_start
            
            # Chiudi browser prima di continuare
            try:
                context.close()
                browser.close()
            except:
                pass
        
        # Processa link
        phase_start = time.time()
        
        # Validazione finale
        tutti_i_link = validate_links(all_links)
        print(f"📦 Totale link validi: {len(tutti_i_link)}")
        
        # Identifica nuove storie
        storie_da_processare = []
        for url in tutti_i_link:
            clean_id = get_clean_id(url)
            if clean_id and clean_id not in seen_ids:
                storie_da_processare.append({'url': url, 'id': clean_id})
        
        num_nuove = len(storie_da_processare)
        
        phase_timers["processing"] = time.time() - phase_start
        
        # FASE 3: Invio Telegram
        phase_start = time.time()
        
        if num_nuove > SOGLIA_ALLUVIONE:
            print(f"⚠️ FLOOD GUARD ({num_nuove} > {SOGLIA_ALLUVIONE}). Skip invio.")
            for item in storie_da_processare:
                ids_to_add.append(item['id'])
        elif num_nuove > 0:
            print(f"📨 Invio {num_nuove} nuove storie...")
            
            for i, item in enumerate(storie_da_processare):
                url = item['url']
                clean_id = item['id']
                
                # Determina tipo
                is_video = ".mp4" in url.lower() or "video" in url.lower()
                tipo = "VIDEO" if is_video else "FOTO"
                
                # Didascalia base
                dida = f"Storia {i+1}/{num_nuove}"
                
                # OCR per foto
                if tipo == "FOTO" and OCR_KEY:
                    txt = ocr_scan(url)
                    if txt:
                        found_keyword = next((k for k in PAROLE_CHIAVE if k in txt), None)
                        if found_keyword:
                            dida = f"Storia su {found_keyword.title()}"
                
                # Invio
                send_telegram(dida, url, is_video)
                ids_to_add.append(clean_id)
                
                # Sleep adattivo
                if i < len(storie_da_processare) - 1:
                    sleep_time = 2 + (i * 0.5)  # Incrementa progressivamente
                    sleep_time = min(sleep_time, 5)  # Max 5 secondi
                    time.sleep(sleep_time)
        
        phase_timers["telegram"] = time.time() - phase_start
        
        # Salva history
        if ids_to_add and check_disk_space():
            updated_history = seen_ids + ids_to_add
            
            # Limita history
            if len(updated_history) > MAX_HISTORY:
                updated_history = updated_history[-MAX_HISTORY:]
                print(f"📊 History troncata a {MAX_HISTORY} elementi")
            
            with open("history.txt", "w", encoding="utf-8") as f:
                for sid in updated_history:
                    if sid.strip():
                        f.write(f"{sid}\n")
            
            print(f"💾 History aggiornata: {len(updated_history)} elementi")
        
        # HEALTH CHECK INTELLIGENTE
        print("\n🔍 Health Check dettagliato...")
        
        send_alert = False
        alert_message = ""
        
        # Analisi StoriesViewer
        if storiesviewer_status == "HTTP_ERROR":
            send_alert = True
            alert_message += f"🔴 STORIESVIEWER DOWN: {storiesviewer_error}\n"
        elif storiesviewer_status == "CRASH":
            send_alert = True
            alert_message += f"🔴 STORIESVIEWER CRASH: {storiesviewer_error}\n"
        elif storiesviewer_status == "TIMEOUT":
            if iqsaved_status != "SUCCESS":
                send_alert = True
                alert_message += f"🟡 STORIESVIEWER TIMEOUT\n"
        elif storiesviewer_status == "INPUT_ERROR":
            send_alert = True
            alert_message += f"🔴 STORIESVIEWER LAYOUT CAMBIATO\n"
        
        # Analisi IQSaved
        if iqsaved_status == "HTTP_ERROR":
            send_alert = True
            alert_message += f"🔴 IQSAVED DOWN: {iqsaved_error}\n"
        elif iqsaved_status == "CRASH":
            send_alert = True
            alert_message += f"🔴 IQSAVED CRASH: {iqsaved_error}\n"
        
        # Invia alert se necessario
        if send_alert:
            alert_message += f"\n📊 CONTESTO:\n"
            alert_message += f"• Profilo: {IG_USER}\n"
            alert_message += f"• StoriesViewer: {storiesviewer_status}\n"
            alert_message += f"• IQSaved: {iqsaved_status}\n"
            alert_message += f"• Storie trovate: {len(tutti_i_link)}\n"
            alert_message += f"• Nuove storie: {num_nuove}\n"
            
            if len(tutti_i_link) == 0:
                alert_message += f"\n⚠️ CRITICO: Nessuna storia trovata!"
            else:
                alert_message += f"\n✅ Backup funzionante"
            
            send_telegram(f"🚨 ALLARME SITI\n\n{alert_message}")
        
        # Log status
        print(f"\n📋 Riepilogo Status:")
        print(f"   StoriesViewer: {storiesviewer_status} ({len(links_viewer)} link)")
        print(f"   IQSaved: {iqsaved_status} ({len(links_iq)} link)")
        
        # Allarme critico se entrambi down
        critical_statuses = ["NO_STORIES", "UNKNOWN", "SERVER_UNAVAILABLE"]
        if (len(tutti_i_link) == 0 and 
            storiesviewer_status not in critical_statuses and 
            iqsaved_status not in ["NO_STORIES", "UNKNOWN", "NOT_TESTED"]):
            
            print("🚨 ALLARME CRITICO: Nessun sito funziona!")
            send_telegram(
                f"🔴 CRITICO: Nessun sito funziona per {IG_USER}\n\n"
                f"StoriesViewer: {storiesviewer_status}\n"
                f"IQSaved: {iqsaved_status}\n\n"
                f"Intervento richiesto!"
            )
        
        # Analisi performance finale
        total_time = time.time() - start_total
        print(f"\n⏱️ ANALISI PERFORMANCE:")
        print(f"  Totale: {total_time:.1f}s")
        
        for phase, t in phase_timers.items():
            if t > 0:
                percent = (t / total_time) * 100
                print(f"  {phase}: {t:.1f}s ({percent:.1f}%)")
        
        # Warning se troppo lento
        if total_time > 120:
            slowest_phase = max(phase_timers, key=phase_timers.get)
            print(f"⚠️ AVVISO: Bot lento ({total_time:.1f}s)")
            print(f"   Fase più lenta: {slowest_phase} ({phase_timers[slowest_phase]:.1f}s)")
            
            if total_time > 180:  # 3 minuti
                send_telegram(
                    f"⚠️ Bot estremamente lento: {total_time:.1f}s\n"
                    f"Fase critica: {slowest_phase}\n"
                    f"Storie: {len(tutti_i_link)} trovate, {num_nuove} nuove"
                )
        
        print(f"\n✅ BOT COMPLETATO")
        print(f"📊 Riepilogo: {len(tutti_i_link)} storie trovate, {num_nuove} nuove")
        
    except Exception as e:
        print(f"💀 ERRORE FATALE nel run(): {e}")
        emergency_cleanup(browser, context)
        
        # Notifica errore fatale
        try:
            send_telegram(
                f"💀 ERRORE FATALE BOT\n\n"
                f"Errore: {str(e)[:200]}\n"
                f"Profilo: {IG_USER}\n"
                f"Time: {datetime.now().strftime('%H:%M:%S')}"
            )
        except:
            pass
        
        raise

# ===============================
# AVVIO
# ===============================

if __name__ == "__main__":
    run()
