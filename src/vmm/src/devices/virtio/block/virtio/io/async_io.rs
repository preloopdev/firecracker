// Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

use std::fmt::Debug;
use std::fs::File;
use std::os::fd::RawFd;
use std::os::unix::io::AsRawFd;

use vm_memory::{GuestMemoryBackend, GuestMemoryError};
use vmm_sys_util::eventfd::EventFd;

use super::{AlignedBuf, DIRECT_IO_ALIGN, RequestError};
use crate::devices::virtio::block::virtio::{IO_URING_NUM_ENTRIES, PendingRequest};
use crate::io_uring::operation::{Cqe, OpCode, Operation};
use crate::io_uring::restriction::Restriction;
use crate::io_uring::{IoUring, IoUringError};
use crate::logger::log_dev_preview_warning;
use crate::vstate::memory::{Bytes, GuestAddress, GuestMemory, GuestMemoryExtension, GuestMemoryMmap};

#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum AsyncIoError {
    /// Not implemented
    NotImplemented,
    /// IO: {0}
    IO(std::io::Error),
    /// IoUring: {0}
    IoUring(IoUringError),
    /// Submit: {0}
    Submit(std::io::Error),
    /// SyncAll: {0}
    SyncAll(std::io::Error),
    /// EventFd: {0}
    EventFd(std::io::Error),
    /// GuestMemory: {0}
    GuestMemory(GuestMemoryError),
    /// BounceBufferAlloc: failed to allocate aligned bounce buffer
    BounceBufferAlloc,
}

#[derive(Debug)]
pub struct AsyncFileEngine {
    file: File,
    ring: IoUring<WrappedRequest>,
    completion_evt: EventFd,
    direct: bool,
}

/// Wraps a `PendingRequest` with optional dirty-tracking info and an optional
/// bounce buffer for O_DIRECT I/O with unaligned guest memory addresses.
#[derive(Debug)]
pub struct WrappedRequest {
    /// Guest address to mark dirty on read completion (for dirty tracking).
    addr: Option<GuestAddress>,
    req: PendingRequest,
    /// Bounce buffer held alive for the duration of the in-flight io_uring op.
    /// On read completion, data is copied from this buffer to guest memory.
    bounce_buf: Option<AlignedBuf>,
}

impl WrappedRequest {
    fn new(req: PendingRequest) -> Self {
        WrappedRequest {
            addr: None,
            req,
            bounce_buf: None,
        }
    }

    fn new_with_dirty_tracking(addr: GuestAddress, req: PendingRequest) -> Self {
        WrappedRequest {
            addr: Some(addr),
            req,
            bounce_buf: None,
        }
    }

    fn new_with_bounce_buf(
        addr: GuestAddress,
        req: PendingRequest,
        bounce_buf: AlignedBuf,
    ) -> Self {
        WrappedRequest {
            addr: Some(addr),
            req,
            bounce_buf: Some(bounce_buf),
        }
    }

    fn mark_dirty_mem_and_unwrap(
        mut self,
        mem: &GuestMemoryMmap,
        count: u32,
    ) -> PendingRequest {
        if let Some(addr) = self.addr {
            // If there is a bounce buffer, this was a read: copy data to guest memory.
            if let Some(ref bounce) = self.bounce_buf {
                let data = &bounce.as_slice()[..count as usize];
                if let Err(err) = mem.write_slice(data, addr) {
                    crate::logger::error!(
                        "Failed to copy bounce buffer to guest memory: {:?}",
                        err
                    );
                }
            }
            mem.mark_dirty(addr, count as usize);
        }
        // Drop bounce_buf here, freeing the aligned allocation.
        self.bounce_buf = None;
        self.req
    }
}

impl AsyncFileEngine {
    fn new_ring(
        file: &File,
        completion_fd: RawFd,
    ) -> Result<IoUring<WrappedRequest>, IoUringError> {
        IoUring::new(
            u32::from(IO_URING_NUM_ENTRIES),
            vec![file],
            vec![
                // Make sure we only allow operations on pre-registered fds.
                Restriction::RequireFixedFds,
                // Allowlist of opcodes.
                Restriction::AllowOpCode(OpCode::Read),
                Restriction::AllowOpCode(OpCode::Write),
                Restriction::AllowOpCode(OpCode::Fsync),
            ],
            Some(completion_fd),
        )
    }

    pub fn from_file(file: File, direct: bool) -> Result<AsyncFileEngine, AsyncIoError> {
        log_dev_preview_warning("Async file IO", Option::None);

        let completion_evt = EventFd::new(libc::EFD_NONBLOCK).map_err(AsyncIoError::EventFd)?;
        let ring =
            Self::new_ring(&file, completion_evt.as_raw_fd()).map_err(AsyncIoError::IoUring)?;

        Ok(AsyncFileEngine {
            file,
            ring,
            completion_evt,
            direct,
        })
    }

    /// Update the backing file and the direct-IO flag.
    pub fn update_file(&mut self, file: File, direct: bool) -> Result<(), AsyncIoError> {
        let ring = Self::new_ring(&file, self.completion_evt.as_raw_fd())
            .map_err(AsyncIoError::IoUring)?;

        self.file = file;
        self.ring = ring;
        self.direct = direct;
        Ok(())
    }

    pub fn file(&self) -> &File {
        &self.file
    }

    pub fn completion_evt(&self) -> &EventFd {
        &self.completion_evt
    }

    /// Returns true if the guest address requires a bounce buffer for O_DIRECT.
    fn needs_bounce_buf(&self, addr: GuestAddress) -> bool {
        self.direct && !(addr.0 as usize).is_multiple_of(DIRECT_IO_ALIGN)
    }

    pub fn push_read(
        &mut self,
        offset: u64,
        mem: &GuestMemoryMmap,
        addr: GuestAddress,
        count: u32,
        req: PendingRequest,
    ) -> Result<(), RequestError<AsyncIoError>> {
        if self.needs_bounce_buf(addr) {
            // Allocate an aligned bounce buffer; the io_uring read will go into it.
            // On completion, the data will be copied to guest memory in pop().
            let bounce = match AlignedBuf::new(count as usize, DIRECT_IO_ALIGN) {
                Some(b) => b,
                None => return Err(RequestError { req, error: AsyncIoError::BounceBufferAlloc }),
            };
            let buf_ptr = bounce.as_ptr();
            let wrapped_user_data = WrappedRequest::new_with_bounce_buf(addr, req, bounce);

            self.ring
                .push(Operation::read(
                    0,
                    buf_ptr as usize,
                    count,
                    offset,
                    wrapped_user_data,
                ))
                .map_err(|(io_uring_error, data)| RequestError {
                    req: data.req,
                    error: AsyncIoError::IoUring(io_uring_error),
                })
        } else {
            let buf = match mem.get_slice(addr, count as usize) {
                Ok(slice) => slice.ptr_guard_mut().as_ptr(),
                Err(err) => {
                    return Err(RequestError {
                        req,
                        error: AsyncIoError::GuestMemory(err),
                    });
                }
            };

            let wrapped_user_data = WrappedRequest::new_with_dirty_tracking(addr, req);

            self.ring
                .push(Operation::read(
                    0,
                    buf as usize,
                    count,
                    offset,
                    wrapped_user_data,
                ))
                .map_err(|(io_uring_error, data)| RequestError {
                    req: data.req,
                    error: AsyncIoError::IoUring(io_uring_error),
                })
        }
    }

    pub fn push_write(
        &mut self,
        offset: u64,
        mem: &GuestMemoryMmap,
        addr: GuestAddress,
        count: u32,
        req: PendingRequest,
    ) -> Result<(), RequestError<AsyncIoError>> {
        if self.needs_bounce_buf(addr) {
            // Copy guest data into an aligned bounce buffer, then submit write from it.
            let mut bounce = match AlignedBuf::new(count as usize, DIRECT_IO_ALIGN) {
                Some(b) => b,
                None => return Err(RequestError { req, error: AsyncIoError::BounceBufferAlloc }),
            };
            if let Err(err) = mem.read_slice(bounce.as_mut_slice(), addr) {
                return Err(RequestError {
                    req,
                    error: AsyncIoError::GuestMemory(err),
                });
            }
            let buf_ptr = bounce.as_ptr();
            // No dirty-tracking needed for writes; bounce_buf is dropped at completion.
            let wrapped_user_data = WrappedRequest {
                addr: None,
                req,
                bounce_buf: Some(bounce),
            };

            self.ring
                .push(Operation::write(
                    0,
                    buf_ptr as usize,
                    count,
                    offset,
                    wrapped_user_data,
                ))
                .map_err(|(io_uring_error, data)| RequestError {
                    req: data.req,
                    error: AsyncIoError::IoUring(io_uring_error),
                })
        } else {
            let buf = match mem.get_slice(addr, count as usize) {
                Ok(slice) => slice.ptr_guard_mut().as_ptr(),
                Err(err) => {
                    return Err(RequestError {
                        req,
                        error: AsyncIoError::GuestMemory(err),
                    });
                }
            };

            let wrapped_user_data = WrappedRequest::new(req);

            self.ring
                .push(Operation::write(
                    0,
                    buf as usize,
                    count,
                    offset,
                    wrapped_user_data,
                ))
                .map_err(|(io_uring_error, data)| RequestError {
                    req: data.req,
                    error: AsyncIoError::IoUring(io_uring_error),
                })
        }
    }

    pub fn push_flush(&mut self, req: PendingRequest) -> Result<(), RequestError<AsyncIoError>> {
        let wrapped_user_data = WrappedRequest::new(req);

        self.ring
            .push(Operation::fsync(0, wrapped_user_data))
            .map_err(|(io_uring_error, data)| RequestError {
                req: data.req,
                error: AsyncIoError::IoUring(io_uring_error),
            })
    }

    pub fn discard(&mut self, _range: (u64, u32)) -> Result<u32, AsyncIoError> {
        Err(AsyncIoError::NotImplemented)
    }

    pub fn kick_submission_queue(&mut self) -> Result<(), AsyncIoError> {
        self.ring
            .submit()
            .map(|_| ())
            .map_err(AsyncIoError::IoUring)
    }

    pub fn drain(&mut self, discard_cqes: bool) -> Result<(), AsyncIoError> {
        self.ring
            .submit_and_wait_all()
            .map(|_| ())
            .map_err(AsyncIoError::IoUring)?;

        if discard_cqes {
            // Drain the completion queue so that we may deallocate the user_data fields.
            while self.do_pop()?.is_some() {}
        }

        Ok(())
    }

    pub fn drain_and_flush(&mut self, discard_cqes: bool) -> Result<(), AsyncIoError> {
        self.drain(discard_cqes)?;

        // Sync data out to physical media on host.
        // We don't need to call flush first since all the ops are performed through io_uring
        // and Rust shouldn't manage any data in its internal buffers.
        self.file.sync_all().map_err(AsyncIoError::SyncAll)?;

        Ok(())
    }

    fn do_pop(&mut self) -> Result<Option<Cqe<WrappedRequest>>, AsyncIoError> {
        self.ring.pop().map_err(AsyncIoError::IoUring)
    }

    pub fn pop(
        &mut self,
        mem: &GuestMemoryMmap,
    ) -> Result<Option<Cqe<PendingRequest>>, AsyncIoError> {
        let cqe = self.do_pop()?.map(|cqe| {
            let count = cqe.count();
            cqe.map_user_data(|wrapped_user_data| {
                wrapped_user_data.mark_dirty_mem_and_unwrap(mem, count)
            })
        });

        Ok(cqe)
    }
}
