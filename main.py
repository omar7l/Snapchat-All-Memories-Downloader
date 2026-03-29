import argparse
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field, field_validator
from tqdm.asyncio import tqdm

DEFAULT_CONCURRENT = 2
DEFAULT_RETRIES = 4
DEFAULT_REQUEST_DELAY = 2.0
DEFAULT_REQUEST_JITTER = 0.5
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
MIN_FREE_SPACE_GB = 5
RECOMMENDED_FREE_SPACE_GB = 12
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}
EXIFTOOL_PATH = shutil.which("exiftool")


class Memory(BaseModel):
    date: datetime = Field(alias="Date")
    download_link: str = Field(alias="Download Link")
    media_download_url: str = Field(default="", alias="Media Download Url")
    location: str = Field(default="", alias="Location")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    resolved_filename: str = ""

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, v):
        if isinstance(v, str):
            return datetime.strptime(v, "%Y-%m-%d %H:%M:%S UTC")
        return v

    def model_post_init(self, __context):
        if self.location and not self.latitude:
            if match := re.search(r"([-\d.]+),\s*([-\d.]+)", self.location):
                self.latitude = float(match.group(1))
                self.longitude = float(match.group(2))

    @property
    def filename(self) -> str:
        return self.date.strftime("%Y-%m-%d_%H-%M-%S")

    @property
    def media_id(self) -> str:
        for url in (self.media_download_url, self.download_link):
            if not url:
                continue
            query = parse_qs(urlparse(url).query)
            for key in ("mid", "sid"):
                values = query.get(key)
                if values and values[0]:
                    return values[0]
        return ""

    @property
    def output_name(self) -> str:
        return self.resolved_filename or self.filename


class Stats(BaseModel):
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    mb: float = 0
    repaired: int = 0
    imported: int = 0


class RequestPacer:
    def __init__(self, request_delay: float, request_jitter: float):
        self.request_delay = max(request_delay, 0.0)
        self.request_jitter = max(request_jitter, 0.0)
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def wait_turn(self):
        async with self._lock:
            now = time.monotonic()
            target = max(now, self._next_request_at)
            spacing = self.request_delay + random.uniform(0, self.request_jitter)
            self._next_request_at = target + spacing

        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def penalize(self, penalty_seconds: float):
        if penalty_seconds <= 0:
            return

        async with self._lock:
            self._next_request_at = max(
                self._next_request_at,
                time.monotonic() + penalty_seconds,
            )


def load_memories(json_path: Path) -> list[Memory]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    memories = [Memory(**item) for item in data["Saved Media"]]
    assign_unique_filenames(memories)
    return memories


def assign_unique_filenames(memories: list[Memory]):
    counts = Counter(memory.filename for memory in memories)
    used_names: set[str] = set()
    collision_counts: Counter[str] = Counter()

    for memory in memories:
        base_name = memory.filename
        if counts[base_name] == 1:
            memory.resolved_filename = base_name
            used_names.add(base_name)
            continue

        collision_counts[base_name] += 1
        suffix = memory.media_id[:8].lower() if memory.media_id else f"{collision_counts[base_name]:02d}"
        candidate = f"{base_name}_{suffix}"
        if candidate in used_names:
            candidate = f"{base_name}_{collision_counts[base_name]:02d}"

        memory.resolved_filename = candidate
        used_names.add(candidate)


def resolve_json_path(json_file: str) -> Path:
    requested_path = Path(json_file)
    if requested_path.exists():
        return requested_path

    common_paths = [
        Path("json/memories_history.json"),
        Path("memories/json/memories_history.json"),
        Path("memories_history.json"),
    ]

    for candidate in common_paths:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"Could not find memories_history.json. Tried '{requested_path}' and common export paths: "
        + ", ".join(str(path) for path in common_paths)
    )


def is_retryable_exception(exc: Exception) -> bool:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES

    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    )


def backoff_seconds(attempt: int) -> float:
    # Keep retries short early on, then back off harder if Snapchat starts refusing requests.
    return min(2 ** (attempt - 1), 20)


def penalty_seconds_for_exception(exc: Exception, attempt: int) -> float:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 403:
            return max(45.0, backoff_seconds(attempt) * 6)
        if status_code == 429:
            return max(60.0, backoff_seconds(attempt) * 8)
        if status_code >= 500:
            return max(15.0, backoff_seconds(attempt) * 2)

    return backoff_seconds(attempt)


async def get_cdn_url(client: Any, download_link: str) -> str:
    response = await client.post(
        download_link,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()
    return response.text.strip()


def extension_from_url(url: str) -> str:
    return Path(url.split("?")[0]).suffix or ".jpg"


def decimal_to_dms_rational(decimal: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    degrees = int(abs(decimal))
    minutes_float = (abs(decimal) - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60 * 10000)
    return ((degrees, 1), (minutes, 1), (seconds, 10000))


def format_iso6709(latitude: float, longitude: float) -> str:
    return f"{latitude:+08.4f}{longitude:+09.4f}/"


def format_exiftool_datetime(date: datetime) -> str:
    return date.strftime("%Y:%m:%d %H:%M:%S+00:00")


def format_quicktime_header_datetime(date: datetime) -> str:
    return date.strftime("%Y:%m:%d %H:%M:%S")


def add_jpeg_metadata(image_path: Path, memory: Memory) -> bool:
    try:
        import piexif

        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        try:
            exif_dict = piexif.load(str(image_path))
        except Exception:
            pass

        dt_str = memory.date.strftime("%Y:%m:%d %H:%M:%S")
        exif_dict["0th"][piexif.ImageIFD.DateTime] = dt_str
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_str
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_str

        if memory.latitude is not None and memory.longitude is not None:
            exif_dict["GPS"][piexif.GPSIFD.GPSVersionID] = (2, 3, 0, 0)
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = "N" if memory.latitude >= 0 else "S"
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = decimal_to_dms_rational(memory.latitude)
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = "E" if memory.longitude >= 0 else "W"
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = decimal_to_dms_rational(memory.longitude)
            exif_dict["GPS"][piexif.GPSIFD.GPSDateStamp] = memory.date.strftime("%Y:%m:%d")
            exif_dict["GPS"][piexif.GPSIFD.GPSTimeStamp] = (
                (memory.date.hour, 1),
                (memory.date.minute, 1),
                (memory.date.second, 1),
            )

        piexif.insert(piexif.dump(exif_dict), str(image_path))
        return True
    except Exception:
        return False


def add_mp4_metadata(video_path: Path, memory: Memory) -> bool:
    if EXIFTOOL_PATH:
        return add_mp4_metadata_with_exiftool(video_path, memory)

    try:
        from mutagen.mp4 import MP4

        video = MP4(str(video_path))
        if video.tags is None:
            video.add_tags()

        video.tags["\xa9day"] = [memory.date.strftime("%Y-%m-%dT%H:%M:%SZ")]

        if memory.latitude is not None and memory.longitude is not None:
            video.tags["\xa9xyz"] = [format_iso6709(memory.latitude, memory.longitude)]

        video.save()
        return True
    except Exception:
        return False


def add_mp4_metadata_with_exiftool(video_path: Path, memory: Memory) -> bool:
    try:
        command = [
            EXIFTOOL_PATH,
            "-overwrite_original",
            "-P",
            f"-Keys:CreationDate={format_exiftool_datetime(memory.date)}",
            f"-UserData:DateTimeOriginal={format_exiftool_datetime(memory.date)}",
            f"-QuickTime:CreateDate={format_quicktime_header_datetime(memory.date)}",
            f"-QuickTime:ModifyDate={format_quicktime_header_datetime(memory.date)}",
            f"-QuickTime:TrackCreateDate={format_quicktime_header_datetime(memory.date)}",
            f"-QuickTime:TrackModifyDate={format_quicktime_header_datetime(memory.date)}",
            f"-QuickTime:MediaCreateDate={format_quicktime_header_datetime(memory.date)}",
            f"-QuickTime:MediaModifyDate={format_quicktime_header_datetime(memory.date)}",
        ]

        if memory.latitude is not None and memory.longitude is not None:
            iso6709 = format_iso6709(memory.latitude, memory.longitude)
            command.extend(
                [
                    f"-Keys:GPSCoordinates={iso6709}",
                    f"-ItemList:GPSCoordinates={iso6709}",
                ]
            )

        command.append(str(video_path))

        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def add_media_metadata(media_path: Path, memory: Memory) -> bool:
    if media_path.suffix.lower() in {".jpg", ".jpeg"}:
        return add_jpeg_metadata(media_path, memory)
    if media_path.suffix.lower() == ".mp4":
        return add_mp4_metadata(media_path, memory)
    return False


def find_existing_media(output_dir: Path, memory: Memory) -> Optional[Path]:
    for candidate in sorted(output_dir.glob(f"{memory.output_name}.*")):
        if candidate.is_file():
            return candidate
    return None


def parse_media_id_from_local_filename(file_path: Path) -> str:
    match = re.match(
        r"^\d{4}-\d{2}-\d{2}_(.+?)-(main|overlay)\.[^.]+$",
        file_path.name,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return match.group(1)


def build_local_media_index(source_root: Path) -> dict[str, Path]:
    media_index: dict[str, Path] = {}
    for file_path in source_root.rglob("*-main.*"):
        if not file_path.is_file():
            continue
        media_id = parse_media_id_from_local_filename(file_path)
        if not media_id:
            continue
        media_index.setdefault(media_id, file_path)
    return media_index


def ensure_disk_space(output_dir: Path):
    target_dir = output_dir if output_dir.exists() else output_dir.parent
    usage = shutil.disk_usage(target_dir)
    free_gb = usage.free / (1024 ** 3)

    print(
        f"Disk space check: {free_gb:.1f} GiB free on {target_dir}"
    )

    if free_gb < MIN_FREE_SPACE_GB:
        raise RuntimeError(
            f"Only {free_gb:.1f} GiB free. Free up space before downloading memories."
        )

    if free_gb < RECOMMENDED_FREE_SPACE_GB:
        print(
            f"Warning: only {free_gb:.1f} GiB free. A full export may be tight; "
            "consider freeing up more space before a clean run."
        )


def write_failed_download_log(output_dir: Path, failed_downloads: list[tuple[Memory, str]]):
    if not failed_downloads:
        return

    log_path = output_dir / "failed_downloads.txt"
    with open(log_path, "w", encoding="utf-8") as log_file:
        for memory, error in failed_downloads:
            log_file.write(f"{memory.output_name}\t{error}\n")

    print(f"Failed download log written to {log_path}")


def write_failed_import_log(output_dir: Path, failed_imports: list[tuple[Memory, str]]):
    if not failed_imports:
        return

    log_path = output_dir / "failed_local_imports.txt"
    with open(log_path, "w", encoding="utf-8") as log_file:
        for memory, error in failed_imports:
            log_file.write(f"{memory.output_name}\t{error}\n")

    print(f"Failed local import log written to {log_path}")


async def download_memory(
    memory: Memory,
    output_dir: Path,
    add_exif: bool,
    semaphore: asyncio.Semaphore,
    client: Any,
    pacer: RequestPacer,
    max_retries: int,
) -> tuple[bool, int, Optional[str]]:
    async with semaphore:
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                if memory.media_download_url.strip():
                    cdn_url = memory.media_download_url.strip()
                else:
                    await pacer.wait_turn()
                    cdn_url = await get_cdn_url(client, memory.download_link)

                ext = extension_from_url(cdn_url)
                output_path = output_dir / f"{memory.output_name}{ext}"

                try:
                    await pacer.wait_turn()
                    response = await client.get(cdn_url)
                    response.raise_for_status()
                except Exception:
                    if memory.media_download_url:
                        await pacer.wait_turn()
                        cdn_url = await get_cdn_url(client, memory.download_link)
                        ext = extension_from_url(cdn_url)
                        output_path = output_dir / f"{memory.output_name}{ext}"
                        await pacer.wait_turn()
                        response = await client.get(cdn_url)
                        response.raise_for_status()
                    else:
                        raise

                output_path.write_bytes(response.content)

                timestamp = memory.date.timestamp()
                os.utime(output_path, (timestamp, timestamp))

                if add_exif:
                    add_media_metadata(output_path, memory)

                return True, len(response.content), None
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries or not is_retryable_exception(exc):
                    break
                await pacer.penalize(penalty_seconds_for_exception(exc, attempt))
                await asyncio.sleep(backoff_seconds(attempt))

        print(f"\nError: {last_error}")
        return False, 0, str(last_error)


def repair_existing_metadata(memories: list[Memory], output_dir: Path) -> Stats:
    stats = Stats()
    for memory in memories:
        media_path = find_existing_media(output_dir, memory)
        if media_path is None:
            stats.skipped += 1
            continue

        timestamp = memory.date.timestamp()
        os.utime(media_path, (timestamp, timestamp))

        if add_media_metadata(media_path, memory):
            stats.repaired += 1
        else:
            stats.failed += 1

    return stats


def import_local_media(
    memories: list[Memory],
    source_root: Path,
    output_dir: Path,
    add_exif: bool,
    skip_existing: bool,
):
    output_dir.mkdir(parents=True, exist_ok=True)
    ensure_disk_space(output_dir)

    if not source_root.exists():
        raise FileNotFoundError(f"Local media source folder not found: {source_root}")

    media_index = build_local_media_index(source_root)
    print(
        f"Local import source: {source_root} ({len(media_index)} main media files found)"
    )

    stats = Stats()
    failed_imports: list[tuple[Memory, str]] = []
    start_time = time.time()
    progress_bar = tqdm(
        total=len(memories),
        desc="Importing",
        unit="file",
        disable=False,
    )

    for memory in memories:
        existing_media = find_existing_media(output_dir, memory)
        if skip_existing and existing_media is not None:
            stats.skipped += 1
            progress_bar.update(1)
            continue

        source_path = media_index.get(memory.media_id)
        if source_path is None:
            stats.failed += 1
            failed_imports.append((memory, "No matching local media file found"))
            progress_bar.update(1)
            continue

        output_path = output_dir / f"{memory.output_name}{source_path.suffix.lower()}"

        try:
            shutil.copy2(source_path, output_path)
            timestamp = memory.date.timestamp()
            os.utime(output_path, (timestamp, timestamp))

            if add_exif:
                add_media_metadata(output_path, memory)

            stats.imported += 1
            stats.mb += output_path.stat().st_size / 1024 / 1024
        except Exception as exc:
            stats.failed += 1
            failed_imports.append((memory, str(exc)))

        elapsed = time.time() - start_time
        mb_per_sec = stats.mb / elapsed if elapsed > 0 else 0
        progress_bar.set_postfix({"MB/s": f"{mb_per_sec:.2f}"}, refresh=False)
        progress_bar.update(1)

    progress_bar.close()
    write_failed_import_log(output_dir, failed_imports)
    print(
        f"\n{'='*50}\nImported: {stats.imported} ({stats.mb:.1f} MB) | Skipped: {stats.skipped} | Failed: {stats.failed}\n{'='*50}"
    )


async def download_all(
    memories: list[Memory],
    output_dir: Path,
    max_concurrent: int,
    add_exif: bool,
    skip_existing: bool,
    request_delay: float,
    request_jitter: float,
    max_retries: int,
):
    output_dir.mkdir(parents=True, exist_ok=True)
    ensure_disk_space(output_dir)
    print(
        f"Run settings: concurrency={max_concurrent}, delay={request_delay:.1f}s, "
        f"jitter={request_jitter:.1f}s, retries={max_retries}"
    )
    semaphore = asyncio.Semaphore(max_concurrent)
    pacer = RequestPacer(request_delay, request_jitter)
    stats = Stats()
    start_time = time.time()
    failed_downloads: list[tuple[Memory, str]] = []

    to_download = []
    for memory in memories:
        jpg_path = output_dir / f"{memory.output_name}.jpg"
        mp4_path = output_dir / f"{memory.output_name}.mp4"
        if skip_existing and (jpg_path.exists() or mp4_path.exists()):
            stats.skipped += 1
        else:
            to_download.append(memory)

    if not to_download:
        print("All files already downloaded!")
        return

    progress_bar = tqdm(
        total=len(to_download),
        desc="Downloading",
        unit="file",
        disable=False,
    )

    import httpx

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers=REQUEST_HEADERS,
    ) as client:
        async def process_and_update(memory):
            success, bytes_downloaded, error_message = await download_memory(
                memory,
                output_dir,
                add_exif,
                semaphore,
                client,
                pacer,
                max_retries,
            )
            if success:
                stats.downloaded += 1
            else:
                stats.failed += 1
                if error_message:
                    failed_downloads.append((memory, error_message))
            stats.mb += bytes_downloaded / 1024 / 1024

            elapsed = time.time() - start_time
            mb_per_sec = (stats.mb) / elapsed if elapsed > 0 else 0
            progress_bar.set_postfix({"MB/s": f"{mb_per_sec:.2f}"}, refresh=False)
            progress_bar.update(1)

        await asyncio.gather(*[process_and_update(m) for m in to_download])

    progress_bar.close()
    write_failed_download_log(output_dir, failed_downloads)
    elapsed = time.time() - start_time
    mb_total = stats.mb
    mb_per_sec = mb_total / elapsed if elapsed > 0 else 0
    print(
        f"\n{'='*50}\nDownloaded: {stats.downloaded} ({mb_total:.1f} MB @ {mb_per_sec:.2f} MB/s) | Skipped: {stats.skipped} | Failed: {stats.failed}\n{'='*50}"
    )


async def main():
    parser = argparse.ArgumentParser(
        description="Download Snapchat memories from data export"
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        default="memories/json/memories_history.json",
        help="Path to memories_history.json",
    )
    parser.add_argument(
        "-o", "--output", default="./downloads", help="Output directory"
    )
    parser.add_argument(
        "--import-local-media",
        nargs="?",
        const="raw-zips",
        default=None,
        help="Import already-extracted local media files instead of downloading (defaults to ./raw-zips)",
    )
    parser.add_argument(
        "-c",
        "--concurrent",
        type=int,
        default=DEFAULT_CONCURRENT,
        help="Max concurrent downloads",
    )
    parser.add_argument(
        "-r",
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Retries for transient download failures",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY,
        help="Base delay in seconds between starting requests",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=DEFAULT_REQUEST_JITTER,
        help="Extra random delay added on top of --delay",
    )
    parser.add_argument(
        "--no-exif",
        action="store_true",
        help="Disable photo/video metadata writing",
    )
    parser.add_argument(
        "--no-skip-existing", action="store_true", help="Re-download existing files"
    )
    parser.add_argument(
        "--repair-existing",
        action="store_true",
        help="Rewrite metadata for files already present in the output directory",
    )
    args = parser.parse_args()

    json_path = resolve_json_path(args.json_file)
    output_dir = Path(args.output)

    memories = load_memories(json_path)

    if args.repair_existing:
        stats = repair_existing_metadata(memories, output_dir)
        print(
            f"\n{'='*50}\nRepaired: {stats.repaired} | Missing: {stats.skipped} | Failed: {stats.failed}\n{'='*50}"
        )
        return

    if args.import_local_media is not None:
        import_local_media(
            memories,
            Path(args.import_local_media),
            output_dir,
            not args.no_exif,
            not args.no_skip_existing,
        )
        return

    await download_all(
        memories,
        output_dir,
        args.concurrent,
        not args.no_exif,
        not args.no_skip_existing,
        args.delay,
        args.jitter,
        args.retries,
    )


if __name__ == "__main__":
    asyncio.run(main())
