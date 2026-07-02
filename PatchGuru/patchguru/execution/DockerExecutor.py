import docker
import tempfile
import tarfile
import os
import re
from os import chdir, getcwd
from typing import Optional, Tuple
import argparse
import time
from patchguru.utils.PullRequest import PullRequest
from patchguru.analysis.PRRetriever import get_repo
from patchguru.utils.Tracker import append_event, Event
from patchguru.utils.marshmallow_layout import shell_cleanup_script


def container_project(container_name: str) -> Optional[str]:
    """从容器名解析项目 slug，如 marshmallow-dev1 / pgabl-marshmallow-dev2 -> marshmallow。"""
    name = container_name.lstrip("/")
    match = re.match(r"^(?:[^-]+-)?(.+)-dev\d+$", name)
    return match.group(1) if match else None


class DockerExecutor:
    def __init__(self, container_name):
        client = docker.from_env()
        self.container = client.containers.get(container_name)
        self.container.start()

    def copy_code_to_container(self, code, target_file_path):
        target_dir = target_file_path.rsplit("/", 1)[0]
        target_file_name = target_file_path.rsplit("/", 1)[1]

        with tempfile.TemporaryDirectory() as tmp_dir:
            code_file = os.path.join(tmp_dir, target_file_name)
            with open(code_file, "w") as f:
                f.write(code)
            tar_file = os.path.join(tmp_dir, "archive.tar")
            with tarfile.open(tar_file, mode="w") as tar:
                wd = getcwd()
                try:
                    chdir(tmp_dir)
                    tar.add(target_file_name)
                finally:
                    chdir(wd)

            data = open(tar_file, "rb").read()
            self.container.put_archive(target_dir, data)

    def filter_logs(self, logs: str, container_name: str) -> str:
        import re

    def execute_python_code(self, code: str, python_executable: str = "python3", timeout: Optional[int] = 900) -> Tuple[bool, str, str]:
        append_event(Event(
            level="INFO",
            message=f"Executing code in container {self.container.name} with timeout {timeout} seconds.",
            type="ExecutionStart"
        ))
        exec_result = self.container.exec_run("rm -rf /tmp/PatchGuru")

        exec_result = self.container.exec_run("mkdir /tmp/PatchGuru")
        self.copy_code_to_container(code, "/tmp/PatchGuru/PatchGuru_test_code.py")
        command = (
            f"timeout {timeout}s {python_executable} /tmp/PatchGuru/PatchGuru_test_code.py"
        )

        project = container_project(self.container.name)
        if project == "scipy":
            command = (
                f"bash -c 'source /root/conda/etc/profile.d/conda.sh"
                f" && eval \"$(mamba shell hook --shell bash)\" && mamba activate scipy-dev"
                f" && {command}'"
            )

        elif project == "keras":
            command = (
                f"bash -c 'cd /home/keras/"
                f" && pip install -e ."
                f" && {command}'"
            )

        elif project == "marshmallow":
            cleanup = shell_cleanup_script()
            command = (
                f"bash -c 'cd /home/marshmallow/"
                f" && {cleanup}"
                f" && pip install -q -e '.[dev]' 2>/dev/null || pip install -q -e ."
                f" && {command}'"
            )

        exec_result = self.container.exec_run(command)
        output = exec_result.output.decode("utf-8")
        exit_code = exec_result.exit_code
        from patchguru.utils.log_util import truncate_log_text

        logged_output = truncate_log_text(output, max_bytes=1_048_576, label="execution output")
        append_event(Event(
            level="INFO",
            message=[
                f"Execution completed in container {self.container.name} with exit code {exit_code}.",
                "---------------------- Execution Output -----------------",
                logged_output
            ],
            type="ExecutionEnd",
            info={
                "exit_code": exit_code,
                "output_bytes": len(output.encode("utf-8", errors="replace")),
                "output_truncated": logged_output != output,
            }
        ))
        return exit_code, output

    def execute_shell_command(self, command: str, timeout: int = 3600) -> Tuple[bool, str, str]:
        append_event(Event(
            level="INFO",
            message=f"Executing shell command in container {self.container.name}.",
            type="ShellCommandStart"
        ))


        project = container_project(self.container.name)
        if project == "scipy":
            if type(command) == list:
                command = [
                    "bash", "-c",
                    f"source /root/conda/etc/profile.d/conda.sh"
                    f" && eval \"$(mamba shell hook --shell bash)\""
                    f" && mamba activate scipy-dev"
                    f" && "
                    f" && " + " ".join(command)
                ]
            else:
                command = (
                    f"bash -c 'source /root/conda/etc/profile.d/conda.sh"
                    f" && eval \"$(mamba shell hook --shell bash)\" && mamba activate scipy-dev"
                    f" && cd /home/scipy && {command}'"
                )
        print(command)
        exec_result = self.container.exec_run(command)
        output = exec_result.output.decode("utf-8")
        exit_code = exec_result.exit_code
        append_event(Event(
            level="INFO",
            message=[
                f"Shell command execution completed in container {self.container.name} with exit code {exit_code}.",
                "---------------------- Command Output -----------------",
                output
            ],
            type="ShellCommandEnd",
            info={
                "exit_code": exit_code,
                "output": output
            }
        ))
        return exit_code, output
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Execute Python code in a Docker container for a given PR.")
    parser.add_argument("--repo", required=True, help="Repository name (e.g., keras)")
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout for code execution (seconds)")
    parser.add_argument("--file_path", type=str, required=True, help="Path to the Python file to execute")
    args = parser.parse_args()

    github_repo, cloned_repo_manager = get_repo(args.repo)
    github_pr = github_repo.get_pull(args.pr)
    pr = PullRequest(github_pr, github_repo, cloned_repo_manager)
    commit = pr.post_commit
    cloned_repo = cloned_repo_manager.get_cloned_repo(commit)
    container_name = cloned_repo.container_name
    docker_executor = DockerExecutor(container_name)
    file_path = args.file_path
    while True:
        start_time = time.time()
        with open(file_path, "r") as f:
            code = f.read()
        exit_code, output = docker_executor.execute_python_code(
            code, timeout=args.timeout)
        print(output)
        print(exit_code)
        print(f"Time taken for import: {time.time() - start_time} seconds")
        input("Press Enter to re-run the code...")
