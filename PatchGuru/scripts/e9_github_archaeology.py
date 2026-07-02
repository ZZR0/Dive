#!/usr/bin/env python3
"""E9 case-study GitHub archaeology for DIVE-only GT-TP candidates.

Fetches PR metadata, inline review comments, post-merge follow-up commits/PRs,
and git-blame for DIVE cluster hit lines mapped back to upstream source.

Usage (from PatchGuru/):
  python3 scripts/e9_github_archaeology.py
  python3 scripts/e9_github_archaeology.py --pr-list .cache/.../candidates.txt
  python3 scripts/e9_github_archaeology.py --selection-json .cache/.../e9_case_study_selection.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GT = ROOT / ".cache/manual_annotation/dive_new200/ground_truth.txt"
DEFAULT_DIVE = ROOT / ".cache_dive_new200"
DEFAULT_OUT = ROOT / ".cache/manual_annotation/dive_new200/e9_github_archaeology.json"
DEFAULT_SELECTION = ROOT / ".cache/manual_annotation/dive_new200/e9_case_study_selection.json"

REPOS: dict[str, str] = {
    "pandas": "pandas-dev/pandas",
    "scipy": "scipy/scipy",
    "keras": "keras-team/keras",
    "marshmallow": "marshmallow-code/marshmallow",
    "scikit-learn": "scikit-learn/scikit-learn",
    "numpy": "numpy/numpy",
    "transformers": "huggingface/transformers",
    "pytorch_geometric": "pyg-team/pytorch_geometric",
    "scapy": "secdev/scapy",
}

DEFAULT_CANDIDATES: list[tuple[str, int]] = [
    ("keras", 21432),
    ("marshmallow", 750),
    ("pandas", 64183),
    ("scipy", 23579),
    ("scipy", 25235),
    ("keras", 21850),
    ("marshmallow", 744),
    ("pandas", 64689),
    ("scipy", 24181),
    ("keras", 22116),
    ("marshmallow", 2797),
    ("pandas", 65156),
]

CLOSING_ISSUE_RE = re.compile(
    r"(?:close[sd]?|fixe[sd]?|resolve[sd]?)\s+#(\d+)", re.I
)
POST_HEADER_RE = re.compile(r"###\s+([\w\.]+)#(\d+)-(\d+)")
POST_DEF_RE = re.compile(r"^def (post_\w+)\(", re.M)
REGRESS_KW = ("regress", "break", "broken", "unexpected", "side effect", "follow-up")


def load_token(token_path: Path) -> str:
    for p in (token_path, ROOT / ".github_token", ROOT / ".github_tokens"):
        if p.exists():
            text = p.read_text().strip()
            if text:
                return text.splitlines()[0].strip()
    raise FileNotFoundError("No GitHub token found (.github_token)")


class GitHubClient:
    def __init__(self, token: str, delay: float = 0.15) -> None:
        self.token = token
        self.delay = delay

    def _request(self, path: str, accept: str | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        req = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:800]
            return {"error": exc.code, "body": body}
        time.sleep(self.delay)
        return data

    def paginate(self, path: str, max_pages: int = 3) -> list[Any] | dict[str, Any]:
        items: list[Any] = []
        for page in range(1, max_pages + 1):
            sep = "&" if "?" in path else "?"
            data = self._request(f"{path}{sep}page={page}&per_page=100")
            if isinstance(data, dict) and "error" in data:
                return data
            if not isinstance(data, list):
                return data
            items.extend(data)
            if len(data) < 100:
                break
        return items


def load_ground_truth(path: Path) -> dict[tuple[str, int], dict[str, str]]:
    out: dict[tuple[str, int], dict[str, str]] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(\S+)\s+(\d+)\s+(\S+)\s+(TP|FP)\s*(?:#\s*(.*))?", line)
        if not m:
            continue
        out[(m.group(1), int(m.group(2)))] = {
            "phase": m.group(3),
            "label": m.group(4),
            "note": (m.group(5) or "").strip(),
        }
    return out


def is_dive_bug(cache: Path, project: str, pr: int) -> tuple[bool, str | None]:
    root = cache / "oracles" / project / str(pr)
    p1_path = root / "results.json"
    if not p1_path.exists():
        return False, None
    p1 = json.loads(p1_path.read_text())
    if p1.get("review_conclusion") == "BUG":
        return True, "p1"
    p2_path = root / "phase2" / "results.json"
    if p2_path.exists():
        p2 = json.loads(p2_path.read_text())
        if p2.get("review_conclusion") == "BUG":
            return True, "p2"
    return False, None


def rank_dive_only_candidates(
    gt: dict[tuple[str, int], dict[str, str]],
    dive_cache: Path,
    baseline_cache: Path,
    limit: int,
) -> list[tuple[str, int]]:
    scored: list[tuple[int, str, int]] = []
    for (project, pr), meta in gt.items():
        if meta["label"] != "TP":
            continue
        dive_ok, phase = is_dive_bug(dive_cache, project, pr)
        base_ok, _ = is_dive_bug(baseline_cache, project, pr)
        if not dive_ok or base_ok:
            continue
        p2_path = dive_cache / "oracles" / project / str(pr) / "phase2" / "results.json"
        clusters = 0
        if p2_path.exists():
            clusters = len(json.loads(p2_path.read_text()).get("dive_clusters") or [])
        score = 0
        if phase == "p2":
            score += 10
        score += clusters
        if "RQ4" not in meta.get("note", ""):
            score += 2
        scored.append((score, project, pr))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [(p, pr) for _, p, pr in scored[:limit]]


def load_pr_list(path: Path) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        out.append((parts[0], int(parts[1])))
    return out


def load_selection_prs(path: Path) -> list[tuple[str, int]]:
    data = json.loads(path.read_text())
    selected = data.get("selected_case_studies") or []
    return [(c["project"], int(c["pr"])) for c in selected]


def resolve_repo_file(
    module: str, project: str, changed_py: list[str] | None = None
) -> str:
    """Map embedded module path to repo-relative source file."""
    parts = module.split(".")
    if changed_py:
        for n in range(len(parts), 0, -1):
            dotted = ".".join(parts[:n])
            slash = "/".join(parts[:n])
            for fn in changed_py:
                stem = fn[:-3] if fn.endswith(".py") else fn
                if stem == slash or stem.replace("/", ".") == dotted:
                    return fn
                if dotted in stem.replace("/", "."):
                    return fn
    if len(parts) >= 2 and parts[-1][0].islower():
        base = "/".join(parts[:-1]) + ".py"
    else:
        base = "/".join(parts) + ".py"
    return base


def parse_post_function_meta(spec_text: str) -> dict[str, Any] | None:
    idx = spec_text.rfind("## After Pull Request")
    section = spec_text[idx:] if idx >= 0 else spec_text
    header = POST_HEADER_RE.search(section)
    defn = POST_DEF_RE.search(section)
    if not header or not defn:
        return None
    module, start, end = header.group(1), int(header.group(2)), int(header.group(3))
    fut_name = defn.group(1).removeprefix("post_")
    post_fn_name = defn.group(1)
    # Line number of `def post_...` in full spec file
    def_pos = spec_text.find(defn.group(0))
    post_spec_line = spec_text[:def_pos].count("\n") + 1
    return {
        "module": module,
        "repo_line_start": start,
        "repo_line_end": end,
        "fut_name": fut_name,
        "post_fn_name": post_fn_name,
        "post_spec_line": post_spec_line,
    }


def map_spec_hits_to_repo(
    meta: dict[str, Any], hit_lines: list[int]
) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    base = meta["post_spec_line"]
    repo_start = meta["repo_line_start"]
    for spec_ln in sorted(set(int(x) for x in hit_lines)):
        offset = spec_ln - base
        repo_ln = repo_start + offset
        mapped.append(
            {
                "spec_line": spec_ln,
                "repo_line": repo_ln,
                "in_header_range": meta["repo_line_start"]
                <= repo_ln
                <= meta["repo_line_end"],
            }
        )
    return mapped


BLAME_RE = re.compile(
    r"^([0-9a-f]{7,40})\s+(?:\S+\s+)?\((.+?)\s+(\d{4}-\d{2}-\d{2})"
)


def normalize_blame_entries(raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in raw_entries:
        if "commit" in entry:
            out.append(entry)
            continue
        raw = entry.get("raw", "")
        m = BLAME_RE.match(raw)
        if m:
            out.append(
                {
                    "repo_line": entry.get("repo_line"),
                    "commit": m.group(1),
                    "author": m.group(2).strip(),
                    "date": m.group(3),
                    "raw": raw[:400],
                }
            )
        else:
            out.append(entry)
    return out


def find_clone_dir(project: str) -> Path | None:
    pool = Path(os.environ.get("PATCHGURU_CLONES_DIR", str(ROOT.parent / "clones")))
    for clone_id in ("clone1", "clone2", "clone3", "golden", "collect"):
        candidate = pool / clone_id / project
        if (candidate / ".git").exists():
            return candidate
    return None


def git_blame_lines(
    clone_dir: Path,
    commit: str,
    file_path: str,
    repo_lines: list[int],
) -> list[dict[str, Any]] | dict[str, str]:
    if not repo_lines:
        return []
    out: list[dict[str, Any]] = []
    for ln in sorted(set(repo_lines)):
        cmd = [
            "git",
            "-C",
            str(clone_dir),
            "blame",
            "-L",
            f"{ln},{ln}",
            commit,
            "--",
            file_path,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        except (subprocess.SubprocessError, OSError) as exc:
            return {"error": str(exc)}
        if proc.returncode != 0:
            out.append(
                {
                    "repo_line": ln,
                    "error": (proc.stderr or proc.stdout or "git blame failed").strip()[:300],
                }
            )
            continue
        line = proc.stdout.strip()
        m = BLAME_RE.match(line)
        if m:
            out.append(
                {
                    "repo_line": ln,
                    "commit": m.group(1),
                    "author": m.group(2).strip(),
                    "date": m.group(3),
                    "raw": line[:400],
                }
            )
        else:
            out.append({"repo_line": ln, "raw": line[:400]})
    return normalize_blame_entries(out)


def load_dive_clusters(dive_cache: Path, project: str, pr: int) -> list[dict[str, Any]]:
    p2 = dive_cache / "oracles" / project / str(pr) / "phase2" / "results.json"
    if not p2.exists():
        return []
    return json.loads(p2.read_text()).get("dive_clusters") or []


def parse_pr_patch_lines(patch: str | None) -> set[int]:
    """Return new-side line numbers touched in a unified diff patch."""
    if not patch:
        return set()
    lines: set[int] = set()
    new_line = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            if m:
                new_line = int(m.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.add(new_line)
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            if not raw.startswith("\\"):
                new_line += 1
    return lines


def fetch_follow_up(
    gh: GitHubClient,
    repo: str,
    merge_commit: str | None,
    merged_at: str | None,
    file_path: str,
    pr_number: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "file_path": file_path,
        "merge_commit": merge_commit,
        "file_commits_after_merge": [],
        "follow_up_prs": [],
        "regression_search": [],
    }
    if not merge_commit or not merged_at or not file_path:
        out["skipped"] = "missing merge_commit/merged_at/file_path"
        return out

    since = merged_at
    if not since.endswith("Z") and "+" not in since:
        since += "Z"
    enc_path = urllib.parse.quote(file_path, safe="/")
    commits = gh.paginate(
        f"/repos/{repo}/commits?sha=main&path={enc_path}&since={since}&per_page=30",
        max_pages=2,
    )
    if isinstance(commits, dict) and "error" in commits:
        out["commits_error"] = commits
        return out

    seen_prs: set[int] = set()
    for c in commits:
        sha = c.get("sha")
        if not sha or sha == merge_commit:
            continue
        entry = {
            "sha": sha[:12],
            "date": c.get("commit", {}).get("author", {}).get("date"),
            "message": (c.get("commit", {}).get("message") or "")[:240],
            "author": (c.get("commit", {}).get("author") or {}).get("name"),
            "html_url": c.get("html_url"),
        }
        pulls = gh._request(f"/repos/{repo}/commits/{sha}/pulls")
        if isinstance(pulls, list):
            pr_items = []
            for p in pulls:
                num = p.get("number")
                if num is None or num == pr_number:
                    continue
                pr_items.append(
                    {
                        "number": num,
                        "title": p.get("title"),
                        "merged_at": p.get("merged_at"),
                        "html_url": p.get("html_url"),
                    }
                )
                if num not in seen_prs and p.get("merged_at"):
                    seen_prs.add(num)
                    out["follow_up_prs"].append(pr_items[-1])
            entry["associated_prs"] = pr_items
        out["file_commits_after_merge"].append(entry)
        if len(out["file_commits_after_merge"]) >= 15:
            break

    # Search merged PRs touching the same file after merge date
    merged_date = merged_at[:10]
    q = urllib.parse.quote(
        f"repo:{repo} is:pr is:merged merged:>{merged_date} path:{file_path}",
        safe="",
    )
    search = gh._request(f"/search/issues?q={q}&per_page=10")
    if isinstance(search, dict) and search.get("items"):
        for item in search["items"]:
            num = item.get("number")
            if num == pr_number:
                continue
            out["regression_search"].append(
                {
                    "number": num,
                    "title": item.get("title"),
                    "state": item.get("state"),
                    "html_url": item.get("html_url"),
                }
            )
    return out


def analyze_pr(
    gh: GitHubClient,
    project: str,
    pr_num: int,
    dive_cache: Path,
    *,
    skip_git: bool = False,
) -> dict[str, Any]:
    repo = REPOS.get(project)
    if not repo:
        return {"project": project, "pr": pr_num, "error": f"unknown project {project}"}

    out: dict[str, Any] = {"project": project, "pr": pr_num, "repo": repo}
    pr = gh._request(f"/repos/{repo}/pulls/{pr_num}")
    if isinstance(pr, dict) and "error" in pr:
        out["fetch_error"] = pr
        return out

    body = pr.get("body") or ""
    out.update(
        {
            "title": pr.get("title", ""),
            "state": pr.get("state"),
            "merged": pr.get("merged_at") is not None,
            "merged_at": pr.get("merged_at"),
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "html_url": pr.get("html_url"),
            "body_snip": body[:800].replace("\r", ""),
            "body_closing_issue_refs": sorted(set(CLOSING_ISSUE_RE.findall(body)), key=int),
        }
    )

    # Issue comments
    comments = gh.paginate(f"/repos/{repo}/issues/{pr_num}/comments")
    comment_texts: list[str] = []
    if isinstance(comments, list):
        out["n_comments"] = len(comments)
        for c in comments:
            comment_texts.append(
                {
                    "user": (c.get("user") or {}).get("login"),
                    "created_at": c.get("created_at"),
                    "body": (c.get("body") or "")[:2000],
                }
            )
        joined = "\n".join(x["body"] for x in comment_texts).lower()
        out["comments_mention_regress"] = any(k in joined for k in REGRESS_KW)
    else:
        out["comments_error"] = comments
    out["issue_comments"] = comment_texts[:20]

    # Inline review comments (full text)
    review_comments = gh.paginate(f"/repos/{repo}/pulls/{pr_num}/comments", max_pages=5)
    inline: list[dict[str, Any]] = []
    if isinstance(review_comments, list):
        out["n_review_comments"] = len(review_comments)
        for rc in review_comments:
            inline.append(
                {
                    "user": (rc.get("user") or {}).get("login"),
                    "created_at": rc.get("created_at"),
                    "path": rc.get("path"),
                    "line": rc.get("line") or rc.get("original_line"),
                    "side": rc.get("side"),
                    "body": (rc.get("body") or "")[:2000],
                }
            )
        joined = "\n".join(x["body"] for x in inline).lower()
        out["inline_mention_regress"] = any(k in joined for k in REGRESS_KW)
    else:
        out["review_comments_error"] = review_comments
    out["inline_review_comments"] = inline[:40]

    # Changed files + patches
    files = gh.paginate(f"/repos/{repo}/pulls/{pr_num}/files", max_pages=5)
    test_files: list[str] = []
    changed_py: list[str] = []
    patch_lines_by_file: dict[str, set[int]] = {}
    if isinstance(files, list):
        out["n_files"] = len(files)
        for f in files:
            fn = f.get("filename") or ""
            if re.search(r"test", fn, re.I):
                test_files.append(fn)
            if fn.endswith(".py") and "test" not in fn.lower():
                changed_py.append(fn)
            patch_lines_by_file[fn] = parse_pr_patch_lines(f.get("patch"))
        out["test_files"] = test_files[:15]
        out["has_test_changes"] = bool(test_files)
        out["changed_py_files"] = changed_py[:20]
    else:
        out["files_error"] = files

    # Timeline linked issues
    timeline = gh.paginate(f"/repos/{repo}/issues/{pr_num}/timeline", max_pages=2)
    linked: list[int] = []
    if isinstance(timeline, list):
        for ev in timeline:
            src = ev.get("source") or {}
            if isinstance(src, dict) and src.get("issue"):
                num = src["issue"].get("number")
                if num:
                    linked.append(num)
    out["timeline_linked_issues"] = sorted(set(linked))

    search2 = gh._request(f"/search/issues?q=repo:{repo}+is:issue+{pr_num}&per_page=8")
    if isinstance(search2, dict) and search2.get("items"):
        out["issues_mentioning_pr"] = [
            {"number": i["number"], "title": (i.get("title") or "")[:120], "state": i.get("state")}
            for i in search2["items"]
        ]

    # DIVE cluster -> repo line mapping + git blame
    spec_path = dive_cache / "oracles" / project / str(pr_num) / "phase2" / "specification.py"
    clusters = load_dive_clusters(dive_cache, project, pr_num)
    out["dive_cluster_count"] = len(clusters)
    dive_hits: list[dict[str, Any]] = []
    primary_file: str | None = None

    if spec_path.exists():
        spec_text = spec_path.read_text()
        meta = parse_post_function_meta(spec_text)
        if meta:
            out["dive_target"] = meta
            repo_file = resolve_repo_file(meta["module"], project, changed_py)
            out["dive_target"]["repo_file"] = repo_file
            primary_file = repo_file
            pr_changed = patch_lines_by_file.get(repo_file, set())
            pr_changed_in_fn = sorted(
                ln
                for ln in pr_changed
                if meta["repo_line_start"] <= ln <= meta["repo_line_end"]
            )
            if not pr_changed_in_fn and pr_changed:
                # GitHub file patches are often truncated; keep out-of-range signal.
                out["dive_target"]["pr_patch_lines_outside_fn"] = sorted(pr_changed)[:40]
            if not pr_changed_in_fn:
                # Fallback anchors: function header span at merge commit.
                span = meta["repo_line_end"] - meta["repo_line_start"]
                mid = meta["repo_line_start"] + span // 2
                pr_changed_in_fn = sorted(
                    {
                        meta["repo_line_start"],
                        mid,
                        meta["repo_line_end"],
                    }
                )
                out["dive_target"]["blame_anchor_fallback"] = True
            out["dive_target"]["pr_patch_lines_in_fn"] = pr_changed_in_fn

            clone = None if skip_git else find_clone_dir(project)
            merge_sha = out.get("merge_commit_sha")
            if clone and merge_sha and pr_changed_in_fn:
                out["dive_target"]["git_blame_pr_patch_in_fn"] = git_blame_lines(
                    clone, merge_sha, repo_file, pr_changed_in_fn
                )

            for idx, cl in enumerate(clusters):
                hits = cl.get("hit_changed_lines") or []
                mapped = map_spec_hits_to_repo(meta, hits)
                for m in mapped:
                    m["touched_in_pr_patch"] = m["repo_line"] in pr_changed
                repo_lines = [
                    m["repo_line"]
                    for m in mapped
                    if m.get("in_header_range") or m.get("touched_in_pr_patch")
                ]
                if not repo_lines:
                    repo_lines = [m["repo_line"] for m in mapped]
                blame: list[dict[str, Any]] | dict[str, str] = []
                if not skip_git and repo_lines:
                    if clone and merge_sha:
                        blame = git_blame_lines(clone, merge_sha, repo_file, repo_lines)
                    else:
                        blame = {
                            "skipped": "clone or merge_commit unavailable",
                            "clone": str(clone) if clone else None,
                        }
                dive_hits.append(
                    {
                        "cluster_index": idx,
                        "category": cl.get("category"),
                        "args_expr_snip": (cl.get("args_expr") or "")[:200],
                        "pre": cl.get("pre"),
                        "post": cl.get("post"),
                        "hit_changed_lines_spec": hits,
                        "repo_line_mapping": mapped,
                        "git_blame_at_merge": blame,
                    }
                )
        else:
            out["dive_target_error"] = "could not parse post_ function header from phase2 spec"
    else:
        out["dive_spec_missing"] = str(spec_path)

    out["dive_cluster_hits"] = dive_hits

    # Follow-up commits / PRs on primary changed file
    if primary_file:
        out["follow_up"] = fetch_follow_up(
            gh,
            repo,
            out.get("merge_commit_sha"),
            out.get("merged_at"),
            primary_file,
            pr_num,
        )
    else:
        out["follow_up"] = {"skipped": "no primary repo file resolved"}

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="E9 GitHub archaeology for DIVE case studies")
    parser.add_argument("--token-file", type=Path, default=ROOT / ".github_token")
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_GT)
    parser.add_argument("--dive-cache", type=Path, default=DEFAULT_DIVE)
    parser.add_argument("--baseline-cache", type=Path, default=ROOT / ".cache_baseline_new200")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--selection-json", type=Path, help="Only analyze selected case studies")
    parser.add_argument("--pr-list", type=Path, help="Lines: project pr")
    parser.add_argument("--dive-only-top", type=int, default=12, help="When auto-selecting candidates")
    parser.add_argument("--delay", type=float, default=0.15, help="Seconds between GitHub API calls")
    parser.add_argument("--skip-git", action="store_true", help="Skip local git blame")
    args = parser.parse_args()

    if args.selection_json:
        candidates = load_selection_prs(args.selection_json)
    elif args.pr_list:
        candidates = load_pr_list(args.pr_list)
    else:
        gt = load_ground_truth(args.ground_truth)
        candidates = rank_dive_only_candidates(
            gt, args.dive_cache, args.baseline_cache, args.dive_only_top
        )
        if not candidates:
            candidates = DEFAULT_CANDIDATES

    token = load_token(args.token_file)
    gh = GitHubClient(token, delay=args.delay)

    results: list[dict[str, Any]] = []
    for i, (project, pr) in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {project}#{pr} ...", flush=True)
        results.append(
            analyze_pr(
                gh,
                project,
                pr,
                args.dive_cache,
                skip_git=args.skip_git,
            )
        )

    payload = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": (
            "GitHub REST (PR/comments/files/timeline/search) + "
            "post-merge file commits + inline review full text + "
            "DIVE hit line -> repo line mapping + git blame at merge commit"
        ),
        "candidate_count": len(candidates),
        "candidates": [{"project": p, "pr": n} for p, n in candidates],
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {args.output} ({len(results)} PRs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
