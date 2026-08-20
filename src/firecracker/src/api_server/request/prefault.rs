// Copyright 2026 kvcache-ai. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

use vmm::rpc_interface::VmmAction;
use vmm::vstate::prefault::PreFaultMemoryRequest;

use super::super::parsed_request::{ParsedRequest, RequestError};
use super::Body;

/// Parses a request to pre-fault selected guest memory.
pub(crate) fn parse_put_pre_fault_memory(body: &Body) -> Result<ParsedRequest, RequestError> {
    let request = serde_json::from_slice::<PreFaultMemoryRequest>(body.raw())?;
    Ok(ParsedRequest::new_sync(VmmAction::PreFaultMemory(request)))
}
