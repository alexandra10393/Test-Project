import os
import time
import re
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURAZIONE ---
IG_USER = "gabrieleparpiglia"
TARGET_URL = f"https://iqsaved.com/it/viewer/{IG_USER}/"
PAROLE_CHIAVE = ["DE MARTINO", "BELEN", "STEFANO DE MARTINO"]
SOGLIA_ALLUVIONE = 5   
MAX_HISTORY = 200      

# RECUPERO CHIAVI
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OCR_KEY = os.environ.get("OCR_KEY", "")

def get_clean_id(url):
    try:
        if "filename=" in url:
            return url.split("filename=")[1].split("&")[0]
        return url.split("/")[-1].split("?")[0]
    except:
        return url

def send_telegram(text, media_url=None, is_video=False):
    api_url = f"https://api.telegram.org/bot{TOKEN}/"
    method = "sendVideo" if is_video else "sendPhoto"
    print(f"Tentativo invio Telegram: {text}")
    if media_url:
        try:
            payload = {"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"}
            files_key = 'video' if is_video else 'photo'
            requests.post(api_url + method, data=payload, params={files_key: media_url}, timeout=60)
        except Exception as e:
            print(f"Errore media: {e}")
            requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text + f"\n\n(Link: {media_url})"})
    else:
        requests.post(api_url + "sendMessage", json={"chat_id": CHAT_ID, "text": text})

def ocr_scan(image_url):
    if not OCR_KEY: return ""
    try:
        # Timeout breve per l'OCR
        url = f"https://api.ocr.space/parse/imageurl?apikey={OCR_KEY}&url={image_url}&language=ita&isOverlayRequired=false"
        r = requests.get(url, timeout=10).json() 
        if r.get("ParsedResults"):
            return r["ParsedResults"][0]["ParsedText"].upper()
    except: pass
    return ""

def extract_links_from_page(page):
    content = page.content()
    links = re.findall(r'https://cdn\.iqsaved\.com/[^"\']+', content)
    links = [l.replace('&amp;', '&') for l in links]
    return list(dict.fromkeys(links))

def run():
    print("Avvio Browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        
        found_links = []

        try:
            # TIMEOUT AGGRESSIVI (Punto 1)
            print("Caricamento pagina...")
            page.goto(TARGET_URL, timeout=60000, wait_until="domcontentloaded") # Max 60s
            time.sleep(8)

            try:
                page.click("button.fc-cta-consent, button.primary-button, .cookie-agree", timeout=5000) # Max 5s
                time.sleep(2)
            except: pass

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(5)
            
            found_links = extract_links_from_page(page)

            # Retry se 0 link
            if len(found_links) == 0:
                print("⚠️ 0 link trovati. Reload...")
                page.reload(timeout=60000)
                time.sleep(10)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(5)
                found_links = extract_links_from_page(page)

        except Exception as e:
            print(f"Errore critico pagina: {e}")
            try:
                page.screenshot(path="error_screenshot.png")
            except: pass
            browser.close()
            return

        # GESTIONE MEMORIA
        seen_ids = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r") as f:
                seen_ids = f.read().splitlines()
        
        storie_da_processare = []
        for url in found_links:
            if "filename=" not in url: continue
            clean_id = get_clean_id(url)
            if clean_id not in seen_ids:
                storie_da_processare.append({'url': url, 'id': clean_id})

        num_nuove = len(storie_da_processare)
        ids_to_add = []

        if num_nuove > SOGLIA_ALLUVIONE:
            print(f"⚠️ FLOOD GUARD ATTIVO ({num_nuove} storie). Skip notifiche e OCR.")
            for item in storie_da_processare:
                ids_to_add.append(item['id'])
        else:
            for item in storie_da_processare:
                url = item['url']
                clean_id = item['id']
                tipo = "VIDEO" if ".mp4" in url or "video" in url else "FOTO"
                
                dida = "Storia"
                # OCR SELETTIVO (Punto 3): Lo facciamo solo se NON siamo in flood
                if tipo == "FOTO" and OCR_KEY:
                    txt = ocr_scan(url)
                    for k in PAROLE_CHIAVE:
                        if k in txt: 
                            dida = f"Storia su {k.title()}"
                            break
                
                send_telegram(dida, url, tipo == "VIDEO")
                ids_to_add.append(clean_id)
                time.sleep(3)

        browser.close()

        # SALVATAGGIO (Rolling Buffer)
        updated_history = seen_ids + ids_to_add
        if len(updated_history) > MAX_HISTORY:
            updated_history = updated_history[-MAX_HISTORY:]
        
        if ids_to_add or len(seen_ids) != len(updated_history):
            with open("history.txt", "w") as f:
                for sid in updated_history:
                    f.write(f"{sid}\n")
            print("History aggiornata.")

if __name__ == "__main__":
    run()
