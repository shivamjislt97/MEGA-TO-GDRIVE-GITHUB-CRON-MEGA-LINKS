#!/usr/bin/env python3
"""Oversized video processor: chunked download via curl -r + openssl, concat, GDrive upload."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
COMPLETED_FILE = os.path.join(WORKSPACE, "completed_links.json")
CHUNKS_HISTORY_FILE = os.path.join(WORKSPACE, "chunks_history.json")
GDRIVE_REMOTE = "gdrive"
BASE_FOLDER = "MEGA_Transfer"
CHUNK_MAX = 5261334938  # 4.9 GB
QUOTA_SAFE = 4.5 * 1024 * 1024 * 1024
MAX_RETRIES = 3
API_URL = "https://g.api.mega.co.nz/cs"
PROCESSOR_VERSION = 2


def log(msg, end="\n"):
    print(msg, flush=True, end=end)


def clean_filename(fname):
    """Remove MEGA file IDs (bracket-enclosed 8-11 char alphanumeric) from filenames for cleaner logging."""
    return re.sub(r'\s*\[[A-Za-z0-9_-]{8,11}\]\s*', ' ', fname).strip()


def fmt_size(b):
    if b is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mega_b64decode(s):
    s = s.replace("-", "+").replace("_", "/")
    pad = 4 - (len(s) % 4)
    if pad != 4:
        s += "=" * pad
    return base64.b64decode(s)


def parse_mega_url(url):
    url = url.strip()
    m = re.search(r"/file/([^#]+)#(.+)", url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"/#!([^!]+)!([^#]+)", url)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Cannot parse MEGA URL: {url[:80]}")


def extract_key_iv(key_b64url):
    raw = mega_b64decode(key_b64url)
    if len(raw) != 32:
        raise ValueError(f"Invalid key length: {len(raw)}")
    raw_hex = raw.hex()
    k1 = int(raw_hex[0:16], 16)
    k2 = int(raw_hex[16:32], 16)
    k3 = int(raw_hex[32:48], 16)
    k4 = int(raw_hex[48:64], 16)
    aes_key_hex = f"{k1 ^ k3:016x}{k2 ^ k4:016x}"
    iv_nonce = raw_hex[32:48]
    iv_hex = iv_nonce + "0000000000000000"
    return aes_key_hex, iv_hex, raw_hex


def make_range_iv(iv_nonce_hex, start_byte):
    counter = start_byte // 16
    return iv_nonce_hex + f"{counter:016x}"


def mega_api(file_id, g_flag=False):
    params = [{"a": "g", "p": file_id}]
    if g_flag:
        params[0]["g"] = 1
    result = subprocess.run(
        ["curl", "-s", "-X", "POST",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(params),
         API_URL],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"MEGA API curl failed: {result.stderr[:200]}")
    data = json.loads(result.stdout)
    if not data or not isinstance(data, list):
        raise RuntimeError(f"MEGA API bad response: {result.stdout[:200]}")
    return data[0]


def get_download_url(file_id):
    resp = mega_api(file_id, g_flag=True)
    if "g" not in resp:
        raise RuntimeError(f"MEGA API no download URL: {resp}")
    return resp["g"], int(resp["s"])


def get_file_metadata(file_id, aes_key_hex):
    try:
        resp = mega_api(file_id, g_flag=False)
        at_b64 = resp.get("at", "")
        if not at_b64:
            return None, None
        at_raw = mega_b64decode(at_b64)
        result = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-cbc",
             "-K", aes_key_hex,
             "-iv", "00000000000000000000000000000000",
             "-nopad"],
            input=at_raw, capture_output=True, timeout=10
        )
        dec = result.stdout.rstrip(b"\x00").decode("utf-8", errors="replace")
        m = re.search(r'"n"\s*:\s*"([^"]+)"', dec)
        filename = m.group(1) if m else None
        return filename, int(resp.get("s", 0))
    except Exception as e:
        log(f"  warn: metadata fetch failed: {e}")
        return None, None


def calculate_chunks(total_size):
    chunks = []
    pos = 0
    idx = 1
    while pos < total_size:
        end = min(pos + CHUNK_MAX - 1, total_size - 1)
        chunks.append({
            "index": idx,
            "start_byte": pos,
            "end_byte": end,
            "expected_size": end - pos + 1,
            "status": "pending"
        })
        pos = end + 1
        idx += 1
    return chunks


def download_chunk(raw_url, aes_key_hex, iv_nonce_hex, start_byte, end_byte, output_path):
    enc_tmp = output_path + ".enc"
    try:
        total_size = end_byte - start_byte + 1
        range_hdr = f"bytes={start_byte}-{end_byte}"
        iv_hex = make_range_iv(iv_nonce_hex, start_byte)
        QUOTA_LIMIT = 5_368_709_120

        for attempt in range(MAX_RETRIES):
            try:
                req = urllib.request.Request(raw_url, headers={"Range": range_hdr})
                resp = urllib.request.urlopen(req, timeout=300)
                content_length = resp.headers.get("Content-Length")
                remote_size = int(content_length) if content_length else total_size
                downloaded = 0
                dl_start = time.time()
                last_log = 0
                with open(enc_tmp, "wb") as f:
                    while True:
                        buf = resp.read(1048576)
                        if not buf:
                            break
                        f.write(buf)
                        downloaded += len(buf)
                        elapsed = time.time() - dl_start
                        now = time.time()
                        if now - last_log >= 2 or downloaded >= total_size:
                            percent = downloaded * 100 / total_size
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            log(
                                f"  DOWNLOAD: {fmt_size(downloaded)} / {fmt_size(total_size)} "
                                f"({percent:.0f}%) @ {fmt_size(speed)}/s "
                                f"| Quota: {fmt_size(downloaded)}/{fmt_size(QUOTA_LIMIT)}",
                            )
                            last_log = now
                log("")
                if downloaded != total_size:
                    raise RuntimeError(f"Size mismatch: got {downloaded}, expected {total_size}")
                break
            except Exception as e:
                log(f"")
                log(f"  [retry {attempt+1}/{MAX_RETRIES}] download failed: {e}")
                time.sleep(5)
                if os.path.exists(enc_tmp):
                    os.remove(enc_tmp)
        else:
            raise RuntimeError(f"download failed after {MAX_RETRIES} attempts")

        r = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-ctr",
             "-K", aes_key_hex, "-iv", iv_hex,
             "-in", enc_tmp, "-out", output_path],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode != 0:
            raise RuntimeError(f"openssl decrypt failed: {r.stderr[:200]}")
        actual = os.path.getsize(output_path)
        expected = total_size
        if actual != expected:
            raise RuntimeError(
                f"Size mismatch after decrypt: got {actual}, expected {expected}"
            )
    finally:
        if os.path.exists(enc_tmp):
            os.remove(enc_tmp)



def load_completed():
    if os.path.exists(COMPLETED_FILE):
        try:
            with open(COMPLETED_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"folders": {}, "completed": [], "current_folder": None}


def save_completed(state):
    with open(COMPLETED_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_unupload_items(state):
    return [item for item in state.get("completed", []) if item.get("status") == "unupload"]


def load_chunks_history():
    if os.path.exists(CHUNKS_HISTORY_FILE):
        try:
            with open(CHUNKS_HISTORY_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return None


def save_chunks_history(state):
    with open(CHUNKS_HISTORY_FILE, "w") as f:
        json.dump(state, f, indent=2)


def set_github_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")
    os.environ[name] = value


def find_artifact_id(artifact_name):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return None
    for attempt in range(MAX_RETRIES):
        r = subprocess.run(
            ["gh", "api", f"/repos/{repo}/actions/artifacts?per_page=100",
             "--jq", f'.artifacts[] | select(.name=="{artifact_name}") | .id',
             "--paginate"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            ids = [x.strip() for x in r.stdout.strip().split("\n") if x.strip() and x.strip() != "null"]
            if ids:
                return ids[0]
        log(f"  [retry {attempt+1}/{MAX_RETRIES}] find artifact {artifact_name}")
        time.sleep(5)
    return None


def download_artifact(artifact_name, output_dir="."):
    import zipfile
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    if not repo:
        return False

    aid = find_artifact_id(artifact_name)
    if not aid:
        return False

    zip_path = os.path.join(output_dir, f"{artifact_name}.zip")
    api_url = f"https://api.github.com/repos/{repo}/actions/artifacts/{aid}/zip"

    signed_url = None
    for attempt in range(MAX_RETRIES):
        curl_cmd = ["curl", "-sS", "-D", "-", "-o", "/dev/null",
                     "-H", "Accept: application/json"]
        if token:
            curl_cmd += ["-H", f"Authorization: Bearer {token}"]
        curl_cmd.append(api_url)
        r = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=60)
        for line in r.stderr.splitlines():
            if line.lower().startswith("location:"):
                signed_url = line.split(":", 1)[1].strip()
                break
        if not signed_url:
            for line in r.stdout.splitlines():
                if line.lower().startswith("location:"):
                    signed_url = line.split(":", 1)[1].strip()
                    break
        if signed_url:
            break
        log(f"  [retry {attempt+1}/{MAX_RETRIES}] get signed URL: {r.stderr[:100]}")
        time.sleep(5)
    if not signed_url:
        return False

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(signed_url)
            resp = urllib.request.urlopen(req, timeout=600)
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            dl_start = time.time()
            last_log = 0
            with open(zip_path, "wb") as f:
                while True:
                    buf = resp.read(1048576)
                    if not buf:
                        break
                    f.write(buf)
                    downloaded += len(buf)
                    now = time.time()
                    if now - last_log >= 2 or not buf:
                        elapsed = now - dl_start
                        pct = downloaded * 100 / total_size if total_size else 0
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        log(
                            f"  DOWNLOAD: {fmt_size(downloaded)} / {fmt_size(total_size)} "
                            f"({pct:.0f}%) @ {fmt_size(speed)}/s",
                        )
                        last_log = now
            log("")
            break
        except Exception as e:
            log(f"")
            log(f"  [retry {attempt+1}/{MAX_RETRIES}] download {artifact_name}: {e}")
            time.sleep(5)
            if os.path.exists(zip_path):
                os.remove(zip_path)
    else:
        return False

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(output_dir)
    except Exception as e:
        log(f"  warn: zip extract failed: {e}")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return False
    if os.path.exists(zip_path):
        os.remove(zip_path)
    if not list(Path(output_dir).glob("*.bin")):
        return False
    return True


def delete_artifact(artifact_name):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        log(f"  warn: delete_artifact: no GITHUB_REPOSITORY")
        return False
    r = subprocess.run(
        ["gh", "api",
         f"/repos/{repo}/actions/artifacts?per_page=100",
         "--jq", f'.artifacts[] | select(.name=="{artifact_name}") | .id',
         "--paginate"],
        capture_output=True, text=True, timeout=30
    )
    ids = [x.strip() for x in r.stdout.strip().split("\n") if x.strip()]
    if not ids:
        return False
    for aid in ids:
        sub = subprocess.run(
            ["gh", "api", "-X", "DELETE",
             f"/repos/{repo}/actions/artifacts/{aid}"],
            capture_output=True, timeout=30
        )
        if sub.returncode != 0:
            log(f"  warn: delete artifact {artifact_name} id={aid} failed: {sub.stderr[:100]}")
        else:
            log(f"  Deleted artifact {artifact_name}")
    return True


def git_push(quiet=True):
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                       capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email",
                        "github-actions[bot]@users.noreply.github.com"],
                       capture_output=True, timeout=5)
        subprocess.run(["git", "add", "completed_links.json"],
                       check=True, capture_output=True, timeout=15)
        r = subprocess.run(
            ["git", "commit", "-m", "update state [skip ci]"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0 and "nothing to commit" not in r.stderr and "nothing to commit" not in r.stdout:
            return
        subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                       capture_output=True, timeout=30)
        subprocess.run(["git", "push"], capture_output=True, timeout=30)
    except Exception:
        pass


def _parse_rclone_json(line):
    if not line.startswith("{"):
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    total = d.get("totalBytes", 0) or 1
    transferred = d.get("bytes", 0)
    speed = d.get("speed", 0)
    eta_sec = d.get("eta", 0)
    if eta_sec < 0:
        eta_sec = 0
    pct = min(round(transferred * 100 / total), 100)
    return {
        "transferred": transferred,
        "total": total,
        "percent": pct,
        "speed": speed,
        "eta_sec": eta_sec
    }


def _read_stderr_thread(proc, lines_list):
    try:
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            lines_list.append(line)
    except:
        pass


def upload_to_gdrive(filepath, target_folder):
    target = f"{GDRIVE_REMOTE}:{BASE_FOLDER}/{target_folder}/"
    try:
        subprocess.run(["rclone", "mkdir", target],
                       capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        log("  Warning: rclone mkdir timed out, continuing anyway")
    file_size = os.path.getsize(filepath)
    log(f"  Uploading {os.path.basename(filepath)} ({fmt_size(file_size)}) to GDrive...")
    proc = subprocess.Popen(
        ["rclone", "copy", filepath, target,
         "--multi-thread-streams=16",
         "--multi-thread-cutoff=50M",
         "--drive-chunk-size=64M",
         "--buffer-size=128M",
         "--tpslimit=10",
         "--tpslimit-burst=10",
         "--stats=3s", "--stats-one-line"],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, bufsize=1
    )
    collected = []
    reader = threading.Thread(target=_read_stderr_thread, args=(proc, collected), daemon=True)
    reader.start()
    last_log = 0
    start_time = time.time()
    last_line = ""
    while reader.is_alive():
        now = time.time()
        elapsed = now - start_time
        if collected:
            line = collected.pop(0).strip().replace("\r", "")
            if not line:
                continue
            m = re.search(r'Transferred:\s+([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+),\s*(\d+)%,\s*([\d.]+\s*\w+/s),\s*ETA\s+(\S+)', line)
            if m:
                line_text = f"  UPLOAD: {m.group(1)} / {m.group(2)} ({m.group(3)}%) @ {m.group(4)} ETA {m.group(5)}"
                if line_text != last_line:
                    log(line_text)
                    last_line = line_text
                    last_log = now
                continue
        # Manual progress: estimate based on elapsed time
        if elapsed > 3 and file_size > 0:
            pct = min(int(elapsed * 100 / max(elapsed + 60, 1)), 99)
            done_mb = file_size * pct / 100 / (1024 * 1024)
            total_mb = file_size / (1024 * 1024)
            spd = done_mb / elapsed if elapsed > 0 else 0
            eta = max(int((total_mb - done_mb) / spd), 0) if spd > 0 else 0
            eta_m = eta // 60
            eta_s = eta % 60
            line_text = f"  UPLOAD: {done_mb:.0f} MB / {total_mb:.0f} MB ({pct}%) @ {spd:.1f} MB/s ETA {eta_m}m{eta_s:02d}s"
            if line_text != last_line:
                log(line_text)
                last_line = line_text
                last_log = now
        time.sleep(2)
    proc.wait(timeout=3600)
    elapsed = time.time() - start_time
    log("")
    if proc.returncode != 0:
        raise RuntimeError(f"rclone upload failed (exit {proc.returncode})")
    # Clean up rclone temp files (tmp*.bin, tmp*.enc etc) from GDrive
    try:
        subprocess.run(
            ["rclone", "delete", target, "--include", "tmp*", "--min-age", "1m"],
            capture_output=True, timeout=60
        )
    except Exception:
        pass
    log(f"  Uploaded to GDrive: {BASE_FOLDER}/{target_folder}/")
    return os.path.basename(filepath)


def auto_trigger_next():
    remaining = 0
    state = load_chunks_history()
    if state:
        for v in state.get("videos", []):
            if v.get("status") not in ("gdrive_uploaded", "done"):
                remaining += 1
    if remaining > 0:
        for attempt in range(3):
            r = subprocess.run(
                ["gh", "workflow", "run",
                 "MEGA to Google Drive Transfer", "--ref", "main"],
                capture_output=True, text=True
            )
            if r.returncode == 0:
                log(f"  Auto-triggered next cycle ({remaining} oversized remaining)")
                return
            log(f"  Trigger attempt {attempt+1} failed: {r.stderr.strip()}")
            time.sleep(10)
        log("  Auto-trigger failed — cron will pick up")


def process_concat_run(video, video_idx, state):
    log(f"\n{'=' * 55}")
    log(f"  CONCAT RUN: {clean_filename(video['filename'])}")
    log(f"{'=' * 55}")

    chunk_files = []
    for ch in video["chunks"]:
        aname = ch["artifact_name"]
        log(f"  Downloading artifact: {aname}...")
        ok = download_artifact(aname)
        expected_fname = f"chunk_{ch['index']:02d}.bin"
        if not ok or not Path(expected_fname).exists():
            log(f"  Artifact {aname} unavailable — resetting to download mode")
            ch["status"] = "pending"
            ch.pop("actual_size", None)
            ch.pop("sha256", None)
            video["status"] = "downloading"
            save_chunks_history(state)
            return False
        if "sha256" in ch:
            sha = hashlib.sha256()
            with open(expected_fname, "rb") as sf:
                while True:
                    buf = sf.read(65536)
                    if not buf:
                        break
                    sha.update(buf)
            if sha.hexdigest() != ch["sha256"]:
                log(f"  SHA256 MISMATCH — chunk corrupted, resetting to download mode")
                ch["status"] = "pending"
                ch.pop("actual_size", None)
                video["status"] = "downloading"
                save_chunks_history(state)
                return False
            log(f"  SHA256 OK")
        chunk_files.append(expected_fname)

    chunk_files.sort(key=lambda x: int(re.search(r"_(\d+)", x).group(1)))
    output_path = video["filename"]

    total_chunks = len(chunk_files)
    concat_total = sum(os.path.getsize(cf) for cf in chunk_files)
    concat_done = 0
    log(f"  Concatenating {total_chunks} chunks...")
    with open(output_path, "wb") as out:
        for i, cf in enumerate(chunk_files, 1):
            with open(cf, "rb") as f:
                while True:
                    buf = f.read(1048576)
                    if not buf:
                        break
                    out.write(buf)
                    concat_done += len(buf)
                    log(
                        f"  CONCAT: {i}/{total_chunks} {fmt_size(concat_done)} / {fmt_size(concat_total)} "
                        f"({concat_done * 100 // concat_total}%)",
                    )
    log("")
    total_size = os.path.getsize(output_path)
    expected = video["total_size"]
    log(f"  Concat done: {fmt_size(total_size)}")

    if total_size != expected:
        log(f"  ERROR: Size mismatch {total_size} vs {expected}")
        return False

    hash_sha256 = hashlib.sha256()
    with open(output_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hash_sha256.update(chunk)
    log(f"  SHA256: {hash_sha256.hexdigest()}")

    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration", "-of", "default=noprint_wrappers=1",
         output_path],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode == 0 and r.stdout.strip():
        log(f"  Duration: {r.stdout.strip()}")
    else:
        log(f"  ERROR: ffprobe rejected the file — data corruption detected")
        for cf in chunk_files:
            if os.path.exists(cf):
                os.remove(cf)
        if os.path.exists(output_path):
            os.remove(output_path)
        for ch in video["chunks"]:
            ch["status"] = "pending"
            ch.pop("actual_size", None)
            ch.pop("sha256", None)
            delete_artifact(ch["artifact_name"])
        video["status"] = "pending"
        save_chunks_history(state)
        return False

    log(f"  Uploading to GDrive...")
    try:
        upload_to_gdrive(output_path, video["target_folder"])
    except RuntimeError as e:
        log(f"  ERROR: {e}")
        return False

    for cf in chunk_files:
        if os.path.exists(cf):
            os.remove(cf)
    if os.path.exists(output_path):
        os.remove(output_path)

    for ch in video["chunks"]:
        delete_artifact(ch["artifact_name"])

    completed_state = load_completed()
    # Update the existing completed entry from "unupload" to "uploaded"
    for item in completed_state.get("completed", []):
        if item.get("url") == video["url"] and item.get("status") == "unupload":
            item["status"] = "uploaded"
            item["completed_at"] = timestamp()
            break

    # Update folder done count
    target_folder = video.get("target_folder", "Oversized")
    if "folders" in completed_state and target_folder in completed_state["folders"]:
        completed_state["folders"][target_folder]["done"] += 1
        fd = completed_state["folders"][target_folder]
        log(f"  Folder [{target_folder}] progress: {fd['done']}/{fd['total']}")

        if fd["done"] >= fd["total"] and fd.get("status") != "completed":
            fd["status"] = "completed"
            log(f"  FOLDER COMPLETE: [{target_folder}] - all {fd['total']} files done!")

            # Activate next pending folder
            next_folder = None
            for name, fdata in completed_state["folders"].items():
                if fdata.get("status") == "pending":
                    fdata["status"] = "active"
                    next_folder = name
                    break
            if next_folder:
                completed_state["current_folder"] = next_folder
                log(f"  Next folder activated: [{next_folder}]")
            else:
                completed_state["current_folder"] = None
                log(f"  ALL FOLDERS COMPLETE!")

    save_completed(completed_state)
    git_push()
    log(f"  State updated + git pushed")

    state["videos"][video_idx]["status"] = "gdrive_uploaded"
    save_chunks_history(state)
    log(f"  Video complete: {clean_filename(video['filename'])}")
    return True


def process_chunk_download(video, video_idx, chunk, state):
    idx = chunk["index"]
    log(f"\n  --- Chunk {idx}/{len(video['chunks'])}: {clean_filename(video['filename'])} ---")
    log(f"  Range: {fmt_size(chunk['start_byte'])} - {fmt_size(chunk['end_byte'])}")

    file_id, key_b64url = parse_mega_url(video["url"])
    aes_key_hex, iv_hex, raw_hex = extract_key_iv(key_b64url)
    iv_nonce = raw_hex[32:48]

    raw_url, file_size = get_download_url(file_id)
    log(f"  MEGA CDN URL obtained, file size: {fmt_size(file_size)}")

    output_name = f"chunk_{idx:02d}.bin"
    log(f"  Downloading with curl -r {chunk['start_byte']}-{chunk['end_byte']}...")
    dl_start = time.time()
    try:
        download_chunk(
            raw_url, aes_key_hex, iv_nonce,
            chunk["start_byte"], chunk["end_byte"],
            output_name
        )
    except RuntimeError as e:
        log(f"  ERROR: {e}")
        return False

    elapsed = time.time() - dl_start
    actual_size = os.path.getsize(output_name)
    log(f"  Downloaded: {fmt_size(actual_size)} in {elapsed:.0f}s")

    sha = hashlib.sha256()
    with open(output_name, "rb") as sf:
        while True:
            buf = sf.read(65536)
            if not buf:
                break
            sha.update(buf)
    chunk_sha = sha.hexdigest()
    log(f"  SHA256: {chunk_sha}")

    video_hash = file_id.lower()
    artifact_name = f"ck_{video_hash}_{idx:02d}"
    set_github_output("chunk_artifact", artifact_name)
    set_github_output("chunk_path", output_name)

    chunk["status"] = "done"
    chunk["actual_size"] = actual_size
    chunk["artifact_name"] = artifact_name
    chunk["sha256"] = chunk_sha

    all_done = all(c["status"] == "done" for c in video["chunks"])
    if all_done:
        video["status"] = "concat_ready"
        log(f"\n  ALL CHUNKS DONE! Video ready for concat.")
    else:
        done_count = sum(1 for c in video["chunks"] if c["status"] == "done")
        log(f"\n  Chunk {idx} done ({done_count}/{len(video['chunks'])})")

    save_chunks_history(state)
    return True


def set_gdrive_uploaded(video_url):
    state = load_completed()
    for item in state.get("completed", []):
        if item.get("url") == video_url and item.get("status") == "unupload":
            item["status"] = "uploaded"
            item["completed_at"] = timestamp()
            break
    save_completed(state)


def sync_gdrive_status(ch_state, completed_state):
    """Sync gdrive_status from completed list. If already uploaded to GDrive, mark uploaded regardless of chunk status."""
    unupload_items = get_unupload_items(completed_state)
    url_to_status = {it["url"]: "pending" for it in unupload_items}
    uploaded_items = [it for it in completed_state.get("completed", []) if it.get("status") == "uploaded"]
    for it in uploaded_items:
        url_to_status[it["url"]] = "uploaded"
    for v in ch_state.get("videos", []):
        url = v.get("url", "")
        if url not in url_to_status:
            continue
        desired = url_to_status[url]
        if desired == "uploaded":
            v["gdrive_status"] = "uploaded"
            continue
        v["gdrive_status"] = desired


def add_chunks_summary(state):
    videos = state.get("videos", [])
    total = len(videos)
    uploaded = sum(1 for v in videos if v.get("gdrive_status") == "uploaded")
    concat_ready = sum(1 for v in videos if v.get("status") == "concat_ready" and v.get("gdrive_status") != "uploaded")
    downloading = sum(1 for v in videos if v.get("status") == "downloading" and v.get("gdrive_status") != "uploaded")
    pending = sum(1 for v in videos if v.get("status") == "pending")
    state["summary"] = {
        "total": total,
        "gdrive_uploaded": uploaded,
        "concat_ready": concat_ready,
        "downloading": downloading,
        "pending": pending,
        "status": "completed" if uploaded == total > 0 else "in_progress"
    }


def print_summary(state):
    unupload = [it for it in state.get("completed", []) if it.get("status") == "unupload" and it.get("oversized")]
    uploaded = [it for it in state.get("completed", []) if it.get("status") == "uploaded" and it.get("oversized")]
    total = len(unupload) + len(uploaded)
    if total > 0:
        log(f"\n  {'=' * 45}")
        log(f"  OVERSIZED SUMMARY: {len(uploaded)}/{total} uploaded to GDrive")
        for it in unupload:
            log(f"    PENDING: {clean_filename(it.get('filename', '?'))}")
        for it in uploaded:
            log(f"    UPLOADED: {clean_filename(it.get('filename', '?'))}")
        log(f"  {'=' * 45}")


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


def main():
    sys.stdout.reconfigure(line_buffering=True)
    log("=" * 55)
    log(f"  Oversized Processor | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 55)

    # Clean up any rclone temp files from previous runs
    cleanup_gdrive_temps()

    completed_state = load_completed()
    unupload_items = get_unupload_items(completed_state)
    total_unupload = len(unupload_items)
    total_oversized = sum(1 for item in completed_state.get("completed", []) if item.get("oversized"))
    done_oversized = sum(1 for item in completed_state.get("completed", []) if item.get("status") == "uploaded" and item.get("oversized"))

    if total_unupload == 0 and done_oversized == 0:
        log("  No oversized videos to process.")
        ch_state = load_chunks_history()
        if ch_state:
            remaining = sum(
                1 for v in ch_state.get("videos", [])
                if v.get("status") in ("downloading", "concat_ready")
            )
            if remaining > 0:
                log(f"  But chunks_history has {remaining} in-progress videos - continuing processing")
            else:
                log("  All oversized videos completed!")
                print_summary(completed_state)
                return
        else:
            print_summary(completed_state)
            return

    if total_unupload == 0 or (total_oversized > 0 and done_oversized == total_oversized):
        log("  All oversized videos already uploaded to GDrive!")
        print_summary(completed_state)
        return

    state = load_chunks_history()
    first_run = state is None

    if not first_run:
        sync_gdrive_status(state, completed_state)
        for v in state.get("videos", []):
            fixed = False
            for ch in v.get("chunks", []):
                if ch.get("status") == "done":
                    aname = ch.get("artifact_name", "")
                    if aname:
                        aid = find_artifact_id(aname)
                        if not aid:
                            log(f"  fix: Artifact {aname} missing — resetting chunk {ch['index']} to pending")
                            ch["status"] = "pending"
                            ch.pop("actual_size", None)
                            fixed = True
            if v.get("gdrive_status") == "uploaded" and v.get("status") not in ("gdrive_uploaded", "done"):
                log(f"  fix: {clean_filename(v['filename'])} already uploaded to GDrive, cleaning up")
                v["status"] = "done"
                for ch in v.get("chunks", []):
                    if ch.get("status") != "done":
                        ch["status"] = "done"
                save_chunks_history(state)
            elif fixed:
                v["status"] = "downloading"
                save_chunks_history(state)

    if not first_run and state.get("version", 1) != PROCESSOR_VERSION:
        log(f"  Processor version changed {state.get('version', 1)} → {PROCESSOR_VERSION}, resetting all chunks...")
        for v in state.get("videos", []):
            for ch in v.get("chunks", []):
                delete_artifact(ch.get("artifact_name", ""))
            v["status"] = "pending"
            for ch in v.get("chunks", []):
                ch["status"] = "pending"
                ch.pop("actual_size", None)
                ch.pop("sha256", None)
        state["version"] = PROCESSOR_VERSION
        save_chunks_history(state)
        log("  All chunks reset — clean re-download with fixed key derivation")

    if first_run:
        log("  First run — initializing chunks history...")
        videos = []
        seen_urls = set()
        for ov in unupload_items:
            if ov["url"] in seen_urls:
                continue
            seen_urls.add(ov["url"])
            file_id, key_b64url = parse_mega_url(ov["url"])
            aes_key_hex, iv_hex, raw_hex = extract_key_iv(key_b64url)
            filename = ov.get("filename", f"unknown_{file_id}.mp4")
            total_size = ov.get("size", 0)
            if not total_size:
                _, total_size = get_download_url(file_id)
            chunks = calculate_chunks(total_size)
            for ch in chunks:
                ch["artifact_name"] = f"ck_{file_id.lower()}_{ch['index']:02d}"
            videos.append({
                "url": ov["url"],
                "filename": filename,
                "target_folder": ov.get("target_folder", "Oversized"),
                "file_id": file_id,
                "total_size": total_size,
                "total_chunks": len(chunks),
                "chunks": chunks,
                "status": "pending"
            })
        state = {"videos": videos, "current_index": 0, "version": PROCESSOR_VERSION}
        save_chunks_history(state)
        log(f"  Initialized {len(videos)} oversized videos")
    else:
        log(f"  Resuming from chunks_history ({len(state.get('videos', []))} videos)")
        existing_urls = {v["url"] for v in state.get("videos", [])}
        new_count = 0
        for ov in unupload_items:
            if ov["url"] not in existing_urls:
                file_id, key_b64url = parse_mega_url(ov["url"])
                aes_key_hex, iv_hex, raw_hex = extract_key_iv(key_b64url)
                filename = ov.get("filename", f"unknown_{file_id}.mp4")
                total_size = ov.get("size", 0)
                if not total_size:
                    _, total_size = get_download_url(file_id)
                chunks = calculate_chunks(total_size)
                for ch in chunks:
                    ch["artifact_name"] = f"ck_{file_id.lower()}_{ch['index']:02d}"
                state.setdefault("videos", []).append({
                    "url": ov["url"],
                    "filename": filename,
                    "target_folder": ov.get("target_folder", "Oversized"),
                    "file_id": file_id,
                    "total_size": total_size,
                    "total_chunks": len(chunks),
                    "chunks": chunks,
                    "status": "pending"
                })
                new_count += 1
        if new_count:
            save_chunks_history(state)
            log(f"  Added {new_count} new oversized video(s) to chunks_history")

    idx = state.get("current_index", 0)
    videos = state.get("videos", [])

    while idx < len(videos):
        video = videos[idx]
        status = video.get("status", "pending")
        log(f"\n  Current: [{idx+1}/{len(videos)}] {clean_filename(video['filename'])} ({status})")
        print(f"::notice title=Oversized::{idx+1}/{len(videos)} {clean_filename(video['filename'])} ({status})", flush=True)

        if status == "gdrive_uploaded" or status == "done":
            idx += 1
            state["current_index"] = idx
            save_chunks_history(state)
            continue

        if video.get("gdrive_status") == "uploaded":
            log(f"  Already uploaded to GDrive, marking done: {clean_filename(video['filename'])}")
            video["status"] = "done"
            for ch in video.get("chunks", []):
                if ch.get("status") != "done":
                    ch["status"] = "done"
            idx += 1
            state["current_index"] = idx
            save_chunks_history(state)
            continue

        if status == "concat_ready":
            ok = process_concat_run(video, idx, state)
            if ok:
                idx += 1
                state["current_index"] = idx
                save_chunks_history(state)
                continue
            else:
                log(f"  Concat failed, will retry next run")
                break

        if video["status"] == "pending":
            video["status"] = "downloading"
            save_chunks_history(state)

        all_ok = True
        while all_ok:
            pending_chunk = None
            for ch in video["chunks"]:
                if ch["status"] == "pending":
                    pending_chunk = ch
                    break

            if pending_chunk is None:
                video["status"] = "concat_ready"
                save_chunks_history(state)
                log(f"  All chunks downloaded for this video.")
                print(f"::notice::All chunks done for {clean_filename(video['filename'])} — concat ready", flush=True)
                break

            ok = process_chunk_download(video, idx, pending_chunk, state)
            if not ok:
                log(f"  Chunk download failed, will retry next run")
                all_ok = False
                break

            remaining = sum(1 for ch in video["chunks"] if ch["status"] == "pending")
            if remaining > 0:
                log(f"  {remaining} chunks remaining for this video — continuing...")

        if video["status"] == "concat_ready" and video.get("gdrive_status") != "uploaded":
            log(f"  All chunks done, attempting concat + upload...")
            ok = process_concat_run(video, idx, state)
            if ok:
                log(f"  Video uploaded to GDrive: {clean_filename(video['filename'])}")
                print(f"::notice::UPLOADED oversized: {clean_filename(video['filename'])}", flush=True)
                idx += 1
                state["current_index"] = idx
                save_chunks_history(state)
                continue
            else:
                log(f"  Concat failed, will retry next run")
                break

        break

    state["current_index"] = idx
    completed_state = load_completed()
    sync_gdrive_status(state, completed_state)
    add_chunks_summary(state)
    save_chunks_history(state)

    remaining_videos = sum(
        1 for v in videos[idx:] if v.get("status") not in ("gdrive_uploaded", "done")
    )
    if remaining_videos > 0:
        log(f"\n  {remaining_videos} oversized videos remaining — next cycle needed")
        print("::notice::Oversized files remaining - next cycle will continue", flush=True)
        print_summary(completed_state)
        auto_trigger_next()
    else:
        log(f"\n  ALL OVERSIZED VIDEOS COMPLETE!")
        print("::notice::All oversized videos processed successfully", flush=True)
        if state.get("videos"):
            for v in state["videos"]:
                for ch in v.get("chunks", []):
                    delete_artifact(ch.get("artifact_name", ""))
            sync_gdrive_status(state, completed_state)
            add_chunks_summary(state)
            save_chunks_history(state)
        print_summary(completed_state)

    log("=" * 55)


if __name__ == "__main__":
    main()
