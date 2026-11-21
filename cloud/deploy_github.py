"""
Atomic GitHub REST API Deployer (requires no local git.exe).
Reads token strictly in-memory via `gh auth token` and pushes the repository tree
to `gamehotspot62/mt5-leading-confluence-cloud` on branch `main`.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
REPO_OWNER = "gamehotspot62"
REPO_NAME = "mt5-leading-confluence-cloud"

ALLOWED_PATHS = [
    ".github/workflows/live_data_and_signals.yml",
    ".github/workflows/ml_retrain_optimize.yml",
    "cloud/requirements.txt",
    "cloud/data_clients.py",
    "cloud/fetch_macro_data.py",
    "cloud/features.py",
    "cloud/ml_optimizer.py",
    "cloud/live_signal_scanner.py",
    "cloud/deploy_github.py",
    "LeadingConfluencePro.mq5",
    "LeadingConfluencePro.ex5",
    "LeadingConfluenceCloudBridge.mq5",
    "LeadingConfluenceCloudBridge.ex5",
    "LeadingConfluenceAutoEntry_M15.mq5",
    "LeadingConfluenceAutoEntry_M15.ex5",
    "index.html",
    "README.md",
    "RESEARCH_AND_DESIGN.md",
    "MacroFlowBias_Sample.csv",
]


def collect_repo_files() -> List[Path]:
    files: List[Path] = []
    for rel in ALLOWED_PATHS:
        p = ROOT_DIR / rel
        if p.is_file():
            files.append(p)
    data_dir = ROOT_DIR / "data"
    if data_dir.is_dir():
        for p in sorted(data_dir.iterdir()):
            if p.is_file() and not p.name.endswith(".pyc"):
                files.append(p)
    return files


def main() -> int:
    token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    if not token:
        print("ERROR: Failed to obtain token from gh CLI.", file=sys.stderr)
        return 1

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )

    api_base = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"

    # 1) Get current main branch HEAD commit
    ref_resp = session.get(f"{api_base}/git/ref/heads/main", timeout=20)
    if ref_resp.status_code != 200:
        print(f"ERROR fetching main ref: {ref_resp.status_code} {ref_resp.text}", file=sys.stderr)
        return 1
    parent_sha = ref_resp.json()["object"]["sha"]

    # 2) Upload all files as base64 blobs
    files = collect_repo_files()
    tree_items: List[Dict[str, str]] = []
    print(f"Uploading {len(files)} files to {REPO_OWNER}/{REPO_NAME}...")

    for fpath in files:
        rel_posix = fpath.relative_to(ROOT_DIR).as_posix()
        raw_bytes = fpath.read_bytes()
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        blob_resp = session.post(
            f"{api_base}/git/blobs",
            json={"content": b64, "encoding": "base64"},
            timeout=30,
        )
        if blob_resp.status_code not in (200, 201):
            print(f"ERROR creating blob for {rel_posix}: {blob_resp.status_code} {blob_resp.text}", file=sys.stderr)
            return 1
        blob_sha = blob_resp.json()["sha"]
        tree_items.append(
            {
                "path": rel_posix,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            }
        )

    # 3) Create Git Tree
    tree_resp = session.post(
        f"{api_base}/git/trees",
        json={"tree": tree_items},
        timeout=30,
    )
    if tree_resp.status_code not in (200, 201):
        print(f"ERROR creating tree: {tree_resp.status_code} {tree_resp.text}", file=sys.stderr)
        return 1
    tree_sha = tree_resp.json()["sha"]

    # 4) Create Commit
    commit_msg = (
        "feat: deploy LeadingConfluencePro v2.00 + 24/7 Cloud Macro/On-Chain/ETF/COT & ML Optimizer"
    )
    commit_resp = session.post(
        f"{api_base}/git/commits",
        json={
            "message": commit_msg,
            "tree": tree_sha,
            "parents": [parent_sha],
        },
        timeout=30,
    )
    if commit_resp.status_code not in (200, 201):
        print(f"ERROR creating commit: {commit_resp.status_code} {commit_resp.text}", file=sys.stderr)
        return 1
    new_commit_sha = commit_resp.json()["sha"]

    # 5) Update main ref
    upd_resp = session.patch(
        f"{api_base}/git/refs/heads/main",
        json={"sha": new_commit_sha, "force": True},
        timeout=20,
    )
    if upd_resp.status_code != 200:
        print(f"ERROR updating ref: {upd_resp.status_code} {upd_resp.text}", file=sys.stderr)
        return 1

    print(f"SUCCESS: Pushed commit {new_commit_sha[:10]} to {REPO_OWNER}/{REPO_NAME}@main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
