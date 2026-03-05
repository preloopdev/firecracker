# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Firecracker is a Virtual Machine Monitor (VMM) that uses Linux KVM to create and manage lightweight microVMs. It powers AWS Lambda and AWS Fargate. Written in Rust (2024 edition), it targets `x86_64-unknown-linux-musl` with toolchain 1.89.0.

## Build Commands

All build/test commands use `./tools/devtool`, which runs inside a Docker container (image: `public.ecr.aws/firecracker/fcuvm`).

```bash
./tools/devtool build                    # Debug build → build/debug/
./tools/devtool build --release          # Release build → build/release/
./tools/devtool shell                    # Interactive shell in dev container
./tools/devtool -y checkstyle           # Lint: Rust, Python, markdown
./tools/devtool -y fmt                  # Auto-fix formatting
./tools/devtool checkbuild --all        # Verify compilation on all architectures
```

Direct cargo commands (inside dev container or with correct toolchain):
```bash
cargo build                              # Build default workspace members
cargo test                               # Run unit tests
cargo test --test integration_tests --all  # Run Rust integration tests only
cargo clippy --all-targets --all-features  # Run clippy lints
```

Note: `jailer` is excluded from default workspace members because it requires static (musl) compilation.

## Testing

Integration tests use **pytest** (Python). Paths are relative to `tests/`:

```bash
./tools/devtool -y test                  # Run all CI tests
./tools/devtool -y test -- integration_tests/performance/test_boottime.py  # Specific file
./tools/devtool -y test -- integration_tests/performance/test_boottime.py::test_boottime  # Single test
./tools/devtool -y test -- -k 1024 integration_tests/performance/test_boottime.py  # Filter by name
./tools/devtool -y test -- -s            # Show stdout/stderr during tests
```

Test markers: `nonci` (excluded from PR CI), `no_block_pr` (optional). Default timeout: 300s per test.

## Architecture

### Process Model

One Firecracker process = one microVM, with three thread types:
- **API thread**: HTTP server accepting control plane requests (Unix socket at `/run/firecracker.socket`)
- **VMM thread**: Machine model, device emulation, MMDS, VirtIO event loop
- **vCPU thread(s)**: KVM_RUN main loop (one per virtual CPU)

### Workspace Crates (`src/`)

| Crate | Purpose |
|-------|---------|
| `firecracker` | Main binary: CLI parsing, API server, seccomp setup, VMM orchestration |
| `vmm` | Core library: device models, CPU config, memory, snapshotting, arch support |
| `jailer` | Process isolation binary (cgroups, namespaces, privilege dropping) |
| `seccompiler` | Compiles JSON seccomp filter definitions to BPF |
| `cpu-template-helper` | CPU template configuration utility |
| `snapshot-editor` | Snapshot file manipulation tool |
| `rebase-snap` | Snapshot rebasing utility |
| `acpi-tables` | ACPI table generation |
| `clippy-tracing` | Custom clippy lint plugin |
| `utils` | Shared utilities (arg parser, validators, etc.) |
| `pci` | PCI device model |
| `log-instrument` / `log-instrument-macros` | Logging and instrumentation |

### VMM Internal Structure (`src/vmm/src/`)

- `arch/` — Architecture-specific code (x86_64, aarch64): boot params, GDT/IDT, MPTABLE, interrupts
- `devices/virtio/` — VirtIO devices: block, net, balloon, vsock, entropy, pmem
- `devices/legacy/` — Legacy devices: serial, i8042 (keyboard controller)
- `devices/acpi/` — ACPI device emulation
- `devices/pci/` — PCI bus emulation
- `builder.rs` — MicroVM construction and startup (builds the full VM from config)
- `rpc_interface.rs` — API server RPC handlers (translates HTTP requests into VMM actions)
- `device_manager/` — Device lifecycle: creation, attachment, hot-plug
- `persist.rs` — Snapshot save/restore logic
- `vstate/` — Virtual CPU state management, KVM interactions
- `cpu_config/` — CPU template system (static, custom, or no templates)
- `mmds/` — MicroVM Metadata Service (guest-accessible metadata, like EC2 instance metadata)
- `rate_limiter/` — Token-bucket rate limiting for I/O
- `io_uring/` — Async I/O via Linux io_uring (requires kernel >= 5.10.51)
- `dumbo/` — Minimalist HTTP/TCP/IPv4 stack for MMDS
- `seccomp/` — Per-thread seccomp BPF filter application

### Configuration & Resources

- `resources/seccomp/` — Seccomp filter definitions (JSON, per-architecture)
- `.cargo/config.toml` — Cargo build config: target dir `build/cargo_target`, single codegen unit
- `rust-toolchain.toml` — Pinned toolchain (important for seccomp filter A/B testing across versions)

## Code Conventions

- **`unsafe` is heavily discouraged.** When required, must include both a JUSTIFICATION comment (why safe alternatives weren't used) and a SAFETY comment (proving invariants are upheld, per `clippy::undocumented_unsafe_blocks`).
- **Avoid `.unwrap()`** — prefer error propagation with `?`, or use `.expect()` with an explanatory comment. Rewrite with `.map`/`.map_err` where possible.
- **Commits require DCO sign-off**: `git commit -s` (Signed-off-by line).
- **Workspace clippy lints** are configured in root `Cargo.toml` — includes warnings for unsafe blocks, cast operations, `exit` calls, and more.
- **Panic mode is "abort"** in both dev and release profiles.
- **Release builds use LTO** with a single codegen unit for deterministic output.

## CI System

Primary CI: **Buildkite** (pipeline definitions in `.buildkite/`). PR pipeline runs:
1. Style checks (`checkstyle`)
2. Build verification
3. Kani formal verification (optional, long-running)
4. Build, functional, security, and performance tests
5. Tests run on multiple architectures (x86_64, aarch64) and instance types

GitHub Actions handle supplementary checks: Cargo.lock freshness, dependency modification review, A/B test triggering on Rust code changes.

## Remote Testing on edge-dev

The local machine lacks permissions to run devtool containers. Tests must be run on a remote machine accessible via `ssh edge-dev`.

### Remote machine setup

- Repo location: `/home/dsec/test/fc/firecracker` (checked out at `v1.14.2` tag on `main` branch)
- Has Docker and AWS CLI installed (required by devtool for downloading test artifacts)
- devtool requires `sudo`

### Workflow

1. **Create a patch** from local changes (against the matching base):
   ```bash
   cd /weka-hg/prod/deepseek/permanent/huangjialiang/firecracker
   git diff v1.14.2 > /tmp/my-changes.patch
   ```

2. **Copy the patch to remote**:
   ```bash
   scp /tmp/my-changes.patch edge-dev:/home/dsec/test/fc/firecracker/
   ```

3. **Apply the patch on remote**:
   ```bash
   ssh edge-dev "cd /home/dsec/test/fc/firecracker && git checkout . && git apply my-changes.patch"
   ```
   Use `git checkout .` first to reset any previously applied patches.

4. **Run tests on remote**:
   ```bash
   ssh edge-dev "cd /home/dsec/test/fc/firecracker && sudo ./tools/devtool -y test"
   ```
   This runs the full test suite: clippy, unit tests, integration tests, and doc tests inside a Docker container. It takes ~16 minutes.

   To run a subset:
   ```bash
   ssh edge-dev "cd /home/dsec/test/fc/firecracker && sudo ./tools/devtool -y test -- integration_tests/build/"
   ```

### Known pre-existing test failures

These failures exist on the base `v1.14.2` and are unrelated to local changes:
- **`cpu-template-helper`**: binary test fails with error code 101
- **`test_coverage`**: fails with `BUILDKITE: unbound variable` (CI environment variable not set locally)
