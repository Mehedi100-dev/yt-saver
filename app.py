#!/usr/bin/env python3
"""
YT Saver — small YouTube download engine.

Endpoints
---------
GET /                      health check (JSON)
GET /api/info?url=<yt>     title / thumbnail / duration + grouped format options
GET /api/file?url=<yt>&kind=video&height=720
GET /api/file?url=<yt>&kind=audio[&fmt=mp3]

- Progressive (combined) formats are proxied straight from googlevideo with a
  proper Content-Disposition so a tap in the browser (incl. iPhone Safari)
  starts a real download with the video title as the filename.
- DASH qualities (usually 1080p and above) are downloaded as video+audio with
  yt-dlp, muxed into ONE mp4 with ffmpeg (-c copy, NO re-encode, +faststart),
  cached briefly, then streamed as an attachment.

Run:  PORT=8080 python3 app.py
Deps: flask, yt-dlp  (+ ffmpeg binary on PATH for merges / mp3)
"""
import glob
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, request, send_file

import yt_dlp

app = Flask(__name__)

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
WORK_DIR = os.environ.get("YT_SAVER_WORK", "/tmp/yt-saver-work")
CACHE_DIR = os.path.join(WORK_DIR, "cache")
CACHE_TTL_SEC = 30 * 60  # merged files are kept at most this long
MERGE_SLOTS = threading.Semaphore(2)  # at most 2 merges at once
os.makedirs(CACHE_DIR, exist_ok=True)

YDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 20,
    "retries": 3,
}

QUALITY_LABELS = {2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def size_text(n):
    if not n:
        return None
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit in ("B", "KB") else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return None


def fmt_size(f):
    return f.get("filesize") or f.get("filesize_approx")


def est_size(f, duration):
    """filesize when known, else estimate from bitrate x duration.
    (Some player clients return no filesize fields at all.)"""
    s = fmt_size(f)
    if s:
        return s
    tbr = f.get("tbr") or f.get("abr")
    if tbr and duration:
        return int(float(tbr) * 1000 / 8 * duration)
    return None


def video_id_of(url):
    if not url:
        return None
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url.strip()):
        return url.strip()
    return None


def safe_filename(title, ext):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title or "video").strip()
    name = re.sub(r"\s+", " ", name)[:80].strip() or "video"
    return "%s.%s" % (name, ext)


# YouTube intermittently throws a "Sign in to confirm you're not a bot" wall
# at datacenter IPs on the default web client. Other player clients often
# still work, so extraction walks a chain and remembers the last winner.
CLIENT_CHAIN = [None, ["tv_embedded"], ["tv"], ["android"]]
_last_client = {"idx": 0}
_client_lock = threading.Lock()


def _ydl_opts(client, **kw):
    opts = dict(YDL_BASE)
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": client}}
    opts.update(kw)
    return opts


def _max_height(info):
    hs = [f.get("height") or 0 for f in info.get("formats") or []
          if f.get("vcodec") not in (None, "none")]
    return max(hs) if hs else 0


def extract(url):
    """Returns (info, client) — client is the player_client list that worked
    (None = default web client), so downloads can reuse the same route.
    A rich result (>=1080p available) is accepted at once; poorer results
    (some clients only expose a 360p set behind the bot wall) are kept as a
    fallback while the rest of the chain is tried."""
    with _client_lock:
        start = _last_client["idx"]
    order = list(range(len(CLIENT_CHAIN)))
    order = order[start:] + order[:start]
    best = None  # (score, info, client)
    last_err = None
    for attempt in range(2):
        for i in order:
            client = CLIENT_CHAIN[i]
            try:
                with yt_dlp.YoutubeDL(_ydl_opts(client)) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info and info.get("formats"):
                    score = _max_height(info)
                    if score >= 1080:
                        with _client_lock:
                            _last_client["idx"] = i
                        return info, client
                    if best is None or score > best[0]:
                        best = (score, info, client)
                else:
                    last_err = RuntimeError("no formats returned")
            except Exception as e:  # noqa: BLE001
                last_err = e
        if attempt == 0:
            time.sleep(4)
    if best:
        return best[1], best[2]
    raise last_err if last_err else RuntimeError("extraction failed")


def is_h264_mp4(f):
    return (f.get("ext") == "mp4") and str(f.get("vcodec") or "").startswith("avc1")


# --------------------------------------------------------------------------
# /api/info
# --------------------------------------------------------------------------
def build_info(info):
    formats = info.get("formats") or []
    duration = info.get("duration")
    heights = {}
    for f in formats:
        h = f.get("height")
        if not h or not f.get("vcodec") or f.get("vcodec") == "none":
            continue
        heights.setdefault(h, []).append(f)

    best_audio = None  # best audio-only format (for merged size estimates)
    audio_only = [f for f in formats if f.get("acodec") not in (None, "none")
                  and f.get("vcodec") in (None, "none")]
    if audio_only:
        best_audio = max(audio_only, key=lambda f: (f.get("abr") or 0))

    videos = []
    for h in sorted(heights, reverse=True):
        fs = heights[h]
        progressive = [f for f in fs if f.get("acodec") not in (None, "none")]
        video_only = [f for f in fs if f.get("acodec") in (None, "none")]
        has_mp4_h264 = any(is_h264_mp4(f) for f in fs)
        label = "%dp" % h
        if h in QUALITY_LABELS:
            label += " · " + QUALITY_LABELS[h]
        if progressive:
            best = max(progressive, key=lambda f: (est_size(f, duration) or 0))
            videos.append({
                "height": h, "label": label, "direct": True, "merge": False,
                "size": est_size(best, duration),
                "sizeText": size_text(est_size(best, duration)),
                "ext": best.get("ext"), "iphoneOk": h <= 1080 and is_h264_mp4(best),
            })
        elif video_only:
            best = max(video_only, key=lambda f: ((f.get("tbr") or 0)))
            v_est = est_size(best, duration)
            a_est = est_size(best_audio, duration) if best_audio else None
            est = (v_est + a_est) if (v_est and a_est) else v_est
            videos.append({
                "height": h, "label": label, "direct": False, "merge": True,
                "size": est, "sizeText": size_text(est),
                "ext": "mp4", "iphoneOk": h <= 1080 and has_mp4_h264,
            })

    audios = []
    if best_audio:
        audios.append({
            "label": "সেরা অডিও (%s)" % (best_audio.get("ext") or "m4a").upper(),
            "fmt": best_audio.get("ext") or "m4a",
            "abr": best_audio.get("abr"),
            "size": est_size(best_audio, duration),
            "sizeText": size_text(est_size(best_audio, duration)),
        })
        # a couple of clearly-lower bitrate steps, if they exist
        prev_abr = best_audio.get("abr") or 0
        for f in sorted(audio_only, key=lambda x: (x.get("abr") or 0), reverse=True):
            abr = f.get("abr") or 0
            if abr and abr <= prev_abr * 0.8 and len(audios) < 3:
                prev_abr = abr
                audios.append({
                    "label": "অডিও %dkbps (%s)" % (round(abr), (f.get("ext") or "").upper()),
                    "fmt": f.get("ext") or "m4a", "formatId": f.get("format_id"),
                    "abr": abr, "size": est_size(f, duration),
                    "sizeText": size_text(est_size(f, duration)),
                })
        audios.append({"label": "MP3 অডিও", "fmt": "mp3", "abr": best_audio.get("abr"),
                       "size": None, "sizeText": None})

    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "thumbnail": thumb,
        "duration": info.get("duration"),
        "videos": videos,
        "audios": audios,
    }


@app.route("/api/info")
def api_info():
    url = request.args.get("url", "")
    if not video_id_of(url):
        return jsonify({"error": "সঠিক YouTube লিংক দাও"}), 400
    try:
        info, _client = extract(url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "YouTube থেকে তথ্য আনা যায়নি: " + str(e)[:180]}), 502
    return jsonify(build_info(info))


# --------------------------------------------------------------------------
# /api/file — direct (progressive) : proxy stream with attachment header
# --------------------------------------------------------------------------
def proxy_stream(fmt, filename):
    src = fmt["url"]
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
    try:
        upstream = urllib.request.urlopen(req, timeout=30)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "ডাউনলোড শুরু করা যায়নি: " + str(e)[:150]}), 502

    def gen():
        try:
            while True:
                chunk = upstream.read(262144)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Disposition": 'attachment; filename*=UTF-8\'\'%s' % urllib.parse.quote(filename),
    }
    length = fmt.get("filesize") or upstream.headers.get("Content-Length")
    if length:
        headers["Content-Length"] = str(length)
    ctype = "video/mp4" if fmt.get("ext") == "mp4" else "application/octet-stream"
    if (fmt.get("acodec") not in (None, "none")) and fmt.get("vcodec") in (None, "none"):
        ctype = "audio/mp4" if fmt.get("ext") == "m4a" else "application/octet-stream"
    return Response(gen(), headers=headers, content_type=ctype)


# --------------------------------------------------------------------------
# /api/file — merge path (DASH video, or mp3 conversion)
# --------------------------------------------------------------------------
def find_cached(key):
    hits = glob.glob(os.path.join(CACHE_DIR, key + ".*"))
    for path in hits:
        if os.path.getsize(path) > 0 and time.time() - os.path.getmtime(path) < CACHE_TTL_SEC:
            return path
    return None


def janitor():
    while True:
        time.sleep(300)
        now = time.time()
        for path in glob.glob(os.path.join(CACHE_DIR, "*")):
            try:
                if now - os.path.getmtime(path) > CACHE_TTL_SEC:
                    os.remove(path)
            except OSError:
                pass


threading.Thread(target=janitor, daemon=True).start()


def _client_order(start_client):
    idx = 0
    for i, c in enumerate(CLIENT_CHAIN):
        if c == start_client:
            idx = i
            break
    n = len(CLIENT_CHAIN)
    return [CLIENT_CHAIN[(idx + k) % n] for k in range(n)]


def merged_video(url, vid, height, client=None):
    """Download best video<=height + best audio, mux to one mp4. Returns path.
    Walks the player-client chain on failure — YouTube's bot wall flickers
    per client, and a download re-extracts on its own."""
    key = "%s_%dp" % (vid, height)
    cached = find_cached(key)
    if cached:
        return cached
    # Exact-height pair first — H.264 (avc1) preferred so <=1080p results
    # stay iPhone-playable — then any pair at/below the height. Never a
    # bare progressive fallback: a client that cannot see this quality must
    # fail the attempt so the walk moves on, instead of silently delivering
    # a much lower quality than the one that was asked for.
    fmt = ("bv*[height=%d][vcodec^=avc1]+ba[ext=m4a]/"
           "bv*[height=%d][ext=mp4]+ba[ext=m4a]/"
           "bv*[height=%d]+ba/"
           "bv*[height<=%d]+ba") % (height, height, height, height)
    last_err = None
    with MERGE_SLOTS:
        cached = find_cached(key)
        if cached:
            return cached
        for dl_client in _client_order(client):
            job = os.path.join(WORK_DIR, "job_%s_%d" % (vid, int(time.time())))
            os.makedirs(job, exist_ok=True)
            out = os.path.join(job, "out.mp4")
            try:
                with yt_dlp.YoutubeDL(_ydl_opts(
                        dl_client, format=fmt,
                        outtmpl=os.path.join(job, "dl.%(ext)s"),
                        merge_output_format="mp4")) as ydl:
                    ydl.download([url])
                srcs = [p for p in glob.glob(os.path.join(job, "dl.*"))
                        if os.path.getsize(p) > 0]
                if not srcs:
                    raise RuntimeError("download produced no file")
                src = max(srcs, key=os.path.getsize)
                # Normalise through ffmpeg: -c copy (no re-encode) + faststart.
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", src, "-c", "copy",
                     "-movflags", "+faststart", out],
                    capture_output=True, timeout=1200)
                final = out if (proc.returncode == 0 and os.path.exists(out)
                                and os.path.getsize(out) > 0) else src
                dest = os.path.join(CACHE_DIR, key + ".mp4")
                shutil.copyfile(final, dest)
                return dest
            except Exception as e:  # noqa: BLE001
                last_err = e
            finally:
                shutil.rmtree(job, ignore_errors=True)
    raise last_err if last_err else RuntimeError("merge failed")


def mp3_file(url, vid, client=None):
    key = "%s_mp3" % vid
    cached = find_cached(key)
    if cached:
        return cached
    last_err = None
    with MERGE_SLOTS:
        cached = find_cached(key)
        if cached:
            return cached
        for dl_client in _client_order(client):
            job = os.path.join(WORK_DIR, "jobmp3_%s_%d" % (vid, int(time.time())))
            os.makedirs(job, exist_ok=True)
            try:
                with yt_dlp.YoutubeDL(_ydl_opts(
                        dl_client, format="ba/bestaudio",
                        outtmpl=os.path.join(job, "a.%(ext)s"),
                        postprocessors=[{"key": "FFmpegExtractAudio",
                                         "preferredcodec": "mp3",
                                         "preferredquality": "192"}])) as ydl:
                    ydl.download([url])
                srcs = glob.glob(os.path.join(job, "a.mp3"))
                if not srcs:
                    raise RuntimeError("mp3 conversion produced no file")
                dest = os.path.join(CACHE_DIR, key + ".mp3")
                shutil.copyfile(srcs[0], dest)
                return dest
            except Exception as e:  # noqa: BLE001
                last_err = e
            finally:
                shutil.rmtree(job, ignore_errors=True)
    raise last_err if last_err else RuntimeError("mp3 failed")


@app.route("/api/file")
def api_file():
    url = request.args.get("url", "")
    vid = video_id_of(url)
    if not vid:
        return jsonify({"error": "সঠিক YouTube লিংক দাও"}), 400
    kind = request.args.get("kind", "video")
    try:
        info, client = extract(url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "YouTube থেকে তথ্য আনা যায়নি: " + str(e)[:180]}), 502
    title = info.get("title") or "video"
    formats = info.get("formats") or []

    if kind == "audio":
        want_fmt = request.args.get("fmt", "")
        if want_fmt == "mp3":
            try:
                path = mp3_file(url, vid, client)
            except Exception as e:  # noqa: BLE001
                return jsonify({"error": "অডিও তৈরি করা যায়নি: " + str(e)[:180]}), 502
            return send_file(path, as_attachment=True,
                             download_name=safe_filename(title, "mp3"))
        fid = request.args.get("formatId")
        audio_only = [f for f in formats if f.get("acodec") not in (None, "none")
                      and f.get("vcodec") in (None, "none") and f.get("url")]
        if fid:
            audio_only = [f for f in audio_only if f.get("format_id") == fid] or audio_only
        if not audio_only:
            return jsonify({"error": "অডিও ফরম্যাট পাওয়া যায়নি"}), 404
        best = max(audio_only, key=lambda f: (f.get("abr") or 0))
        return proxy_stream(best, safe_filename(title, best.get("ext") or "m4a"))

    # ---- video ----
    try:
        height = int(request.args.get("height", "720"))
    except ValueError:
        height = 720
    at_h = [f for f in formats if f.get("height") == height and f.get("url")]
    progressive = [f for f in at_h if f.get("vcodec") not in (None, "none")
                   and f.get("acodec") not in (None, "none")]
    if progressive:
        best = max(progressive, key=lambda f: ((fmt_size(f) or 0)))
        return proxy_stream(best, safe_filename(title, best.get("ext") or "mp4"))
    try:
        path = merged_video(url, vid, height, client)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "ভিডিও তৈরি করা যায়নি: " + str(e)[:180]}), 502
    return send_file(path, as_attachment=True,
                     download_name=safe_filename(title, "mp4"))


@app.route("/")
def health():
    return jsonify({"ok": True, "service": "yt-saver",
                    "endpoints": ["/api/info?url=", "/api/file?url=&kind=video&height="]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
