# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Firecracker is a Virtual Machine Monitor (VMM) that uses Linux KVM to create and manage lightweight microVMs. Written in Rust (2024 edition), it targets `x86_64-unknown-linux-musl` with toolchain 1.89.0.

Key crates: `firecracker` (main binary + API server), `vmm` (core library), `jailer` (process isolation, requires musl). Other crates are utilities — see `Cargo.toml` workspace members.

## Build & Test

All build/test commands use `./tools/devtool` (runs inside Docker container `public.ecr.aws/firecracker/fcuvm`). The local machine **cannot** compile or run tests — must use remote (see below).

```bash
./tools/devtool build                    # Debug build
./tools/devtool build --release          # Release build
./tools/devtool -y test                  # Full test suite (~16 min)
./tools/devtool -y test -- integration_tests/build/  # Subset
./tools/devtool -y checkstyle           # Lint
```

For faster unit test iteration, run cargo directly inside the Docker container:
```bash
sudo docker run --rm -v $(pwd):/firecracker -w /firecracker --privileged \
  public.ecr.aws/firecracker/fcuvm:v85 bash -c \
  "cargo test -p vmm --lib test_name -- --nocapture"
```

CI uses Buildkite (`.buildkite/`) and GitHub Actions for supplementary checks.

## Architecture

### Process & Thread Model

One Firecracker process = one microVM, with three thread types:
- **API thread**: HTTP server on Unix socket, receives REST requests
- **VMM thread**: Machine model, device emulation, VirtIO event loop
- **vCPU thread(s)**: KVM_RUN main loop (one per virtual CPU)

### API Request Flow (critical for adding new endpoints)

HTTP request → `ParsedRequest::try_from()` (routing in `src/firecracker/src/api_server/parsed_request.rs`)
→ `VmmAction` enum variant (defined in `src/vmm/src/rpc_interface.rs`)
→ sent over mpsc channel to VMM thread
→ `RuntimeApiController::handle_request()` dispatches to handler method
→ returns `Result<VmmData, VmmActionError>` → serialized to HTTP response

**To add a new endpoint:**
1. Add OpenAPI spec in `src/firecracker/swagger/firecracker.yaml` (Swagger 2.0 format — uses `definitions`, not `components/schemas`)
2. Add `VmmAction` variant and `VmmData` variant in `rpc_interface.rs`
3. Add routing in `parsed_request.rs` (`ParsedRequest::try_from`)
4. Add response conversion in `parsed_request.rs` (`convert_to_response`)
5. Add handler method on `RuntimeApiController`
6. Add the action to `PrebootApiController`'s disallowed list if it's post-boot only
7. Tests: routing test, response test, preboot rejection test, handler test

### Pre-boot vs Post-boot API

`PrebootApiController` handles requests before VM boot (config, boot-source, drives, etc.).
`RuntimeApiController` handles requests after boot (pause, resume, snapshot, metrics, etc.).
New runtime-only actions **must** be added to the disallowed operations match arm in `PrebootApiController::handle_preboot_request()`, or you'll get a non-exhaustive pattern error.

### Key VMM Files (`src/vmm/src/`)

- `builder.rs` — MicroVM construction from config; `default_vmm()` test helper (128 MiB, 1 vCPU)
- `rpc_interface.rs` — API dispatch; test helpers: `runtime_request()`, `preboot_request()`
- `persist.rs` — Snapshot save/restore, uffd handshake, `GuestRegionUffdMapping`, `build_uffd_mappings()`
- `vstate/memory.rs` — Guest memory types and traits
- `vstate/vm.rs` — KVM VM wrapper, `guest_memory()` accessor
- `device_manager/` — Device lifecycle, hot-plug
- `devices/virtio/` — VirtIO devices: block, net, balloon, vsock, entropy, pmem
- `arch/` — Architecture-specific: boot params, GDT/IDT, MPTABLE, interrupts

### Guest Memory Type Hierarchy

```
GuestMemoryMmap = GuestRegionCollection<GuestRegionMmapExt>
  └─ iter() yields &GuestRegionMmapExt
       ├─ implements GuestMemoryRegion (start_addr, len)
       ├─ Deref<Target = MmapRegion> (as_ptr, size)
       └─ field: inner: GuestRegionMmap
            └─ also Deref<Target = MmapRegion>
```

Key traits to import: `GuestMemory` (for `.iter()`, `.num_regions()`), `GuestMemoryRegion` (for `.start_addr()`, `.len()`). Methods like `.as_ptr()` and `.size()` come from `MmapRegion` via `Deref`, not from traits.

### Guest Memory Gotchas

- **HugePageConfig is config-only**: Page size is NOT stored on `MmapRegion` or `GuestRegionMmap`. The only source of truth is `vm_resources.machine_config.huge_pages`. This is by design.
- **GuestRegionUffdMapping.offset**: This is a cumulative byte offset into a contiguous layout of all regions (snapshot file position), **NOT** the Guest Physical Address. GPAs have gaps (MMIO hole near 4 GiB), but the snapshot layout is contiguous. See `build_uffd_mappings()` in `persist.rs`.
- **Shared mapping logic**: `build_uffd_mappings()` in `persist.rs` is the single source of truth for constructing `GuestRegionUffdMapping` lists — used by both the uffd restore path and the runtime API.

## Code Conventions

- **`unsafe` is heavily discouraged.** Requires JUSTIFICATION + SAFETY comments.
- **Avoid `.unwrap()`** — use `?` or `.expect("reason")`.
- **Commits require DCO sign-off**: `git commit -s`.
- **Panic mode is "abort"** in both dev and release profiles.
- Workspace clippy lints configured in root `Cargo.toml`.

## Remote Testing on edge-dev

The local machine cannot run devtool. Tests run on `ssh edge-dev`.

- Remote repo: `/home/dsec/test/fc/firecracker` (at `v1.14.2` tag)
- Docker + AWS CLI installed; devtool requires `sudo`

### Workflow

```bash
# 1. Generate patch (only for files in your feature!)
git diff v1.14.2 -- src/file1.rs src/file2.rs > /tmp/my-changes.patch

# 2. Copy and apply
scp /tmp/my-changes.patch edge-dev:/home/dsec/test/fc/firecracker/
ssh edge-dev "cd /home/dsec/test/fc/firecracker && git checkout . && git apply my-changes.patch"

# 3. Run tests
ssh edge-dev "cd /home/dsec/test/fc/firecracker && sudo ./tools/devtool -y test"
```

**Caveat**: If the local branch has multiple features, `git diff v1.14.2` includes ALL of them. Scope the diff to specific files (`git diff v1.14.2 -- path/to/files`) to isolate per-feature patches, or the remote will fail to compile due to missing cross-feature dependencies.

### Known pre-existing test failures

- **`cpu-template-helper`**: binary test fails with error code 101
- **`test_coverage`**: fails with `BUILDKITE: unbound variable` (CI env not set locally)
