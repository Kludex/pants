# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import os
import pprint
import subprocess
from textwrap import dedent
from typing import Callable

import pytest

from pants.backend.go import target_type_rules
from pants.backend.go.goals import package_binary
from pants.backend.go.goals.package_binary import GoBinaryFieldSet
from pants.backend.go.target_types import GoBinaryTarget, GoModTarget, GoPackageTarget
from pants.backend.go.util_rules import (
    assembly,
    build_opts,
    build_pkg,
    build_pkg_target,
    first_party_pkg,
    go_mod,
    goroot,
    import_analysis,
    link,
    sdk,
    third_party_pkg,
)
from pants.backend.go.util_rules.build_opts import (
    GoBuildOptions,
    GoBuildOptionsFromTargetRequest,
    msan_supported,
    race_detector_supported,
)
from pants.backend.go.util_rules.goroot import GoRoot
from pants.build_graph.address import Address
from pants.core.goals.package import BuiltPackage
from pants.engine.rules import QueryRule
from pants.engine.target import Target
from pants.testutil.rule_runner import RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    rule_runner = RuleRunner(
        rules=[
            *build_opts.rules(),
            # for building binaries:
            *import_analysis.rules(),
            *package_binary.rules(),
            *assembly.rules(),
            *build_pkg.rules(),
            *build_pkg_target.rules(),
            *first_party_pkg.rules(),
            *go_mod.rules(),
            *goroot.rules(),
            *link.rules(),
            *target_type_rules.rules(),
            *third_party_pkg.rules(),
            *sdk.rules(),
            QueryRule(GoBuildOptions, (GoBuildOptionsFromTargetRequest,)),
            QueryRule(BuiltPackage, (GoBinaryFieldSet,)),
            QueryRule(GoRoot, ()),
        ],
        target_types=[GoModTarget, GoPackageTarget, GoBinaryTarget],
    )
    rule_runner.set_options([], env_inherit={"PATH"})
    return rule_runner


@pytest.mark.parametrize(
    "field_name,getter,enabled_for_platform",
    (
        ("race", lambda opts: opts.with_race_detector, race_detector_supported),
        ("msan", lambda opts: opts.with_msan, msan_supported),
    ),
)
def test_race_detector_fields_work_as_expected(
    rule_runner: RuleRunner,
    field_name: str,
    getter: Callable[[GoBuildOptions], bool],
    enabled_for_platform: Callable[[GoRoot], bool],
) -> None:
    def module_files(dir_path: str, value: bool | None) -> dict:
        field = f", {field_name}={value}" if value is not None else ""
        return {
            f"{dir_path}/BUILD": dedent(
                f"""\
            go_mod(name="mod"{field})
            go_package(name="pkg")
            go_binary(name="bin_with_field_unspecified")
            go_binary(name="bin_with_field_false", {field_name}=False)
            go_binary(name="bin_with_field_true", {field_name}=True)
            """
            ),
            f"{dir_path}/go.mod": f"module test.pantsbuild.org/{dir_path}\n",
            f"{dir_path}/main.go": dedent(
                """\
            package main
            func main() {}
            """
            ),
            f"{dir_path}/pkg_false/BUILD": f"go_package(test_{field_name}=False)\n",
            f"{dir_path}/pkg_false/foo.go": "package pkg_false\n",
            f"{dir_path}/pkg_true/BUILD": f"go_package(test_{field_name}=True)\n",
            f"{dir_path}/pkg_true/foo.go": "package pkg_true\n",
        }

    files = {
        **module_files("mod_unspecified", None),
        **module_files("mod_false", False),
        **module_files("mod_true", True),
    }
    pprint.pprint(files)
    rule_runner.write_files(files)

    goroot = rule_runner.request(GoRoot, [])
    if not enabled_for_platform(goroot):
        pytest.skip(f"Skipping test because `{field_name}` is not supported on this platform.")

    def assert_value(
        address: Address, expected_value: bool, *, for_tests: bool = False, msg: str
    ) -> None:
        opts = rule_runner.request(
            GoBuildOptions,
            (GoBuildOptionsFromTargetRequest(address=address, for_tests=for_tests),),
        )
        assert getter(opts) is expected_value, f"{address}: expected {expected_value} {msg}"

    # go_mod does not specify a value for `race`
    assert_value(
        Address("mod_unspecified", target_name="bin_with_field_unspecified"),
        False,
        msg="when unspecified on go_binary and when unspecified on go_mod",
    )
    assert_value(
        Address("mod_unspecified", target_name="bin_with_field_false"),
        False,
        msg=f"when {field_name}=False on go_binary and when unspecified on go_mod",
    )
    assert_value(
        Address("mod_unspecified", target_name="bin_with_field_true"),
        True,
        msg=f"when {field_name}=True on go_binary and when unspecified on go_mod",
    )
    assert_value(
        Address("mod_unspecified", target_name="pkg"),
        False,
        for_tests=True,
        msg="for go_package when unspecified on go_mod",
    )
    assert_value(
        Address("mod_unspecified/pkg_false"),
        False,
        for_tests=True,
        msg=f"for go_package(test_{field_name}=False) when unspecified on go_mod",
    )
    assert_value(
        Address("mod_unspecified/pkg_true"),
        True,
        for_tests=True,
        msg=f"for go_package(test_{field_name}=True) when unspecified on go_mod",
    )
    assert_value(
        Address("mod_unspecified", target_name="mod"),
        False,
        msg="for go_mod when unspecified on go_mod",
    )

    # go_mod specifies False for `race`
    assert_value(
        Address("mod_false", target_name="bin_with_field_unspecified"),
        False,
        msg=f"when unspecified on go_binary and when {field_name}=False on go_mod",
    )
    assert_value(
        Address("mod_false", target_name="bin_with_field_false"),
        False,
        msg=f"when {field_name}=False on go_binary and when {field_name}=False on go_mod",
    )
    assert_value(
        Address("mod_false", target_name="bin_with_field_true"),
        True,
        msg=f"when {field_name}=True on go_binary and when {field_name}=False on go_mod",
    )
    assert_value(
        Address("mod_false", target_name="pkg"),
        False,
        for_tests=True,
        msg=f"for go_package when {field_name}=False on go_mod",
    )
    assert_value(
        Address("mod_false/pkg_false"),
        False,
        for_tests=True,
        msg=f"for go_package(test_{field_name}=False) when {field_name}=False on go_mod",
    )
    assert_value(
        Address("mod_false/pkg_true"),
        True,
        for_tests=True,
        msg=f"for go_package(test_{field_name}=True) when {field_name}=False on go_mod",
    )
    assert_value(
        Address("mod_false", target_name="mod"),
        False,
        msg=f"for go_mod when {field_name}=False on go_mod",
    )

    # go_mod specifies True for `race`
    assert_value(
        Address("mod_true", target_name="bin_with_field_unspecified"),
        True,
        msg=f"when unspecified on go_binary and when {field_name}=True on go_mod",
    )
    assert_value(
        Address("mod_true", target_name="bin_with_field_false"),
        False,
        msg=f"when {field_name}=False on go_binary and when {field_name}=True on go_mod",
    )
    assert_value(
        Address("mod_true", target_name="bin_with_field_true"),
        True,
        msg=f"when {field_name}=True on go_binary and when {field_name}=True on go_mod",
    )
    assert_value(
        Address("mod_true", target_name="pkg"),
        True,
        for_tests=True,
        msg=f"for go_package when {field_name}=True on go_mod",
    )
    assert_value(
        Address("mod_true/pkg_false"),
        False,
        for_tests=True,
        msg=f"for go_package(test_{field_name}=False) when {field_name}=True on go_mod",
    )
    assert_value(
        Address("mod_true/pkg_true"),
        True,
        for_tests=True,
        msg=f"for go_package(test_{field_name}=True) when {field_name}=True on go_mod",
    )
    assert_value(
        Address("mod_true", target_name="mod"),
        True,
        msg=f"for go_mod when {field_name}=True on go_mod",
    )

    # Test when `--go-test-force-{race,msan}` is in effect.
    rule_runner.set_options([f"--go-test-force-{field_name}"], env_inherit={"PATH"})
    assert_value(
        Address("mod_unspecified", target_name="pkg"),
        True,
        for_tests=True,
        msg=f"for go_package when --go-test-force-{field_name} and when unspecified on go_mod",
    )
    assert_value(
        Address("mod_false", target_name="pkg"),
        True,
        for_tests=True,
        msg=f"for go_package when --go-test-force-{field_name }and when {field_name}=False on go_mod",
    )


def build_package(rule_runner: RuleRunner, binary_target: Target) -> BuiltPackage:
    field_set = GoBinaryFieldSet.create(binary_target)
    result = rule_runner.request(BuiltPackage, [field_set])
    rule_runner.write_digest(result.digest)
    return result


def test_race_detector_actually_works(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "BUILD": dedent(
                """\
            go_mod(name="mod")
            go_package()
            go_binary(name="bin", race=True)
            """
            ),
            "go.mod": "module example.pantsbuild.org/racy\n",
            "racy.go": dedent(
                """\
            // From example in https://go.dev/blog/race-detector
            package main

            import "fmt"

            func main() {
                done := make(chan bool)
                m := make(map[string]string)
                m["name"] = "world"
                go func() {
                    m["name"] = "data race"
                    done <- true
                }()
                fmt.Println("Hello,", m["name"])
                <-done
            }
            """
            ),
        }
    )

    binary_tgt = rule_runner.get_target(Address("", target_name="bin"))
    built_package = build_package(rule_runner, binary_tgt)
    assert len(built_package.artifacts) == 1
    assert built_package.artifacts[0].relpath == "bin"

    result = subprocess.run([os.path.join(rule_runner.build_root, "bin")], capture_output=True)
    assert result.returncode == 66  # standard exit code if race detector finds a race
    assert b"WARNING: DATA RACE" in result.stderr