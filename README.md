# Snapchat Memories Toolkit

Fix a Snapchat export so your photos and videos keep the correct date, time, and location metadata, especially in Apple Photos.

This project exists because I ran into the exact same mess a lot of other people describe online:
- Snapchat exports split across multiple ZIP files
- photos importing with today's date instead of the real capture date
- missing location metadata in Apple Photos
- videos split into 10-second chunks
- confusing JSON files and unclear folder structure

I tried other workflows first. They were either confusing, broken, or assumed you had to download everything again from Snapchat. For my export, that was the wrong path. As of March 2026, the simplest workflow was:
- request the right Snapchat export
- unzip all parts
- import the local media files already inside the export
- write proper metadata back into the copied files

If you are searching for any of these, you are in the right place:
- how to export Snapchat memories with correct metadata
- Snapchat export wrong date in Apple Photos
- Snapchat memories missing location metadata
- Snapchat export JSON files
- Snapchat split ZIP export memories
- Snapchat videos split into 10 second clips

## What This Tool Does

This toolkit can:
- import Snapchat Memories from the exported ZIP parts you already downloaded
- restore correct date, time, and GPS metadata to images and videos
- make imports behave much better in Apple Photos
- keep filenames collision-safe when multiple memories share the same timestamp
- merge Snapchat videos that were exported as multiple 10-second clips

## What To Select In Snapchat

When requesting your data export from [Snapchat Download My Data](https://accounts.snapchat.com/accounts/downloadmydata), select:
- `Export your Memories`
- `Export JSON Files`

Those are the two important options.

## What You Will Download From Snapchat

Snapchat may give you:
- one ZIP file
- or multiple ZIP files split into parts, often because the export is large

After unzipping, you will usually end up with something like this:

```text
snapchat-export/
  mydata~123456789/
    json/
      memories_history.json
    html/
    memories/
  memories/
  memories 2/
  memories 3/
  memories 4/
```

This looks weird, but it is normal.

The important idea is:
- one folder contains `json/memories_history.json`
- the actual photo and video files may be spread across several `memories` folders

For many exports, the media is already there locally as `*-main.jpg` and `*-main.mp4`.

That means you do **not** need to download everything again from Snapchat.

## The Simple Guide

This is the easiest path and the one I recommend.

### 1. Clone the repo

```bash
git clone https://github.com/omar7l/Snapchat-All-Memories-Downloader.git
cd Snapchat-All-Memories-Downloader
```

### 2. Put your Snapchat export parts into one place

Create a folder such as `snapchat-export/` in the repo and put all downloaded Snapchat ZIP files there.

Then unzip all of them.

On Mac:
- you can usually double-click each ZIP file
- if Snapchat gave you multiple parts, unzip all of them into the same parent folder

After unzipping, you should have:
- one extracted folder that contains `json/memories_history.json`
- one or more extracted folders containing the actual `memories` media files

Do not worry if the folder names look inconsistent. That is common with Snapchat exports.

### 3. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

### 4. Import the local media files from the export

Run this:

```bash
python3 main.py snapchat-export/mydata~YOUR_EXPORT/json/memories_history.json -o imported_memories --import-local-media snapchat-export
```

What this does:
- scans all extracted export folders under `snapchat-export`
- finds the real Snapchat media files already inside the export
- copies them into `imported_memories/`
- writes proper date, time, and location metadata onto the copied files

When it finishes, `imported_memories/` is the folder you actually want to import into Apple Photos or keep as your clean archive.

## Why Apple Photos Shows The Wrong Date For Snapchat Exports

This is one of the biggest problems people search for.

A raw Snapchat export often does **not** store the useful metadata directly inside the image or video in the way Apple Photos expects. The date and location can be present in `memories_history.json`, but Apple Photos does not organize your library based on that JSON file.

This toolkit fixes that by writing:
- JPEG EXIF date and GPS metadata
- MP4 QuickTime-compatible date and GPS metadata

That makes Apple Photos much more likely to show the real capture date and location correctly.

For best MP4 compatibility on macOS, install `exiftool`:

```bash
brew install exiftool
```

## How To Import Snapchat Memories Into Apple Photos

Once `imported_memories/` has been created:
- open Apple Photos
- import from `imported_memories/`

If you previously imported the broken raw export into Apple Photos:
- remove those bad imports first
- then re-import the fixed files from `imported_memories/`

Otherwise Apple Photos may keep showing the old metadata.

## Why Snapchat Videos Are Split Into 10-Second Clips

Another common problem is that one Snapchat video gets exported as multiple short clips like:

```text
2018-04-29_16-10-01.mp4
2018-04-29_16-10-11.mp4
2018-04-29_16-10-21.mp4
```

This repo includes a helper script to detect and merge those.

Install `ffmpeg` first:

```bash
brew install ffmpeg
```

Preview candidate chains:

```bash
python3 merge_split_videos.py snapchat-export/mydata~YOUR_EXPORT/json/memories_history.json -i imported_memories --dry-run --min-parts 2
```

Bulk merge them:

```bash
python3 merge_split_videos.py snapchat-export/mydata~YOUR_EXPORT/json/memories_history.json -i imported_memories -o merged_videos --min-parts 2 --replace-strong
```

What that does:
- strong split-video chains are merged back into `imported_memories/`
- original parts are moved to `split_video_parts_backup/`
- weaker 2-part candidates are written as extra review copies to `merged_videos/`

## What If My Export Only Has JSON And No Local Media Files?

Some Snapchat exports are different.

If your export does **not** include local `*-main.jpg` and `*-main.mp4` files, and only includes JSON plus signed download links, the old downloader mode still exists:

```bash
python3 main.py memories/json/memories_history.json -o downloads
```

But if your export already contains the real media files, local import is simpler and better.

## Common Questions

### Does this fix the wrong date in Apple Photos?

That is exactly one of the main goals.

### Does this restore location metadata?

Yes, when the location exists in the Snapchat export JSON.

### Does this work with split Snapchat ZIP exports?

Yes. That is one of the main use cases.

### Do I need to download the media again from Snapchat?

Usually no, if your extracted export already contains the `*-main.*` media files.

### Why are there multiple `memories` folders after unzipping?

Because Snapchat often splits large exports into multiple parts. That is normal.

### Why do some videos get broken into 10-second files?

Because Snapchat sometimes exports longer videos as a chain of short clips. Use `merge_split_videos.py` to merge them back together.

## Commands

Import local media from the extracted export:

```bash
python3 main.py snapchat-export/mydata~YOUR_EXPORT/json/memories_history.json -o imported_memories --import-local-media snapchat-export
```

Repair metadata on files you already imported:

```bash
python3 main.py snapchat-export/mydata~YOUR_EXPORT/json/memories_history.json -o imported_memories --repair-existing
```

Preview split-video candidates:

```bash
python3 merge_split_videos.py snapchat-export/mydata~YOUR_EXPORT/json/memories_history.json -i imported_memories --dry-run --min-parts 2
```

Merge split videos:

```bash
python3 merge_split_videos.py snapchat-export/mydata~YOUR_EXPORT/json/memories_history.json -i imported_memories -o merged_videos --min-parts 2 --replace-strong
```

Fallback downloader mode:

```bash
python3 main.py memories/json/memories_history.json -o downloads
```

## Notes

- Some memories share the same timestamp, so this project uses collision-safe filenames.
- Local export folders and output folders are ignored by Git in this repo.
- The local import path is the recommended path whenever the export already contains the actual media files.
