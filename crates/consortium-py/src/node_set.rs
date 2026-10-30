//! PyO3 wrappers for consortium::node_set.
//!
//! Exposes a Python-visible `NodeSet` class backed by
//! `consortium::node_set::NodeSet`, covering the scalar (non-group) API:
//! parsing, set operations, iteration, list-like `index()`/`__getitem__()`
//! with slice support (including stepped slices), and `split()`.

use pyo3::exceptions::{PyAssertionError, PyIndexError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PySlice;

use consortium::node_set::{NodeSet as RustNodeSet, NodeSetError as RustNodeSetError};

use crate::range_set::extract_autostep;

pyo3::create_exception!(
    ClusterShell.NodeSet,
    NodeSetException,
    pyo3::exceptions::PyException
);
pyo3::create_exception!(ClusterShell.NodeSet, NodeSetError, NodeSetException);
pyo3::create_exception!(ClusterShell.NodeSet, NodeSetParseError, NodeSetError);
pyo3::create_exception!(
    ClusterShell.NodeSet,
    NodeSetParseRangeError,
    NodeSetParseError
);
pyo3::create_exception!(ClusterShell.NodeSet, NodeSetExternalError, NodeSetError);

/// Convert a core NodeSetError into the matching Python exception,
/// formatting messages like upstream ClusterShell.NodeSet.
fn to_py_err(py: Python<'_>, err: RustNodeSetError) -> PyErr {
    match err {
        RustNodeSetError::ParseError { part, msg } => {
            let full = if part.is_empty() {
                msg.clone()
            } else {
                format!("{}: \"{}\"", msg, part)
            };
            let exc: PyErr = NodeSetParseError::new_err(full);
            let val = exc.value_bound(py);
            let _ = val.setattr("part", part);
            exc
        }
        RustNodeSetError::RangeError(rerr) => {
            NodeSetParseRangeError::new_err(format!("bad range: \"{}\"", rerr))
        }
        RustNodeSetError::ExternalError(msg) => NodeSetExternalError::new_err(msg),
        RustNodeSetError::IndexError(msg) => PyIndexError::new_err(msg),
    }
}

/// Python-visible NodeSet class (scalar API).
///
/// The user-facing autostep value is tracked in the wrapper itself, as the
/// core does not expose autostep accessors on NodeSet.
#[pyclass(name = "NodeSet", module = "ClusterShell.NodeSet")]
#[derive(Debug, Clone)]
pub struct PyNodeSet {
    pub inner: RustNodeSet,
    autostep: Option<u32>,
}

/// Structural equality helper (core NodeSet has no PartialEq): compare
/// iteration order element vectors.
fn ns_eq(a: &RustNodeSet, b: &RustNodeSet) -> bool {
    a.iter().collect::<Vec<_>>() == b.iter().collect::<Vec<_>>()
}

impl PyNodeSet {
    /// Parse `other` as a single-node argument like upstream NodeSet.index()
    /// does; returns the position or raises ValueError.
    fn index_impl(&self, other: &str) -> PyResult<usize> {
        let searched = RustNodeSet::parse(other)
            .map_err(|_| PyValueError::new_err("index() argument must be a single node"))?;
        if searched.len() != 1 {
            return Err(PyValueError::new_err(
                "index() argument must be a single node",
            ));
        }
        match self.inner.index(other) {
            Some(pos) => Ok(pos),
            None => Err(PyValueError::new_err(format!(
                "'{}' is not in nodeset",
                other
            ))),
        }
    }
}

#[pymethods]
impl PyNodeSet {
    #[new]
    #[pyo3(signature = (nodes=None, autostep=None))]
    fn new(
        py: Python<'_>,
        nodes: Option<&Bound<'_, PyAny>>,
        autostep: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let autostep = extract_autostep(autostep)?;
        let mut ns = RustNodeSet::new();
        match nodes {
            None => Ok(Self {
                inner: ns,
                autostep,
            }),
            Some(obj) => {
                if let Ok(other) = obj.downcast::<PyNodeSet>() {
                    ns.update(&other.borrow().inner);
                    return Ok(Self {
                        inner: ns,
                        autostep,
                    });
                }
                let pat: String = if let Ok(s) = obj.extract::<String>() {
                    s
                } else {
                    // any iterable of node names
                    let mut parts = Vec::new();
                    for item in obj.iter()? {
                        parts.push(item?.str()?.to_string());
                    }
                    parts.join(",")
                };
                let parsed = match autostep {
                    Some(v) => RustNodeSet::parse_with_autostep(&pat, Some(v)),
                    None => RustNodeSet::parse(&pat),
                }
                .map_err(|e| to_py_err(py, e))?;
                ns.update(&parsed);
                Ok(Self {
                    inner: ns,
                    autostep,
                })
            }
        }
    }

    fn __str__(&self) -> String {
        self.inner.to_string()
    }

    fn __repr__(&self) -> String {
        self.inner.to_string()
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    fn __contains__(&self, node: &str) -> bool {
        self.inner.contains(node)
    }

    fn __iter__(slf: PyRef<'_, Self>) -> PyResult<Py<NodeSetIterator>> {
        let values: Vec<String> = slf.inner.iter().collect();
        Py::new(slf.py(), NodeSetIterator { values, index: 0 })
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        match other.downcast::<PyNodeSet>() {
            Ok(ns) => Ok(ns_eq(&self.inner, &ns.borrow().inner).into_py(py)),
            Err(_) => Ok(py.NotImplemented().into_py(py)),
        }
    }

    fn __ne__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        match other.downcast::<PyNodeSet>() {
            Ok(ns) => Ok((!ns_eq(&self.inner, &ns.borrow().inner)).into_py(py)),
            Err(_) => Ok(py.NotImplemented().into_py(py)),
        }
    }

    fn _binary_sanity_check(&self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        if other.downcast::<PyNodeSet>().is_err() {
            return Err(PyTypeError::new_err(
                "Binary operation only permitted between NodeSet",
            ));
        }
        Ok(())
    }

    /// s.update(t) returns nodeset s with elements added from t.
    fn update(&mut self, py: Python<'_>, other: &Bound<'_, PyAny>) -> PyResult<()> {
        if let Ok(ns) = other.downcast::<PyNodeSet>() {
            let borrowed = match ns.try_borrow() {
                Ok(b) => b.inner.clone(),
                Err(_) => self.inner.clone(), // other is self
            };
            self.inner.update(&borrowed);
            return Ok(());
        }
        if let Ok(s) = other.extract::<String>() {
            self.inner.update_str(&s).map_err(|e| to_py_err(py, e))?;
            return Ok(());
        }
        Err(PyTypeError::new_err(format!(
            "cannot update NodeSet with {}",
            other.str()?
        )))
    }

    fn union(&self, other: &Bound<'_, PyAny>) -> PyResult<PyNodeSet> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        Ok(PyNodeSet {
            inner: self.inner.union(&ns.borrow().inner),
            autostep: self.autostep,
        })
    }

    fn __or__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        if other.downcast::<PyNodeSet>().is_err() {
            return Ok(py.NotImplemented().into_py(py));
        }
        Ok(Py::new(py, self.union(other)?)?.into_py(py))
    }

    fn __ior__(&mut self, py: Python<'_>, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self.update(py, other)
    }

    fn intersection(&self, other: &Bound<'_, PyAny>) -> PyResult<PyNodeSet> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        Ok(PyNodeSet {
            inner: self.inner.intersection(&ns.borrow().inner),
            autostep: self.autostep,
        })
    }

    fn __and__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        if other.downcast::<PyNodeSet>().is_err() {
            return Ok(py.NotImplemented().into_py(py));
        }
        Ok(Py::new(py, self.intersection(other)?)?.into_py(py))
    }

    fn intersection_update(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        let borrowed = match ns.try_borrow() {
            Ok(b) => b.inner.clone(),
            Err(_) => self.inner.clone(),
        };
        self.inner.intersection_update(&borrowed);
        Ok(())
    }

    fn __iand__(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self.intersection_update(other)
    }

    fn difference(&self, other: &Bound<'_, PyAny>) -> PyResult<PyNodeSet> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        Ok(PyNodeSet {
            inner: self.inner.difference(&ns.borrow().inner),
            autostep: self.autostep,
        })
    }

    fn __sub__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        if other.downcast::<PyNodeSet>().is_err() {
            return Ok(py.NotImplemented().into_py(py));
        }
        Ok(Py::new(py, self.difference(other)?)?.into_py(py))
    }

    #[pyo3(signature = (other, strict=false))]
    fn difference_update(&mut self, other: &Bound<'_, PyAny>, strict: bool) -> PyResult<()> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        let borrowed = match ns.try_borrow() {
            Ok(b) => b.inner.clone(),
            Err(_) => self.inner.clone(),
        };
        if strict {
            for node in borrowed.iter() {
                if !self.inner.contains(&node) {
                    return Err(pyo3::exceptions::PyKeyError::new_err(node));
                }
            }
        }
        self.inner.difference_update(&borrowed);
        Ok(())
    }

    fn __isub__(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self.difference_update(other, false)
    }

    fn symmetric_difference(&self, other: &Bound<'_, PyAny>) -> PyResult<PyNodeSet> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        Ok(PyNodeSet {
            inner: self.inner.symmetric_difference(&ns.borrow().inner),
            autostep: self.autostep,
        })
    }

    fn __xor__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        if other.downcast::<PyNodeSet>().is_err() {
            return Ok(py.NotImplemented().into_py(py));
        }
        Ok(Py::new(py, self.symmetric_difference(other)?)?.into_py(py))
    }

    fn symmetric_difference_update(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        let borrowed = match ns.try_borrow() {
            Ok(b) => b.inner.clone(),
            Err(_) => self.inner.clone(),
        };
        self.inner.symmetric_difference_update(&borrowed);
        Ok(())
    }

    fn __ixor__(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self.symmetric_difference_update(other)
    }

    /// Remove all nodes from this NodeSet.
    fn clear(&mut self) {
        self.inner.clear();
    }

    /// Return a shallow copy of this NodeSet.
    fn copy(&self) -> Self {
        self.clone()
    }

    fn __copy__(&self) -> Self {
        self.clone()
    }

    fn issubset(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        Ok(self.inner.is_subset(&ns.borrow().inner))
    }

    fn issuperset(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        Ok(self.inner.is_superset(&ns.borrow().inner))
    }

    fn __lt__(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        Ok(self.inner.len() < ns.borrow().inner.len() && self.issubset(other)?)
    }

    fn __gt__(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self._binary_sanity_check(other)?;
        let ns = other.downcast::<PyNodeSet>()?;
        Ok(self.inner.len() > ns.borrow().inner.len() && self.issuperset(other)?)
    }

    fn __le__(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.issubset(other)
    }

    fn __ge__(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.issuperset(other)
    }

    /// Return the node at specified index, or a subnodeset when a slice is
    /// specified (with optional step, across multiple patterns).
    fn __getitem__(slf: PyRef<'_, Self>, index: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        if let Ok(slice) = index.downcast::<PySlice>() {
            let start_attr = slice.getattr("start")?;
            let stop_attr = slice.getattr("stop")?;
            let step_attr = slice.getattr("step")?;
            let start: Option<i64> = if start_attr.is_none() {
                None
            } else {
                Some(start_attr.extract()?)
            };
            let stop: Option<i64> = if stop_attr.is_none() {
                None
            } else {
                Some(stop_attr.extract()?)
            };
            let step: i64 = if step_attr.is_none() {
                1
            } else {
                step_attr.extract()?
            };
            let sliced = slf
                .inner
                .slice(start, stop, step)
                .map_err(|e| to_py_err(py, e))?;
            return Ok(Py::new(
                py,
                PyNodeSet {
                    inner: sliced,
                    autostep: slf.autostep,
                },
            )?
            .into_py(py));
        }
        if let Ok(idx) = index.extract::<i64>() {
            match slf.inner.get(idx) {
                Some(node) => return Ok(node.into_py(py)),
                None => return Err(PyIndexError::new_err(format!("{} out of range", idx))),
            }
        }
        Err(PyTypeError::new_err("NodeSet indices must be integers"))
    }

    /// Return the zero-based index in the nodeset of the node `other`
    /// (list.index() semantics; ValueError when absent).
    #[pyo3(signature = (other, start=0, stop=None))]
    fn index(&self, other: &str, start: i64, stop: Option<i64>) -> PyResult<usize> {
        let found = self.index_impl(other)?;
        if start != 0 || stop.is_some() {
            let length = self.inner.len() as i64;
            let start = if start < 0 {
                (length + start).max(0)
            } else {
                start
            };
            let stop = match stop {
                None => length,
                Some(s) if s < 0 => (length + s).max(0),
                Some(s) => s,
            };
            if !(start <= found as i64 && (found as i64) < stop) {
                return Err(PyValueError::new_err(format!(
                    "'{}' is not in nodeset",
                    other
                )));
            }
        }
        Ok(found)
    }

    /// Split the nodeset into nbr sub-nodesets (at most).
    fn split(&self, nbr: usize) -> PyResult<Vec<PyNodeSet>> {
        if nbr == 0 {
            return Err(PyAssertionError::new_err("assertion failed"));
        }
        Ok(self
            .inner
            .split(nbr)
            .into_iter()
            .map(|ns| PyNodeSet {
                inner: ns,
                autostep: self.autostep,
            })
            .collect())
    }

    /// Get autostep value (property getter).
    #[getter(autostep)]
    fn get_autostep_prop(&self) -> Option<u32> {
        self.autostep
    }

    /// Set autostep value (property setter): accepts None, int or float.
    #[setter(autostep)]
    fn set_autostep_prop(&mut self, val: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        self.autostep = extract_autostep(val)?;
        Ok(())
    }

    fn get_autostep(&self) -> Option<u32> {
        self.autostep
    }

    #[pyo3(signature = (val=None))]
    fn set_autostep(&mut self, val: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        self.autostep = extract_autostep(val)?;
        Ok(())
    }
}

#[pyclass]
struct NodeSetIterator {
    values: Vec<String>,
    index: usize,
}

#[pymethods]
impl NodeSetIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&mut self) -> Option<String> {
        if self.index < self.values.len() {
            let val = self.values[self.index].clone();
            self.index += 1;
            Some(val)
        } else {
            None
        }
    }
}

/// Commodity function that expands a nodeset pattern into a list of nodes.
#[pyfunction]
fn expand(py: Python<'_>, pat: &str) -> PyResult<Vec<String>> {
    consortium::node_set::expand(pat).map_err(|e| to_py_err(py, e))
}

/// Commodity function that folds a nodeset pattern into a nodeset string.
#[pyfunction]
fn fold(py: Python<'_>, pat: &str) -> PyResult<String> {
    consortium::node_set::fold(pat).map_err(|e| to_py_err(py, e))
}

/// Register node_set types into the parent module.
pub fn register(parent: &Bound<'_, PyModule>) -> PyResult<()> {
    parent.add_class::<PyNodeSet>()?;
    parent.add_function(wrap_pyfunction!(expand, parent)?)?;
    parent.add_function(wrap_pyfunction!(fold, parent)?)?;
    let py = parent.py();
    parent.add("NodeSetException", py.get_type_bound::<NodeSetException>())?;
    parent.add("NodeSetError", py.get_type_bound::<NodeSetError>())?;
    parent.add(
        "NodeSetParseError",
        py.get_type_bound::<NodeSetParseError>(),
    )?;
    parent.add(
        "NodeSetParseRangeError",
        py.get_type_bound::<NodeSetParseRangeError>(),
    )?;
    parent.add(
        "NodeSetExternalError",
        py.get_type_bound::<NodeSetExternalError>(),
    )?;
    Ok(())
}
