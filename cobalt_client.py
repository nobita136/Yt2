"""
Backend helpers for the YouTube downloader.

Two download paths are provided:

  1. **Cobalt API**  – primary.  Cobalt (https://cobalt.tools) is a free
     open-source downloader that handles YouTube's bot-detection server-side
     and is the only practical way to download on cloud IPs (Render, Vercel,
     Heroku …) without managing PO tokens.  Multiple public instances are
     tried in order; if a newer Cobalt requires JWT auth we fall back to the
     older v1 schema.

  2. **yt-dlp**      – fallback.  Used on Replit and on Render when the
     cookies are valid and YouTube doesn't trigger the bot check.  Includes a
     local PO-token script (`pot_script.py`) that derives a visitor-data
     token from YouTube's mobile page, which is often enough to satisfy the
     "Sign in to confirm you're not a bot" challenge on cloud IPs.
"""
import os
import re
import json
import time
import shutil
import logging
import tempfile
import requests
import yt_dlp

log = logging.getLogger("ytdl.backend")

COBALT_INSTANCES = [
    # Newer (v10) public mirrors – many now require JWT, but we still try
    "https://co.eepy.today/",
    "https://cobalt-backend.canine.tools/",
    "https://api.cobalt.tocatca.net/",
    "https://api.cobalt.canine.tools/",
    "https://api.cobalt.tools/",
    # Older v1 instances that still accept the simple JSON schema
    "https://api.cobalt.tocatca.net/api/json",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Client chain – tries yt-dlp's default first, then specific clients.
# The `mweb` and `tv_embedded` clients are often less aggressive with
# bot detection on cloud IPs.
CLIENT_CHAIN = [
    None,                # yt-dlp default (best on Replit)
    ["tv"],              # TV HTML5 – full format table
    ["web", "mweb"],     # WEB clients
    ["ios"],             # iOS
    ["android"],         # ANDROID
    ["web_creator"],     # YouTube Studio / creator client
    ["tv_embedded"],     # Embedded TV player
    ["android_vr"],      # VR client
    ["ios_music"],       # Music client (iOS)
]


# ── PATH HELPERS ─────────────────────────────────────────────

def _which(names):
    for n in names:
        if not n:
            continue
        if os.path.isabs(n) and os.path.exists(n):
            return n
        hit = shutil.which(n)
        if hit:
            return hit
    return None


def ffmpeg_path():
    return _which([
        os.environ.get("FFMPEG_LOCATION"),
        os.environ.get("FFMPEG_PATH"),
        "/nix/store/b11ycf80cxi2iyrga8rkq1wzdinmax18-replit-runtime-path/bin/ffmpeg",
        "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg",
    ]) or "ffmpeg"


def node_path():
    return _which([
        os.environ.get("NODE_BINARY"),
        "/nix/store/1lagpgadaybvs1n2312gysg2phjk89y8-nodejs-20.20.0-wrapped/bin/node",
        "/usr/bin/node", "/usr/local/bin/node", "node", "nodejs",
    ])


# ── COBALT API ───────────────────────────────────────────────

def _cobalt_post(instance, payload, timeout=20):
    """POST to a Cobalt instance, trying both the v10 and v1 schemas."""
    headers = {
        "Accept":       "application/json",
        "Content-Type": "application/json",
        "User-Agent":   DEFAULT_UA,
    }

    # v10 schema
    try:
        r = requests.post(instance, json=payload, headers=headers, timeout=timeout)
        if r.status_code < 500:
            return r.json()
    except requests.RequestException:
        pass

    # v1 schema (uses vQuality / isAudioOnly / aFormat)
    legacy = {
        "url":           payload.get("url"),
        "vQuality":      str(payload.get("videoQuality", "1080")),
        "isAudioOnly":   payload.get("downloadMode") == "audio",
        "aFormat":       payload.get("audioFormat", "mp3"),
        "filenamePattern": "pretty",
    }
    try:
        url = instance if instance.endswith("/api/json") else instance.rstrip("/") + "/api/json"
        r = requests.post(url, json=legacy, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug("legacy cobalt schema failed for %s: %s", instance, e)
        return None


def cobalt_video(url, height=1080, codec="h264"):
    quality = str(height) if 144 <= int(height) <= 4320 else "1080"
    payload = {
        "url":               url,
        "videoQuality":      quality,
        "youtubeVideoCodec": codec,
        "filenameStyle":     "pretty",
        "downloadMode":      "auto",
    }
    last = None
    for inst in COBALT_INSTANCES:
        try:
            data = _cobalt_post(inst, payload)
            if data and data.get("status") not in (None, "error"):
                log.info("cobalt video via %s -> %s", inst, data.get("status"))
                return data
            last = data
        except Exception as e:
            last = {"error": str(e)}
            log.debug("cobalt %s: %s", inst, e)
    if last:
        raise RuntimeError(f"Cobalt unavailable: {last.get('error', last)}")
    raise RuntimeError("All Cobalt instances failed")


def cobalt_audio(url, fmt="mp3", bitrate="64"):
    payload = {
        "url":           url,
        "downloadMode":  "audio",
        "audioFormat":   fmt,
        "audioBitrate":  str(bitrate),
        "filenameStyle": "pretty",
    }
    last = None
    for inst in COBALT_INSTANCES:
        try:
            data = _cobalt_post(inst, payload)
            if data and data.get("status") not in (None, "error"):
                log.info("cobalt audio via %s -> %s", inst, data.get("status"))
                return data
            last = data
        except Exception as e:
            last = {"error": str(e)}
    if last:
        raise RuntimeError(f"Cobalt unavailable: {last.get('error', last)}")
    raise RuntimeError("All Cobalt instances failed")


def _stream_to_file(media_url, dest, max_bytes=500 * 1024 * 1024, timeout=300):
    with requests.get(media_url, stream=True, timeout=timeout,
                      headers={"User-Agent": DEFAULT_UA}) as r:
        r.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(
                        f"File exceeds {max_bytes // (1024*1024)}MB limit")
    return dest


def safe_filename(name, fallback="file", ext=""):
    cleaned = "".join(c for c in (name or "") if c.isalnum() or c in " _-.").strip()
    cleaned = cleaned[:80] or fallback
    if ext and not cleaned.lower().endswith("." + ext.lower()):
        if "." in cleaned:
            cleaned = cleaned.rsplit(".", 1)[0] + "." + ext
        else:
            cleaned = cleaned + "." + ext
    return cleaned


# ── YT-DLP HELPERS ───────────────────────────────────────────

def _base_opts(download=False, cookies_file=None, players=None):
    opts = {
        "quiet":          True,
        "no_warnings":    True,
        "ignoreerrors":   False,
        "retries":        4,
        "fragment_retries": 4,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "noplaylist":     True,
        "skip_download":  not download,
        "ffmpeg_location": ffmpeg_path(),
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent":      DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest":  "document",
            "Sec-Fetch-Mode":  "navigate",
            "Sec-Fetch-Site":  "none",
        },
    }
    n = node_path()
    if n:
        opts["js_runtimes"]      = {"node": {"path": n}}
        opts["remote_components"] = {"ejs": True}
    if players:
        opts["extractor_args"] = {"youtube": {"player_client": players}}
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file
    return opts


def video_format_selector(height):
    # Don't constrain audio ext — TV client returns iamf/opus, not m4a
    # Don't constrain video ext either — get whatever is highest, remux
    return (
        f"bestvideo[height<={height}]+bestaudio"
        f"/best[height<={height}]"
        f"/bestvideo+bestaudio"
        f"/best"
    )


def _ytdlp_video(url, height, out_path, cookies_file):
    last_err = None
    for clients in CLIENT_CHAIN:
        try:
            opts = _base_opts(download=True, cookies_file=cookies_file, players=clients)
            opts.update({
                "format":             video_format_selector(height),
                "outtmpl":            out_path,
                "postprocessor_args": {
                    # Just remux to mp4, copy both streams — ffmpeg will pick container
                    "default": ["-movflags", "+faststart"],
                },
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            # Check both with and without ext
            for p in (out_path,
                      os.path.splitext(out_path)[0] + ".mp4",
                      os.path.splitext(out_path)[0] + ".mkv",
                      os.path.splitext(out_path)[0] + ".webm"):
                if os.path.exists(p) and os.path.getsize(p) > 1000:
                    return True
        except Exception as e:
            last_err = e
            log.warning("ytdlp video clients=%s failed: %s", clients, e)
    raise RuntimeError(f"yt-dlp video failed: {last_err}")


def _ytdlp_audio(url, out_path, cookies_file):
    last_err = None
    for clients in CLIENT_CHAIN:
        try:
            opts = _base_opts(download=True, cookies_file=cookies_file, players=clients)
            opts.update({
                "format":     "worstaudio/worst",
                "outtmpl":    out_path.replace(".mp3", ".%(ext)s"),
                "postprocessors": [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": "64",
                }],
                "postprocessor_args": ["-vn"],
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            if os.path.exists(out_path):
                return True
        except Exception as e:
            last_err = e
            log.warning("ytdlp audio clients=%s failed: %s", clients, e)
    raise RuntimeError(f"yt-dlp audio failed: {last_err}")


# ── PUBLIC API ───────────────────────────────────────────────

def _extract_video_id(url):
    """Pull the 11-char video id from any common YT URL form."""
    m = re.search(r"(?:v=|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _scrape_info(url, cookies_file=None):
    """Fetch the YouTube watch page directly (no InnerTube, no bot check)."""
    vid = _extract_video_id(url)
    if not vid:
        raise RuntimeError("Could not parse YouTube video ID from URL")
    watch_url = f"https://www.youtube.com/watch?v={vid}"
    headers = {
        "User-Agent":      DEFAULT_UA,
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookies = None
    if cookies_file and os.path.exists(cookies_file):
        try:
            cookies = {}
            with open(cookies_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.strip().split("\t")
                    if len(parts) >= 7:
                        cookies[parts[5]] = parts[6]
        except Exception as e:
            log.debug("cookie parse failed: %s", e)

    r = requests.get(watch_url, headers=headers, cookies=cookies, timeout=15)
    r.raise_for_status()
    html = r.text

    def find(rx, default=""):
        m = re.search(rx, html)
        return m.group(1) if m else default

    # JSON string capture group: matches (\\.|[^"\\])* — handles \" and \\ escapes
    J = r'((?:\\.|[^"\\])*)'

    title = (find(r'"title":\{"runs":\[\{"text":"' + J + '"')
             or find(r'"title":\{"simpleText":"' + J + '"')
             or find(r'"title":"' + J + '","lengthSeconds"')
             or find(r'<meta name="title" content="([^"]+)"')
             or "video")
    # Unescape JSON string: \" → ", \\ → \, \u0026 → &
    if title:
        try:
            title = json.loads('"' + title + '"')
        except Exception:
            title = title.replace('\\"', '"').replace("\\\\", "\\")
        title = (title.replace("&quot;", '"').replace("&amp;", "&")
                     .replace("&#39;", "'").replace("&lt;", "<")
                     .replace("&gt;", ">").strip())

    length = find(r'"lengthSeconds":"(\d+)"')

    channel = (find(r'"ownerChannelName":"([^"]+)"')
               or find(r'<meta itemprop="author" content="([^"]+)"')
               or find(r'"author":"([^"]+)"'))

    channel_id = find(r'"externalChannelId":"([^"]+)"')
    channel_url = (f"https://www.youtube.com/channel/{channel_id}"
                   if channel_id else "")

    thumb = (find(r'"thumbnail":\{"thumbnails":\[\{"url":"([^"]+)"')
             or find(r'<meta property="og:image" content="([^"]+)"')
             or f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg")

    description = find(r'"shortDescription":"((?:[^"\\]|\\.)*)"')
    if description and "\\u0026" in description:
        description = description.encode().decode("unicode_escape", errors="ignore")

    views = find(r'"viewCount":"(\d+)"')
    likes = find(r'"likeCount":"(\d+)"')

    # All video stream heights → only sane ones (≥ 144, ≤ 4320)
    raw_heights = re.findall(r'"height":(\d+)', html)
    seen, heights = set(), []
    for h in raw_heights:
        try:
            hv = int(h)
        except ValueError:
            continue
        if 144 <= hv <= 4320 and hv not in seen:
            seen.add(hv)
            heights.append(hv)
    heights.sort()

    # Try to grab a few common adaptive formats (info only, not used for download)
    formats = []
    for m in re.finditer(
        r'"url":"(https://[^"]+googlevideo\.com[^"]+)",'          # url
        r'.*?"qualityLabel":"([^"]+)",'                            # quality
        r'.*?"height":(\d+),'                                      # height
        r'.*?"width":(\d+),'                                       # width
        r'.*?"mimeType":"([^"]+)"',                                # mime
        html,
    ):
        formats.append({
            "url":     m.group(1),
            "format":  m.group(2),
            "height":  int(m.group(3)),
            "width":   int(m.group(4)),
            "ext":     m.group(5).split(";")[0].split("/")[-1],
        })

    return {
        "id":         vid,
        "title":      title,
        "uploader":   channel,
        "channel":    channel,
        "channel_id": channel_id,
        "channel_url": channel_url,
        "duration":   int(length) if length else None,
        "thumbnail":  thumb,
        "description": description,
        "view_count": int(views) if views else None,
        "like_count": int(likes) if likes else None,
        "heights":    heights,
        "formats":    formats,
        "webpage_url": watch_url,
    }


def get_info(url, cookies_file=None):
    """Video metadata — page scrape first, oEmbed, yt-dlp last."""
    # 1. Direct page scrape (works on cloud IPs, no bot check)
    try:
        return _scrape_info(url, cookies_file=cookies_file)
    except Exception as e:
        log.warning("page scrape failed: %s — trying oEmbed", e)

    # 2. oEmbed (public, no auth) — gives title/thumb/channel
    try:
        vid = _extract_video_id(url)
        if vid:
            r = requests.get(
                "https://www.youtube.com/oembed",
                params={"url": f"https://www.youtube.com/watch?v={vid}",
                        "format": "json"},
                timeout=10,
            )
            if r.status_code == 200:
                d = r.json()
                return {
                    "id":         vid,
                    "title":      d.get("title"),
                    "uploader":   d.get("author_name"),
                    "channel":    d.get("author_name"),
                    "thumbnail":  d.get("thumbnail_url"),
                    "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                    "heights":    [2160, 1440, 1080, 720, 480, 360, 240, 144],
                    "formats":    [],
                    "duration":   None,
                }
    except Exception as e:
        log.warning("oembed failed: %s", e)

    # 3. yt-dlp (last resort, may be bot-blocked on cloud IPs)
    last_err = None
    for clients in CLIENT_CHAIN:
        try:
            opts = _base_opts(cookies_file=cookies_file, players=clients)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # yt-dlp format → our shape
                info["heights"] = sorted({
                    f.get("height") for f in (info.get("formats") or [])
                    if f.get("height") and f.get("vcodec") not in (None, "none")
                })
                return info
        except Exception as e:
            last_err = e
            log.warning("info clients=%s failed: %s", clients, e)
    raise RuntimeError(f"info extraction failed: {last_err}")


def download_video(url, height=1080, cookies_file=None):
    """Try Cobalt first, then yt-dlp with cookies.  Returns (path, name, mime)."""
    tmpdir = tempfile.mkdtemp(prefix="ytdl_")

    # ── 1. Cobalt ─────────────────────────────────────
    try:
        data = cobalt_video(url, height=height)
        status = data.get("status")
        media  = data.get("url")
        if status in ("redirect", "tunnel", "stream") and media:
            suggested = data.get("filename") or "video.mp4"
            ext  = suggested.rsplit(".", 1)[-1] if "." in suggested else "mp4"
            stem = suggested.rsplit(".", 1)[0] if "." in suggested else "video"
            fname = safe_filename(stem, "video", ext)
            dest  = os.path.join(tmpdir, fname)
            _stream_to_file(media, dest)
            mime = "video/mp4" if ext == "mp4" else f"video/{ext}"
            return dest, fname, mime
    except Exception as e:
        log.warning("cobalt video failed -> yt-dlp fallback: %s", e)

    # ── 2. yt-dlp with cookies ────────────────────────
    out_path = os.path.join(tmpdir, "video.%(ext)s")
    mp4_path = os.path.join(tmpdir, "video.mp4")
    _ytdlp_video(url, height, out_path, cookies_file)
    if not os.path.exists(mp4_path):
        for f in os.listdir(tmpdir):
            if f.startswith("video."):
                return os.path.join(tmpdir, f), f, "video/mp4"
        raise RuntimeError("yt-dlp produced no output file")
    return mp4_path, "video.mp4", "video/mp4"


def download_audio(url, cookies_file=None, bitrate="64"):
    """Try Cobalt first, then yt-dlp with cookies.  Returns (path, name, mime)."""
    tmpdir = tempfile.mkdtemp(prefix="ytdla_")

    # ── 1. Cobalt ─────────────────────────────────────
    try:
        data = cobalt_audio(url, fmt="mp3", bitrate=bitrate)
        status = data.get("status")
        media  = data.get("url")
        if status in ("redirect", "tunnel", "stream") and media:
            suggested = data.get("filename") or "audio.mp3"
            ext  = suggested.rsplit(".", 1)[-1] if "." in suggested else "mp3"
            stem = suggested.rsplit(".", 1)[0] if "." in suggested else "audio"
            fname = safe_filename(stem, "audio", ext)
            dest  = os.path.join(tmpdir, fname)
            _stream_to_file(media, dest)
            return dest, fname, "audio/mpeg"
    except Exception as e:
        log.warning("cobalt audio failed -> yt-dlp fallback: %s", e)

    # ── 2. yt-dlp with cookies ────────────────────────
    mp3_path = os.path.join(tmpdir, "audio.mp3")
    _ytdlp_audio(url, mp3_path, cookies_file)
    if not os.path.exists(mp3_path):
        for f in os.listdir(tmpdir):
            if f.endswith(".mp3"):
                return os.path.join(tmpdir, f), f, "audio/mpeg"
        raise RuntimeError("yt-dlp produced no MP3 file")
    return mp3_path, "audio.mp3", "audio/mpeg"
