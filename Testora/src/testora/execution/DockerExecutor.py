import os
import uuid
import docker
import tarfile
import tempfile
from os.path import join
from os import chdir, getcwd

from testora.util.container_deps import wrap_test_command


class DockerExecutor:
    def __init__(self, container_name, project_name, coverage_files):
        client = docker.from_env()
        self.container = client.containers.get(container_name)
        self.container.start()
        self.project_name = project_name

        # adapt paths of coverage files to the container's file system
        self.coverage_files = [f"/home/{project_name}/{f}" for f in coverage_files]

    def copy_code_to_container(self, code, target_file_path):
        target_dir = target_file_path.rsplit("/", 1)[0]
        target_file_name = target_file_path.rsplit("/", 1)[1]

        with tempfile.TemporaryDirectory() as tmp_dir:
            code_file = join(tmp_dir, target_file_name)
            with open(code_file, "w") as f:
                f.write(code)
            tar_file = join(tmp_dir, "archive.tar")
            with tarfile.open(tar_file, mode="w") as tar:
                wd = getcwd()
                try:
                    chdir(tmp_dir)
                    tar.add(target_file_name)
                finally:
                    chdir(wd)

            data = open(tar_file, "rb").read()
            self.container.put_archive(target_dir, data)

    def copy_file_from_container(self, file_path_in_container, target_dir):
        data, _ = self.container.get_archive(file_path_in_container)
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tar_fp:
            temp_tar_file = tar_fp.name
            for chunk in data:
                tar_fp.write(chunk)
        try:
            with tarfile.open(temp_tar_file, mode="r") as tar:
                tar.extractall(target_dir)
        finally:
            os.remove(temp_tar_file)

    def execute_python_code(self, code):
        run_id = uuid.uuid4().hex
        testora_dir = f"/tmp/Testora_{run_id}"
        coverage_path = f"/tmp/coverage_report_{run_id}"

        # create a fresh directory to get rid of any old state
        self.container.exec_run(f"rm -rf {testora_dir} {coverage_path}")
        self.container.exec_run(f"mkdir -p {testora_dir}")

        self.copy_code_to_container(code, f"{testora_dir}/Testora_test_code.py")
        coverage_files = ",".join(f"\"{f}\"" for f in self.coverage_files)
        # -u to avoid non-deterministic buffering
        command = (
            f"timeout 300s python -u -m coverage run "
            f"--include={coverage_files} "
            f"--data-file {coverage_path} {testora_dir}/Testora_test_code.py"
        )
        command = wrap_test_command(
            self.container.name, self.project_name, command)

        exec_result = self.container.exec_run(command)
        output = exec_result.output.decode("utf-8")

        with tempfile.TemporaryDirectory() as tmp_dir:
            self.copy_file_from_container(coverage_path, tmp_dir)
            coverage_file = join(tmp_dir, os.path.basename(coverage_path))
            if not os.path.isfile(coverage_file):
                for root, _, files in os.walk(tmp_dir):
                    if os.path.basename(coverage_path) in files:
                        coverage_file = join(root, os.path.basename(coverage_path))
                        break
            with open(coverage_file, "rb") as f:
                coverage_report = f.read()

        self.container.exec_run(f"rm -rf {testora_dir} {coverage_path}")
        return output, coverage_report


if __name__ == "__main__":
    code = """
x = 23

print(x)
x.foo()
print("never reach this")
"""

    executor = DockerExecutor("pandas-dev", "pandas", coverage_files=[])
    output = executor.execute_python_code(code)
    print(output)
