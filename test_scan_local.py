#!/usr/bin/env python3
"""Local test of scan_folder_recursive logic without FastAPI deps."""
import json, urllib.request, base64, re, sys
from collections import defaultdict

# Copy the scan logic from app.py here for testing
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
        return m.group(1), m.group(2), "file"
    m = re.search(r"/folder/([^#]+)#(.+)", url)
    if m:
        return m.group(1), m.group(2), "folder"
    m = re.search(r"/#!([^!]+)!([^#]+)", url)
    if m:
        return m.group(1), m.group(2), "file"
    raise ValueError(f"Cannot parse MEGA URL: {url[:80]}")

def scan_folder_recursive(url):
    try:
        file_id, key_b64, link_type = parse_mega_url(url)
    except ValueError as e:
        return {"error": str(e), "folders": {}, "totalFiles": 0, "totalFolders": 0}

    result = {"folders": {}, "totalFiles": 0, "totalFolders": 0}
    tree_parts = []

    if link_type == "file":
        filename = f"file_{file_id[:8]}"
        try:
            from mega import Mega
            info = Mega().get_public_url_info(url)
            filename = info.get("name", filename)
        except Exception:
            pass
        result["folders"]["Scanned_Files"] = [url]
        result["totalFiles"] = 1
        result["totalFolders"] = 1
        return result

    print(f"  Fetching folder structure via API (handle: {file_id})...")
    try:
        params = json.dumps([{"a": "f", "c": 1, "ca": 1}]).encode()
        api_url = f"https://g.api.mega.co.nz/cs?id=0&n={file_id}"
        req = urllib.request.Request(api_url, data=params, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=30)
        raw_text = resp.read().decode()
        api_data = json.loads(raw_text)
        if not api_data or not isinstance(api_data, list):
            return {"error": f"Unexpected API response: {raw_text[:200]}", **result}
        first = api_data[0]
        if isinstance(first, int):
            return {"error": f"MEGA API error code: {first}", **result}
        if not isinstance(first, dict):
            return {"error": f"Unexpected data type: {type(first).__name__}", **result}
        nodes = first.get("f", [])
        if not nodes:
            return {"error": "No nodes found in folder", **result}
    except Exception as e:
        return {"error": f"MEGA API error: {str(e)[:200]}", **result}

    try:
        from mega.crypto import decrypt_attr, base64_to_a32, a32_to_base64, decrypt_key
    except ImportError:
        return {"error": "mega.py crypto not available", **result}

    children = defaultdict(list)
    for n in nodes:
        children[n["p"]].append(n)

    all_handles = set(n["h"] for n in nodes)
    all_parents = set(n["p"] for n in nodes)
    root_handle = next((p for p in all_parents if p not in all_handles), file_id)
    print(f"  Root handle: {root_handle}, Total nodes: {len(nodes)}")

    # Count types
    node_types = defaultdict(int)
    for n in nodes:
        node_types[n["t"]] += 1
    print(f"  Node types: {dict(node_types)}")

    def derive_key(node, parent_key):
        k_field = node.get("k", "")
        node_handle = node["h"]
        keys_dict = {}
        for part in k_field.split("/"):
            if ":" in part:
                h, k = part.split(":", 1)
                keys_dict[h] = k
        if node_handle not in keys_dict:
            return parent_key
        try:
            enc_key = base64_to_a32(keys_dict[node_handle])
            return decrypt_key(enc_key, parent_key)
        except Exception:
            return parent_key

    def decrypt_node_name(node, node_key):
        attrs_raw = node.get("a", "")
        try:
            raw_attr = mega_b64decode(attrs_raw)
            attrs = decrypt_attr(raw_attr, node_key)
            if attrs and "n" in attrs:
                return attrs["n"]
        except Exception:
            pass
        return None

    def walk(parent_handle, parent_key, path=""):
        for node in children.get(parent_handle, []):
            node_type = node["t"]
            node_key = derive_key(node, parent_key)
            name = decrypt_node_name(node, node_key) or f"node_{node['h'][:8]}"
            subpath = f"{path}/{name}" if path else name

            if node_type == 0:  # File
                k = (node_key[0] ^ node_key[4], node_key[1] ^ node_key[5],
                     node_key[2] ^ node_key[6], node_key[3] ^ node_key[7])
                file_key = a32_to_base64(k)
                export_url = f"https://mega.nz/file/{node['h']}#{file_key}"
                folder_path = "/".join(subpath.split("/")[:-1]) or "Scanned"
                if folder_path not in result["folders"]:
                    result["folders"][folder_path] = []
                result["folders"][folder_path].append(export_url)
                result["totalFiles"] += 1
            elif node_type == 1:  # Folder
                walk(node["h"], node_key, subpath)

    parent_key = base64_to_a32(key_b64)
    walk(root_handle, parent_key)

    for folder_name, urls in sorted(result["folders"].items()):
        depth = folder_name.count("/")
        folder_display = folder_name.split("/")[-1]
        tree_parts.append(f'<div class="folder">{"  " * depth}📁 {folder_display}/</div>')
        for u in urls[:20]:
            m = re.search(r"/file/([^#]+)", u)
            fid = m.group(1)[:8] if m else "???"
            tree_parts.append(f'<div class="file">{"  " * (depth+1)}🎬 {fid}...</div>')
        if len(urls) > 20:
            tree_parts.append(f'<div class="file" style="color:#8b949e">{"  " * (depth+1)}... and {len(urls)-20} more</div>')

    result["totalFolders"] = len(result["folders"])
    result["treeHtml"] = "\n".join(tree_parts)
    return result


# ==== TESTS ====
print("=" * 60)
print("TEST 1: Single file URL")
print("=" * 60)
r = scan_folder_recursive("https://mega.nz/file/abc123#def456key")
print(f"  files={r['totalFiles']}, folders={r['totalFolders']}")
for f, u in r["folders"].items():
    print(f"    {f}: {len(u)} URLs")

print()
print("=" * 60)
print("TEST 2: Folder URL (m1gVlbbA)")
print("=" * 60)
r = scan_folder_recursive("https://mega.nz/folder/m1gVlbbA#O6osjeOOdpzKjtBpCOW4qA")
if "error" in r:
    print(f"  ERROR: {r['error']}")
else:
    print(f"  files={r['totalFiles']}, folders={r['totalFolders']}")
    for f, u in sorted(r["folders"].items())[:15]:
        print(f"    {f}: {len(u)} URLs")
        if u:
            print(f"      first: {u[0][:80]}")

print()
print("=" * 60)
print("TEST 3: Invalid URL")
print("=" * 60)
r = scan_folder_recursive("not-a-mega-url")
print(f"  error: {r.get('error', 'none')}")
