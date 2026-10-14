"""
motorsportsclicks — Instagram -> Telegram bridge (+ website RSS -> Telegram)

Runs on GitHub Actions every ~15 minutes.
- Reads the newest posts of the Instagram professional account (Business or Creator)
  via Meta's "Instagram API with Instagram Login".
- Publishes new photos, videos/reels and carousels (as albums) to the Telegram channel,
  with hashtags removed and a short footer.
- Optionally posts new website articles (RSS) as title + link.
- On the very first run it only memorises existing posts, so the channel is not flooded.
- If anything fails, it sends a private alert to the admin on Telegram.
"""
import html
import json
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET

import requests

IG_API = "https://graph.instagram.com/v21.0"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHANNEL = os.environ.get("TG_CHANNEL", "").strip()          # e.g. @motorsportsclicks
TG_ADMIN = os.environ.get("TG_ADMIN_CHAT_ID", "").strip()      # your private chat id with the bot
IG_TOKEN = os.environ.get("IG_TOKEN", "").strip()
RSS_URL = os.environ.get("RSS_URL", "").strip()
IG_PROFILE_URL = os.environ.get("IG_PROFILE_URL", "https://www.instagram.com/motorsportsclicks/").strip()

CAPTION_LIMIT = 1024          # Telegram limit for media captions
TEXT_LIMIT = 4096             # Telegram limit for text messages
UPLOAD_LIMIT = 49 * 1024 * 1024
MAX_POSTS_PER_RUN = 10
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"ig_initialized": False, "ig_seen": [], "rss_initialized": False, "rss_seen": []}


def save_state(state):
    state["ig_seen"] = state["ig_seen"][-300:]
    state["rss_seen"] = state["rss_seen"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------- telegram
def tg(method, data=None, files=None):
    if DRY_RUN:
        print(f"[dry-run] {method}: {json.dumps(data, ensure_ascii=False)[:300]}")
        return {}
    for attempt in range(4):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data, files=files, timeout=120)
        j = r.json()
        if j.get("ok"):
            return j["result"]
        retry = j.get("parameters", {}).get("retry_after")
        if retry:
            time.sleep(retry + 1)
            continue
        raise RuntimeError(f"Telegram {method} failed: {j.get('description')}")
    raise RuntimeError(f"Telegram {method} failed after retries")


def alert(text):
    if TG_ADMIN and TG_TOKEN:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_ADMIN, "text": text[:TEXT_LIMIT]}, timeout=30)
        except Exception:
            pass


# ---------------------------------------------------------------- captions
HASHTAG = re.compile(r"(?<!\w)#[\wÀ-￿]+", re.UNICODE)


def clean_caption(raw):
    text = raw or ""
    text = HASHTAG.sub("", text)
    lines = [re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # drop trailing dots/lines often used to push hashtags down
    text = re.sub(r"(\n\s*[.·•\-_]\s*)+$", "", text).strip()
    return text


def footer(permalink):
    link = permalink or IG_PROFILE_URL
    return f'\n\n📸 <a href="{html.escape(link)}">Instagram</a>'


def build_texts(caption, permalink):
    """Return (media_caption_html, extra_text_html_or_None)."""
    body = html.escape(clean_caption(caption))
    full = (body + footer(permalink)).strip()
    if len(full) <= CAPTION_LIMIT:
        return full, None
    if len(full) <= TEXT_LIMIT:
        return None, full
    return None, body[: TEXT_LIMIT - 200].rsplit(" ", 1)[0] + "…" + footer(permalink)


# ---------------------------------------------------------------- media
def download(url):
    r = requests.get(url, timeout=180, stream=True)
    r.raise_for_status()
    data = b""
    for chunk in r.iter_content(1 << 20):
        data += chunk
        if len(data) > UPLOAD_LIMIT:
            return None
    return data


def send_single(item, caption, permalink):
    kind = item["media_type"]
    url = item.get("media_url")
    data = download(url) if url else None
    if data is None:
        # too big (long reels) or no URL: send text with a link instead
        txt = (caption + "\n\n" if caption else "") + f'🎬 <a href="{html.escape(permalink)}">Watch on Instagram</a>'
        return tg("sendMessage", {"chat_id": TG_CHANNEL, "text": txt[:TEXT_LIMIT], "parse_mode": "HTML"})
    params = {"chat_id": TG_CHANNEL, "parse_mode": "HTML"}
    if caption:
        params["caption"] = caption
    if kind == "VIDEO":
        params["supports_streaming"] = "true"
        return tg("sendVideo", params, files={"video": ("video.mp4", data)})
    return tg("sendPhoto", params, files={"photo": ("photo.jpg", data)})


def send_album(children, caption):
    children = children[:10]
    files, media = {}, []
    for i, ch in enumerate(children):
        data = download(ch.get("media_url")) if ch.get("media_url") else None
        if data is None:
            continue
        key = f"f{i}"
        is_video = ch["media_type"] == "VIDEO"
        files[key] = (f"{key}.{'mp4' if is_video else 'jpg'}", data)
        entry = {"type": "video" if is_video else "photo", "media": f"attach://{key}"}
        if not media and caption:
            entry["caption"] = caption
            entry["parse_mode"] = "HTML"
        media.append(entry)
    if len(media) == 1:
        only = media[0]
        method, field = ("sendVideo", "video") if only["type"] == "video" else ("sendPhoto", "photo")
        params = {"chat_id": TG_CHANNEL}
        if caption:
            params.update(caption=caption, parse_mode="HTML")
        return tg(method, params, files={field: files[only["media"].split("//")[1]]})
    if not media:
        raise RuntimeError("carousel without downloadable media")
    return tg("sendMediaGroup", {"chat_id": TG_CHANNEL, "media": json.dumps(media)}, files=files)


def publish(post):
    caption, extra = build_texts(post.get("caption"), post.get("permalink"))
    if post["media_type"] == "CAROUSEL_ALBUM":
        children = post.get("children", {}).get("data", [])
        send_album(children, caption)
    else:
        send_single(post, caption, post.get("permalink") or IG_PROFILE_URL)
    if extra:
        tg("sendMessage", {"chat_id": TG_CHANNEL, "text": extra, "parse_mode": "HTML",
                           "disable_web_page_preview": "true"})


def fetch_instagram():
    fields = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,children{media_type,media_url}"
    r = requests.get(f"{IG_API}/me/media", params={"fields": fields, "limit": 15, "access_token": IG_TOKEN}, timeout=60)
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"Instagram API error: {j['error'].get('message')}")
    return j.get("data", [])


# ---------------------------------------------------------------- rss
def fetch_rss():
    r = requests.get(RSS_URL, timeout=60, headers={"User-Agent": "motorsportsclicks-bridge/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        items.append({"id": (it.findtext("guid") or it.findtext("link") or "").strip(),
                      "title": (it.findtext("title") or "").strip(),
                      "link": (it.findtext("link") or "").strip()})
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.findall("a:entry", ns):
        link = e.find("a:link", ns)
        items.append({"id": (e.findtext("a:id", namespaces=ns) or "").strip(),
                      "title": (e.findtext("a:title", namespaces=ns) or "").strip(),
                      "link": link.get("href") if link is not None else ""})
    return [i for i in items if i["id"]]


# ---------------------------------------------------------------- main
def main():
    missing = [k for k, v in {"TG_BOT_TOKEN": TG_TOKEN, "TG_CHANNEL": TG_CHANNEL, "IG_TOKEN": IG_TOKEN}.items() if not v]
    if missing:
        print("Not configured yet (missing: " + ", ".join(missing) + "). Nothing to do.")
        return

    state = load_state()

    # Instagram
    posts = fetch_instagram()
    if not state["ig_initialized"]:
        state["ig_seen"] = [p["id"] for p in posts]
        state["ig_initialized"] = True
        print(f"First run: memorised {len(posts)} existing Instagram posts, nothing published.")
    else:
        new = [p for p in posts if p["id"] not in state["ig_seen"]]
        new.sort(key=lambda p: p.get("timestamp", ""))
        for p in new[:MAX_POSTS_PER_RUN]:
            publish(p)
            state["ig_seen"].append(p["id"])
            save_state(state)          # save after each post so a crash never re-posts
            print(f"Published Instagram post {p['id']} ({p['media_type']})")
            time.sleep(2)

    # Website RSS (optional)
    if RSS_URL:
        items = fetch_rss()
        if not state["rss_initialized"]:
            state["rss_seen"] = [i["id"] for i in items]
            state["rss_initialized"] = True
            print(f"First run: memorised {len(items)} existing articles.")
        else:
            new = [i for i in items if i["id"] not in state["rss_seen"]][::-1]
            for i in new[:5]:
                # only the link: Telegram shows the article preview (title + image) by itself
                tg("sendMessage", {"chat_id": TG_CHANNEL, "text": i["link"]})
                state["rss_seen"].append(i["id"])
                save_state(state)
                print(f"Published article: {i['title']}")

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        alert("⚠️ Automazione Instagram → Telegram: qualcosa non ha funzionato.\n\n"
              f"Errore: {e}\n\nNiente panico: apri la chat con Claude e incolla questo messaggio.")
        sys.exit(1)
