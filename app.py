"""
SC Downloader - Flask backend
Downloads a SoundCloud playlist and streams a ZIP back to the browser.
"""

import os, uuid, zipfile, threading, tempfile, shutil, subprocess, sys
from flask import Flask, request, jsonify, send_file, render_template

# ── Install ffmpeg at startup if missing ──────────────────────────────────────
def ensure_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        print("ffmpeg already available")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ffmpeg not found, installing via imageio-ffmpeg...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "imageio-ffmpeg", "-q"])
        import imageio_ffmpeg
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        # Symlink or add to PATH
        bin_dir = "/usr/local/bin"
        try:
            if not os.path.exists(f"{bin_dir}/ffmpeg"):
                os.symlink(ffmpeg_path, f"{bin_dir}/ffmpeg")
            if not os.path.exists(f"{bin_dir}/ffprobe"):
                os.symlink(ffmpeg_path, f"{bin_dir}/ffprobe")
            print(f"ffmpeg symlinked to {bin_dir}")
        except Exception as e:
            print(f"Symlink failed: {e}, adding to PATH instead")
            os.environ["PATH"] = os.path.dirname(ffmpeg_path) + ":" + os.environ["PATH"]
        print("ffmpeg ready via imageio-ffmpeg")

ensure_ffmpeg()

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

app = Flask(__name__)

jobs = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    if yt_dlp is None:
        return jsonify({"error": "yt-dlp not installed on server."}), 500

    data = request.get_json(force=True)
    url  = (data.get("url") or "").strip()

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

    threading.Thread(target=_worker, args=(job_id, url, tmp), daemon=True).start()
    return jsonify({"job_id": job_id})


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


@app.route("/api/zip/<job_id>")
def download_zip(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["zip"]:
        return jsonify({"error": "Not ready"}), 404

    zip_path = job["zip"]
    if not os.path.exists(zip_path):
        return jsonify({"error": "ZIP file missing"}), 404

    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="soundcloud_playlist.zip",
    )


def _worker(job_id, url, tmp):
    job = jobs[job_id]

    def log(msg, kind="info"):
        job["logs"].append({"msg": msg, "kind": kind})

    log("Fetching playlist info...", "accent")

    try:
        # Get ffmpeg path
        ffmpeg_loc = None
        try:
            import imageio_ffmpeg
            ffmpeg_loc = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

        # Probe
        probe_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = info.get("entries", [info]) if info else []
        entries = [e for e in entries if e]
        total   = len(entries)
        job["total"] = total
        log(f"   Found {total} track(s)", "muted")
        log("-" * 46, "muted")

        for i, e in enumerate(entries, 1):
            title = e.get("title") or f"Track {i}"
            log(f"   {i:02d}. {title}", "muted")
        log("-" * 46, "muted")

        # Build ydl options
        ydl_opts = {
            "format":         "bestaudio/best",
            "outtmpl":        os.path.join(tmp, "%(playlist_index)02d - %(title)s.%(ext)s"),
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "320",
            }],
            "ignoreerrors":   True,
            "quiet":          True,
            "no_warnings":    True,
            "progress_hooks": [lambda d: _hook(job_id, d)],
            "logger":         _Logger(job_id),
        }

        if ffmpeg_loc:
            ydl_opts["ffmpeg_location"] = os.path.dirname(ffmpeg_loc)
            log(f"   ffmpeg: {ffmpeg_loc}", "muted")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Zip
        log("Zipping files...", "accent")
        zip_path  = tmp + ".zip"
        mp3_files = [f for f in os.listdir(tmp) if f.endswith(".mp3")]

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in sorted(mp3_files):
                zf.write(os.path.join(tmp, fname), fname)

        skipped = total - len(mp3_files)
        job["zip"]    = zip_path
        job["status"] = "done"
        log("-" * 46, "muted")
        log(f"Done! {len(mp3_files)} downloaded, {skipped} skipped (unavailable).", "success")

    except Exception as e:
        job["status"] = "error"
        log(f"Error: {e}", "error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _hook(job_id, d):
    job = jobs.get(job_id)
    if not job:
        return
    if d["status"] == "finished":
        fname = os.path.basename(d.get("filename", "?"))
        name  = os.path.splitext(fname)[0]
        job["done"] += 1
        job["logs"].append({"msg": f"+ {name}", "kind": "success"})


class _Logger:
    def __init__(self, job_id):
        self.job_id = job_id

    def debug(self, msg): pass
    def info(self, msg):  pass

    def warning(self, msg):
        job = jobs.get(self.job_id)
        if job:
            job["logs"].append({"msg": f"! Skipped: {msg[:80]}", "kind": "accent"})

    def error(self, msg):
        job = jobs.get(self.job_id)
        if job:
            job["logs"].append({"msg": f"x {msg[:100]}", "kind": "error"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
