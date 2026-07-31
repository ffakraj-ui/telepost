# telepost

TW Mods ke private GitHub release repos se already-mod APK utha kar,
uska poster banake, AI se caption likhwa kar Telegram channel pe post
karne wala automation.

## Setup (ek baar)

1. GitHub par ye repo private rakho.
2. **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `CONFIG_JSON`
   - Value: apna poora config.json content (github_configs, groq_api_key, telegram block sab) paste kar do.

## Manual run

Actions tab → "Post APK to Telegram" → **Run workflow**:
- `repo`: e.g. `ffakraj-ui/apps`
- `release_id` ya `release_tag` (dono optional; khaali = latest release)
- `asset_name` (optional; khaali = release ka pehla `.apk` asset)
- `dry_run`: `true` rakhoge to sirf preview hoga, kuch post nahi hoga

`repo` khaali chhod doge to yeh **automatic mode** me chalega — `CONFIG_JSON` ke
andar `github_configs` me diye saare repos ke latest release check karega.

## Automatic run

Har 30 minute me apne aap chalega (cron schedule) — automatic mode wahi logic
use karta hai jo upar bataya. Naya APK milne pe post karega, `posted.json` me
naam save kar dega taaki dobara post na ho.

## Files

| File | Kaam |
|---|---|
| `.github/workflows/post.yml` | Trigger: manual + har 30 min automatic |
| `scripts/post_apk.py` | Download → info extract → banner → AI caption → Telegram post |
| `requirements.txt` | Python dependencies |
| `posted.json` | Already-posted APK names (auto-updated, commit ho jata hai) |
