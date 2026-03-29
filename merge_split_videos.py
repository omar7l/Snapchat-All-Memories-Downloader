import argparse
import functools
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from main import add_mp4_metadata, load_memories

FFMPEG_PATH = shutil.which("ffmpeg")
EXIFTOOL_PATH = shutil.which("exiftool")
STRICT_TWO_PART_MIN_DURATION = 8.5


def location_distance(memory_a, memory_b) -> float:
    if None in (memory_a.latitude, memory_a.longitude, memory_b.latitude, memory_b.longitude):
        return math.inf
    return math.hypot(
        memory_a.latitude - memory_b.latitude,
        memory_a.longitude - memory_b.longitude,
    )


@functools.lru_cache(maxsize=None)
def get_duration_seconds(video_path: Path) -> Optional[float]:
    if EXIFTOOL_PATH is None:
        return None

    result = subprocess.run(
        [EXIFTOOL_PATH, "-n", "-s3", "-Duration", str(video_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    if not value:
        return None

    try:
        return float(value)
    except ValueError:
        return None


def is_split_continuation(previous_memory, current_memory, previous_path: Path, current_path: Path) -> bool:
    delta_seconds = (current_memory.date - previous_memory.date).total_seconds()
    if not 9 <= delta_seconds <= 11:
        return False

    if location_distance(previous_memory, current_memory) > 0.0015:
        return False

    previous_duration = get_duration_seconds(previous_path)
    current_duration = get_duration_seconds(current_path)
    if previous_duration is None or current_duration is None:
        return True

    return 8.0 <= previous_duration <= 11.5 and 8.0 <= current_duration <= 11.5


def is_strong_two_part_chain(chain) -> bool:
    if len(chain) != 2:
        return True

    first_memory, first_path = chain[0]
    second_memory, second_path = chain[1]

    if None in (
        first_memory.latitude,
        first_memory.longitude,
        second_memory.latitude,
        second_memory.longitude,
    ):
        return False

    if (
        abs(first_memory.latitude) < 1e-9
        and abs(first_memory.longitude) < 1e-9
        and abs(second_memory.latitude) < 1e-9
        and abs(second_memory.longitude) < 1e-9
    ):
        return False

    first_duration = get_duration_seconds(first_path)
    second_duration = get_duration_seconds(second_path)
    if first_duration is None or second_duration is None:
        return False

    return min(first_duration, second_duration) >= STRICT_TWO_PART_MIN_DURATION


def classify_chain(chain) -> str:
    if len(chain) >= 3:
        return "strong"
    if is_strong_two_part_chain(chain):
        return "strong"
    return "weak"


def collect_video_chains(memories: list, media_dir: Path, min_parts: int, match_text: str = ""):
    videos = []
    for memory in memories:
        if not memory.output_name:
            continue
        if match_text and match_text not in memory.output_name:
            continue
        video_path = media_dir / f"{memory.output_name}.mp4"
        if video_path.exists():
            videos.append((memory, video_path))

    videos.sort(key=lambda item: item[0].date)

    chains = []
    current_chain = []
    for memory, video_path in videos:
        if not current_chain:
            current_chain = [(memory, video_path)]
            continue

        previous_memory, previous_path = current_chain[-1]
        if is_split_continuation(previous_memory, memory, previous_path, video_path):
            current_chain.append((memory, video_path))
        else:
            if len(current_chain) >= min_parts:
                chains.append(current_chain)
            current_chain = [(memory, video_path)]

    if len(current_chain) >= min_parts:
        chains.append(current_chain)

    return chains


def filter_chains(chains, match_text: str):
    if not match_text:
        return chains

    filtered = []
    for chain in chains:
        names = [memory.output_name for memory, _ in chain]
        haystack = " ".join(names)
        if match_text in haystack:
            filtered.append(chain)
    return filtered


def merge_chain(chain, output_path: Path):
    if FFMPEG_PATH is None:
        raise RuntimeError("ffmpeg is not installed. Install it first to merge split videos.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    first_memory, _ = chain[0]

    with tempfile.TemporaryDirectory() as temp_dir:
        concat_list_path = Path(temp_dir) / "concat.txt"
        with open(concat_list_path, "w", encoding="utf-8") as concat_file:
            for _, video_path in chain:
                concat_file.write(f"file '{video_path.resolve()}'\n")

        result = subprocess.run(
            [
                FFMPEG_PATH,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
                "-c",
                "copy",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg concat failed")

    add_mp4_metadata(output_path, first_memory)
    return output_path


def replace_strong_chain(chain, media_dir: Path, backup_dir: Path) -> Path:
    first_memory, first_path = chain[0]
    replaced_output_path = media_dir / first_path.name
    temp_output_path = media_dir / f".{first_memory.output_name}_merged_tmp.mp4"

    merge_chain(chain, temp_output_path)

    backup_dir.mkdir(parents=True, exist_ok=True)
    for _, video_path in chain:
        target = backup_dir / video_path.name
        if target.exists():
            target.unlink()
        shutil.move(str(video_path), str(target))

    os.replace(temp_output_path, replaced_output_path)
    return replaced_output_path


def main():
    parser = argparse.ArgumentParser(
        description="Detect and merge Snapchat videos that were exported as 10-second chunks"
    )
    parser.add_argument(
        "json_file",
        help="Path to memories_history.json",
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        default="imported_memories",
        help="Directory containing imported MP4 files",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="merged_videos",
        help="Directory for merged weak-review output videos",
    )
    parser.add_argument(
        "--min-parts",
        type=int,
        default=3,
        help="Minimum number of consecutive 10-second parts before treating a sequence as a split video",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print candidate chains without merging",
    )
    parser.add_argument(
        "--match",
        default="",
        help="Only include chains whose filenames contain this text",
    )
    parser.add_argument(
        "--replace-strong",
        action="store_true",
        help="For strong chains, replace the first clip in the input directory with the merged result and move originals to backup",
    )
    parser.add_argument(
        "--backup-dir",
        default="split_video_parts_backup",
        help="Directory where original strong-chain clips are moved when --replace-strong is used",
    )
    args = parser.parse_args()

    memories = load_memories(Path(args.json_file))
    chains = collect_video_chains(memories, Path(args.input_dir), args.min_parts, args.match)
    chains = filter_chains(chains, args.match)

    if not chains:
        print("No split-video candidates found.")
        return

    strong_chains = [chain for chain in chains if classify_chain(chain) == "strong"]
    weak_chains = [chain for chain in chains if classify_chain(chain) == "weak"]

    print(
        f"Found {len(chains)} candidate split-video chains "
        f"({len(strong_chains)} strong, {len(weak_chains)} weak)."
    )
    for chain in chains:
        first_memory, _ = chain[0]
        last_memory, _ = chain[-1]
        label = classify_chain(chain)
        print(
            f"[{label}] {first_memory.output_name} -> {last_memory.output_name} "
            f"({len(chain)} parts)"
        )

    if args.dry_run:
        return

    replaced = 0
    review_merged = 0

    if args.replace_strong:
        for chain in strong_chains:
            output_path = replace_strong_chain(
                chain,
                Path(args.input_dir),
                Path(args.backup_dir),
            )
            replaced += 1
            print(f"Replaced strong chain -> {output_path}")
    else:
        for chain in strong_chains:
            first_memory, _ = chain[0]
            output_path = merge_chain(
                chain,
                Path(args.output_dir) / f"{first_memory.output_name}_merged.mp4",
            )
            replaced += 1
            print(f"Merged strong chain -> {output_path}")

    for chain in weak_chains:
        first_memory, _ = chain[0]
        output_path = merge_chain(
            chain,
            Path(args.output_dir) / f"{first_memory.output_name}_merged.mp4",
        )
        review_merged += 1
        print(f"Merged weak review copy -> {output_path}")

    if args.replace_strong:
        print(
            f"\nReplaced {replaced} strong chains in {args.input_dir}, "
            f"backed up originals to {args.backup_dir}, and wrote {review_merged} weak review merges to {args.output_dir}"
        )
    else:
        print(
            f"\nMerged {replaced} strong chains and {review_merged} weak review chains into {args.output_dir}"
        )


if __name__ == "__main__":
    main()
