import os
import re
import socket
import subprocess
import sys
import threading
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

import musordrop as md

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
UPLOADS = WORK / "uploads"
THUMBS = WORK / "thumbs"
OUTPUT = ROOT / "output"
for d in (UPLOADS, THUMBS, OUTPUT):
    d.mkdir(parents=True, exist_ok=True)

HOST, PORT = "127.0.0.1", 8765
QUALITY = {"high": 18, "medium": 22, "light": 26}

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
lock = threading.RLock()
wake = threading.Event()

settings = {"scale": 100, "audio": True, "similarity": 0.25, "quality": "high"}
state = {"banner": None, "banner_error": None, "banner_thumb": None}
jobs: dict[str, dict] = {}


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "video"


def options() -> md.Options:
    return md.Options(
        scale=None if settings["scale"] >= 100 else settings["scale"] / 100,
        similarity=float(settings["similarity"]),
        audio=settings["audio"],
        crf=QUALITY[settings["quality"]],
    )


def ensure_banner() -> md.Banner | None:
    if state["banner"] and state["banner"].path.exists():
        return state["banner"]
    try:
        banner = md.load_banner()
    except md.BannerError as e:
        state.update(banner=None, banner_error=str(e), banner_thumb=None)
        return None
    thumb = THUMBS / f"banner_{uuid.uuid4().hex[:8]}.png"
    md.frame_png(banner.path, thumb, min(4.0, banner.media.duration * 0.66), 480, banner.chroma)
    state.update(banner=banner, banner_error=None, banner_thumb=thumb.name if thumb.exists() else None)
    return banner


def unique_output(stem: str) -> Path:
    out = OUTPUT / f"{stem}_musordrop.mp4"
    i = 2
    while out.exists():
        out = OUTPUT / f"{stem}_musordrop ({i}).mp4"
        i += 1
    return out


def job_view(jid, item):
    j = item["job"]
    return {
        "id": jid,
        "name": item["name"],
        "status": item["status"],
        "progress": round(item["progress"], 4),
        "error": item["error"],
        "output": item["output"],
        "thumb": item["thumb"],
        "duration": j.v.duration,
        "total": j.total,
        "banner_len": j.banner.media.duration,
        "starts": j.starts,
        "width": j.v.width,
        "height": j.v.height,
        "fps": j.v.fps,
    }


def snapshot():
    with lock:
        b = ensure_banner()
        return {
            "settings": settings,
            "banner": {
                "name": b.path.name if b else None,
                "duration": b.media.duration if b else None,
                "width": b.media.width if b else None,
                "height": b.media.height if b else None,
                "chroma": b.chroma if b else None,
                "thumb": state["banner_thumb"],
                "error": state["banner_error"],
            },
            "jobs": [job_view(k, v) for k, v in jobs.items()],
        }


def worker():
    while True:
        wake.wait()
        with lock:
            nxt = next((v for v in jobs.values() if v["status"] == "queued"), None)
            if not nxt:
                wake.clear()
                continue
            item = nxt
            item.update(status="processing", progress=0.0, error=None)
            opts = options()
            job = item["job"]
            out = unique_output(Path(item["name"]).stem)

        def progress(p, item=item):
            item["progress"] = p

        try:
            md.render(job, out, opts, progress)
            with lock:
                item.update(status="done", progress=1.0, output=out.name)
        except Exception as e:
            with lock:
                item.update(status="error", error=str(e))
            out.unlink(missing_ok=True)


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/api/state")
def api_state():
    return jsonify(snapshot())


@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400)
    with lock:
        banner = ensure_banner()
    if not banner:
        return jsonify(error=state["banner_error"]), 400
    jid = uuid.uuid4().hex[:10]
    name = safe_name(Path(f.filename).name)
    path = UPLOADS / f"{jid}{Path(name).suffix.lower() or '.mp4'}"
    f.save(path)
    try:
        job = md.make_job(path, banner)
    except md.BannerError as e:
        path.unlink(missing_ok=True)
        return jsonify(error=f"{name}: {e}"), 400
    thumb = THUMBS / f"{jid}.jpg"
    md.frame_png(path, thumb, min(1.0, job.v.duration / 3), 180)
    with lock:
        jobs[jid] = {"job": job, "name": name, "status": "ready", "progress": 0.0, "error": None,
                     "output": None, "thumb": thumb.name if thumb.exists() else None}
    return jsonify(snapshot())


@app.post("/api/settings")
def api_settings():
    data = request.get_json(force=True) or {}
    with lock:
        if "scale" in data:
            settings["scale"] = max(30, min(100, int(data["scale"])))
        if "audio" in data:
            settings["audio"] = bool(data["audio"])
        if "similarity" in data:
            settings["similarity"] = max(0.05, min(0.6, float(data["similarity"])))
        if data.get("quality") in QUALITY:
            settings["quality"] = data["quality"]
    return jsonify(snapshot())


@app.post("/api/start")
def api_start():
    ids = (request.get_json(silent=True) or {}).get("ids")
    with lock:
        for k, v in jobs.items():
            if (ids is None or k in ids) and v["status"] in ("ready", "error"):
                v.update(status="queued", progress=0.0, error=None)
    wake.set()
    return jsonify(snapshot())


@app.post("/api/jobs/<jid>/redo")
def api_redo(jid):
    with lock:
        item = jobs.get(jid) or abort(404)
        if item["status"] in ("done", "error"):
            item.update(status="ready", progress=0.0, output=None, error=None)
    return jsonify(snapshot())


def drop_job(jid):
    item = jobs.pop(jid)
    item["job"].video.unlink(missing_ok=True)
    if item["thumb"]:
        (THUMBS / item["thumb"]).unlink(missing_ok=True)


@app.delete("/api/jobs/<jid>")
def api_delete(jid):
    with lock:
        item = jobs.get(jid)
        if not item or item["status"] == "processing":
            abort(409)
        drop_job(jid)
    return jsonify(snapshot())


@app.post("/api/clear-done")
def api_clear_done():
    with lock:
        for jid in [k for k, v in jobs.items() if v["status"] == "done"]:
            drop_job(jid)
    return jsonify(snapshot())


@app.post("/api/reveal")
def api_reveal():
    name = (request.get_json(silent=True) or {}).get("output")
    target = (OUTPUT / name).resolve() if name else OUTPUT
    if name and (target.parent != OUTPUT.resolve() or not target.exists()):
        abort(404)
    if sys.platform == "win32":
        if name:
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(OUTPUT)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)] if name else ["open", str(OUTPUT)])
    else:
        subprocess.Popen(["xdg-open", str(OUTPUT)])
    return jsonify(ok=True)


@app.get("/output/<path:name>")
def output_file(name):
    return send_from_directory(OUTPUT, name, conditional=True)


@app.get("/thumbs/<path:name>")
def thumb_file(name):
    return send_from_directory(THUMBS, name, max_age=3600)


def already_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def clean_work_dir():
    # jobs live in memory, so files left from a previous run are orphans
    for d in (UPLOADS, THUMBS):
        for f in d.iterdir():
            f.unlink(missing_ok=True)


def main():
    global PORT
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 >= len(sys.argv) or not sys.argv[i + 1].isdigit():
            sys.exit("Укажи порт числом: --port 8766")
        PORT = int(sys.argv[i + 1])
    url = f"http://{HOST}:{PORT}"
    if already_running():
        print("Уже запущено, открываю в браузере")
        webbrowser.open(url)
        return
    clean_work_dir()
    with lock:
        ensure_banner()
    threading.Thread(target=worker, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Musor Drop Banner: {url}\n"
          "Не закрывай это окно, пока работаешь.\n"
          "Новости и обновления: https://t.me/attavian0")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
