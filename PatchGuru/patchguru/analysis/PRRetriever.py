from patchguru import Config
from patchguru.utils.ClonedRepoManager import ClonedRepoManager
from patchguru.utils.PullRequest import PullRequest
from patchguru.utils.proxy import apply_host_proxy_env
from patchguru.utils.git_network import git_fetch_with_retry, git_pull_with_retry
# from change_reviewer.utils.Logger import get_logger
from github import Github, Auth
from patchguru.utils.Tracker import append_event, Event

# logger = get_logger(__name__)

def get_repo(project_name):
    apply_host_proxy_env()
    pool = Config.clones_pool_dir()
    cbase = Config.container_base_name
    if project_name == "pandas":
        cloned_repo_manager = ClonedRepoManager(
            pool, "pandas", "pandas-dev/pandas", cbase("pandas"), "pandas")
    elif project_name == "scikit-learn":
        cloned_repo_manager = ClonedRepoManager(
            pool, "scikit-learn", "scikit-learn/scikit-learn", cbase("scikit-learn"), "sklearn")
    elif project_name == "scipy":
        cloned_repo_manager = ClonedRepoManager(
            pool, "scipy", "scipy/scipy", cbase("scipy"), "scipy")
    elif project_name == "numpy":
        cloned_repo_manager = ClonedRepoManager(
            pool, "numpy", "numpy/numpy", cbase("numpy"), "numpy")
    elif project_name == "transformers":
        cloned_repo_manager = ClonedRepoManager(
            pool, "transformers", "huggingface/transformers", cbase("transformers"), "transformers")
    elif project_name == "keras":
        cloned_repo_manager = ClonedRepoManager(
            pool, "keras", "keras-team/keras", cbase("keras"), "keras")
    elif project_name == "marshmallow":
        cloned_repo_manager = ClonedRepoManager(
            pool, "marshmallow", "marshmallow-code/marshmallow", cbase("marshmallow"), "marshmallow")
    elif project_name == "pytorch_geometric":
        cloned_repo_manager = ClonedRepoManager(
            pool, "pytorch_geometric", "pyg-team/pytorch_geometric",
            cbase("pytorch_geometric"), "torch_geometric")
    elif project_name == "scapy":
        cloned_repo_manager = ClonedRepoManager(
            pool, "scapy", "secdev/scapy", cbase("scapy"), "scapy")
    else:
        raise ValueError(f"Project {project_name} is not supported.")

    cloned_repo = cloned_repo_manager.get_cloned_repo("main")
    repo = cloned_repo.repo
    for branch in ("main", "master"):
        try:
            repo.git.checkout(branch)
            if git_pull_with_retry(repo, context=f"{project_name}/{branch}"):
                break
        except Exception:
            continue
    else:
        git_fetch_with_retry(repo, context=f"{project_name}/main")
    token = open(".github_token", "r").read().strip()
    github = Github(auth=Auth.Token(token))
    github_repo = github.get_repo(cloned_repo_manager.repo_id)

    return github_repo, cloned_repo_manager

def retrieve_pr(project, pr_nb):
    #logger.info(f"Retrieving information of Pull Request #{pr_nb} for project {project}...")
    append_event(Event(
        level="INFO", pr_nb=pr_nb,
        message=f"Retrieving information of Pull Request #{pr_nb} for project {project}..."
    ))
    try:
        github_repo, cloned_repo_manager = get_repo(project)
        github_pr = github_repo.get_pull(pr_nb)
        append_event(Event(
            level="DEBUG", pr_nb=pr_nb,
            message=f"Successfully retrieved information from GitHub. Extracting additional information from local repo."
        ))
        pr = PullRequest(github_pr, github_repo, cloned_repo_manager)
        append_event(
            Event(
            level="INFO",
            pr_nb=pr_nb,
            message=f"Successfully retrieved Pull Request #{pr_nb}: {github_pr.title}",
            type="PRInfo",
            info = {
                "pr_title": github_pr.title,
                "pr_url": github_pr.html_url,
                "pre_commit": pr.pre_commit,
                "post_commit": pr.post_commit,
                "num_changed_files": len(pr.get_modified_files()),
                "modified_files": ", ".join(pr.get_modified_files()),
                "num_changed_functions": len(pr.changed_functions),
                "changed_functions": ", ".join(pr.changed_functions),
            }
            )
        )
    except Exception as e:
        import traceback
        append_event(Event(
            level="ERROR", pr_nb=pr_nb,
            message=f"Error while retrieving Pull Request #{pr_nb}: {e}\n ----- Stack Trace -----\n{traceback.format_exc()}"
        ))
        return None, None, None
    return pr, cloned_repo_manager, github_repo

if __name__ == "__main__":
    project = "pandas"  # Change this to the desired project
    pr_nb = 62101  # Change this to the desired PR number
    pr, cloned_repo_manager, github_repo = retrieve_pr(project, pr_nb)
