//! PyO3 wrappers for consortium::range_set.
//!
//! Exposes a Python-visible `RangeSet` class with the same public API as
//! upstream `ClusterShell.RangeSet.RangeSet` (ClusterShell 1.10.1), backed by
//! `consortium::range_set::RangeSet`.

use pyo3::exceptions::{PyAssertionError, PyIndexError, PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyIterator, PyList, PySlice, PyTuple, PyType};

use consortium::range_set::{RangeSet as RustRangeSet, RangeSetError, AUTOSTEP_DISABLED};

pyo3::create_exception!(
    ClusterShell.RangeSet,
    RangeSetException,
    pyo3::exceptions::PyException
);
pyo3::create_exception!(ClusterShell.RangeSet, RangeSetParseError, RangeSetException);
pyo3::create_exception!(
    ClusterShell.RangeSet,
    RangeSetPaddingError,
    RangeSetParseError
);

/// RangeSet serial version number (matches upstream RangeSet._VERSION).
const RANGESET_VERSION: i32 = 4;

/// Convert a core RangeSetError into the matching Python exception,
/// formatting the message like upstream RangeSetParseError and attaching
/// the faulty subrange as the `part` attribute.
fn to_py_err(py: Python<'_>, err: RangeSetError) -> PyErr {
    let (part, msg, is_padding) = match err {
        RangeSetError::ParseError { part, msg } => (part, msg, false),
        RangeSetError::PaddingError { part, msg } => (part, msg, true),
    };
    let full = if part.is_empty() {
        msg.clone()
    } else {
        format!("{} : \"{}\"", msg, part)
    };
    let exc: PyErr = if is_padding {
        RangeSetPaddingError::new_err(format!("padding mismatch ({})", full))
    } else {
        RangeSetParseError::new_err(full)
    };
    {
        let val = exc.value_bound(py);
        let _ = val.setattr("part", part);
    }
    exc
}

/// Build a RangeSetParseError (with .part) from static parts.
fn parse_err(py: Python<'_>, part: &str, msg: &str) -> PyErr {
    to_py_err(
        py,
        RangeSetError::ParseError {
            part: part.to_string(),
            msg: msg.to_string(),
        },
    )
}

/// Extract an autostep threshold from an arbitrary Python value, mirroring
/// upstream RangeSet.set_autostep(): None or any value >= AUTOSTEP_DISABLED
/// (eg. the 1E100 float constant) disables stepping. The core stores the
/// threshold as Option<u32>, so values in (u32::MAX, 1E100) are clamped to
/// "disabled" — folding behavior is identical for any such huge threshold,
/// only a getter read-back would diverge (no upstream test does this).
pub(crate) fn extract_autostep(val: Option<&Bound<'_, PyAny>>) -> PyResult<Option<u32>> {
    match val {
        None => Ok(None),
        Some(v) if v.is_none() => Ok(None),
        Some(v) => {
            let f: f64 = v.extract()?;
            if f >= AUTOSTEP_DISABLED || f >= u32::MAX as f64 {
                Ok(None)
            } else {
                // saturating cast also covers negatives (-> 0)
                Ok(Some(f as u32))
            }
        }
    }
}

/// Oracle-faithful port of RangeSet._parse(), used instead of the core
/// parser because the binding must reproduce Python behaviors the core
/// parser does not cover: negative ranges ("-9--6") and whitespace-tolerant
/// integer conversion ("0 -8 / 2").
fn parse_oracle(py: Python<'_>, pattern: &str, autostep: Option<u32>) -> PyResult<RustRangeSet> {
    let mut rs = RustRangeSet::new();
    rs.set_autostep(autostep);
    for raw in pattern.split(',') {
        let subrange = raw.trim();

        let (baserange, step): (&str, i64) = match subrange.find('/') {
            None => (subrange, 1),
            Some(pos) => {
                let step_str = subrange[pos + 1..].trim();
                match step_str.parse::<i64>() {
                    Ok(v) => (&subrange[..pos], v),
                    Err(_) => {
                        return Err(parse_err(py, subrange, "cannot convert string to integer"))
                    }
                }
            }
        };

        // parse begin/end parts, handling negative numbers
        let (begin, end, begin_sign, end_sign): (String, String, i64, i64) =
            if !baserange.contains('-') {
                if step != 1 {
                    return Err(parse_err(py, subrange, "invalid step usage"));
                }
                (baserange.to_string(), baserange.to_string(), 1, 1)
            } else {
                let parts: Vec<&str> = baserange.split('-').collect();
                match parts.len() {
                    2 => {
                        let b = parts[0].trim();
                        let e = parts[1].trim();
                        if b.is_empty() {
                            // single negative number "-5"
                            (e.to_string(), e.to_string(), -1, -1)
                        } else {
                            (b.to_string(), e.to_string(), 1, 1)
                        }
                    }
                    // "-0-3"
                    3 => (
                        parts[1].trim().to_string(),
                        parts[2].trim().to_string(),
                        -1,
                        1,
                    ),
                    // "-8--4"
                    4 => (
                        parts[1].trim().to_string(),
                        parts[3].trim().to_string(),
                        -1,
                        -1,
                    ),
                    _ => {
                        return Err(PyValueError::new_err(format!(
                            "too many values to unpack (expected 2): {}",
                            subrange
                        )))
                    }
                }
            };

        // compute padding and numeric bounds (Python int() tolerates
        // surrounding whitespace: inputs are pre-trimmed)
        let int_conv = |txt: &str| -> Result<i64, ()> { txt.trim().parse::<i64>().map_err(|_| ()) };
        let conv_err = |py: Python<'_>| {
            if subrange.is_empty() {
                parse_err(py, subrange, "empty range")
            } else {
                parse_err(py, subrange, "cannot convert string to integer")
            }
        };
        let begin_int = int_conv(&begin).map_err(|_| conv_err(py))?;
        let mut pad = 0usize;
        let start: i64 = if begin_int != 0 {
            let begins = begin.trim_start_matches('0');
            if begin.len() - begins.len() > 0 {
                pad = begin.len();
            }
            begins.parse::<i64>().map_err(|_| conv_err(py))?
        } else {
            if begin.len() > 1 {
                pad = begin.len();
            }
            0
        };
        let end_int_probe = int_conv(&end).map_err(|_| conv_err(py))?;
        let ends = if end_int_probe != 0 {
            end.trim_start_matches('0')
        } else {
            end.as_str()
        };
        let endpad = if end.len() - ends.len() > 0 {
            end.len()
        } else {
            0
        };
        if (pad > 0 || endpad > 0) && begin.len() != end.len() {
            return Err(parse_err(py, subrange, "padding length mismatch"));
        }
        let stop: i64 = ends.parse::<i64>().map_err(|_| conv_err(py))?;

        // check preconditions
        if pad > 0 && begin_sign < 0 {
            return Err(parse_err(
                py,
                subrange,
                "padding not supported in negative ranges",
            ));
        }
        if (stop as f64) > 1e100 || start * begin_sign > stop * end_sign || step < 1 {
            return Err(parse_err(py, subrange, "invalid values in range"));
        }

        let lo = start * begin_sign;
        let hi = stop * end_sign + 1;
        // mirrors add_range() assertions (raised as AssertionError upstream)
        if lo >= hi {
            return Err(PyAssertionError::new_err(
                "please provide ordered node index ranges",
            ));
        }
        if hi - lo >= 1_000_000_000 {
            return Err(PyAssertionError::new_err("range too large"));
        }
        rs.add_range(lo, hi, step, pad as u32);
    }
    Ok(rs)
}

/// Internal (Python-private) autostep value: user-facing value - 1, or
/// AUTOSTEP_DISABLED when stepping is off.
fn internal_autostep(rs: &RustRangeSet) -> f64 {
    match rs.autostep() {
        None => AUTOSTEP_DISABLED,
        Some(user) => (user as f64) - 1.0,
    }
}

/// A (start, stop, step, pad) folded slice, mirroring Python's slice+pad pairs.
#[derive(Debug, Clone)]
struct FoldedSlice {
    start: i64,
    stop: i64, // exclusive
    step: i64,
    pad: usize,
}

/// Port of Python's RangeSet._slices_padding(): fold sorted elements into
/// (slice, pad) pairs honoring the autostep threshold.
fn slices_padding(sorted: &[String], autostep: f64, self_autostep: f64) -> Vec<FoldedSlice> {
    let mut result: Vec<FoldedSlice> = Vec::new();
    if sorted.is_empty() {
        return result;
    }

    let mut cur_pad: usize = 0;
    let mut cur_padded = false;
    let mut cur_start: Option<i64> = None;
    let mut cur_step: Option<i64> = None;
    let mut last_idx: i64 = 0;

    for si in sorted {
        let idx: i64 = si.parse().unwrap_or(0);
        let digitlen = si.len();
        let padded = digitlen > 1 && si.starts_with('0');

        if let Some(cs) = cur_start {
            let padding_mismatch = if cur_padded {
                digitlen != cur_pad
            } else {
                padded
            };
            let step_mismatch = match cur_step {
                Some(cstep) => cstep != idx - last_idx,
                None => false,
            };

            if padding_mismatch || step_mismatch {
                let (stepped, step) = match cur_step {
                    Some(cstep) => {
                        let stepped =
                            (cstep == 1) || ((last_idx - cs) as f64 >= autostep * cstep as f64);
                        (stepped, cstep)
                    }
                    None => (true, 1i64),
                };

                if stepped {
                    result.push(FoldedSlice {
                        start: cs,
                        stop: last_idx + 1,
                        step,
                        pad: if cur_padded { cur_pad } else { 0 },
                    });
                    cur_start = Some(idx);
                    cur_padded = padded;
                    cur_pad = digitlen;
                } else if padding_mismatch {
                    let stop = last_idx + 1;
                    let mut j = cs;
                    while j < stop {
                        result.push(FoldedSlice {
                            start: j,
                            stop: j + 1,
                            step: 1,
                            pad: if cur_padded { cur_pad } else { 0 },
                        });
                        j += step;
                    }
                    cur_start = Some(idx);
                    cur_padded = padded;
                    cur_pad = digitlen;
                } else {
                    let stop = last_idx - step + 1;
                    let mut j = cs;
                    while j < stop {
                        result.push(FoldedSlice {
                            start: j,
                            stop: j + 1,
                            step: 1,
                            pad: if cur_padded { cur_pad } else { 0 },
                        });
                        j += step;
                    }
                    cur_start = Some(last_idx);
                }

                cur_step = if step_mismatch {
                    Some(idx - last_idx)
                } else {
                    None
                };
                last_idx = idx;
                continue;
            }
        } else {
            // first index
            cur_padded = padded;
            cur_pad = digitlen;
            cur_start = Some(idx);
            cur_step = None;
            last_idx = idx;
            continue;
        }

        cur_step = Some(idx - last_idx);
        last_idx = idx;
    }

    if let Some(cs) = cur_start {
        match cur_step {
            Some(cstep) => {
                let stepped = (last_idx - cs) as f64 >= self_autostep * cstep as f64;
                if stepped || cstep == 1 {
                    result.push(FoldedSlice {
                        start: cs,
                        stop: last_idx + 1,
                        step: cstep,
                        pad: if cur_padded { cur_pad } else { 0 },
                    });
                } else {
                    let mut j = cs;
                    while j <= last_idx {
                        result.push(FoldedSlice {
                            start: j,
                            stop: j + 1,
                            step: 1,
                            pad: if cur_padded { cur_pad } else { 0 },
                        });
                        j += cstep;
                    }
                }
            }
            None => {
                result.push(FoldedSlice {
                    start: cs,
                    stop: last_idx + 1,
                    step: 1,
                    pad: if cur_padded { cur_pad } else { 0 },
                });
            }
        }
    }

    result
}

/// Python-visible RangeSet class.
///
/// Wraps `consortium::range_set::RangeSet` and exposes the same API as
/// `ClusterShell.RangeSet.RangeSet`.
#[pyclass(name = "RangeSet", module = "ClusterShell.RangeSet")]
#[derive(Debug, Clone)]
pub struct PyRangeSet {
    pub inner: RustRangeSet,
}

/// Extract the string elements of a Python iterable argument (used by
/// update() and friends): strings are kept as-is, anything else is
/// stringified like Python's `str(item)`.
fn iterable_to_strings(obj: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let mut items = Vec::new();
    for item in obj.iter()? {
        let item = item?;
        items.push(item.str()?.to_string());
    }
    Ok(items)
}

/// Set-like argument extraction: accepts PyRangeSet, builtin set/frozenset
/// or any iterable; returns element strings.
fn setlike_to_strings(obj: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    if let Ok(rs) = obj.downcast::<PyRangeSet>() {
        return Ok(rs.borrow().inner.sorted());
    }
    iterable_to_strings(obj)
}

/// Variant of setlike_to_strings() for in-place (&mut self) methods: if the
/// argument is the same object as self, the mutable borrow is already held,
/// so fall back to the provided snapshot of self's elements.
fn setlike_to_strings_inplace(
    obj: &Bound<'_, PyAny>,
    self_elems: &[String],
) -> PyResult<Vec<String>> {
    if let Ok(rs) = obj.downcast::<PyRangeSet>() {
        return match rs.try_borrow() {
            Ok(b) => Ok(b.inner.sorted()),
            Err(_) => Ok(self_elems.to_vec()),
        };
    }
    iterable_to_strings(obj)
}

/// Build a RangeSet from element strings (no parsing).
fn rs_from_elements(elems: Vec<String>, autostep_user: Option<u32>) -> RustRangeSet {
    let mut rs = RustRangeSet::new();
    rs.set_autostep(autostep_user);
    for e in elems {
        rs.add_str(&e);
    }
    rs
}

impl PyRangeSet {
    /// Shared implementation of updaten().
    fn do_updaten(&mut self, py: Python<'_>, rangesets: &Bound<'_, PyAny>) -> PyResult<()> {
        for rng in rangesets.iter()? {
            let rng = rng?;
            if let Ok(rs) = rng.downcast::<PyRangeSet>() {
                for e in rs.borrow().inner.sorted() {
                    self.inner.add_str(&e);
                }
            } else if rng.downcast::<pyo3::types::PySet>().is_ok()
                || rng.downcast::<pyo3::types::PyFrozenSet>().is_ok()
            {
                for e in iterable_to_strings(&rng)? {
                    self.inner.add_str(&e);
                }
            } else if let Ok(s) = rng.extract::<String>() {
                let other = RustRangeSet::parse(&s, None).map_err(|e| to_py_err(py, e))?;
                for e in other.sorted() {
                    self.inner.add_str(&e);
                }
            } else {
                let s = iterable_to_strings(&rng)?.join(",");
                let other = RustRangeSet::parse(&s, None).map_err(|e| to_py_err(py, e))?;
                for e in other.sorted() {
                    self.inner.add_str(&e);
                }
            }
        }
        Ok(())
    }

    /// Shared implementation of add().
    fn do_add(&mut self, element: &Bound<'_, PyAny>, pad: u32) -> PyResult<()> {
        if let Ok(s) = element.extract::<String>() {
            self.inner.add_str(&s);
        } else {
            let value: i64 = element.extract()?;
            self.inner.add_int(value, pad);
        }
        Ok(())
    }

    /// Shared implementation of add_range().
    fn do_add_range(&mut self, start: i64, stop: i64, step: i64, pad: u32) -> PyResult<()> {
        if start >= stop {
            return Err(PyAssertionError::new_err(
                "please provide ordered node index ranges",
            ));
        }
        if step <= 0 {
            return Err(PyAssertionError::new_err("assertion failed"));
        }
        if stop - start >= 1_000_000_000 {
            return Err(PyAssertionError::new_err("range too large"));
        }
        self.inner.add_range(start, stop, step, pad);
        Ok(())
    }
}

#[pymethods]
impl PyRangeSet {
    #[new]
    #[pyo3(signature = (pattern=None, autostep=None))]
    fn new(
        py: Python<'_>,
        pattern: Option<&Bound<'_, PyAny>>,
        autostep: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        // NB: like upstream, passing autostep=None disables stepping, even
        // when pattern is another RangeSet.
        let autostep = extract_autostep(autostep)?;
        match pattern {
            None => {
                let mut rs = RustRangeSet::new();
                rs.set_autostep(autostep);
                Ok(Self { inner: rs })
            }
            Some(obj) => {
                let pat_string: String = if let Ok(s) = obj.extract::<String>() {
                    s
                } else if let Ok(other) = obj.downcast::<PyRangeSet>() {
                    // another RangeSet is an iterable of its elements
                    other.borrow().inner.sorted().join(",")
                } else {
                    // any other iterable of integers/strings
                    iterable_to_strings(obj)?.join(",")
                };
                let rs = parse_oracle(py, &pat_string, autostep)?;
                Ok(Self { inner: rs })
            }
        }
    }

    /// Class method: new RangeSet from a list of ranges (patterns or sets).
    #[classmethod]
    #[pyo3(signature = (rnglist, autostep=None))]
    fn fromlist(
        cls: &Bound<'_, PyType>,
        rnglist: &Bound<'_, PyAny>,
        autostep: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyRangeSet>> {
        let mut rs = RustRangeSet::new();
        rs.set_autostep(extract_autostep(autostep)?);
        let mut inst = PyRangeSet { inner: rs };
        inst.do_updaten(cls.py(), rnglist)?;
        Py::new(cls.py(), inst)
    }

    /// Class method: new RangeSet of one single item or single range.
    /// NB: pad=None is tolerated (upstream NodeSet passes its autostep
    /// positionally, which lands here; pad is ignored for string indexes).
    #[classmethod]
    #[pyo3(signature = (index, pad=None, autostep=None))]
    fn fromone(
        cls: &Bound<'_, PyType>,
        index: &Bound<'_, PyAny>,
        pad: Option<usize>,
        autostep: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyRangeSet>> {
        let py = cls.py();
        let pad = pad.unwrap_or(0);
        let mut rs = RustRangeSet::new();
        rs.set_autostep(extract_autostep(autostep)?);
        let mut inst = PyRangeSet { inner: rs };
        match inst.do_add(index, pad as u32) {
            Ok(_) => Py::new(py, inst),
            Err(err) => {
                if err.is_instance_of::<PyTypeError>(py) {
                    // support slice object with duck-typing
                    if let Ok(slice) = index.downcast::<PySlice>() {
                        let stop = slice.getattr("stop")?;
                        if stop.is_none() {
                            return Err(PyValueError::new_err(format!(
                                "Invalid range upper limit ({})",
                                stop.str()?
                            )));
                        }
                        let start = slice.getattr("start")?;
                        let step = slice.getattr("step")?;
                        let start: i64 = if start.is_none() { 0 } else { start.extract()? };
                        let stop: i64 = stop.extract()?;
                        let step: i64 = if step.is_none() { 1 } else { step.extract()? };
                        inst.do_add_range(start, stop, step, pad as u32)?;
                        return Py::new(py, inst);
                    }
                }
                Err(err)
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

    fn dim(&self) -> usize {
        if self.inner.is_empty() {
            0
        } else {
            1
        }
    }

    fn __contains__(&self, element: &Bound<'_, PyAny>) -> PyResult<bool> {
        // A set argument is treated as a subset check (upstream behavior).
        if let Ok(rs) = element.downcast::<PyRangeSet>() {
            let other = rs.borrow();
            return Ok(other
                .inner
                .sorted()
                .iter()
                .all(|e| self.inner.contains_str(e)));
        }
        if element.downcast::<pyo3::types::PySet>().is_ok()
            || element.downcast::<pyo3::types::PyFrozenSet>().is_ok()
        {
            for item in element.iter()? {
                let item = item?;
                // items are compared as-is: only strings may match
                match item.extract::<String>() {
                    Ok(s) => {
                        if !self.inner.contains_str(&s) {
                            return Ok(false);
                        }
                    }
                    Err(_) => return Ok(false),
                }
            }
            return Ok(true);
        }
        // otherwise, stringified membership test
        Ok(self.inner.contains_str(&element.str()?.to_string()))
    }

    fn __iter__(slf: PyRef<'_, Self>) -> PyResult<Py<RangeSetStrIterator>> {
        let values: Vec<String> = slf.inner.sorted();
        Py::new(slf.py(), RangeSetStrIterator { values, index: 0 })
    }

    /// Iterate over each element as strings with optional zero-padding.
    fn striter<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        PyIterator::from_bound_object(&PyList::new_bound(py, self.inner.sorted()))
    }

    /// Iterate over each element as integers (padding ignored).
    fn intiter<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        let ints: Vec<i64> = self
            .inner
            .sorted()
            .iter()
            .map(|s| s.parse::<i64>().unwrap_or(0))
            .collect();
        PyIterator::from_bound_object(&PyList::new_bound(py, ints))
    }

    /// Get sorted list of elements (upstream _sorted()).
    fn _sorted(&self) -> Vec<String> {
        self.inner.sorted()
    }

    /// Iterate over contiguous range sets.
    fn contiguous(slf: PyRef<'_, Self>) -> PyResult<Vec<PyRangeSet>> {
        let sorted = slf.inner.sorted();
        let result = slices_padding(&sorted, AUTOSTEP_DISABLED, internal_autostep(&slf.inner));
        Ok(result
            .iter()
            .map(|sli| {
                let mut rs = RustRangeSet::new();
                rs.set_autostep(slf.inner.autostep());
                rs.add_range(sli.start, sli.stop, sli.step, sli.pad as u32);
                PyRangeSet { inner: rs }
            })
            .collect())
    }

    /// Iterate over RangeSet ranges as Python slice objects.
    fn slices(slf: PyRef<'_, Self>) -> PyResult<Py<PyList>> {
        let py = slf.py();
        let sorted = slf.inner.sorted();
        let autostep = internal_autostep(&slf.inner);
        let folded = slices_padding(&sorted, autostep, autostep);
        let list = PyList::empty_bound(py);
        for sli in folded {
            let slice =
                PySlice::new_bound(py, sli.start as isize, sli.stop as isize, sli.step as isize);
            list.append(slice)?;
        }
        Ok(list.into())
    }

    /// Return state information for pickling. Empty sets store a None
    /// pattern (upstream 9df4c5a) so that unpickling works.
    fn __reduce__(slf: PyRef<'_, Self>) -> PyResult<(Py<PyType>, (Option<String>,), Py<PyDict>)> {
        let py = slf.py();
        let pattern: Option<String> = if slf.inner.is_empty() {
            None
        } else {
            Some(slf.inner.to_string())
        };
        let state = PyDict::new_bound(py);
        state.set_item("padding", slf.inner.padding())?;
        state.set_item("_autostep", internal_autostep(&slf.inner))?;
        state.set_item("_version", RANGESET_VERSION)?;
        let cls: Py<PyType> = slf.py().get_type_bound::<PyRangeSet>().unbind();
        Ok((cls, (pattern,), state.unbind()))
    }

    /// Called upon unpickling: restore state, converting legacy formats.
    fn __setstate__(&mut self, _py: Python<'_>, dic: &Bound<'_, PyDict>) -> PyResult<()> {
        // restore internal autostep value
        if let Some(val) = dic.get_item("_autostep")? {
            let internal: f64 = val.extract()?;
            if internal >= AUTOSTEP_DISABLED || internal + 1.0 >= u32::MAX as f64 {
                self.inner.set_autostep(None);
            } else {
                self.inner.set_autostep(Some(internal as u32 + 1));
            }
        }
        let version: i32 = match dic.get_item("_version")? {
            Some(v) => v.extract().unwrap_or(0),
            None => 0,
        };
        if version < RANGESET_VERSION {
            // legacy formats (ClusterShell < 1.9): convert _ranges
            if let Some(ranges) = dic.get_item("_ranges")? {
                let ranges = ranges.downcast::<PyList>()?.clone();
                for entry in ranges.iter() {
                    let entry = entry.downcast::<PyTuple>()?.clone();
                    let first = entry.get_item(0)?;
                    let pad: usize = entry.get_item(entry.len() - 1)?.extract()?;
                    if let Ok(slice) = first.downcast::<PySlice>() {
                        // (slice(start, stop, step), pad)
                        let start: i64 = slice.getattr("start")?.extract()?;
                        let stop: i64 = slice.getattr("stop")?.extract()?;
                        let step: i64 = slice.getattr("step")?.extract()?;
                        self.inner.add_range(start, stop, step, pad as u32);
                    } else if entry.len() == 2 {
                        // v2: ((start, stop, step), pad)
                        let inner_t = first.downcast::<PyTuple>()?.clone();
                        let start: i64 = inner_t.get_item(0)?.extract()?;
                        let stop: i64 = inner_t.get_item(1)?.extract()?;
                        let step: i64 = inner_t.get_item(2)?.extract()?;
                        self.inner.add_range(start, stop, step, pad as u32);
                    } else {
                        // v1: (start, stop, step, pad) with inclusive stop
                        let start: i64 = first.extract()?;
                        let stop: i64 = entry.get_item(1)?.extract()?;
                        let step: i64 = entry.get_item(2)?.extract()?;
                        self.inner.add_range(start, stop + 1, step, pad as u32);
                    }
                }
            }
        }
        Ok(())
    }

    fn __getitem__(slf: PyRef<'_, Self>, index: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let sorted = slf.inner.sorted();
        if let Ok(slice) = index.downcast::<PySlice>() {
            let n = sorted.len() as isize;
            let indices = slice.indices(n)?;
            let mut rs = RustRangeSet::new();
            rs.set_autostep(slf.inner.autostep());
            let mut i = indices.start;
            if indices.step > 0 {
                while i < indices.stop {
                    rs.add_str(&sorted[i as usize]);
                    i += indices.step;
                }
            } else {
                while i > indices.stop {
                    rs.add_str(&sorted[i as usize]);
                    i += indices.step;
                }
            }
            let out = Py::new(py, PyRangeSet { inner: rs })?;
            return Ok(out.into_py(py));
        }
        if let Ok(idx) = index.extract::<i64>() {
            let n = sorted.len() as i64;
            let real = if idx < 0 { idx + n } else { idx };
            if real < 0 || real >= n {
                return Err(PyIndexError::new_err("list index out of range"));
            }
            return Ok(sorted[real as usize].clone().into_py(py));
        }
        Err(PyTypeError::new_err("RangeSet indices must be integers"))
    }

    /// Return the zero-based index of element in the sorted RangeSet.
    ///
    /// Reverse operation of __getitem__(), behaving like list.index().
    #[pyo3(signature = (elem, start=0, stop=None))]
    fn index(&self, elem: &Bound<'_, PyAny>, start: isize, stop: Option<isize>) -> PyResult<usize> {
        let key = elem.str()?.to_string();
        match self.inner.index_str_within(&key, start, stop) {
            Some(pos) => Ok(pos),
            None => Err(PyValueError::new_err(format!(
                "{} is not in RangeSet",
                elem.str()?
            ))),
        }
    }

    /// Split the rangeset into nbr sub-rangesets (at most).
    fn split(slf: PyRef<'_, Self>, nbr: usize) -> PyResult<Vec<PyRangeSet>> {
        if nbr == 0 {
            return Err(PyAssertionError::new_err("assertion failed"));
        }
        let sorted = slf.inner.sorted();
        let total = sorted.len();
        let slice_size = total / nbr;
        let leftover = total % nbr;
        let mut result = Vec::new();
        let mut begin = 0usize;
        for i in 0..nbr.min(total) {
            let length = slice_size + usize::from(i < leftover);
            let mut rs = RustRangeSet::new();
            rs.set_autostep(slf.inner.autostep());
            for e in &sorted[begin..begin + length] {
                rs.add_str(e);
            }
            result.push(PyRangeSet { inner: rs });
            begin += length;
        }
        Ok(result)
    }

    /// Add a range (start, stop, step, pad) to the RangeSet. Like range(),
    /// the last element is the largest start + i * step less than stop.
    #[pyo3(signature = (start, stop, step=1, pad=0))]
    fn add_range(&mut self, start: i64, stop: i64, step: i64, pad: u32) -> PyResult<()> {
        self.do_add_range(start, stop, step, pad)
    }

    /// Return a shallow copy of this RangeSet.
    fn copy(&self) -> Self {
        self.clone()
    }

    fn __copy__(&self) -> Self {
        self.clone()
    }

    fn __eq__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        match other.downcast::<PyRangeSet>() {
            Ok(rs) => Ok((self.inner == rs.borrow().inner).into_py(py)),
            Err(_) => Ok(py.NotImplemented().into_py(py)),
        }
    }

    fn __ne__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        match other.downcast::<PyRangeSet>() {
            Ok(rs) => Ok((self.inner != rs.borrow().inner).into_py(py)),
            Err(_) => Ok(py.NotImplemented().into_py(py)),
        }
    }

    /// Check that the other argument to a binary operation is also a set.
    fn _binary_sanity_check(&self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        if other.downcast::<PyRangeSet>().is_err()
            && other.downcast::<pyo3::types::PySet>().is_err()
            && other.downcast::<pyo3::types::PyFrozenSet>().is_err()
        {
            return Err(PyTypeError::new_err(
                "Binary operation only permitted between sets",
            ));
        }
        Ok(())
    }

    fn __or__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        if other.downcast::<PyRangeSet>().is_err()
            && other.downcast::<pyo3::types::PySet>().is_err()
            && other.downcast::<pyo3::types::PyFrozenSet>().is_err()
        {
            return Ok(py.NotImplemented().into_py(py));
        }
        let mut rs = self.inner.clone();
        for e in setlike_to_strings(other)? {
            rs.add_str(&e);
        }
        Ok(Py::new(py, PyRangeSet { inner: rs })?.into_py(py))
    }

    fn union(&self, other: &Bound<'_, PyAny>) -> PyResult<PyRangeSet> {
        let mut rs = self.inner.clone();
        for e in setlike_to_strings(other)? {
            rs.add_str(&e);
        }
        Ok(PyRangeSet { inner: rs })
    }

    fn __and__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        if other.downcast::<PyRangeSet>().is_err()
            && other.downcast::<pyo3::types::PySet>().is_err()
            && other.downcast::<pyo3::types::PyFrozenSet>().is_err()
        {
            return Ok(py.NotImplemented().into_py(py));
        }
        let elems: std::collections::HashSet<String> =
            setlike_to_strings(other)?.into_iter().collect();
        let kept: Vec<String> = self
            .inner
            .sorted()
            .into_iter()
            .filter(|e| elems.contains(e))
            .collect();
        let rs = rs_from_elements(kept, self.inner.autostep());
        Ok(Py::new(py, PyRangeSet { inner: rs })?.into_py(py))
    }

    fn intersection(&self, other: &Bound<'_, PyAny>) -> PyResult<PyRangeSet> {
        let elems: std::collections::HashSet<String> =
            setlike_to_strings(other)?.into_iter().collect();
        let kept: Vec<String> = self
            .inner
            .sorted()
            .into_iter()
            .filter(|e| elems.contains(e))
            .collect();
        Ok(PyRangeSet {
            inner: rs_from_elements(kept, self.inner.autostep()),
        })
    }

    fn __xor__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        if other.downcast::<PyRangeSet>().is_err()
            && other.downcast::<pyo3::types::PySet>().is_err()
            && other.downcast::<pyo3::types::PyFrozenSet>().is_err()
        {
            return Ok(py.NotImplemented().into_py(py));
        }
        Ok(Py::new(py, self.symmetric_difference(other)?)?.into_py(py))
    }

    fn symmetric_difference(&self, other: &Bound<'_, PyAny>) -> PyResult<PyRangeSet> {
        let other_elems: std::collections::HashSet<String> =
            setlike_to_strings(other)?.into_iter().collect();
        let self_elems: std::collections::HashSet<String> =
            self.inner.sorted().into_iter().collect();
        let sym: std::collections::HashSet<String> = self_elems
            .symmetric_difference(&other_elems)
            .cloned()
            .collect();
        Ok(PyRangeSet {
            inner: rs_from_elements(sym.into_iter().collect(), self.inner.autostep()),
        })
    }

    fn __sub__(&self, other: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = other.py();
        if other.downcast::<PyRangeSet>().is_err()
            && other.downcast::<pyo3::types::PySet>().is_err()
            && other.downcast::<pyo3::types::PyFrozenSet>().is_err()
        {
            return Ok(py.NotImplemented().into_py(py));
        }
        Ok(Py::new(py, self.difference(other)?)?.into_py(py))
    }

    fn difference(&self, other: &Bound<'_, PyAny>) -> PyResult<PyRangeSet> {
        let elems: std::collections::HashSet<String> =
            setlike_to_strings(other)?.into_iter().collect();
        let kept: Vec<String> = self
            .inner
            .sorted()
            .into_iter()
            .filter(|e| !elems.contains(e))
            .collect();
        Ok(PyRangeSet {
            inner: rs_from_elements(kept, self.inner.autostep()),
        })
    }

    fn issubset(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self._binary_sanity_check(other)?;
        let elems: std::collections::HashSet<String> =
            setlike_to_strings(other)?.into_iter().collect();
        Ok(self.inner.sorted().iter().all(|e| elems.contains(e)))
    }

    fn issuperset(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self._binary_sanity_check(other)?;
        let elems = setlike_to_strings(other)?;
        Ok(elems.iter().all(|e| self.inner.contains_str(e)))
    }

    fn __lt__(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self._binary_sanity_check(other)?;
        let other_len = if let Ok(rs) = other.downcast::<PyRangeSet>() {
            rs.borrow().inner.len()
        } else {
            other.len()?
        };
        Ok(self.inner.len() < other_len && self.issubset(other)?)
    }

    fn __gt__(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self._binary_sanity_check(other)?;
        let other_len = if let Ok(rs) = other.downcast::<PyRangeSet>() {
            rs.borrow().inner.len()
        } else {
            other.len()?
        };
        Ok(self.inner.len() > other_len && self.issuperset(other)?)
    }

    fn __le__(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.issubset(other)
    }

    fn __ge__(&self, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        self.issuperset(other)
    }

    fn __ior__(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self._binary_sanity_check(other)?;
        let snapshot = self.inner.sorted();
        for e in setlike_to_strings_inplace(other, &snapshot)? {
            self.inner.add_str(&e);
        }
        Ok(())
    }

    fn union_update(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self.update(other)
    }

    fn __iand__(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self._binary_sanity_check(other)?;
        self.intersection_update(other)
    }

    fn intersection_update(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        let snapshot = self.inner.sorted();
        let elems: std::collections::HashSet<String> =
            setlike_to_strings_inplace(other, &snapshot)?
                .into_iter()
                .collect();
        let kept: Vec<String> = self
            .inner
            .sorted()
            .into_iter()
            .filter(|e| elems.contains(e))
            .collect();
        self.inner = rs_from_elements(kept, self.inner.autostep());
        Ok(())
    }

    fn __ixor__(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self._binary_sanity_check(other)?;
        self.symmetric_difference_update(other)
    }

    fn symmetric_difference_update(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        let snapshot = self.inner.sorted();
        let other_elems: std::collections::HashSet<String> =
            setlike_to_strings_inplace(other, &snapshot)?
                .into_iter()
                .collect();
        let self_elems: std::collections::HashSet<String> =
            self.inner.sorted().into_iter().collect();
        let sym: std::collections::HashSet<String> = self_elems
            .symmetric_difference(&other_elems)
            .cloned()
            .collect();
        self.inner = rs_from_elements(sym.into_iter().collect(), self.inner.autostep());
        Ok(())
    }

    fn __isub__(&mut self, other: &Bound<'_, PyAny>) -> PyResult<()> {
        self._binary_sanity_check(other)?;
        self.difference_update(other, false)
    }

    /// Remove all elements of another set from this RangeSet. If strict is
    /// True, raise KeyError if an element cannot be removed.
    #[pyo3(signature = (other, strict=false))]
    fn difference_update(&mut self, other: &Bound<'_, PyAny>, strict: bool) -> PyResult<()> {
        let snapshot = self.inner.sorted();
        if strict && !self.__contains__(other)? {
            // raise KeyError of first missing element
            let elems = setlike_to_strings_inplace(other, &snapshot)?;
            for e in &elems {
                if !self.inner.contains_str(e) {
                    return Err(PyKeyError::new_err(e.clone()));
                }
            }
        }
        let elems: std::collections::HashSet<String> =
            setlike_to_strings_inplace(other, &snapshot)?
                .into_iter()
                .collect();
        let kept: Vec<String> = self
            .inner
            .sorted()
            .into_iter()
            .filter(|e| !elems.contains(e))
            .collect();
        self.inner = rs_from_elements(kept, self.inner.autostep());
        Ok(())
    }

    /// Add all indexes (as strings) from an iterable.
    fn update(&mut self, iterable: &Bound<'_, PyAny>) -> PyResult<()> {
        if iterable.extract::<String>().is_ok() {
            return Err(PyAssertionError::new_err("assertion failed"));
        }
        let snapshot = self.inner.sorted();
        for e in setlike_to_strings_inplace(iterable, &snapshot)? {
            self.inner.add_str(&e);
        }
        Ok(())
    }

    /// Update a rangeset with the union of itself and several others.
    fn updaten(&mut self, py: Python<'_>, rangesets: &Bound<'_, PyAny>) -> PyResult<()> {
        self.do_updaten(py, rangesets)
    }

    /// Remove all elements from this RangeSet.
    fn clear(&mut self) {
        self.inner = rs_from_elements(Vec::new(), self.inner.autostep());
    }

    /// Add an element to the RangeSet (string, or integer with padding).
    #[pyo3(signature = (element, pad=0))]
    fn add(&mut self, element: &Bound<'_, PyAny>, pad: u32) -> PyResult<()> {
        self.do_add(element, pad)
    }

    /// Remove an element from the RangeSet.
    #[pyo3(signature = (element, pad=0))]
    fn remove(&mut self, element: &Bound<'_, PyAny>, pad: u32) -> PyResult<()> {
        let key = if let Ok(s) = element.extract::<String>() {
            s
        } else {
            let value: i64 = element.extract().map_err(|_| {
                PyValueError::new_err(format!(
                    "invalid literal for int(): {}",
                    element.str().unwrap()
                ))
            })?;
            if pad > 0 {
                format!("{:0>width$}", value, width = pad as usize)
            } else {
                format!("{}", value)
            }
        };
        if !self.inner.contains_str(&key) {
            return Err(PyKeyError::new_err(key));
        }
        self.inner.discard_str(&key);
        Ok(())
    }

    /// Discard an element from the RangeSet if present.
    #[pyo3(signature = (element, pad=0))]
    fn discard(&mut self, element: &Bound<'_, PyAny>, pad: u32) -> PyResult<()> {
        if let Ok(s) = element.extract::<String>() {
            self.inner.discard_str(&s);
        } else if let Ok(value) = element.extract::<i64>() {
            let key = if pad > 0 {
                format!("{:0>width$}", value, width = pad as usize)
            } else {
                format!("{}", value)
            };
            self.inner.discard_str(&key);
        }
        // ignore other object types
        Ok(())
    }

    /// Get largest padding value of whole set (property getter).
    #[getter]
    fn get_padding(&self) -> Option<usize> {
        self.inner.padding()
    }

    /// Force padding length on the whole set (property setter).
    #[setter]
    fn set_padding(&mut self, value: Option<usize>) -> PyResult<()> {
        let pad = value.unwrap_or(1);
        let elems: Vec<i64> = self
            .inner
            .sorted()
            .iter()
            .map(|s| s.parse::<i64>().unwrap_or(0))
            .collect();
        let autostep = self.inner.autostep();
        let mut rs = RustRangeSet::new();
        rs.set_autostep(autostep);
        for v in elems {
            rs.add_int(v, pad as u32);
        }
        self.inner = rs;
        Ok(())
    }

    /// Get autostep value (property getter): None when disabled.
    #[getter(autostep)]
    fn get_autostep_prop(&self) -> Option<u32> {
        self.inner.autostep()
    }

    /// Set autostep value (property setter): accepts None, int or float.
    #[setter(autostep)]
    fn set_autostep_prop(&mut self, val: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        self.inner.set_autostep(extract_autostep(val)?);
        Ok(())
    }

    /// Get autostep value (method form).
    fn get_autostep(&self) -> Option<u32> {
        self.inner.autostep()
    }

    /// Set autostep value (method form): accepts None, int or float.
    #[pyo3(signature = (val=None))]
    fn set_autostep(&mut self, val: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        self.inner.set_autostep(extract_autostep(val)?);
        Ok(())
    }
}

#[pyclass]
struct RangeSetStrIterator {
    values: Vec<String>,
    index: usize,
}

#[pymethods]
impl RangeSetStrIterator {
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

/// Register range_set types into the parent module.
pub fn register(parent: &Bound<'_, PyModule>) -> PyResult<()> {
    parent.add_class::<PyRangeSet>()?;
    parent.add(
        "RangeSetException",
        parent.py().get_type_bound::<RangeSetException>(),
    )?;
    parent.add(
        "RangeSetParseError",
        parent.py().get_type_bound::<RangeSetParseError>(),
    )?;
    parent.add(
        "RangeSetPaddingError",
        parent.py().get_type_bound::<RangeSetPaddingError>(),
    )?;
    Ok(())
}
