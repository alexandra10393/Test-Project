import os
import time
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURAZIONE ---
IG_USER = "gabrieleparpiglia"
TARGET_URL = f"https://iqsaved.com/it/viewer/{IG_USER}/"
PAROLE_CHIAVE = ["DE MARTINO", "BELEN", "STEFANO DE MARTINO"]

# RECUPERO CHIAVI
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OCR_KEY = os.environ.get("OCR_KEY", "")

def send_telegram(text, media_url=None, is_video=False):
    api_url = f"https://api.telegram.org/bot{TOKEN}/"
    method = "sendVideo" if is_video else "sendPhoto"
    if media_url:
        try:
            payload = {"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"}
            files_key = 'video' if is_video else 'photo'
            requests.post(api_url + method, data=payload, params={files_key: media_url})
        except:
            requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text + f"\nLink: {media_url}"})
    else:
        requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text})

def ocr_scan(image_url):
    if not OCR_KEY: return ""
    try:
        url = f"https://api.ocr.space/parse/imageurl?apikey={OCR_KEY}&url={image_url}&language=ita&isOverlayRequired=false"
        r = requests.get(url, timeout=10).json()
        if r.get("ParsedResults"): return r["ParsedResults"][0]["ParsedText"].upper()
    except: pass
    return ""

def run():
    print("Avvio browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        
        try:
            page.goto(TARGET_URL, timeout=60000, wait_until="domcontentloaded")
            time.sleep(5) # Aspetta caricamento
            # Cerca elementi che contengono SCARICA
            page.wait_for_selector("a:has-text('SCARICA')", timeout=15000)
        except:
            print("Nessuna storia trovata o errore sito.")
            browser.close()
            return

        # Estrazione Link dai bottoni SCARICA
        found_media = page.evaluate("""() => {
            const links = [];
            document.querySelectorAll('a').forEach(a => {
                const t = a.innerText.toUpperCase();
                if (t.includes('SCARICA FOTO') || t.includes('SCARICA VIDEO')) {
                    links.push({ url: a.href, type: t.includes('VIDEO') ? 'VIDEO' : 'FOTO' });
                }
            });
            return links;
        }""")
        
        # Gestione Memoria (Lettura history.txt)
        seen_stories = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r") as f:
                seen_stories = f.read().splitlines()
        
        new_ids = []
        for item in found_media:
            url = item['url']
            if url not in seen_stories:
                print(f"Nuova storia: {item['type']}")
                new_ids.append(url)
                
                dida = "storia"
                if item['type'] == "FOTO" and OCR_KEY:
                    txt = ocr_scan(url)
                    for k in PAROLE_CHIAVE:
                        if k in txt: dida = f"storia su {k.title()}"; break
                
                send_telegram(dida, url, item['type'] == "VIDEO")
                time.sleep(2)

        # Salvataggio Memoria
        if new_ids:
            with open("history.txt", "a") as f:
                for sid in new_ids: f.write(f"{sid}\n")

        browser.close()

if __name__ == "__main__":
    run()
