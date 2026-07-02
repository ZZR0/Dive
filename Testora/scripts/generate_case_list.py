#!/usr/bin/env python3
"""从 PatchGuru .cache/pr_ids 生成 Testora case 列表。

用法:
  python scripts/generate_case_list.py
  python scripts/generate_case_list.py --patchguru-root ../PatchGuru --out scripts/patchguru_all_400.txt
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATCHGURU = ROOT.parent / "PatchGuru"
DEFAULT_OUT = ROOT / "scripts" / "patchguru_all_400.txt"
PROJECTS = ["keras", "marshmallow", "pandas", "scipy"]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 PatchGuru 全量 PR case 列表")
    parser.add_argument("--patchguru-root", type=Path, default=DEFAULT_PATCHGURU)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    pr_ids_dir = args.patchguru_root / ".cache" / "pr_ids"
    if not pr_ids_dir.is_dir():
        raise SystemExit(f"缺少 PR 列表目录: {pr_ids_dir}")

    lines: list[str] = []
    for project in PROJECTS:
        pr_file = pr_ids_dir / f"{project}.txt"
        if not pr_file.exists():
            raise SystemExit(f"缺少: {pr_file}")
        prs = [line.strip() for line in pr_file.read_text().splitlines() if line.strip()]
        for pr in prs:
            lines.append(f"{project} {pr}")

    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"写入 {len(lines)} 个 case -> {out}")
    for project in PROJECTS:
        n = sum(1 for line in lines if line.startswith(project + " "))
        print(f"  {project}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
