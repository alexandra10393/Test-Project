import os
import time
import re
import json
import requests
import shutil
import glob
import base64
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from playwright.sync_api import sync_playwright
from urllib.parse import unquote, urlparse, parse_qs
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import List, Tuple, Optional, Dict

# Import opzionale playwright-stealth 2.0+ (per evitare blocchi anti-bot)
try:
    from playwright_stealth import Stealth
    STEALTH_AVAILABLE = True
    STEALTH_INSTANCE = Stealth()  # Crea istanza una volta sola
except ImportError:
    STEALTH_AVAILABLE = False
    STEALTH_INSTANCE = None
    print("⚠️ playwright-stealth non installato, continuo senza stealth")

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
ERROR_LOG_FILE = "error_log.txt"

# Cache per evitare richieste duplicate
_url_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_cache_ttl = 3600  # 1 ora

def get_adaptive_timeout(site_name: str, base_timeout: int = 25000) -> int:
    """Calcola timeout adattivo basato su fallimenti consecutivi"""
    consecutive_fails = get_consecutive_fails(site_name)
    
    if consecutive_fails >= 3:
        # Sito problematico, dai più tempo
        return min(60000, base_timeout + (consecutive_fails * 5000))
    elif consecutive_fails == 0:
        # Sito affidabile, riduci timeout
        return max(15000, base_timeout - 5000)
    else:
        return base_timeout

# ===============================
# CLEANUP AUTOMATICO LOG
# ===============================

def cleanup_cache():
    """Pulisce cache URL vecchia"""
    global _url_cache
    current_time = time.time()
    expired_keys = [
        url for url, (cache_time, _) in _url_cache.items()
        if current_time - cache_time >= _cache_ttl
    ]
    for key in expired_keys:
        _url_cache.pop(key, None)
    if expired_keys:
        print(f"  🗑️  Rimossi {len(expired_keys)} entry dalla cache URL")

def cleanup_old_logs(days_to_keep=7, max_performance_entries=1000):
    """Pulisce file log vecchi e mantiene dimensioni gestibili"""
    print("🧹 Pulizia log in corso...")
    
    # Pulisci cache URL
    cleanup_cache()
    
    # 1. Pulizia file per data
    log_files = [PERFORMANCE_FILE, FAILURE_FILE, ERROR_LOG_FILE, "debug_screenshot.png"]
    
    cutoff_time = time.time() - (days_to_keep * 86400)
    
    for log_file in log_files:
        if os.path.exists(log_file):
            try:
                file_mtime = os.path.getmtime(log_file)
                if file_mtime < cutoff_time:
                    os.remove(log_file)
                    print(f"  ✅ Rimosso log vecchio: {log_file}")
            except Exception as e:
                print(f"  ⚠️ Errore rimozione {log_file}: {e}")
    
    # 2. Limita dimensioni performance_log.txt
    if os.path.exists(PERFORMANCE_FILE):
        try:
            with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            if len(lines) > max_performance_entries:
                with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
                    # Mantieni ultime 1000 righe
                    f.writelines(lines[-max_performance_entries:])
                print(f"  📉 Performance log troncato: {len(lines)} → {max_performance_entries} righe")
        except Exception as e:
            print(f"  ⚠️ Errore cleanup performance log: {e}")
    
    # 3. Limita dimensioni failure_tracker.json
    if os.path.exists(FAILURE_FILE):
        try:
            with open(FAILURE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Mantieni solo fallimenti ultime 48 ore
            if "failures" in data:
                cutoff_date = (datetime.now() - timedelta(hours=48)).isoformat()
                old_keys = [k for k, v in data["failures"].items() 
                           if v.get("time", "") < cutoff_date]
                
                for key in old_keys:
                    del data["failures"][key]
                
                if old_keys:
                    print(f"  🗑️  Rimossi {len(old_keys)} fallimenti vecchi")
                    with open(FAILURE_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"  ⚠️ Errore cleanup failure tracker: {e}")
    
    # 4. Rimuovi screenshot debug vecchi
    try:
        for screenshot in glob.glob("debug_*.png"):
            if os.path.getmtime(screenshot) < cutoff_time:
                os.remove(screenshot)
                print(f"  🖼️  Rimosso screenshot vecchio: {screenshot}")
    except:
        pass
    
    print("✅ Pulizia log completata")

def decode_mollygram_url(proxy_url):
    """Decodifica URL Mollygram per ottenere link diretto Instagram"""
    try:
        parsed = urlparse(proxy_url)
        query_params = parse_qs(parsed.query)
        
        if 'media' not in query_params:
            return None
        
        media_param = query_params['media'][0]
        first_decode = unquote(media_param)
        final_url = unquote(first_decode) # Seconda decodifica
        
        if '%' in final_url:
            final_url = unquote(final_url)
            
        if 'instagram.com' in final_url or 'cdninstagram.com' in final_url:
            return final_url
        return final_url
    except Exception as e:
        print(f"❌ Errore decodifica Mollygram: {e}")
        return None

# ===============================
# FUNZIONI DI TRACKING E MONITORAGGIO
# ===============================

def track_performance(phase, duration):
    """Logga performance per fase"""
    try:
        with open(PERFORMANCE_FILE, "a", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{timestamp}|{phase}|{duration:.2f}\n")
    except Exception as e:
        print(f"⚠️ Errore log performance: {e}")
        
def log_semplice(messaggio):
    """Scrive un messaggio semplice da leggere su telefono"""
    # Prende l'ora attuale (es: "14:30")
    ora_attuale = datetime.now().strftime("%H:%M")
    
    # Crea la riga del log
    riga_log = f"[{ora_attuale}] {messaggio}"
    
    # La scrive nel file
    with open("log_telefono.txt", "a", encoding="utf-8") as file:
        file.write(riga_log + "\n")
    
    # Tiene solo le ultime 50 righe (per non ingrandire troppo)
    try:
        with open("log_telefono.txt", "r", encoding="utf-8") as file:
            tutte_righe = file.readlines()
        
        if len(tutte_righe) > 50:
            # Tieni solo le ultime 50
            ultime_50 = tutte_righe[-50:]
            with open("log_telefono.txt", "w", encoding="utf-8") as file:
                file.writelines(ultime_50)
    except:
        pass  # Se c'è errore, non fare niente
    
    # Stampa anche nella console
    print(riga_log)
    
def track_failure(site, status):
    """Traccia fallimenti consecutivi per ogni sito"""
    try:
        if os.path.exists(FAILURE_FILE):
            with open(FAILURE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {
                "failures": {},
                "consecutive_fails": {},
                "last_success": {},
                "stats": {"total_runs": 0, "successful_runs": 0}
            }
        
        now = datetime.now().isoformat()
        data["stats"]["total_runs"] = data["stats"].get("total_runs", 0) + 1
        
        if status in ["SUCCESS", "NO_STORIES", "SERVER_UNAVAILABLE"]:
            data["consecutive_fails"][site] = 0
            if status == "SUCCESS":
                data["last_success"][site] = now
                data["stats"]["successful_runs"] = data["stats"].get("successful_runs", 0) + 1
        else:
            current_fails = data["consecutive_fails"].get(site, 0)
            data["consecutive_fails"][site] = current_fails + 1
            
            # Log errore dettagliato
            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{now}|{site}|{status}|{current_fails + 1}\n")
        
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

def retry_with_backoff(func, max_retries=1, *args, **kwargs):
    """Esegue retry con backoff esponenziale per errori transienti - VERSIONE MIGLIORATA"""
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            start_time = time.time()
            result = func(*args, **kwargs)
            elapsed = time.time() - start_time
            
            if attempt > 0:
                print(f"✅ Retry {attempt} riuscito dopo {elapsed:.1f}s")
                
            return result
            
        except Exception as e:
            last_exception = e
            error_type = type(e).__name__
            error_msg = str(e)[:80]
            
            if attempt == max_retries:
                print(f"❌ Tutti i {max_retries + 1} tentativi falliti")
                print(f"   Ultimo errore: {error_type}: {error_msg}")
                # Log dettagliato per debugging
                with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now().isoformat()}|RETRY_FAILED|{error_type}|{error_msg}\n")
                raise e
            
            wait_time = (2 ** attempt) + 1  # 2, 3, 5 secondi...
            print(f"⚠️ Tentativo {attempt + 1}/{max_retries + 1} fallito ({error_type}). "
                  f"Ritento in {wait_time}s... ({error_msg})")
            time.sleep(wait_time)
    
    # Non dovrebbe mai arrivare qui, ma per sicurezza
    if last_exception:
        raise last_exception

def extract_real_url(iqsaved_url):
    """Estrae il vero URL Instagram da un link IQSaved - VERSIONE POTENZIATA"""
    try:
        # CASO 1: Nuovo formato IQSaved (img2.php?url=)
        if "img2.php?url=" in iqsaved_url:
            parsed = urlparse(iqsaved_url)
            query_params = parse_qs(parsed.query)
            if 'url' in query_params:
                real_url = query_params['url'][0]
                # Decodifica URL encoding (potrebbe esserci doppia codifica)
                real_url = unquote(unquote(real_url))
                print(f"   🔗 Estrazione: {real_url[:80]}...")
                return real_url
            return iqsaved_url
            
        # CASO 2: Vecchio formato IQSaved o altri
        if "filename=" in iqsaved_url:
            real_url = iqsaved_url.split("filename=")[1].split("&")[0]
            real_url = unquote(real_url)
            return real_url
            
        return iqsaved_url
        
    except Exception as e:
        print(f"⚠️ Errore estrazione URL da IQSaved: {e}")
        return iqsaved_url

def extract_instasaved_url(instasaved_url):
    """Estrae il vero URL Instagram da un link Instasaved - VERSIONE DEFINITIVA"""
    print(f"   🔧 extract_instasaved_url chiamata con: {instasaved_url[:80]}...")
    
    try:
        # Decodifica URL
        decoded_once = unquote(instasaved_url)
        print(f"   🔓 Decodificato 1: {decoded_once[:80]}...")
        
        # Cerca il parametro 'file'
        if "file=" in decoded_once:
            # Estrai tutto dopo file= fino alla fine o al prossimo &
            file_param = decoded_once.split("file=")[1].split("&")[0]
            file_decoded = unquote(file_param)
            print(f"   📄 Parametro file: {file_decoded[:80]}...")
            
            # I link Instasaved hanno due formati:
            # 1. https://stories-cdn.fun/aHR0cHM6Ly9zY29udGVudC1sZ2EzLTIuY2RuaW5zdGFncmFtLmNvbS8...
            # 2. https://cdn.storynavigation.com/?aHR0cHM6Ly9zY29udGVudC1sZ2EzLTMuY2RuaW5zdGFncmFtLmNvbS8
            
            # Cerca base64 nel percorso (dopo /)
            if "/aHR0cHM6Ly" in file_decoded:
                base64_part = file_decoded.split("/aHR0cHM6Ly")[1]
                base64_string = "aHR0cHM6Ly" + base64_part.split("?")[0].split("&")[0]
                print(f"   🔑 Base64 trovato (formato 1): {base64_string[:60]}...")
            
            # Cerca base64 nel query parameter (dopo ?)
            elif "?aHR0cHM6Ly" in file_decoded:
                base64_part = file_decoded.split("?aHR0cHM6Ly")[1]
                base64_string = "aHR0cHM6Ly" + base64_part.split("&")[0]
                print(f"   🔑 Base64 trovato (formato 2): {base64_string[:60]}...")
            
            else:
                # Prova regex per trovare base64 ovunque
                import re
                base64_match = re.search(r'(aHR0cHM6Ly[^&\s]+)', file_decoded)
                if base64_match:
                    base64_string = base64_match.group(1)
                    print(f"   🔑 Base64 trovato (regex): {base64_string[:60]}...")
                else:
                    print(f"   ⚠️ Nessun base64 trovato, ritorno originale")
                    return instasaved_url
            
            # Decodifica base64
            import base64
            try:
                # Aggiungi padding se necessario
                missing_padding = len(base64_string) % 4
                if missing_padding:
                    base64_string += "=" * (4 - missing_padding)
                
                decoded_bytes = base64.b64decode(base64_string)
                instagram_url = decoded_bytes.decode('utf-8')
                
                print(f"   ✅ URL Instagram estratto: {instagram_url[:80]}...")
                return instagram_url
                
            except Exception as e:
                print(f"   ❌ Errore decodifica base64: {e}")
                print(f"   Stringa base64: {base64_string}")
                return instasaved_url
        
        print(f"   ⚠️ Nessun parametro 'file' trovato")
        return instasaved_url
        
    except Exception as e:
        print(f"   💥 Errore generale: {e}")
        return instasaved_url

def validate_url_format(url: str) -> bool:
    """Valida formato URL base"""
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc and len(url) >= 10)
    except Exception:
        return False

def validate_links(links: List[str]) -> List[str]:
    """Valida che i link siano corretti e rimuovi malformati - VERSIONE OTTIMIZZATA"""
    if not links:
        return []
    
    valid_links = []
    invalid_count = 0
    
    print(f"🔍 Validazione di {len(links)} link...")
    
    for i, link in enumerate(links):
        if not link or not isinstance(link, str):
            invalid_count += 1
            continue
        
        link = link.strip()
        
        if len(link) < 10:
            invalid_count += 1
            continue
        
        original_link = link
        
        # CASO 1: Link IQSaved - converti
        if "cdn.iqsaved.com" in link:
            real_url = extract_real_url(link)
            if not real_url or real_url == link or not validate_url_format(real_url):
                invalid_count += 1
                continue
            link = real_url
        
        # CASO 2: Link Instasaved - converti
        elif "instasaved.net" in link and "download-file" in link:
            real_url = extract_instasaved_url(link)
            if not real_url or real_url == link or not validate_url_format(real_url):
                invalid_count += 1
                continue
            link = real_url
        
        # Validazione formato URL base
        if not validate_url_format(link):
            invalid_count += 1
            continue
        
        # Validazione pattern Instagram
        instagram_patterns = [
            "cdninstagram.com",
            "scontent.cdninstagram.com", 
            "fbcdn.net",
            "instagram.f",
            "instagram.com",
            "scontent-",
            ".mp4", ".jpg", ".jpeg", ".png", ".webp"
        ]
        
        is_valid = any(pattern in link.lower() for pattern in instagram_patterns)
        
        if not is_valid:
            invalid_count += 1
            if i < 5:  # Log solo i primi 5 per non intasare
                print(f"  [{i}] ❌ Non valido: {link[:100]}...")
            continue
        
        if not link.startswith(("http://", "https://")):
            invalid_count += 1
            continue
        
        valid_links.append(link)
    
    print(f"\n📊 RISULTATO: {len(valid_links)} validi, {invalid_count} invalidi")
    
    # Rimuovi duplicati (più efficiente con set)
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
        return True

def _get_url_timestamp(url: str, index: int) -> Tuple[str, float, int]:
    """Estrae timestamp da un URL (funzione helper per parallelizzazione) con caching"""
    global _url_cache
    
    # Controlla cache
    current_time = time.time()
    if url in _url_cache:
        cached_time, cached_timestamp = _url_cache[url]
        if current_time - cached_time < _cache_ttl and cached_timestamp is not None:
            return (url, cached_timestamp, index)
    
    try:
        # Prova HEAD request per ottenere Last-Modified
        response = requests.head(url, timeout=3, allow_redirects=True, stream=False)
        last_modified = response.headers.get('Last-Modified')
        
        if last_modified:
            try:
                date_obj = parsedate_to_datetime(last_modified)
                timestamp = date_obj.timestamp()
                # Salva in cache
                _url_cache[url] = (current_time, timestamp)
                return (url, timestamp, index)
            except Exception:
                pass
        
        # Fallback: usa Date header
        date_header = response.headers.get('Date')
        if date_header:
            try:
                date_obj = parsedate_to_datetime(date_header)
                timestamp = date_obj.timestamp()
                # Salva in cache
                _url_cache[url] = (current_time, timestamp)
                return (url, timestamp, index)
            except Exception:
                pass
        
        # Se non riesci a ottenere la data, mantieni l'ordine originale
        _url_cache[url] = (current_time, None)
        return (url, float('inf'), index)
        
    except Exception:
        # In caso di errore, mantieni l'ordine originale
        _url_cache[url] = (current_time, None)
        return (url, float('inf'), index)

def sort_links_chronologically(links: List[str]) -> List[str]:
    """Ordina i link cronologicamente usando le date HTTP (più recenti prima) - VERSIONE PARALLELA"""
    if not links or len(links) == 1:
        return links
    
    print(f"📅 Ordinamento cronologico di {len(links)} link (parallelizzato)...")
    
    # Limita a max 100 link per non rallentare troppo (aumentato grazie alla parallelizzazione)
    max_links_to_sort = 100
    links_to_sort = links[:max_links_to_sort]
    remaining_links = links[max_links_to_sort:]
    
    links_with_dates = []
    
    # Parallelizza le richieste HTTP (max 10 worker per non sovraccaricare)
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {
            executor.submit(_get_url_timestamp, url, i): url 
            for i, url in enumerate(links_to_sort)
        }
        
        for future in as_completed(future_to_url):
            try:
                result = future.result()
                links_with_dates.append(result)
            except Exception as e:
                # In caso di errore nel future, usa fallback
                url = future_to_url[future]
                idx = links_to_sort.index(url) if url in links_to_sort else 0
                links_with_dates.append((url, float('inf'), idx))
    
    # Ordina per timestamp (decrescente = più recenti prima)
    # Se stesso timestamp, mantieni ordine originale
    links_with_dates.sort(key=lambda x: (-x[1], x[2]))
    
    sorted_links = [url for url, _, _ in links_with_dates]
    
    # Aggiungi i link rimanenti in ordine originale
    sorted_links.extend(remaining_links)
    
    print(f"✅ Ordinamento completato ({len(sorted_links)} link)")
    return sorted_links

# ===============================
# CONFIGURAZIONE
# ===============================

IG_USER = os.environ.get("IG_USER", "").strip()
if not IG_USER or not IG_USER.replace('_', '').replace('.', '').isalnum():
    print("❌ ERRORE: Nome Instagram non valido!")
    exit(1)

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

# Verifica variabili obbligatorie
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TOKEN:
    print("❌ ERRORE: TELEGRAM_TOKEN non impostato!")
    exit(1)

CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not CHAT_ID:
    print("❌ ERRORE: TELEGRAM_CHAT_ID non impostato!")
    exit(1)

OCR_KEY = os.environ.get("OCR_KEY", "").strip()

# ===============================
# FUNZIONI CORE
# ===============================
 
def get_clean_id(url):
    """Estrai ID univoco dal link - VERSIONE PER INSTASAVED"""
    try:
        # Per link Instasaved, usa il parametro 'file'
        if "instasaved.net/download-file" in url:
            from urllib.parse import urlparse, parse_qs, unquote
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            if 'file' in query:
                # Decodifica l'URL Instagram nascosto
                insta_url = unquote(query['file'][0])
                if "/" in insta_url:
                    return insta_url.split("/")[-1].split("?")[0]
                return insta_url[-20:]
        
        # Per altri tipi di link (Instagram diretti)
        if "/" in url:
            return url.split("/")[-1].split("?")[0]
        return url[-20:]
        
    except Exception:
        # Fallback: usa una parte dell'URL come ID
        try:
            return url.split("/")[-1].split("?")[0][:20]
        except:
            return url[-20:] if len(url) > 20 else url

def send_telegram(text, media_url=None, is_video=False):
    """Invia messaggio a Telegram con connection pooling"""
    api_url = f"https://api.telegram.org/bot{TOKEN}/"
    method = "sendVideo" if is_video else "sendPhoto"
    
    log_text = text[:80] + "..." if len(text) > 80 else text
    print(f"✈️ Invio Telegram: {log_text}")
    
    try:
        if media_url:
            # ASSICURIAMOCI che l'URL non sia un link IQSaved
            if "cdn.iqsaved.com/img2.php" in media_url:
                print("🚨 ATTENZIONE: Tentativo di inviare link IQSaved a Telegram!")
                media_url = extract_real_url(media_url)
                print(f"   🔄 Convertito in: {media_url[:80]}...")
            
            payload = {"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"}
            files_key = 'video' if is_video else 'photo'
            
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
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=30
            )
            response.raise_for_status()
            
    except Exception as e:
        print(f"❌ Errore invio Telegram: {e}")
        
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
        except:
            pass

@lru_cache(maxsize=100)
def ocr_scan(image_url: str) -> str:
    """Esegue OCR su immagine con caching per evitare richieste duplicate"""
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
# MOTORI DI SCRAPING OTTIMIZZATI
# ===============================

def retry_storiesviewer(page, max_retries=3):
    """Tenta StoriesViewer con retry automatico e refresh - VERSIONE CON PIÙ RETRY"""
    print(f"🔄 Tentativo StoriesViewer con {max_retries} retry...")
    
    for attempt in range(max_retries + 1):
        try:
            print(f"  Tentativo {attempt + 1}/{max_retries + 1}")
            links, status, error_details = check_storiesviewer(page)
            
            if links or status in ["NO_STORIES", "SERVER_UNAVAILABLE"]:
                return links, status, error_details
            
            # Se nessun link ma non è NO_STORIES, riprova con refresh
            if attempt < max_retries:
                wait_time = 3 + (attempt * 2)  # 3s, 5s, 7s...
                print(f"  ⏳ Nessun link trovato, refresh e riprovo tra {wait_time}s...")
                page.reload()
                time.sleep(wait_time)
                
        except Exception as e:
            if attempt < max_retries:
                wait_time = 3 + (attempt * 2)
                print(f"  ⚠️ Errore, riprovo tra {wait_time}s: {str(e)[:80]}")
                page.reload()
                time.sleep(wait_time)
            else:
                return [], "RETRY_FAILED", str(e)
    
    return [], "ALL_RETRIES_FAILED", "Tutti i tentativi falliti"

def check_storiesviewer(page):
    """Scarica storie da StoriesViewer.net con timeout ottimizzati"""
    print(f"⏩ Controllo StoriesViewer.net...")
    
    target_url = "https://storiesviewer.net/it/"
    links = []
    status = "UNKNOWN"
    error_details = ""
    start_time = time.time()
    
    # Usa timeout adattivo (coerente con altri siti)
    adjusted_timeout = get_adaptive_timeout("StoriesViewer", 25000)
    print(f"⏱️ Timeout adattivo StoriesViewer: {adjusted_timeout/1000:.0f}s")
    
    try:
        response = page.goto(target_url, timeout=adjusted_timeout, wait_until="domcontentloaded")
        
        if response.status != 200:
            status = "HTTP_ERROR"
            error_details = f"Status {response.status}"
            print(f"❌ StoriesViewer HTTP Error: {response.status}")
            track_failure("StoriesViewer", status)
            return links, status, error_details
        
        try:
            page.click("button:has-text('Consent'), .fc-cta-consent", timeout=2000)
        except:
            pass
        
        try:
            search_input = page.locator('input[name="url"], input[type="text"]').first
            search_input.wait_for(state="visible", timeout=8000)
            search_input.click()
            search_input.fill(IG_USER)
            time.sleep(0.5)
            
            search_btn = page.locator('button[type="submit"], button:has(i), button.btn-default').first
            search_btn.wait_for(state="visible", timeout=4000)
            search_btn.click()
            print("🖱️ Lente cliccata!")
            
        except Exception as e:
            status = "INPUT_ERROR"
            error_details = f"Input non trovato: {str(e)[:100]}"
            print(f"⚠️ Errore fase ricerca: {e}")
            track_failure("StoriesViewer", status)
            return links, status, error_details

        try:
            try:
                page.wait_for_selector('text="Caricamento", text="Loading"', state='hidden', timeout=15000)
                print("✅ Caricamento completato.")
            except:
                print("ℹ️ Nessun indicatore di caricamento")
                pass
            
            try:
                page.wait_for_selector('text="Sorry, the server is temporarily unavailable"', timeout=3000)
                status = "SERVER_UNAVAILABLE"
                error_details = "Server temporaneamente non disponibile"
                print("ℹ️ StoriesViewer: Server temporaneamente non disponibile")
                track_failure("StoriesViewer", status)
                return links, status, error_details
            except:
                pass
            
            try:
                page.wait_for_selector('text="No stories found", text="Nessuna storia", text="not found"', timeout=3000)
                status = "NO_STORIES"
                error_details = "Profilo senza storie o privato"
                print("ℹ️ StoriesViewer: Nessuna storia trovata")
                track_failure("StoriesViewer", status)
                return links, status, error_details
            except:
                pass
                
            page.wait_for_selector('a:has-text("Download HD"), .story-item, .stories-container', timeout=15000)
            print("✨ Elementi storie trovati!")
            
        except Exception as e:
            status = "TIMEOUT"
            error_details = f"Timeout caricamento: {str(e)[:100]}"
            print("⚠️ Timeout caricamento storie")
        
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
            
        if elapsed > 25000:
            print(f"⚠️ ATTENZIONE: StoriesViewer lento ({elapsed:.1f}s)")
            
        return links, status, error_details
        
    except Exception as e:
        status = "CRASH"
        error_details = f"Errore generale: {str(e)[:150]}"
        print(f"❌ Errore StoriesViewer: {e}")
        track_failure("StoriesViewer", status)
        return links, status, error_details

def check_mollygram(page):
    """Scarica storie da Mollygram.com (Versione CLICK + ATTESA 15s + SCROLL)"""
    # Maschera il nome utente per i log
    user_masked = IG_USER[:3] + "***" if len(IG_USER) > 3 else "***"
    
    base_url = "https://mollygram.com/it"
    max_retries = 3  # Mollygram è prioritario, diamo 3 tentativi
    
    print(f"🦄 Controllo MOLLYGRAM (PRIORITARIO) per {user_masked}...")
    
    for attempt in range(max_retries):
        print(f"\n🔄 TENTATIVO {attempt + 1}/{max_retries}...")
        
        links = []
        status = "UNKNOWN"
        start_time = time.time()
        
        try:
            # 1. Navigazione con TIMEOUT DINAMICO
            try:
                timeout = get_adaptive_timeout("Mollygram", 30000)
                print(f"  ⏱️ Timeout adattivo: {timeout/1000:.0f}s")
                response = page.goto(base_url, timeout=timeout, wait_until="domcontentloaded")
            except:
                print("  ⚠️ Timeout caricamento pagina iniziale")
                continue

            time.sleep(2)
            
            # Pulizia Banner Cookie via JavaScript (come originale - funziona al primo tentativo)
            try:
                page.evaluate("""
                    () => {
                        const blockers = document.querySelectorAll('.fc-consent-root, .fc-ab-root, .fc-dialog-overlay, .fc-dialog-container, [class*="cookie"], [id*="cookie"], [class*="consent"]');
                        blockers.forEach(el => el.remove());
                        document.body.style.overflow = 'auto';
                        document.documentElement.style.overflow = 'auto';
                    }
                """)
            except:
                pass
            
            # 2. Ricerca con CLICK
            print(f"  🔍 Inserisco username: {user_masked}")
            try:
                search_input = page.wait_for_selector('input[placeholder*="Username"], input[type="text"]', timeout=10000)
                search_input.click()
                search_input.fill(IG_USER)
                time.sleep(0.5)
                
                print("  🖱️  Clicco 'Vedere'...")
                search_btn = page.wait_for_selector('button:has-text("Vedere"), button:has-text("Vedi"), [type="submit"], button[class*="search"]', timeout=5000)
                search_btn.click()
                
                # Piccolo delay per dare tempo al server di iniziare l'elaborazione
                time.sleep(2)
                
            except Exception as e:
                print(f"  ⚠️ Errore fase input: {e}")
                continue

            # 3. ATTESA FISSA (come originale - più affidabile)
            print("  ⏳ Attendo 15 secondi che il sito carichi...")
            for s in range(15, 0, -1):
                if s % 5 == 0: 
                    print(f"     ...{s}s")
                time.sleep(1)
            
            # 4. Scroll Aggressivo (valori originali per affidabilità)
            print("  📜 Scroll per sbloccare le immagini...")
            found_any = False
            
            # 5 scroll come originale
            for i in range(5): 
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)  # Attesa originale 2s
                
                # Controllo rapido se è apparso qualcosa
                if page.query_selector('a:has-text("DOWNLOAD HD"), button:has-text("DOWNLOAD HD")'):
                    found_any = True
                    print(f"  ✅ Bottoni trovati dopo {i+1} scroll")
                    break
                
                # Scroll leggermente su e giù per sbloccare lazy load
                page.evaluate("window.scrollBy(0, -300)")
                time.sleep(1)  # Attesa originale 1s
            
            if not found_any:
                 print("  ⚠️ Nessun bottone 'DOWNLOAD HD' apparso durante lo scroll")
                 # Check errori comuni
                 if page.query_selector('text="Not found"') or page.query_selector('text="Non trovato"'):
                     print("  ⚠️ Utente non trovato")
                     if attempt == max_retries - 1: return [], "NO_STORIES", "User not found"
                     continue

            # 5. Estrazione Link
            print("  🔗 Estrazione link...")
            proxy_urls = []
            
            elements = page.query_selector_all('a[href*="anon-viewer.com/media.php"], a:has-text("DOWNLOAD HD"), button:has-text("DOWNLOAD HD")')
            
            for el in elements:
                try:
                    href = el.get_attribute('href')
                    if not href:
                         href = el.evaluate("el => el.closest('a') ? el.closest('a').href : null")
                    
                    if href and 'anon-viewer.com/media.php' in href:
                        if href.startswith('/'): href = f"https://anon-viewer.com{href}"
                        elif not href.startswith('http'): href = f"https://{href}"
                        
                        if href not in proxy_urls: 
                            proxy_urls.append(href)
                except:
                    continue
            
            print(f"  📊 Trovati {len(proxy_urls)} link.")
            
            if len(proxy_urls) > 0:
                for p_url in proxy_urls:
                    direct = decode_mollygram_url(p_url)
                    if direct: links.append(direct)
                
                elapsed = time.time() - start_time
                status = "SUCCESS"
                print(f"✅ MOLLYGRAM: {len(links)} link trovati in {elapsed:.1f}s")
                track_failure("Mollygram", status)
                return links, status, ""
            
            else:
                print("  ⚠️ 0 link trovati.")
                if attempt < max_retries - 1:
                    print("  🔄 Ricarico e riprovo...")
                    time.sleep(2)
            
        except Exception as e:
            print(f"  ❌ Errore imprevisto: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
            
    return [], "NO_LINKS", "Nessun link trovato dopo i tentativi"

def safe_check_mollygram(page):
    """Wrapper sicuro per Mollygram"""
    try:
        return check_mollygram(page)
    except Exception as e:
        print(f"💀 Crash Mollygram wrapper: {e}")
        return [], "FATAL_ERROR", str(e)

def check_iqsaved(page):
    """Scarica storie da IQSaved.com - Versione semplificata POST-CAMBIO"""
    print(f"🔍 Controllo IQSAVED per {IG_USER}...")
    
    target_url = f"https://iqsaved.com/it/viewer/{IG_USER}/"
    links = []
    status = "UNKNOWN"
    start_time = time.time()
    
    try:
        timeout = get_adaptive_timeout("IQSaved", 25000)
        print(f"  ⏱️ Timeout adattivo: {timeout/1000:.0f}s")
        response = page.goto(target_url, timeout=timeout, wait_until="domcontentloaded")
        if response.status != 200:
            status = "HTTP_ERROR"
            print(f"❌ IQSaved HTTP Error: {response.status}")
            track_failure("IQSaved", status)
            return links, status, f"Status {response.status}"
        
        time.sleep(4)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        
        # STRATEGIA 1: Cerca i pulsanti "SCARICA" e prendi il loro link href
        download_buttons = page.query_selector_all('a:has-text("SCARICA"), button:has-text("SCARICA")')
        for btn in download_buttons:
            href = btn.get_attribute('href')
            if href and 'cdn.iqsaved.com' in href:
                links.append(href)
        
        # STRATEGIA 2: Cerca TUTTI i link che contengono 'img2.php'
        all_links = page.query_selector_all('a[href*="img2.php"]')
        for link in all_links:
            href = link.get_attribute('href')
            if href:
                links.append(href)
        
        # Rimuovi duplicati
        links = list(set(links))
        print(f"  📊 Trovati {len(links)} link (strategia post-cambio).")
        
        elapsed = time.time() - start_time
        
        if links:
            status = "SUCCESS"
            print(f"✅ IQSaved (POST-CAMBIO): {len(links)} link in {elapsed:.1f}s")
            track_failure("IQSaved", status)
            return links, status, ""
        else:
            status = "NO_LINKS"
            print(f"⚠️ IQSaved: nessun link trovato dopo il cambio di sistema.")
            track_failure("IQSaved", status)
            return [], status, "Sistema cambiato, nessun link estraibile"
            
    except Exception as e:
        status = "CRASH"
        print(f"❌ Errore IQSaved: {e}")
        track_failure("IQSaved", status)
        return [], status, str(e)

def check_instasaved(page):
    """Scarica storie da Instasaved.net - VERSIONE DEFINITIVA (link diretti)"""
    print(f"🚀 Controllo INSTASAVED (PRIMARIO) per {IG_USER}...")
    
    target_url = f"https://instasaved.net/it/save-stories/{IG_USER}/"
    links = []
    status = "UNKNOWN"
    error_details = ""
    start_time = time.time()
    
    try:
        timeout = get_adaptive_timeout("Instasaved", 25000)
        print(f"   ⏱️ Timeout adattivo: {timeout/1000:.0f}s")
        response = page.goto(target_url, timeout=timeout, wait_until="domcontentloaded")
        
        if response.status != 200:
            status = "HTTP_ERROR"
            error_details = f"Status {response.status}"
            print(f"❌ Instasaved HTTP Error: {response.status}")
            track_failure("Instasaved", status)
            return links, status, error_details
        
        time.sleep(2)
        
        # Gestione cookie
        try:
            page.click("text=Acconsento", timeout=3000)
            print("   ✅ Cookie accettati.")
            time.sleep(1)
        except:
            pass
        
        # Scroll veloce per caricare tutto
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        
        # CERCA TUTTI i link di download direttamente (strategia sicura)
        print("   🔍 Cerco link di download nella pagina...")
        download_elements = page.locator('a[href*="download-file"]').all()
        
        if not download_elements:
            status = "NO_LINKS"
            error_details = "Nessun link 'download-file' trovato"
            print("⚠️ Instasaved: nessun link trovato")
            track_failure("Instasaved", status)
            return links, status, error_details
        
        # Estrai gli URL unici
        unique_links = []
        for element in download_elements:
            href = element.get_attribute('href')
            if href and href.startswith('http') and href not in unique_links:
                unique_links.append(href)
        
        # Usa i link trovati
        links = unique_links
        
        elapsed = time.time() - start_time
        
        if links:
            # Conta foto vs video (solo per log)
            video_count = sum(1 for link in links if 'fileType=video' in link or 'video/mp4' in link)
            photo_count = len(links) - video_count
            
            status = "SUCCESS"
            print(f"✅ INSTASAVED: {len(links)} link ({photo_count} foto, {video_count} video) in {elapsed:.1f}s")
            track_failure("Instasaved", status)
            track_performance("Instasaved", elapsed)
        else:
            status = "NO_LINKS"
            print(f"⚠️ Instasaved: nessun link valido in {elapsed:.1f}s")
            track_failure("Instasaved", status)
            
        return links, status, error_details
        
    except Exception as e:
        status = "CRASH"
        error_details = f"Errore: {str(e)[:150]}"
        print(f"❌ Errore Instasaved: {e}")
        track_failure("Instasaved", status)
        return links, status, error_details

# ===============================
# FUNZIONI DI RECOVERY
# ===============================

def safe_check_instasaved(page):
    """Wrapper con gestione errori robusta per Instasaved"""
    try:
        print("🔒 Esecuzione sicura Instasaved (PRIMARIO)...")
        return check_instasaved(page)
    except Exception as e:
        error_msg = f"💥 CRASH GRAVE Instasaved: {str(e)[:200]}"
        print(error_msg)
        return [], "FATAL_ERROR", f"Crash: {str(e)[:100]}"

def safe_check_storiesviewer(page):
    """Wrapper con gestione errori robusta - CON PIÙ RETRY"""
    try:
        print("🔒 Esecuzione sicura StoriesViewer...")
        return retry_storiesviewer(page, max_retries=3)
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

# ===============================
# CODICE PER CREARE FILE DEBUG
# ===============================

def crea_file_debug():
    """Crea file di debug vuoti per GitHub Actions"""
    debug_files = [
        "debug.html",
        "debug.png",
        "iqsaved_debug.html",
        "playwright_logs.txt"
    ]
    
    for file in debug_files:
        try:
            with open(file, "w", encoding="utf-8") as f:
                if file.endswith(".txt"):
                    f.write(f"Debug file creato il: {datetime.now()}\n")
                    f.write("Il bot non ha creato file debug reali.\n")
                elif file.endswith(".html"):
                    f.write(f"<!-- Debug HTML creato il {datetime.now()} -->\n")
                    f.write("<html><body><h1>Debug placeholder</h1></body></html>")
            print(f"✅ Creato file debug placeholder: {file}")
        except:
            print(f"⚠️ Non ho potuto creare: {file}")

# ===============================
# FUNZIONE PRINCIPALE OTTIMIZZATA
# ===============================

def run():
    """Funzione principale del bot"""
    print("=" * 60)
    print("🚀 AVVIO BOT SU GITHUB ACTIONS")
    print("=" * 60)
    
    # Debug ambiente
    import sys
    print(f"Python: {sys.version}")
    print(f"Playwright disponibile: {'playwright' in sys.modules}")
    print("IG_USER impostato:", "SI" if IG_USER else "NO")
    print("TELEGRAM_TOKEN impostato:", "SI" if TOKEN else "NO")
    print("CHAT_ID impostato:", "SI" if CHAT_ID else "NO")

    
    # Continua con il codice esistente...
    crea_file_debug()
    cleanup_old_logs(7)

    # Backup automatico history
    if os.path.exists("history.txt"):
        import shutil
        data_oggi = datetime.now().strftime("%Y%m%d")
        backup_file = f"history_backup_{data_oggi}.txt"
        if not os.path.exists(backup_file):
            shutil.copy2("history.txt", backup_file)
            print(f"💾 Backup creato: {backup_file}")
        
        # Tieni solo ultimi 7 backup
        backups = sorted([f for f in os.listdir(".") if f.startswith("history_backup_")])
        for old_backup in backups[:-7]:
            os.remove(old_backup)
            print(f"🗑️  Rimosso vecchio backup: {old_backup}")
    
    log_semplice("🚀 Avvio Bot Ibrido Avanzato...")
    
    start_total = time.time()
    phase_timers = {
        "setup": 0,
        "instasaved": 0, 
        "storiesviewer": 0,
        "iqsaved": 0,
        "processing": 0,
        "telegram": 0
    }
    
    browser = None
    context = None
    
    try:
        phase_start = time.time()
        
        seen_ids = []
        if os.path.exists("history.txt"):
            with open("history.txt", "r", encoding="utf-8") as f:
                seen_ids = [line.strip() for line in f if line.strip()]
        
        updated_history = seen_ids.copy()
        ids_to_add = []
        
        phase_timers["setup"] = time.time() - phase_start
        
        with sync_playwright() as p:
            # BROWSER OTTIMIZZATO PER VELOCITÀ
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
                    '--max_old_space_size=256',
                    '--disable-features=site-per-process,TranslateUI',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-background-timer-throttling',
                    '--disable-renderer-backgrounding',
                    '--disable-backgrounding-occluded-windows',
                ]
            )
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.7499.170 Safari/537.36",
                viewport={'width': 1280, 'height': 800},
                locale="it-IT",
                timezone_id="Europe/Rome",
                permissions=['geolocation'],  # Simula utente con permessi attivi
                device_scale_factor=1,        # Evita discrepanze nel rendering
                extra_http_headers={
                    # Client Hints per Windows 11 (19.0.0 = Build 2025)
                    "Sec-CH-UA": "\"Google Chrome\";v=\"143\", \"Chromium\";v=\"143\", \"Not?A_Brand\";v=\"99\"",
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": "\"Windows\"",
                    "Sec-CH-UA-Platform-Version": "\"19.0.0\"",
                    # Accept-Language con coda inglese realistica
                    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                    "DNT": "1"  # Do Not Track, comune tra utenti reali
                }
            )
            
            page = context.new_page()
            
            # Applica stealth per mascherare il bot (riduce blocchi anti-bot)
            # playwright-stealth 2.0+ applica a tutto il context
            if STEALTH_AVAILABLE and STEALTH_INSTANCE:
                try:
                    STEALTH_INSTANCE.apply_stealth_sync(context)
                    print("🎭 Playwright-stealth 2.0 attivato")
                except Exception as e:
                    print(f"⚠️ Stealth non applicato: {e}")
            
            # ==========================================
            # NUOVO ORDINE PRIORITÀ: MOLLYGRAM -> STORIESVIEWER -> INSTASAVED -> IQSAVED
            # ==========================================
            
            # Maschera utente per i log generali
            user_masked = IG_USER[:3] + "***" if len(IG_USER) > 3 else "***"
            
            # 1. MOLLYGRAM (Priorità Assoluta)
            print("\n=== FASE 1: MOLLYGRAM (PRIORITARIO) ===")
            links_molly, molly_status, molly_error = [], "NOT_TESTED", ""
            try:
                links_molly, molly_status, molly_error = retry_with_backoff(
                    safe_check_mollygram, max_retries=1, page=page
                )
            except Exception as e:
                print(f"Errore chiamata Mollygram: {e}")
                molly_status = "CRASH"
            
            # 2. STORIESVIEWER (Esegue SOLO se Mollygram fallisce)
            print("\n=== FASE 2: STORIESVIEWER ===")
            links_viewer, storiesviewer_status, storiesviewer_error = [], "NOT_TESTED", ""
            
            if not links_molly:
                print("⚠️ Mollygram non ha trovato storie, provo StoriesViewer...")
                try:
                    links_viewer, storiesviewer_status, storiesviewer_error = retry_with_backoff(
                        safe_check_storiesviewer, max_retries=1, page=page
                    )
                except Exception as e:
                    print(f"Errore StoriesViewer: {e}")
                    storiesviewer_status = "CRASH"
            else:
                print("ℹ️ StoriesViewer saltato (Mollygram ha funzionato)")
                storiesviewer_status = "SKIPPED_SUCCESS"

            # 3. INSTASAVED (Terza Scelta)
            print("\n=== FASE 3: INSTASAVED ===")
            links_insta, insta_status, insta_error = [], "NOT_TESTED", ""
            
            if not links_molly and not links_viewer:
                 try:
                    links_insta, insta_status, insta_error = retry_with_backoff(
                        safe_check_instasaved, max_retries=1, page=page
                    )
                 except:
                    insta_status = "CRASH"
            else:
                print("ℹ️ Instasaved saltato (Mollygram o StoriesViewer hanno funzionato)")
                insta_status = "SKIPPED_SUCCESS"

            # 4. IQSAVED (Ultima spiaggia)
            links_iq, iqsaved_status, iqsaved_error = [], "NOT_TESTED", ""
            if not links_molly and not links_viewer and not links_insta:
                print("\n=== FASE 4: IQSAVED (RISERVA) ===")
                try:
                    links_iq, iqsaved_status, iqsaved_error = retry_with_backoff(
                        safe_check_iqsaved, max_retries=1, page=page
                    )
                except:
                    iqsaved_status = "CRASH"
            
            # UNISCI TUTTI I LINK (senza duplicati)
            all_links = []
            seen_urls = set()
            
            def add_links_safe(source_links):
                for url in source_links:
                    clean_id = get_clean_id(url)
                    if clean_id and clean_id not in seen_urls:
                        all_links.append(url)
                        seen_urls.add(clean_id)
            
            add_links_safe(links_molly)
            add_links_safe(links_viewer)
            add_links_safe(links_insta)
            add_links_safe(links_iq)
            
            print(f"📊 Link unificati: {len(all_links)} (Molly: {len(links_molly)}, Viewer: {len(links_viewer)}, Insta: {len(links_insta)}, IQ: {len(links_iq)})")
            
            # Chiudi browser ASAP
            try:
                context.close()
                browser.close()
            except:
                pass
        
        # PROCESSING
        phase_start = time.time()
        
        tutti_i_link = validate_links(all_links)
        log_semplice(f"📦 Totale storie trovate: {len(tutti_i_link)}")
        
        # Ordina cronologicamente (più recenti prima)
        if tutti_i_link:
            try:
                tutti_i_link = sort_links_chronologically(tutti_i_link)
            except Exception as e:
                print(f"⚠️ Errore ordinamento cronologico: {e}, uso ordine originale")
        
        # Rimuovi duplicati basati su ID e filtra già viste
        storie_da_processare = []
        seen_processing_ids = set(seen_ids)
        
        for url in tutti_i_link:
            clean_id = get_clean_id(url)
            if clean_id and clean_id not in seen_processing_ids:
                storie_da_processare.append({'url': url, 'id': clean_id})
                seen_processing_ids.add(clean_id)  # Evita duplicati anche tra nuove
        
        num_nuove = len(storie_da_processare)
        
        if num_nuove > 0:
            print(f"📋 {num_nuove} nuove storie da processare (ordinate cronologicamente)")
        
        phase_timers["processing"] = time.time() - phase_start
        
        # INVIO TELEGRAM
        phase_start = time.time()
        
        if num_nuove > SOGLIA_ALLUVIONE:
            print(f"⚠️ FLOOD GUARD ({num_nuove} > {SOGLIA_ALLUVIONE}). Skip invio.")
            for item in storie_da_processare:
                ids_to_add.append(item['id'])
        elif num_nuove > 0:
            log_semplice(f"📨 Invio {num_nuove} nuove storie...")
            
            # Rate limiting intelligente: più veloce all'inizio, più lento dopo
            base_delay = 1.2
            max_delay = 3.5
            consecutive_success = 0
            
            for i, item in enumerate(storie_da_processare):
                url = item['url']
                clean_id = item['id']
                
                # GARANTISCI che l'URL sia diretto, non IQSaved
                if "cdn.iqsaved.com/img2.php" in url:
                    print(f"🔄 Conversione link IQSaved per invio {i+1}...")
                    url = extract_real_url(url)
                    if not url:
                        print(f"❌ Impossibile convertire link, salto storia {i+1}")
                        continue
                
                is_video = ".mp4" in url.lower() or "video" in url.lower()
                tipo = "VIDEO" if is_video else "FOTO"
                
                dida = f"Storia {i+1}/{num_nuove}"
                
                if tipo == "FOTO" and OCR_KEY:
                    txt = ocr_scan(url)
                    if txt:
                        found_keyword = next((k for k in PAROLE_CHIAVE if k in txt), None)
                        if found_keyword:
                            dida = f"Storia su {found_keyword.title()}"
                
                try:
                    send_telegram(dida, url, is_video)
                    ids_to_add.append(clean_id)
                    consecutive_success += 1
                    
                    # Rate limiting adattivo: se tutto va bene, accelera leggermente
                    if i < len(storie_da_processare) - 1:
                        # Delay progressivo ma con ottimizzazione per successi consecutivi
                        sleep_time = base_delay + (i * 0.25) - (consecutive_success * 0.05)
                        sleep_time = max(0.8, min(sleep_time, max_delay))
                        time.sleep(sleep_time)
                except Exception as e:
                    print(f"⚠️ Errore invio storia {i+1}: {e}")
                    consecutive_success = 0
                    # In caso di errore, aumenta il delay
                    if i < len(storie_da_processare) - 1:
                        time.sleep(max_delay)
        
        phase_timers["telegram"] = time.time() - phase_start
        
        # SALVA HISTORY
        # (Assicurati che questo blocco sia allineato con le altre fasi, es. 8 spazi)
        if ids_to_add and check_disk_space():
            updated_history = seen_ids + ids_to_add
            
            if len(updated_history) > MAX_HISTORY:
                updated_history = updated_history[-MAX_HISTORY:]
                print(f"📊 History troncata a {MAX_HISTORY} elementi")
            
            with open("history.txt", "w", encoding="utf-8") as f:
                for sid in updated_history:
                    if sid.strip():
                        f.write(f"{sid}\n")
            
            print(f"💾 History aggiornata: {len(updated_history)} elementi")
        
        # ===============================
        # HEALTH CHECK AGGIORNATO (Versione Sicura)
        # ===============================
        # IMPORTANTE: Questo print deve essere allineato con "SALVA HISTORY" sopra (8 spazi)
        print("\n🔍 Health Check dettagliato...")
        
        # Inizializzazione variabili (Deve essere fuori da qualsiasi IF precedente)
        send_alert = False
        alert_message = ""
        
        # Definiamo chi ha funzionato davvero
        # Usa variabili difensive per evitare NameError se qualcosa è andato storto prima
        molly_ok = (locals().get('molly_status') == "SUCCESS") and (len(locals().get('links_molly', [])) > 0)
        viewer_ok = (locals().get('storiesviewer_status') == "SUCCESS") and (len(locals().get('links_viewer', [])) > 0)
        insta_ok = (locals().get('insta_status') == "SUCCESS") and (len(locals().get('links_insta', [])) > 0)
        iq_ok = (locals().get('iqsaved_status') == "SUCCESS") and (len(locals().get('links_iq', [])) > 0)
        
        any_success = molly_ok or viewer_ok or insta_ok or iq_ok
        
        # --- CONTROLLO SINGOLI SITI ---
        
        # Se Mollygram è stato testato (non skippato) e ha fallito
        m_status = locals().get('molly_status', 'UNKNOWN')
        if m_status not in ["NOT_TESTED", "SKIPPED_SUCCESS", "SUCCESS", "NO_LINKS"]:
             alert_message += f"⚠️ Mollygram issue: {m_status}\n"
        
        # Se Instasaved è stato testato e ha fallito
        i_status = locals().get('insta_status', 'UNKNOWN')
        if i_status in ["HTTP_ERROR", "CRASH", "TIMEOUT"]:
            alert_message += f"🔴 Instasaved issue: {i_status}\n"
            send_alert = True
            
        # Se StoriesViewer è stato testato e ha fallito
        v_status = locals().get('storiesviewer_status', 'UNKNOWN')
        if v_status in ["HTTP_ERROR", "CRASH"]:
            alert_message += f"🔴 StoriesViewer issue: {v_status}\n"
            send_alert = True
            
        # --- ALLARME CRITICO GENERALE ---
        # Scatta SOLO se NESSUNO ha portato a casa il risultato
        if not any_success:
            # Caso speciale: Se tutti dicono "NO_LINKS", non è un crash, ma forse non ci sono storie
            all_no_links = (
                (m_status in ["NO_LINKS", "NOT_TESTED"]) and
                (v_status in ["NO_LINKS", "NOT_TESTED", "SKIPPED_SUCCESS"]) and
                (i_status in ["NO_LINKS", "NOT_TESTED", "SKIPPED_SUCCESS"])
            )
            
            if not all_no_links:
                print("🚨 ALLARME CRITICO: Tutti i motori hanno fallito!")
                critical_alert = (
                    f"🔴 CRITICO: Nessun sito è riuscito a scaricare le storie!\n\n"
                    f"📊 STATO:\n"
                    f"1. Mollygram: {m_status}\n"
                    f"2. StoriesViewer: {v_status}\n"
                    f"3. Instasaved: {i_status}\n\n"
                    f"Intervento richiesto su {user_masked}!"
                )
                send_telegram(critical_alert)
            else:
                print("ℹ️ Nessuna storia trovata su nessun sito (non è un errore tecnico).")

        # Se c'è un alert parziale ma il download è riuscito
        elif send_alert:
            print("⚠️ Rilevati problemi su alcuni mirror secondari, ma il download è riuscito.")
        
        print(f"\n📋 Riepilogo Status:")
        print(f"   Mollygram:     {m_status}")
        print(f"   StoriesViewer: {v_status}")
        print(f"   Instasaved:    {i_status}")
        
        # ANALISI PERFORMANCE
        total_time = time.time() - start_total
        print(f"\n⏱️ ANALISI PERFORMANCE:")
        print(f"  Totale: {total_time:.1f}s")
        
        if total_time > 90:
            print(f"⚠️ AVVISO: Bot lento ({total_time:.1f}s)")
            
        print(f"\n✅ BOT COMPLETATO")
        log_semplice(f"✅ Bot completato: {num_nuove} nuove su {len(tutti_i_link)}")
        
    except Exception as e:
        log_semplice(f"💀 ERRORE GRAVE: {str(e)[:100]}")
        print(f"💀 ERRORE FATALE nel run(): {e}")
        emergency_cleanup(browser, context)
        
        try:
            send_telegram(
                f"💀 ERRORE FATALE BOT\n\n"
                f"Errore: {str(e)[:200]}\n"
                f"Time: {datetime.now().strftime('%H:%M:%S')}"
            )
        except:
            pass
        
        raise
        
        # ANALISI PERFORMANCE
        total_time = time.time() - start_total
        print(f"\n⏱️ ANALISI PERFORMANCE:")
        print(f"  Totale: {total_time:.1f}s")
        
        for phase, t in phase_timers.items():
            if t > 0:
                percent = (t / total_time) * 100
                print(f"  {phase}: {t:.1f}s ({percent:.1f}%)")
        
        if total_time > 60:
            slowest_phase = max(phase_timers, key=phase_timers.get)
            print(f"⚠️ AVVISO: Bot lento ({total_time:.1f}s)")
            print(f"   Fase più lenta: {slowest_phase} ({phase_timers[slowest_phase]:.1f}s)")
            
            if total_time > 90:
                send_telegram(
                    f"⚠️ Bot estremamente lento: {total_time:.1f}s\n"
                    f"Fase critica: {slowest_phase}\n"
                    f"Storie: {len(tutti_i_link)} trovate, {num_nuove} nuove"
                )
        
        print(f"\n✅ BOT COMPLETATO")
        log_semplice(f"✅ Bot completato: {num_nuove} nuove su {len(tutti_i_link)}")
        
    except Exception as e:
        log_semplice(f"💀 ERRORE GRAVE: {str(e)[:100]}")
        print(f"💀 ERRORE FATALE nel run(): {e}")
        emergency_cleanup(browser, context)
        
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
