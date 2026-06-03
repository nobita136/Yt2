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

# Player-client list.  yt-dlp defaults to a set that includes the "tv"
# client which exposes the full height table (144/240/360/480/720/1080/...)
# but only as separate video+audio streams.  "web" returns a few progressive
# MP4 streams.  We try the most-capable clients first and fall back through
# the rest.  "default" tells yt-dlp to use its built-in priority, which is
# usually what works on Replit/Replit-like IPs.
CLIENT_CHAIN = [
    None,                # yt-dlp default (best on Replit)
    ["tv"],              # TV HTML5 – full set, but no progressive MP4
    ["web", "mweb"],     # WEB clients – progressive MP4
    ["ios"],             # iOS – limited
    ["android"],         # ANDROID – limited
]


# ── YT-DLP BASE OPTIONS ──────────────────────────────────────
def base_opts(download=False, player_clients=None):
    opts = {
        "quiet":            True,
        "no_warnings":      True,
        "ignoreerrors":     False,
        "retries":          5,
        "fragment_retries": 5,
        "socket_timeout":   30,
        "concurrent_fragment_downloads": 4,
        "noplaylist":       True,
        "skip_download":    not download,
        "ffmpeg_location":  FFMPEG,
        # Always merge any video+audio pair into an MP4 container.
        "merge_output_format": "mp4",
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
    if player_clients:
        opts["extractor_args"] = {"youtube": {"player_client": player_clients}}
    if NODE:
        opts["js_runtimes"] = {"node": {"path": NODE}}
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
    """Extract video info.  Tries several player client sets so the request
    succeeds whether the server is on a Replit IP, a Render IP, with or
    without cookies."""
    last = None
    for clients in CLIENT_CHAIN:
        try:
            opts = base_opts(player_clients=clients)
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last = e
            log.warning("extract_info with clients=%s failed: %s", clients, e)
    raise last or RuntimeError("extract_info failed for all player clients")


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
    """Heights that exist as video streams (any modern codec).  We use the
    presence of a video stream as a hint that we can produce a merged MP4 at
    that height, even if the original stream is webm/av1 – ffmpeg will
    transcode / remux as needed."""
    seen = set()
    for f in info.get("formats", []) or []:
        h    = f.get("height")
        vc   = f.get("vcodec") or "none"
        ac   = f.get("acodec") or "none"
        if h and vc not in (None, "none"):
            # Any video stream (progressive OR video-only) means the height
            # is achievable.  ffmpeg will merge with audio automatically.
            seen.add(h)
    out = []
    for t in TARGET_HEIGHTS:
        if any(abs(h - t) <= 10 for h in seen):
            out.append(t)
    return out


def video_format_string(target_h):
    """Format selector that always succeeds and always ends up merged to MP4.

    Order of preference:
      1. H.264 MP4 video + M4A audio (broad player support)
      2. Any MP4 video + M4A audio
      3. Any video + any audio   → ffmpeg merges to MP4
      4. Single progressive MP4
      5. Anything → ffmpeg remuxes to MP4
    """
    return (
        f"bestvideo[height<={target_h}][ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={target_h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={target_h}]+bestaudio"
        f"/best[height<={target_h}][ext=mp4]"
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

    client_sets = CLIENT_CHAIN
    last_error  = None
    for clients in client_sets:
        try:
            opts = base_opts(download=True, player_clients=clients)
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
                files = [f for f in os.listdir(TMP_DIR)
                         if f.startswith(sid) and f.endswith(".mp3")]
                if files:
                    mp3 = os.path.join(TMP_DIR, files[0])
            if os.path.exists(mp3):
                return serve_and_clean(mp3,
                                        f"{safe_title(info, 'audio')}.mp3",
                                        "audio/mpeg")
            last_error = RuntimeError("MP3 file not produced")
        except Exception as ex:
            last_error = ex
            cleanup_prefix(sid)
            log.warning("audio attempt clients=%s failed: %s", clients, ex)
            continue

    cleanup_prefix(sid)
    log.exception("audio_download failed")
    return jsonify({"status": "error",
                    "error": str(last_error) if last_error else "audio failed"}), 500


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
        if height.isdigit():
            target_h = int(height)
        elif height.lower() == "best":
            target_h = 2160
        else:
            target_h = 1080

        sid = uuid.uuid4().hex
        tpl = os.path.join(TMP_DIR, f"{sid}.%(ext)s")
        mp4 = os.path.join(TMP_DIR, f"{sid}.mp4")

        client_sets = CLIENT_CHAIN
        last_error  = None
        info        = None
        for clients in client_sets:
            try:
                opts = base_opts(download=True, player_clients=clients)
                opts.update({
                    "format":             video_format_string(target_h),
                    "outtmpl":            tpl,
                    "merge_output_format": "mp4",
                    # Re-encode audio to AAC so the output MP4 plays on every
                    # device (YouTube often serves eac3/opus in the source).
                    "postprocessor_args": {
                        "default": [
                            "-movflags", "+faststart",
                            "-c:v", "copy",
                            "-c:a", "aac", "-b:a", "192k",
                        ],
                    },
                })
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)

                if not os.path.exists(mp4):
                    files = [f for f in os.listdir(TMP_DIR)
                             if f.startswith(sid) and f.endswith(".mp4")]
                    if files:
                        mp4 = os.path.join(TMP_DIR, files[0])
                if os.path.exists(mp4):
                    break
                last_error = RuntimeError("Merged MP4 not produced")
            except Exception as ex:
                last_error = ex
                cleanup_prefix(sid)
                log.warning("video attempt clients=%s failed: %s", clients, ex)
                continue

        if not os.path.exists(mp4):
            cleanup_prefix(sid)
            return jsonify({
                "status": "error",
                "error":  str(last_error) if last_error else "video failed",
            }), 500

        # Detect actual height for filename.  We pick the highest video
        # stream that is <= target_h, falling back to the lowest available.
        actual_h = None
        if info:
            for f in info.get("formats", []) or []:
                h = f.get("height") or 0
                vc = f.get("vcodec") or "none"
                if h and vc not in (None, "none") and h <= target_h:
                    if actual_h is None or h > actual_h:
                        actual_h = h
        if actual_h is None:
            actual_h = target_h

        return serve_and_clean(
            mp4,
            f"{safe_title(info or {}, 'video')}_{actual_h}p.mp4",
            "video/mp4",
        )

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
