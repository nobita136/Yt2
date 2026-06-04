import os
import re
import logging
import threading
import shutil
from flask import Flask, request, jsonify, render_template, send_file, after_this_request
from flask_cors import CORS
import cobalt_client as dl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ytdl")

app = Flask(__name__)
CORS(app)

# ── PATHS ────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")
HAS_COOKIES = os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0

log.info("cookies  = %s (%d bytes)", COOKIE_FILE,
         os.path.getsize(COOKIE_FILE) if HAS_COOKIES else 0)
log.info("cobalt   = %d instances", len(dl.COBALT_INSTANCES))
log.info("yt-dlp   = v%s", dl.yt_dlp.version.__version__)

# ── HELPERS ──────────────────────────────────────────────────
URL_RE = re.compile(r"^https?://", re.I)


def normalize_url(link):
    link = (link or "").strip()
    if not link:
        return link
    if link.startswith("https:/") and not link.startswith("https://"):
        link = "https://" + link[7:]
    elif link.startswith("http:/") and not link.startswith("http://"):
        link = "http://" + link[6:]
    elif not URL_RE.match(link):
        link = "https://" + link
    return link


def format_duration(sec):
    if not sec:
        return "N/A"
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def serve_and_clean(path, name, mime):
    """Send the file, then delete its parent temp dir."""
    parent = os.path.dirname(os.path.abspath(path))

    def _cleanup(_p=parent):
        shutil.rmtree(_p, ignore_errors=True)

    @after_this_request
    def _after(resp):
        threading.Thread(target=_cleanup, daemon=True).start()
        return resp

    return send_file(path, as_attachment=True, download_name=name, mimetype=mime)


TARGET_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
QUALITY_LABELS = {
    2160: "4K UHD", 1440: "2K QHD", 1080: "Full HD",
    720: "HD", 480: "SD", 360: "Low", 240: "Mobile", 144: "Tiny",
}


def available_heights(info):
    # Prefer the pre-computed list from page scrape
    if "heights" in info and info["heights"]:
        seen = set(info["heights"])
    else:
        seen = set()
        for f in (info.get("formats") or []):
            h  = f.get("height")
            vc = f.get("vcodec") or "none"
            if h and vc not in (None, "none"):
                seen.add(h)
    out = []
    for t in TARGET_HEIGHTS:
        if any(abs(h - t) <= 10 for h in seen):
            out.append(t)
    return out


def actual_height(info, target_h):
    """Highest video stream height <= target_h (for filename)."""
    best = None
    for f in (info.get("formats") or []):
        h  = f.get("height") or 0
        vc = f.get("vcodec") or "none"
        if h and vc not in (None, "none") and h <= target_h:
            if best is None or h > best:
                best = h
    return best or target_h


# ══════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status":           "ok",
        "cookies":          HAS_COOKIES,
        "yt_dlp":           dl.yt_dlp.version.__version__,
        "cobalt_instances": len(dl.COBALT_INSTANCES),
        "ffmpeg":           dl.ffmpeg_path(),
    })


# ── SEARCH ─────────────────────────────────────────────
@app.route("/search")
def search():
    q     = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 12)), 20)
    if not q:
        return jsonify({"error": "Query is required"}), 400
    try:
        opts = {
            "quiet": True, "no_warnings": True, "ignoreerrors": False,
            "noplaylist": True, "skip_download": True,
            "extract_flat": True, "playlistend": limit,
            "http_headers": {"User-Agent": dl.DEFAULT_UA,
                             "Accept-Language": "en-US,en;q=0.9"},
        }
        if HAS_COOKIES:
            opts["cookiefile"] = COOKIE_FILE
        with dl.yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
        videos = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            vid   = e.get("id", "")
            tnls  = e.get("thumbnails") or []
            thumb = (tnls[-1]["url"] if tnls
                     else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
            videos.append({
                "id":        vid,
                "title":     e.get("title", ""),
                "thumbnail": thumb,
                "duration":  format_duration(e.get("duration")),
                "channel":   e.get("channel") or e.get("uploader") or "",
                "views":     e.get("view_count"),
                "url":       e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            })
        return jsonify({"results": videos})
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


# ── AUDIO INFO (JSON) ────────────────────────────────────
@app.route("/download/audio")
@app.route("/download/audio/<path:link>")
def audio_info(link=None):
    raw = request.args.get("url") or link or ""
    if not raw:
        return jsonify({"status": "error", "error": "url required"}), 400
    try:
        info = dl.get_info(normalize_url(raw),
                           cookies_file=COOKIE_FILE if HAS_COOKIES else None)
        return jsonify({
            "status":    "ok",
            "title":     info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration":  format_duration(info.get("duration")),
            "channel":   info.get("uploader") or info.get("channel"),
        })
    except Exception as ex:
        return jsonify({"status": "error", "error": str(ex)}), 500


# ── AUDIO DOWNLOAD (MP3, 64 kbps) ────────────────────────
@app.route("/dl/audio")
def audio_download():
    raw = (request.args.get("url") or "").strip()
    if not raw:
        return jsonify({"status": "error", "error": "url required"}), 400
    url = normalize_url(raw)
    try:
        path, name, mime = dl.download_audio(
            url, cookies_file=COOKIE_FILE if HAS_COOKIES else None
        )
        try:
            info = dl.get_info(url, cookies_file=COOKIE_FILE if HAS_COOKIES else None)
            title = info.get("title") or "audio"
        except Exception:
            title = "audio"
        final_name = f"{dl.safe_filename(title, 'audio')}.mp3"
        return serve_and_clean(path, final_name, "audio/mpeg")
    except Exception as ex:
        log.exception("audio_download failed")
        return jsonify({"status": "error", "error": str(ex)}), 500


# ── VIDEO INFO + DOWNLOAD ───────────────────────────────
@app.route("/download/video")
@app.route("/download/video/<path:link>")
def video(link=None):
    raw    = (request.args.get("url") or link or "").strip()
    height = (request.args.get("height") or "").strip()
    if not raw:
        return jsonify({"status": "error", "error": "url required"}), 400
    url = normalize_url(raw)

    # ── DOWNLOAD MODE ──────────────────────────────────
    if height:
        if height.isdigit():
            target_h = int(height)
        elif height.lower() == "best":
            target_h = 2160
        else:
            target_h = 1080

        # Try to detect actual height + title for filename
        try:
            info  = dl.get_info(url, cookies_file=COOKIE_FILE if HAS_COOKIES else None)
            h     = actual_height(info, target_h)
            title = info.get("title") or "video"
        except Exception:
            h, title = target_h, "video"

        try:
            path, name, mime = dl.download_video(
                url, height=target_h,
                cookies_file=COOKIE_FILE if HAS_COOKIES else None,
            )
            ext = os.path.splitext(name)[1] or ".mp4"
            final_name = f"{dl.safe_filename(title, 'video')}_{h}p{ext}"
            return serve_and_clean(path, final_name, mime)
        except Exception as ex:
            log.exception("video_download failed")
            return jsonify({"status": "error", "error": str(ex)}), 500

    # ── INFO MODE ──────────────────────────────────────
    try:
        info    = dl.get_info(url, cookies_file=COOKIE_FILE if HAS_COOKIES else None)
        heights = available_heights(info)
        return jsonify({
            "status":    "ok",
            "title":     info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration":  format_duration(info.get("duration")),
            "channel":   info.get("uploader") or info.get("channel"),
            "qualities": [
                {"height": h, "label": QUALITY_LABELS.get(h, f"{h}p")}
                for h in heights
            ],
        })
    except Exception as ex:
        return jsonify({"status": "error", "error": str(ex)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
