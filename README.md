# Snapchat Memories Toolkit
Import a Snapchat data export into a clean local library with:
- Apple Photos-friendly date and location metadata for images and videos
- support for split Snapchat export zips
- optional merging of videos that Snapchat exported as 10-second chunks

![demo](./demo.gif)

## What To Request From Snapchat
When requesting your export from [Snapchat Download My Data](https://accounts.snapchat.com/accounts/downloadmydata), select:
- `Export your Memories`
- `Export JSON Files`

Those two options are the important part.

## How It Works
Snapchat exports often come as:
- one folder containing `json/memories_history.json`
- one or more `memories` folders or zip parts containing the actual `*-main.jpg` / `*-main.mp4` files

This project does not just rely on signed download links. If your export already contains the real media files, it can import them directly, which is the safest path.

## Quick Start
1. Clone this repo.
2. Put your extracted Snapchat export folders in the repo, for example under `raw-zips/`.
3. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

4. Import the media into a clean output folder:

```bash
python3 main.py raw-zips/mydata~YOUR_EXPORT/json/memories_history.json -o imported_memories --import-local-media raw-zips
```

That command:
- scans the split export folders recursively
- finds the actual local media files
- copies them into `imported_memories/`
- writes Apple-friendly metadata onto the copied files

## Merge Split Videos
Older Snapchat videos are sometimes exported as a sequence of 10-second clips. This repo includes a helper to detect and merge those chains.

Preview candidate chains:

```bash
python3 merge_split_videos.py raw-zips/mydata~YOUR_EXPORT/json/memories_history.json -i imported_memories --dry-run --min-parts 2
```

Bulk merge them:

```bash
python3 merge_split_videos.py raw-zips/mydata~YOUR_EXPORT/json/memories_history.json -i imported_memories -o merged_videos --min-parts 2 --replace-strong
```

With that command:
- strong chains are merged back into `imported_memories/`
- original split parts are moved into `split_video_parts_backup/`
- weaker 2-part candidates are written as review copies into `merged_videos/`

## Apple Photos Metadata
The importer writes:
- JPEG EXIF date and GPS metadata
- MP4 QuickTime-compatible date and GPS metadata

For best MP4 compatibility with Apple Photos, install `exiftool`:

```bash
brew install exiftool
```

For split-video merging, install `ffmpeg`:

```bash
brew install ffmpeg
```

## Optional Download Mode
If your export does not include the actual `*-main.*` media files and only includes JSON plus signed URLs, the original downloader flow still exists:

```bash
python3 main.py memories/json/memories_history.json -o downloads
```

There are pacing and retry controls available through `main.py --help`, but the local import path is preferred whenever the real media files already exist in the export.

## Commands
Import local media:

```bash
python3 main.py raw-zips/mydata~YOUR_EXPORT/json/memories_history.json -o imported_memories --import-local-media raw-zips
```

Repair metadata on already-imported files:

```bash
python3 main.py raw-zips/mydata~YOUR_EXPORT/json/memories_history.json -o imported_memories --repair-existing
```

Preview split-video candidates:

```bash
python3 merge_split_videos.py raw-zips/mydata~YOUR_EXPORT/json/memories_history.json -i imported_memories --dry-run --min-parts 2
```

Merge split videos:

```bash
python3 merge_split_videos.py raw-zips/mydata~YOUR_EXPORT/json/memories_history.json -i imported_memories -o merged_videos --min-parts 2 --replace-strong
```

## Notes
- Some memories can share the same timestamp, so the importer uses collision-safe filenames.
- Split export parts are fine; point the JSON path at the export root that has `json/memories_history.json`, and point `--import-local-media` at the parent folder containing all extracted `memories` directories.
- Local export folders and output folders are ignored by Git in this repo.
