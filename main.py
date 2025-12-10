import os
import time
import re
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURAZIONE ---
IG_USER = "gabrieleparpiglia"
TARGET_URL = f"https://iqsaved.com/it/viewer/{IG_USER}/"
PAROLE_CHIAVE = ["DE MARTINO", "BELEN", "STEFANO"]

# RECUPERO CHIAVI
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OCR_KEY = os.environ.get("OCR_KEY", "")

def get_clean_id(url):
    """Estrae l'ID univoco dal parametro filename"""
    # Esempio: ...&filename=12345_n.jpg -> 12345_n.jpg
    try:
        if "filename=" in url:
            return url.split("filename=")[1].split("&")[0]
        return url.split("/")[-1].split("?")[0]
    except:
        return url

def send_telegram(text, media_url=None, is_video=False):
    api_url = f"https://api.telegram.org/bot{TOKEN}/"
    method = "sendVideo" if is_video else "sendPhoto"
    
    print(f"Tentativo invio a Telegram: {text}")
    
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
    try:
        url = f"https://api.ocr.space/parse/imageurl?apikey={OCR_KEY}&url={image_url}&language=ita&isOverlayRequired=false"
        r = requests.get(url, timeout=15).json()
        if r.get("ParsedResults"):
            return r["ParsedResults"][0]["ParsedText"].upper()
    except: pass
    return ""

def run():
    print("Avvio Browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        
        try:
            page.goto(TARGET_URL, timeout=90000, wait_until="domcontentloaded")
            time.sleep(10) # Attesa caricamento

            # Bypass Cookie
            try:
                page.click("button.fc-cta-consent, button.primary-button, .cookie-agree", timeout=4000)
                time.sleep(2)
            except: pass

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(5)

        except Exception as e:
            print(f"Errore caricamento: {e}")
            browser.close()
            return

        # Estrazione Link
        content = page.content()
        found_links = re.findall(r'https://cdn\.iqsaved\.com/[^"\']+', content)
        found_links = [l.replace('&amp;', '&') for l in found_links]
        found_links = list(dict.fromkeys(found_links))

        # --- MEMORIA ---
        seen_ids = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r") as f:
                seen_ids = f.read().splitlines()
        
        new_ids_to_save = []
        
        for url in found_links:
            # --- FILTRO ANT-PROFILO ---
            # Se non c'è "filename=", al 99% è la foto profilo o sporcizia -> SALTA
            if "filename=" not in url:
                continue

            clean_id = get_clean_id(url)
            
            if clean_id in seen_ids:
                continue
            
            tipo = "VIDEO" if ".mp4" in url or "video" in url else "FOTO"
            print(f"NUOVA STORIA: {clean_id}")
            
            dida = "Storia"
            if tipo == "FOTO" and OCR_KEY:
                txt = ocr_scan(url)
                for k in PAROLE_CHIAVE:
                    if k in txt: 
                        dida = f"Storia su {k.title()}"
                        break
            
            send_telegram(dida, url, tipo == "VIDEO")
            
            new_ids_to_save.append(clean_id)
            seen_ids.append(clean_id)
            time.sleep(3)

        browser.close()

        # --- SALVATAGGIO ---
        if new_ids_to_save:
            with open("history.txt", "a") as f:
                for sid in new_ids_to_save:
                    f.write(f"{sid}\n")

if __name__ == "__main__":
    run()
