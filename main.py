import os
import time
import re
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURAZIONE ---
IG_USER = "gabrieleparpiglia"
TARGET_URL = f"https://iqsaved.com/it/viewer/{IG_USER}/"
PAROLE_CHIAVE = ["DE MARTINO", "BELEN", "STEFANO"]

# RECUPERO CHIAVI (DA GITHUB SECRETS)
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
            # Usiamo una richiesta POST con dati
            requests.post(api_url + method, data=payload, params={files_key: media_url}, timeout=30)
        except Exception as e:
            print(f"Errore invio media: {e}")
            # Fallback: manda solo il link se il media fallisce
            requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text + f"\n\n(Media non caricato, link: {media_url})"})
    else:
        requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text})

def ocr_scan(image_url):
    if not OCR_KEY: return ""
    print("Eseguo OCR sulla foto...")
    try:
        url = f"https://api.ocr.space/parse/imageurl?apikey={OCR_KEY}&url={image_url}&language=ita&isOverlayRequired=false"
        r = requests.get(url, timeout=15).json()
        if r.get("ParsedResults"):
            testo_trovato = r["ParsedResults"][0]["ParsedText"].upper()
            print(f"Testo OCR trovato: {testo_trovato[:50]}...")
            return testo_trovato
    except Exception as e:
        print(f"Errore OCR: {e}")
    return ""

def run():
    print("Avvio browser Playwright...")
    with sync_playwright() as p:
        # Lanciamo chromium (simile a Chrome/Brave)
        browser = p.chromium.launch(headless=True)
        # Usiamo uno UserAgent reale per sembrare un PC vero
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        
        try:
            print(f"Vado su {TARGET_URL}")
            page.goto(TARGET_URL, timeout=90000, wait_until="domcontentloaded")
            
            # --- BYPASS BANNER COOKIE (La tecnica imparata oggi) ---
            print("Attendo caricamento pagina (10s)...")
            time.sleep(10) 
            
            try:
                # Cerca e clicca bottoni comuni di consenso
                page.click("button.fc-cta-consent, button.primary-button, .cookie-agree", timeout=5000)
                print("Banner Cookie cliccato!")
                time.sleep(3)
            except:
                print("Nessun banner trovato o già accettato.")

            # Scorriamo la pagina per attivare il caricamento delle storie
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(5)

        except Exception as e:
            print(f"Errore caricamento pagina: {e}")
            browser.close()
            return

        # --- ESTRAZIONE LINK ---
        # Cerchiamo direttamente i link 'cdn.iqsaved.com' nel codice, come fatto col PowerShell
        content = page.content()
        # Regex per trovare i link lunghi delle immagini/video
        found_links = re.findall(r'https://cdn\.iqsaved\.com/[^"\']+', content)
        
        # Pulizia link (rimuovi eventuali escape HTML)
        found_links = [l.replace('&amp;', '&') for l in found_links]
        # Rimuoviamo duplicati mantenendo l'ordine
        found_links = list(dict.fromkeys(found_links))

        print(f"Trovati {len(found_links)} link potenziali.")

        # --- GESTIONE MEMORIA (HISTORY.TXT) ---
        seen_stories = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r") as f:
                seen_stories = f.read().splitlines()
        
        new_ids = []
        
        for url in found_links:
            # Controllo se l'abbiamo già mandato
            if url in seen_stories:
                continue
            
            # Determina se è VIDEO o FOTO guardando l'URL o il filename
            tipo = "VIDEO" if ".mp4" in url or "video" in url else "FOTO"
            
            print(f"Nuova storia trovata: {tipo}")
            
            dida = f"Nuova storia di {IG_USER}"
            
            # --- LOGICA OCR e PAROLE CHIAVE ---
            if tipo == "FOTO" and OCR_KEY:
                txt = ocr_scan(url)
                trovato_key = False
                for k in PAROLE_CHIAVE:
                    if k in txt: 
                        dida = f"🚨 <b>ALLARME GOSSIP: {k.title()}!</b>\n\nTrovato nella storia di {IG_USER}!"
                        trovato_key = True
                        break
                if not trovato_key and txt:
                    dida += f"\n\nTesto rilevato: <i>{txt[:100]}...</i>"
            
            # Invio
            send_telegram(dida, url, tipo == "VIDEO")
            new_ids.append(url)
            time.sleep(5) # Pausa anti-ban tra un invio e l'altro

        # --- SALVATAGGIO MEMORIA ---
        if new_ids:
            with open("history.txt", "a") as f:
                for sid in new_ids: f.write(f"{sid}\n")
            print(f"Salvate {len(new_ids)} nuove storie in history.txt")
        else:
            print("Nessuna novità.")

        browser.close()

if __name__ == "__main__":
    run()
