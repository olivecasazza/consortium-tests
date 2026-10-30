"""Fallback: re-export the original pure-Python ClusterShell modules.

This sub-package is a symlink/copy bridge so that any symbol not yet
ported to Rust can be imported from `ClusterShell._py.ModuleName`.
The actual source lives in this test repository at `lib/ClusterShell/`.
"""

import sys
import os

# Add the original lib/ directory to sys.path so imports resolve.
# Search order: $LIB_CLUSTERSHELL, then this repository's lib/.
_repo_root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_candidates = [
    os.environ.get("LIB_CLUSTERSHELL"),
    os.path.join(_repo_root, "lib"),
]
for _cand in _candidates:
    if _cand and os.path.isdir(_cand):
        _lib_dir = os.path.normpath(_cand)
        if _lib_dir not in sys.path:
            sys.path.insert(0, _lib_dir)
        break

# Re-export everything from the original package under the _py namespace.
# Each wrapper module (e.g. ClusterShell/RangeSet.py) imports from
# ClusterShell._py.RangeSet which triggers the original code.
import importlib as _importlib

def __getattr__(name):
    """Lazily import original ClusterShell submodules."""
    try:
        mod = _importlib.import_module(f"ClusterShell.{name}", package=None)
        return mod
    except ImportError:
        raise AttributeError(f"module 'ClusterShell._py' has no attribute {name!r}")
