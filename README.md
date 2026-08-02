# MEGA to Google Drive Transfer

Multi-folder file transfer system from MEGA to Google Drive using GitHub Actions. Uses **git-based state tracking** to survive crashes, **smart quota management** to handle MEGA's 5GB/day bandwidth limit, and an **oversized processor** that chunks files >5GB across multiple runs with AES-128-CTR decryption and ffprobe verification.

---

## Required GitHub Secrets

Workflow chalane ke liye **3 GitHub Secrets** banane padenge. Inke bina workflow fail hoga.

| Secret Name | Required? | Kya Hai? | Kaise Milega? |
|-------------|-----------|----------|---------------|
| `MEGA_LINKS` | Required | Aapki MEGA files ka JSON — `{"Folder":["url1","url2"]}` | **Web Tool se banao** (neeche link hai) ya manually |
| `RCLONE_CONF` | Required | rclone config file content | `rclone config show gdrive` command se |
| `GH_PAT` | Required | GitHub Personal Access Token | GitHub → Settings → Developer settings → Personal access tokens |

> **Important:** `GH_PAT` token ke liye **`repo`** aur **`workflow`** permissions select karna zaroori hai.

### MEGA_LINKS Banane Ka Easy Tarika

Manual JSON banana mushkil hai. Isliye yeh **free web tool** bana diya hai:

**[https://megajsonlinksgenerator-kappa.vercel.app/](https://megajsonlinksgenerator-kappa.vercel.app/)**

**Kaise use karein:**
1. Link kholo — **Folder Name** daalo (e.g. "Movies")
2. **MEGA links** paste karo textarea mein (har line par ek URL)
3. **"+ Add Folder"** click karo
4. Jitne folders chahein utne add karo
5. **"Generate Secret"** click karo — minified JSON ban jayega
6. **Copy** karo → GitHub Secret `MEGA_LINKS` mein paste karo
7. **(Bonus)** **Reset State** — `{"folders": {}}` copy karein → `completed_links.json` reset karne ke liye

---

## Table of Contents

- [Required Secrets](#required-github-secrets)
- [How It Works](#how-it-works)
- [Oversized Video Processing](#oversized-video-processing)
- [Complete Flow Diagram](#complete-flow-diagram)
- [Artifact System Explained](#artifact-system-explained)
- [Chunks History File Structure](#chunks-history-file-structure)
- [MEGA Encryption & Key Derivation](#mega-encryption--key-derivation)
- [Features](#features)
- [Setup Guide](#setup-guide)
- [Secret Formats](#secret-formats)
- [How Quota Is Managed](#how-quota-is-managed)
- [Folder Auto-Advance](#folder-auto-advance)
- [Upload Strategy](#upload-strategy)
- [Log Output Examples](#log-output-examples)
- [Troubleshooting](#troubleshooting)
- [Files](#files)

---

## How It Works

### The Problem

MEGA free accounts have a **~5GB daily download quota**. GitHub Actions runners are ephemeral — every run gets a fresh VM with a new IP, which means **fresh 5GB quota every run**. The system exploits this to transfer large amounts of data across multiple runs.

### The Solution

We use **git** to maintain a persistent `completed_links.json` file that tracks:

- Which **files** are already uploaded (per URL)
- Which **folders** are active/completed/pending
- Which **folder** is currently being processed
- Which **oversized files** (>5GB) need chunked handling

State is saved to git after each folder completion or run end. GitHub Actions Artifacts are used as backup for `completed_links.json` and for storing oversized chunk `.bin` files across runs.

### Key Design

| Aspect | How It Works |
|--------|-------------|
| **Metadata fetch** | `megadl --info` first (CLI, fast). Falls back to `mega.py` Python library (`get_public_url_info()`) if megadl fails |
| **Download** | `megadl --path` only — with 600s timeout. `mega.py` `download_url()` removed (hangs on broken links) |
| **Upload verify** | No post-upload verify — rclone copy always succeeds or raises error. Upload failure → skip to next file |
| **State save** | Git commit + push at **run end** and **folder completion** (not per-file). Artifact uploaded as backup |
| **Auto-trigger** | Only on `success()` or `failure()`, not on cancellation. `auto_trigger.py` calls `gh workflow run` if files remain |
| **Inline Python** | Standalone `auto_trigger.py` file (not inline `python -c` in YAML) |
| **Schedule** | Cron `*/15 * * * *` + auto-trigger (dual mechanism) |
| **Source of links** | Reads `MEGA_LINKS` **GitHub Secret** — updating repo file `MEGA_LINKS_merged.json` has no effect |
| **Download logging** | Clean `DOWNLOADING...` / `Downloaded: X MB in Ys` — megadl stdout captured but not printed |

---

## Oversized Video Processing

### Problem: Files >5GB

MEGA free accounts have a **~5GB daily download quota** per IP. Files larger than 5GB can never be downloaded in a single GitHub Actions run. Additionally, MEGA encrypts all files with AES-128 — a direct download from the CDN gives **encrypted garbage**, not the actual video.

### Solution: Chunked Download + Decrypt + Concat

The oversized processor (`oversized_processor.py`) splits large files into 4.5 GB chunks, downloads each chunk in a separate workflow run, decrypts with the correct key, verifies integrity, concatenates, checks with ffprobe, then uploads to Google Drive.

### Pipeline Overview

```
MEGA CDN            GitHub Artifact          Local VM             Google Drive
encrypted bytes --> chunk_01.enc              chunk_01.bin --+-->
                    (stored 90 days)   -->    (decrypted)    |
                                        -->  SHA256 stored   |
                                                            +--> concat > ffprobe > upload
MEGA CDN            GitHub Artifact          chunk_02.bin --+
encrypted bytes --> chunk_02.enc              (decrypted)
```

### Step-by-Step

| Step | What Happens | File |
|------|-------------|------|
| **1. Detect oversized** | `mega_to_gdrive.py` checks if file >5GB, adds to `completed[]` array with `status:"unupload"` and `oversized:true` | `mega_to_gdrive.py` |
| **2. Initialize chunks** | On first run, `oversized_processor.py` finds `status:"unupload"` items, parses MEGA URLs, extracts file ID + key, calculates chunk boundaries, creates `chunks_history.json` | `oversized_processor.py` |
| **3. Download chunk** | Fetches raw encrypted bytes from MEGA CDN via `urllib` with `Range:` header, decrypts with `openssl aes-128-ctr`, saves as `.bin`, computes SHA256 | `download_chunk()` |
| **4. Upload artifact** | Workflow uploads the `.bin` file as a GitHub Artifact (retention: 90 days) | `.github/workflows/mega_gdrive_transfer.yml` |
| **5. Repeat** | Each run processes one chunk per video. Auto-trigger queues next run until all chunks downloaded | `process_chunk_download()` |
| **6. Concat** | Once all chunks are `done`, downloads all artifacts, verifies SHA256 of each, concatenates in order, saves final video | `process_concat_run()` |
| **7. ffprobe check** | Runs `ffprobe -v error -show_entries format=duration` on the concatenated file. If ffprobe rejects it, chunks reset to `pending`, artifacts deleted, re-download | `process_concat_run()` |
| **8. Upload to GDrive** | rclone copies the final video to Google Drive. On success, artifacts deleted, `completed_links.json` entry updated from `"unupload"` to `"uploaded"` | `upload_to_gdrive()` |
| **9. Cleanup** | Chunk artifacts deleted from GitHub, local temp files removed, video marked uploaded in state files | `delete_artifact()` |

### Key Design Decisions

| Decision | Why |
|----------|-----|
| **4.5 GB chunk size** (`CHUNK_MAX = 4831838208`) | Safe margin under 5GB MEGA quota limit per run |
| **AES-128-CTR** | MEGA uses AES-128-CTR for file data (not CBC). CBC is only for metadata attribute decryption |
| **Range requests** | `urllib` with `Range:` header fetches only the chunk's byte range from MEGA CDN — no need to download entire file |
| **SHA256 per chunk** | Stored in `chunks_history.json` when chunk is downloaded. Verified again before concat to detect artifact corruption |
| **ffprobe gate** | Upload only happens if ffprobe can read the concatenated file. If it fails, data corruption is assumed and auto-retry happens |
| **Per-chunk artifact** | Each chunk uploaded as a separate GitHub Artifact (named `ck_{fileid}_{index}`). Survives across workflow runs (90 day retention) |
| **Real-time progress** | Every 2 seconds: `[DOWNLOAD] X MB / Y MB (Z%) @ W MB/s` during chunk download. `[CONCAT] [i/n] X GB / Y GB (Z%)` during concat. `[UPLOAD]` from rclone stats |
| **PROCESSOR_VERSION** | If key derivation logic changes, old chunks are auto-detected and deleted; all chunks re-downloaded with new logic |

### State Files

Two files track oversized progress:

1. **`completed_links.json`** — `completed[]` array contains oversized items with `status:"unupload"`, `oversized:true`. After upload, status changes to `"uploaded"`
2. **`chunks_history.json`** — Per-video: file ID, total size, chunk list (each with `index`, `start_byte`, `end_byte`, `status`, `artifact_name`, `sha256`), overall `summary`

Both files are committed to git after every meaningful state change (per-chunk, per-concat, per-upload).

---

### Main Workflow (Top-Level View)

```
    +------------------------------------------+
    |    MANUAL TRIGGER or AUTO-TRIGGER        |
    |    (Cron */15 + auto-trigger)            |
    +--------------------+---------------------+
                         | Trigger
                         v
    +------------------------------------------+
    |  PHASE 1: SETUP                          |
    |  Install megatools + rclone + Python     |
    |  pip install async-mega.py               |
    |  Install ffmpeg                          |
    +--------------------+---------------------+
                         |
                         v
    +------------------------------------------+
    |  PHASE 2: LOAD STATE                     |
    |  actions/checkout@v4 (git checkout)      |
    |  Reads completed_links.json from repo    |
    |  First run = {"folders":{}}              |
    +--------------------+---------------------+
                         |
                         v
    +------------------------------------------+
    |  PHASE 3: PREPARE                        |
    |  Parse MEGA_LINKS secret (JSON format)   |
    |  Identify active folder                  |
    |  Filter already-completed URLs           |
    |  Separate oversized files (>5GB)         |
    +--------------------+---------------------+
                         |
                         v
    +------------------------------------------+
    |  PHASE 4: MAIN TRANSFER                  |
    |  Run mega_to_gdrive.py                   |
    |  Per file:                               |
    |  - megadl --info (metadata)              |
    |  - Check oversized (>5GB)                |
    |  - Check quota                           |
    |  - megadl --path (download)              |
    |  - rclone copy (upload)                  |
    +--------------------+---------------------+
                         |
            +------------+------------+
            v                         v
       +----------+            +--------------+
       | More     |            | No more      |
       | files?   |--YES------>| files /      |
       |          |            | quota full?  |
       +----------+            +------+-------+
            NO                        |
            |                         v
       +------------------------------------------+
       |  PHASE 5: OVERSIZED PROCESSING           |
       |  Run oversized_processor.py              |
       |  - Download chunks from MEGA CDN         |
       |  - Decrypt with AES-128-CTR              |
       |  - Upload chunk artifacts                |
       |  - OR: Concat + ffprobe + upload         |
       +--------------------+---------------------+
                         |
                         v
    +------------------------------------------+
    |  PHASE 6: COMPLETE RUN                   |
    |  1. Upload artifact (backup)              |
    |  2. Git commit + push (state backup)      |
    |  3. Auto-trigger? Only if not cancelled   |
    |     (if: success() || failure())          |
    |  4. Auto-stop if all folders done         |
    +--------------------+---------------------+
                         |
                         v
    +------------------------------------------+
    |            WORKFLOW END                  |
    +------------------------------------------+
```

### Per-File Processing (Detailed)

When Phase 4 starts, each file goes through these steps:

```
    +-----------------------------------------------------+
    |              START PROCESSING ONE FILE               |
    +---------------------------+---------------------------+
                              |
                              v
    +-----------------------------------------------------+
    |  STEP A: GET FILE INFO                               |
    |  +-----------------------------------------------+  |
    |  | 1. megadl --info <url> (primary, CLI)         |  |
    |  |    Returns: filename + size in ~1-2 sec       |  |
    |  | 2. Fallback: mega.py get_public_url_info()    |  |
    |  |    if megadl fails or returns no size          |  |
    |  +-----------------------------------------------+  |
    +---------------------------+---------------------------+
                              |
                              v
    +-----------------------------------------------------+
    |  STEP B: OVERSIZED CHECK                            |
    |  +-----------------------------------------------+  |
    |  | Is file_size > 5GB?                           |  |
    |  +----------+---------------------+--------------+  |
    |             | YES                 | NO              |
    |             v                     |                 |
    |  +------------------+            |                 |
    |  | Add to completed |            |                 |
    |  | array with       |            |                 |
    |  | status:"unupload"|            |                 |
    |  | oversized:true   |            |                 |
    |  | Skip forever     |            |                 |
    |  +------------------+            |                 |
    +-----------------------------------+-----------------+
                                       | (only if NOT oversized)
                                       v
    +-----------------------------------------------------+
    |  STEP C: QUOTA CHECK                                |
    |  +-----------------------------------------------+  |
    |  | quota_used + file_size > 5GB?                 |  |
    |  +----------+---------------------+--------------+  |
    |             | YES                 | NO              |
    |             v                     v                 |
    |  +------------------+    +--------------------+     |
    |  | Skip this file   |    | quota_used += size |     |
    |  | (break loop)     |    | Start download     |     |
    |  +------------------+    +--------------------+     |
    +-----------------------------------------------------+
                              | (only if quota OK)
                              v
    +-----------------------------------------------------+
    |  STEP D: DOWNLOAD FROM MEGA (via megadl only)       |
    |  +-----------------------------------------------+  |
    |  | megadl --path TEMP_DIR <url>                  |  |
    |  | Timeout: 600s (megadl hangs, runner moves on) |  |
    |  | File saved: TEMP_DIR/<filename>               |  |
    |  +-----------------------------------------------+  |
    +---------------------------+---------------------------+
                              |
                              v
    +-----------------------------------------------------+
    |  STEP E: UPLOAD TO GOOGLE DRIVE                     |
    |  +-----------------------------------------------+  |
    |  | 1. Create folder if not exists:               |  |
    |  |    rclone mkdir gdrive:MEGA_Transfer/Folder   |  |
    |  | 2. Upload file:                               |  |
    |  |    rclone copy <file> <gdrive:/>              |  |
    |  | 3. No verify — upload always succeeds or      |  |
    |  |    raises error (rclone return code != 0)     |  |
    |  +-----------------------------------------------+  |
    +---------------------------+---------------------------+
                              |
                              v
    +-----------------------------------------------------+
    |  STEP F: SAVE STATE                                  |
    |  +-----------------------------------------------+  |
    |  | 1. Append to completed_links.json completed[]:|  |
    |  |    - url, filename, size                      |  |
    |  |    - target_folder, completed_at              |  |
    |  |    - status:"uploaded"                        |  |
    |  | 2. Increment folder done count                |  |
    |  | NOTE: Git push happens at RUN END,            |  |
    |  | not after every file                          |  |
    |  +-----------------------------------------------+  |
    +---------------------------+---------------------------+
                              |
                              v
    +-----------------------------------------------------+
    |  STEP G: CLEANUP & LOG                              |
    |  +-----------------------------------------------+  |
    |  | 1. Delete: TEMP_DIR/*                         |  |
    |  | 2. Print: "5/10 done | Quota: 3.4/5.0 GB"    |  |
    |  +-----------------------------------------------+  |
    +---------------------------+---------------------------+
                              |
                              v
    +-----------------------------------------------------+
    |  Return to "More files?" check in Main Diagram      |
    +-----------------------------------------------------+
```

### Oversized Video Processing (Detailed)

When a file >5GB is detected, it gets added to `completed[]` with `status:"unupload"`. The oversized processor then takes over in the next phase:

```
     +------------------------------------------------------+
     |          OVERSIZED PIPELINE                           |
     |                                                       |
     |  mega_to_gdrive.py detects >5GB file                  |
     |  -> Adds to completed_links.json completed[]           |
     |     with status:"unupload", oversized:true             |
     +--------------------+---------------------------------+
                          |
                          v
     +------------------------------------------------------+
     |  oversized_processor.py INIT PHASE                    |
     |                                                       |
     |  1. Find all status:"unupload" items                 |
     |  2. Parse MEGA URL -> file_id + key_b64url           |
     |  3. Extract AES key: k1^k3, k2^k4 (half-XOR)        |
     |  4. IV = raw[32:48] + counter (16-byte aligned)     |
     |  5. Get download URL from MEGA API (curl POST)       |
     |  6. Calculate chunks (max 4.5 GB each)               |
     |  7. Create chunks_history.json with all state        |
     +--------------------+---------------------------------+
                          |
                          v
     +------------------------------------------------------+
     |  LOOP: One chunk per run (auto-triggered)            |
     |                                                       |
     |  +-----------------------------------------------+   |
     |  | DOWNLOAD PHASE (runs independently per chunk) |   |
     |  | 1. GET MEGA CDN URL (via MEGA API)            |   |
     |  | 2. urllib Range header (byte range request)    |   |
     |  | 3. Save encrypted bytes -> .enc file           |   |
     |  | 4. openssl aes-128-ctr -d -> .bin (decrypted) |   |
     |  | 5. SHA256 of decrypted chunk -> stored         |   |
     |  | 6. Workflow uploads .bin as artifact           |   |
     |  | 7. chunks_history: chunk status = "done"      |   |
     |  | 8. Auto-trigger runs next chunk/video         |   |
     |  +-----------------------------------------------+   |
     |                          |                            |
     |                          v                            |
     |  +-----------------------------------------------+   |
     |  | All chunks "done"? -> CONCAT PHASE            |   |
     |  +--------------------------+--------------------+   |
     |                             |                         |
     |                             v                         |
     |  +-----------------------------------------------+   |
     |  | CONCAT PHASE (single run)                     |   |
     |  | 1. Download all chunk artifacts               |   |
     |  | 2. Verify SHA256 of each chunk                |   |
     |  |    -> Mismatch? Reset chunk to "pending"       |   |
     |  | 3. Concatenate in order (chunk_01 + chunk_02) |   |
     |  | 4. Verify total file size matches expected    |   |
     |  | 5. ffprobe check -> corruption? Reset all      |   |
     |  | 6. Upload final video to GDrive via rclone    |   |
     |  | 7. Delete chunk artifacts from GitHub         |   |
     |  | 8. Update both state files -> git push         |   |
     |  +-----------------------------------------------+   |
     +------------------------------------------------------+
```

### MEGA Encryption Details

MEGA encrypts file content with AES-128-CTR. The 32-byte key material from the URL is split into four 64-bit quarters (`k1`, `k2`, `k3`, `k4`). The AES key = `k1 XOR k3` + `k2 XOR k4` (first half XOR second half). The IV nonce = `k3` (as hex) + 64-bit zero-padded counter. Each 16-byte block increments the counter by 1.

```
URL key: base64url (32 bytes)
    |
raw = mega_b64decode(key_b64url)  ->  32 bytes
    |
raw_hex = raw.hex()  ->  64 hex chars
    |
k1 = raw_hex[0:16]   |  (quarters)
k2 = raw_hex[16:32]  |
k3 = raw_hex[32:48]  |
k4 = raw_hex[48:64]  |
    |
AES-128 key = k1^k3  +  k2^k4     (16 bytes hex)
IV          = k3     +  0000000000000000  (16 bytes hex - counter at 0)

Range IV for chunk starting at byte S:
IV = k3 + f"{S//16:016x}"   (counter = S / 16)
```

### Version Migration

When `PROCESSOR_VERSION` in `oversized_processor.py` changes, the software auto-detects old-format chunks:

```
1. Read chunks_history.json version field
2. If version < current PROCESSOR_VERSION:
   a. Delete ALL chunk artifacts (via gh api DELETE)
   b. Reset all video status to "pending"
   c. Reset all chunk status to "pending"
   d. Remove all sha256 fields
   e. Update version field
   f. save -> next run re-downloads everything with new logic
```

This ensures that if the key derivation or encryption logic changes, old (potentially corrupt) chunks are never reused.

---

```
        RUN 1                        RUN 2                        RUN 3                        RUN 4
  +------------------+      +------------------+      +------------------+      +------------------+
  | State: empty     |      | Git: 5 done      |      | Git: 10 done    |      | Git: 13 done    |
  |                  |      | (run-end push)   |      | (run-end push)  |      | (run-end push)  |
  | Bollywood: 10   |      | Bollywood: 10   |      | Bollywood:10 OK |      | Hollywood:5 OK  |
  | Hollywood: 5    |      | Hollywood: 5    |      | Hollywood:5 --> |      |                  |
  |                  |      |                  |      |                  |      |                  |
  | Process: 1-5    |      | Process: 6-10   |      | Process: 1-3    |      | Process: 4-5    |
  | (quota: 4.8GB)  |      | (quota: 4.2GB)  |      | (quota: 3.1GB)  |      | (quota: 2.1GB)  |
  |                  |      |                  |      |                  |      |                  |
  | Bollywood:5/10  |      | Bollywood:10/10 |      | Hollywood:3/5   |      | Hollywood:5/5   |
  | Hollywood: wait |      | Hollywood: wait |      |                  |      |                  |
  |                  |      |                  |      |  **CRASH HERE?**|      |                  |
  |                  |      |                  |      |  No problem!    |      |                  |
  |                  |      |                  |      |  Git has 10/10  |      |                  |
  +------------------+      +------------------+      +------------------+      +------------------+
                                                                                                        |
                                                                                                        v
                                                                                               +------------------+
                                                                                               |   ALL DONE !    |
                                                                                               |  30/30 files    |
                                                                                               +------------------+
```

## Artifact System Explained

### What is an Artifact?

GitHub Actions **Artifacts** are files that persist **across workflow runs**. Unlike /tmp/ which is destroyed when a VM shuts down, artifacts stay on GitHub's servers for up to 90 days.

### How We Use Artifacts

**For main transfer (completed_links.json):**
```
Run 1:  [No artifact] -> Create empty state -> Process -> Upload artifact
Run 2:  [Download artifact] -> Read state -> Process more -> Upload (overwrite)
Run 3:  [Download artifact] -> Read state -> Process more -> Upload (overwrite)
...
```

**For oversized processor (chunks + artifacts):**
```
Run 1:  Download chunk_01 -> Upload artifact "ck_xyz_01" (chunk .bin file)
Run 2:  Download chunk_02 -> Upload artifact "ck_xyz_02"
Run 3:  Download all chunk artifacts -> Concat -> ffprobe OK -> Upload to GDrive
        -> Delete artifacts "ck_xyz_01", "ck_xyz_02"
```

### State Files

Two files track the entire system state, both committed to git after every change:

| File | Purpose | Updated By |
|------|---------|------------|
| `completed_links.json` | Tracks folders, completed files, oversized items (per-video status) | Both main & oversized scripts |
| `chunks_history.json` | Tracks oversized videos, chunk status, SHA256 hashes, version | Only `oversized_processor.py` |

### Artifact File Structure (completed_links.json)

```json
{
  "folders": {
    "Bollywood": {
      "total": 10,
      "done": 5,
      "status": "active"
    },
    "Hollywood": {
      "total": 8,
      "done": 0,
      "status": "pending"
    }
  },
  "completed": [
    {
      "url": "https://mega.nz/file/abc#key123",
      "filename": "Interstellar.mp4",
      "size": 2454900000,
      "target_folder": "Bollywood",
      "completed_at": "2025-01-15T10:30:00Z",
      "status": "uploaded"
    },
    {
      "url": "https://mega.nz/file/xyz#key456",
      "filename": "BigFile_6GB.mp4",
      "size": 6442450944,
      "target_folder": "Bollywood",
      "completed_at": "2025-01-15T10:35:00Z",
      "status": "unupload",
      "oversized": true
    }
  ],
  "current_folder": "Bollywood",
  "oversized": {
    "total": 0,
    "done": 0,
    "status": "completed",
    "items": []
  }
}
```

### Chunk Artifacts

Each downloaded chunk is stored as a GitHub Actions **Artifact** with naming convention:

```
ck_{file_id_lower}_{chunk_index:02d}
```

- Example: `ck_xyzabc_01`, `ck_xyzabc_02`
- Retention: 90 days
- Deleted automatically after successful concat + upload
- Contents: single `.bin` file (decrypted chunk data)

### Crash-Proof Design

Every successful file upload is saved to git at run end:

```
Process File 1 -> Save to state (in memory)
Process File 2 -> Save to state (in memory)
...
Run ends -> Git commit + push (all files processed this run saved at once)
Process File 3 -> ** CRASH (VM dies, git push never runs)
Next Run -> git pull -> Files 1,2 already in state -> Skip!
             -> Start from File 3 (not from beginning!)
```

Git push at run end = crash-proof for the entire batch processed in that run.

For oversized videos, crash-proofing extends to chunk artifacts:

```
Chunk 1 downloaded -> artifact saved + git push (chunks_history updated)
Chunk 2 download -> ** CRASH (artifact not uploaded)
Next Run -> git pull -> chunk 1 is "done", chunk 2 is "pending"
             -> Resume from chunk 2 (no re-download of chunk 1!)

Concat phase -> ** CRASH midway
Next Run -> git pull -> video still "concat_ready"
             -> Re-download all chunk artifacts -> SHA256 verify
             -> Resume concat from scratch (chunk artifacts survive)
```

---

## Chunks History File Structure

`chunks_history.json` is the second state file that tracks all oversized video processing state at the chunk level.

### Structure

```json
{
  "videos": [
    {
      "url": "https://mega.nz/file/xyz#key456",
      "filename": "BigFile_6GB.mp4",
      "target_folder": "Bollywood",
      "file_id": "xyz",
      "total_size": 6442450944,
      "total_chunks": 2,
      "chunks": [
        {
          "index": 1,
          "start_byte": 0,
          "end_byte": 4831838207,
          "expected_size": 4831838208,
          "status": "done",
          "artifact_name": "ck_xyz_01",
          "actual_size": 4831838208,
          "sha256": "a1b2c3d4e5f6..."
        },
        {
          "index": 2,
          "start_byte": 4831838208,
          "end_byte": 6442450943,
          "expected_size": 1610612736,
          "status": "pending",
          "artifact_name": "ck_xyz_02"
        }
      ],
      "status": "downloading",
      "gdrive_status": "downloading"
    }
  ],
  "current_index": 0,
  "version": 2,
  "summary": {
    "total": 1,
    "gdrive_uploaded": 0,
    "concat_ready": 0,
    "downloading": 1,
    "pending": 0,
    "status": "in_progress"
  }
}
```

### Fields

| Field | Description |
|-------|-------------|
| `videos[].status` | Overall video status: `pending` -> `downloading` -> `concat_ready` -> `gdrive_uploaded` |
| `videos[].gdrive_status` | Upload status synced from `completed_links.json`: `pending`, `downloading`, `uploaded` |
| `chunks[].status` | Per-chunk status: `pending` -> `done` (after download + decrypt + sha256 computed) |
| `chunks[].sha256` | SHA256 hash of decrypted chunk, stored at download time, verified at concat time |
| `chunks[].artifact_name` | GitHub Artifact name where the chunk .bin file is stored |
| `current_index` | Index of the currently processing video (for resumption after crash) |
| `version` | Processor version — when incremented, all old chunks are invalidated |
| `summary` | Auto-calculated counts: uploaded, concat-ready, downloading, pending |

### Lifecycle

```
                     +--------------+
                     |   pending    |
                     +------+-------+
                            | first chunk download starts
                            v
                     +--------------+
                     | downloading  |  <- per-video
                     +------+-------+
                            | all chunks "done"
                            v
                     +--------------+
                     | concat_ready |  <- waits for concat run
                     +------+-------+
                            | concat + ffprobe OK + upload OK
                            v
                     +--------------+
                     |gdrive_upload |  <- terminal state
                     +--------------+

Chunk state:  pending --> done
              (queued)     (downloaded, decrypted, SHA'd, artifact uploaded)
```

---

## MEGA Encryption & Key Derivation

MEGA encrypts all files at rest. The public URL contains a base64url-encoded 32-byte key material. The oversized processor must correctly derive the AES-128-CTR key and initial counter to decrypt chunk data.

### Key Derivation Logic

```
Input:  key_b64url (32 bytes, base64url-encoded from MEGA URL after #)
        |
        v
raw = mega_b64decode(key_b64url)    -> 32 bytes
raw_hex = raw.hex()                  -> 64 hex characters
        |
        v
k1 = raw_hex[0:16]   (first 8 bytes as hex)
k2 = raw_hex[16:32]  (second 8 bytes)
k3 = raw_hex[32:48]  (third 8 bytes)
k4 = raw_hex[48:64]  (fourth 8 bytes)
        |
        v
aes_key = f"{k1 ^ k3:016x}{k2 ^ k4:016x}"
         = k1 XOR k3 concatenated with k2 XOR k4
         = first half XOR second half -> 16 bytes AES-128 key

iv_nonce = raw_hex[32:48]            = k3 (8 bytes as hex)
iv       = iv_nonce + "0000000000000000"
         = 16 bytes with counter initialized to 0
```

### Range IV (Chunk-Level)

Since each chunk downloads a byte range (not starting at byte 0), the AES-CTR counter must be adjusted:

```
counter = start_byte // 16           (each AES block = 16 bytes)
range_iv = iv_nonce + f"{counter:016x}"
```

### Historical Fix: XOR Grouping

In the first version, the key was incorrectly derived as `k1 XOR k2` + `k3 XOR k4` (adjacent quarter XOR). The correct MEGA derivation is `k1 XOR k3` + `k2 XOR k4` (half-over-half XOR). This was fixed in `PROCESSOR_VERSION = 2`, and the version migration system ensures old (corrupt) chunks are automatically deleted and re-downloaded.

---

## Features

| Feature | Description |
|---------|-------------|
| **Multi-folder** | JSON-based folder mapping. Each key = GDrive folder name. Auto-created via rclone mkdir. |
| **megadl-only download** | `mega.py` `download_url()` removed (hangs on broken links). Uses `megadl --path` with 600s timeout. |
| **mega.py metadata** | Used as fallback for `get_public_url_info()` when `megadl --info` fails. |
| **asyncio.coroutine fallback** | Python 3.12+ compatibility fix for mega.py dependency. |
| **Per-run git push** | Git commit + push happens at run end and folder completion. State saved in memory during run. |
| **Smart quota** | Har file se pehle metadata fetch, size check. Agar quota exceed hone wala ho, skip gracefully. |
| **Oversized processor** | Files >5GB are chunked (4.5 GB each), downloaded across multiple runs, decrypted with AES-128-CTR, SHA256-verified, concatenated, ffprobe-checked, then uploaded to GDrive |
| **MEGA encryption** | Direct CDN download via urllib Range header, decrypt with `openssl aes-128-ctr` using MEGA's half-XOR key derivation (`k1^k3, k2^k4`) |
| **Chunk progress** | Real-time `[DOWNLOAD] X MB / Y MB (Z%) @ W MB/s` every 2 seconds, `[CONCAT] [i/n] X GB / Y GB (Z%)` every 1 MB, `[UPLOAD]` from rclone stats |
| **SHA256 verification** | Each chunk's SHA256 stored at download time, re-verified before concat. Mismatch, auto-reset to pending for re-download |
| **ffprobe gate** | Concatenated file must pass `ffprobe -v error` check before upload. Failure, all chunks reset, artifacts deleted, re-download |
| **Artifact cleanup** | Chunk artifacts auto-deleted after successful concat + upload. Logs confirm each deletion |
| **Version migration** | `PROCESSOR_VERSION` field auto-detects old chunks (e.g., after key derivation fix), deletes all artifacts, resets to pending for clean re-download |
| **Concurrency guard** | Only 1 run at a time, parallel runs prevented; queued runs wait (`cancel-in-progress: false`) |
| **Folder auto-advance** | Ek folder complete, next pending folder automatically active. |
| **Clean logs** | Only `DOWNLOADING...` / `Downloaded: X MB in Ys` — no megadl progress spam. |
| **Git backup** | State saved to git at run end and folder completion. Artifact uploaded as backup. |
| **Auto-trigger** | Files baki hain, next cycle automatically trigger via gh workflow run (skip on cancellation). |
| **Auto-stop** | Saare folders complete, no more cycles triggered. |
| **Cron safety net** | `*/15 * * * *` cron triggers every 15 min — if auto-trigger fails, cron picks up |
| **Secret-based links** | `MEGA_LINKS` GitHub Secret is the source of truth — not the repo files. |

---

## Setup Guide

### Prerequisites

- A **GitHub account**
- A **MEGA account** with files to transfer
- A **Google Drive** account (any, even free 15GB)
- **rclone configured** with Google Drive (one-time setup)

---

### Step 1: Fork / Clone This Repository

```bash
git clone https://github.com/your-username/MEGA-TO-GDRIVE-GITHUB-CRON.git
cd MEGA-TO-GDRIVE-GITHUB-CRON
```

### Step 2: Get Your rclone Config

If you don't have rclone configured for Google Drive:

```bash
# Install rclone (if not installed)
curl -s https://rclone.org/install.sh | sudo bash

# Configure Google Drive remote
rclone config
```

Follow the prompts:
```
n) New remote
name> gdrive
Storage> drive
client_id> (press Enter for default)
client_secret> (press Enter for default)
scope> 1 (Full access)
root_folder_id> (press Enter)
service_account_file> (press Enter)
Edit advanced config? n
Use auto config? y
```

After setup, view your config:
```bash
rclone config show gdrive
```

Copy the **entire output** — it looks like:
```
[gdrive]
type = drive
client_id = 202264815644.apps.googleusercontent.com
client_secret = X4Z3ca8xfWDb1Voo-F9a7ZxJ
scope = drive
token = {"access_token":"...","refresh_token":"..."}
```

### Step 3: Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

#### Secret 1: MEGA_LINKS

Your MEGA file links in JSON format — one line (minified):

```json
{"Bollywood Movies":["https://mega.nz/file/abc123#key1","https://mega.nz/file/def456#key2"],"Hollywood Movies":["https://mega.nz/file/ghi789#key3","https://mega.nz/file/jkl012#key4"]}
```

> **Important:** Script repo file (`MEGA_LINKS_merged.json`) nahi, **GitHub Secret `MEGA_LINKS`** padhta hai. Isliye files update karne ka effect nahi hoga — secret update karna zaroori hai.

**Rules:**
- **Key** = GDrive folder name (automatically created)
- **Value** = Array of MEGA file links (not folder links)
- Multiple folders supported (separate with comma)
- Empty arrays allowed: `"FolderName": []`

**How to create MEGA_LINKS from a text file:**

Agar aapke paas ek `.txt` file hai jisme har line mein ek MEGA link hai:

```
https://mega.nz/file/abc#key1
https://mega.nz/file/def#key2
https://mega.nz/file/ghi#key3
```

Toh JSON convert karne ke liye ye Python command use karo:

```bash
python -c "import json; urls=[l.strip() for l in open('links.txt') if l.strip()]; print(json.dumps({'FolderName': urls}, separators=(',',':')))"
```

Output copy karo aur GitHub Secret mein paste karo. Example output:

```json
{"FolderName":["https://mega.nz/file/abc#key1","https://mega.nz/file/def#key2","https://mega.nz/file/ghi#key3"]}
```

**Multi-Folder JSON from Multiple Text Files:**

Agar aapke paas **do alag folders ke liye do alag text files** hain, toh ek single merged JSON banana hoga:

```python
import json

# Har folder ke text file se URLs read karo
shorts_urls = [l.strip() for l in open('Shorts/MEGA_LINKS.txt') if l.strip()]
bg_urls = [l.strip() for l in open('.github/Documentry/MEGA_LINKS.txt') if l.strip()]

print(f'Shorts: {len(shorts_urls)} URLs')
print(f'Documentry: {len(bg_urls)} URLs')

# Merged JSON with two folders
merged = {
    "Shorts": shorts_urls,
    "Documentry": bg_urls
}

# Minified JSON output (GitHub Secret mein paste karna)
print(json.dumps(merged, separators=(',', ':')))
```

**Output — directly paste into GitHub Secret:**

```json
{"Shorts":["https://mega.nz/file/abc#key1","https://mega.nz/file/def#key2"],"Documentry":["https://mega.nz/file/xyz#key3","https://mega.nz/file/uvw#key4"]}
```

**Ek command mein (one-liner):**

```bash
python -c "import json; s=[l.strip() for l in open('Shorts/MEGA_LINKS.txt') if l.strip()]; b=[l.strip() for l in open('.github/Documentry/MEGA_LINKS.txt') if l.strip()]; print(json.dumps({'Shorts':s,'Documentry':b}, separators=(',',':')))"
```

#### Secret 2: RCLONE_CONF

Paste the **entire output** of `rclone config show gdrive`:

```
[gdrive]
type = drive
client_id = 202264815644.apps.googleusercontent.com
client_secret = X4Z3ca8xfWDb1Voo-F9a7ZxJ
scope = drive
token = {"access_token":"...","refresh_token":"..."}
```

#### Secret 3: GH_PAT (GitHub Personal Access Token)

Auto-trigger feature ke liye zaroori hai. Jab ek run khatam hota hai aur files remaining hoti hain, toh yeh token next cycle automatically trigger karta hai.

**Kaise banayein:**
1. GitHub → **Settings** (top right profile menu)
2. **Developer settings** → **Personal access tokens** → **Tokens (classic)**
3. **Generate new token (classic)** click karo
4. Name: `MEGA_TRANSFER_PAT`
5. Expiration: **No expiration** (ya jitna chaho)
6. **Permissions (select karo):**
   - [x] **`repo`** — Full control of private repositories
   - [x] **`workflow`** — Update GitHub Action workflows
7. **Generate token** click karo
8. Token copy karo (phir kabhi nahi dikhega!)
9. Apne repo mein jao → **Settings → Secrets and variables → Actions → New repository secret**
10. Name: `GH_PAT`, Value: (token paste karo) → **Add secret**

> **Note:** Token ke bina auto-trigger kaam nahi karega. Workflow manually tab hi chalana padega. Files transfer toh hongi, par har run ke baad aapko khud "Run workflow" click karna hoga.

---

### Step 4: Run the Workflow

1. Go to your repo's **Actions** tab
2. Click **"MEGA to Google Drive Transfer"** in the left sidebar
3. Click the **"Run workflow"** button
4. Watch the logs in real-time

The workflow runs automatically via three mechanisms:

1. **Manual** — Click **"Run workflow"** from the Actions tab (`workflow_dispatch`)
2. **Cron schedule** — GitHub cron `*/15 * * * *` triggers every 15 minutes automatically
3. **Auto-trigger** — `auto_trigger.py` runs at end of each cycle, calls `gh workflow run` if files remain

**Trigger behavior:**
- Concurrency group `mega-transfer-single` with `cancel-in-progress: false` — new runs queue up, don't cancel running ones
- Auto-trigger fires immediately when a cycle ends with remaining files
- Cron acts as a safety net — if auto-trigger fails, cron picks it up within 15 min
- All triggers stop automatically when all folders are complete (`auto_trigger.py`)
- Runs only on `success()` or `failure()` — NOT on cancellation

---

## Secret Formats

### MEGA_LINKS (JSON Format)

**Correct format (one line, minified):**
```json
{"FolderA":["https://mega.nz/file/abc#key","https://mega.nz/file/def#key"],"FolderB":["https://mega.nz/file/ghi#key"]}
```

**Readable version (for understanding, do NOT use in secret):**
```json
{
  "Bollywood Movies": [
    "https://mega.nz/file/abc123#key1",
    "https://mega.nz/file/def456#key2"
  ],
  "Hollywood Movies": [
    "https://mega.nz/file/ghi789#key3"
  ]
}
```

**Wrong format (plain text — will cause JSON parse error):**
```
https://mega.nz/file/abc123#key1
https://mega.nz/file/def456#key2
```

---

## Resetting Completion List (State Reset)

### Kab reset karna chahiye?
- Pehli baar setup kar rahe hain
- GDrive se saari files delete karke fresh start karna chahte hain
- Koi corruption hui hai state file mein (e.g., merge conflict markers)
- Poora transfer dobara start karna chahte hain

### Reset kaise karein?

#### Method 1: GitHub Web UI (Sabse Easy — Recommended)

1. Apne repo mein jao → `completed_links.json` file open karo
2. Edit button (pencil icon) click karo
3. Puri file **replace** karo with:

```json
{"folders": {}}
```

4. Neeche "Commit changes" click karo
   - Commit message: `reset state [skip ci]`
   - Branch: `main`
5. Ho gaya! File ab empty state dikhayegi
6. Next workflow run **first folder se start hoga, saari files scratch se transfer hogi**

#### Method 2: Local Git Push

```bash
# 1. State file reset karo
python -c "import json; json.dump({'folders':{}}, open('completed_links.json','w'), indent=2)"

# 2. Git commit + push karo
git add completed_links.json
git commit -m "reset state [skip ci]"
git push
```

#### Method 3: GitHub CLI (gh)

```bash
# Current file SHA lo
$sha = gh api repos/shivamjislt97/MEGA-TO-GDRIVE-GITHUB-CRON/contents/completed_links.json --jq '.sha'

# File overwrite karo (content = base64 of '{"folders": {}}')
gh api -X PUT repos/shivamjislt97/MEGA-TO-GDRIVE-GITHUB-CRON/contents/completed_links.json `
  -f message="reset state [skip ci]" `
  -f content="eyJmb2xkZXJzIjoge319" `
  -f sha=$sha `
  -f branch=main
```

---

### Example: Reset ke baad kya hota hai?

Maano pehle state tha:

```json
{
  "folders": {
    "Shorts": { "total": 45, "done": 45, "status": "completed" },
    "Documentry": { "total": 208, "done": 18, "status": "active" }
  },
  "completed": [
    { "url": "https://...", "filename": "video1.mp4", ... },
    { "url": "https://...", "filename": "video2.mp4", ... }
  ],
  "current_folder": "Documentry",
  "oversized": { "total": 0, "done": 0, "status": "completed", "items": [] }
}
```

Reset ke **baad** state:

```json
{"folders": {}}
```

Agli run mein kya hoga:
1. Script `MEGA_LINKS` secret se folders detect karega
2. **Shorts** → `pending`, **Documentry** → `pending`
3. `current_folder` = `null` → **Shorts** auto-activate hoga
4. Saari **45 + 208 = 253 files** dobara process hogi
5. Pehle se GDrive mein existing files hain toh **duplicate upload hogi** (rclone overwrite nahi karta)

> **Warning:** Sirf tab reset karo jab sach mein fresh start chahiye. Agar sirf kuch files skip karni hain, toh `completed_links.json` manually edit karo (Web UI se) aur unwanted URLs `completed` array mein daal do.

### Partial Reset — Sirf Ek Folder Reset Karna

Agar ek folder ki files dobara chahiye (baaki folders ka state preserve rakhna hai):

```json
{
  "folders": {
    "Shorts": { "total": 45, "done": 45, "status": "completed" },
    "Documentry": { "total": 208, "done": 0, "status": "pending" }
  },
  "completed": [],
  "current_folder": null,
  "oversized": { "total": 0, "done": 0, "status": "completed", "items": [] }
}
```

Yeh:
- Shorts ka progress **preserve** karega (45/45 done)
- Documentry ki saari files **reset** karega (0/208, wapas pending)
- `completed` array **empty** karega (Documentry ki files dobara download hogi)
- Oversized list **clear** karega

### Reset ke baad kya hota hai?
- Saare folders wapas `pending` state mein aa jayenge
- `current_folder` null ho jayega
- `completed` array empty ho jayega
- Agli run first folder se start hogi, saari files from scratch process hogi

---

## How Quota Is Managed

### The Problem

MEGA free accounts limit download bandwidth to approximately **5GB per day** per IP. When exceeded, downloads fail with "quota exceeded" errors.

### How This System Solves It

| Mechanism | Description |
|-----------|-------------|
| **Fresh VM = Fresh Quota** | Every GitHub Actions run gets a new VM with a new IP — MEGA sees it as a new user with full quota |
| **Pre-check before download** | `megadl --info` fetches file size without downloading. If current run's remaining quota < file size, skip gracefully |
| **Graceful exit** | When quota nears exhaustion, script exits cleanly. State saved to git at run end, next run resumes |
| **Per-run limit** | Script tracks `quota_used` in memory. Once it exceeds 5GB, stops processing more files |

### Example Quota Scenario

```
Run starts: quota_used = 0GB, quota_max = 5GB

File 1: size = 1.2GB -> 0 + 1.2 = 1.2 <= 5 -> Download + Upload
File 2: size = 2.3GB -> 1.2 + 2.3 = 3.5 <= 5 -> Download + Upload
File 3: size = 1.8GB -> 3.5 + 1.8 = 5.3 > 5 -> Skip (next run)
File 4: size = 800MB -> (Not checked, loop already broke)
```

---

## Folder Auto-Advance

### How It Works

1. Script reads MEGA_LINKS JSON → discovers folders from `{"folders": {}}` state
2. Each folder gets state: `pending` → `active` → `completed`
3. First folder in JSON is auto-marked `active`
4. When active folder's `done >= total`:
   - Mark folder `completed`
   - Find next `pending` folder
   - Mark it `active`
   - Update `current_folder` in state
5. If no pending folders remain → **ALL DONE!** → auto-trigger stops

### State Propagation

```
MEGA_LINKS JSON (secret)             completed_links.json (state)
+-----------------------+             +----------------------------+
| {                     |     --->    | "folders": {              |
|   "Shorts": [45 URLs] |             |   "Shorts": {             |
| }                     |             |     "total": 45,          |
+-----------------------+             |     "done": 16,           |
                                      |     "status": "active"    |
                                      |   }                       |
                                      | }                          |
                                      | "completed": [...]        |
                                      | "current_folder": "Shorts"|
                                      | "oversized": {...}        |
                                      +----------------------------+
```

### Visualization

```
Initial:  Shorts [pending]
          | (auto-activate first)
Run 1-3:  Shorts [>> active] --- 16/45 done (quota hit)
          | (auto-trigger next run)
Run 4-5:  Shorts [>> active] --- 32/45 done
          |
Run 6:    Shorts [>> active] --- 45/45 done
          | (folder complete, git push at run end)
Final:    Shorts [OK done] --- ALL DONE!
```

---

## Upload Strategy

### No Verification Needed

Previous versions used `rclone lsjson` to verify each upload. This was **removed** because:

1. **Upload always succeeds or raises error** — rclone copy returns non-zero on failure
2. **Timeouts caused crashes** — `rclone lsjson` could timeout (30s) and crash the script mid-batch
3. **Git push at run end** provides the crash-proofing — state saved to git when run completes

### What happens after upload:

```
rclone copy <local_file> gdrive:MEGA_Transfer/<folder>/
if rclone returns 0 -> upload succeeded -> save state (in memory)
if rclone returns non-zero -> RuntimeError -> skip to next file (TEMP_DIR cleaned)

At run end: git add + git commit + git push (all files from this run saved at once)
```

---

## Log Output Examples

### Normal Run (Mid-Progress)

```
=======================================================
  MEGA -> GDrive Transfer | 2026-07-16 23:26:08
=======================================================
  Artifact loaded: 58 completed files, 0 oversized
  Total pending: 195
-------------------------------------------------------
  [DONE] Shorts: 45/45
  [ACTIVE] Documentry: 13/208
-------------------------------------------------------
  Active: [Documentry] -> 195 files pending
=======================================================

  --- [1/195] Documentry ---
  Fetching: https://mega.nz/file/AbCdEfG#DeMoKeY123...
  [Documentry] "1169470_720.mp4" | Size: 483.3 MB
  DOWNLOADING: "1169470_720.mp4" (483.3 MB)...
  Downloaded: 483.3 MB in 12s
  UPLOADING: "1169470_720.mp4" (483.3 MB) to GDrive/MEGA_Transfer/Documentry/...
  Uploaded: "1169470_720.mp4" (483.3 MB in 28s)
  [1/195] Complete | Quota: 483.3 MB/5.0 GB
  --------------------------------------------------

  --- [2/195] Documentry ---
  Fetching: https://mega.nz/file/XyZ789Ab#DeMoKeY456...
  [Documentry] "1186769_720.mp4" | Size: 287.5 MB
  DOWNLOADING: "1186769_720.mp4" (287.5 MB)...
  Downloaded: 287.5 MB in 11s
  UPLOADING...
  Uploaded: "1186769_720.mp4" (287.5 MB in 61s)
  [2/195] Complete | Quota: 770.7 MB/5.0 GB
  --------------------------------------------------

  ... (more files) ...

=======================================================
  RUN SUMMARY
  --------------------------------------------------
  Processed: 5 files
  [DONE] Shorts: 45/45
  [ACTIVE] Documentry: 18/208
=======================================================

  5 files transferred — next cycle auto-continues
```

### Quota Exhausted

```
  --- [3/5] Bollywood Movies ---
  Fetching: https://mega.nz/file/ghi789...
  [Bollywood Movies] "Tenet.mp4" | Size: 2.0 GB
  Quota full: 3.4 GB + 2.0 GB > 5GB
  Skipping "Tenet.mp4" for this run
```

### Oversized File Detected

```
  --- [3/5] Bollywood Movies ---
  Fetching: https://mega.nz/file/xyz789...
  [Bollywood Movies] "BigVideo_6GB.mp4" | Size: 6.0 GB
  OVERSIZED: BigVideo_6GB.mp4 (6.0 GB) > 5GB — adding to pending oversized
```

### Folder Complete

```
  FOLDER COMPLETE: [Bollywood Movies] - 10/10 files
  >>> Activating next: [Hollywood Movies]
```

### All Done

```
  ALL FOLDERS COMPLETE! Sab kaam ho gaya!
```

### Oversized Video — Chunk Download

```
=======================================================
  Oversized Processor | 2026-07-18 12:30:15
=======================================================
  Resuming from chunks_history (4 videos)

  Current: [1/4] My Big Video... (downloading)

  --- Chunk 1/2: My Big Video... ---
  Range: 0.0 B - 4.5 GB
  MEGA CDN URL obtained, file size: 6.5 GB
  Downloading with curl -r 0-4831838207...
  [DOWNLOAD] 512.0 MB / 4.5 GB (11%) @ 85.3 MB/s | Quota: 512.0 MB/5.0 GB
  [DOWNLOAD] 1.0 GB / 4.5 GB (22%) @ 82.1 MB/s | Quota: 1.0 GB/5.0 GB
  [DOWNLOAD] 4.5 GB / 4.5 GB (100%) @ 79.6 MB/s | Quota: 4.5 GB/5.0 GB
  Downloaded: 4.5 GB in 58s
  SHA256: a1b2c3d4e5f6...
  ALL CHUNKS DONE! Video ready for concat.
```

### Oversized Video — Concat + Upload

```
=======================================================
  CONCAT RUN: My Big Video...
=======================================================
  Downloading artifact: ck_abc123_01...
  SHA256 OK
  Downloading artifact: ck_abc123_02...
  SHA256 OK
  Concatenating 2 chunks...
  [CONCAT] [1/2] 2.0 GB / 6.5 GB (30%)
  [CONCAT] [2/2] 6.5 GB / 6.5 GB (100%)
  Concat done: 6.5 GB
  SHA256: f1e2d3c4b5a6...
  Duration: duration=30.250000
  Uploading to GDrive...
  Uploaded to GDrive: MEGA_Transfer/My Folder/
  Deleted artifact ck_abc123_01
  Deleted artifact ck_abc123_02
  State updated + git pushed
  Video complete: My Big Video...
```

### Oversized Summary

```
  =============================================
  OVERSIZED SUMMARY: 3/4 uploaded to GDrive
    [    uploaded] Video One...
    [    uploaded] Video Two...
    [  uploading] Video Three...
    [     pending] Video Four...
  =============================================
```

### Processor Version Migration

```
  Processor version changed 1 -> 2, resetting all chunks...
  Deleted artifact ck_abc123_01
  Deleted artifact ck_abc123_02
  Deleted artifact ck_def456_01
  ...
  All chunks reset — clean re-download with fixed key derivation
```

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| MEGA_LINKS is not valid JSON | Secret is plain text, not JSON | Convert links to {"Folder":["url1","url2"]} format minified |
| RCLONE_CONF secret is empty | Secret not set | Add RCLONE_CONF with output of `rclone config show gdrive` |
| Artifact download warning in first run | No artifact exists yet | Normal! continue-on-error: true handles it |
| Quota hit mid-download | MEGA bandwidth exhausted | Expected! Next run gets fresh quota |
| File stuck in "pending" but already in GDrive | State file corrupted/lost | Check completed_links.json in git — reset state if needed |
| Upload fails with 403 | Token expired | rclone auto-refreshes token |
| Folder not appearing in GDrive | Remote name wrong | Default remote is gdrive, must match rclone config |
| Workflow cancels but new one starts | Auto-trigger ran on cancellation | Fixed! Now uses `if: success() \|\| failure()` |
| Files >5GB never get processed | MEGA quota limit per run | Oversized processor handles them via chunked download |
| State file corrupted/merge conflict | Git pull --rebase conflict in completed_links.json | Reset state using methods in "Resetting Completion List" section |
| 422 error on workflow_dispatch | YAML parse error (inline Python broke YAML) | Fixed! Python code extracted to auto_trigger.py |
| mega.py ImportError / asyncio.coroutine error | Python 3.12+ removed coroutine() | Fixed! Script adds fallback: `asyncio.coroutine = lambda c: c` |
| mega.py download hung / timeout | mega.py download_url() hangs on broken links | Fixed! Removed mega.py download — only megadl with 600s timeout |
| Log mein megadl progress lines aa rahi hain | Script was printing megadl stdout | Fixed! megadl stdout captured but not printed — only clean DOWNLOADING/Downloaded shown |
| MEGA_LINKS_merged.json update ka effect nahi ho raha | Secret used, not repo file | Update the `MEGA_LINKS` GitHub Secret directly, not the file |
| Oversized video stuck at "pending" | Chunks history corrupted or version mismatch | Check `chunks_history.json` version field; if mismatch, processor auto-resets |
| Chunk download fails with "Size mismatch" | MEGA CDN returned incomplete data | Auto-retry logic (3 attempts) handles transient failures |
| ffprobe rejects concatenated file | Data corruption during download or artifact storage | Processor auto-resets all chunks to `pending`, deletes artifacts, re-downloads |
| SHA256 mismatch during concat | Chunk artifact was corrupted in storage (expired or overwritten) | Processor resets that chunk to pending, re-downloads in next cycle |
| Artifact not found during concat | Retention period expired (>90 days) or manually deleted | Processor resets that chunk to pending, re-downloads |
| Chunk artifact shows "null" ID | Artifact hasn't been uploaded yet (still pending) | Normal — download phase will upload it |
| Oversized processor version increment | Key derivation or encryption logic changed | Auto-detected, all old chunks deleted and re-downloaded with new logic |
| `actions/download-artifact` removed from workflow | State files now always come from git checkout, not artifacts | Normal — `completed_links.json` and `chunks_history.json` read from working directory |

---

## Files

```
MEGA-TO-GDRIVE-GITHUB-CRON/
├── .github/
│   └── workflows/
│       └── mega_gdrive_transfer.yml       <- GitHub Actions workflow (manual + cron + auto-trigger)
├── mega_to_gdrive.py                      <- Main transfer script (normal files)
├── oversized_processor.py                 <- Oversized video processor (chunk, decrypt, concat, upload)
├── auto_trigger.py                        <- Auto-trigger next cycle if files remain
├── completed_links.json                   <- State file (auto-generated, git-pushed at run end)
├── chunks_history.json                    <- Chunks state file (auto-generated, oversized tracking)
├── test_oversized.py                      <- Unit tests for oversized processor logic
├── test_scan_local.py                     <- Unit tests for MEGA folder scanning
├── app.py                                 <- FastAPI web app (local transfer runner + MEGA scanner)
├── backend.py                             <- Flask backend (MEGA folder scanner API)
├── mega_links_generator.html              <- Web UI for generating MEGA_LINKS secret
├── all_folders_*.json                     <- Scanned folder data (auto-generated)
├── MEGA_LINKS.json                        <- Example MEGA links (example source)
├── MEGA_LINKS_merged.json                 <- Merged JSON from multiple text files (backup reference only)
├── MEGA_LINKS_TREE.md                     <- MEGA folder tree documentation
├── requirements.txt                       <- Python dependencies
├── .gitignore                             <- Ignores TEMP_DIR (downloads)
└── README.md                              <- This file
```

### File Responsibilities

| File | What It Does |
|------|-------------|
| `mega_to_gdrive.py` | Reads MEGA_LINKS + RCLONE_CONF secrets, manages state, downloads via `megadl --path` (with `megadl --info` for metadata), uploads to GDrive via rclone, git push at run end/folder completion |
| `oversized_processor.py` | Handles files >5GB: parses MEGA URL, extracts AES key, calculates chunks, downloads encrypted ranges via urllib, decrypts with openssl, SHA256 verification, concatenation, ffprobe check, GDrive upload, artifact cleanup |
| `auto_trigger.py` | Checks completed_links.json + chunks_history.json for remaining files; triggers next `gh workflow run` if needed; auto-stops when all folders complete; prevents infinite loops via progress tracking |
| `mega_gdrive_transfer.yml` | Defines GitHub Actions workflow: manual trigger + cron, installs megatools/mega.py/rclone/ffmpeg, runs mega_to_gdrive.py then oversized_processor.py, uploads artifacts, git backup, auto-trigger |
| `completed_links.json` | Persistent state: tracks folders (pending/active/completed), completed files array (with status:uploaded or status:unupload for oversized), current folder, oversized metadata |
| `chunks_history.json` | Chunk-level state: per-video chunks (index, range, status, sha256, artifact_name), summary, processor version |
| `app.py` | FastAPI web app: local transfer runner, MEGA folder scanning, Google Drive OAuth device flow, rclone config generation |
| `backend.py` | Flask backend: MEGA folder/file scanner API (async), CLI scan mode |
| `mega_links_generator.html` | Web UI: paste MEGA links, organize by folder, generate minified JSON for GitHub Secret |
| `test_oversized.py` | Unit tests for oversized processor: chunk calculation, key extraction, state validation |
| `test_scan_local.py` | Unit tests for MEGA folder scanning |
| `requirements.txt` | Python dependencies: fastapi, uvicorn, httpx, async-mega.py, apscheduler |

---

## Architecture Summary

```
   +-------------------------------------------------------------+
   |                    GITHUB ACTIONS RUNNER                     |
   |                    (Ephemeral Linux VM)                      |
   |                                                             |
   |   +-----------------------------------------------------+   |
   |   |  WORKFLOW (mega_gdrive_transfer.yml)                 |   |
   |   |                                                     |   |
   |   |  1. Checkout repo (git checkout)                    |   |
   |   |  2. Install megatools + mega.py + rclone + ffmpeg   |   |
   |   |                                                     |   |
   |   |  ---- MAIN TRANSFER PHASE ----                      |   |
   |   |  3. Run mega_to_gdrive.py                           |   |
   |   |     (megadl download, rclone upload,                |   |
   |   |      oversized detection, state to memory)          |   |
   |   |                                                     |   |
   |   |  ---- OVERSIZED PHASE ----                          |   |
   |   |  4. Run oversized_processor.py                      |   |
   |   |     -> Download encrypted chunk from MEGA CDN       |   |
   |   |     -> Decrypt with openssl aes-128-ctr             |   |
   |   |     -> Upload chunk as artifact                     |   |
   |   |     -> OR: Download all artifacts -> SHA256 verify  |   |
   |   |     -> Concatenate -> ffprobe check                 |   |
   |   |     -> Upload to GDrive -> Delete artifacts         |   |
   |   |                                                     |   |
   |   |  5. Upload artifacts (chunks, state files)          |   |
   |   |  6. Git commit + push (state backup)                |   |
   |   |  7. Auto-trigger next cycle if files remain         |   |
   |   +-----------------------------------------------------+   |
   |                             |                                |
   |          +------------------+------------------+            |
   |          |                                     |            |
   |          v                                     v            |
   |   +------------------+            +------------------------+|
   |   |  mega_to_gdrive  |            |  oversized_processor   ||
   |   |  (normal files)  |            |  (files >5GB)          ||
   |   |                  |            |                        ||
   |   |  Load state      |            |  Parse MEGA URL key    ||
   |   |  Find folder     |            |  Extract AES key + IV  ||
   |   |  Per file:       |            |  Calculate chunks      ||
   |   |  +- megadl --info|            |  Per chunk:            ||
   |   |  +- check over-  |            |  +- urllib Range       ||
   |   |  |  sized        |            |  +- openssl decrypt    ||
   |   |  +- check quota  |            |  +- SHA256 compute     ||
   |   |  +- megadl dload |            |  +- upload artifact    ||
   |   |  +- rclone upload|            |  Concat phase:         ||
   |   |  +- save state   |            |  +- download artifacts ||
   |   |  |  (in memory)  |            |  +- verify SHA256 each ||
   |   |  +- cleanup      |            |  +- cat chunks -> file ||
   |   |                  |            |  +- ffprobe check      ||
   |   |  Folder done? -> |            |  +- rclone upload      ||
   |   |  auto-advance    |            |  +- delete artifacts   ||
   |   +------------------+            +------------------------+|
   +---------------------------+---------------------------------+
                               |
           +-------------------+-------------------+
           |                   |                   |
           v                   v                   v
   +---------------+   +---------------+   +-------------------+
   |  MEGA CLOUD   |   | GDRIVE CLOUD  |   | GITHUB REPO      |
   |               |   |               |   |  + ARTIFACTS     |
   |  Source via   |   |  Destination  |   |                  |
   |  megadl CLI   |   |  MEGA_Transfer|   | completed_links  |
   |  OR urllib    |   |  /{Folder}/   |   | chunks_history   |
   |  (chunked)    |   |               |   | chunk artifacts  |
   |  ~5GB quota   |   |               |   | per-run git      |
   |  per IP/day   |   |               |   |                  |
   +---------------+   +---------------+   +-------------------+
```

---

## License

Free to use. Made by Shivam.
