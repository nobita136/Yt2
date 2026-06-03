import os
import re
import uuid
import shutil
import logging
import threading
import yt_dlp
from flask import Flask, request, jsonify, render_template, send_file, after_this_request
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ytdl")

app = Flask(__name__)
CORS(app)

# ── PATHS ────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
TMP_DIR     = "/tmp/ytdl"
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")
os.makedirs(TMP_DIR, exist_ok=True)


def _which(names):
    """Return the first existing path from a list of candidate names/paths."""
    for n in names:
        if not n:
            continue
        if os.path.isabs(n) and os.path.exists(n):
            return n
        hit = shutil.which(n)
        if hit:
            return hit
    return None


FFMPEG = _which([
    os.environ.get("FFMPEG_LOCATION"),
    os.environ.get("FFMPEG_PATH"),
    "/nix/store/b11ycf80cxi2iyrga8rkq1wzdinmax18-replit-runtime-path/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "ffmpeg",
]) or "ffmpeg"

NODE = _which([
    os.environ.get("NODE_BINARY"),
    "/nix/store/1lagpgadaybvs1n2312gysg2phjk89y8-nodejs-20.20.0-wrapped/bin/node",
    "/usr/bin/node",
    "/usr/local/bin/node",
    "node",
    "nodejs",
])

HAS_COOKIES = os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0

log.info("ffmpeg = %s", FFMPEG)
log.info("node   = %s", NODE)
log.info("cookies= %s (%d bytes)", COOKIE_FILE, os.path.getsize(COOKIE_FILE) if HAS_COOKIES else 0)

# ── YT-DLP BASE OPTIONS ──────────────────────────────────────
def base_opts(download=False):
    opts = {
        "quiet":            True,
        "no_warnings":      True,
        "ignoreerrors":     False,
        "retries":          3,
        "fragment_retries": 3,
        "socket_timeout":   30,
        "concurrent_fragment_downloads": 4,
        "noplaylist":       True,
        "skip_download":    not download,
        "ffmpeg_location":  FFMPEG,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest":  "document",
            "Sec-Fetch-Mode":  "navigate",
            "Sec-Fetch-Site":  "none",
        },
    }
    # node is REQUIRED to solve YouTube signature/n-challenge on 2026
    if NODE:
        opts["js_runtimes"] = {"node": {"path": NODE}}
        # remote EJS scripts are needed for current n-sig challenges
        opts["remote_components"] = {"ejs": True}
    if HAS_COOKIES:
        opts["cookiefile"] = COOKIE_FILE
    return opts


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


def extract_info(url):
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        return ydl.extract_info(url, download=False)


def format_duration(sec):
    if not sec:
        return "N/A"
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_title(info, fallback="file"):
    t = (info.get("title") or fallback) if isinstance(info, dict) else fallback
    cleaned = "".join(c for c in t if c.isalnum() or c in " _-").strip()
    return cleaned[:80] or fallback


def serve_and_clean(path, name, mime):
    def _del(p):
        try: os.remove(p)
        except OSError: pass

    @after_this_request
    def _after(resp):
        threading.Thread(target=_del, args=(path,), daemon=True).start()
        return resp

    return send_file(path, as_attachment=True, download_name=name, mimetype=mime)


def cleanup_prefix(prefix):
    try:
        for f in os.listdir(TMP_DIR):
            if f.startswith(prefix):
                try: os.remove(os.path.join(TMP_DIR, f))
                except OSError: pass
    except OSError:
        pass


# Heights the user asked for (and higher) – mp4-only
TARGET_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
QUALITY_LABELS = {
    2160: "4K UHD", 1440: "2K QHD", 1080: "Full HD",
    720: "HD", 480: "SD", 360: "Low", 240: "Mobile", 144: "Tiny",
}


def available_heights(info):
    """Heights that exist as MP4 streams (H.264 preferred)."""
    seen = set()
    for f in info.get("formats", []) or []:
        h  = f.get("height")
        vc = f.get("vcodec") or "none"
        ext = f.get("ext")
        if h and vc not in (None, "none") and ext == "mp4":
            seen.add(h)
    out = []
    for t in TARGET_HEIGHTS:
        if any(abs(h - t) <= 10 for h in seen):
            out.append(t)
    return out


def video_format_string(target_h):
    """Pick best MP4-with-audio at <= target_h. H.264 first, fall back gracefully."""
    return (
        f"bestvideo[height<={target_h}][ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={target_h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={target_h}]+bestaudio"
        f"/best[height<={target_h}]"
        f"/best"
    )


# ══════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status":      "ok",
        "ffmpeg":      FFMPEG,
        "ffmpeg_ok":   os.path.exists(FFMPEG) if os.path.isabs(FFMPEG) else True,
        "node":        NODE,
        "node_ok":     bool(NODE),
        "cookies":     HAS_COOKIES,
        "yt_dlp":      yt_dlp.version.__version__,
    })


# ── SEARCH ─────────────────────────────────────────────
@app.route("/search")
def search():
    q     = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 12)), 20)
    if not q:
        return jsonify({"error": "Query is required"}), 400
    try:
        opts = base_opts()
        opts["extract_flat"] = True
        opts["playlistend"]  = limit
        with yt_dlp.YoutubeDL(opts) as ydl:
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
        info = extract_info(normalize_url(raw))
        return jsonify({
            "status":    "ok",
            "title":     info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration":  format_duration(info.get("duration")),
            "channel":   info.get("uploader"),
        })
    except Exception as ex:
        return jsonify({"status": "error", "error": str(ex)}), 500


# ── AUDIO DOWNLOAD (MP3, low quality) ───────────────────
@app.route("/dl/audio")
def audio_download():
    raw = (request.args.get("url") or "").strip()
    if not raw:
        return jsonify({"status": "error", "error": "url required"}), 400
    url = normalize_url(raw)
    sid = uuid.uuid4().hex
    tpl = os.path.join(TMP_DIR, f"{sid}.%(ext)s")
    mp3 = os.path.join(TMP_DIR, f"{sid}.mp3")
    try:
        opts = base_opts(download=True)
        opts.update({
            # Lowest-bitrate audio-only stream → tiny MP3
            "format":          "worstaudio/worst",
            "outtmpl":         tpl,
            "postprocessors":  [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "64",   # 64 kbps – "low quality"
            }],
            "postprocessor_args": ["-vn"],
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if not os.path.exists(mp3):
            files = [f for f in os.listdir(TMP_DIR) if f.startswith(sid)]
            if files:
                mp3 = os.path.join(TMP_DIR, files[0])
        if not os.path.exists(mp3):
            return jsonify({"status": "error", "error": "MP3 file not produced"}), 500

        return serve_and_clean(mp3, f"{safe_title(info, 'audio')}.mp3", "audio/mpeg")
    except Exception as ex:
        cleanup_prefix(sid)
        log.exception("audio_download failed")
        return jsonify({"status": "error", "error": str(ex)}), 500


# ── VIDEO INFO (JSON, lists available MP4 qualities) ───
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
        # allow "best" / numeric
        if height.isdigit():
            target_h = int(height)
        elif height.lower() == "best":
            target_h = 2160
        else:
            target_h = 1080

        sid = uuid.uuid4().hex
        tpl = os.path.join(TMP_DIR, f"{sid}.%(ext)s")
        mp4 = os.path.join(TMP_DIR, f"{sid}.mp4")
        try:
            opts = base_opts(download=True)
            opts.update({
                "format":             video_format_string(target_h),
                "outtmpl":            tpl,
                "merge_output_format": "mp4",
                "postprocessor_args": {
                    "ffmpeg_o": ["-movflags", "+faststart"],
                },
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

            if not os.path.exists(mp4):
                files = [f for f in os.listdir(TMP_DIR) if f.startswith(sid) and f.endswith(".mp4")]
                if files:
                    mp4 = os.path.join(TMP_DIR, files[0])
            if not os.path.exists(mp4):
                return jsonify({"status": "error",
                                 "error": "Merged MP4 not produced"}), 500

            actual_h = target_h
            for f in info.get("formats", []) or []:
                if f.get("format_id") and (f.get("height") or 0) <= target_h and f.get("height"):
                    actual_h = f["height"]
            return serve_and_clean(
                mp4,
                f"{safe_title(info, 'video')}_{actual_h}p.mp4",
                "video/mp4",
            )
        except Exception as ex:
            cleanup_prefix(sid)
            log.exception("video_download failed")
            return jsonify({"status": "error", "error": str(ex)}), 500

    # ── INFO MODE ──────────────────────────────────────
    try:
        info    = extract_info(url)
        heights = available_heights(info)
        return jsonify({
            "status":    "ok",
            "title":     info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration":  format_duration(info.get("duration")),
            "channel":   info.get("uploader"),
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
