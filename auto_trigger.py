import json, os, re, subprocess, sys, time

MAX_NO_PROGRESS_RUNS = 3
WORKFLOW_FILE = '.github/workflows/main.yml'
DONE_MARKER = '.transfer-done'


def mega_links_configured():
    """Check if MEGA_LINKS secret/env is set and non-empty."""
    links = os.environ.get('MEGA_LINKS', '').strip()
    return bool(links)


def git_remote_base():
    r = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True)
    url = (r.stdout or '').strip()
    if not url:
        url = 'https://github.com/shivamjislt97/MEGA-TO-GDRIVE-GITHUB-CRON-MEGA-LINKS.git'
    return re.sub(r'^https?://[^@]*@', 'https://', url)


def push_with_pat():
    pat = os.environ.get('GH_TOKEN') or os.environ.get('GH_PAT')
    if not pat:
        print('  No GH_TOKEN/GH_PAT env - auto-stop commit not pushed', flush=True)
        return False
    base = git_remote_base()
    authed = re.sub(r'^https://', 'https://x-access-token:' + pat + '@', base)
    subprocess.run(['git', 'remote', 'set-url', 'origin', authed], check=False, capture_output=True)
    subprocess.run(['git', 'pull', '--rebase', 'origin', 'main'], check=False, capture_output=True)
    r = subprocess.run(['git', 'push', 'origin', 'HEAD:main'], capture_output=True, text=True)
    if r.returncode != 0:
        print('  git push failed: ' + (r.stderr or '').strip(), flush=True)
        return False
    return True


def cron_removed(content):
    return not re.search(r"^\s*schedule:\s*$", content, re.M)


def disable_cron():
    """Write done-marker, strip the cron schedule and push so the workflow
    stops triggering by itself once the transfer is complete."""
    if not os.path.exists(DONE_MARKER):
        try:
            with open(DONE_MARKER, 'w') as f:
                f.write('Transfer complete. Scheduled auto-trigger disabled.\n')
            print('  Wrote done marker', flush=True)
        except Exception as e:
            print('  Cannot write marker: %s' % e, flush=True)

    if not os.path.exists(WORKFLOW_FILE):
        print('  Workflow file missing - cannot disable cron', flush=True)
        return

    with open(WORKFLOW_FILE) as f:
        content = f.read()

    if cron_removed(content):
        print('  Cron schedule already removed', flush=True)
    else:
        new_content, n = re.subn(
            r"\n\s*schedule:\n\s*- cron: '[^']*'\n?", '\n', content, count=1)
        if n == 0:
            print('  Could not remove schedule from workflow', flush=True)
            return
        with open(WORKFLOW_FILE, 'w') as f:
            f.write(new_content)
        print('  Removed cron schedule from workflow', flush=True)

    subprocess.run(['git', 'add', WORKFLOW_FILE, DONE_MARKER], check=False, capture_output=True)
    r = subprocess.run(
        ['git', 'commit', '-m', 'auto stop: transfer complete, cron disabled [skip ci]'],
        capture_output=True, text=True)
    if r.returncode != 0:
        print('  Commit note: ' + (r.stderr or r.stdout or '').strip(), flush=True)

    if push_with_pat():
        print('  AUTO-STOP COMPLETE: cron disabled and pushed to main', flush=True)
    else:
        print('  Stopped locally, push failed - retry on next run', flush=True)


# ---------- load state ----------
try:
    d = json.load(open('completed_links.json'))
except Exception:
    d = {'folders': {}}

folders = d.get('folders', {})
oversized_raw = d.get('oversized', [])
done_marker = os.path.exists(DONE_MARKER)

# ---------- Early exit: MEGA_LINKS not configured ----------
if not mega_links_configured():
    print('  MEGA_LINKS secret not set or empty - disabling cron', flush=True)
    disable_cron()
    sys.exit(0)

# ---------- progress tracking (persisted inside completed_links.json so it
# survives across runs - the old .auto_trigger_progress file was never
# committed, so the stall-detection counter reset every run) ----------
def progress_metric():
    metric = sum(1 for item in d.get('completed', []) if item.get('status') == 'uploaded')
    try:
        ch = json.load(open('chunks_history.json'))
        for v in ch.get('videos', []):
            for c in v.get('chunks', []):
                if c.get('status') == 'done':
                    metric += 1
            if v.get('gdrive_status') == 'uploaded':
                metric += 100
    except Exception:
        pass
    return metric

prev = d.get('_auto_progress', {}) if isinstance(d.get('_auto_progress'), dict) else {}
prev_uploaded = prev.get('uploaded_count', 0)
no_progress_count = prev.get('no_progress_count', 0)

metric = progress_metric()
if metric > prev_uploaded:
    no_progress_count = 0
else:
    no_progress_count += 1

d['_auto_progress'] = {'uploaded_count': metric, 'no_progress_count': no_progress_count}
try:
    with open('completed_links.json', 'w') as f:
        json.dump(d, f, indent=2)
except Exception:
    pass

uploaded_count = sum(1 for item in d.get('completed', []) if item.get('status') == 'uploaded')

# ---------- determine pending work ----------
all_done = False
remaining = 0

if done_marker:
    all_done = True
    print('  DONE MARKER present - transfer already completed', flush=True)
else:
    all_done = True
    for name, fdata in folders.items():
        total = fdata.get('total', 0)
        done = fdata.get('done', 0)
        status = fdata.get('status', 'pending')
        if status != 'completed' or done < total:
            all_done = False
            remaining += max(total - done, 0)
            print('  [%s] %d/%d (%s)' % (name, done, total, status), file=sys.stderr, flush=True)

    if isinstance(oversized_raw, dict):
        ov = oversized_raw
        total = ov.get('total', 0)
        done = ov.get('done', 0)
        status = ov.get('status', 'completed')
        if status != 'completed' and total > 0 and done < total:
            all_done = False
            remaining += total - done
            print('  [OVERSIZED] %d/%d uploaded | status: %s' % (done, total, status), file=sys.stderr, flush=True)
    elif isinstance(oversized_raw, list) and oversized_raw:
        all_done = False
        remaining += len(oversized_raw)
        print('  [HINT] Next run will migrate oversized to structured format', file=sys.stderr, flush=True)

    try:
        ch = json.load(open('chunks_history.json'))
        for v in ch.get('videos', []):
            if v.get('status') not in ('gdrive_uploaded', 'done'):
                all_done = False
                remaining += 1
    except Exception:
        pass

# ---------- stop if stalled (no new uploads for N runs) ----------
max_runs = MAX_NO_PROGRESS_RUNS if uploaded_count > 0 else 5
if not done_marker and remaining > 0 and no_progress_count >= max_runs:
    print('STOPPED: No progress for %d runs (%d uploaded total). Check rclone/RCLONE_CONF/GDrive connectivity.' % (
        no_progress_count, uploaded_count), flush=True)
    disable_cron()
    sys.exit(0)

# ---------- decision ----------
if done_marker:
    disable_cron()
elif remaining > 0:
    max_attempts = 3
    for attempt in range(max_attempts):
        r = subprocess.run(
            ['gh', 'workflow', 'run', 'MEGA to Google Drive Transfer', '--ref', 'main'],
            capture_output=True, text=True)
        if r.returncode == 0:
            print('Triggered next cycle (%d remaining, no_progress=%d)' % (remaining, no_progress_count), flush=True)
            break
        print('Attempt %d/%d failed: %s' % (attempt + 1, max_attempts, r.stderr.strip()), flush=True)
        if attempt < max_attempts - 1:
            time.sleep(10)
    else:
        print('All trigger attempts failed', flush=True)
        sys.exit(1)
elif folders and all_done:
    print('All transfers complete - stopping', flush=True)
    disable_cron()
elif not folders:
    print('No folders configured yet - schedule kept for retry', flush=True)
else:
    print('No pending work - stopping', flush=True)
    disable_cron()