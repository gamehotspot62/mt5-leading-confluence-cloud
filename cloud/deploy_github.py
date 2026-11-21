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


def collect_repo_files(include_data: bool = True) -> List[Path]:
    files: List[Path] = []
    for rel in ALLOWED_PATHS:
        p = ROOT_DIR / rel
        if p.is_file():
            files.append(p)
    if include_data:
        data_dir = ROOT_DIR / "data"
        if data_dir.is_dir():
            for p in sorted(data_dir.iterdir()):
                if p.is_file() and not p.name.endswith(".pyc"):
                    files.append(p)
    return files


def sync_gh_pages(session: requests.Session, api_base: str) -> int:
    """Publish static dashboard (index.html + .nojekyll) to dedicated gh-pages branch."""
    index_path = ROOT_DIR / "index.html"
    if not index_path.is_file():
        print("ERROR: index.html not found for gh-pages sync.", file=sys.stderr)
        return 1

    blobs = []
    for path_name, raw_bytes in [("index.html", index_path.read_bytes()), (".nojekyll", b"")]:
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        b_resp = session.post(
            f"{api_base}/git/blobs",
            json={"content": b64, "encoding": "base64"},
            timeout=30,
        )
        if b_resp.status_code not in (200, 201):
            print(f"ERROR creating gh-pages blob {path_name}: {b_resp.status_code} {b_resp.text}", file=sys.stderr)
            return 1
        blobs.append({"path": path_name, "mode": "100644", "type": "blob", "sha": b_resp.json()["sha"]})

    t_resp = session.post(f"{api_base}/git/trees", json={"tree": blobs}, timeout=30)
    if t_resp.status_code not in (200, 201):
        print(f"ERROR creating gh-pages tree: {t_resp.status_code} {t_resp.text}", file=sys.stderr)
        return 1
    tree_sha = t_resp.json()["sha"]

    ref_resp = session.get(f"{api_base}/git/ref/heads/gh-pages", timeout=20)
    parents: List[str] = []
    ref_exists = ref_resp.status_code == 200
    if ref_exists:
        parents = [ref_resp.json()["object"]["sha"]]

    c_resp = session.post(
        f"{api_base}/git/commits",
        json={"message": "chore(pages): publish static dashboard to gh-pages", "tree": tree_sha, "parents": parents},
        timeout=30,
    )
    if c_resp.status_code not in (200, 201):
        print(f"ERROR creating gh-pages commit: {c_resp.status_code} {c_resp.text}", file=sys.stderr)
        return 1
    commit_sha = c_resp.json()["sha"]

    if ref_exists:
        u_resp = session.patch(
            f"{api_base}/git/refs/heads/gh-pages",
            json={"sha": commit_sha, "force": True},
            timeout=20,
        )
        if u_resp.status_code != 200:
            print(f"ERROR updating gh-pages ref: {u_resp.status_code} {u_resp.text}", file=sys.stderr)
            return 1
    else:
        cr_resp = session.post(
            f"{api_base}/git/refs",
            json={"ref": "refs/heads/gh-pages", "sha": commit_sha},
            timeout=20,
        )
        if cr_resp.status_code not in (200, 201):
            print(f"ERROR creating gh-pages ref: {cr_resp.status_code} {cr_resp.text}", file=sys.stderr)
            return 1

    print(f"SUCCESS: Synced gh-pages branch ({commit_sha[:10]})")
    return 0


def main() -> int:
    skip_data = "--skip-data" in sys.argv
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
    parent_tree_sha = session.get(f"{api_base}/git/commits/{parent_sha}", timeout=20).json()["tree"]["sha"]

    # 2) Upload all files as base64 blobs
    files = collect_repo_files(include_data=not skip_data)
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
    tree_payload: Dict[str, object] = {"tree": tree_items}
    if skip_data:
        tree_payload["base_tree"] = parent_tree_sha
    tree_resp = session.post(
        f"{api_base}/git/trees",
        json=tree_payload,
        timeout=30,
    )
    if tree_resp.status_code not in (200, 201):
        print(f"ERROR creating tree: {tree_resp.status_code} {tree_resp.text}", file=sys.stderr)
        return 1
    tree_sha = tree_resp.json()["sha"]

    # 4) Create Commit
    commit_msg = (
        "feat: decouple static dashboard onto gh-pages & fetch live raw data from main"
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
    return sync_gh_pages(session, api_base)


if __name__ == "__main__":
    raise SystemExit(main())
