import os
import time
import re
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURAZIONE ---
IG_USER = "gabrieleparpiglia"
TARGET_URL = f"https://iqsaved.com/it/viewer/{IG_USER}/"
PAROLE_CHIAVE = ["DE MARTINO", "BELEN", "STEFANO"]
SOGLIA_ALLUVIONE = 5  # Se ci sono più di 5 storie nuove, non inviare notifiche (le segna solo come lette)

# RECUPERO CHIAVI
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OCR_KEY = os.environ.get("OCR_KEY", "")

def get_clean_id(url):
    """Estrae l'ID univoco dal parametro filename"""
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
            time.sleep(10)

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

        # --- CARICAMENTO MEMORIA ---
        seen_ids = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r") as f:
                seen_ids = f.read().splitlines()
        
        # Filtriamo le storie per capire quali sono VERAMENTE nuove
        storie_da_processare = []
        for url in found_links:
            if "filename=" not in url: continue # Salta foto profilo
            clean_id = get_clean_id(url)
            if clean_id not in seen_ids:
                storie_da_processare.append({'url': url, 'id': clean_id})

        num_nuove = len(storie_da_processare)
        print(f"Trovate {num_nuove} storie non presenti in memoria.")

        new_ids_to_save = []

        # --- LOGICA ANTI-ALLUVIONE ---
        if num_nuove > SOGLIA_ALLUVIONE:
            print(f"⚠️ RILEVATE TROPPE STORIE ({num_nuove}). MODALITÀ SILENZIOSA ATTIVA.")
            print("Salvo tutto come 'già visto' senza inviare notifiche Telegram.")
            
            # Aggiungiamo tutto alla lista di salvataggio senza inviare
            for item in storie_da_processare:
                new_ids_to_save.append(item['id'])
                
        else:
            # Funzionamento normale: invia le notifiche
            for item in storie_da_processare:
                url = item['url']
                clean_id = item['id']
                
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
                time.sleep(3)

        browser.close()

        # --- SALVATAGGIO ---
        if new_ids_to_save:
            with open("history.txt", "a") as f:
                for sid in new_ids_to_save:
                    f.write(f"{sid}\n")
            print("Memoria aggiornata.")

if __name__ == "__main__":
    run()
