import subprocess
import typer
from cli.utils import remove_all_package_builds, root_env_vars, build_all_packages
from python_on_whales.docker_client import DockerClient

app = typer.Typer()


@app.command()
def indexer(build: bool = True, open_shell: bool = False):
    if build:
        remove_all_package_builds()
        build_all_packages()
    docker = DockerClient(compose_files=["packages/indexer/docker-compose.tests.yaml"])
    docker.compose.down(remove_orphans=True)
    try:
        if open_shell:
            docker.compose.run(
                "indexer-test",
                command=["bash"],
                tty=True,
                remove=True,
            )
        else:
            docker.compose.up(build=build, abort_on_container_exit=True)
    except:
        pass


@app.command()
def parse():
    docker = DockerClient(compose_files=["packages/parse/docker-compose.tests.yaml"])
    docker.compose.down(remove_orphans=True)
    try:
        docker.compose.up(build=True, abort_on_container_exit=True)
    except:
        pass
