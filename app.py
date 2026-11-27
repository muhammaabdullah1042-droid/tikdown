import os
import re
import uuid
import shutil
import tempfile
from urllib.parse import urlparse

from flask import (
    Flask, abort, after_this_request, flash, redirect, render_template,
    request, send_file, url_for
)
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-before-deploying")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["DOWNLOAD_TIMEOUT"] = int(os.environ.get("DOWNLOAD_TIMEOUT", "180"))
app.config["MAX_DOWNLOAD_MB"] = int(os.environ.get("MAX_DOWNLOAD_MB", "150"))

# Short-lived in-memory cache. On a server restart, users simply analyze the URL again.
VIDEO_CACHE = {}
CACHE_LIMIT = 100


def valid_tiktok_url(value):
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        allowed = host == "tiktok.com" or host.endswith(".tiktok.com")
        return parsed.scheme in ("http", "https") and allowed
    except Exception:
        return False


def clean_text(value, limit=5000):
    if not value:
        return ""
    return str(value).strip()[:limit]


def human_size(size):
    if not size:
        return "Size unavailable"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "Size unavailable"


def ydl_base_options():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 1,
        "ignoreerrors": False,
    }


def get_video_info(url):
    opts = ydl_base_options()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def available_formats(info):
    result = []
    seen = set()
    for item in info.get("formats") or []:
        # Offer only single-file formats with both video and audio.
        # This avoids requiring FFmpeg to merge separate streams on Android.
        if item.get("vcodec") in (None, "none") or item.get("acodec") in (None, "none"):
            continue
        fmt_id = str(item.get("format_id", ""))
        if not fmt_id or fmt_id in seen:
            continue
        seen.add(fmt_id)
        height = item.get("height")
        ext = item.get("ext") or "video"
        label = f"{height}p" if height else (item.get("format_note") or "Available quality")
        result.append({
            "id": fmt_id,
            "label": label,
            "ext": ext,
            "size": human_size(item.get("filesize") or item.get("filesize_approx")),
            "height": height or 0,
            "note": clean_text(item.get("format_note"), 80),
        })
    result.sort(key=lambda x: (x["height"], x["ext"] == "mp4"), reverse=True)
    return result[:20]


def available_captions(info):
    tracks = []
    seen = set()
    for kind, collection in (
        ("Subtitles", info.get("subtitles") or {}),
        ("Auto captions", info.get("automatic_captions") or {}),
    ):
        for lang, entries in collection.items():
            if not entries:
                continue
            key = (kind, lang)
            if key in seen:
                continue
            seen.add(key)
            tracks.append({
                "kind": kind,
                "lang": lang,
                "name": clean_text(entries[0].get("name") or lang, 100),
            })
    return tracks[:40]


def find_cached(token):
    record = VIDEO_CACHE.get(token)
    if not record:
        abort(404)
    return record


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", result=None)


@app.route("/analyze", methods=["POST"])
def analyze():
    url = (request.form.get("url") or "").strip()
    if not valid_tiktok_url(url):
        flash("Please paste a valid TikTok video URL.", "error")
        return redirect(url_for("index"))

    try:
        info = get_video_info(url)
        if not info:
            raise ValueError("TikTok did not return video information.")
        formats = available_formats(info)
        captions = available_captions(info)

        if len(VIDEO_CACHE) >= CACHE_LIMIT:
            VIDEO_CACHE.clear()

        token = uuid.uuid4().hex
        VIDEO_CACHE[token] = {"url": url, "info": info, "formats": formats}

        result = {
            "token": token,
            "title": clean_text(info.get("title") or "TikTok video", 180),
            "description": clean_text(info.get("description"), 3000),
            "thumbnail": info.get("thumbnail"),
            "uploader": clean_text(info.get("uploader") or info.get("channel") or "TikTok creator", 120),
            "duration": info.get("duration"),
            "formats": formats,
            "captions": captions,
            "webpage_url": clean_text(info.get("webpage_url") or url, 500),
        }
        return render_template("index.html", result=result)
    except Exception as exc:
        app.logger.exception("TikTok analysis failed")
        flash("Couldn't read this video. It may be unavailable, unsupported, or temporarily blocked. Try another public link.", "error")
        return redirect(url_for("index"))


@app.route("/download/<token>/<format_id>")
def download_video(token, format_id):
    record = find_cached(token)
    selected = next((f for f in record["formats"] if f["id"] == format_id), None)
    if not selected:
        abort(400)

    temp_dir = tempfile.mkdtemp(prefix="tikdown_")
    output_template = os.path.join(temp_dir, "%(title).80s-%(id)s.%(ext)s")
    opts = ydl_base_options()
    opts.update({
        "format": format_id,
        "outtmpl": output_template,
        "max_filesize": app.config["MAX_DOWNLOAD_MB"] * 1024 * 1024,
        "noprogress": True,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([record["url"]])
        files_found = [
            os.path.join(temp_dir, name) for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
        ]
        if not files_found:
            raise RuntimeError("No downloadable file was produced.")
        path = max(files_found, key=os.path.getsize)
        response = send_file(
            path, as_attachment=True,
            download_name=os.path.basename(path),
            mimetype="application/octet-stream"
        )
        response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return response
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.exception("Video download failed")
        flash("The selected format could not be downloaded. Try another available quality.", "error")
        return redirect(url_for("index"))


@app.route("/captions/<token>", methods=["GET"])
def download_captions(token):
    record = find_cached(token)
    lang = (request.args.get("lang") or "").strip()
    kind = (request.args.get("kind") or "auto").strip()
    permitted = {
        (track["lang"], track["kind"])
        for track in available_captions(record["info"])
    }
    expected_kind = "Subtitles" if kind == "manual" else "Auto captions"
    if (lang, expected_kind) not in permitted:
        abort(400)

    temp_dir = tempfile.mkdtemp(prefix="tikdown_sub_")
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")
    opts = ydl_base_options()
    opts.update({
        "skip_download": True,
        "writesubtitles": kind == "manual",
        "writeautomaticsub": kind != "manual",
        "subtitleslangs": [lang],
        "subtitlesformat": "srt/best",
        "outtmpl": output_template,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([record["url"]])
        found = [
            os.path.join(temp_dir, name) for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
        ]
        if not found:
            raise RuntimeError("No subtitle file was returned.")
        path = found[0]
        response = send_file(path, as_attachment=True, download_name=f"captions-{lang}.srt")
        response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return response
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.exception("Caption download failed")
        flash("This caption track is no longer available.", "error")
        return redirect(url_for("index"))


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", message="This link has expired or does not exist."), 404


@app.errorhandler(413)
def too_large(_error):
    return render_template("error.html", message="The submitted request was too large."), 413


if __name__ == "__main__":
    # For Pydroid local testing only. Use Gunicorn on Render.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
