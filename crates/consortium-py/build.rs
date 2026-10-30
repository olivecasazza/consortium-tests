//! Build script for consortium-py.
//!
//! maturin passes the extension-module link flags itself, but plain
//! `cargo build --workspace` on macOS needs them too: Python symbols are
//! resolved at load time by the interpreter, so the cdylib must link with
//! `-undefined dynamic_lookup`.

fn main() {
    pyo3_build_config::add_extension_module_link_args();
}
