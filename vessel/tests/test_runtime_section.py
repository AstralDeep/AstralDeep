# Tests for the [runtime] section and for folders that hold container data from earlier releases.

from __future__ import annotations

import os
import stat

import pytest
from conftest import needs_sh_scripts
from pydantic import BaseModel, ConfigDict, ValidationError
from test_deploy import LEGACY_OWNER, deployment_text

from astralvessel.deployment import ContainerData, DeploymentFile, RuntimeSection, load_deployment, runtime_of


def write(tmp_path, package_dir, head=""):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(package_dir, mounts="", egress=False, head=head))
    return path


def tree_bytes(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_a_file_with_runtime_loads(tmp_path, package_dir):
    head = '[runtime]\nmode = "process"\nbundle = "/p"\nbundle_sha256 = "' + "a" * 64 + '"\n'
    path = write(tmp_path, package_dir, head)
    deployment = load_deployment(path)
    assert runtime_of(deployment, lambda project: ["x_node_data"]) == RuntimeSection(
        mode="process", bundle="/p", bundle_sha256="a" * 64)


def test_a_new_folder_defaults_to_process(tmp_path, package_dir):
    deployment = load_deployment(write(tmp_path, package_dir))
    assert deployment.runtime is None
    assert runtime_of(deployment, lambda project: []).mode == "process"


def test_a_compose_file_means_container_data_and_nothing_is_written(tmp_path, package_dir):
    path = write(tmp_path, package_dir)
    (tmp_path / "compose.generated.yml").write_text("name: old\n")
    (tmp_path / ".generated").mkdir()
    (tmp_path / ".generated" / "node.env").write_text("K=v\n")
    before = tree_bytes(tmp_path)
    with pytest.raises(ContainerData, match='mode = "container"') as raised:
        runtime_of(load_deployment(path), lambda project: [])
    assert raised.value.code == "deployment.container_data"
    assert tree_bytes(tmp_path) == before


@needs_sh_scripts
def test_docker_volumes_alone_mean_container_data(tmp_path, package_dir, monkeypatch):
    path = write(tmp_path, package_dir)
    fake = tmp_path / "bin"
    fake.mkdir()
    docker = fake / "docker"
    docker.write_text('#!/bin/sh\nprintf "astralvessel_node_data\\nastralvessel_executor_data\\nother\\n"\n')
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    before = tree_bytes(tmp_path)
    with pytest.raises(ContainerData, match="astralvessel_node_data, astralvessel_executor_data"):
        runtime_of(load_deployment(path))
    assert tree_bytes(tmp_path) == before


def test_no_docker_means_no_volumes(tmp_path, package_dir, monkeypatch):
    path = write(tmp_path, package_dir)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert runtime_of(load_deployment(path)).mode == "process"


def test_an_old_file_loads_with_the_owner_notice(tmp_path, package_dir):
    deployment = load_deployment(write(tmp_path, package_dir, LEGACY_OWNER))
    assert deployment.notices and "[owner]" in deployment.notices[0]


def test_a_bad_runtime_section_names_the_key(tmp_path, package_dir):
    with pytest.raises(ValidationError, match="runtime.mode"):
        load_deployment(write(tmp_path, package_dir, '[runtime]\nmode = "vm"\n'))
    with pytest.raises(ValidationError, match="runtime.flavor"):
        load_deployment(write(tmp_path, package_dir, '[runtime]\nflavor = "x"\n'))
    with pytest.raises(ValidationError, match="runtime.bundle_sha256"):
        load_deployment(write(tmp_path, package_dir, '[runtime]\nbundle_sha256 = "abc"\n'))


def test_an_older_release_names_the_section_it_cannot_read():
    class Earlier(BaseModel):
        model_config = ConfigDict(extra="forbid")
        node: dict = {}
        agents: list = []

    document = {key: value for key, value in DeploymentFile().model_dump().items() if key in ("node", "agents")}
    with pytest.raises(ValidationError, match="runtime"):
        Earlier.model_validate({**document, "runtime": {"mode": "process"}})
