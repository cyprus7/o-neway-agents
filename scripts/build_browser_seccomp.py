"""Resolve the reviewed Docker profile into a stricter native-amd64 OCI profile.

Target: one non-root Chromium worker, all Linux capabilities dropped. This is
not a general converter and must not be used for another architecture/capset.
Docker's includes/excludes/archMap are not OCI rules. Containerd unmarshals
Localhost profiles directly into OCI LinuxSeccomp, so copying the Docker file
would lose its conditional-capability restrictions:
https://github.com/containerd/containerd/blob/main/contrib/seccomp/seccomp.go
https://github.com/moby/profiles/blob/main/seccomp/seccomp.go
https://kubernetes.io/docs/reference/node/seccomp/

Playwright documents clone/setns/unshare for Chromium's user-namespace sandbox:
https://playwright.dev/docs/docker#crawling-and-scraping
They and the locally validated chroot permission are already explicit in the
reviewed source; this converter never injects additional syscall permissions.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "infra/price-worker/seccomp.json"
OUTPUT = ROOT / "infra/price-worker/seccomp-kubernetes.json"
ARCHITECTURE = "SCMP_ARCH_X86_64"
DOCKER_ARCHITECTURE = "amd64"
# No mount, tracing, host control, keyring, module or BPF privileges are needed
# by this worker. In particular the old Docker minKernel ptrace rule is omitted
# deliberately even on modern kernels; do not turn it into an OCI blanket allow.
BLOCKED_SYSCALLS = frozenset({
    "mount", "umount", "umount2", "pivot_root", "fsopen", "fsconfig", "fsmount",
    "move_mount", "open_tree", "mount_setattr", "bpf", "perf_event_open", "ptrace",
    "process_vm_readv", "process_vm_writev", "kcmp", "keyctl", "add_key", "request_key",
    "open_by_handle_at", "name_to_handle_at", "init_module", "finit_module", "delete_module",
    "reboot", "kexec_load", "kexec_file_load", "iopl", "ioperm", "swapon", "swapoff",
    "settimeofday", "clock_settime", "setdomainname", "sethostname", "syslog",
})
ARGUMENT_OPERATORS = frozenset({"SCMP_CMP_NE", "SCMP_CMP_LT", "SCMP_CMP_LE", "SCMP_CMP_EQ",
                                "SCMP_CMP_GE", "SCMP_CMP_GT", "SCMP_CMP_MASKED_EQ"})


def _filter(value: dict | None) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"arches", "caps", "minKernel"}:
        raise ValueError("Unreviewed Docker seccomp condition")
    for key in ("arches", "caps"):
        if key in value and (not isinstance(value[key], list) or any(not isinstance(row, str) for row in value[key])):
            raise ValueError("Invalid Docker seccomp condition list")
    if value.get("minKernel") and not re.fullmatch(r"[0-9]+\.[0-9]+", str(value["minKernel"])):
        raise ValueError("Invalid minimum-kernel condition")
    return value


def _applies(rule: dict) -> bool:
    includes, excludes = _filter(rule.get("includes")), _filter(rule.get("excludes"))
    # A version-sensitive allow is omitted rather than guessed for future nodes.
    # No retained rule in the reviewed profile needs a kernel-dependent allowance.
    if includes.get("minKernel") or excludes.get("minKernel"):
        return False
    if includes.get("caps"):
        return False  # The worker's permitted, effective and bounding sets are empty.
    if includes.get("arches") and DOCKER_ARCHITECTURE not in includes["arches"]:
        return False
    if DOCKER_ARCHITECTURE in excludes.get("arches", []):
        return False
    return True  # None of the excluded capabilities exist in the empty target capset.


def _arguments(value: list | None) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Invalid syscall arguments")
    for argument in value:
        if not isinstance(argument, dict) or set(argument) - {"index", "value", "valueTwo", "op"}:
            raise ValueError("Unreviewed syscall argument")
        if argument.get("op") not in ARGUMENT_OPERATORS or type(argument.get("index")) is not int or not 0 <= argument["index"] <= 5:
            raise ValueError("Invalid syscall argument operator/index")
        for key in ("value", "valueTwo"):
            if key in argument and (type(argument[key]) is not int or not 0 <= argument[key] < 2**64):
                raise ValueError("Invalid syscall argument value")
        if "value" not in argument:
            raise ValueError("Missing syscall argument value")
    return deepcopy(value)


def build_profile(source: dict) -> dict:
    if not isinstance(source, dict) or set(source) - {"defaultAction", "defaultErrnoRet", "archMap", "architectures", "syscalls"}:
        raise ValueError("Unreviewed Docker profile fields")
    if source.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise ValueError("The profile must deny unknown syscalls")
    if source.get("architectures") not in (None, [ARCHITECTURE]):
        raise ValueError("Only the audited native amd64 target is supported")
    arch_map = source.get("archMap", [])
    if not isinstance(arch_map, list) or not any(isinstance(row, dict) and row.get("architecture") == ARCHITECTURE for row in arch_map):
        raise ValueError("Source does not describe the audited amd64 architecture")
    rules = source.get("syscalls")
    if not isinstance(rules, list):
        raise ValueError("Missing syscall rules")
    output = {"defaultAction":"SCMP_ACT_ERRNO", "architectures":[ARCHITECTURE], "syscalls":[]}
    if "defaultErrnoRet" in source:
        if type(source["defaultErrnoRet"]) is not int or not 1 <= source["defaultErrnoRet"] <= 4095:
            raise ValueError("Invalid default errno")
        output["defaultErrnoRet"] = source["defaultErrnoRet"]
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"names", "action", "args", "errnoRet", "includes", "excludes", "comment"}:
            raise ValueError("Unreviewed Docker syscall fields")
        if rule.get("action") not in ("SCMP_ACT_ALLOW", "SCMP_ACT_ERRNO"):
            raise ValueError("Only reviewed allow/errno rules are supported")
        names = rule.get("names")
        if not isinstance(names, list) or not names or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name) for name in names):
            raise ValueError("Invalid syscall names")
        arguments = _arguments(rule.get("args", []))
        if not _applies(rule):
            continue
        # Additional hardening is a subset of the resolved source permissions.
        names = [name for name in names if name not in BLOCKED_SYSCALLS or rule["action"] != "SCMP_ACT_ALLOW"]
        if not names:
            continue
        resolved = {"names":names, "action":rule["action"], "args":arguments}
        if "errnoRet" in rule:
            if type(rule["errnoRet"]) is not int or not 1 <= rule["errnoRet"] <= 4095:
                raise ValueError("Invalid syscall errno")
            resolved["errnoRet"] = rule["errnoRet"]
        output["syscalls"].append(resolved)
    return output


def encoded_profile(profile: dict) -> bytes:
    return (json.dumps(profile, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the committed OCI profile differs")
    args = parser.parse_args()
    expected = encoded_profile(build_profile(json.loads(SOURCE.read_text(encoding="utf-8"))))
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != expected:
            raise SystemExit("OCI browser profile differs; regenerate and review before deployment")
    else:
        OUTPUT.write_bytes(expected)
    print(f"{OUTPUT.relative_to(ROOT).as_posix()} sha256:{hashlib.sha256(expected).hexdigest()}")


if __name__ == "__main__":
    main()
