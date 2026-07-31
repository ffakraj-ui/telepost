"""
TW Mods Publish Pipeline (GitHub Actions version) - Modified for sitemap file support
"""

import os
import re
import sys
import argparse
import subprocess
import zipfile
import shutil
import tempfile
import requests
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape

# ══════════════════════════════════════════════
#  CONFIG (CONFIG_JSON secret se, jaisa telepost me hai)
# ══════════════════════════════════════════════

CONFIG_RAW = os.environ.get("CONFIG_JSON")
if not CONFIG_RAW:
    print("[ERROR] CONFIG_JSON environment variable missing!")
    sys.exit(1)

CONFIG = json.loads(CONFIG_RAW)

GITHUB_CONFIGS = CONFIG["github_configs"]
DATABASE_URL = CONFIG["database_url"]
TARGET_PATTERNS = CONFIG.get("target_patterns", {})
REPLACEMENTS = CONFIG.get("replacements", {})
VALID_CATEGORIES = CONFIG.get("valid_categories", ["apps", "games", "tools", "social", "entertainment", "education"])
GROQ_API_KEY = CONFIG["groq_api_key"]
TELEGRAM_LINK = CONFIG.get("telegram_link", "https://t.me/twmodstore")
PREFETCH_WORKERS = CONFIG.get("prefetch_workers", 8)
PREFETCH_BATCH = CONFIG.get("prefetch_batch", 25)

# Runner-local working directories
WORKDIR = os.path.abspath("pipeline_work")
INPUT_FOLDER = os.path.join(WORKDIR, "getmodfiles")
OUTPUT_FOLDER = os.path.join(WORKDIR, "published")
os.makedirs(INPUT_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Service account + keystore
SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", os.path.join(WORKDIR, "firebase-service-account.json"))
KEYSTORE_PATH = os.environ.get("TWMODS_KEYSTORE_PATH", os.path.join(WORKDIR, "twmodskey.jks"))
KEYSTORE_ALIAS = os.environ.get("TWMODS_KEY_ALIAS", "twmods")
KEYSTORE_PASSWORD = os.environ.get("TWMODS_KEYSTORE_PASSWORD")
KEY_PASSWORD = os.environ.get("TWMODS_KEY_PASSWORD", KEYSTORE_PASSWORD)

DB_CACHE_FILE = os.path.join(OUTPUT_FOLDER, ".firebase_db_cache.json")
DOWNLOAD_HISTORY_FILE = os.path.join(INPUT_FOLDER, "download_history.json")
PROCESSED_DB = os.path.join(OUTPUT_FOLDER, ".processed.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

GREEN, RED, YELLOW, BLUE, CYAN, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[96m", "\033[1m", "\033[0m"
)


def print_success(m): print(f"{GREEN}[SUCCESS] {m}{RESET}")
def print_error(m):   print(f"{RED}[ERROR] {m}{RESET}")
def print_warning(m): print(f"{YELLOW}[WARNING] {m}{RESET}")
def print_info(m):    print(f"{BLUE}[INFO] {m}{RESET}")
def print_step(tag, m): print(f"\n{BOLD}{CYAN}[{tag}]{RESET} {YELLOW}{m}{RESET}")


def format_size(bytes_size):
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def safe_parse_iso_datetime(s):
    if not s:
        return None
    try:
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_app_name(url):
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    return parts[-1].replace("-", " ").title() if parts else "unknown"


def extract_version_from_filename(filename):
    if not filename:
        return None
    m = re.search(r"v([\d.]+)", filename, re.IGNORECASE)
    return m.group(1) if m else None


# ================= FILE / HISTORY HELPERS =================

def load_download_history():
    if os.path.exists(DOWNLOAD_HISTORY_FILE):
        try:
            with open(DOWNLOAD_HISTORY_FILE) as f:
                data = json.load(f)
            data.setdefault("downloaded", [])
            data.setdefault("failed", [])
            return data
        except Exception:
            pass
    return {"downloaded": [], "failed": []}


def save_download_history(history):
    with open(DOWNLOAD_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def mark_downloaded(filename, history):
    if filename not in history["downloaded"]:
        history["downloaded"].append(filename)
        save_download_history(history)


def mark_download_failed(filename, history):
    if filename not in history["failed"]:
        history["failed"].append(filename)
        save_download_history(history)


def load_processed_db():
    if os.path.exists(PROCESSED_DB):
        try:
            with open(PROCESSED_DB) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_processed_db(processed_set):
    with open(PROCESSED_DB, "w") as f:
        json.dump(list(processed_set), f, indent=2)


def mark_processed(name, processed_set):
    processed_set.add(name)
    save_processed_db(processed_set)


# ================= FIREBASE =================

_token_cache = {"token": None, "expiry": 0}


def get_firebase_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expiry"]:
        return _token_cache["token"]
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleRequest
        SCOPES = [
            "https://www.googleapis.com/auth/firebase.database",
            "https://www.googleapis.com/auth/userinfo.email",
        ]
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT, scopes=SCOPES)
        creds.refresh(GoogleRequest())
        _token_cache["token"] = creds.token
        _token_cache["expiry"] = now + 3000
        return creds.token
    except Exception as e:
        print_error(f"Firebase token error: {e}")
        return None


def firebase_db_push(slug, data):
    token = get_firebase_token()
    if not token:
        return False
    url = f"{DATABASE_URL}/apps/{slug}.json"
    try:
        res = requests.put(url, json=data, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return res.status_code == 200
    except Exception as e:
        print_error(f"Firebase DB exception: {e}")
        return False


def firebase_db_get_all():
    token = get_firebase_token()
    if not token:
        return None
    url = f"{DATABASE_URL}/apps.json"
    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print_error(f"DB fetch exception: {e}")
    return None


def firebase_db_delete(slug):
    token = get_firebase_token()
    if not token:
        return False
    url = f"{DATABASE_URL}/apps/{slug}.json"
    try:
        res = requests.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return res.status_code == 200
    except Exception:
        return False


# ================= SITEMAP PARSE (Modified) =================

def parse_sitemap_from_file(sitemap_file):
    """Parse sitemap URLs from a file"""
    print_info(f"Reading sitemap URLs from file: {sitemap_file}")
    try:
        with open(sitemap_file, 'r') as f:
            urls = [line.strip() for line in f if line.strip()]
        
        items = []
        for url in urls:
            if not url.endswith((".png", ".jpg", ".webp", ".css", ".js", ".xml")):
                items.append((url, None))
        
        # Sort by lastmod if available (but we don't have it from file)
        print_success(f"Loaded {len(items)} URLs from sitemap file")
        return items
    except Exception as e:
        print_error(f"Failed to read sitemap file: {e}")
        return []


def parse_sitemap(sitemap_url):
    """Original parse_sitemap function with better error handling"""
    print_info(f"Fetching sitemap: {sitemap_url}")
    try:
        resp = requests.get(sitemap_url, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        
        # Remove invalid XML characters
        content = resp.content
        content = re.sub(b'[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]', b'', content)
        
        # Try different parsing methods
        root = None
        
        # Method 1: Try standard ET
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            print_warning(f"Standard XML parse failed: {e}")
            
            # Method 2: Try with lxml if available
            try:
                from lxml import etree
                parser = etree.XMLParser(recover=True)
                root = etree.fromstring(content, parser=parser)
                print_info("Parsed with lxml (recover mode)")
            except ImportError:
                print_warning("lxml not installed, using fallback")
                # Method 3: Regex fallback
                text = content.decode('utf-8', errors='ignore')
                urls = re.findall(r'<loc>(https?://[^<]+)</loc>', text)
                items = []
                for url in urls:
                    if not url.endswith((".png", ".jpg", ".webp", ".css", ".js", ".xml")):
                        items.append((url.strip(), None))
                return items
            except Exception as e:
                print_error(f"lxml parse failed: {e}")
                return []
        
        # Parse with namespace
        ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
        items = []
        for url_elem in root.findall(f".//{{{ns}}}url"):
            loc = url_elem.find(f"{{{ns}}}loc")
            lastmod = url_elem.find(f"{{{ns}}}lastmod")
            if loc is not None and loc.text:
                url = loc.text.strip()
                if not url.endswith((".png", ".jpg", ".webp", ".css", ".js", ".xml")):
                    dt = safe_parse_iso_datetime(lastmod.text) if lastmod is not None else None
                    items.append((url, dt))
        
        # Deduplicate
        dedup = {}
        for url, dt in items:
            if url not in dedup or (dt and (dedup[url] is None or dt > dedup[url])):
                dedup[url] = dt
        items = [(u, dedup[u]) for u in dedup]
        items.sort(key=lambda x: (x[1] is not None, x[1] or datetime(1970, 1, 1, tzinfo=timezone.utc)), reverse=True)
        return items
    except Exception as e:
        print_error(f"Sitemap parse error: {e}")
        return []


# ================= DOWNLOAD =================

def extract_apk_links(page_url):
    if not page_url.endswith("/?download=links"):
        page_url = page_url.rstrip("/") + "/?download=links"
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("#list-downloadlinks li a")
        urls = [a.get("href") for a in links if a.get("href")]
        return urls[0] if urls else None
    except Exception as e:
        print_error(f"Error extracting links: {e}")
        return None


def download_file(url, filename, download_dir):
    try:
        resp = requests.get(url, headers=HEADERS, stream=True, timeout=300)
        total = int(resp.headers.get("content-length", 0))
        if total > 600 * 1024 * 1024:
            print_warning(f"Skipped — {format_size(total)} exceeds 600MB")
            resp.close()
            return False
        filepath = os.path.join(download_dir, filename)
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print_error(f"Download failed: {e}")
        return False


def getmodpc_download(app_url, download_dir, history):
    print_step("DOWNLOAD", app_url)
    app_name = get_app_name(app_url)
    apk_url = extract_apk_links(app_url)
    if not apk_url:
        print_error("No download link found!")
        return None, None, None

    filename = os.path.basename(apk_url.split("?")[0])
    if not filename.endswith(".apk"):
        filename = f"{app_name.replace(' ', '_')}.apk"

    if filename in history.get("downloaded", []):
        filepath = os.path.join(download_dir, filename)
        if os.path.exists(filepath):
            return filepath, app_name, extract_version_from_filename(filename)

    print_info(f"Downloading: {filename}")
    if download_file(apk_url, filename, download_dir):
        mark_downloaded(filename, history)
        return os.path.join(download_dir, filename), app_name, extract_version_from_filename(filename)
    mark_download_failed(filename, history)
    return None, None, None


# ================= .SO MODIFIER =================

def get_strings_from_so(so_path):
    result = subprocess.run(["r2", "-q", "-c", "iz", so_path], capture_output=True, text=True)
    strings = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            offset_str = parts[1]
            if not offset_str.startswith("0x"):
                continue
            offset = int(offset_str, 16)
            val = " ".join(parts[8:]) if len(parts) > 8 else parts[-1]
            strings.append({"offset": offset, "string": val.strip('"')})
        except (ValueError, IndexError):
            continue
    return strings


def replace_exact_string(so_path, search_str, replace_str):
    if len(search_str) != len(replace_str):
        return False
    offsets = [s["offset"] for s in get_strings_from_so(so_path) if s["string"] == search_str]
    if not offsets:
        return False
    for offset in offsets:
        cmd = f"oo+; s {offset}; w {replace_str}"
        subprocess.run(["r2", "-w", "-q", "-c", cmd, so_path], capture_output=True)
    return True


def replace_substring(so_path, search_str, replace_str):
    if len(search_str) != len(replace_str):
        return False
    replacements = []
    for s in get_strings_from_so(so_path):
        pos = s["string"].find(search_str)
        if pos != -1:
            replacements.append(s["offset"] + pos)
    if not replacements:
        return False
    for offset in replacements:
        cmd = f"oo+; s {offset}; w {replace_str}"
        subprocess.run(["r2", "-w", "-q", "-c", cmd, so_path], capture_output=True)
    return True


def modify_so_file(so_path):
    fname = os.path.basename(so_path)
    search_terms = None
    for pattern, terms in TARGET_PATTERNS.items():
        if re.match(pattern, fname, re.IGNORECASE):
            search_terms = terms
            break
    if not search_terms:
        return False
    success = False
    for term in search_terms:
        if term not in REPLACEMENTS:
            continue
        replace_str = REPLACEMENTS[term]
        fn = replace_exact_string if term == "show" else replace_substring
        if fn(so_path, term, replace_str):
            success = True
    return success


# ================= BUILD / SIGN =================

def rebuild_apk(original_apk, modified_dir, output_apk):
    replace_map = {}
    for root, _, files in os.walk(modified_dir):
        for f in files:
            for pattern in TARGET_PATTERNS:
                if re.match(pattern, f, re.IGNORECASE):
                    full = os.path.join(root, f)
                    arc = os.path.relpath(full, modified_dir).replace(os.sep, "/")
                    replace_map[arc] = full
                    break
    with zipfile.ZipFile(original_apk, "r") as orig:
        with zipfile.ZipFile(output_apk, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as new:
            for item in orig.infolist():
                if item.filename in replace_map:
                    new.write(replace_map[item.filename], item.filename)
                else:
                    new.writestr(item, orig.read(item.filename))
    return True


def sign_apk(apk_path, output_path):
    aligned = apk_path + "_aligned.apk"
    subprocess.run(f'zipalign -f -v -p 4 "{apk_path}" "{aligned}"', shell=True, capture_output=True)
    r = subprocess.run(
        f'apksigner sign --ks "{KEYSTORE_PATH}" --ks-key-alias {KEYSTORE_ALIAS} '
        f'--ks-pass pass:{KEYSTORE_PASSWORD} --key-pass pass:{KEY_PASSWORD} '
        f"--v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true "
        f'--out "{output_path}" "{aligned}"',
        shell=True, capture_output=True,
    )
    if os.path.exists(aligned):
        os.remove(aligned)
    if r.returncode == 0:
        return True
    print_error(f"Signing failed:\n{r.stderr.decode(errors='ignore')}")
    return False


def get_output_filename(original_path):
    base = os.path.splitext(os.path.basename(original_path))[0]
    base = re.sub(r"_twmods$", "", base)
    base = re.sub(r"(?i)getmodpc", "twmods", base)
    return os.path.join(OUTPUT_FOLDER, f"{base}_twmods.apk")


def modify_apk(apk_path):
    label = os.path.basename(apk_path)
    print_step("MODIFY", label)
    output_apk = get_output_filename(apk_path)
    work_dir = tempfile.mkdtemp()
    try:
        found = []
        with zipfile.ZipFile(apk_path, "r") as zf:
            for name in zf.namelist():
                base = os.path.basename(name)
                for pattern in TARGET_PATTERNS:
                    if re.match(pattern, base, re.IGNORECASE):
                        zf.extract(name, work_dir)
                        found.append(name)
                        break

        if not found:
            print_warning("No target .so files — signing as-is")
            return output_apk if sign_apk(apk_path, output_apk) else None

        modified = 0
        for root, _, files in os.walk(work_dir):
            for f in files:
                for pattern in TARGET_PATTERNS:
                    if re.match(pattern, f, re.IGNORECASE):
                        if modify_so_file(os.path.join(root, f)):
                            modified += 1
                        break

        if modified == 0:
            print_warning("No strings replaced — signing as-is")
            return output_apk if sign_apk(apk_path, output_apk) else None

        rebuilt = os.path.join(work_dir, "rebuilt.apk")
        rebuild_apk(apk_path, work_dir, rebuilt)
        if not sign_apk(rebuilt, output_apk):
            return None
        print_success(f"Modified + Signed: {output_apk}")
        return output_apk
    except Exception as e:
        print_error(f"Error modifying: {e}")
        return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ================= PLAY STORE =================

def extract_playstore_from_html(html_text):
    if not html_text:
        return None
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if "play.google.com/store/apps/details" in href or href.startswith("market://details"):
                qs = parse_qs(urlparse(href).query)
                pkg = (qs.get("id") or [None])[0]
                if pkg:
                    return pkg
    except Exception:
        pass
    m = re.search(r"play\.google\.com/store/apps/details\?id=([a-zA-Z0-9._]+)", html_text)
    return m.group(1) if m else None


def fetch_playstore_info(page_url):
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=30)
        html_text = unescape(resp.text)
        pkg = extract_playstore_from_html(html_text)
        if pkg:
            return {"play_url": f"https://play.google.com/store/apps/details?id={pkg}", "package_id": pkg}
    except Exception:
        pass
    return {"play_url": None, "package_id": None}


def scrape_playstore(playstore_id):
    if not playstore_id:
        return None
    headers = {"User-Agent": HEADERS["User-Agent"], "Referer": "https://twmods.in/", "Origin": "https://twmods.in"}
    apis = [f"https://twmods.in/api/playstore?id={playstore_id}", f"https://twmods.in/api/playstore1?id={playstore_id}"]
    combined = {}
    for api_url in apis:
        try:
            res = requests.get(api_url, headers=headers, timeout=15)
            if res.status_code != 200:
                continue
            data = res.json()
            for k, v in data.items():
                if v and (k not in combined or not combined[k]):
                    combined[k] = v
        except Exception:
            continue
    if not combined:
        return None
    screenshots = combined.get("screenshots", [])
    if isinstance(screenshots, str):
        screenshots = [s.strip() for s in screenshots.split(",") if s.strip()]
    elif not isinstance(screenshots, list):
        screenshots = []
    return {
        "name": (combined.get("name") or combined.get("title") or playstore_id.split(".")[-1].title()).strip(),
        "description": combined.get("description") or combined.get("full_description") or "",
        "icon": combined.get("icon") or combined.get("image") or "",
        "screenshots": screenshots[:6],
        "rating": float(combined.get("rating") or combined.get("score") or 0 or 0),
        "developer": combined.get("developer") or combined.get("author") or "",
        "version": combined.get("version") or "",
    }


# ================= GITHUB UPLOAD =================

def upload_to_github(apk_path, repo, token, release_id):
    file_name = os.path.basename(apk_path)
    file_size_mb = round(os.path.getsize(apk_path) / (1024 * 1024), 1)
    print_info(f"Uploading: {file_name} ({file_size_mb} MB) -> {repo}")
    url = f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={file_name}"
    headers = {"Authorization": f"token {token}", "Content-Type": "application/vnd.android.package-archive"}
    try:
        with open(apk_path, "rb") as f:
            res = requests.post(url, headers=headers, data=f.read(), timeout=300)
        if res.status_code == 201:
            return res.json()["browser_download_url"], file_size_mb
    except Exception as e:
        print_error(f"Upload error: {e}")
    return None, file_size_mb


def delete_from_github(file_name, repo, token, release_id):
    url = f"https://api.github.com/repos/{repo}/releases/{release_id}/assets"
    headers = {"Authorization": f"token {token}"}
    try:
        res = requests.get(url, headers=headers, timeout=30)
        if res.status_code != 200:
            return False
        for asset in res.json():
            if asset["name"] == file_name:
                requests.delete(f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", headers=headers)
                return True
    except Exception:
        pass
    return False


# ================= AI DESCRIPTION =================

def generate_ai_description(app_name, description, category_list):
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        categories_str = ", ".join(category_list)
        prompt = f"""App: {app_name}
Available Categories: {categories_str}
Original Description: {description[:3000]}

Generate THREE things:
1. CATEGORY: Choose the BEST category from the available list above. Return ONLY the category name.
2. FULL_DESC: Write a detailed app description of 50-60 lines covering purpose, features, mod highlights, who it's for.
3. MOD_DESC: Write 25-35 lines listing everything unlocked/modified, each starting with "✓ ".

Format EXACTLY:
CATEGORY: [category]
FULL_DESC: [description]
MOD_DESC: [features]"""
        data = {"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}], "max_tokens": 3500}
        response = requests.post(url, json=data, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        return f"AI error: {response.status_code}"
    except Exception as e:
        return f"AI error: {e}"


def parse_ai_response(ai_output):
    category, full_desc, mod_desc, current = "apps", "", "", None
    for line in ai_output.split("\n"):
        if line.startswith("CATEGORY:"):
            category = line.replace("CATEGORY:", "").strip().lower()
            current = None
        elif line.startswith("FULL_DESC:"):
            current = "full"
            full_desc = line.replace("FULL_DESC:", "").strip()
        elif line.startswith("MOD_DESC:"):
            current = "mod"
            mod_desc = line.replace("MOD_DESC:", "").strip()
        elif current == "full" and line.strip():
            full_desc += " " + line.strip()
        elif current == "mod" and line.strip():
            mod_desc += "\n" + line.strip()
    if category not in VALID_CATEGORIES:
        category = "apps"
    return category, full_desc, mod_desc


# ================= PUBLISH TO FIREBASE =================

def publish_to_firebase(download_url, file_size_mb, app_name, version, playstore_id,
                         full_desc, mod_desc, category, image_url, db_data, ps_data, repo, token, release_id):
    slug = re.sub(r"[^a-z0-9]+", "-", app_name.lower()).strip("-")

    existing_slug, existing_data = None, None
    if db_data:
        for s, data in db_data.items():
            if s == slug or data.get("name", "").lower() == app_name.lower():
                existing_slug, existing_data = s, data
                break

    if existing_slug and existing_data:
        old_version = existing_data.get("version", "")
        if version and old_version and version == old_version:
            print_warning(f"Same version ({version}) — skipping")
            return False
        old_filename = existing_data.get("download", "").split("/")[-1]
        if old_filename:
            delete_from_github(old_filename, repo, token, release_id)
        firebase_db_delete(existing_slug)

    data = {
        "name": app_name,
        "slug": slug,
        "version": version or "1.0",
        "size": f"{file_size_mb} MB",
        "category": category or "apps",
        "image": image_url or "",
        "download": download_url,
        "description": full_desc or "",
        "mod_desc": mod_desc or "",
        "telegram": TELEGRAM_LINK,
        "upload_date": datetime.now().strftime("%Y-%m-%d"),
    }

    if playstore_id:
        data["playstore_id"] = playstore_id
        if ps_data:
            if ps_data.get("screenshots"):
                data["screenshots"] = ps_data["screenshots"][:6]
            if ps_data.get("rating"):
                data["rating"] = round(float(ps_data["rating"]), 1)
            if ps_data.get("developer"):
                data["developer"] = ps_data["developer"]
            if ps_data.get("icon") and not data["image"]:
                data["image"] = ps_data["icon"]

    if firebase_db_push(slug, data):
        print_success(f"Published: https://twmods.in/{slug}")
        return True
    return False


# ================= PROCESS ONE APP =================

def process_single_app(app_url, db_data, history, processed_set, repo, token, release_id, known_package_id=None):
    result = {"url": app_url, "status": "pending"}
    apk_path, app_name, version = getmodpc_download(app_url, INPUT_FOLDER, history)
    if not apk_path:
        result["status"] = "download_failed"
        return result

    playstore_id = known_package_id
    if not playstore_id:
        try:
            page_url = app_url.rstrip("/") + "/?download=links"
            resp = requests.get(page_url, headers=HEADERS, timeout=30)
            playstore_id = extract_playstore_from_html(resp.text)
        except Exception:
            pass

    ps_data = scrape_playstore(playstore_id) if playstore_id else None
    if ps_data:
        app_name = app_name or ps_data.get("name", app_name)
        version = version or ps_data.get("version", version)

    full_desc = (ps_data.get("description") if ps_data else "") or f"{app_name} — modded by TW MODS."

    print_step("AI", f"Generating descriptions for: {app_name}")
    ai_output = generate_ai_description(app_name, full_desc, VALID_CATEGORIES)
    if "AI error" in ai_output:
        category, mod_desc = "apps", "✓ Premium Unlocked\n✓ Ads Removed\n✓ All Features Enabled"
    else:
        category, full_desc, mod_desc = parse_ai_response(ai_output)

    print_step("MODIFY", os.path.basename(apk_path))
    modified_apk = modify_apk(apk_path)
    if not modified_apk:
        result["status"] = "modify_failed"
        return result

    try:
        os.remove(apk_path)
    except Exception:
        pass

    print_step("UPLOAD", repo)
    download_url, file_size_mb = upload_to_github(modified_apk, repo, token, release_id)
    if not download_url:
        result["status"] = "upload_failed"
        return result

    print_step("PUBLISH", app_name)
    image_url = ps_data.get("icon", "") if ps_data else ""
    success = publish_to_firebase(
        download_url, file_size_mb, app_name, version, playstore_id,
        full_desc, mod_desc, category, image_url, db_data, ps_data, repo, token, release_id
    )

    if success:
        mark_processed(os.path.basename(modified_apk), processed_set)
        result["status"] = "success"
    else:
        result["status"] = "publish_failed"
    return result


# ================= MAIN PIPELINE (Modified for sitemap file) =================

def run_pipeline_from_file(sitemap_file, limit, account_key):
    """Run pipeline using URLs from a file"""
    cfg = GITHUB_CONFIGS.get(account_key)
    if not cfg:
        print_error(f"'{account_key}' github_configs me nahi mila!")
        sys.exit(1)
    repo, token, release_id = cfg["repo"], cfg["token"], cfg["release_id"]
    print_info(f"Target: {cfg['label']}")

    db_data = firebase_db_get_all() or {}
    
    # Read URLs from file
    items = parse_sitemap_from_file(sitemap_file)
    if not items:
        print_error("Sitemap file me koi URL nahi mila!")
        sys.exit(1)

    total_available = len(items)
    limit = min(limit, total_available)
    print_success(f"Found {total_available} apps, processing up to {limit}")

    history = load_download_history()
    downloaded_set = set(history.get("downloaded", []))
    processed_set = load_processed_db()

    download_jobs = []
    idx = 0
    while len(download_jobs) < limit and idx < len(items):
        batch = items[idx: idx + PREFETCH_BATCH]
        idx += PREFETCH_BATCH
        with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as ex:
            futures = {ex.submit(extract_apk_links, url): (url, dt) for url, dt in batch}
            for fut in as_completed(futures):
                url, dt = futures[fut]
                try:
                    apk_url = fut.result()
                except Exception:
                    apk_url = None
                if not apk_url:
                    continue
                filename = os.path.basename(apk_url.split("?")[0])
                if filename in downloaded_set or filename in processed_set:
                    continue
                download_jobs.append({"url": url, "apk_url": apk_url, "filename": filename})
                if len(download_jobs) >= limit:
                    break

    if not download_jobs:
        print_warning("Koi naya app nahi mila process karne ke liye!")
        return

    print_success(f"Ready: {len(download_jobs)} app(s)")

    success_count, fail_count = 0, 0
    for i, job in enumerate(download_jobs, start=1):
        print(f"\n{'#'*60}\n[{i}/{len(download_jobs)}] {job['url']}\n{'#'*60}")
        result = process_single_app(job["url"], db_data, history, processed_set, repo, token, release_id)
        if result["status"] == "success":
            success_count += 1
            print_success(f"DONE: {job['filename']}")
        else:
            fail_count += 1
            print_error(f"FAILED: {job['filename']} — {result['status']}")
        if i < len(download_jobs):
            time.sleep(2)

    print(f"\n{BOLD}{GREEN}{'='*60}\nCOMPLETE — Success: {success_count} | Failed: {fail_count}\n{'='*60}{RESET}")


def run_pipeline(sitemap_url, limit, account_key):
    """Original run_pipeline function"""
    cfg = GITHUB_CONFIGS.get(account_key)
    if not cfg:
        print_error(f"'{account_key}' github_configs me nahi mila!")
        sys.exit(1)
    repo, token, release_id = cfg["repo"], cfg["token"], cfg["release_id"]
    print_info(f"Target: {cfg['label']}")

    db_data = firebase_db_get_all() or {}
    items = parse_sitemap(sitemap_url)
    if not items:
        print_error("Sitemap me koi app nahi mila!")
        sys.exit(1)

    total_available = len(items)
    limit = min(limit, total_available)
    print_success(f"Found {total_available} apps, processing up to {limit}")

    history = load_download_history()
    downloaded_set = set(history.get("downloaded", []))
    processed_set = load_processed_db()

    download_jobs = []
    idx = 0
    while len(download_jobs) < limit and idx < len(items):
        batch = items[idx: idx + PREFETCH_BATCH]
        idx += PREFETCH_BATCH
        with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as ex:
            futures = {ex.submit(extract_apk_links, url): (url, dt) for url, dt in batch}
            for fut in as_completed(futures):
                url, dt = futures[fut]
                try:
                    apk_url = fut.result()
                except Exception:
                    apk_url = None
                if not apk_url:
                    continue
                filename = os.path.basename(apk_url.split("?")[0])
                if filename in downloaded_set or filename in processed_set:
                    continue
                download_jobs.append({"url": url, "apk_url": apk_url, "filename": filename})
                if len(download_jobs) >= limit:
                    break

    if not download_jobs:
        print_warning("Koi naya app nahi mila process karne ke liye!")
        return

    print_success(f"Ready: {len(download_jobs)} app(s)")

    success_count, fail_count = 0, 0
    for i, job in enumerate(download_jobs, start=1):
        print(f"\n{'#'*60}\n[{i}/{len(download_jobs)}] {job['url']}\n{'#'*60}")
        result = process_single_app(job["url"], db_data, history, processed_set, repo, token, release_id)
        if result["status"] == "success":
            success_count += 1
            print_success(f"DONE: {job['filename']}")
        else:
            fail_count += 1
            print_error(f"FAILED: {job['filename']} — {result['status']}")
        if i < len(download_jobs):
            time.sleep(2)

    print(f"\n{BOLD}{GREEN}{'='*60}\nCOMPLETE — Success: {success_count} | Failed: {fail_count}\n{'='*60}{RESET}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sitemap", help="Sitemap URL, e.g. https://getmodpc.net/post-sitemap.xml")
    parser.add_argument("--sitemap-file", help="File containing sitemap URLs (one per line)")
    parser.add_argument("--limit", type=int, default=5, help="Kitne apps process karne hain")
    parser.add_argument("--account", required=True, help="github_configs key, e.g. ffakraj ya mlkraj")
    args = parser.parse_args()

    missing = [cmd for cmd in ["r2", "zipalign", "apksigner"] if not shutil.which(cmd)]
    if missing:
        print_error(f"Missing tools: {', '.join(missing)}")
        sys.exit(1)

    if args.sitemap_file:
        run_pipeline_from_file(args.sitemap_file, args.limit, args.account)
    elif args.sitemap:
        run_pipeline(args.sitemap, args.limit, args.account)
    else:
        print_error("Either --sitemap or --sitemap-file is required!")
        sys.exit(1)


if __name__ == "__main__":
    main()
