"""
SC Downloader — Flask backend
Downloads a SoundCloud playlist and streams a ZIP back to the browser.
"""

import os, uuid, zipfile, threading, tempfile, shutil
from flask import Flask, request, jsonify, send_file, render_template

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

app = Flask(__name__)

# ── In-memory job store ───────────────────────────────────────────────────────
jobs: dict = {}   # job_id -> { status, logs, total, done, folder, zip }


# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── Start download job ─────────────────────────────────────────────────────────
@app.route("/api/download", methods=["POST"])
def start_download():
    if yt_dlp is None:
        return jsonify({"error": "yt-dlp not installed on server."}), 500

    data    = request.get_json(force=True)
    url     = (data.get("url") or "").strip()
    quality = str(data.get("quality") or "320")

    if quality not in ("128", "256", "320"):
        quality = "320"

    if not url or "soundcloud.com" not in url:
        return jsonify({"error": "Invalid SoundCloud URL."}), 400

    job_id = str(uuid.uuid4())[:8]
    tmp    = tempfile.mkdtemp(prefix="scdl_")
    jobs[job_id] = {
        "status": "running",
        "logs":   [],
        "total":  0,
        "done":   0,
        "folder": tmp,
        "zip":    None,
    }

    threading.Thread(
        target=_worker,
        args=(job_id, url, tmp, quality),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


# ── Poll status ────────────────────────────────────────────────────────────────
@app.route("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "logs":   job["logs"],
        "total":  job["total"],
        "done":   job["done"],
    })


# ── Download the ZIP ───────────────────────────────────────────────────────────
@app.route("/api/zip/<job_id>")
def download_zip(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["zip"]:
        return jsonify({"error": "Not ready"}), 404

    zip_path = job["zip"]
    if not os.path.exists(zip_path):
        return jsonify({"error": "ZIP file missing"}), 404

    def cleanup():
        try:
            os.remove(zip_path)
        except Exception:
            pass
        jobs.pop(job_id, None)

    response = send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="soundcloud_playlist.zip",
    )
    # Clean up after response is sent
    threading.Thread(target=cleanup, daemon=True).start()
    return response


# ── Worker thread ──────────────────────────────────────────────────────────────
def _worker(job_id: str, url: str, tmp: str, quality: str):
    job = jobs[job_id]

    def log(msg, kind="info"):
        job["logs"].append({"msg": msg, "kind": kind})

    log("▶  Fetching info…", "accent")

    try:
        # ── 1. Probe to get track count ────────────────────────────────────
        probe_opts = {
            "quiet":        True,
            "no_warnings":  True,
            "extract_flat": True,
        }
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise Exception("Could not fetch info from SoundCloud. Check the URL.")

        # Handle both single tracks and playlists
        entries = info.get("entries")
        if entries:
            entries = [e for e in entries if e]
        else:
            entries = [info]

        total        = len(entries)
        job["total"] = total
        log(f"   Found {total} track(s)", "muted")
        log("─" * 46, "muted")

        # ── 2. Download all tracks ─────────────────────────────────────────
        ydl_opts = {
            "format":         "bestaudio/best",
            "outtmpl":        os.path.join(tmp, "%(playlist_index)02d - %(title)s.%(ext)s"),
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": quality,
            }],
            "ignoreerrors":   True,
            "quiet":          True,
            "no_warnings":    True,
            "progress_hooks": [lambda d: _hook(job_id, d)],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # ── 3. Zip everything ──────────────────────────────────────────────
        log("📦  Zipping files…", "accent")
        zip_path  = tmp + ".zip"
        mp3_files = sorted(f for f in os.listdir(tmp) if f.endswith(".mp3"))

        if not mp3_files:
            raise Exception("No MP3 files were downloaded. The URL may be private or invalid.")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in mp3_files:
                zf.write(os.path.join(tmp, fname), fname)

        job["zip"]    = zip_path
        job["status"] = "done"
        log("─" * 46, "muted")
        log(f"✓  Done! {len(mp3_files)} track(s) ready to download.", "success")

    except Exception as e:
        job["status"] = "error"
        log(f"✗  {e}", "error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _hook(job_id: str, d: dict):
    job = jobs.get(job_id)
    if not job:
        return
    if d["status"] == "finished":
        fname = os.path.basename(d.get("filename", "?"))
        name  = os.path.splitext(fname)[0]
        job["done"] += 1
        job["logs"].append({"msg": f"✓  {name}", "kind": "success"})


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
