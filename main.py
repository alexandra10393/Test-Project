import os
import time
import re
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURAZIONE ---
# Usa le variabili d'ambiente (Secrets) se ci sono, altrimenti i valori di default
IG_USER = os.environ.get("IG_USER", "gabrieleparpiglia") 
PAROLE_CHIAVE = ["DE MARTINO", "BELEN", "STEFANO DE MARTINO"]
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

# --- MOTORE 1: MOLLYGRAM (Aggiornato) ---
def check_mollygram(page):
    print(f"🔎 Controllo MOLLYGRAM per {IG_USER}...")
    links = []
    try:
        # Caricamento con un wait più intelligente
        page.goto("https://mollygram.com/it", timeout=60000)
        
        # 1. Gestione Cookie (Proviamo più selettori)
        try:
            page.wait_for_selector("text=Consent, .fc-cta-consent", timeout=5000)
            page.click("text=Consent, .fc-cta-consent")
            print("🍪 Cookie accettati.")
            time.sleep(2)
        except: 
            print("ℹ️ Nessun banner cookie trovato o già accettato.")

        # 2. Ricerca Utente (Versione Robusta)
        try:
            # Cerchiamo la barra in modo generico (il primo input di testo visibile)
            search_input = page.locator('input[name="url"], input[type="text"], input.form-control').first
            
            # Ci assicuriamo che sia visibile e cliccabile
            search_input.wait_for(state="visible", timeout=10000)
            search_input.click()
            search_input.fill(IG_USER)
            search_input.press('Enter')
            
            print("⌨️ Utente cercato, attendo risultati...")
            
            # Aspettiamo che appaia qualcosa che assomiglia a un risultato (avatar o media)
            # Aumentiamo l'attesa perché Mollygram a volte è lento a processare
            time.sleep(10) 
            
        except Exception as e:
            print(f"⚠️ Errore ricerca Mollygram (Barra non trovata): {e}")
            # Se fallisce la ricerca, proviamo ad andare direttamente all'URL (tentativo disperato)
            # Nota: Spesso non funziona su Mollygram, ma vale la pena tentare
            return []

        # 3. Scroll e Estrazione
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)
        
        # Cerchiamo i tasti download o i link diretti ai media
        found_elements = page.query_selector_all('a[href*=".mp4"], a[href*=".jpg"], a[href*=".jpeg"], a[download]')
        
        for el in found_elements:
            link = el.get_attribute("href")
            if link and "http" in link:
                links.append(link)

        links = list(dict.fromkeys(links))
        print(f"✅ Mollygram: trovati {len(links)} link potenziali.")
        return links

    except Exception as e:
        print(f"❌ Errore critico Mollygram: {e}")
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

        # --- FASE 1: MOLLYGRAM ---
        links_molly = check_mollygram(page)
        
        # --- FASE 2: IQSAVED (Solo se Molly ha pochi risultati o per sicurezza) ---
        # Li eseguiamo entrambi per massimizzare le possibilità
        links_iq = check_iqsaved(page)

        # Unione liste (senza duplicati)
        tutti_i_link = list(set(links_molly + links_iq))
        print(f"📦 Totale link unici trovati: {len(tutti_i_link)}")

        storie_da_processare = []
        for url in tutti_i_link:
            clean_id = get_clean_id(url)
            
            # Filtro base: ignoriamo se non sembra una storia (opzionale)
            # Ma visto che history protegge, prendiamo tutto.
            
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
                if tipo == "FOTO" and OCR_KEY:
                    txt = ocr_scan(url)
                    if any(k in txt for k in PAROLE_CHIAVE):
                        dida = f"🔥 TROVATO KEYWORD: {txt[:50]}..."

                send_telegram(dida, url, tipo == "VIDEO")
                ids_to_add.append(clean_id)
                time.sleep(3)

        browser.close()

        # Salvataggio History
        updated_history = seen_ids + ids_to_add
        if len(updated_history) > MAX_HISTORY:
            updated_history = updated_history[-MAX_HISTORY:]
        
        if ids_to_add:
            with open("history.txt", "w") as f:
                for sid in updated_history:
                    f.write(f"{sid}\n")
            print("💾 History aggiornata.")

if __name__ == "__main__":
    run()
