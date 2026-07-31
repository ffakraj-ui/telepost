"""
TW Mods Telegram Poster
------------------------
Ye script GitHub ke private repos ke releases se APK utha kar,
uska icon/naam/version nikaal kar, ek banner poster bana kar,
Groq AI se caption likhwa kar Telegram channel pe post kar deti hai.

Ye headless (non-interactive) hai -- GitHub Actions me chalne ke liye banayi gayi hai.
Koi input() prompt nahi hai; sab kuch environment variables / CLI args se aata hai.
"""

import os
import sys
import io
import json
import html
import argparse
import time
import zipfile
import tempfile
import requests
from pyrogram import Client, enums
from pyaxmlparser import APK
from PIL import Image, ImageDraw, ImageFont
import pillow_avif  # noqa: F401  -- registers AVIF decoder with Pillow

# ================= CONFIG LOAD (CONFIG_JSON secret se) =================

CONFIG_RAW = os.environ.get("CONFIG_JSON")
if not CONFIG_RAW:
    print("[ERROR] CONFIG_JSON environment variable missing!")
    sys.exit(1)

CFG = json.loads(CONFIG_RAW)

GITHUB_CONFIGS = CFG["github_configs"]
GROQ_API_KEY = CFG["groq_api_key"]
TELEGRAM_CFG = CFG["telegram"]
TELEGRAM_LINK = CFG.get("telegram_link", "https://t.me/twmodstore")
CHANNEL_USERNAME = "@" + TELEGRAM_LINK.rstrip("/").split("/")[-1]

GROQ_MODEL = "llama-3.3-70b-versatile"

TW_LOGO_URL = "https://twmods.in/favicon.avif"
TW_LOGO_FALLBACK = "https://cdn-icons-png.flaticon.com/512/732/732221.png"

POSTED_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "posted.json")


# ================= POSTED-TRACKER (duplicate posts na ho) =================

def load_posted():
    if os.path.exists(POSTED_DB_PATH):
        try:
            with open(POSTED_DB_PATH) as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_posted(posted_set):
    with open(POSTED_DB_PATH, "w") as f:
        json.dump(sorted(posted_set), f, indent=2)


# ================= GITHUB: RELEASE + ASSET FETCH =================

def get_release(repo, token, release_id=None, release_tag=None):
    headers = {"Authorization": f"token {token}"}
    if release_id:
        url = f"https://api.github.com/repos/{repo}/releases/{release_id}"
    elif release_tag:
        url = f"https://api.github.com/repos/{repo}/releases/tags/{release_tag}"
    else:
        url = f"https://api.github.com/repos/{repo}/releases/latest"

    res = requests.get(url, headers=headers, timeout=30)
    res.raise_for_status()
    return res.json()


def download_asset(repo, token, asset_id, dest_path):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/octet-stream",
    }
    url = f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}"
    with requests.get(url, headers=headers, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    return dest_path


def pick_apk_assets(release_json, asset_name=None):
    assets = release_json.get("assets", [])
    apk_assets = [a for a in assets if a["name"].lower().endswith(".apk")]
    if asset_name:
        apk_assets = [a for a in apk_assets if a["name"] == asset_name]
    return apk_assets


# ================= AI CAPTION (Groq) =================

def groq_generate(app_name, package, version):
    prompt = f"""You are writing a promotional post for a modded Android APK for a store called TW MODS.

App name: {app_name}
Package: {package}
Version: {version}

Return ONLY valid JSON, no markdown, no extra text, in this exact shape:
{{
  "type": "app" or "game",
  "description": "a 2 to 3 line catchy description of what this app/game does, naturally mentioning 'TW MODS' once",
  "mod_info": "short mod info line e.g. Premium Features Unlocked",
  "features": ["feature 1", "feature 2", "feature 3", "feature 4", "feature 5", "feature 6","feature 7","feature 8"]
}}

List at least 6 to 8 realistic mod/premium features, one short phrase each."""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ================= APK INFO + ICON EXTRACT =================

def find_icon_in_zip(apk_path):
    density_order = [
        "mipmap-xxxhdpi", "mipmap-xxhdpi", "mipmap-xhdpi", "mipmap-hdpi", "mipmap-mdpi",
        "drawable-xxxhdpi", "drawable-xxhdpi", "drawable-xhdpi", "drawable-hdpi", "drawable-mdpi",
        "drawable",
    ]

    def density_rank(path):
        for i, d in enumerate(density_order):
            if f"/{d}/" in path or path.startswith(d + "/"):
                return i
        return len(density_order)

    def penalty(path):
        base = os.path.basename(path).lower()
        p = 0
        if "round" in base:
            p += 2
        if "foreground" in base or "background" in base:
            p += 1
        return p

    try:
        with zipfile.ZipFile(apk_path) as z:
            candidates = [
                n for n in z.namelist()
                if "ic_launcher" in n.lower() and n.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda p: (penalty(p), density_rank(p)))
            return z.read(candidates[0])
    except Exception:
        return None


def is_valid_image(data):
    if not data:
        return False
    try:
        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:
        return False


def extract_apk_info(apk_path):
    apk = APK(apk_path)
    app_name = apk.get_app_name() or os.path.splitext(os.path.basename(apk_path))[0]
    version = apk.version_name or "1.0"
    package = apk.package or "unknown"

    icon_bytes = None
    icon_ref = apk.get_app_icon()
    if icon_ref:
        try:
            with zipfile.ZipFile(apk_path) as z:
                if icon_ref in z.namelist():
                    icon_bytes = z.read(icon_ref)
        except Exception:
            icon_bytes = None

    if not is_valid_image(icon_bytes):
        icon_bytes = find_icon_in_zip(apk_path)

    return app_name, version, package, icon_bytes


# ================= BANNER CREATOR =================

_tw_logo_cache = None


def get_tw_logo():
    global _tw_logo_cache
    if _tw_logo_cache is not None:
        return _tw_logo_cache
    for url in (TW_LOGO_URL, TW_LOGO_FALLBACK):
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            _tw_logo_cache = Image.open(io.BytesIO(r.content)).convert("RGBA")
            return _tw_logo_cache
        except Exception:
            continue
    _tw_logo_cache = Image.new("RGBA", (24, 24), (255, 255, 255, 255))
    return _tw_logo_cache


def rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def cover_resize(img, size):
    tw, th = size
    ratio_img = img.width / img.height
    ratio_target = tw / th
    if ratio_img > ratio_target:
        nh = th
        nw = int(nh * ratio_img)
    else:
        nw = tw
        nh = int(nw / ratio_img)
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def load_font(size, bold=True):
    candidates = [
        "/usr/share/fonts/truetype/roboto/Roboto-Bold.ttf" if bold else "/usr/share/fonts/truetype/roboto/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/roboto/hinted/Roboto-Bold.ttf" if bold else "/usr/share/fonts/truetype/roboto/hinted/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def create_banner(icon_bytes, output_path):
    W, H = 800, 450
    canvas = Image.new("RGBA", (W, H), (0, 71, 171, 255))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.polygon([(0, 0), (int(W * 0.35), 0), (int(W * 0.15), H), (0, H)], fill=(0, 50, 150, 150))
    od.polygon([(W, 0), (int(W * 0.65), 0), (int(W * 0.85), H), (W, H)], fill=(0, 80, 200, 120))
    canvas = Image.alpha_composite(canvas, overlay)

    draw = ImageDraw.Draw(canvas)

    box_size = 150
    box_x = (W - box_size) // 2
    box_y = (H - box_size) // 2
    draw.rounded_rectangle(
        [box_x, box_y, box_x + box_size, box_y + box_size], radius=20, fill=(255, 255, 255, 255)
    )

    icon_img = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
    inner = box_size
    icon_img = cover_resize(icon_img, (inner, inner))
    icon_img.putalpha(rounded_mask((inner, inner), 20))
    canvas.paste(icon_img, (box_x, box_y), icon_img)

    badge_font = load_font(14)
    badge_text = "MOD"
    bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    tw_, th_ = bbox[2] - bbox[0], bbox[3] - bbox[1]
    badge_w, badge_h = tw_ + 16, th_ + 8
    badge_x = box_x + box_size - badge_w + 10
    badge_y = box_y - 10
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], radius=6, fill=(220, 20, 20, 255)
    )
    draw.text((badge_x + 8, badge_y + 4), badge_text, font=badge_font, fill=(255, 255, 255, 255))

    logo = get_tw_logo().resize((24, 24), Image.LANCZOS)
    logo.putalpha(rounded_mask((24, 24), 4))

    footer_font_small = load_font(11, bold=False)
    footer_font_big = load_font(15, bold=True)

    footer_x, footer_y = 20, H - 64
    footer_w, footer_h = 150, 44
    fdraw_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fdraw_layer)
    fd.rounded_rectangle(
        [footer_x, footer_y, footer_x + footer_w, footer_y + footer_h], radius=8, fill=(0, 0, 0, 180)
    )
    canvas = Image.alpha_composite(canvas, fdraw_layer)
    canvas.paste(logo, (footer_x + 10, footer_y + 10), logo)

    draw = ImageDraw.Draw(canvas)
    draw.text((footer_x + 44, footer_y + 6), "Download on", font=footer_font_small, fill=(255, 255, 255, 255))
    draw.text((footer_x + 44, footer_y + 20), "TWMODS", font=footer_font_big, fill=(255, 255, 255, 255))

    canvas.convert("RGB").save(output_path, "PNG")
    return output_path


# ================= PROCESS ONE APK =================

def process_asset(app_client, repo, token, asset, posted, dry_run=False):
    asset_name = asset["name"]
    if asset_name in posted:
        print(f"[SKIP] Already posted: {asset_name}")
        return False

    print(f"\n{'='*50}\n📦 Processing: {asset_name} ({repo})\n{'='*50}")

    with tempfile.TemporaryDirectory() as tmp:
        apk_path = os.path.join(tmp, asset_name)
        print("⬇️  Downloading APK...")
        download_asset(repo, token, asset["id"], apk_path)

        try:
            app_name, version, package, icon_bytes = extract_apk_info(apk_path)
        except Exception as e:
            print(f"[ERROR] APK parse failed: {e}")
            return False

        print(f"App: {app_name} | Version: {version} | Package: {package}")

        try:
            ai_data = groq_generate(app_name, package, version)
        except Exception as e:
            print(f"[ERROR] AI generation failed: {e}")
            ai_data = {"type": "app", "description": f"{app_name} — modded by TW MODS.",
                       "mod_info": "Premium Features Unlocked", "features": []}

        emoji = "🎮" if ai_data.get("type") == "game" else "📱"
        description = ai_data.get("description", "")
        mod_info = ai_data.get("mod_info", "Premium Features Unlocked")
        features = ai_data.get("features", [])

        safe_app_name = html.escape(app_name)
        safe_description = html.escape(description)
        safe_mod_info = html.escape(mod_info)
        feature_text = "\n".join(f"• {html.escape(f)}" for f in features).strip()

        caption = f"""<b>{emoji} {safe_app_name} {version}</b>

{safe_description}

🤖 <b>Mod Info</b>: <i>{safe_mod_info}</i>

<blockquote>
{feature_text}
</blockquote>
📢 <a href="{TELEGRAM_LINK}">Telegram</a> 💬 <a href="https://t.me/twmodschat">Discuss</a> 👑 <a href="https://www.instagram.com/tw_mods/">Instagram</a>"""

        image_path = None
        if icon_bytes:
            try:
                banner_path = os.path.join(tmp, f"{package}_banner.png")
                create_banner(icon_bytes, banner_path)
                image_path = banner_path
                print(f"🖼️  Banner generated")
            except Exception as e:
                print(f"[WARN] Banner creation failed: {e}")

        if dry_run:
            print("\n--- DRY RUN (nothing sent) ---")
            print(caption)
            return True

        try:
            if image_path:
                app_client.send_photo(
                    chat_id=CHANNEL_USERNAME,
                    photo=image_path,
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                app_client.send_message(
                    chat_id=CHANNEL_USERNAME,
                    text=caption,
                    parse_mode=enums.ParseMode.HTML,
                )

            print("📤 Uploading APK to Telegram...")
            app_client.send_document(
                chat_id=CHANNEL_USERNAME,
                document=apk_path,
                file_name=asset_name,
                caption=f"<b>{safe_app_name} {version}</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            print("✅ Posted successfully")
            posted.add(asset_name)
            save_posted(posted)
            return True
        except Exception as e:
            print(f"[ERROR] Telegram post failed: {e}")
            return False


# ================= MAIN =================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="e.g. ffakraj-ui/apps (manual mode only)")
    parser.add_argument("--release-id", help="Specific release ID (manual mode)")
    parser.add_argument("--release-tag", help="Specific release tag, e.g. v1.0 (manual mode)")
    parser.add_argument("--asset-name", help="Specific asset filename inside the release (optional)")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually post, just preview")
    parser.add_argument("--limit", type=int, default=10, help="Max APKs to process this run (capped at 50)")
    args = parser.parse_args()

    limit = max(1, min(args.limit, 50))
    processed_count = 0

    posted = load_posted()

    api_id = TELEGRAM_CFG["api_id"]
    api_hash = TELEGRAM_CFG["api_hash"]
    bot_token = TELEGRAM_CFG["bot_token"]

    app_client = Client(
        "twmods_poster_bot",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
    )

    any_posted = False

    with app_client:
        if args.repo:
            # ---- MANUAL MODE: ek specific repo/release process karo ----
            matching_cfg = next(
                (c for c in GITHUB_CONFIGS.values() if c["repo"] == args.repo), None
            )
            token = matching_cfg["token"] if matching_cfg else None
            if not token:
                print(f"[ERROR] '{args.repo}' CONFIG_JSON ke github_configs me nahi mila!")
                sys.exit(1)

            release = get_release(args.repo, token, args.release_id, args.release_tag)
            assets = pick_apk_assets(release, args.asset_name)
            if not assets:
                print("[ERROR] Is release me koi .apk asset nahi mila.")
                sys.exit(1)

            for asset in assets:
                if processed_count >= limit:
                    print(f"\n[LIMIT] {limit} APK ka limit pura ho gaya, ruk raha hoon.")
                    break
                if process_asset(app_client, args.repo, token, asset, posted, args.dry_run):
                    any_posted = True
                    processed_count += 1
                    if not args.dry_run:
                        time.sleep(3)

        else:
            # ---- AUTOMATIC MODE: saare configured repos ki latest release check karo ----
            for key, cfg in GITHUB_CONFIGS.items():
                repo = cfg["repo"]
                token = cfg["token"]
                release_id = cfg.get("release_id")
                try:
                    release = get_release(repo, token, release_id=release_id)
                except Exception as e:
                    print(f"[ERROR] Could not fetch release for {repo}: {e}")
                    continue

                assets = pick_apk_assets(release)
                for asset in assets:
                    if processed_count >= limit:
                        break
                    if process_asset(app_client, repo, token, asset, posted, args.dry_run):
                        any_posted = True
                        processed_count += 1
                        if not args.dry_run:
                            time.sleep(3)
                if processed_count >= limit:
                    print(f"\n[LIMIT] {limit} APK ka limit pura ho gaya, ruk raha hoon.")
                    break

    print("\n🎉 Done!" if any_posted else "\nℹ️  Koi naya APK nahi mila post karne ke liye.")


if __name__ == "__main__":
    main()
