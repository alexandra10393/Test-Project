import os
import time
import re
import requests
from playwright.sync_api import sync_playwright
from urllib.parse import unquote
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Crea sessione con pooling
def create_telegram_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=['POST', 'GET']
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
        pool_block=False
    )
    session.mount('https://', adapter)
    return session

TELEGRAM_SESSION = create_telegram_session()

# --- CONFIGURAZIONE ---
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

def get_clean_id(url):
    # Cerca di estrarre un ID univoco dal link (funziona per entrambi i siti)
    try:
        if "filename=" in url:
            return url.split("filename=")[1].split("&")[0]
        if "/media/" in url:
            return url.split("/media/")[1].split("?")[0]
        return url.split("/")[-1].split("?")[0]
    except:
        return url

def send_telegram(text, media_url=None, is_video=False):
    api_url = f"https://api.telegram.org/bot{TOKEN}/"
    method = "sendVideo" if is_video else "sendPhoto"
    print(f"✈️ Invio Telegram: {text}")
    
    try:
        if media_url:
            payload = {"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"}
            files_key = 'video' if is_video else 'photo'
            # Usa la sessione con pooling
            response = TELEGRAM_SESSION.post(
                api_url + method, 
                data=payload, 
                params={files_key: media_url}, 
                timeout=120
            )
            response.raise_for_status()
        else:
            response = TELEGRAM_SESSION.post(
                api_url + "sendMessage", 
                json={"chat_id": CHAT_ID, "text": text},
                timeout=30
            )
            response.raise_for_status()
            
    except Exception as e:
        print(f"❌ Errore invio Telegram: {e}")
        # Fallback con sessione nuova
        try:
            requests.post(api_url + "sendMessage", 
                         json={"chat_id": CHAT_ID, 
                               "text": text + f"\n\n(Link: {media_url if media_url else 'N/A'})"},
                         timeout=30)
        except:
            print(f"❌ Fallback anche fallito")

def ocr_scan(image_url):
    if not OCR_KEY: return ""
    try:
        url = f"https://api.ocr.space/parse/imageurl?apikey={OCR_KEY}&url={image_url}&language=ita&isOverlayRequired=false"
        r = requests.get(url, timeout=10).json() 
        if r.get("ParsedResults"):
            return r["ParsedResults"][0]["ParsedText"].upper()
    except: pass
    return ""

# === MOTORE: STORIESVIEWER.NET (Click Fisico sulla Lente) ===
def check_storiesviewer(page):
    print(f"⏩ Controllo StoriesViewer.net...")
    target_url = "https://storiesviewer.net/it/"
    links = []
    status = "UNKNOWN"
    error_details = ""
    
    try:
        # 1. Carica la Home
        response = page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
        
        # Controlla status HTTP
        if response.status != 200:
            status = "HTTP_ERROR"
            error_details = f"Status {response.status}"
            print(f"❌ StoriesViewer HTTP Error: {response.status}")
            return links, status, error_details
        
        # Gestione Cookie
        try:
            page.click("button:has-text('Consent'), .fc-cta-consent", timeout=3000)
        except: pass
        
        # 2. Ricerca
        try:
            search_input = page.locator('input[name="url"], input[type="text"]').first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            search_input.fill(IG_USER)
            time.sleep(1)
            
            # Clicca lente
            search_btn = page.locator('button[type="submit"], button:has(i), button.btn-default').first
            search_btn.wait_for(state="visible", timeout=5000)
            search_btn.click()
            print("🖱️ Lente cliccata!")
            
        except Exception as e:
            status = "INPUT_ERROR"
            error_details = f"Input non trovato: {str(e)[:100]}"
            print(f"⚠️ Errore fase ricerca: {e}")
            return links, status, error_details

        # 3. Attesa Risultati con gestione del caricamento lento e errori server
        try:
            # Aspetta che le scritte "Caricamento" o "Loading" scompaiano
            try:
                page.wait_for_selector('text="Caricamento", text="Loading"', state='hidden', timeout=30000)
                print("✅ Caricamento completato.")
            except:
                print("ℹ️ Nessun indicatore di caricamento rilevato")
                pass
            
            # Controlla se c'è il messaggio di errore del server
            try:
                page.wait_for_selector('text="Sorry, the server is temporarily unavailable"', timeout=5000)
                status = "SERVER_UNAVAILABLE"
                error_details = "Server temporaneamente non disponibile"
                print("ℹ️ StoriesViewer: Server temporaneamente non disponibile (si risolverà da solo)")
                return links, status, error_details
            except:
                pass  # Nessun messaggio di errore
            
            # Controlla se appare messaggio "nessuna storia"
            try:
                page.wait_for_selector('text="No stories found", text="Nessuna storia", text="not found"', timeout=5000)
                status = "NO_STORIES"
                error_details = "Profilo senza storie o privato"
                print("ℹ️ StoriesViewer: Nessuna storia trovata per questo profilo")
                return links, status, error_details
            except:
                pass
                
            # Attende i risultati (timeout più lungo per siti lenti)
            page.wait_for_selector('a:has-text("Download HD"), .story-item, .stories-container', timeout=30000)
            print("✨ Elementi storie trovati!")
            
        except Exception as e:
            # Se timeout, potrebbero esserci già elementi? Continuiamo a estrarre
            status = "TIMEOUT"
            error_details = f"Timeout caricamento risultati: {str(e)[:100]}"
            print("⚠️ Timeout caricamento storie, procedo con estrazione eventuali link...")
            # Non usciamo, continuiamo a cercare link
        
        # 4. Estrazione link (anche in caso di timeout, se ci sono elementi)
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

        links = list(dict.fromkeys(links))
        
        if links:
            status = "SUCCESS"
            print(f"✅ StoriesViewer: {len(links)} link trovati.")
        else:
            # Se non abbiamo link, ma lo status è ancora UNKNOWN (non è stato impostato da altri casi)
            if status == "UNKNOWN":
                status = "NO_LINKS"
                print("⚠️ StoriesViewer: Sito caricato ma nessun link estratto")
            elif status == "TIMEOUT":
                # Manteniamo il TIMEOUT, ma se ci sono link li abbiamo già presi
                if not links:
                    print("⚠️ StoriesViewer: Timeout e nessun link estratto")
            
        return links, status, error_details
        
    except Exception as e:
        status = "CRASH"
        error_details = f"Eccezione generale: {str(e)[:150]}"
        print(f"❌ Errore StoriesViewer: {e}")
        return links, status, error_details
        
# --- MOTORE 2: IQSAVED (Riserva) ---
def check_iqsaved(page):
    print(f"🔎 Controllo IQSAVED per {IG_USER}...")
    target_url = f"https://iqsaved.com/it/viewer/{IG_USER}/"
    links = []
    status = "UNKNOWN"
    error_details = ""
    
    try:
        response = page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
        
        # Controlla status HTTP
        if response.status != 200:
            status = "HTTP_ERROR"
            error_details = f"Status {response.status}"
            print(f"❌ IQSaved HTTP Error: {response.status}")
            return links, status, error_details
            
        time.sleep(5)
        
        # Cookie
        try:
            page.click("button.fc-cta-consent, button.primary-button, .cookie-agree", timeout=3000)
        except: pass

        # Scrolling
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)

        # Controlla se ci sono messaggi di errore
        page_content = page.content()
        
        if "No stories found" in page_content or "Nessuna storia" in page_content:
            status = "NO_STORIES"
            error_details = "Profilo senza storie o privato"
            print("ℹ️ IQSaved: Nessuna storia trovata per questo profilo")
            return links, status, error_details
            
        # Estrazione link
        raw_links = re.findall(r'https://cdn\.iqsaved\.com/[^"\']+', page_content)
        links = [l.replace('&amp;', '&') for l in raw_links]
        
        if links:
            status = "SUCCESS"
            print(f"✅ IQSaved: trovati {len(links)} link.")
        else:
            status = "NO_LINKS"
            print("⚠️ IQSaved: Sito caricato ma nessun link estratto")
            
        return list(dict.fromkeys(links)), status, error_details
        
    except Exception as e:
        status = "CRASH"
        error_details = f"Eccezione: {str(e)[:150]}"
        print(f"❌ Errore IQSaved: {e}")
        return links, status, error_details

# --- FUNZIONI DI RECOVERY E GESTIONE ERRORI ---

def safe_check_storiesviewer(page):
    """Wrapper con gestione errori robusta per StoriesViewer"""
    try:
        print("🔒 Esecuzione sicura StoriesViewer...")
        return check_storiesviewer(page)
    except Exception as e:
        error_msg = f"💀 CRASH GRAVE StoriesViewer: {str(e)[:200]}"
        print(error_msg)
        return [], "FATAL_ERROR", f"Crash completo: {str(e)[:100]}"

def safe_check_iqsaved(page):
    """Wrapper con gestione errori robusta per IQSaved"""
    try:
        print("🔒 Esecuzione sicura IQSaved...")
        return check_iqsaved(page)
    except Exception as e:
        error_msg = f"💀 CRASH GRAVE IQSaved: {str(e)[:200]}"
        print(error_msg)
        return [], "FATAL_ERROR", f"Crash completo: {str(e)[:100]}"

def emergency_cleanup(browser=None, context=None):
    """Pulizia di emergenza se tutto va male"""
    print("🆘 Esecuzione cleanup di emergenza...")
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
    # Forza garbage collection
    import gc
    gc.collect()
    print("✅ Cleanup di emergenza completato")
# -----------------------------------------------------------------
def run():
    print("🚀 Avvio Bot Ibrido...")

    # Variabili per cleanup
    browser = None
    context = None

    try:
        # Carica History
        seen_ids = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r") as f:
                seen_ids = f.read().splitlines()

        # Definisci le variabili all'inizio per evitare NameError
        updated_history = seen_ids.copy()
        ids_to_add = []

        with sync_playwright() as p:
            # Browser con argomenti ottimizzati
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                ]
            )
            
            # Context con viewport più grande per simulare desktop
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                viewport={'width': 1280, 'height': 800}
            )
            
            page = context.new_page()

        # --- STRATEGIA NUOVA (StoriesViewer + IQSaved) ---
        all_links = []

        # FASE 1: StoriesViewer (con recovery)
        try:
            links_viewer, storiesviewer_status, storiesviewer_error = safe_check_storiesviewer(page)
            all_links.extend(links_viewer)
            print(f"✅ StoriesViewer completato: {len(links_viewer)} link")
        except Exception as e:
            print(f"⚠️ Fallback a safe_check: {e}")
            links_viewer, storiesviewer_status, storiesviewer_error = [], "SAFECHECK_ERROR", str(e)[:100]
        
        # FASE 2: IQSaved (Riserva con recovery)
        links_iq, iqsaved_status, iqsaved_error = [], "NOT_TESTED", ""
        try:
            if len(all_links) < 5:
                print("\n=== FASE 2: IQSAVED (FALLBACK) ===")
                links_iq, iqsaved_status, iqsaved_error = safe_check_iqsaved(page)
                all_links.extend(links_iq)
                print(f"✅ IQSaved completato: {len(links_iq)} link")
        except Exception as e:
            print(f"⚠️ Fallback IQSaved fallito: {e}")
            links_iq, iqsaved_status, iqsaved_error = [], "SAFECHECK_ERROR", str(e)[:100]
        
        # Unione liste (senza duplicati) e conteggio
        tutti_i_link = list(dict.fromkeys(all_links))
        print(f"📦 Totale link unici trovati: {len(tutti_i_link)}")

        storie_da_processare = []
        for url in tutti_i_link:
            clean_id = get_clean_id(url)
            
            # Aggiungiamo alla lista solo se non l'abbiamo già vista
            if clean_id not in seen_ids:
                storie_da_processare.append({'url': url, 'id': clean_id})

        num_nuove = len(storie_da_processare)
        ids_to_add = []

        if num_nuove > SOGLIA_ALLUVIONE:
            print(f"⚠️ FLOOD GUARD ({num_nuove} > {SOGLIA_ALLUVIONE}). Skip invio.")
            for item in storie_da_processare:
                ids_to_add.append(item['id'])
        else:
            print(f"📨 Invio {num_nuove} nuove storie...")
            for item in storie_da_processare:
                url = item['url']
                clean_id = item['id']
                tipo = "VIDEO" if ".mp4" in url else "FOTO"
                
                dida = "Storia"

                # --- MODIFICA OCR GENTILE ---
                if tipo == "FOTO" and OCR_KEY:
                    txt = ocr_scan(url)
                    # Cerca quale parola chiave specifica è stata trovata
                    found_keyword = next((k for k in PAROLE_CHIAVE if k in txt), None)
                    
                    if found_keyword:
                        # Usa .title() per dare la lettera maiuscola ai Nomi
                        dida = f"Storia su {found_keyword.title()}"
                
                send_telegram(dida, url, tipo == "VIDEO")
                ids_to_add.append(clean_id)
                time.sleep(3)

        browser.close()

        if ids_to_add:
            # Uniamo la vecchia history con i nuovi ID
            updated_history = seen_ids + ids_to_add
            
            # Manteniamo solo gli ultimi MAX_HISTORY elementi per non ingrossare il file
            if len(updated_history) > MAX_HISTORY:
                updated_history = updated_history[-MAX_HISTORY:]
            
            with open("history.txt", "w") as f:
                for sid in updated_history:
                    f.write(f"{sid}\n")
            print(f"\n💾 History aggiornata: {len(updated_history)} elementi totali")

                # === HEALTH CHECK INTELLIGENTE ===
        print("\n🔍 Health Check dettagliato...")
        
        # Notifiche solo per problemi reali, non per profili senza storie
        send_alert = False
        alert_message = ""
        
        # Analisi StoriesViewer
        if storiesviewer_status == "HTTP_ERROR":
            send_alert = True
            alert_message += f"🔴 STORIESVIEWER DOWN: Errore HTTP {storiesviewer_error}\n"
        elif storiesviewer_status == "CRASH":
            send_alert = True
            alert_message += f"🔴 STORIESVIEWER CRASH: {storiesviewer_error}\n"
        elif storiesviewer_status == "TIMEOUT":
            # Timeout è sospetto ma non sempre critico
            if iqsaved_status != "SUCCESS":  # Se anche IQSaved non funziona
                send_alert = True
                alert_message += f"🟡 STORIESVIEWER TIMEOUT: Caricamento lento\n"
        elif storiesviewer_status == "INPUT_ERROR":
            send_alert = True
            alert_message += f"🔴 STORIESVIEWER CAMBIO LAYOUT: Input/lente non trovati\n"
        # NOTA: SERVER_UNAVAILABLE non genera notifica - si risolve da solo
        
        # Analisi IQSaved
        if iqsaved_status == "HTTP_ERROR":
            send_alert = True
            alert_message += f"🔴 IQSAVED DOWN: Errore HTTP {iqsaved_error}\n"
        elif iqsaved_status == "CRASH":
            send_alert = True
            alert_message += f"🔴 IQSAVED CRASH: {iqsaved_error}\n"
        
        # Costruzione messaggio dettagliato
        if send_alert:
            # Aggiungi info di contesto
            alert_message += f"\n📊 CONTESTO:\n"
            alert_message += f"• Profilo: {IG_USER}\n"
            alert_message += f"• StoriesViewer: {storiesviewer_status} ({len(links_viewer)} storie)\n"
            alert_message += f"• IQSaved: {iqsaved_status} ({len(links_iq)} storie)\n"
            alert_message += f"• Totale storie: {len(tutti_i_link)}\n"
            alert_message += f"• Errori: {storiesviewer_error if storiesviewer_error else iqsaved_error}\n"
            
            if len(tutti_i_link) == 0:
                alert_message += f"\n⚠️ CRITICO: Nessuna storia trovata da nessun sito!"
            else:
                alert_message += f"\n✅ Backup funzionante: {len(tutti_i_link)} storie trovate"
            
            print(f"📢 Invio allarme: {alert_message[:100]}...")
            send_telegram(f"🚨 ALLARME SITI\n\n{alert_message}")
        
        # Log dettagliato (sempre visibile nei log)
        print(f"\n📋 Riepilogo Health Check:")
        print(f"   StoriesViewer: {storiesviewer_status} - Error: {storiesviewer_error}")
        print(f"   IQSaved: {iqsaved_status} - Error: {iqsaved_error}")
        
        # Allarme critico solo se entrambi i siti falliscono completamente
        # Escludiamo SERVER_UNAVAILABLE che è temporaneo
        critical_statuses = ["NO_STORIES", "UNKNOWN", "SERVER_UNAVAILABLE"]
        if len(tutti_i_link) == 0 and storiesviewer_status not in critical_statuses and iqsaved_status not in ["NO_STORIES", "UNKNOWN"]:
            print("🚨 ALLARME CRITICO: Nessun sito funziona!")
            critical_msg = f"🔴 CRITICO: Nessun sito funziona per {IG_USER}\n\n"
            critical_msg += f"StoriesViewer: {storiesviewer_status} ({storiesviewer_error})\n"
            critical_msg += f"IQSaved: {iqsaved_status} ({iqsaved_error})\n\n"
            critical_msg += "Intervento immediato richiesto!"
            send_telegram(critical_msg)
        # =========================================================
        
        print(f"\n✅ BOT COMPLETATO")
        print(f"📊 Riepilogo: {num_nuove} storie processate, {len(ids_to_add)} aggiunte a history")

if __name__ == "__main__":
    run()
