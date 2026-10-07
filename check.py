#!/usr/bin/env python3
"""
Streaming Notifier
-------------------
Syncs a Twitch stream schedule into a Discord message (edited in place),
and posts Discord notifications when you go live on Twitch/YouTube/Kick
or publish a new YouTube video.

All credentials are read from environment variables (set as GitHub Actions
secrets in the workflow). State (last video id, live flags, schedule
message id) is persisted in state.json, which this script updates and the
workflow commits back to the repo after every run.
"""

import calendar
import io
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

STATE_FILE = "state.json"
LOCAL_TZ = ZoneInfo("Europe/Zurich")
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
WEEKDAYS_DE_UPPER = [w.upper() for w in WEEKDAYS_DE]
MONTH_NAMES_DE = [
    "", "JANUAR", "FEBRUAR", "MÄRZ", "APRIL", "MAI", "JUNI",
    "JULI", "AUGUST", "SEPTEMBER", "OKTOBER", "NOVEMBER", "DEZEMBER",
]

COLOR_TWITCH = 0x9146FF
COLOR_YOUTUBE = 0xFF0000
COLOR_KICK = 0x53FC18

# --- Calendar image colors/fonts ---
CAL_BLACK = (0, 0, 0)
CAL_WHITE = (255, 255, 255)
CAL_BLUE = (0, 116, 255)  # #0074ff
CAL_GREY = (55, 55, 55)
CAL_DIMGREY = (120, 120, 120)
CAL_CELL_ACTIVE_BG = (10, 14, 22)
CAL_TEXT_STROKE = (5, 20, 45)  # dark navy outline around blue text


def _cal_font(name, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def describe_error(e):
    """Turn a requests exception into a diagnostic message including the
    server's actual error body, not just the generic HTTP status text."""
    resp = getattr(e, "response", None)
    if resp is not None:
        body = (resp.text or "")[:500]
        # Strip the query string so API keys never end up in (possibly public) logs.
        return f"HTTP {resp.status_code} from {resp.url.split('?')[0]} -> {body}"
    return str(e)


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------

MENTION_EVERYONE = {
    "content": "@everyone",
    "allowed_mentions": {"parse": ["everyone"]},
}


def discord_post(webhook_url, embed, mention=True):
    payload = {"embeds": [embed]}
    if mention:
        payload.update(MENTION_EVERYONE)
    resp = requests.post(
        webhook_url + "?wait=true",
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def discord_edit(webhook_url, message_id, embed, mention=True):
    payload = {"embeds": [embed]}
    if mention:
        payload.update(MENTION_EVERYONE)
    resp = requests.patch(
        f"{webhook_url}/messages/{message_id}",
        json=payload,
        timeout=15,
    )
    if resp.status_code == 404:
        return None  # message was deleted, caller should re-post
    resp.raise_for_status()
    return message_id


def discord_upsert(webhook_url, embed, existing_message_id, mention=True):
    """Edit the existing message in place, or post a new one if it doesn't exist yet."""
    if existing_message_id:
        result = discord_edit(webhook_url, existing_message_id, embed, mention=mention)
        if result:
            return result
        log("Stored schedule message no longer exists, posting a new one.")
    return discord_post(webhook_url, embed, mention=mention)


def discord_post_image(webhook_url, image_bytes, filename, mention=True):
    payload = {"attachments": [{"id": 0, "filename": filename}], "embeds": []}
    if mention:
        payload.update(MENTION_EVERYONE)
    resp = requests.post(
        webhook_url + "?wait=true",
        data={"payload_json": json.dumps(payload)},
        files={"files[0]": (filename, image_bytes, "image/png")},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def discord_edit_image(webhook_url, message_id, image_bytes, filename, mention=True):
    payload = {"attachments": [{"id": 0, "filename": filename}], "embeds": []}
    if mention:
        payload.update(MENTION_EVERYONE)
    resp = requests.patch(
        f"{webhook_url}/messages/{message_id}",
        data={"payload_json": json.dumps(payload)},
        files={"files[0]": (filename, image_bytes, "image/png")},
        timeout=30,
    )
    if resp.status_code == 404:
        return None  # message was deleted, caller should re-post
    resp.raise_for_status()
    return message_id


def discord_upsert_image(webhook_url, image_bytes, filename, existing_message_id, mention=True):
    """Edit the existing message's image in place, or post a new one if it doesn't exist yet."""
    if existing_message_id:
        result = discord_edit_image(webhook_url, existing_message_id, image_bytes, filename, mention=mention)
        if result:
            return result
        log("Stored schedule message no longer exists, posting a new one.")
    return discord_post_image(webhook_url, image_bytes, filename, mention=mention)


# ---------------------------------------------------------------------------
# Twitch
# ---------------------------------------------------------------------------

def twitch_get_token(client_id, client_secret):
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def twitch_headers(client_id, token):
    return {"Client-Id": client_id, "Authorization": f"Bearer {token}"}


def twitch_get_broadcaster_id(client_id, token, login):
    resp = requests.get(
        "https://api.twitch.tv/helix/users",
        headers=twitch_headers(client_id, token),
        params={"login": login},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        raise RuntimeError(f"Twitch user '{login}' not found")
    return data[0]["id"]


def twitch_get_schedule(client_id, token, broadcaster_id):
    resp = requests.get(
        "https://api.twitch.tv/helix/schedule",
        headers=twitch_headers(client_id, token),
        params={"broadcaster_id": broadcaster_id, "first": 10},
        timeout=15,
    )
    if resp.status_code == 404:
        return []  # no schedule configured
    resp.raise_for_status()
    return resp.json()["data"].get("segments") or []


def twitch_is_live(client_id, token, broadcaster_id):
    resp = requests.get(
        "https://api.twitch.tv/helix/streams",
        headers=twitch_headers(client_id, token),
        params={"user_id": broadcaster_id},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data[0] if data else None


def twitch_get_games(client_id, token, game_ids):
    """Batch-fetch box art URLs for a list of Twitch game/category ids."""
    game_ids = [gid for gid in dict.fromkeys(game_ids) if gid]  # unique, preserve order
    if not game_ids:
        return {}
    resp = requests.get(
        "https://api.twitch.tv/helix/games",
        headers=twitch_headers(client_id, token),
        params=[("id", gid) for gid in game_ids],
        timeout=15,
    )
    resp.raise_for_status()
    out = {}
    for g in resp.json().get("data") or []:
        url = (g.get("box_art_url") or "").replace("{width}", "144").replace("{height}", "192")
        out[g["id"]] = url
    return out


def _download_image(url):
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        log(f"Could not download box art from {url}: {describe_error(e)}")
        return None


def _wrap_text(draw, text, font, max_w, max_lines):
    words = text.split(" ")
    lines, cur = [], ""
    for w_ in words:
        test = (cur + " " + w_).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def build_schedule_calendar_image(login, segments, client_id, token):
    """Renders the upcoming Twitch schedule as a black/blue month-calendar PNG,
    with real Twitch box art thumbnails for days that have a game set."""
    now_local = datetime.now(LOCAL_TZ)
    year, month = now_local.year, now_local.month

    # Bucket segments that fall within the displayed month, keyed by day-of-month.
    by_day = {}
    skipped = 0
    for seg in segments:
        start = datetime.fromisoformat(seg["start_time"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        if start.year != year or start.month != month:
            skipped += 1
            continue
        category = seg.get("category") or {}
        by_day[start.day] = {
            "time": start.strftime("%H:%M"),
            "game": category.get("name"),
            "game_id": category.get("id"),
            "title": (seg.get("title") or "").upper(),
        }
    if skipped:
        log(f"Schedule calendar: {skipped} segment(s) fall outside {MONTH_NAMES_DE[month]} {year}, not shown.")

    # Fetch real box art for every distinct game in view.
    game_ids = [ev["game_id"] for ev in by_day.values() if ev.get("game_id")]
    try:
        box_art_urls = twitch_get_games(client_id, token, game_ids)
    except Exception as e:
        log(f"Twitch box art lookup failed: {describe_error(e)}")
        box_art_urls = {}
    art_images = {gid: _download_image(url) for gid, url in box_art_urls.items()}

    f_title_month = _cal_font("YoungSerif-Regular.ttf", 54)
    f_weekday_hdr = _cal_font("Outfit-Bold.ttf", 18)
    f_daynum = _cal_font("Outfit-Bold.ttf", 26)
    f_time = _cal_font("Outfit-Bold.ttf", 19)
    f_game = _cal_font("Outfit-Bold.ttf", 17)
    f_evtitle = _cal_font("Outfit-Regular.ttf", 15)
    f_sub = _cal_font("Outfit-Regular.ttf", 19)

    cal = calendar.Calendar(firstweekday=0)  # Monday first
    weeks = cal.monthdayscalendar(year, month)

    W = 1600
    PAD = 50
    HEADER_H = 130
    WD_HDR_H = 46
    COLS = 7
    CELL_W = (W - 2 * PAD) // COLS
    CELL_H = 190
    ART_SCRIM_ALPHA = 165  # darkens the full-cell box art so text stays readable
    H = HEADER_H + WD_HDR_H + len(weeks) * CELL_H + PAD

    img = Image.new("RGB", (W, H), CAL_BLACK)
    draw = ImageDraw.Draw(img)

    draw.text((PAD, 34), MONTH_NAMES_DE[month], font=f_title_month, fill=CAL_WHITE)
    mw = draw.textlength(MONTH_NAMES_DE[month], font=f_title_month)
    draw.text((PAD + mw + 22, 52), str(year), font=f_sub, fill=CAL_DIMGREY)
    draw.text((PAD, 94), f"{login} STREAMINGPLAN", font=f_sub, fill=CAL_BLUE, stroke_width=1, stroke_fill=CAL_TEXT_STROKE)

    grid_top = HEADER_H + WD_HDR_H
    for c, wd in enumerate(WEEKDAYS_DE_UPPER):
        x = PAD + c * CELL_W
        draw.text((x + 10, HEADER_H + 12), wd, font=f_weekday_hdr, fill=CAL_DIMGREY)
    draw.line([(PAD, grid_top), (W - PAD, grid_top)], fill=CAL_GREY, width=1)

    for r, week in enumerate(weeks):
        y0 = grid_top + r * CELL_H
        y1 = y0 + CELL_H
        for c, day in enumerate(week):
            x0 = PAD + c * CELL_W
            x1 = x0 + CELL_W
            max_w = CELL_W - 24

            ev = by_day.get(day) if day != 0 else None
            art = art_images.get(ev.get("game_id")) if ev else None

            if ev:
                if art:
                    # Fill the entire cell with a cropped/scaled box-art image, then
                    # darken it with a scrim so the white/blue text on top stays readable.
                    fitted = ImageOps.fit(art, (x1 - x0, y1 - y0), method=Image.LANCZOS)
                    scrim = Image.new("RGBA", fitted.size, (0, 0, 0, ART_SCRIM_ALPHA))
                    darkened = Image.alpha_composite(fitted.convert("RGBA"), scrim).convert("RGB")
                    img.paste(darkened, (x0, y0))
                else:
                    draw.rectangle([x0, y0, x1, y1], fill=CAL_CELL_ACTIVE_BG)
                draw.rectangle([x0, y0, x1, y1], outline=CAL_BLUE, width=2)
            else:
                draw.rectangle([x0, y0, x1, y1], outline=CAL_GREY, width=1)

            if day != 0:
                draw.text((x0 + 12, y0 + 10), str(day), font=f_daynum, fill=CAL_WHITE if ev else CAL_DIMGREY)

            if ev:
                ty = y0 + 48
                draw.text((x0 + 12, ty), f"{ev['time']} UHR", font=f_time, fill=CAL_BLUE, stroke_width=1, stroke_fill=CAL_TEXT_STROKE)
                ty += 26
                if ev["game"]:
                    for line in _wrap_text(draw, ev["game"], f_game, max_w, 2):
                        draw.text((x0 + 12, ty), line, font=f_game, fill=CAL_BLUE, stroke_width=1, stroke_fill=CAL_TEXT_STROKE)
                        ty += 21
                for line in _wrap_text(draw, ev["title"], f_evtitle, max_w, 3):
                    draw.text((x0 + 12, ty), line, font=f_evtitle, fill=CAL_WHITE)
                    ty += 19

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def handle_twitch(cfg, state):
    client_id = cfg["TWITCH_CLIENT_ID"]
    client_secret = cfg["TWITCH_CLIENT_SECRET"]
    login = cfg["TWITCH_LOGIN"]

    token = twitch_get_token(client_id, client_secret)
    broadcaster_id = twitch_get_broadcaster_id(client_id, token, login)

    # --- Schedule sync ---
    try:
        segments = twitch_get_schedule(client_id, token, broadcaster_id)
        image_bytes = build_schedule_calendar_image(login, segments, client_id, token)
        msg_id = discord_upsert_image(
            cfg["DISCORD_WEBHOOK_SCHEDULE"], image_bytes, "streamingplan.png",
            state.get("schedule_message_id"),
        )
        state["schedule_message_id"] = msg_id
        log(f"Twitch schedule calendar synced ({len(segments)} segments).")
    except Exception as e:
        log(f"Twitch schedule sync failed: {describe_error(e)}")

    # --- Live check ---
    try:
        stream = twitch_is_live(client_id, token, broadcaster_id)
        was_live = state.get("twitch_live", False)
        if stream:
            # Cache-bust the thumbnail URL so Discord actually re-fetches a fresh
            # preview on every edit instead of reusing the very first snapshot.
            raw_thumb = (stream.get("thumbnail_url") or "").replace("{width}", "640").replace("{height}", "360")
            cache_bust = int(datetime.now(timezone.utc).timestamp())
            thumb_url = f"{raw_thumb}?_={cache_bust}" if raw_thumb else ""
            embed = {
                "title": f"\U0001F534 {login} ist jetzt LIVE auf Twitch!",
                "description": stream.get("title", ""),
                "url": f"https://www.twitch.tv/{login}",
                "color": COLOR_TWITCH,
                "fields": [{"name": "Kategorie", "value": stream.get("game_name") or "-", "inline": True}],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if thumb_url:
                embed["image"] = {"url": thumb_url}

            if not was_live:
                msg_id = discord_post(cfg["DISCORD_WEBHOOK_TWITCH_LIVE"], embed, mention=True)
                state["twitch_live_message_id"] = msg_id
                log("Posted Twitch live notification.")
            else:
                existing_id = state.get("twitch_live_message_id")
                result = discord_edit(cfg["DISCORD_WEBHOOK_TWITCH_LIVE"], existing_id, embed, mention=False) if existing_id else None
                if result:
                    log("Updated Twitch live notification with a fresh preview.")
                else:
                    msg_id = discord_post(cfg["DISCORD_WEBHOOK_TWITCH_LIVE"], embed, mention=True)
                    state["twitch_live_message_id"] = msg_id
                    log("Twitch live message was missing, posted a new one.")
        else:
            state["twitch_live_message_id"] = None
        state["twitch_live"] = bool(stream)
    except Exception as e:
        log(f"Twitch live check failed: {describe_error(e)}")


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def youtube_get_channel(api_key, handle):
    handle_param = handle if handle.startswith("@") else f"@{handle}"
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "id,contentDetails", "forHandle": handle_param, "key": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items") or []
    if not items:
        raise RuntimeError(f"YouTube channel '{handle}' not found")
    channel_id = items[0]["id"]
    uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    return channel_id, uploads_playlist


def youtube_get_latest_video(api_key, uploads_playlist_id):
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        params={"part": "snippet,contentDetails", "playlistId": uploads_playlist_id, "maxResults": 1, "key": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items") or []
    if not items:
        return None
    item = items[0]
    return {
        "video_id": item["contentDetails"]["videoId"],
        "title": item["snippet"]["title"],
        "thumbnail": (item["snippet"].get("thumbnails", {}).get("high") or {}).get("url", ""),
    }


def youtube_is_live(api_key, channel_id):
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={"part": "snippet", "channelId": channel_id, "eventType": "live", "type": "video", "key": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("items") or []
    return items[0] if items else None


def handle_youtube(cfg, state):
    api_key = cfg["YOUTUBE_API_KEY"]
    handle = cfg["YOUTUBE_HANDLE"]

    try:
        channel_id, uploads_playlist = youtube_get_channel(api_key, handle)
    except Exception as e:
        log(f"YouTube channel lookup failed: {describe_error(e)}")
        return

    # --- New video check ---
    try:
        latest = youtube_get_latest_video(api_key, uploads_playlist)
        last_seen = state.get("youtube_last_video_id")
        if latest:
            if last_seen is None:
                log("YouTube: first run, recording latest video without announcing it.")
            elif latest["video_id"] != last_seen:
                embed = {
                    "title": f"\U0001F4F9 Neues Video: {latest['title']}",
                    "url": f"https://www.youtube.com/watch?v={latest['video_id']}",
                    "color": COLOR_YOUTUBE,
                    "image": {"url": latest["thumbnail"]},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                discord_post(cfg["DISCORD_WEBHOOK_YOUTUBE"], embed)
                log("Posted YouTube new video notification.")
            state["youtube_last_video_id"] = latest["video_id"]
    except Exception as e:
        log(f"YouTube new video check failed: {describe_error(e)}")

    # --- Live check ---
    try:
        live_item = youtube_is_live(api_key, channel_id)
        was_live = state.get("youtube_live", False)
        if live_item and not was_live:
            video_id = live_item["id"]["videoId"]
            embed = {
                "title": f"\U0001F534 Live auf YouTube: {live_item['snippet']['title']}",
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "color": COLOR_YOUTUBE,
                "image": {"url": (live_item["snippet"].get("thumbnails", {}).get("high") or {}).get("url", "")},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            discord_post(cfg["DISCORD_WEBHOOK_YOUTUBE"], embed)
            log("Posted YouTube live notification.")
        state["youtube_live"] = bool(live_item)
    except Exception as e:
        log(f"YouTube live check failed: {describe_error(e)}")


# ---------------------------------------------------------------------------
# Kick
# ---------------------------------------------------------------------------

def kick_get_token(client_id, client_secret):
    resp = requests.post(
        "https://id.kick.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def kick_get_channel(token, slug):
    resp = requests.get(
        "https://api.kick.com/public/v1/channels",
        headers={"Authorization": f"Bearer {token}"},
        params={"slug": slug},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or []
    return data[0] if data else None


def handle_kick(cfg, state):
    try:
        token = kick_get_token(cfg["KICK_CLIENT_ID"], cfg["KICK_CLIENT_SECRET"])
        channel = kick_get_channel(token, cfg["KICK_SLUG"])
    except Exception as e:
        log(f"Kick lookup failed: {describe_error(e)}")
        return

    log(f"Kick raw channel response: {json.dumps(channel)[:800]}")

    try:
        stream = (channel or {}).get("stream") or {}
        is_live = bool(stream.get("is_live"))
        was_live = state.get("kick_live", False)
        if is_live and not was_live:
            embed = {
                "title": f"\U0001F7E2 {cfg['KICK_SLUG']} ist jetzt LIVE auf Kick!",
                "description": (channel.get("stream_title") or ""),
                "url": f"https://kick.com/{cfg['KICK_SLUG']}",
                "color": COLOR_KICK,
                "fields": [{"name": "Zuschauer", "value": str(stream.get("viewer_count", "-")), "inline": True}],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            discord_post(cfg["DISCORD_WEBHOOK_KICK"], embed)
            log("Posted Kick live notification.")
        state["kick_live"] = is_live
    except Exception as e:
        log(f"Kick live check failed: {describe_error(e)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

REQUIRED_ENV = [
    "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_LOGIN",
    "YOUTUBE_API_KEY", "YOUTUBE_HANDLE",
    "KICK_CLIENT_ID", "KICK_CLIENT_SECRET", "KICK_SLUG",
    "DISCORD_WEBHOOK_SCHEDULE", "DISCORD_WEBHOOK_TWITCH_LIVE",
    "DISCORD_WEBHOOK_YOUTUBE", "DISCORD_WEBHOOK_KICK",
]


def main():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        log(f"Missing environment variables: {missing}")
        sys.exit(1)

    cfg = {k: os.environ[k] for k in REQUIRED_ENV}
    state = load_state()

    handle_twitch(cfg, state)
    handle_youtube(cfg, state)
    handle_kick(cfg, state)

    state["last_checked_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log("Done.")


if __name__ == "__main__":
    main()
