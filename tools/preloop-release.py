#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Package already-built fork binaries; never publish or infer snapshot compatibility."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--validation-evidence", required=True,
                        help="URL to the isolated KVM/lifecycle validation record; not a compatibility assertion")
    args = parser.parse_args()
    evidence = urlparse(args.validation_evidence)
    if evidence.scheme != "https" or not evidence.netloc or evidence.username or evidence.password:
        parser.error("validation evidence must be a credential-free HTTPS URL")
    contract = json.loads((ROOT / "resources/preloop-runtime.json").read_text())
    version = contract["runtime_version"]
    target = contract["target"]
    if target != "x86_64-unknown-linux-musl":
        parser.error("this release packager supports only the x86_64 musl runtime")
    cargo = tomllib.loads((ROOT / "src/firecracker/Cargo.toml").read_text())
    if "v" + cargo["package"]["version"] != version:
        parser.error("runtime contract does not match Firecracker Cargo version")
    if run("git", "status", "--porcelain"):
        parser.error("source tree must be committed and clean before packaging")
    commit = run("git", "rev-parse", "HEAD")
    subprocess.run(["git", "merge-base", "--is-ancestor", contract["upstream_commit"], commit],
                   cwd=ROOT, check=True)
    toolchain = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    rustc = run("rustc", "--version", "--verbose")
    if f"release: {toolchain}" not in rustc.splitlines():
        parser.error("packaging must run with the repository's pinned Rust toolchain")
    release_dir = f"release-{version}-x86_64"
    artifact = f"firecracker-{version}-x86_64.tgz"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destinations = [output / artifact, output / "provenance.json", output / "SHA256SUMS"]
    if any(path.exists() for path in destinations):
        parser.error("release outputs already exist; choose a fresh output directory")
    binaries = {}
    with tempfile.TemporaryDirectory(prefix="preloop-release-") as temporary:
        package = Path(temporary) / release_dir
        package.mkdir()
        for name in ("firecracker", "jailer", "cpu-template-helper"):
            source = args.binary_dir.resolve() / name
            with source.open("rb") as binary:
                header = binary.read(20)
            if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00":
                parser.error(f"{name} is not a little-endian x86_64 ELF binary")
            if "INTERP" in run("readelf", "--program-headers", str(source)):
                parser.error(f"{name} is not statically linked")
            if run(str(source), "--version").splitlines()[0].split()[-1] != version:
                parser.error(f"{name} version does not match {version}")
            destination = package / f"{name}-{version}-x86_64"
            shutil.copy2(source, destination)
            binaries[name] = digest(destination)
        for name in ("LICENSE", "NOTICE", "THIRD-PARTY"):
            shutil.copy2(ROOT / name, package / name)
        shutil.copy2(ROOT / "src/firecracker/swagger/firecracker.yaml", package / f"firecracker_spec-{version}.yaml")
        shutil.copy2(ROOT / f"resources/seccomp/{target}.json", package / f"seccomp-filter-{version}-x86_64.json")
        shutil.copy2(ROOT / "resources/preloop-runtime.json", package / "runtime-contract.json")
        with tarfile.open(destinations[0], "x:gz") as archive:
            archive.add(package, arcname=release_dir)
    provenance = {
        **contract,
        "source_commit": commit,
        "source_tag": version,
        "artifact": artifact,
        "artifact_sha256": digest(destinations[0]),
        "binary_sha256": binaries,
        "rustc": rustc,
        "cargo": run("cargo", "--version"),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "validation_evidence": args.validation_evidence,
    }
    destinations[1].write_text(json.dumps(provenance, indent=2) + "\n")
    destinations[2].write_text("".join(f"{digest(path)}  {path.name}\n" for path in destinations[:2]))
    print(json.dumps({"source_commit": commit, "artifact": str(destinations[0]),
                      "artifact_sha256": provenance["artifact_sha256"]}))


if __name__ == "__main__":
    main()
