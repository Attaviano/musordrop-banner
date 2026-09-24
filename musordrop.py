import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BannerError(Exception):
    pass


@dataclass
class Media:
    duration: float
    width: int
    height: int
    has_audio: bool
    fps: float


@dataclass
class Options:
    scale: float | None = None  # fraction of video width, None = full width
    similarity: float = 0.25
    blend: float = 0.08
    audio: bool = True
    crf: int = 18
    preset: str = "veryfast"


@dataclass
class Banner:
    path: Path
    media: Media
    chroma: str | None


@dataclass
class Job:
    video: Path
    v: Media
    banner: Banner
    total: float = 0.0
    starts: list[float] = field(default_factory=list)
    cuts: list[float] = field(default_factory=list)


def _run(cmd, **kw):
    return subprocess.run(cmd, creationflags=NO_WINDOW, **kw)


def probe(path: Path) -> Media:
    out = _run([FFMPEG, "-hide_banner", "-i", str(path)],
               capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    v = re.search(r"Stream #0:\d+.*?: Video: .*?(\d{2,5})x(\d{2,5})", out)
    if not v or duration <= 0:
        raise BannerError("не похоже на видео")
    w, h = int(v[1]), int(v[2])
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", out) or re.search(r"rotate\s*:\s*(-?\d+)", out)
    if rot and abs(round(float(rot[1]))) % 180 == 90:
        w, h = h, w
    has_audio = re.search(r"Stream #0:\d+.*?: Audio:", out) is not None
    fps = re.search(r"Stream #0:\d+.*?: Video: .*?(\d+(?:\.\d+)?) fps", out)
    return Media(duration, w, h, has_audio, float(fps[1]) if fps else 30.0)


def detect_chroma(path: Path) -> str | None:
    raw = _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", "0.1", "-i", str(path),
                "-frames:v", "1", "-vf", "crop=4:4:2:2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
               capture_output=True).stdout
    if len(raw) < 3:
        return None
    r, g, b = raw[0], raw[1], raw[2]
    if g > 180 and r < 90 and b < 90:
        return f"{r:02x}{g:02x}{b:02x}"
    return None


def find_banner() -> Path | None:
    for pattern in ("*green-screen*.mp4", "*musor*drop*.mp4", "*musordrop*.mp4", "banner.mp4"):
        found = sorted(HERE.glob(pattern))
        if found:
            return found[0]
    return None


def load_banner(path: Path | None = None) -> Banner:
    path = path or find_banner()
    if not path:
        raise BannerError("Не найден баннер Musor Drop. Скачай его в кабинете партнёрки "
                          "и положи в папку с программой.")
    try:
        media = probe(path)
    except BannerError as e:
        raise BannerError(f"{path.name}: {e}") from None
    if media.duration > 30:
        raise BannerError(f"{path.name}: баннер длиннее 30 секунд, это точно он?")
    return Banner(path, media, detect_chroma(path))


def banner_count(length: float) -> int:
    return 1 if length < 120 else int(length // 60)


def plan(duration: float, banner_len: float):
    """Returns (final length, banner starts in the output, cut points in the source)."""
    # Count by the final length: every banner makes the video longer.
    n = banner_count(duration)
    for _ in range(50):
        m = banner_count(duration + n * banner_len)
        if m == n:
            break
        n = m
    total = duration + n * banner_len
    starts = [total / 2] if n == 1 else [30 + 60 * i for i in range(n)]
    cuts = [s - i * banner_len for i, s in enumerate(starts)]
    if cuts[0] <= 0 or cuts[-1] >= duration:
        raise BannerError("видео слишком короткое для баннера")
    return total, starts, cuts


def fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:05.2f}"


def make_job(video: Path, banner: Banner) -> Job:
    v = probe(video)
    job = Job(video, v, banner)
    job.total, job.starts, job.cuts = plan(v.duration, banner.media.duration)
    return job


def build_command(job: Job, out: Path, opts: Options) -> list[str]:
    v, b = job.v, job.banner
    n = len(job.cuts)
    fps = v.fps
    cut_frames = [round(c * fps) for c in job.cuts]
    total_frames = round(v.duration * fps)
    freeze_frames = max(1, round(b.media.duration * fps))
    freeze_len = freeze_frames / fps
    banner_audio = opts.audio and b.media.has_audio

    width = round(v.width * opts.scale) if opts.scale else min(b.media.width, v.width)
    width -= width % 2
    key = f"colorkey=0x{b.chroma}:{opts.similarity}:{opts.blend}," if b.chroma else ""
    afmt = "aformat=sample_rates=48000:channel_layouts=stereo"

    f = [f"[0:v]fps={fps},setsar=1,split={2 * n + 1}" + "".join(f"[vs{i}]" for i in range(2 * n + 1))]
    if v.has_audio:
        f.append(f"[0:a]{afmt},asplit={n + 1}" + "".join(f"[as{i}]" for i in range(n + 1)))
    else:
        f.append(f"anullsrc=r=48000:cl=stereo,atrim=end={v.duration},asplit={n + 1}"
                 + "".join(f"[as{i}]" for i in range(n + 1)))
    f.append(f"[1:v]{key}format=rgba,scale={width}:-1,split={n}" + "".join(f"[bv{i}]" for i in range(n)))
    if banner_audio:
        f.append(f"[1:a]{afmt},asplit={n}" + "".join(f"[ba{i}]" for i in range(n)))

    # Cut by frame numbers so the freeze uses exactly the last frame before each cut.
    # (tpad=stop_mode=clone after trim produces no frames in ffmpeg 7.1, hence trim + loop.)
    bounds = [0] + cut_frames + [total_frames]
    concat_in = ""
    for i in range(n + 1):
        s, e = bounds[i], bounds[i + 1]
        f.append(f"[vs{i}]trim=start_frame={s}:end_frame={e},setpts=PTS-STARTPTS[vt{i}]")
        f.append(f"[as{i}]atrim=start={s / fps}:end={e / fps},asetpts=PTS-STARTPTS[at{i}]")
        concat_in += f"[vt{i}][at{i}]"
        if i == n:
            break
        last = max(e - 1, 0)
        f.append(f"[vs{n + 1 + i}]trim=start_frame={last}:end_frame={last + 1},setpts=PTS-STARTPTS,"
                 f"loop=loop={freeze_frames - 1}:size=1,setpts=N/{fps}/TB[fz{i}]")
        f.append(f"[bv{i}]setpts=PTS-STARTPTS[bd{i}]")
        f.append(f"[fz{i}][bd{i}]overlay=(W-w)/2:(H-h)/2:eof_action=pass[fo{i}]")
        if banner_audio:
            f.append(f"[ba{i}]apad,atrim=end={freeze_len},asetpts=PTS-STARTPTS[fa{i}]")
        else:
            f.append(f"anullsrc=r=48000:cl=stereo,atrim=end={freeze_len}[fa{i}]")
        concat_in += f"[fo{i}][fa{i}]"
    f.append(f"{concat_in}concat=n={2 * n + 1}:v=1:a=1[outv][outa]")

    # Without explicit cfr the mp4 muxer drops a frame at every seam.
    return [FFMPEG, "-hide_banner", "-y", "-i", str(job.video), "-i", str(b.path),
            "-filter_complex", ";".join(f), "-map", "[outv]", "-map", "[outa]",
            "-fps_mode", "cfr", "-r", f"{fps}",
            "-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(out)]


def render(job: Job, out: Path, opts: Options, on_progress: Callable[[float], None] | None = None):
    cmd = build_command(job, out, opts)
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, creationflags=NO_WINDOW,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            if on_progress and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                on_progress(min(1.0, max(0.0, us / 1e6 / job.total)))
        proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            tail = err.read().decode("utf-8", "replace").strip().splitlines()[-12:]
            raise BannerError("ffmpeg завершился с ошибкой:\n" + "\n".join(tail))
    if on_progress:
        on_progress(1.0)


def frame_png(path: Path, out: Path, t: float, width: int, chroma: str | None = None, bg: str = "0x14121c"):
    if chroma:
        vf = (f"[0:v]colorkey=0x{chroma}:0.25:0.08,scale={width}:-2,format=rgba,split[a][b];"
              f"[a]drawbox=c={bg}@1:t=fill[bg];[bg][b]overlay=format=auto")
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t}", "-i", str(path),
               "-filter_complex", vf, "-frames:v", "1", str(out)]
    else:
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t}", "-i", str(path),
               "-frames:v", "1", "-vf", f"scale={width}:-2", str(out)]
    _run(cmd, capture_output=True)
    return out.exists()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Вставляет баннер Musor Drop в видео по правилам партнёрки")
    p.add_argument("videos", type=Path, nargs="+", help="одно или несколько видео")
    p.add_argument("-o", "--out", type=Path, help="куда сохранить, только для одного видео")
    p.add_argument("--scale", type=float, help="ширина баннера как доля ширины видео, например 0.8")
    p.add_argument("--similarity", type=float, default=0.25, help="сила вырезания зелёного фона, 0.01-1")
    p.add_argument("--mute", action="store_true", help="без звука баннера")
    p.add_argument("--crf", type=int, default=18, help="качество x264, меньше - лучше")
    p.add_argument("--dry-run", action="store_true", help="только показать тайминги")
    args = p.parse_args()

    if args.out and len(args.videos) > 1:
        sys.exit("-o работает только с одним видео")
    try:
        banner = load_banner()
    except BannerError as e:
        sys.exit(str(e))

    opts = Options(scale=args.scale, similarity=args.similarity, audio=not args.mute, crf=args.crf)
    failed = False
    for video in args.videos:
        try:
            if not video.exists():
                raise BannerError("файл не найден")
            job = make_job(video, banner)
            print(f"\n{video.name}: {fmt(job.v.duration)} -> {fmt(job.total)}, баннеров: {len(job.starts)}")
            for s in job.starts:
                print(f"  {fmt(s)} - {fmt(s + banner.media.duration)}")
            if args.dry_run:
                continue
            out = args.out or video.with_name(f"{video.stem}_musordrop.mp4")
            if out.resolve() == video.resolve():
                raise BannerError("нельзя сохранять поверх исходника")
            render(job, out, opts)
            print(f"  сохранено: {out}")
        except BannerError as e:
            failed = True
            print(f"{video.name}: {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
