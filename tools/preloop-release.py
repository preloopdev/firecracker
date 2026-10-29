#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Package and index the Preloop Firecracker runtime for every declared architecture.

Every subcommand requires a clean checkout whose HEAD is --source-commit. That commit
must be reachable from refs/remotes/origin/<source_branch> and must descend from the
pinned upstream release. The patch-to-port map in the contract is checked against
the checkout.

  package --arch ARCH --binary-dir DIR --source-commit SHA --builder-toolchain FILE --output-dir DIR
      Inside GitHub Actions only. DIR must be target/<target>/release of this checkout,
      built by the same job. Each binary must run natively to report its version.
  package --arch ARCH --binary-dir DIR --source-commit SHA --build-record FILE --output-dir DIR
      Outside Actions only. FILE is the builder's record. Its binary digests must match
      DIR exactly, and its build commands must include a --frozen --release build of the
      contract's packages for the architecture's target. The output is marked
      local-build-record and has no GitHub attestation.
  index --input-dir DIR --source-commit SHA [--allow-missing-architectures] --output-dir DIR
      Cross-checks each architecture's archive against its provenance, then writes
      provenance.json and SHA256SUMS.
  pins --release-json FILE --release-dir DIR --tag-commit SHA --source-commit SHA --output FILE
      Inside Actions only. Records the prerelease, tag, and asset identifiers.

Nothing here builds, publishes, or deploys, and nothing here records a test result.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "resources" / "preloop-runtime.json"
SCHEMA_VERSION = 4
CHECKSUMS = "SHA256SUMS"
BUILD_ORIGIN_ACTIONS = "github-actions"
BUILD_ORIGIN_RECORD = "local-build-record"
ELF_MAGIC = b"\x7fELF"
ET_EXEC = 2
ET_DYN = 3
COMMIT = re.compile(r"[0-9a-f]{40}")
EXACT_RELEASE = re.compile(r"\d+\.\d+\.\d+")
RUSTC_RELEASE_LINE = re.compile(r"^release: (\S+)$", re.MULTILINE)
RUSTC_IDENTITY = re.compile(rb"rustc version (\d+\.\d+\.\d+)")
SNAPSHOT_DEFINITION = re.compile(r"SNAPSHOT_VERSION: Version = Version::new\((\d+), (\d+), (\d+)\);")
SWAGGER_VERSION = re.compile(r"^  version: (\S+)$", re.MULTILINE)


class PackagingError(RuntimeError):
    """A release precondition failed. Nothing partial is published."""


def fail(message: str) -> None:
    raise PackagingError(message)


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        fail(f"{name} must be set inside GitHub Actions")
    return value


def run_bytes(command: list[str], data: bytes | None = None) -> bytes:
    result = subprocess.run(command, cwd=ROOT, input=data, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        fail(f"{command[0]} exited {result.returncode}: {detail}")
    return result.stdout


def git_bytes(*args: str, data: bytes | None = None) -> bytes:
    return run_bytes(["git", *args], data)


def git(*args: str) -> str:
    return git_bytes(*args).decode().strip()


def is_ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=ROOT,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        detail = result.stderr.decode(errors="replace").strip()
        fail(f"cannot compare {ancestor} with {descendant}: {detail}")
    return result.returncode == 0


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_contract() -> dict:
    contract = json.loads(CONTRACT_PATH.read_text())
    if contract.get("schema_version") != SCHEMA_VERSION:
        fail(f"{CONTRACT_PATH.name} must declare schema_version {SCHEMA_VERSION}")
    architectures = contract["architectures"]
    if sorted(architectures) != ["aarch64", "x86_64"]:
        fail("the contract must declare exactly the x86_64 and aarch64 architectures")
    for arch, spec in architectures.items():
        if spec["target"] != f"{arch}-unknown-linux-musl":
            fail(f"{arch} must build for {arch}-unknown-linux-musl")
        if not (ROOT / spec["seccomp"]).is_file():
            fail(f"missing seccomp filter {spec['seccomp']}")
    if sorted(contract["build_packages"]) != sorted(contract["binaries"]):
        fail("build_packages must name exactly the shipped binaries")
    for name, reason in contract["omitted_binaries"].items():
        if name in contract["binaries"] or not reason.strip():
            fail(f"omitted binary {name} must not be shipped and must state a reason")
    version = contract["runtime_version"]
    cargo_version = tomllib.loads((ROOT / "src/firecracker/Cargo.toml").read_text())["package"]["version"]
    if version != f"v{cargo_version}":
        fail(f"runtime_version {version} does not match the Cargo version {cargo_version}")
    swagger = SWAGGER_VERSION.search((ROOT / "src/firecracker/swagger/firecracker.yaml").read_text())
    if swagger is None or swagger.group(1) != cargo_version:
        fail("the swagger version does not match the runtime version")
    snapshot = SNAPSHOT_DEFINITION.search((ROOT / "src/vmm/src/persist.rs").read_text())
    if snapshot is None or ".".join(snapshot.groups()) != contract["snapshot_version"]:
        fail("persist.rs SNAPSHOT_VERSION does not match the contract")
    for relative in contract["release_inputs"]:
        if not (ROOT / relative).is_file():
            fail(f"missing release input {relative}")
    for template in contract["cpu_templates"]:
        if not (ROOT / "tests/data/custom_cpu_templates" / f"{template}.json").is_file():
            fail(f"missing CPU template {template}")
    return contract


def pinned_channel() -> str:
    channel = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    if not EXACT_RELEASE.fullmatch(channel):
        fail("rust-toolchain.toml must pin an exact release such as 1.97.0")
    return channel


def require_release(text: str, channel: str, source: str) -> None:
    releases = RUSTC_RELEASE_LINE.findall(text)
    if releases != [channel]:
        fail(f"{source} reports rustc releases {releases}; expected exactly [{channel}]")


def require_repository(contract: dict) -> None:
    if os.environ.get("GITHUB_REPOSITORY") != contract["source_repository"]:
        fail(f"GITHUB_REPOSITORY must be {contract['source_repository']}")


def check_source(contract: dict, source_commit: str) -> str:
    if not COMMIT.fullmatch(source_commit):
        fail("--source-commit must be a full 40-character lowercase SHA")
    if git("status", "--porcelain", "--", ".", ":(exclude)target"):
        fail("the source checkout must be clean outside target/")
    head = git("rev-parse", "HEAD")
    if head != source_commit:
        fail(f"HEAD {head} is not the requested source commit {source_commit}")
    branch = f"refs/remotes/origin/{contract['source_branch']}"
    if not is_ancestor(head, branch):
        fail(f"{head} is not reachable from {branch}")
    if not is_ancestor(contract["upstream_commit"], head):
        fail(f"{head} does not descend from upstream {contract['upstream_commit']}")
    return head


def patch_id(commit: str) -> str:
    diff = git_bytes("show", "--format=", commit)
    identity = git_bytes("patch-id", "--stable", data=diff).decode().split()
    if not identity:
        fail(f"{commit} has no patch content")
    return identity[0]


def changed_files(commit: str) -> list[str]:
    return git("diff-tree", "--no-commit-id", "--name-only", "-r", commit).split()


def check_patch_port(contract: dict, head: str) -> None:
    upstream = contract["upstream_commit"]
    for patch in contract["patch_commits"]:
        port = patch["port_commit"]
        if not is_ancestor(port, head):
            fail(f"port commit {port} for {patch['commit']} is not in HEAD's history")
        if not is_ancestor(upstream, port):
            fail(f"port commit {port} does not descend from upstream {upstream}")
        if patch["status"] == "identical":
            if patch_id(port) != patch["patch_id"]:
                fail(f"port commit {port} is not patch-id identical to {patch['commit']}")
        elif patch["status"] == "adapted":
            if not set(patch["files"]) & set(changed_files(port)):
                fail(f"port commit {port} changes none of the files changed by {patch['commit']}")
        else:
            fail(f"patch {patch['commit']} has unknown status {patch['status']!r}")


def prepare_output(path: Path) -> Path:
    output = path.resolve()
    if output.exists() and any(output.iterdir()):
        fail(f"output directory {output} must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)
    return output


def check_elf(path: Path, machine: int) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
    if len(header) != 64 or header[:4] != ELF_MAGIC or header[4] != 2 or header[5] != 1:
        fail(f"{path.name} is not a little-endian ELF64 image")
    e_type, e_machine = struct.unpack_from("<HH", header, 16)
    if e_type not in (ET_EXEC, ET_DYN) or e_machine != machine:
        fail(f"{path.name} has e_type {e_type} and e_machine {e_machine}; expected machine {machine}")
    headers = run_bytes(["readelf", "--program-headers", "--wide", str(path)]).decode(errors="replace")
    if any(line.split()[:1] == ["INTERP"] for line in headers.splitlines()):
        fail(f"{path.name} is dynamically linked")


def rustc_identities(path: Path) -> list[str]:
    found = {match.decode() for match in RUSTC_IDENTITY.findall(path.read_bytes())}
    if not found:
        fail(f"{path.name} carries no rustc identification string")
    return sorted(found)


def check_version(path: Path, arch: str, version: str, native_required: bool) -> dict:
    if platform.machine() == arch:
        lines = run_bytes([str(path), "--version"]).decode(errors="replace").splitlines()
        line = lines[0] if lines else ""
        if not re.search(rf" {re.escape(version)}$", line):
            fail(f"{path.name} reports {line!r}; expected {version}")
        return {"method": "native-exec", "executed": True, "version_line": line}
    if native_required:
        fail(f"{path.name} must be executed on a {arch} host to check its version")
    needle = version[1:].encode()
    if needle not in path.read_bytes():
        fail(f"{path.name} does not embed the runtime version {version[1:]}")
    return {"method": "embedded-version-string", "executed": False}


def actions_builder(channel: str, toolchain_text: str, target: str, packages: list[str]) -> dict:
    package_flags = " ".join(f"-p {package}" for package in packages)
    return {
        "repository": require_env("GITHUB_REPOSITORY"),
        "workflow_ref": require_env("GITHUB_WORKFLOW_REF"),
        "run_id": require_env("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "job": require_env("GITHUB_JOB"),
        "event": require_env("GITHUB_EVENT_NAME"),
        "runner_os": os.environ.get("RUNNER_OS", ""),
        "runner_arch": os.environ.get("RUNNER_ARCH", ""),
        "host_machine": platform.machine(),
        "rust_channel": channel,
        "rustc_verbose": toolchain_text,
        "build_commands": [
            f"cargo +{channel} fetch --locked --target {target}",
            f"cargo +{channel} build --frozen --release --target {target} {package_flags}",
        ],
        "cargo_lock_sha256": sha256_file(ROOT / "Cargo.lock"),
    }


def verify_build_record(
    record: dict, contract: dict, arch: str, spec: dict, head: str, channel: str
) -> tuple[dict, dict]:
    if record.get("schema_version") != SCHEMA_VERSION or record.get("build_origin") != BUILD_ORIGIN_RECORD:
        fail(f"the build record must declare schema_version {SCHEMA_VERSION} and build_origin {BUILD_ORIGIN_RECORD}")
    cargo_lock = sha256_file(ROOT / "Cargo.lock")
    expected = {
        "source_commit": head,
        "arch": arch,
        "target": spec["target"],
        "cargo_lock_sha256": cargo_lock,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            fail(f"build record {key} is {record.get(key)!r}; expected {value!r}")
    require_release(str(record.get("rustc_verbose", "")), channel, "the build record")
    for key in ("builder_host", "build_commands"):
        if not record.get(key):
            fail(f"build record lacks {key}")
    builds = [command for command in record["build_commands"] if " build " in f" {command} "]
    required = [f"-p {package}" for package in contract["build_packages"]]
    if not any(
        "--frozen" in command
        and "--release" in command
        and spec["target"] in command
        and all(flag in command for flag in required)
        for command in builds
    ):
        fail("the build record must show a --frozen --release build of the contract's packages for the target")
    digests = record.get("binary_sha256", {})
    if sorted(digests) != sorted(contract["binaries"]):
        fail("the build record must list exactly the contract's shipped binaries")
    builder = {
        "host": record["builder_host"],
        "rust_channel": channel,
        "rustc_verbose": record["rustc_verbose"],
        "build_commands": record["build_commands"],
        "cargo_lock_sha256": cargo_lock,
    }
    return builder, digests


def expected_members(contract: dict, arch: str, binary_files: list[str]) -> set[str]:
    version = contract["runtime_version"]
    names = set(binary_files)
    names.update({"LICENSE", "NOTICE", "THIRD-PARTY", "preloop-runtime-contract.json"})
    names.add(f"firecracker_spec-{version}.yaml")
    names.add(f"seccomp-filter-{version}-{arch}.json")
    names.update(f"{template}-{version}.json" for template in contract["cpu_templates"])
    root = f"release-{version}-{arch}"
    return {f"{root}/{name}" for name in names}


def stage_release_files(contract: dict, arch: str, tree: Path) -> None:
    version = contract["runtime_version"]
    spec = contract["architectures"][arch]
    for name in ("LICENSE", "NOTICE", "THIRD-PARTY"):
        shutil.copy2(ROOT / name, tree / name)
    shutil.copy2(ROOT / "src/firecracker/swagger/firecracker.yaml", tree / f"firecracker_spec-{version}.yaml")
    shutil.copy2(ROOT / spec["seccomp"], tree / f"seccomp-filter-{version}-{arch}.json")
    shutil.copy2(CONTRACT_PATH, tree / "preloop-runtime-contract.json")
    for template in contract["cpu_templates"]:
        shutil.copy2(
            ROOT / "tests/data/custom_cpu_templates" / f"{template}.json",
            tree / f"{template}-{version}.json",
        )


def write_archive(tree: Path, archive: Path) -> None:
    root = tree.name

    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    with archive.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed:
        with tarfile.open(fileobj=packed, mode="w") as tar:
            tar.add(tree, arcname=root, recursive=False, filter=normalize)
            for entry in sorted(tree.iterdir()):
                tar.add(entry, arcname=f"{root}/{entry.name}", recursive=False, filter=normalize)


def verify_archive(archive: Path, contract: dict, arch: str, binaries: dict) -> None:
    version = contract["runtime_version"]
    root = f"release-{version}-{arch}"
    seen: dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/")
            if member.name.startswith("/") or ".." in parts:
                fail(f"{arch}: unsafe archive member {member.name}")
            if member.isdir():
                if member.name != root:
                    fail(f"{arch}: unexpected archive directory {member.name}")
                continue
            if not member.isfile():
                fail(f"{arch}: archive member {member.name} is not a regular file")
            extracted = tar.extractfile(member)
            if extracted is None:
                fail(f"{arch}: cannot read archive member {member.name}")
            seen[member.name] = sha256_bytes(extracted.read())
    expected = expected_members(contract, arch, [info["file"] for info in binaries.values()])
    if set(seen) != expected:
        extra = sorted(set(seen) - expected)
        missing = sorted(expected - set(seen))
        fail(f"{arch}: archive members differ from the release layout (extra {extra}, missing {missing})")
    for name, info in binaries.items():
        if seen[f"{root}/{info['file']}"] != info["sha256"]:
            fail(f"{arch}: archive binary {info['file']} differs from its provenance")


def verify_record(
    record: dict, contract: dict, arch: str, spec: dict, head: str, channel: str, archive: Path
) -> None:
    version = contract["runtime_version"]
    if record.get("schema_version") != SCHEMA_VERSION:
        fail(f"{arch}: provenance schema_version must be {SCHEMA_VERSION}")
    if record.get("build_origin") not in (BUILD_ORIGIN_ACTIONS, BUILD_ORIGIN_RECORD):
        fail(f"{arch}: unknown build_origin {record.get('build_origin')!r}")
    expected = {
        "runtime_version": version,
        "arch": arch,
        "target": spec["target"],
        "elf_machine": spec["elf_machine"],
        "source_commit": head,
        "source_branch": contract["source_branch"],
        "snapshot_version": contract["snapshot_version"],
        "contract_sha256": sha256_file(CONTRACT_PATH),
        "artifact": archive.name,
        "artifact_sha256": sha256_file(archive),
    }
    for key, value in expected.items():
        if record.get(key) != value:
            fail(f"{arch}: provenance {key} is {record.get(key)!r}; expected {value!r}")
    if record.get("build_packages") != contract["build_packages"]:
        fail(f"{arch}: provenance build_packages differ from the contract")
    if record.get("omitted_binaries") != contract["omitted_binaries"]:
        fail(f"{arch}: provenance omitted_binaries differ from the contract")
    if record.get("builder", {}).get("rust_channel") != channel:
        fail(f"{arch}: the builder did not use the pinned Rust {channel}")
    binaries = record.get("binaries", {})
    if sorted(binaries) != sorted(contract["binaries"]):
        fail(f"{arch}: provenance must list exactly the contract's shipped binaries")
    for name, info in binaries.items():
        if info.get("file") != f"{name}-{version}-{arch}":
            fail(f"{arch}: {name} is recorded under an unexpected file name")
        if info.get("rustc_identities") != [channel]:
            fail(f"{arch}: {name} records rustc identities {info.get('rustc_identities')}")
    if record.get("binary_sha256") != {name: info["sha256"] for name, info in binaries.items()}:
        fail(f"{arch}: binary_sha256 disagrees with the binaries record")
    verify_archive(archive, contract, arch, binaries)


def write_checksums(directory: Path) -> None:
    lines = [
        f"{sha256_file(path)}  {path.name}\n"
        for path in sorted(directory.iterdir())
        if path.is_file() and path.name != CHECKSUMS
    ]
    (directory / CHECKSUMS).write_text("".join(lines))


def verify_checksums(directory: Path) -> None:
    for line in (directory / CHECKSUMS).read_text().splitlines():
        digest, name = line.split("  ", 1)
        if sha256_file(directory / name) != digest:
            fail(f"checksum mismatch for {name}")


def cmd_package(args: argparse.Namespace) -> None:
    contract = load_contract()
    version = contract["runtime_version"]
    if args.arch not in contract["architectures"]:
        fail(f"{args.arch} is not declared in the contract")
    spec = contract["architectures"][args.arch]
    channel = pinned_channel()
    head = check_source(contract, args.source_commit)
    check_patch_port(contract, head)
    output = prepare_output(args.output_dir)
    binary_dir = (ROOT / args.binary_dir).resolve()
    if in_actions():
        require_repository(contract)
        if args.builder_toolchain is None or args.build_record is not None:
            fail("inside GitHub Actions, package takes --builder-toolchain and no --build-record")
        expected_dir = (ROOT / "target" / spec["target"] / "release").resolve()
        if binary_dir != expected_dir:
            fail(f"inside GitHub Actions, --binary-dir must be {expected_dir}")
        toolchain_text = args.builder_toolchain.read_text()
        require_release(toolchain_text, channel, "the builder toolchain record")
        origin = BUILD_ORIGIN_ACTIONS
        builder = actions_builder(channel, toolchain_text, spec["target"], contract["build_packages"])
        expected_digests = None
        native_required = True
    else:
        if args.build_record is None or args.builder_toolchain is not None:
            fail("outside GitHub Actions, package takes --build-record and no --builder-toolchain")
        origin = BUILD_ORIGIN_RECORD
        builder, expected_digests = verify_build_record(
            json.loads(args.build_record.read_text()), contract, args.arch, spec, head, channel
        )
        native_required = False
    tree_root = f"release-{version}-{args.arch}"
    archive_name = f"firecracker-{version}-{args.arch}.tgz"
    provenance_name = f"provenance-{args.arch}.json"
    with tempfile.TemporaryDirectory(prefix="preloop-package-") as scratch_name:
        scratch = Path(scratch_name)
        tree = scratch / tree_root
        tree.mkdir()
        binaries: dict[str, dict] = {}
        for name in contract["binaries"]:
            source = binary_dir / name
            if not source.is_file():
                fail(f"missing binary {source}")
            check_elf(source, spec["elf_machine"])
            identities = rustc_identities(source)
            if identities != [channel]:
                fail(f"{name} was built by rustc {identities}; expected exactly [{channel}]")
            destination = tree / f"{name}-{version}-{args.arch}"
            shutil.copy2(source, destination)
            digest = sha256_file(destination)
            if expected_digests is not None and expected_digests.get(name) != digest:
                fail(f"{name} digest {digest} does not match the build record")
            binaries[name] = {
                "file": destination.name,
                "sha256": digest,
                "bytes": destination.stat().st_size,
                "rustc_identities": identities,
                "version_check": check_version(destination, args.arch, version, native_required),
            }
        stage_release_files(contract, args.arch, tree)
        archive = scratch / archive_name
        write_archive(tree, archive)
        actual = {f"{tree_root}/{path.name}" for path in tree.iterdir()}
        if actual != expected_members(contract, args.arch, [info["file"] for info in binaries.values()]):
            fail("the staged tree differs from the release layout")
        record = {
            "schema_version": SCHEMA_VERSION,
            "build_origin": origin,
            "runtime_version": version,
            "arch": args.arch,
            "target": spec["target"],
            "elf_machine": spec["elf_machine"],
            "source_repository": contract["source_repository"],
            "source_branch": contract["source_branch"],
            "source_commit": head,
            "source_tag": version,
            "upstream_repository": contract["upstream_repository"],
            "upstream_tag": contract["upstream_tag"],
            "upstream_commit": contract["upstream_commit"],
            "patch_source_repository": contract["patch_source_repository"],
            "patch_source_commit": contract["patch_source_commit"],
            "patch_commits": contract["patch_commits"],
            "build_packages": contract["build_packages"],
            "omitted_binaries": contract["omitted_binaries"],
            "snapshot_version": contract["snapshot_version"],
            "required_features": contract["required_features"],
            "feature_support": contract["feature_support"],
            "contract_sha256": sha256_file(CONTRACT_PATH),
            "artifact": archive_name,
            "artifact_sha256": sha256_file(archive),
            "binary_sha256": {name: info["sha256"] for name, info in binaries.items()},
            "binaries": binaries,
            "builder": builder,
            "runtime_verification": "not performed by packaging",
        }
        (scratch / provenance_name).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        for path in (archive, scratch / provenance_name):
            shutil.copy2(path, output / path.name)
    print(
        json.dumps(
            {
                "arch": args.arch,
                "build_origin": origin,
                "archive": archive_name,
                "archive_sha256": record["artifact_sha256"],
                "binary_sha256": record["binary_sha256"],
                "output": str(output),
            },
            indent=2,
        )
    )


def cmd_index(args: argparse.Namespace) -> None:
    contract = load_contract()
    version = contract["runtime_version"]
    channel = pinned_channel()
    if in_actions():
        require_repository(contract)
    head = check_source(contract, args.source_commit)
    check_patch_port(contract, head)
    input_dir = args.input_dir.resolve()
    output = prepare_output(args.output_dir)
    assets: dict[str, dict] = {}
    missing: list[str] = []
    origins: set[str] = set()
    copies: list[Path] = []
    for arch, spec in contract["architectures"].items():
        archive = input_dir / f"firecracker-{version}-{arch}.tgz"
        record_path = input_dir / f"provenance-{arch}.json"
        if not archive.is_file() or not record_path.is_file():
            missing.append(arch)
            continue
        record = json.loads(record_path.read_text())
        verify_record(record, contract, arch, spec, head, channel, archive)
        origins.add(record["build_origin"])
        assets[arch] = {
            "target": spec["target"],
            "archive": archive.name,
            "archive_sha256": record["artifact_sha256"],
            "provenance": record_path.name,
            "provenance_sha256": sha256_file(record_path),
            "binary_sha256": record["binary_sha256"],
        }
        copies.extend([archive, record_path])
    if not assets:
        fail("no packaged architectures were found")
    if missing and not args.allow_missing_architectures:
        fail(f"missing packages for {missing}; pass --allow-missing-architectures for a partial index")
    if len(origins) != 1:
        fail(f"architectures come from mixed build origins: {sorted(origins)}")
    origin = origins.pop()
    for path in copies:
        shutil.copy2(path, output / path.name)
    index = {
        "schema_version": SCHEMA_VERSION,
        "runtime_version": version,
        "build_origin": origin,
        "complete": not missing,
        "missing_architectures": missing,
        "source_repository": contract["source_repository"],
        "source_branch": contract["source_branch"],
        "source_commit": head,
        "source_tag": version,
        "upstream_repository": contract["upstream_repository"],
        "upstream_tag": contract["upstream_tag"],
        "upstream_commit": contract["upstream_commit"],
        "patch_source_repository": contract["patch_source_repository"],
        "patch_source_commit": contract["patch_source_commit"],
        "build_packages": contract["build_packages"],
        "omitted_binaries": contract["omitted_binaries"],
        "snapshot_version": contract["snapshot_version"],
        "contract_sha256": sha256_file(CONTRACT_PATH),
        "checksums": CHECKSUMS,
        "assets": assets,
    }
    (output / "provenance.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    write_checksums(output)
    verify_checksums(output)
    print(json.dumps({"complete": not missing, "missing_architectures": missing, "build_origin": origin}, indent=2))


def cmd_pins(args: argparse.Namespace) -> None:
    if not in_actions():
        fail("pins runs only inside GitHub Actions")
    contract = load_contract()
    require_repository(contract)
    version = contract["runtime_version"]
    head = check_source(contract, args.source_commit)
    if args.tag_commit != head:
        fail(f"the tag points to {args.tag_commit}, not the source commit {head}")
    release = json.loads(args.release_json.read_text())
    if release.get("tag_name") != version or release.get("draft") or not release.get("prerelease"):
        fail("the release must be a prerelease for the runtime tag")
    local = {path.name: path for path in args.release_dir.iterdir() if path.is_file()}
    remote_names = sorted(asset["name"] for asset in release.get("assets", []))
    if remote_names != sorted(local):
        fail("release assets do not match the packaged files")
    assets = []
    for asset in release["assets"]:
        path = local[asset["name"]]
        if asset["size"] != path.stat().st_size:
            fail(f"size mismatch for {asset['name']}")
        assets.append(
            {
                "name": asset["name"],
                "asset_id": asset["id"],
                "bytes": asset["size"],
                "sha256": sha256_file(path),
            }
        )
    pins = {
        "schema_version": SCHEMA_VERSION,
        "runtime_version": version,
        "tag": version,
        "tag_commit": head,
        "release_id": release["id"],
        "prerelease": True,
        "assets": sorted(assets, key=lambda item: item["name"]),
    }
    args.output.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"release_id": release["id"], "assets": len(assets)}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    package = commands.add_parser("package", help="package one architecture")
    package.add_argument("--arch", required=True)
    package.add_argument("--binary-dir", type=Path, required=True)
    package.add_argument("--source-commit", required=True)
    package.add_argument("--builder-toolchain", type=Path, help="inside Actions: rustc -Vv and cargo -V from the build job")
    package.add_argument("--build-record", type=Path, help="outside Actions: the builder's record with binary digests")
    package.add_argument("--output-dir", type=Path, required=True)
    package.set_defaults(handler=cmd_package)

    index = commands.add_parser("index", help="cross-check packages and write the index")
    index.add_argument("--input-dir", type=Path, required=True)
    index.add_argument("--source-commit", required=True)
    index.add_argument("--allow-missing-architectures", action="store_true")
    index.add_argument("--output-dir", type=Path, required=True)
    index.set_defaults(handler=cmd_index)

    pins = commands.add_parser("pins", help="record prerelease identifiers inside Actions")
    pins.add_argument("--release-json", type=Path, required=True)
    pins.add_argument("--release-dir", type=Path, required=True)
    pins.add_argument("--tag-commit", required=True)
    pins.add_argument("--source-commit", required=True)
    pins.add_argument("--output", type=Path, required=True)
    pins.set_defaults(handler=cmd_pins)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except PackagingError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
