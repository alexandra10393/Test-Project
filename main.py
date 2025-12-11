import os
import time
import re
import requests
from playwright.sync_api import sync_playwright
from urllib.parse import unquote

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
    
    if media_url:
        try:
            payload = {"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"}
            files_key = 'video' if is_video else 'photo'
            # Timeout aumentato per video pesanti
            requests.post(api_url + method, data=payload, params={files_key: media_url}, timeout=120)
        except Exception as e:
            print(f"❌ Errore invio media: {e}")
            # Fallback: invia solo il link
            requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text + f"\n\n(Link diretto: {media_url})"})
    else:
        requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text})

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
    
    try:
        # 1. Carica la Home
        page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
        
        # Gestione Cookie (se presente)
        try:
            page.click("button:has-text('Consent'), .fc-cta-consent", timeout=3000)
        except: pass
        
        # 2. Ricerca
        try:
            # Cerca la barra di input
            search_input = page.locator('input[name="url"], input[type="text"]').first
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            search_input.fill(IG_USER)
            time.sleep(1)
            
            # CLICCARE LA LENTE (Modifica Cruciale)
            print("🔍 Cerco il tasto lente...")
            
            # Proviamo diversi selettori comuni per il tasto cerca con lente
            # 1. Bottone generico di submit
            # 2. Bottone che contiene un tag 'i' (icona)
            # 3. Bottone con classe 'btn-default' (comune in questi script)
            search_btn = page.locator('button[type="submit"], button:has(i), button.btn-default').first
            
            search_btn.wait_for(state="visible", timeout=5000)
            search_btn.click()
            print("🖱️ Lente cliccata!")
            
        except Exception as e:
            print(f"⚠️ Errore fase ricerca (Lente non trovata o non cliccabile): {e}")
            return []

        # 3. Attesa Risultati
        print("⏳ Attendo caricamento storie...")
        # Aspettiamo il tasto "Download HD" che hai identificato tu
        try:
            page.wait_for_selector('a:has-text("Download HD")', timeout=20000)
            print("✨ Tasti 'Download HD' apparsi!")
        except:
            print("⚠️ Timeout: Nessun risultato trovato o caricamento lento.")
            return []
            
        # 4. Estrazione e Decodifica
        # Cerchiamo tutti i link che contengono "media.php"
        raw_elements = page.query_selector_all('a[href*="media.php"]')
        
        for el in raw_elements:
            raw_url = el.get_attribute("href")
            
            # Il link è tipo: media.php?media=https%3A%2F%2F...
            if raw_url and "media=" in raw_url:
                try:
                    # Estraiamo la parte dopo media=
                    encoded_part = raw_url.split("media=")[1].split("&")[0]
                    # Decodifichiamo (trasforma %3A in : e %2F in /)
                    clean_url = unquote(encoded_part)
                    
                    if "cdninstagram.com" in clean_url:
                        links.append(clean_url)
                except:
                    continue

        links = list(dict.fromkeys(links))
        print(f"✅ StoriesViewer: {len(links)} link trovati e decodificati.")
        return links
        
    except Exception as e:
        print(f"❌ Errore StoriesViewer: {e}")
        return []
        
# --- MOTORE 2: IQSAVED (Riserva) ---
def check_iqsaved(page):
    print(f"🔎 Controllo IQSAVED per {IG_USER}...")
    target_url = f"https://iqsaved.com/it/viewer/{IG_USER}/"
    links = []
    try:
        page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(5)
        
        try:
            page.click("button.fc-cta-consent, button.primary-button, .cookie-agree", timeout=3000)
        except: pass

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)

        content = page.content()
        raw_links = re.findall(r'https://cdn\.iqsaved\.com/[^"\']+', content)
        links = [l.replace('&amp;', '&') for l in raw_links]
        
        print(f"✅ IQSaved: trovati {len(links)} link.")
        return list(dict.fromkeys(links))
    except Exception as e:
        print(f"❌ Errore IQSaved: {e}")
        return []

def run():
    print("🚀 Avvio Bot Ibrido...")
    
    # Carica History
    seen_ids = []
    if os.path.exists("history.txt"):
        with open("history.txt", "r") as f:
            seen_ids = f.read().splitlines()

    with sync_playwright() as p:
        # Browser con viewport più grande per simulare desktop
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 800}
        )
        page = context.new_page()

        # --- STRATEGIA NUOVA (StoriesViewer + IQSaved) ---
        all_links = []

        # FASE 1: StoriesViewer (Sito Veloce con estrazione Proxy)
        # Questo usa la nuova funzione check_storiesviewer che clicca la lente
        links_viewer = check_storiesviewer(page)
        all_links.extend(links_viewer)
        
        # FASE 2: IQSaved (Riserva)
        # Lo usiamo solo se il primo sito ha fallito o trovato meno di 5 storie
        if len(all_links) < 5:
            print("\n=== FASE 2: IQSAVED (FALLBACK) ===")
            links_iq = check_iqsaved(page) # Usa la tua funzione IQSaved esistente
            all_links.extend(links_iq)
        
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
            with open("history.txt", "w") as f:
                for sid in updated_history:
                    f.write(f"{sid}\n")
            print(f"\n💾 History aggiornata: {len(updated_history)} elementi totali")

        # === HEALTH CHECK CRITICO (Avviso di Fallimento Totale) ===
        # Se la lista totale dei link trovati è vuota, significa che entrambi i siti hanno fallito
        # o il profilo è diventato privato/vuoto.
        if len(tutti_i_link) == 0:
            print("🚨 NESSUN LINK TROVATO! Invio allarme...")
            error_msg = f"🔴 ⚠️ ALLARME CRITICO: Il Bot non ha trovato ALCUNA storia per {IG_USER}.\n\nCause possibili:\n1. Il profilo è diventato PRIVATO.\n2. StoriesViewer e IQSaved sono entrambi GIÙ.\n3. Nessuna storia presente nelle ultime 24h."
            send_telegram(error_msg)
        # =========================================================
        
        print(f"\n✅ BOT COMPLETATO")
        print(f"📊 Riepilogo: {num_nuove} storie processate, {len(ids_to_add)} aggiunte a history")

if __name__ == "__main__":
    run()
