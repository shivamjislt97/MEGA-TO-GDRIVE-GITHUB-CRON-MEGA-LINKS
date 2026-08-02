import json, subprocess, sys, time

MAX_NO_PROGRESS_RUNS = 3

try:
    d = json.load(open('completed_links.json'))
except Exception:
    d = {'folders': {}}

folders = d.get('folders', {})
oversized_raw = d.get('oversized', [])
all_done = True
remaining = 0

# Check normal folders
for name, fdata in folders.items():
    total = fdata.get('total', 0)
    done = fdata.get('done', 0)
    status = fdata.get('status', 'pending')
    if status != 'completed' or done < total:
        all_done = False
        remaining += total - done
        print(f'  [{name}] {done}/{total} ({status})', file=sys.stderr, flush=True)

# Check oversized (structured format)
if isinstance(oversized_raw, dict):
    ov = oversized_raw
    total = ov.get('total', 0)
    done = ov.get('done', 0)
    status = ov.get('status', 'completed')
    if status != 'completed' and total > 0 and done < total:
        all_done = False
        remaining += total - done
        print(f'  [OVERSIZED] {done}/{total} uploaded | status: {status}', file=sys.stderr, flush=True)
elif isinstance(oversized_raw, list):
    for ov in oversized_raw:
        all_done = False
        remaining += 1
    if oversized_raw:
        print(f'  [HINT] Next run will migrate oversized to structured format', file=sys.stderr, flush=True)

# Also check chunks_history for in-progress oversized
try:
    ch = json.load(open('chunks_history.json'))
    for v in ch.get('videos', []):
        if v.get('status') not in ('gdrive_uploaded', 'done'):
            all_done = False
except Exception:
    pass

# --- Progress tracking to prevent infinite loops ---
PROGRESS_FILE = '.auto_trigger_progress'
uploaded_count = sum(1 for item in d.get('completed', []) if item.get('status') == 'uploaded')

try:
    prev = json.load(open(PROGRESS_FILE))
    prev_uploaded = prev.get('uploaded_count', 0)
    no_progress_count = prev.get('no_progress_count', 0)
except Exception:
    prev_uploaded = 0
    no_progress_count = 0

if uploaded_count > prev_uploaded:
    no_progress_count = 0
else:
    no_progress_count += 1

try:
    with open(PROGRESS_FILE, 'w') as f:
        json.dump({'uploaded_count': uploaded_count, 'no_progress_count': no_progress_count}, f)
except Exception:
    pass

# Stop re-triggering if no progress for too many runs
# If some files were uploaded before: stop after 3 runs with no new uploads
# If zero files ever uploaded: stop after 5 runs (likely config/connection issue)
max_runs = MAX_NO_PROGRESS_RUNS if uploaded_count > 0 else 5
if no_progress_count >= max_runs:
    print(f'STOPPED: No progress for {no_progress_count} runs ({uploaded_count} uploaded total). Check rclone config / RCLONE_CONF secret / Google Drive connectivity.', flush=True)
    sys.exit(0)

if remaining > 0:
    max_attempts = 3
    for attempt in range(max_attempts):
        r = subprocess.run(
            ['gh', 'workflow', 'run', 'MEGA to Google Drive Transfer', '--ref', 'main'],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            print(f'Triggered next cycle ({remaining} remaining, no_progress={no_progress_count})', flush=True)
            break
        print(f'Attempt {attempt+1}/{max_attempts} failed: {r.stderr.strip()}', flush=True)
        if attempt < max_attempts - 1:
            time.sleep(10)
    else:
        print('All trigger attempts failed', flush=True)
        sys.exit(1)
elif folders and all_done:
    print('All folders completed - no more cycles', flush=True)
else:
    print('No pending work - no more cycles', flush=True)
