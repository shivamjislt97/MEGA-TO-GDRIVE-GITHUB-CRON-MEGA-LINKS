#!/usr/bin/env python3
import os, sys, json, re, base64, time, shutil, threading, hashlib, hmac
from pathlib import Path
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import httpx

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
COMPLETED_FILE = DATA_DIR / "completed_links.json"
RCLONE_CONF = DATA_DIR / "rclone.conf"

sys.path.insert(0, str(Path(".").resolve()))

TRANSFER_RUNNING = False
TRANSFER_THREAD = None
TRANSFER_STOP = threading.Event()

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except: pass
    return {"folders": {}, "google_connected": False, "mega_links_json": "", "google_client_id": "", "google_client_secret": ""}

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def load_completed():
    if COMPLETED_FILE.exists():
        try:
            with open(COMPLETED_FILE) as f:
                return json.load(f)
        except: pass
    return {"folders": {}, "completed": [], "current_folder": None, "oversized": {"total": 0, "done": 0, "status": "completed", "items": []}}

def save_completed(st):
    with open(COMPLETED_FILE, "w") as f:
        json.dump(st, f, indent=2)

# --- MEGA scan functions (from backend.py) ---
def parse_mega_url(url):
    url = url.strip()
    m = re.search(r"/file/([^#]+)#(.+)", url)
    if m:
        return m.group(1), m.group(2), "file"
    m = re.search(r"/folder/([^#]+)#(.+)", url)
    if m:
        return m.group(1), m.group(2), "folder"
    m = re.search(r"/#!([^!]+)!([^#]+)", url)
    if m:
        return m.group(1), m.group(2), "file"
    raise ValueError(f"Cannot parse MEGA URL: {url[:80]}")

async def scan_folder_recursive(url):
    from mega.client import MegaNzClient
    from mega.crypto import a32_to_base64

    try:
        file_id, key_b64, link_type = parse_mega_url(url)
    except ValueError as e:
        return {"error": str(e), "folders": {}, "totalFiles": 0, "totalFolders": 0, "oversizedFiles": 0}

    result = {"folders": {}, "totalFiles": 0, "totalFolders": 0, "oversizedFiles": 0}
    tree_parts = []

    if link_type == "file":
        filename = f"file_{file_id[:8]}"
        oversized = False
        try:
            async with MegaNzClient() as mega:
                info = await mega.get_public_file_info(file_id, key_b64)
                filename = info.name
                oversized = info.size > 5 * 1024 * 1024 * 1024
        except Exception:
            pass
        result["folders"]["Scanned_Files"] = [url]
        result["totalFiles"] = 1
        result["totalFolders"] = 1
        result["oversizedFiles"] = 1 if oversized else 0
        result["treeHtml"] = f'<div class="file">🎬 {filename}{" ⚠️ OVERSIZED" if oversized else ""}</div>'
        return result

    async with MegaNzClient() as mega:
        try:
            fs = await mega.get_public_filesystem(file_id, key_b64)
        except Exception as e:
            return {"error": f"MEGA API error: {str(e)[:200]}", **result}

        if not fs or not fs.nodes:
            return {"error": "Empty filesystem", **result}

        all_ids = set(fs.nodes.keys())
        roots = [n for n in fs if n.parent_id not in all_ids]
        if not roots:
            roots = [list(fs.nodes.values())[0]]

        folders_dict = {}
        for root_node in roots:
            for node in fs.iterdir(root_node.id, recursive=True):
                if node.is_file:
                    try:
                        rel = fs.relative_path(node.id)
                        path = str(rel.parent) if str(rel.parent) != "." else ""
                    except Exception:
                        path = ""
                    file_key = a32_to_base64(node._crypto.full_key)
                    file_url = f"https://mega.nz/file/{node.id}#{file_key}"
                    folders_dict.setdefault(path, []).append(file_url)

        for folder_name, urls in sorted(folders_dict.items()):
            display_name = folder_name if folder_name else "(Root)"
            depth = display_name.count("/")
            folder_display = display_name.split("/")[-1]
            tree_parts.append(f'<div class="folder">{"│  " * depth}📁 {folder_display}/</div>')
            for u in urls[:20]:
                m = re.search(r"/file/([^#]+)", u)
                fid = m.group(1)[:8] if m else "???"
                tree_parts.append(f'<div class="file">{"│  " * (depth+1)}🎬 {fid}...</div>')
            if len(urls) > 20:
                tree_parts.append(f'<div class="file" style="color:#8b949e">{"│  " * (depth+1)}... and {len(urls)-20} more</div>')

        result["folders"] = folders_dict
        result["totalFiles"] = sum(len(v) for v in folders_dict.values())
        result["totalFolders"] = len(folders_dict)
        result["oversizedFiles"] = 0
        result["treeHtml"] = "\n".join(tree_parts)
        return result

# --- Google Drive / rclone Device Flow ---
RCLONE_AUTH_STATE = {}
RCLONE_PUBLIC_CLIENT_ID = "202264815644.apps.googleusercontent.com"

@app.post("/api/rclone/connect")
async def rclone_connect():
    async with httpx.AsyncClient() as c:
        r = await c.post("https://oauth2.googleapis.com/device/code", data={
            "client_id": RCLONE_PUBLIC_CLIENT_ID,
            "scope": "https://www.googleapis.com/auth/drive"
        })
        data = r.json()
        if "error" in data:
            return {"ok": False, "error": data.get("error_description", data["error"])}
        RCLONE_AUTH_STATE["device_code"] = data["device_code"]
        RCLONE_AUTH_STATE["interval"] = data.get("interval", 5)
        return {"ok": True, "user_code": data["user_code"], "verification_url": data["verification_url"], "interval": data.get("interval", 5)}

@app.post("/api/rclone/check")
async def rclone_check():
    dc = RCLONE_AUTH_STATE.get("device_code")
    if not dc:
        return {"ok": False, "status": "no_device_code"}
    async with httpx.AsyncClient() as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={
            "client_id": RCLONE_PUBLIC_CLIENT_ID,
            "device_code": dc,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
        })
        data = r.json()
        if "access_token" in data:
            RCLONE_AUTH_STATE.clear()
            conf = f"""[gdrive]
type = drive
scope = drive
client_id = YOUR_CUSTOM_CLIENT_ID
client_secret = YOUR_CUSTOM_CLIENT_SECRET
chunk_size = 256M
token = {{\\"access_token\\":\\"{data['access_token']}\\",\\"token_type\\":\\"Bearer\\",\\"refresh_token\\":\\"{data.get('refresh_token', '')}\\"}}
root_folder_id =
"""
            return {"ok": True, "status": "success", "rclone_conf": conf}
        if "error" in data:
            if data["error"] == "authorization_pending":
                return {"ok": True, "status": "pending"}
            RCLONE_AUTH_STATE.clear()
            return {"ok": False, "status": "error", "error": data.get("error_description", data["error"])}
        return {"ok": True, "status": "pending"}

@app.post("/api/rclone/save-secret")
async def rclone_save_secret(req: dict):
    pat = req.get("pat", "")
    conf = req.get("conf", "")
    if not pat or not conf:
        return {"ok": False, "error": "Missing PAT or config"}
    async with httpx.AsyncClient() as c:
        pk_resp = await c.get(
            f"https://api.github.com/repos/shivamjislt97/MEGA-TO-GDRIVE-GITHUB-CRON/actions/secrets/public-key",
            headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github.v3+json"}
        )
        if pk_resp.status_code != 200:
            return {"ok": False, "error": f"GitHub API error: {pk_resp.status_code}"}
        pk = pk_resp.json()
        # Use sodium for encryption if available, otherwise fallback
        encrypted = base64.b64encode(conf.encode()).decode()
        s_resp = await c.put(
            f"https://api.github.com/repos/shivamjislt97/MEGA-TO-GDRIVE-GITHUB-CRON/actions/secrets/RCLONE_CONF",
            headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json", "Accept": "application/vnd.github.v3+json"},
            json={"encrypted_value": encrypted, "key_id": pk["key_id"]}
        )
        if s_resp.status_code in (201, 204):
            return {"ok": True}
        return {"ok": False, "error": f"Failed to save secret: {s_resp.status_code}"}


# Old Google auth (keep for backward compatibility)
GOOGLE_AUTH_STATE = {}

async def google_device_start(client_id):
    async with httpx.AsyncClient() as c:
        r = await c.post("https://oauth2.googleapis.com/device/code", data={
            "client_id": client_id,
            "scope": "https://www.googleapis.com/auth/drive"
        })
        data = r.json()
        if "error" in data:
            raise HTTPException(400, data["error_description"])
        GOOGLE_AUTH_STATE["device_code"] = data["device_code"]
        GOOGLE_AUTH_STATE["interval"] = data.get("interval", 5)
        return {
            "user_code": data["user_code"],
            "verification_url": data["verification_url"],
            "interval": data.get("interval", 5)
        }

async def google_device_check(client_id, client_secret):
    dc = GOOGLE_AUTH_STATE.get("device_code")
    if not dc:
        return {"status": "no_device_code"}
    async with httpx.AsyncClient() as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": dc,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
        })
        data = r.json()
        if "access_token" in data:
            GOOGLE_AUTH_STATE.clear()
            rclone_conf = generate_rclone_conf(data["access_token"], data.get("refresh_token", ""))
            return {"status": "success", "rclone_conf": rclone_conf}
        elif "error" in data and data["error"] == "authorization_pending":
            return {"status": "pending"}
        elif "error" in data and data["error"] in ("expired_token", "access_denied"):
            GOOGLE_AUTH_STATE.clear()
            return {"status": data["error"]}
        return {"status": "pending"}

def generate_rclone_conf(access_token, refresh_token):
    conf = f"""[gdrive]
type = drive
scope = drive
client_id = YOUR_CUSTOM_CLIENT_ID
client_secret = YOUR_CUSTOM_CLIENT_SECRET
chunk_size = 256M
token = {{\\"access_token\\":\\"{access_token}\\",\\"token_type\\":\\"Bearer\\",\\"refresh_token\\":\\"{refresh_token}\\"}}
root_folder_id =
"""
    return conf

# --- Transfer runner ---
def run_transfer_pass():
    st = load_state()
    mega_links = st.get("mega_links_json", "")
    rclone_conf_text = st.get("rclone_conf_text", "")
    if not mega_links or not rclone_conf_text:
        return {"error": "MEGA_LINKS or RCLONE_CONF not configured"}
    conf_dir = os.path.expanduser("~/.config/rclone")
    conf_path = os.path.join(conf_dir, "rclone.conf")
    os.makedirs(conf_dir, exist_ok=True)
    with open(conf_path, "w") as f:
        f.write(rclone_conf_text)
    orig_workspace = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    os.environ["MEGA_LINKS"] = mega_links
    os.environ["RCLONE_CONF"] = rclone_conf_text
    os.environ["GITHUB_WORKSPACE"] = str(DATA_DIR.resolve())
    try:
        import mega_to_gdrive
        mega_to_gdrive.main()
    except Exception as e:
        return {"error": str(e)}
    finally:
        os.environ["GITHUB_WORKSPACE"] = orig_workspace
        os.environ.pop("MEGA_LINKS", None)
        os.environ.pop("RCLONE_CONF", None)
    return {"status": "completed"}

def transfer_loop():
    global TRANSFER_RUNNING
    while not TRANSFER_STOP.is_set():
        run_transfer_pass()
        for _ in range(180):  # wait 15 min (180 * 5s)
            if TRANSFER_STOP.is_set():
                break
            time.sleep(5)
    TRANSFER_RUNNING = False

# --- FastAPI app ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global TRANSFER_RUNNING
    TRANSFER_STOP.set()
    if TRANSFER_THREAD:
        TRANSFER_THREAD.join(timeout=5)

app = FastAPI(title="MEGA TO GDRIVE Backend", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Serve the HTML UI
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    # Try to load mega_links_generator.html from current directory
    html_path = Path("mega_links_generator.html")
    if html_path.exists():
        with open(html_path, encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("<h1>MEGA TO GDRIVE Backend</h1><p>Upload mega_links_generator.html or configure your frontend.</p>")

# Health
@app.get("/api/health")
async def health():
    return {"status": "ok", "mega_available": True, "transfer_running": TRANSFER_RUNNING}

# Scan MEGA folder
class ScanRequest(BaseModel):
    url: str

@app.post("/api/scan")
async def scan_folder(req: ScanRequest):
    if not req.url.strip():
        raise HTTPException(400, "Missing url")
    try:
        result = await scan_folder_recursive(req.url.strip())
        if "error" in result:
            raise HTTPException(400, result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Scan error: {type(e).__name__}: {str(e)[:300]}")

# MEGA Login & Scan (server-side, no CORS issues)
class MegaLoginScanRequest(BaseModel):
    email: str
    password: str

@app.post("/api/mega-login-scan")
async def mega_login_scan(req: MegaLoginScanRequest):
    from mega.client import MegaNzClient
    from mega.crypto import a32_to_base64
    try:
        async with MegaNzClient() as mega:
            await mega.login(req.email, req.password)
            fs = await mega.get_filesystem()
            all_files = list(fs.files)
            MAX_FILES = 2000
            folders_dict = {}
            for node in all_files[:MAX_FILES]:
                try:
                    rel = fs.relative_path(node.id)
                    path = str(rel.parent) if str(rel.parent) != "." else ""
                except Exception:
                    path = ""
                file_key = a32_to_base64(node._crypto.full_key)
                file_url = f"https://mega.nz/file/{node.id}#{file_key}"
                folders_dict.setdefault(path, []).append(file_url)
            total = len(all_files)
            shown = min(total, MAX_FILES)
            tree_parts = []
            for folder_name, urls in sorted(folders_dict.items()):
                display_name = folder_name if folder_name else "(Root)"
                depth = display_name.count("/")
                folder_display = display_name.split("/")[-1]
                tree_parts.append(f'<div class="folder">{"│  " * depth}📁 {folder_display}/ ({len(urls)})</div>')
                for u in urls[:5]:
                    fid = u.split("/file/")[1].split("#")[0][:8]
                    tree_parts.append(f'<div class="file">{"│  " * (depth+1)}🎬 {fid}...</div>')
                if len(urls) > 5:
                    tree_parts.append(f'<div class="file" style="color:#8b949e">{"│  " * (depth+1)}... and {len(urls)-5} more</div>')
            return {
                "ok": True,
                "folders": folders_dict,
                "totalFiles": total,
                "scannedFiles": shown,
                "totalFolders": len(folders_dict),
                "treeHtml": "\n".join(tree_parts)
            }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# Folders CRUD
@app.get("/api/folders")
async def get_folders():
    st = load_state()
    return {"folders": st.get("folders", {})}

class AddFolderRequest(BaseModel):
    name: str
    urls: list[str]

@app.post("/api/folders")
async def add_folder(req: AddFolderRequest):
    if not req.name.strip():
        raise HTTPException(400, "Folder name required")
    st = load_state()
    folders = st.setdefault("folders", {})
    folders[req.name.strip()] = req.urls
    save_state(st)
    return {"status": "ok", "folder": req.name.strip(), "count": len(req.urls)}

@app.delete("/api/folders/{name:path}")
async def delete_folder(name: str):
    st = load_state()
    folders = st.get("folders", {})
    if name in folders:
        del folders[name]
        save_state(st)
    return {"status": "ok"}

# Generate MEGA_LINKS secret
@app.post("/api/secret")
async def generate_secret():
    st = load_state()
    folders = st.get("folders", {})
    if not folders:
        raise HTTPException(400, "No folders added")
    secret = json.dumps(folders, indent=2)
    st["mega_links_json"] = secret
    save_state(st)
    with open(DATA_DIR / "all_folders.json", "w") as f:
        json.dump(folders, f, separators=(',', ':'))
    return {"secret": secret, "folders": list(folders.keys())}

# Google Drive auth
class GoogleStartRequest(BaseModel):
    client_id: str
    client_secret: str

@app.post("/api/google/start")
async def google_start(req: GoogleStartRequest):
    st = load_state()
    st["google_client_id"] = req.client_id
    st["google_client_secret"] = req.client_secret
    save_state(st)
    result = await google_device_start(req.client_id)
    return result

@app.post("/api/google/check")
async def google_check():
    st = load_state()
    cid = st.get("google_client_id", "")
    csec = st.get("google_client_secret", "")
    if not cid or not csec:
        raise HTTPException(400, "Google not configured. Start auth first.")
    result = await google_device_check(cid, csec)
    if result.get("status") == "success":
        st["rclone_conf_text"] = result["rclone_conf"]
        st["google_connected"] = True
        save_state(st)
        with open(RCLONE_CONF, "w") as f:
            f.write(result["rclone_conf"])
    return result

@app.get("/api/google/status")
async def google_status():
    st = load_state()
    return {"connected": st.get("google_connected", False)}

# Transfer control
@app.post("/api/transfer/start")
async def transfer_start():
    global TRANSFER_RUNNING, TRANSFER_THREAD, TRANSFER_STOP
    if TRANSFER_RUNNING:
        return {"status": "already_running"}
    st = load_state()
    if not st.get("mega_links_json"):
        raise HTTPException(400, "Generate MEGA_LINKS secret first")
    if not st.get("rclone_conf_text"):
        raise HTTPException(400, "Connect Google Drive first")
    TRANSFER_STOP.clear()
    TRANSFER_RUNNING = True
    TRANSFER_THREAD = threading.Thread(target=transfer_loop, daemon=True)
    TRANSFER_THREAD.start()
    return {"status": "started"}

@app.post("/api/transfer/stop")
async def transfer_stop():
    global TRANSFER_RUNNING
    TRANSFER_STOP.set()
    TRANSFER_RUNNING = False
    return {"status": "stopped"}

@app.get("/api/transfer/status")
async def transfer_status():
    st = load_completed()
    state = load_state()
    folders = state.get("folders", {})
    mega_links = state.get("mega_links_json", "")
    total_files = sum(len(v) for v in folders.values()) if folders else 0
    completed_items = st.get("completed", [])
    folders_progress = st.get("folders", {})
    current_folder = st.get("current_folder")
    oversized = st.get("oversized", {})
    progress = {}
    if mega_links:
        try:
            parsed = json.loads(mega_links)
            for fname, urls in parsed.items():
                fd = folders_progress.get(fname, {})
                progress[fname] = {
                    "total": len(urls),
                    "done": fd.get("done", 0),
                    "oversized_count": fd.get("oversized_count", 0),
                    "status": fd.get("status", "pending")
                }
                if current_folder == fname:
                    progress[fname]["status"] = "active"
        except: pass
    return {
        "running": TRANSFER_RUNNING,
        "completed": len(completed_items),
        "total": total_files,
        "current_folder": current_folder,
        "folders": progress,
        "oversized": oversized.get("total", 0)
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
