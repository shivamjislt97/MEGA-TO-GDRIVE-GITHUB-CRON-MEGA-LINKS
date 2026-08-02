#!/usr/bin/env python3
import os, re, sys, json, asyncio
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

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

def build_tree_html(folders_dict):
    parts = []
    for folder_name, urls in sorted(folders_dict.items()):
        depth = folder_name.count("/")
        folder_display = folder_name.split("/")[-1]
        parts.append(f'<div class="folder">{"│  " * depth}📁 {folder_display}/</div>')
        for u in urls[:20]:
            m = re.search(r"/file/([^#]+)", u)
            fid = m.group(1)[:8] if m else "???"
            parts.append(f'<div class="file">{"│  " * (depth+1)}🎬 {fid}...</div>')
        if len(urls) > 20:
            parts.append(f'<div class="file" style="color:#8b949e">{"│  " * (depth+1)}... and {len(urls)-20} more</div>')
    return "\n".join(parts)

async def _scan_async(url):
    from mega.client import MegaNzClient
    from mega.crypto import a32_to_base64

    file_id, key_b64, link_type = parse_mega_url(url)
    result = {"folders": {}, "totalFiles": 0, "totalFolders": 0, "oversizedFiles": 0}

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
        fs = await mega.get_public_filesystem(file_id, key_b64)
        if not fs or not fs.nodes:
            result["error"] = "Empty filesystem"
            return result

        all_ids = set(fs.nodes.keys())
        roots = [n for n in fs if n.parent_id not in all_ids]
        if not roots:
            roots = [list(fs.nodes.values())[0]]

        folders = {}
        for root_node in roots:
            for node in fs.iterdir(root_node.id, recursive=True):
                if node.is_file:
                    try:
                        path = str(fs.relative_path(node.id).parent)
                    except Exception:
                        path = "Scanned"
                    file_key = a32_to_base64(node._crypto.full_key)
                    folders.setdefault(path or "Scanned", []).append(
                        f"https://mega.nz/file/{node.id}#{file_key}"
                    )

        result["folders"] = folders
        result["totalFiles"] = sum(len(v) for v in folders.values())
        result["totalFolders"] = len(folders)
        result["treeHtml"] = build_tree_html(folders)
        return result

def scan_folder_recursive(url):
    return asyncio.run(_scan_async(url))

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "MEGA Folder Scanner API",
        "version": "2.0",
        "endpoints": {
            "/scan": "POST — Scan a MEGA folder/file URL",
            "/health": "GET — Health check"
        }
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing 'url' field"}), 400
    try:
        result = scan_folder_recursive(url)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Scan error: {str(e)[:300]}"}), 500

def cli_mode():
    import argparse
    parser = argparse.ArgumentParser(description="MEGA Folder Scanner CLI")
    parser.add_argument("--scan-only", action="store_true", help="Run in CLI scan mode")
    parser.add_argument("--url", required=True, help="MEGA folder/file URL")
    parser.add_argument("--output", required=True, help="Output JSON file path")
    args = parser.parse_args()
    result = scan_folder_recursive(args.url)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2), flush=True)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--scan-only":
        cli_mode()
    else:
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port, debug=True)
