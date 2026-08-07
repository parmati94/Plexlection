#!/usr/bin/env python3
"""Ad-hoc probe used to sanity-check the ffprobe fact lane inside the container.

Not part of the app — this is the throwaway equivalent of the original
"run ffprobe, get a list, lose the list" workflow that Plexlection replaces.

    docker exec plexlection-dev python3 /app/scripts/probe_demo.py [count]
"""
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path("/media/videos/Movies")


def parse_ratio(value: str | None, default: float = 1.0) -> float:
    if not value or ":" not in value:
        return default
    num, den = value.split(":")[:2]
    try:
        if float(den) == 0:
            return default
        return float(num) / float(den)
    except ValueError:
        return default


def hdr_format(stream: dict) -> str:
    """Rough version of the video.hdr_format fact.

    DV Profile 5 has no HDR10 fallback layer, which is why it renders green or
    purple on playback paths that don't understand Dolby Vision — the single
    most useful maintenance collection this app can build.
    """
    side = {s.get("side_data_type"): s for s in stream.get("side_data_list", [])}
    dovi = side.get("DOVI configuration record")
    if dovi:
        profile = dovi.get("dv_profile")
        bl = dovi.get("bl_present_flag")
        return f"dv_p{profile}" + ("" if bl else "_el_only")
    transfer = stream.get("color_transfer")
    if transfer == "smpte2084":
        return "hdr10"
    if transfer == "arib-std-b67":
        return "hlg"
    return "sdr"


def probe(path: Path) -> dict | None:
    proc = subprocess.run(
        ["ffprobe", "-hide_banner", "-v", "quiet",
         "-print_format", "json", "-show_format", "-show_streams", str(path)],
        # NOTE: -nostdin is an ffmpeg-only flag; ffprobe errors on it with
        # "Option not found". stdin=DEVNULL is the portable equivalent and stops
        # either binary from grabbing the terminal on a malformed file.
        capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not video:
        return None

    width, height = video["width"], video["height"]
    sar = parse_ratio(video.get("sample_aspect_ratio"), 1.0)
    dar = round(width / height * sar, 4)

    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    fmt = data.get("format", {})

    return {
        "title": path.parent.name,
        "codec": video.get("codec_name"),
        "stored": f"{width}x{height}",
        "dar": dar,
        "hdr": hdr_format(video),
        "audio": audio[0].get("codec_name") if audio else None,
        "channels": audio[0].get("channels") if audio else None,
        "runtime_min": round(float(fmt.get("duration", 0)) / 60, 1),
        "size_gb": round(int(fmt.get("size", 0)) / 1e9, 2),
    }


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    files: list[Path] = []
    for movie_dir in sorted(ROOT.iterdir()):
        if not movie_dir.is_dir():
            continue
        for ext in ("*.mkv", "*.mp4", "*.m4v", "*.avi"):
            found = sorted(movie_dir.glob(ext))
            if found:
                files.append(max(found, key=lambda p: p.stat().st_size))
                break
        if len(files) >= limit:
            break

    print(f"{'TITLE':<44} {'DAR':>6} {'STORED':>10} {'HDR':>10} {'AUDIO':>8} {'MIN':>6}")
    print("-" * 92)
    rows = []
    for path in files:
        info = probe(path)
        if not info:
            print(f"{path.parent.name[:43]:<44} {'ERROR':>6}")
            continue
        rows.append(info)
        print(f"{info['title'][:43]:<44} {info['dar']:>6} {info['stored']:>10} "
              f"{info['hdr']:>10} {str(info['audio'])[:8]:>8} {info['runtime_min']:>6}")

    wide = [r for r in rows if r["dar"] >= 2.3]
    print("-" * 92)
    print(f"{len(rows)} probed · {len(wide)} at DAR >= 2.3 (scope)")
    for r in wide:
        print(f"   • {r['title']}  ({r['dar']})")


if __name__ == "__main__":
    main()
