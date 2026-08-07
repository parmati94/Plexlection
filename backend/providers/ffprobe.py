"""ffprobe provider — the facts Plex doesn't have.

Reads container and stream metadata straight from the file. Cheap (no decoding),
so it runs on every scan for anything whose file fingerprint changed.

What it deliberately does *not* do: detect black bars baked into the frame.
`video.dar` is the ratio the container declares. For a hard-matted scope file
(a genuine 1920x800) that is the true ratio and the ultrawide use case works.
For a 2.39:1 film letterboxed inside a 1920x1080 frame it reports 1.78, and
only decoding frames could tell the difference.
"""
import asyncio
import json
import time
from fractions import Fraction
from typing import Any, AsyncIterator

from backend.common.config import config
from backend.common.logging_config import get_logger
from backend.facts.spec import CostTier, FactSpec, FactType
from backend.providers.base import (
    STATUS_ERROR,
    STATUS_OK,
    EnrichContext,
    Eligibility,
    FactProvider,
    FactResult,
    ItemRow,
)
from backend.utils.proc import ProcTimeout, run_proc

logger = get_logger(__name__)

# Standard ratios, for bucketing. 2.76 is Ultra Panavision (Hateful Eight,
# Ben-Hur); 2.00 is Univisium, increasingly common on streaming productions.
CANONICAL_RATIOS: tuple[tuple[float, str], ...] = (
    (1.33, "1.33:1"), (1.37, "1.37:1"), (1.66, "1.66:1"), (1.78, "1.78:1"),
    (1.85, "1.85:1"), (2.00, "2.00:1"), (2.20, "2.20:1"), (2.35, "2.35:1"),
    (2.39, "2.39:1"), (2.76, "2.76:1"),
)
RATIO_TOLERANCE = 0.015  # ±1.5%


def snap_ratio(dar: float) -> str:
    for value, label in CANONICAL_RATIOS:
        if abs(dar - value) / value <= RATIO_TOLERANCE:
            return label
    return "other"


def _ratio(text: str | None, default: float = 1.0) -> float:
    """Parse ffprobe's 'W:H' ratio strings, tolerating 0:0 and N/A."""
    if not text or ":" not in text:
        return default
    num, _, den = text.partition(":")
    try:
        n, d = float(num), float(den)
        return n / d if d and n else default
    except ValueError:
        return default


def _fps(text: str | None) -> float | None:
    if not text or "/" not in text:
        return None
    try:
        value = float(Fraction(text))
        return round(value, 3) if value > 0 else None
    except (ZeroDivisionError, ValueError):
        return None


def _hdr_format(stream: dict) -> str:
    """Classify the HDR flavour.

    DV Profile 5 carries no HDR10 base layer, which is why those files render
    green or purple on any playback path that doesn't understand Dolby Vision.
    Separating p5 from p7/p8 is the point of this fact — and the reason the
    image ships a modern ffmpeg, since older builds report DOVI side data poorly.
    """
    for side in stream.get("side_data_list", []) or []:
        if side.get("side_data_type") == "DOVI configuration record":
            profile = side.get("dv_profile")
            if profile is not None:
                return f"dv_p{profile}"
            return "dv"
    transfer = stream.get("color_transfer")
    if transfer == "smpte2084":
        return "hdr10"
    if transfer == "arib-std-b67":
        return "hlg"
    return "sdr"


def _audio_layout(stream: dict) -> str:
    channels = stream.get("channels")
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(channels, f"{channels}ch" if channels else "unknown")


def _is_commentary(stream: dict) -> bool:
    tags = {k.lower(): str(v).lower() for k, v in (stream.get("tags") or {}).items()}
    title = tags.get("title", "")
    return "commentary" in title or stream.get("disposition", {}).get("comment") == 1


class FFprobeProvider(FactProvider):
    id = "ffprobe"
    label = "ffprobe"
    cost = CostTier.CHEAP
    schema_version = 1
    depends_on = ()
    batch_size = 1
    max_age_s = None  # immutable until the file changes
    default_concurrency = 4
    file_fingerprinted = True
    # Anything with a file. Shows have none — their facts come from rollup.
    default_applies_to = ("movie", "episode")

    facts = (
        # ── video ─────────────────────────────────────────────────────────
        FactSpec("video.dar", "Aspect ratio", FactType.NUMBER,
                 "Display aspect ratio declared by the container, corrected for "
                 "non-square pixels. 2.39 = scope, 1.85 = flat, 1.78 = 16:9. "
                 "Reads the container only: a scope film stored as 1920x800 is "
                 "correct here, but one letterboxed inside a 1080p frame reports "
                 "1.78, because the black bars are part of the picture.",
                 group="Video", unit="ratio", format="ratio",
                 indexed=True, aggregatable=True, example=2.39),
        FactSpec("video.aspect_bucket", "Aspect ratio (bucketed)", FactType.ENUM,
                 "video.dar snapped to the nearest standard ratio (±1.5%).",
                 group="Video", indexed=True,
                 enum_values=tuple(label for _, label in CANONICAL_RATIOS) + ("other",),
                 example="2.39:1"),
        FactSpec("video.width", "Width", FactType.NUMBER, "Stored frame width in pixels.",
                 group="Video", unit="px", aggregatable=True, example=3840),
        FactSpec("video.height", "Height", FactType.NUMBER, "Stored frame height in pixels.",
                 group="Video", unit="px", aggregatable=True, example=1600),
        FactSpec("video.sar", "Pixel aspect ratio", FactType.NUMBER,
                 "Sample aspect ratio. Anything but 1.0 means anamorphic storage, "
                 "where stored dimensions alone give the wrong displayed shape.",
                 group="Video", example=1.0),
        FactSpec("video.codec", "Video codec", FactType.STRING,
                 "h264, hevc, av1, mpeg2video…", group="Video", indexed=True, example="hevc"),
        FactSpec("video.hdr_format", "HDR format", FactType.ENUM,
                 "SDR, HDR10, HLG, or a Dolby Vision profile. DV Profile 5 has no "
                 "HDR10 fallback layer and renders green on non-DV playback paths.",
                 group="Video", indexed=True,
                 enum_values=("sdr", "hdr10", "hlg", "dv", "dv_p4", "dv_p5",
                              "dv_p7", "dv_p8", "dv_p9"),
                 example="dv_p8"),
        FactSpec("video.bit_depth", "Bit depth", FactType.NUMBER,
                 "8 or 10 bits per component.", group="Video", unit="bit", example=10),
        FactSpec("video.fps", "Frame rate", FactType.NUMBER,
                 "Average frame rate. 25.0 on a film-sourced title usually means a "
                 "PAL speed-up.", group="Video", unit="fps", aggregatable=True, example=23.976),
        FactSpec("video.interlaced", "Interlaced", FactType.BOOL,
                 "Field order indicates interlaced content.", group="Video", example=False),

        # ── audio ─────────────────────────────────────────────────────────
        FactSpec("audio.codec", "Audio codec", FactType.STRING,
                 "Codec of the default audio track.", group="Audio", example="truehd"),
        FactSpec("audio.channels", "Audio channels", FactType.NUMBER,
                 "Channel count of the default track.", group="Audio",
                 aggregatable=True, example=8),
        FactSpec("audio.layout", "Channel layout", FactType.ENUM,
                 "mono, stereo, 5.1, 7.1…", group="Audio",
                 enum_values=("mono", "stereo", "5.1", "7.1", "unknown", "3ch", "4ch", "10ch", "12ch"),
                 example="7.1"),
        FactSpec("audio.track_count", "Audio tracks", FactType.NUMBER,
                 "Number of audio streams.", group="Audio", aggregatable=True, example=2),
        FactSpec("audio.languages", "Audio languages", FactType.LIST,
                 "ISO codes of every audio track.", group="Audio",
                 element_type=FactType.STRING, example=["eng", "fra"]),
        FactSpec("audio.has_commentary", "Has commentary", FactType.BOOL,
                 "A track is flagged as, or titled, commentary.", group="Audio", example=True),

        # ── subtitles ─────────────────────────────────────────────────────
        FactSpec("subs.track_count", "Subtitle tracks", FactType.NUMBER,
                 "Number of embedded subtitle streams.", group="Subtitles",
                 aggregatable=True, example=3),
        FactSpec("subs.languages", "Subtitle languages", FactType.LIST,
                 "ISO codes of every subtitle track.", group="Subtitles",
                 element_type=FactType.STRING, example=["eng"]),
        FactSpec("subs.has_forced", "Has forced subtitles", FactType.BOOL,
                 "A subtitle track carries the forced disposition.",
                 group="Subtitles", example=False),

        # ── file ──────────────────────────────────────────────────────────
        FactSpec("file.duration_s", "Runtime", FactType.NUMBER,
                 "Duration in seconds, from the container.",
                 group="File", unit="s", format="duration_s",
                 indexed=True, aggregatable=True, example=7200.0),
        FactSpec("file.size_bytes", "File size", FactType.NUMBER,
                 "Size on disk.", group="File", unit="B", format="bytes",
                 aggregatable=True, example=42_000_000_000),
        FactSpec("file.bitrate_kbps", "Overall bitrate", FactType.NUMBER,
                 "Total bitrate. Very low for the resolution suggests a poor "
                 "encode; very high suggests a remux worth reclaiming.",
                 group="File", unit="kbps", format="kbps",
                 indexed=True, aggregatable=True, example=24000.0),
        FactSpec("file.container", "Container", FactType.STRING,
                 "matroska, mp4, avi…", group="File", example="matroska,webm"),
    )

    def is_configured(self) -> bool:
        return True

    def selector(self) -> tuple[str, list]:
        # Deliberately broad. Filtering unreadable files out here instead would
        # hide them from can_enrich, so no skip would be recorded and a scan
        # over an entirely unmapped library would report *nothing* — no work, no
        # skips, no reason. Let can_enrich reject them so the count and the
        # reason both surface on the Scan tab.
        return "1=1", []

    def can_enrich(self, item: ItemRow) -> Eligibility:
        if item.path_status != "mapped" or not item.local_path:
            # Recorded once with a reason rather than throwing per item — this is
            # the difference between "1,904 skipped: path unmapped" and 1,904
            # identical stack traces.
            return Eligibility.skip(
                f"file not readable in this container (path {item.path_status})"
            )
        return Eligibility.yes()

    def fingerprint(self, item: ItemRow) -> str | None:
        return item.file_fp

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        for item in items:
            if ctx.cancelled():
                return
            async with ctx.semaphore:
                if ctx.cancelled():
                    return
                if ctx.progress:
                    ctx.progress(item.title)
                yield await self._probe_one(item, ctx)

    async def _probe_one(self, item: ItemRow, ctx: EnrichContext) -> FactResult:
        started = time.perf_counter()
        timeout = float(getattr(ctx.settings.scan, "ffprobe_timeout_s", 60))
        argv = [
            config.FFPROBE_BIN,
            "-hide_banner",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            item.local_path,
        ]
        # NB: no -nostdin. That is an ffmpeg-only flag and ffprobe exits 1 on it;
        # run_proc closes stdin instead, which works for both binaries.

        try:
            rc, out, err = await run_proc(argv, timeout=timeout)
        except ProcTimeout as exc:
            return FactResult(item.id, STATUS_ERROR, reason=str(exc),
                              input_fp=item.file_fp,
                              duration_ms=int((time.perf_counter() - started) * 1000))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return FactResult(item.id, STATUS_ERROR, reason=f"{type(exc).__name__}: {exc}",
                              input_fp=item.file_fp,
                              duration_ms=int((time.perf_counter() - started) * 1000))

        elapsed = int((time.perf_counter() - started) * 1000)

        if rc != 0:
            detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
            return FactResult(item.id, STATUS_ERROR,
                              reason=f"ffprobe exit {rc}: {detail[-1] if detail else 'no output'}",
                              input_fp=item.file_fp, duration_ms=elapsed)

        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            return FactResult(item.id, STATUS_ERROR, reason=f"unparseable ffprobe output: {exc}",
                              input_fp=item.file_fp, duration_ms=elapsed)

        facts = self._extract(data)
        if not facts:
            return FactResult(item.id, STATUS_ERROR, reason="no video stream found",
                              input_fp=item.file_fp, duration_ms=elapsed)

        return FactResult(item.id, STATUS_OK, facts=facts,
                          input_fp=item.file_fp, duration_ms=elapsed)

    def _extract(self, data: dict) -> dict[str, Any]:
        streams = data.get("streams", []) or []
        fmt = data.get("format", {}) or {}

        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            return {}

        audio = [s for s in streams if s.get("codec_type") == "audio"]
        subs = [s for s in streams if s.get("codec_type") == "subtitle"]

        width = video.get("width") or 0
        height = video.get("height") or 0
        sar = _ratio(video.get("sample_aspect_ratio"), 1.0)

        facts: dict[str, Any] = {}

        if width and height:
            # Prefer the container's declared DAR; fall back to geometry x SAR.
            dar = _ratio(video.get("display_aspect_ratio"), 0.0) or (width / height * sar)
            dar = round(dar, 4)
            facts["video.dar"] = dar
            facts["video.aspect_bucket"] = snap_ratio(dar)
            facts["video.width"] = width
            facts["video.height"] = height

        facts["video.sar"] = round(sar, 4)
        facts["video.codec"] = video.get("codec_name")
        facts["video.hdr_format"] = _hdr_format(video)
        facts["video.interlaced"] = video.get("field_order") not in (None, "progressive", "unknown")

        bits = video.get("bits_per_raw_sample") or video.get("bits_per_coded_sample")
        if bits:
            try:
                facts["video.bit_depth"] = int(bits)
            except (TypeError, ValueError):
                pass

        fps = _fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate"))
        if fps:
            facts["video.fps"] = fps

        if audio:
            default = next(
                (s for s in audio if (s.get("disposition") or {}).get("default") == 1), audio[0]
            )
            facts["audio.codec"] = default.get("codec_name")
            facts["audio.channels"] = default.get("channels")
            facts["audio.layout"] = _audio_layout(default)
            facts["audio.has_commentary"] = any(_is_commentary(s) for s in audio)
        facts["audio.track_count"] = len(audio)
        facts["audio.languages"] = sorted({
            (s.get("tags") or {}).get("language", "und") for s in audio
        })

        facts["subs.track_count"] = len(subs)
        facts["subs.languages"] = sorted({
            (s.get("tags") or {}).get("language", "und") for s in subs
        })
        facts["subs.has_forced"] = any(
            (s.get("disposition") or {}).get("forced") == 1 for s in subs
        )

        if fmt.get("duration"):
            try:
                facts["file.duration_s"] = round(float(fmt["duration"]), 2)
            except (TypeError, ValueError):
                pass
        if fmt.get("size"):
            try:
                facts["file.size_bytes"] = int(fmt["size"])
            except (TypeError, ValueError):
                pass
        if fmt.get("bit_rate"):
            try:
                facts["file.bitrate_kbps"] = round(int(fmt["bit_rate"]) / 1000, 1)
            except (TypeError, ValueError):
                pass
        facts["file.container"] = fmt.get("format_name")

        return {k: v for k, v in facts.items() if v is not None}
