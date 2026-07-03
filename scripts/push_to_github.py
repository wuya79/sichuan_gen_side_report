#!/usr/bin/env python3
"""Push local repo to GitHub via REST API (branches: master or main)."""
import json, subprocess, urllib.request

TOKEN = open("/home/ubuntu/.hermes/keys/GITHUB_TOKEN").read().strip()
CWD = None  # 在终端中设 CWD=仓库目录
REPO = None  # 在终端中设 REPO="owner/repo"
BRANCH = None  # 在终端中设 BRANCH="main" 或 "master"

def api(method, path, data=None):
    url = f"https://api.github.com/repos/{REPO}/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method,
        headers={"Authorization": f"token {TOKEN}", "Content-Type": "application/json",
                 "User-Agent": "hermes-push/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  HTTP {e.code}: {err[:300]}")
        raise

def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, shell=True, cwd=CWD).stdout.strip()

# Step 1
print(f"=== Step 1: 远程ref (branch={BRANCH}) ===")
BASE_SHA = api("GET", f"git/refs/heads/{BRANCH}")["object"]["sha"]
BASE_TREE = api("GET", f"git/commits/{BASE_SHA}")["tree"]["sha"]
print(f"  HEAD={BASE_SHA[:8]} TREE={BASE_TREE[:8]}")

# Step 2
print("\n=== Step 2: 远程文件 ===")
remote_blobs = {}
for item in api("GET", f"git/trees/{BASE_TREE}?recursive=1")["tree"]:
    if item["type"] == "blob":
        remote_blobs[item["path"]] = item["sha"]
print(f"  {len(remote_blobs)} files")

# Step 3
print("\n=== Step 3: 本地文件 ===")
local_tree_sha = sh("git rev-parse HEAD^{tree}")
local_blobs = {}
local_modes = {}
for line in sh(f"git ls-tree -r {local_tree_sha}").split('\n'):
    parts = line.split(None, 3)
    if len(parts) >= 4 and parts[1] == "blob":
        local_blobs[parts[3]] = parts[2]
        local_modes[parts[3]] = parts[0]
print(f"  {len(local_blobs)} files, tree={local_tree_sha[:8]}")

# Step 4
print("\n=== Step 4: 差异 ===")
changed = {p: s for p, s in local_blobs.items() if p not in remote_blobs or remote_blobs[p] != s}
deleted = [p for p in remote_blobs if p not in local_blobs]
print(f"  changed/added: {len(changed)}, deleted: {len(deleted)}")
for p in list(changed.keys())[:10]:
    status = "new" if p not in remote_blobs else "modified"
    print(f"    {status}: {p}")

# Step 5
if changed:
    print(f"\n=== Step 5: Upload {len(changed)} blobs ===")
    for i, (path, sha) in enumerate(changed.items()):
        content = sh(f"git cat-file -p {sha}")
        if not content:
            print(f"  skip empty: {path}")
            continue
        try:
            api("POST", "git/blobs", {"content": content, "encoding": "utf-8"})
        except Exception:
            pass
        if (i+1) % 30 == 0:
            print(f"  {i+1}/{len(changed)} done")
    print(f"  done: {len(changed)} blobs")
else:
    print("\n=== Step 5: 无blobs需上传 ===")

# Step 6
print("\n=== Step 6: Build tree ===")
tree_entries = []
for p, s in remote_blobs.items():
    if p not in changed and p not in deleted:
        tree_entries.append({"path": p, "mode": "100644", "type": "blob", "sha": s})
for p in changed:
    tree_entries.append({"path": p, "mode": local_modes.get(p, "100644"), "type": "blob", "sha": local_blobs[p]})
print(f"  {len(tree_entries)} entries")
new_tree = api("POST", "git/trees", {"tree": tree_entries})
NEW_TREE = new_tree["sha"]
print(f"  new tree: {NEW_TREE[:8]}")

# Step 7
print("\n=== Step 7: Create commit ===")
msg = sh("git log --format=%B -1 HEAD")
author_name = sh("git log --format=%an -1 HEAD")
author_email = sh("git log --format=%ae -1 HEAD")
author_date = sh("git log --format=%aI -1 HEAD")

new_commit = api("POST", "git/commits", {
    "message": msg,
    "tree": NEW_TREE,
    "parents": [BASE_SHA],
    "author": {"name": author_name, "email": author_email, "date": author_date},
    "committer": {"name": author_name, "email": author_email, "date": author_date}
})
COMMIT_SHA = new_commit["sha"]
print(f"  new commit: {COMMIT_SHA[:8]}")

# Step 8
print(f"\n=== Step 8: Update ref ({BRANCH}) ===")
result = api("PATCH", f"git/refs/heads/{BRANCH}", {"sha": COMMIT_SHA, "force": True})
print(f"\n  ✅ 推送成功！HEAD={result['object']['sha'][:8]}")
print(f"  https://github.com/{REPO}")
