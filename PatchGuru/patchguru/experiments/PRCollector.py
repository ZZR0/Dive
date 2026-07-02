from patchguru.analysis.PRRetriever import get_repo, retrieve_pr
from patchguru.utils.Logger import get_logger
from tqdm import tqdm
import os
from pathlib import Path
from patchguru import Config
import argparse
import time

start_time = time.strftime("%Y%m%d-%H%M%S")
logger = get_logger(__name__, log_file=f"logs/pr_collector_{start_time}.log")

DEFAULT_OUT_DIR = ".cache/pr_data/single_changed_function_prs"


def _load_ids(path: Path) -> set[int]:
    ids = set()
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.add(int(line.split()[-1]))
    return ids


def _save_ids(path: Path, ids: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{n}\n" for n in sorted(ids, reverse=True)))


def collect_single_changed_function_prs(
    project_name,
    n_prs=10,
    out_dir=None,
    exclude_dir=None,
    max_pr=None,
):
    logger.info(f"Collecting PRs for project {project_name}...")
    dataset_path = Path(out_dir or DEFAULT_OUT_DIR) / f"{project_name}.txt"
    exclude = _load_ids(Path(exclude_dir) / f"{project_name}.txt") if exclude_dir else set()

    dataset = _load_ids(dataset_path)
    if dataset:
        logger.info(f"Resume from {dataset_path}: {len(dataset)} PRs, target {n_prs}")
        if len(dataset) >= n_prs:
            return dataset

    github_repo, _ = get_repo(project_name)
    cut_off = Config.PR_CUT_OFF[project_name]
    if exclude:
        logger.info(f"Excluding {len(exclude)} benchmark PRs")
    if max_pr is not None:
        logger.info(f"Only scanning PRs <= #{max_pr}")

    for pr_info in tqdm(
        github_repo.get_pulls(state="closed", sort="created", direction="desc"),
        desc=f"Collecting PRs for {project_name}",
    ):
        if len(dataset) >= n_prs:
            break
        pr_number = int(pr_info.number)
        if pr_number < cut_off or pr_number in exclude or pr_number in dataset:
            continue
        if max_pr is not None and pr_number > max_pr:
            continue

        try:
            pr, _, _ = retrieve_pr(project_name, pr_number)
            if pr and len(pr.changed_functions) == 1 and not pr.added_functions and not pr.removed_functions:
                dataset.add(pr_number)
                _save_ids(dataset_path, dataset)
                logger.info(f"Accepted PR #{pr_number} ({len(dataset)}/{n_prs})")
        except Exception as e:
            logger.error(f"Failed to retrieve PR #{pr_number}: {e}")

    logger.info(f"Collected {len(dataset)} PRs -> {dataset_path}")
    _save_ids(dataset_path, dataset)
    return dataset


def filter_backported_prs(project_name, dataset):
    new_dataset = []
    for pr_nb in dataset:
        pr, _, _ = retrieve_pr(project_name, pr_nb)
        if pr and "backport" in pr.title.lower():
            logger.info(f"Removed backported PR #{pr_nb}")
            continue
        new_dataset.append(pr_nb)
        if pr:
            print(pr.title)

    dataset_path = os.path.join(DEFAULT_OUT_DIR, f"{project_name}.txt")
    os.makedirs(DEFAULT_OUT_DIR, exist_ok=True)
    with open(dataset_path, "w") as f:
        for pr_number in new_dataset:
            f.write(f"{pr_number}\n")
    return new_dataset


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect PRs for a given project.")
    parser.add_argument("-p", "--project", type=str, required=True)
    parser.add_argument("-n", "--n_prs", type=int, default=100)
    parser.add_argument("--out-dir", type=str, default=None)
    parser.add_argument("--exclude-dir", type=str, default=None)
    parser.add_argument("--max-pr", type=int, default=None)
    args = parser.parse_args()
    collect_single_changed_function_prs(
        args.project,
        n_prs=args.n_prs,
        out_dir=args.out_dir,
        exclude_dir=args.exclude_dir,
        max_pr=args.max_pr,
    )
