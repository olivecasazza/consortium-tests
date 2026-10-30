// Suppress PyO3 0.22 macro-generated cfg warnings (fixed in PyO3 0.23+).
#![allow(unexpected_cfgs)]

//! PyO3 bindings for consortium.
//!
//! This crate exposes the Rust implementation through a Python module named
//! `ClusterShell._consortium`. Thin Python wrapper modules (ClusterShell/RangeSet.py
//! etc.) re-export types from here so the original test imports work unchanged.

use pyo3::prelude::*;

mod node_set;
mod range_set;

/// The native extension module, importable as `ClusterShell._consortium`.
#[pymodule]
fn _consortium(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Register submodules
    range_set::register(m)?;
    node_set::register(m)?;
    Ok(())
}
