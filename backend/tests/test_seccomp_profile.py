"""A Docker profile must be resolved before containerd can consume it as OCI JSON."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.build_browser_seccomp import BLOCKED_SYSCALLS, build_profile, encoded_profile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "infra/price-worker/seccomp.json"
OUTPUT = ROOT / "infra/price-worker/seccomp-kubernetes.json"


def fixture(*rules):
    return {"defaultAction":"SCMP_ACT_ERRNO", "archMap":[
        {"architecture":"SCMP_ARCH_X86_64","subArchitectures":["SCMP_ARCH_X86","SCMP_ARCH_X32"]}],
        "syscalls":list(rules)}


def allow(names, **changes):
    return {"names":names, "action":"SCMP_ACT_ALLOW", "args":[], **changes}


def names(profile):
    return {name for rule in profile["syscalls"] for name in rule["names"] if rule["action"] == "SCMP_ACT_ALLOW"}


def test_real_profile_is_reproducible_oci_json_without_docker_conditions():
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    profile = build_profile(source)
    assert OUTPUT.read_bytes() == encoded_profile(profile)
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["architectures"] == ["SCMP_ARCH_X86_64"]
    assert set(profile) == {"defaultAction","architectures","syscalls"}
    assert all(set(rule) <= {"names","action","args","errnoRet"} for rule in profile["syscalls"])
    assert {"clone","setns","unshare","chroot","read","write","execve","arch_prctl"} <= names(profile)
    assert not names(profile) & BLOCKED_SYSCALLS


def test_capability_filters_are_resolved_for_empty_capability_set():
    profile = build_profile(fixture(
        allow(["read"]), allow(["mount"],includes={"caps":["CAP_SYS_ADMIN"]}),
        allow(["write"],excludes={"caps":["CAP_SYS_ADMIN"]}),
        allow(["reboot"],includes={"caps":["CAP_SYS_BOOT"]})))
    assert names(profile) == {"read","write"}


def test_architecture_filters_are_resolved_for_native_amd64_only():
    profile = build_profile(fixture(
        allow(["arch_prctl"],includes={"arches":["amd64","x32"]}),
        allow(["arm_fadvise64_64"],includes={"arches":["arm","arm64"]}),
        allow(["read"],excludes={"arches":["s390","s390x"]}),
        allow(["write"],excludes={"arches":["amd64"]})))
    assert names(profile) == {"arch_prctl","read"}


def test_argument_limited_baseline_rules_are_preserved_exactly():
    args = [{"index":0,"value":0,"valueTwo":0,"op":"SCMP_CMP_EQ"}]
    profile = build_profile(fixture(allow(["personality"],args=args)))
    assert profile["syscalls"] == [allow(["personality"],args=args)]
    args[0]["value"] = 123
    assert profile["syscalls"][0]["args"][0]["value"] == 0


def test_minimum_kernel_rules_are_not_silently_broadened_without_kernel_audit():
    profile = build_profile(fixture(allow(["example"],includes={"minKernel":"4.8"}), allow(["read"])))
    assert names(profile) == {"read"}


def test_defense_in_depth_removes_elevated_syscalls_even_if_source_is_unconditional():
    profile = build_profile(fixture(allow(["read","mount","bpf","ptrace","keyctl","init_module"])))
    assert names(profile) == {"read"}


@pytest.mark.parametrize("mutate", [
    lambda p:p.update(defaultAction="SCMP_ACT_ALLOW"),
    lambda p:p.update(architectures=["SCMP_ARCH_AARCH64"]),
    lambda p:p["syscalls"][0].update(includes={"unexpectedCondition":True}),
    lambda p:p["syscalls"][0].update(action="SCMP_ACT_LOG"),
    lambda p:p["syscalls"][0].update(unreviewedField=True),
    lambda p:p.update(listenerPath="/host/listener"),
    lambda p:p["syscalls"][0].update(args=[{"index":0,"value":0,"op":"UNREVIEWED"}]),
])
def test_unreviewed_or_unsafe_schema_changes_fail_closed(mutate):
    value = fixture(allow(["read"]))
    mutate(value)
    with pytest.raises(ValueError):
        build_profile(value)


def test_conversion_does_not_modify_docker_profile_or_add_missing_sandbox_permissions():
    value = fixture(allow(["read"]))
    before = deepcopy(value)
    assert names(build_profile(value)) == {"read"}
    assert value == before
