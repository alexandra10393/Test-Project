import os
import time
import re
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURAZIONE ---
IG_USER = "gabrieleparpiglia"
TARGET_URL = f"https://iqsaved.com/it/viewer/{IG_USER}/"
PAROLE_CHIAVE = ["DE MARTINO", "BELEN", "STEFANO"]

# RECUPERO CHIAVI DA GITHUB
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OCR_KEY = os.environ.get("OCR_KEY", "")

def send_telegram(text, media_url=None, is_video=False):
    api_url = f"https://api.telegram.org/bot{TOKEN}/"
    method = "sendVideo" if is_video else "sendPhoto"
    
    print(f"Invio a Telegram: {text}")
    
    if media_url:
        try:
            payload = {"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"}
            files_key = 'video' if is_video else 'photo'
            requests.post(api_url + method, data=payload, params={files_key: media_url}, timeout=60)
        except Exception as e:
            print(f"Errore invio media: {e}")
            requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text + f"\n\n(Link: {media_url})"})
    else:
        requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text})

def ocr_scan(image_url):
    if not OCR_KEY: return ""
    print("Analisi OCR in corso...")
    try:
        url = f"https://api.ocr.space/parse/imageurl?apikey={OCR_KEY}&url={image_url}&language=ita&isOverlayRequired=false"
        r = requests.get(url, timeout=15).json()
        if r.get("ParsedResults"):
            return r["ParsedResults"][0]["ParsedText"].upper()
    except:
        pass
    return ""

def run():
    print("Avvio Browser (Playwright)...")
    with sync_playwright() as p:
        # Avvia Chromium in modalità headless (invisibile)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        
        try:
            page.goto(TARGET_URL, timeout=90000, wait_until="domcontentloaded")
            time.sleep(8) # Attesa caricamento iniziale

            # --- BYPASS BANNER COOKIE ---
            try:
                page.click("button.fc-cta-consent, button.primary-button, .cookie-agree", timeout=4000)
                print("Cookie accettati.")
                time.sleep(2)
            except:
                print("Banner non trovato o non necessario.")

            # Scroll per forzare il caricamento delle storie
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(5)

        except Exception as e:
            print(f"Errore caricamento pagina: {e}")
            browser.close()
            return

        # --- ESTRAZIONE LINK ---
        # Cerchiamo i link diretti ai file media nel codice HTML renderizzato
        content = page.content()
        found_links = re.findall(r'https://cdn\.iqsaved\.com/[^"\']+', content)
        
        # Pulizia link
        found_links = [l.replace('&amp;', '&') for l in found_links]
        # Rimuove duplicati mantenendo l'ordine
        found_links = list(dict.fromkeys(found_links))

        print(f"Trovati {len(found_links)} contenuti totali.")

        # --- GESTIONE MEMORIA ---
        seen_stories = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r") as f:
                seen_stories = f.read().splitlines()
        
        new_ids = []
        
        for url in found_links:
            if url in seen_stories:
                continue
            
            tipo = "VIDEO" if ".mp4" in url or "video" in url else "FOTO"
            print(f"Nuovo contenuto: {tipo}")
            
            # --- LOGICA DEL MESSAGGIO RICHIESTA ---
            dida = "Storia"
            
            if tipo == "FOTO" and OCR_KEY:
                txt = ocr_scan(url)
                for k in PAROLE_CHIAVE:
                    if k in txt: 
                        dida = f"Storia su {k.title()}"
                        break
            
            send_telegram(dida, url, tipo == "VIDEO")
            new_ids.append(url)
            time.sleep(3) # Pausa di cortesia tra i messaggi

        # Aggiornamento memoria
        if new_ids:
            with open("history.txt", "a") as f:
                for sid in new_ids: f.write(f"{sid}\n")
            print(f"Salvate {len(new_ids)} nuove storie.")
        else:
            print("Nessuna nuova storia.")

        browser.close()

if __name__ == "__main__":
    run()
