# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath
from textwrap import dedent
from typing import Any, Dict, Iterable, List, cast

import pytest
from _pytest.tmpdir import TempPathFactory

from pants.backend.python.target_types import PythonLibrary, PythonRequirementLibrary
from pants.backend.python.util_rules import pex_from_targets
from pants.backend.python.util_rules.pex import Pex, PexPlatforms, PexRequest, PexRequirements
from pants.backend.python.util_rules.pex_from_targets import PexFromTargetsRequest
from pants.build_graph.address import Address
from pants.engine.internals.scheduler import ExecutionError
from pants.testutil.rule_runner import QueryRule, RuleRunner
from pants.util.contextutil import pushd
from pants.util.ordered_set import OrderedSet


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *pex_from_targets.rules(),
            QueryRule(PexRequest, (PexFromTargetsRequest,)),
            QueryRule(Pex, (PexFromTargetsRequest,)),
        ],
        target_types=[PythonLibrary, PythonRequirementLibrary],
    )


@dataclass(frozen=True)
class Project:
    name: str
    version: str


build_deps = ["setuptools==54.1.2", "wheel==0.36.2"]


def create_project_dir(workdir: Path, project: Project) -> PurePath:
    project_dir = workdir / "projects" / project.name
    project_dir.mkdir(parents=True)

    (project_dir / "pyproject.toml").write_text(
        dedent(
            f"""\
            [build-system]
            requires = {build_deps}
            build-backend = "setuptools.build_meta"
            """
        )
    )
    (project_dir / "setup.cfg").write_text(
        dedent(
            f"""\
                [metadata]
                name = {project.name}
                version = {project.version}
                """
        )
    )
    return project_dir


def create_dists(workdir: Path, project: Project, *projects: Project) -> PurePath:
    project_dirs = [create_project_dir(workdir, proj) for proj in (project, *projects)]

    pex = workdir / "pex"
    subprocess.run(
        args=[
            sys.executable,
            "-m",
            "pex",
            *project_dirs,
            *build_deps,
            "--include-tools",
            "-o",
            pex,
        ],
        check=True,
    )

    find_links = workdir / "find-links"
    subprocess.run(
        args=[
            sys.executable,
            "-m",
            "pex.tools",
            pex,
            "repository",
            "extract",
            "--find-links",
            find_links,
        ],
        check=True,
    )
    return find_links


def info(rule_runner: RuleRunner, pex: Pex) -> Dict[str, Any]:
    rule_runner.scheduler.write_digest(pex.digest)
    completed_process = subprocess.run(
        args=[
            sys.executable,
            "-m",
            "pex.tools",
            pex.name,
            "info",
        ],
        cwd=rule_runner.build_root,
        stdout=subprocess.PIPE,
        check=True,
    )
    return cast(Dict[str, Any], json.loads(completed_process.stdout))


def requirements(rule_runner: RuleRunner, pex: Pex) -> List[str]:
    return cast(List[str], info(rule_runner, pex)["requirements"])


def test_constraints_validation(tmp_path_factory: TempPathFactory, rule_runner: RuleRunner) -> None:
    find_links = create_dists(
        tmp_path_factory.mktemp("sdists"),
        Project("Foo-Bar", "1.0.0"),
        Project("Bar", "5.5.5"),
        Project("baz", "2.2.2"),
        Project("QUX", "3.4.5"),
    )

    # Turn the project dir into a git repo, so it can be cloned.
    foorl_dir = create_project_dir(tmp_path_factory.mktemp("git"), Project("foorl", "9.8.7"))
    with pushd(str(foorl_dir)):
        subprocess.check_call(["git", "init"])
        subprocess.check_call(["git", "config", "user.name", "dummy"])
        subprocess.check_call(["git", "config", "user.email", "dummy@dummy.com"])
        subprocess.check_call(["git", "add", "--all"])
        subprocess.check_call(["git", "commit", "-m", "initial commit"])
        subprocess.check_call(["git", "branch", "9.8.7"])

    # This string won't parse as a Requirement if it doesn't contain a netloc,
    # so we explicitly mention localhost.
    url_req = f"foorl@ git+file://localhost{foorl_dir.as_posix()}@9.8.7"

    rule_runner.add_to_build_file(
        "",
        dedent(
            f"""
            python_requirement_library(name="foo", requirements=["foo-bar>=0.1.2"])
            python_requirement_library(name="bar", requirements=["bar==5.5.5"])
            python_requirement_library(name="baz", requirements=["baz"])
            python_requirement_library(name="foorl", requirements=["{url_req}"])
            python_library(name="util", sources=[], dependencies=[":foo", ":bar"])
            python_library(name="app", sources=[], dependencies=[":util", ":baz", ":foorl"])
            """
        ),
    )
    rule_runner.create_file(
        "constraints1.txt",
        dedent(
            """
            # Comment.
            --find-links=https://duckduckgo.com
            Foo._-BAR==1.0.0  # Inline comment.
            bar==5.5.5
            baz==2.2.2
            qux==3.4.5
            # Note that pip does not allow URL requirements in constraints files,
            # so there is no mention of foorl here.
        """
        ),
    )

    def get_pex_request(
        constraints_file: str | None,
        resolve_all_constraints: bool | None,
        *,
        direct_deps_only: bool = False,
        additional_args: Iterable[str] = (),
    ) -> PexRequest:
        args = ["--backend-packages=pants.backend.python"]
        request = PexFromTargetsRequest(
            [Address("", target_name="app")],
            output_filename="demo.pex",
            internal_only=True,
            direct_deps_only=direct_deps_only,
            additional_args=additional_args,
        )
        if resolve_all_constraints is not None:
            args.append(f"--python-setup-resolve-all-constraints={resolve_all_constraints!r}")
        if constraints_file:
            args.append(f"--python-setup-requirement-constraints={constraints_file}")
        args.append("--python-repos-indexes=[]")
        args.append(f"--python-repos-repos={find_links}")
        rule_runner.set_options(args, env_inherit={"PATH"})
        pex_request = rule_runner.request(PexRequest, [request])
        assert OrderedSet(additional_args).issubset(OrderedSet(pex_request.additional_args))
        return pex_request

    additional_args = ["--no-strip-pex-env"]

    pex_req1 = get_pex_request(
        "constraints1.txt",
        resolve_all_constraints=False,
        additional_args=additional_args,
    )
    assert pex_req1.requirements == PexRequirements(
        ["foo-bar>=0.1.2", "bar==5.5.5", "baz", url_req], apply_constraints=True
    )

    pex_req1_direct = get_pex_request(
        "constraints1.txt", resolve_all_constraints=False, direct_deps_only=True
    )
    assert pex_req1_direct.requirements == PexRequirements(["baz", url_req], apply_constraints=True)

    pex_req2 = get_pex_request(
        "constraints1.txt",
        resolve_all_constraints=True,
        additional_args=additional_args,
    )
    pex_req2_reqs = pex_req2.requirements
    assert isinstance(pex_req2_reqs, PexRequirements)
    assert list(pex_req2_reqs.req_strings) == ["bar==5.5.5", "baz", "foo-bar>=0.1.2", url_req]
    assert pex_req2_reqs.resolved_dists is not None
    assert not info(rule_runner, pex_req2_reqs.resolved_dists.pex)["strip_pex_env"]
    resolved_dists = pex_req2_reqs.resolved_dists
    assert ["Foo._-BAR==1.0.0", "bar==5.5.5", "baz==2.2.2", "foorl", "qux==3.4.5"] == requirements(
        rule_runner, resolved_dists.pex
    )

    pex_req2_direct = get_pex_request(
        "constraints1.txt",
        resolve_all_constraints=True,
        direct_deps_only=True,
        additional_args=additional_args,
    )
    pex_req2_reqs = pex_req2_direct.requirements
    assert isinstance(pex_req2_reqs, PexRequirements)
    assert list(pex_req2_reqs.req_strings) == ["baz", url_req]
    assert pex_req2_reqs.resolved_dists == resolved_dists
    assert not info(rule_runner, pex_req2_reqs.resolved_dists.pex)["strip_pex_env"]

    pex_req3_direct = get_pex_request(
        "constraints1.txt",
        resolve_all_constraints=True,
        direct_deps_only=True,
    )
    pex_req3_reqs = pex_req3_direct.requirements
    assert isinstance(pex_req3_reqs, PexRequirements)
    assert list(pex_req3_reqs.req_strings) == ["baz", url_req]
    assert pex_req3_reqs.resolved_dists is not None
    assert pex_req3_reqs.resolved_dists != resolved_dists
    assert info(rule_runner, pex_req3_reqs.resolved_dists.pex)["strip_pex_env"]

    with pytest.raises(ExecutionError) as err:
        get_pex_request(None, resolve_all_constraints=True)
    assert len(err.value.wrapped_exceptions) == 1
    assert isinstance(err.value.wrapped_exceptions[0], ValueError)
    assert (
        "`[python-setup].resolve_all_constraints` is enabled, so "
        "`[python-setup].requirement_constraints` must also be set."
    ) in str(err.value)

    # Shouldn't error, as we don't explicitly set --resolve-all-constraints.
    get_pex_request(None, resolve_all_constraints=None)


def test_issue_12222(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "constraints.txt": "foo==1.0\nbar==1.0",
            "BUILD": dedent(
                """
            python_requirement_library(name="foo",requirements=["foo"])
            python_requirement_library(name="bar",requirements=["bar"])
            python_library(name="lib",sources=[],dependencies=[":foo"])
            """
            ),
        }
    )
    request = PexFromTargetsRequest(
        [Address("", target_name="lib")],
        output_filename="demo.pex",
        internal_only=False,
        platforms=PexPlatforms(["some-platform-x86_64"]),
    )
    rule_runner.set_options(
        [
            "--python-setup-requirement-constraints=constraints.txt",
            "--python-setup-resolve-all-constraints",
        ]
    )
    result = rule_runner.request(PexRequest, [request])

    assert result.requirements == PexRequirements(["foo"], apply_constraints=True)


@pytest.mark.parametrize("internal_only", [True, False])
def test_component_pexes(rule_runner: RuleRunner, internal_only: bool) -> None:
    """An internal-only PexFromTargetsRequest with a lockfile produces component PEXes."""

    rule_runner.write_files(
        {
            "constraints.txt": dedent(
                """
                certifi==2021.5.30
                charset_normalizer==2.0.4
                idna==3.2
                requests==2.26.0
                urllib3==1.26.6
                """
            ),
            "BUILD": dedent(
                """
            python_requirement_library(name="requests",requirements=["requests"])
            python_library(name="lib",sources=[],dependencies=[":requests"])
            """
            ),
        }
    )
    request = PexFromTargetsRequest(
        [Address("", target_name="lib")],
        output_filename="demo.pex",
        internal_only=internal_only,
    )
    rule_runner.set_options(
        [
            "--backend-packages=pants.backend.python",
            "--python-setup-requirement-constraints=constraints.txt",
            "--python-setup-resolve-all-constraints",
        ],
        env_inherit={"PATH", "PYENV_ROOT", "HOME"},
    )
    pex_info = info(rule_runner, rule_runner.request(Pex, [request]))

    if internal_only:
        # Should have a pex-path containing the root requirement.
        assert pex_info["requirements"] == []
        assert pex_info["distributions"] == {}
        assert pex_info["pex_path"] == "__reqs/requests.pex"
    else:
        # Should have a root requirement, and five distributions.
        assert pex_info["requirements"] == ["requests"]
        assert len(pex_info["distributions"]) == 5
        assert pex_info["pex_path"] is None
