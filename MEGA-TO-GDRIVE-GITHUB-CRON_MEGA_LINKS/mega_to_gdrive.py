#!/usr/bin/env python3
"""MEGA to Google Drive multi-folder transfer with artifact-based state tracking."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

RCLONE_CONF_RAW = os.environ.get("RCLONE_CONF", "")

GDRIVE_REMOTE = "gdrive"
BASE_FOLDER = "MEGA_Transfer"
QUOTA_MAX = 5 * 1024 * 1024 * 1024
QUOTA_MARKERS = ["over quota", "bandwidth limit", "quota exceeded", "429", "eoverquota"]

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
COMPLETED_FILE = os.path.join(WORKSPACE, "completed_links.json")
LINKS_FILE = os.path.join(WORKSPACE, "all_folders.json")
TEMP_DIR = os.path.join(WORKSPACE, "mega_temp")
MAX_RETRIES = 3


def fmt_size(b):
    if b is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def parse_size_num(val, unit):
    units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return float(val) * units.get(unit, 1)


def is_quota(text):
    return any(m in text.lower() for m in QUOTA_MARKERS)


def log(msg, end='\n'):
    print(msg, flush=True, end=end)


def git_push(quiet=False):
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], capture_output=True, timeout=5)
        subprocess.run(["git", "add", "completed_links.json"], check=True, capture_output=True, timeout=15)
        r = subprocess.run(
            ["git", "commit", "-m", "update state [skip ci]"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0 and "nothing to commit" not in r.stderr and "nothing to commit" not in r.stdout:
            if not quiet:
                log(f"   [git] commit skipped: {r.stderr.strip() or r.stdout.strip()}")
            return
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            capture_output=True, timeout=30
        )
        r = subprocess.run(["git", "push"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip()
            if not quiet:
                log(f"   [git] push failed: {err}")
            return
        if not quiet:
            log("   [git] state pushed to repo")
    except subprocess.TimeoutExpired:
        if not quiet:
            log("   [git] timeout pushing state")
    except Exception as e:
        if not quiet:
                log(f"   [git] push error: {e}")


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_completed():
    if os.path.exists(COMPLETED_FILE):
        try:
            with open(COMPLETED_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"folders": {}, "completed": [], "current_folder": None, "oversized": {"total": 0, "done": 0, "status": "completed", "items": []}}


def save_completed(state):
    with open(COMPLETED_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_file_info(url):
    try:
        r = subprocess.run(
            ["megadl", "--info", url],
            capture_output=True, text=True, timeout=30
        )
        out = (r.stdout + " " + r.stderr).strip()
        name = re.search(r"(?:File|Name):\s*(.+?)\s*\(", out)
        size = re.search(r"\((\d+)\s*bytes?\)", out)
        if name and size:
            return name.group(1), int(size.group(1))
    except Exception:
        pass
    try:
        import asyncio
        from mega.client import MegaNzClient
        from mega.crypto import b64_to_a32
        async def _get():
            async with MegaNzClient() as mc:
                info = await mc.get_public_file_info(fid, key)
                return info.name, info.size
        m = re.search(r"/file/([^#]+)#(.+)", url)
        if m:
            fid, key = m.group(1), m.group(2)
            return asyncio.run(_get())
    except Exception:
        pass
    return None, None


def download_file(url, timeout=600, quota_used=0, quota_max=0, total_size=0):
    if os.path.isdir(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)

    process = subprocess.Popen(
        ["megadl", "--path", TEMP_DIR, url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True
    )

    output = []
    last_line = ""
    start = time.time()

    def monitor():
        nonlocal last_line
        while process.poll() is None:
            if os.path.isdir(TEMP_DIR):
                files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
                if files:
                    cur_size = os.path.getsize(os.path.join(TEMP_DIR, files[0]))
                    elapsed = time.time() - start
                    speed = cur_size / elapsed if elapsed > 0 else 0
                    if total_size > 0:
                        pct = min(100.0, cur_size * 100 / total_size)
                        line_text = f"   DOWNLOAD: {fmt_size(cur_size)} / {fmt_size(total_size)} ({pct:.0f}%) @ {fmt_size(speed)}/s | Quota: {fmt_size(quota_used + cur_size)}/{fmt_size(quota_max)}"
                    else:
                        line_text = f"   DOWNLOAD: {fmt_size(cur_size)} downloaded @ {fmt_size(speed)}/s | Quota: {fmt_size(quota_used + cur_size)}/{fmt_size(quota_max)}"
                    if line_text != last_line:
                        log(line_text)
                        last_line = line_text
            time.sleep(2)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        thread.join(timeout=3)
        raise RuntimeError("megadl timed out")

    thread.join(timeout=3)
    log("")

    if process.returncode != 0:
        out = "".join(output)
        raise RuntimeError(out.strip() or f"megadl exit {process.returncode}")

    files = [f for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
    if not files:
        raise RuntimeError("No file downloaded")
    return os.path.join(TEMP_DIR, files[0])


def ensure_gdrive_folder(folder_name):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}"
    try:
        r = subprocess.run(
            ["rclone", "mkdir", target],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            log(f"  warning: rclone mkdir stderr: {r.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log("  warning: rclone mkdir timed out (non-fatal)")
    except Exception as e:
        log(f"  warning: rclone mkdir failed: {str(e)[:100]} (non-fatal)")


def upload_file(filepath, folder_name, quota_used=0, quota_max=0):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}/"
    file_size = os.path.getsize(filepath)

    process = subprocess.Popen(
        [
            "rclone", "copy", filepath, target,
            "--multi-thread-streams=16",
            "--multi-thread-cutoff=50M",
            "--drive-chunk-size=64M",
            "--transfers=8",
            "--buffer-size=256M",
            "--drive-upload-cutoff=50M",
            "--no-check-dest",
            "--tpslimit=10",
            "--tpslimit-burst=10",
            "--stats=3s", "--stats-one-line",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, bufsize=1
    )

    last_line = ""
    collected = []

    def reader():
        nonlocal collected
        try:
            while True:
                line = process.stderr.readline()
                if not line:
                    break
                collected.append(line)
        except:
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    start_time = time.time()
    last_speed = 0
    last_eta = ""

    try:
        while thread.is_alive():
            now = time.time()
            elapsed = now - start_time
            if collected:
                line = collected.pop(0).strip().replace("\r", "")
                if not line:
                    continue
                # Try parsing rclone stats line: "Transferred:   1.2 GiB / 2.8 GiB, 43%, 34.5 MiB/s, ETA 45s"
                m = re.search(r'Transferred:\s+([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+),\s*(\d+)%,\s*([\d.]+\s*\w+/s),\s*ETA\s+(\S+)', line)
                if m:
                    line_text = f"   UPLOAD: {m.group(1)} / {m.group(2)} ({m.group(3)}%) @ {m.group(4)} ETA {m.group(5)}"
                    if line_text != last_line:
                        log(line_text)
                        last_line = line_text
                    continue
            # Manual progress: estimate based on elapsed time and file size
            if elapsed > 3 and file_size > 0:
                pct = min(int(elapsed * 100 / max(elapsed + 60, 1)), 99)
                done_mb = file_size * pct / 100 / (1024 * 1024)
                total_mb = file_size / (1024 * 1024)
                spd = done_mb / elapsed if elapsed > 0 else 0
                eta = max(int((total_mb - done_mb) / spd), 0) if spd > 0 else 0
                eta_m = eta // 60
                eta_s = eta % 60
                line_text = f"   UPLOAD: {done_mb:.0f} MB / {total_mb:.0f} MB ({pct}%) @ {spd:.1f} MB/s ETA {eta_m}m{eta_s:02d}s"
                if line_text != last_line:
                    log(line_text)
                    last_line = line_text
            time.sleep(2)

        process.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        process.kill()
        thread.join(timeout=3)
        raise RuntimeError("rclone copy timed out")

    thread.join(timeout=3)
    elapsed = time.time() - start_time
    log("")

    if process.returncode != 0:
        err_lines = [l.strip() for l in collected if l.strip() and not l.strip().startswith('{')]
        err_detail = err_lines[-3:] if err_lines else []
        raise RuntimeError(f"rclone copy exit {process.returncode}: {chr(59).join(err_detail)}")
    # Clean up rclone temp files (tmp*) from GDrive
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}/"
    try:
        subprocess.run(
            ["rclone", "delete", target, "--include", "tmp*", "--min-age", "1m"],
            capture_output=True, timeout=60
        )
    except Exception:
        pass
    return os.path.basename(filepath)


def verify_upload(filename, file_size, folder_name):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder_name}/{filename}"
    try:
        r = subprocess.run(
            ["rclone", "lsjson", target],
            capture_output=True, text=True, timeout=60
        )
        if r.returncode != 0 or not r.stdout.strip():
            return False
        files = json.loads(r.stdout)
        for f in files:
            if f.get("Name") == filename and f.get("Size") == file_size:
                return True
    except Exception:
        pass
    return False


def cleanup_gdrive_temps():
    """Remove rclone temp files (tmp*) from all GDrive folders."""
    log("  Cleaning up rclone temporary files...")
    try:
        result = subprocess.run(
            ["rclone", "lsd", f"{GDRIVE_REMOTE}:{BASE_FOLDER}/"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
            folders = [l.strip().split()[-1] for l in lines if l.strip().split()]
            total = len(folders)
            if total == 0:
                log("  No folders found, cleanup skipped")
                return
            log(f"  Found {total} folders, checking for temp files...")
            for i, folder in enumerate(folders, 1):
                target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{folder}/"
                try:
                    subprocess.run(
                        ["rclone", "delete", target, "--include", "tmp*", "--min-age", "1m"],
                        capture_output=True, timeout=60
                    )
                except Exception:
                    pass
                if i % 50 == 0 or i == total:
                    log(f"  Cleanup progress: {i}/{total} folders checked")
            log(f"  Cleanup complete")
        else:
            log(f"  Cleanup: rclone lsd returned code {result.returncode}")
    except subprocess.TimeoutExpired:
        log("  Cleanup: rclone lsd timed out (non-fatal)")
    except Exception as e:
        log(f"  Cleanup warning: {e}")


def load_mega_links():
    if os.path.exists(LINKS_FILE):
        with open(LINKS_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            total = sum(len(v) for v in data.values())
            log(f"  Loaded: all_folders.json ({len(data)} folders, {total} files)")
            return data
        log("  all_folders.json empty, trying fallback...")
    mp = os.path.join(WORKSPACE, "MEGA_LINKS_merged.json")
    if os.path.exists(mp):
        with open(mp) as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            log(f"  Loaded: MEGA_LINKS_merged.json ({len(data)} folders)")
            return data
    merged = {}
    for fn in sorted(os.listdir(WORKSPACE)):
        if re.match(r'all_folders_\d+\.json$', fn):
            with open(os.path.join(WORKSPACE, fn)) as f:
                for k, v in json.load(f).items():
                    merged.setdefault(k, []).extend(v)
    if merged:
        log(f"  Loaded: {len(merged)} folders from split all_folders_*.json")
        return merged
    raw = os.environ.get("MEGA_LINKS", "").strip()
    if raw:
        data = json.loads(raw)
        log(f"  Loaded: MEGA_LINKS env var ({len(data)} folders)")
        return data
    raise ValueError("No MEGA links found")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    # Setup rclone config
    conf_dir = os.path.expanduser("~/.config/rclone")
    conf_path = os.path.join(conf_dir, "rclone.conf")
    os.makedirs(conf_dir, exist_ok=True)
    if not RCLONE_CONF_RAW:
        log("ERROR: RCLONE_CONF secret is empty")
        sys.exit(1)
    with open(conf_path, "w") as f:
        f.write(RCLONE_CONF_RAW)
    log("  rclone.conf written")
    r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=10)
    log(f"  rclone remotes: {r.stdout.strip() or '(none)'}")

    # Clean up any rclone temp files from previous runs
    cleanup_gdrive_temps()

    # Load artifact state
    state = load_completed()
    folders = state.get("folders", {})
    completed = state.get("completed", [])
    current_folder = state.get("current_folder")
    oversized_raw = state.get("oversized", [])
    oversized = oversized_raw.get("items", []) if isinstance(oversized_raw, dict) else oversized_raw

    # Load MEGA links from all_folders.json (or fallback chain)
    try:
        all_links = load_mega_links()
    except (ValueError, json.JSONDecodeError) as e:
        log(f"ERROR: Failed to load MEGA links: {e}")
        sys.exit(1)
    if not isinstance(all_links, dict):
        log("ERROR: MEGA links must be a JSON object {\"folder\": [urls]}")
        sys.exit(1)

    # STATE MIGRATION: Move old oversized items to completed list with status="unupload"
    if oversized and isinstance(oversized_raw, dict) and oversized_raw.get("items"):
        migrated = 0
        for ov in oversized_raw["items"]:
            url = ov.get("url", "")
            if url and url not in set(item["url"] for item in completed):
                completed.append({
                    "url": url,
                    "filename": ov.get("filename", f"file_{url[-12:]}"),
                    "size": ov.get("size", 0),
                    "target_folder": ov.get("target_folder", "Oversized"),
                    "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "unupload",
                    "oversized": True
                })
                migrated += 1
        if migrated:
            log(f"  Migrated {migrated} oversized items to completed list")
        # Remove old oversized tracking
        state["oversized"] = {"total": 0, "done": 0, "status": "completed", "items": []}

    # Ensure all folders from secret are in state
    for folder_name, links in all_links.items():
        if folder_name not in folders:
            folders[folder_name] = {
                "total": len(links),
                "done": 0,
                "status": "pending"
            }
        elif "oversized_count" in folders[folder_name]:
            # Clean up legacy oversized_count field
            del folders[folder_name]["oversized_count"]

    # Auto-activate first pending folder
    if not current_folder or current_folder not in folders or folders.get(current_folder, {}).get("status") == "completed":
        for name, fdata in folders.items():
            if fdata["status"] == "pending":
                fdata["status"] = "active"
                current_folder = name
                state["current_folder"] = name
                break

    state["folders"] = folders
    save_completed(state)

    # Build lookup sets — only completed URLs (regardless of status)
    completed_urls = set(item["url"] for item in completed)

    # Stats
    total_pending_all = sum(
        f["total"] - f["done"] for f in folders.values() if f["status"] != "completed"
    )

    log("=" * 55)
    log(f"  MEGA -> GDrive Transfer | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 55)
    unupload_count = sum(1 for item in completed if item.get("status") == "unupload")
    log(f"  Artifact loaded: {len(completed)} total entries ({unupload_count} oversized pending)")
    log(f"  Total pending: {total_pending_all}")
    log("-" * 55)
    for name, fdata in folders.items():
        icon = "ACTIVE" if fdata["status"] == "active" else "DONE" if fdata["status"] == "completed" else "WAIT"
        log(f"  {icon}: {name}: {fdata['done']}/{fdata['total']}")
    log("-" * 55)

    processed_total = 0
    all_done_now = False

    while True:
        # Find active folder
        active_folder = None
        for name, fdata in folders.items():
            if fdata["status"] == "active":
                active_folder = name
                break

        if not active_folder:
            if all_done_now:
                break
            # Auto-activate first pending
            for name, fdata in folders.items():
                if fdata["status"] == "pending":
                    fdata["status"] = "active"
                    state["current_folder"] = name
                    active_folder = name
                    break
            if not active_folder:
                break

        folder_links = all_links.get(active_folder, [])
        pending = []
        for url in folder_links:
            if url in completed_urls:
                continue
            pending.append(url)

        total = len(pending)
        log(f"\n  Active: Folder {active_folder} -> {total} files pending")
        print(f"::notice title={active_folder}::Starting {total} files", flush=True)

        if total == 0:
            # All files have entries in completed list — check if all are uploaded
            unupload_count = sum(1 for item in completed if item.get("target_folder") == active_folder and item.get("status") == "unupload")
            if unupload_count > 0:
                log(f"   {active_folder}: Waiting for {unupload_count} oversized uploads. Keeping active.")
                break  # oversized_processor will handle them
            folders[active_folder]["status"] = "completed"
            folders[active_folder]["done"] = folders[active_folder]["total"]
            state["folders"] = folders
            for name, fd in folders.items():
                if fd["status"] == "pending":
                    fd["status"] = "active"
                    state["current_folder"] = name
                    log(f"  Next folder activated: {name}")
                    break
            else:
                state["current_folder"] = None
                log(f"  FOLDER DONE: {active_folder} — all files uploaded")
            save_completed(state)
            completed_urls = set(item["url"] for item in completed)
            continue

        # Process files for this folder
        quota_used = 0
        processed = 0

        for idx, url in enumerate(pending, 1):
            log(f"  --- {idx}/{total}: {active_folder} ---")
            log(f"  Fetching: {url[:60]}...")

            filename, file_size = get_file_info(url)
            metadata_ok = filename and file_size is not None

            if metadata_ok:
                log(f"   {active_folder}: \"{filename}\" | Size: {fmt_size(file_size)}")
                if file_size > QUOTA_MAX:
                    log(f"  OVERSIZED: {filename} ({fmt_size(file_size)}) > 5GB — adding to pending oversized")
                    print(f"::notice::OVERSIZED >5GB: {filename}", flush=True)
                    completed.append({
                        "url": url, "filename": filename,
                        "size": file_size, "target_folder": active_folder,
                        "completed_at": timestamp(),
                        "status": "unupload",
                        "oversized": True
                    })
                    state["completed"] = completed
                    save_completed(state)
                    log(f"   OVERSIZED: Added to pending: {folders[active_folder]['done']}/{folders[active_folder]['total']} done, {sum(1 for c in completed if c.get('target_folder') == active_folder and c.get('status') == 'unupload')} oversized pending")
                    continue
                if quota_used + file_size > QUOTA_MAX:
                    log(f"  Quota full: {fmt_size(quota_used)} + {fmt_size(file_size)} > 5GB")
                    log(f"  Skipping \"{filename}\" for this run")
                    break
            else:
                log(f"  (metadata unavailable — downloading directly)")

            dl_start = time.time()
            log(f"  DOWNLOADING: \"{filename or '?'}\" ({fmt_size(file_size or 0)})...")
            try:
                local_path = download_file(url, timeout=600, quota_used=quota_used, quota_max=QUOTA_MAX, total_size=file_size or 0)
                actual_size = os.path.getsize(local_path)
                actual_name = os.path.basename(local_path)
                dl_elapsed = time.time() - dl_start
                log(f"  Downloaded: {fmt_size(actual_size)} in {dl_elapsed:.0f}s")
            except RuntimeError as e:
                msg = str(e)
                if is_quota(msg):
                    if not metadata_ok:
                        log(f"\n  QUOTA EXCEEDED + no metadata — likely >5GB file. Marking as oversized.")
                        completed.append({
                            "url": url, "filename": f"unknown_{url.split('/')[-1].split('#')[0][:12]}.mp4",
                            "size": 0, "target_folder": active_folder,
                            "completed_at": timestamp(),
                            "status": "unupload",
                            "oversized": True
                        })
                        state["completed"] = completed
                        save_completed(state)
                        shutil.rmtree(TEMP_DIR, ignore_errors=True)
                        continue
                    log(f"\n  QUOTA EXCEEDED mid-download! Stopping.")
                    log(f"  {processed} files done this run.")
                    break
                log(f"  Download failed: {msg[:200]}")
                print(f"::error::download failed: {msg[:200]}", flush=True)
                continue

            if not metadata_ok:
                filename = actual_name
                file_size = actual_size
                if file_size > QUOTA_MAX:
                    log(f"  OVERSIZED: {filename} ({fmt_size(file_size)}) > 5GB — added to pending")
                    completed.append({
                        "url": url, "filename": filename,
                        "size": file_size, "target_folder": active_folder,
                        "completed_at": timestamp(),
                        "status": "unupload",
                        "oversized": True
                    })
                    state["completed"] = completed
                    save_completed(state)
                    shutil.rmtree(TEMP_DIR, ignore_errors=True)
                    continue
                if quota_used + file_size > QUOTA_MAX:
                    log(f"  Quota full after download ({fmt_size(quota_used)} + {fmt_size(file_size)} > 5GB)")
                    log(f"  Processing this file anyway (already downloaded), then stopping.")

            quota_exhausted = (quota_used + file_size) >= QUOTA_MAX

            ul_start = time.time()
            log(f"  UPLOADING: \"{filename}\" ({fmt_size(file_size)}) to GDrive/{BASE_FOLDER}/{active_folder}/...")
            ensure_gdrive_folder(active_folder)
            try:
                uploaded_name = upload_file(local_path, active_folder, quota_used=quota_used, quota_max=QUOTA_MAX)
                ul_elapsed = time.time() - ul_start
                log(f"  Uploaded: \"{uploaded_name}\" ({fmt_size(file_size)} in {ul_elapsed:.0f}s)")
            except RuntimeError as e:
                log(f"  Upload failed: {str(e)[:200]}")
                shutil.rmtree(TEMP_DIR, ignore_errors=True)
                continue

            quota_used += file_size

            completed.append({
                "url": url,
                "filename": uploaded_name,
                "size": file_size,
                "target_folder": active_folder,
                "completed_at": timestamp(),
                "status": "uploaded"
            })
            folders[active_folder]["done"] += 1
            processed_total += 1
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
            print(f"::notice title={active_folder}::Uploaded {idx}/{total}: {uploaded_name}", flush=True)
            log(f"   {idx}/{total}: Complete | Quota: {fmt_size(quota_used)}/{fmt_size(QUOTA_MAX)}")
            log(f"  {'-' * 50}")

            if quota_exhausted:
                log(f"  Quota exhausted — remaining files will be processed next run.")
                break

        # Folder completion check
        fdata = folders[active_folder]
        # Check if all files have entries with status="uploaded" (not just pending)
        folder_done = fdata["done"]
        folder_total = fdata["total"]
        unupload_in_folder = sum(1 for item in completed if item.get("target_folder") == active_folder and item.get("status") == "unupload")
        if folder_done + unupload_in_folder >= folder_total:
            if unupload_in_folder > 0:
                print(f"::notice title={active_folder}::All files done. {folder_done}/{folder_total} uploaded, {unupload_in_folder} oversized pending", flush=True)
                log(f"\n   {active_folder}: All files detected. {folder_done}/{folder_total} uploaded, {unupload_in_folder} oversized pending.")
                log(f"  Keeping active for oversized processing.")
                # Don't complete — oversized_processor will upload pending oversized files
                state["folders"] = folders
                state["completed"] = completed
                save_completed(state)
                git_push(quiet=True)
                break
            else:
                fdata["status"] = "completed"
                log(f"\n  FOLDER COMPLETE: {active_folder} — {fdata['done']}/{fdata['total']} all uploaded")
                next_folder = None
                for name, fd in folders.items():
                    if fd["status"] == "pending":
                        fd["status"] = "active"
                        state["current_folder"] = name
                        next_folder = name
                        log(f"  >>> Activating next: {name}")
                        break
                if next_folder:
                    completed_urls = set(item["url"] for item in completed)
                    state["folders"] = folders
                    state["completed"] = completed
                    save_completed(state)
                    git_push(quiet=True)
                    if quota_used >= QUOTA_MAX:
                        log(f"  Quota exhausted — next folder will be processed next run.")
                        break
                    continue
                else:
                    state["current_folder"] = None
                    all_done_now = True
                    log(f"  ALL FOLDERS COMPLETE! Sab kaam ho gaya!")
        else:
            log(f"\n   {active_folder}: Progress: {fdata['done']}/{fdata['total']}")
            break

        state["folders"] = folders
        state["completed"] = completed
        save_completed(state)
        git_push(quiet=True)

    # Final save
    state["folders"] = folders
    state["completed"] = completed
    save_completed(state)
    git_push(quiet=True)

    # Summary
    log(f"\n{'=' * 55}")
    log(f"  RUN SUMMARY")
    log(f"  {'-' * 55}")
    log(f"  Processed: {processed_total} files")
    for name, fd in folders.items():
        icon = "DONE" if fd["status"] == "completed" else "ACTIVE" if fd["status"] == "active" else "WAIT"
        log(f"  {icon}: {name}: {fd['done']}/{fd['total']}")
    oversized_pending = sum(1 for item in completed if item.get("status") == "unupload")
    if oversized_pending:
        log(f"  OVERSIZED (>5GB): {oversized_pending} files pending upload")
    log("=" * 55)

    remaining = sum(fd["total"] - fd["done"] for fd in folders.values() if fd["status"] != "completed")
    oversized_pending_count = sum(1 for item in completed if item.get("status") == "unupload")
    if remaining > 0:
        log(f"\n  {remaining} files remaining - next cycle will continue")
        print(f"::notice::Remaining: {remaining} files ({oversized_pending_count} oversized pending)", flush=True)
    else:
        log(f"\n  SAB KAAM HO GAYA! :tada:")


if __name__ == "__main__":
    main()
