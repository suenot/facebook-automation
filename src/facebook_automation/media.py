"""Read exact captions and validate the private bytes that will be uploaded."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

from .errors import PublishError


def read_caption(caption_file=None, metadata_file=None) -> str:
    if caption_file and metadata_file:
        raise PublishError("INVALID_ARGUMENTS", "Caption and metadata sources are mutually exclusive.")
    try:
        if caption_file:
            with Path(caption_file).open(encoding="utf-8", newline="") as stream:
                return stream.read()
        if metadata_file:
            from metadata_automation import validate
            envelope = json.loads(Path(metadata_file).read_text(encoding="utf-8"))
            validate(envelope)
            return envelope["platforms"]["facebook"]["caption"]
        return ""
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise PublishError("INVALID_CAPTION", f"Caption or complete metadata envelope is invalid ({type(exc).__name__}).") from exc


def copy_video(source: str, destination: Path) -> str:
    if Path(source).suffix.lower() not in {".mp4", ".mov"}:
        raise PublishError("INVALID_MEDIA", "Use a local MP4 or MOV video.")
    try:
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NONBLOCK), "rb") as incoming:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise PublishError("INVALID_MEDIA", "Video must be a readable regular local file.")
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
        with destination.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise PublishError("INVALID_MEDIA", "Video must be a readable regular local file.") from exc


def probe_video(path: Path):
    if path.stat().st_size == 0:
        raise PublishError("INVALID_MEDIA", "Video must be nonempty.")
    try:
        process = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, timeout=30, check=True)
        document = json.loads(process.stdout)
        video = next(item for item in document["streams"] if item["codec_type"] == "video")
        if video["codec_name"] not in {"h264", "hevc"} or not float(document["format"]["duration"]) > 0 or min(video["width"], video["height"]) < 1:
            raise PublishError("INVALID_MEDIA", "Video must contain H.264/H.265, positive duration and dimensions.")
        if "mov" not in document["format"]["format_name"].split(","):
            raise PublishError("INVALID_MEDIA", "Video bytes must be an MP4/MOV container.")
    except FileNotFoundError as exc:
        raise PublishError("FFPROBE_MISSING", "Install ffmpeg (ffprobe) before publishing.", 3) from exc
    except (subprocess.SubprocessError, ValueError, KeyError, StopIteration, TypeError) as exc:
        raise PublishError("INVALID_MEDIA", "ffprobe could not validate this video.") from exc
