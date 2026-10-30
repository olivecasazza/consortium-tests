"""ClusterShell.RangeSet — backend-aware shim.

When CONSORTIUM_BACKEND=rust (default), the 1D `RangeSet` class and the
exception hierarchy come from the Rust PyO3 bindings (`ClusterShell._consortium`,
backed by `consortium::range_set`).

`RangeSetND` is provided here as a pure-Python port of the upstream
implementation (lib/ClusterShell/RangeSet.py, ClusterShell 1.10.1) operating
on the Rust-backed 1D RangeSet, because the Rust core does not implement
n-dimensional folding (merging/sorting of overlapping vectors) yet.

When CONSORTIUM_BACKEND=python, this file is never reached (the __init__.py
redirects the entire ClusterShell package to the original pure-Python source).
"""

from functools import reduce
from itertools import product
from operator import mul

from ClusterShell._consortium import (
    RangeSet,
    RangeSetException,
    RangeSetParseError,
    RangeSetPaddingError,
)

# Python 3 compatibility
try:
    basestring
except NameError:
    basestring = str

__all__ = ['RangeSetException',
           'RangeSetParseError',
           'RangeSetPaddingError',
           'RangeSet',
           'RangeSetND',
           'AUTOSTEP_DISABLED']

# Special constant used to force turn off autostep feature.
# Note: +inf is 1E400, but a bug in python 2.4 makes it impossible to be
# pickled, so we use less. Later, we could consider sys.maxint here.
AUTOSTEP_DISABLED = 1E100


def _normalized_index_bounds(length, start, stop):
    """Return list.index()-like normalized start/stop bounds."""
    if start < 0:
        start = max(0, length + start)
    if stop is None:
        stop = length
    elif stop < 0:
        stop = max(0, length + stop)
    return start, stop


def _set_rs_autostep_internal(rg, internal):
    """Set a Rust-backed RangeSet autostep from the internal (value - 1)
    representation used by RangeSetND."""
    if internal >= AUTOSTEP_DISABLED:
        rg.autostep = None
    else:
        rg.autostep = int(internal) + 1


class RangeSetND(object):
    """
    Build a N-dimensional RangeSet object.

    .. warning:: You don't usually need to use this class directly, use
        :class:`.NodeSet` instead that has ND support.

    Empty constructor::

        RangeSetND()

    Build from a list of list of :class:`RangeSet` objects::

        RangeSetND([[rs1, rs2, rs3, ...], ...])

    Strings are also supported::

        RangeSetND([["0-3", "4-10", ...], ...])

    Integers are also supported::

        RangeSetND([(0, 4), (0, 5), (1, 4), (1, 5), ...]
    """
    def __init__(self, args=None, pads=None, autostep=None, copy_rangeset=True):
        """RangeSetND initializer

        All parameters are optional.

        :param args: generic "list of list" input argument (default is None)
        :param pads: list of 0-padding length (default is to not pad any
                     dimensions)
        :param autostep: autostep threshold (use range/step notation if more
                         than #autostep items meet the condition) - default is
                         off (None)
        :param copy_rangeset: (advanced) if set to False, do not copy RangeSet
                              objects from args (transfer ownership), which is
                              faster. In that case, you should not modify these
                              objects afterwards (default is True).
        """
        # RangeSetND are arranged as a list of N-dimensional RangeSet vectors
        self._veclist = []
        # Dirty flag to avoid doing veclist folding too often
        self._dirty = True
        # Initialize autostep through property
        self._autostep = None
        self.autostep = autostep #: autostep threshold public instance attribute
        # Hint on whether several dimensions are varying or not
        self._multivar_hint = False
        if args is None:
            return
        for rgvec in args:
            if rgvec:
                if isinstance(rgvec[0], basestring):
                    self._veclist.append([RangeSet(rg, autostep=autostep) \
                                          for rg in rgvec])
                elif isinstance(rgvec[0], RangeSet):
                    if copy_rangeset:
                        self._veclist.append([rg.copy() for rg in rgvec])
                    else:
                        self._veclist.append(rgvec)
                else:
                    if pads is None:
                        self._veclist.append( \
                            [RangeSet.fromone(rg, autostep=autostep) \
                                for rg in rgvec])
                    else:
                        self._veclist.append( \
                            [RangeSet.fromone(rg, pad, autostep) \
                                for rg, pad in zip(rgvec, pads)])

    class precond_fold(object):
        """Decorator to ease internal folding management"""
        def __call__(self, func):
            def inner(*args, **kwargs):
                rgnd, fargs = args[0], args[1:]
                if rgnd._dirty:
                    rgnd._fold()
                return func(rgnd, *fargs, **kwargs)
            # modify the decorator meta-data for pydoc
            inner.__name__ = func.__name__
            inner.__doc__ = func.__doc__
            inner.__dict__ = func.__dict__
            inner.__module__ = func.__module__
            return inner

    @precond_fold()
    def copy(self):
        """Return a new, mutable shallow copy of a RangeSetND."""
        cpy = self.__class__()
        # Shallow "to the extent possible" says the copy module, so here that
        # means calling copy() on each sub-RangeSet to keep mutability.
        cpy._veclist = [[rg.copy() for rg in rgvec] for rgvec in self._veclist]
        cpy._dirty = self._dirty
        return cpy

    __copy__ = copy # For the copy module

    def __eq__(self, other):
        """RangeSetND equality comparison."""
        # Return NotImplemented instead of raising TypeError, to
        # indicate that the comparison is not implemented with respect
        # to the other type (the other comparand then gets a change to
        # determine the result, then it falls back to object address
        # comparison).
        if not isinstance(other, RangeSetND):
            return NotImplemented
        return len(self) == len(other) and self.issubset(other)

    def __bool__(self):
        return bool(self._veclist)

    __nonzero__ = __bool__  # Python 2 compat

    def __len__(self):
        """Count unique elements in N-dimensional rangeset."""
        return sum([reduce(mul, [len(rg) for rg in rgvec]) \
                                 for rgvec in self.veclist])

    @precond_fold()
    def __str__(self):
        """String representation of N-dimensional RangeSet."""
        result = ""
        for rgvec in self._veclist:
            result += "; ".join([str(rg) for rg in rgvec])
            result += "\n"
        return result

    @precond_fold()
    def __iter__(self):
        return self._iter()

    def _iter(self):
        """Iterate through individual items as tuples."""
        for vec in self._veclist:
            for ivec in product(*vec):
                yield ivec

    @precond_fold()
    def iter_padding(self):
        """Iterate through individual items as tuples with padding info.
        As of v1.9, this method returns the largest padding value of each
        items, as mixed length padding is allowed."""
        for vec in self._veclist:
            for ivec in product(*vec):
                yield ivec, [rg.padding for rg in vec]

    @precond_fold()
    def _get_veclist(self):
        """Get folded veclist"""
        return self._veclist

    def _set_veclist(self, val):
        """Set veclist and set dirty flag for deferred folding."""
        self._veclist = val
        self._dirty = True

    veclist = property(_get_veclist, _set_veclist)

    def vectors(self):
        """Get underlying :class:`RangeSet` vectors"""
        return iter(self.veclist)

    def dim(self):
        """Get the current number of dimensions of this RangeSetND
        object.  Return 0 when object is empty."""
        try:
            return len(self._veclist[0])
        except IndexError:
            return 0

    def pads(self):
        """Get a tuple of padding length info for each dimension."""
        # return a tuple of max padding length for each axis
        pad_veclist = ((rg.padding or 0 for rg in vec) for vec in self._veclist)
        return tuple(max(pads) for pads in zip(*pad_veclist))

    def get_autostep(self):
        """Get autostep value (property)"""
        if self._autostep >= AUTOSTEP_DISABLED:
            return None
        else:
            # +1 as user wants node count but _autostep means real steps here
            return self._autostep + 1

    def set_autostep(self, val):
        """Set autostep value (property)"""
        # Must conform to RangeSet.autostep logic
        if val is None:
            self._autostep = AUTOSTEP_DISABLED
        else:
            # Like in RangeSet.set_autostep(): -1 because user means node count,
            # but we mean real steps (this operation has no effect on
            # AUTOSTEP_DISABLED value)
            self._autostep = int(val) - 1

        # Update our RangeSet objects
        for rgvec in self._veclist:
            for rg in rgvec:
                _set_rs_autostep_internal(rg, self._autostep)

    autostep = property(get_autostep, set_autostep)

    @precond_fold()
    def __getitem__(self, index):
        """
        Return the element at index or a subrange when a slice is specified.
        """
        if isinstance(index, slice):
            iveclist = []
            for rgvec in self._veclist:
                iveclist += product(*rgvec)
            assert(len(iveclist) == len(self))
            rnd = RangeSetND(iveclist[index], autostep=self.autostep)
            return rnd

        elif isinstance(index, int):
            # find a tuple of integer (multi-dimensional) at position index
            if index < 0:
                length = len(self)
                if index >= -length:
                    index = length + index
                else:
                    raise IndexError("%d out of range" % index)
            length = 0
            for rgvec in self._veclist:
                cnt = reduce(mul, [len(rg) for rg in rgvec])
                if length + cnt < index:
                    length += cnt
                else:
                    for ivec in product(*rgvec):
                        if index == length:
                            return ivec
                        length += 1
            raise IndexError("%d out of range" % index)
        else:
            raise TypeError("%s indices must be integers" %
                            self.__class__.__name__)

    @precond_fold()
    def index(self, elem, start=0, stop=None):
        """
        Return the zero-based index of element in this RangeSetND, following
        iteration order. This is the reverse operation of
        :meth:`RangeSetND.__getitem__` and behaves like the ``index()`` method
        of the ``list`` type.

        The element is a vector (tuple) of indexes, given as integers or
        strings (zero-padding is then significant). The optional `start` and
        `stop` arguments restrict the search to the matching subsequence, and
        may be negative (counted from the end).

        :raises TypeError: the element is not a vector of indexes
        :raises ValueError: the element is not present in the RangeSetND
        """
        # a bare string or scalar is not a valid index vector: iterating over
        # a string would otherwise wrongly split it into per-character indexes
        # (basestring catches both str and Python 2 unicode strings)
        if isinstance(elem, basestring) or not hasattr(elem, '__iter__'):
            raise TypeError("%s.index() argument must be a vector of indexes"
                            % self.__class__.__name__)
        target = tuple("%s" % e for e in elem)
        for pos, ivec in enumerate(self._iter()):
            if ivec == target:
                if start != 0 or stop is not None:
                    start, stop = _normalized_index_bounds(len(self),
                                                           start, stop)
                    if not start <= pos < stop:
                        break
                return pos
        raise ValueError("%s is not in RangeSetND" % (elem,))

    @precond_fold()
    def contiguous(self):
        """Object-based iterator over contiguous range sets."""
        veclist = self._veclist
        try:
            dim = len(veclist[0])
        except IndexError:
            return
        for dimidx in range(dim):
            new_veclist = []
            for rgvec in veclist:
                for rgsli in rgvec[dimidx].contiguous():
                    rgvec = list(rgvec)
                    rgvec[dimidx] = rgsli
                    new_veclist.append(rgvec)
            veclist = new_veclist
        for rgvec in veclist:
            yield RangeSetND([rgvec])

    # Membership test

    @precond_fold()
    def __contains__(self, element):
        """Report whether an element is a member of a RangeSetND.
        Element can be either another RangeSetND object, a string or
        an integer.

        Called in response to the expression ``element in self``.
        """
        if isinstance(element, RangeSetND):
            rgnd_element = element
        else:
            rgnd_element = RangeSetND([[str(element)]])
        return rgnd_element.issubset(self)

    # Subset and superset test

    def issubset(self, other):
        """Report whether another set contains this RangeSetND."""
        self._binary_sanity_check(other)
        return other.issuperset(self)

    @precond_fold()
    def issuperset(self, other):
        """Report whether this RangeSetND contains another RangeSetND."""
        self._binary_sanity_check(other)
        if self.dim() == 1 and other.dim() == 1:
            return self._veclist[0][0].issuperset(other._veclist[0][0])
        if not other._veclist:
            return True
        test = other.copy()
        test.difference_update(self)
        return not bool(test)

    # Inequality comparisons using the is-subset relation.
    __le__ = issubset
    __ge__ = issuperset

    def __lt__(self, other):
        self._binary_sanity_check(other)
        return len(self) < len(other) and self.issubset(other)

    def __gt__(self, other):
        self._binary_sanity_check(other)
        return len(self) > len(other) and self.issuperset(other)

    # Assorted helpers

    def _binary_sanity_check(self, other):
        """Check that the other argument to a binary operation is also a
        RangeSetND, raising a TypeError otherwise."""
        if not isinstance(other, RangeSetND):
            msg = "Binary operation only permitted between RangeSetND"
            raise TypeError(msg)

    def _sort(self):
        """N-dimensional sorting."""
        def rgveckeyfunc(rgvec):
            # key used for sorting purposes, based on the following
            # conditions:
            #   (1) larger vector first (#elements)
            #   (2) larger dim first  (#elements)
            #   (3) lower first index first
            #   (4) lower last index first
            return (-reduce(mul, [len(rg) for rg in rgvec]), \
                    tuple((-len(rg), rg[0], rg[-1]) for rg in rgvec))
        self._veclist.sort(key=rgveckeyfunc)

    @precond_fold()
    def fold(self):
        """Explicit folding call. Please note that folding of RangeSetND
        nD vectors are automatically managed, so you should not have to
        call this method. It may be still useful in some extreme cases
        where the RangeSetND is heavily modified."""
        pass

    def _fold(self):
        """In-place N-dimensional folding."""
        assert self._dirty
        if len(self._veclist) > 1:
            self._fold_univariate() or self._fold_multivariate()
        else:
            self._dirty = False

    def _fold_univariate(self):
        """Univariate nD folding. Return True on success and False when
        a multivariate folding is required."""
        dim = self.dim()
        vardim = dimdiff = 0
        if dim > 1:
            # We got more than one dimension, see if only one is changing...
            for i in range(dim):
                # Are all rangesets on this dimension the same?
                slist = [vec[i] for vec in self._veclist]
                if slist.count(slist[0]) != len(slist):
                    dimdiff += 1
                    if dimdiff > 1:
                        break
                    vardim = i
        univar = (dim == 1 or dimdiff == 1)
        if univar:
            # Eligible for univariate folding (faster!)
            for vec in self._veclist[1:]:
                self._veclist[0][vardim].update(vec[vardim])
            del self._veclist[1:]
            self._dirty = False
        self._multivar_hint = not univar
        return univar

    def _fold_multivariate(self):
        """Multivariate nD folding"""
        # PHASE 1: expand with respect to uniqueness
        self._fold_multivariate_expand()
        # PHASE 2: merge
        self._fold_multivariate_merge()
        self._dirty = False

    def _fold_multivariate_expand(self):
        """Multivariate nD folding: expand [phase 1]"""
        self._veclist = [[RangeSet.fromone(i, autostep=self.autostep)
                          for i in tvec]
                         for tvec in set(self._iter())]

    def _fold_multivariate_merge(self):
        """Multivariate nD folding: merge [phase 2]"""
        full = False  # try easy O(n) passes first
        chg = True    # new pass (eg. after change on veclist)
        while chg:
            chg = False
            self._sort()  # sort veclist before new pass
            index1, index2 = 0, 1
            while (index1 + 1) < len(self._veclist):
                # use 2 references on iterator to compare items by couples
                item1 = self._veclist[index1]
                index2 = index1 + 1
                index1 += 1
                while index2 < len(self._veclist):
                    item2 = self._veclist[index2]
                    index2 += 1
                    new_item = [None] * len(item1)
                    nb_diff = 0
                    # compare 2 rangeset vector, item by item, the idea being
                    # to merge vectors if they differ only by one item
                    for pos, (rg1, rg2) in enumerate(zip(item1, item2)):
                        if rg1 == rg2:
                            new_item[pos] = rg1
                        elif not rg1 & rg2: # merge on disjoint ranges
                            nb_diff += 1
                            if nb_diff > 1:
                                break
                            new_item[pos] = rg1 | rg2
                        # if fully contained, keep the largest one
                        elif (rg1 > rg2 or rg1 < rg2): # and nb_diff == 0:
                            nb_diff += 1
                            if nb_diff > 1:
                                break
                            new_item[pos] = max(rg1, rg2)
                        # otherwise, compute rangeset intersection and
                        # keep the two disjoint part to be handled
                        # later...
                        else:
                            # intersection but do nothing
                            nb_diff = 2
                            break
                    # one change has been done: use this new item to compare
                    # with other
                    if nb_diff <= 1:
                        chg = True
                        item1 = self._veclist[index1 - 1] = new_item
                        index2 -= 1
                        self._veclist.pop(index2)
                    elif not full:
                        # easy pass so break to avoid scanning all
                        # index2; advance with next index1 for now
                        break
            if not chg and not full:
                # if no change was done during the last normal pass, we do a
                # full O(n^2) pass. This pass is done only at the end in the
                # hope that most vectors have already been merged by easy
                # O(n) passes.
                chg = full = True

    def __or__(self, other):
        """Return the union of two RangeSetNDs as a new RangeSetND.

        (I.e. all elements that are in either set.)
        """
        if not isinstance(other, RangeSetND):
            return NotImplemented
        return self.union(other)

    def union(self, other):
        """Return the union of two RangeSetNDs as a new RangeSetND.

        (I.e. all elements that are in either set.)
        """
        rgnd_copy = self.copy()
        rgnd_copy.update(other)
        return rgnd_copy

    def update(self, other):
        """Add all RangeSetND elements to this RangeSetND."""
        if isinstance(other, RangeSetND):
            iterable = other._veclist
        else:
            iterable = other
        for vec in iterable:
            # copy rangesets and set custom autostep
            assert isinstance(vec[0], RangeSet)
            cpyvec = []
            for rg in vec:
                cpyrg = rg.copy()
                cpyrg.autostep = self.autostep
                cpyvec.append(cpyrg)
            self._veclist.append(cpyvec)
        self._dirty = True
        if not self._multivar_hint:
            self._fold_univariate()

    union_update = update

    def __ior__(self, other):
        """Update a RangeSetND with the union of itself and another."""
        self._binary_sanity_check(other)
        self.update(other)
        return self

    def __isub__(self, other):
        """Remove all elements of another set from this RangeSetND."""
        self._binary_sanity_check(other)
        self.difference_update(other)
        return self

    def difference_update(self, other, strict=False):
        """Remove all elements of another set from this RangeSetND.

        If strict is True, raise KeyError if an element cannot be removed
        (strict is a RangeSet addition)"""
        if strict and not other in self:
            raise KeyError(other.difference(self)[0])

        ergvx = other._veclist # read only
        rgnd_new = []
        index1 = 0
        while index1 < len(self._veclist):
            rgvec1 = self._veclist[index1]
            procvx1 = [ rgvec1 ]
            nextvx1 = []
            index2 = 0
            while index2 < len(ergvx):
                rgvec2 = ergvx[index2]
                while len(procvx1) > 0: # refine diff for each resulting vector
                    rgproc1 = procvx1.pop(0)
                    tmpvx = []
                    for pos, (rg1, rg2) in enumerate(zip(rgproc1, rgvec2)):
                        if rg1 == rg2 or rg1 < rg2: # issubset
                            pass
                        elif rg1 & rg2:             # intersect
                            tmpvec = list(rgproc1)
                            tmpvec[pos] = rg1.difference(rg2)
                            tmpvx.append(tmpvec)
                        else:                       # disjoint
                            tmpvx = [ rgproc1 ]     # reset previous work
                            break
                    if tmpvx:
                        nextvx1 += tmpvx
                if nextvx1:
                    procvx1 = nextvx1
                    nextvx1 = []
                index2 += 1
            if procvx1:
                rgnd_new += procvx1
            index1 += 1
        self.veclist = rgnd_new

    def __sub__(self, other):
        """Return the difference of two RangeSetNDs as a new RangeSetND.

        (I.e. all elements that are in this set and not in the other.)
        """
        if not isinstance(other, RangeSetND):
            return NotImplemented
        return self.difference(other)

    def difference(self, other):
        """
        ``s.difference(t)`` returns a new object with elements in s
        but not in t.
        """
        self_copy = self.copy()
        self_copy.difference_update(other)
        return self_copy

    def intersection(self, other):
        """
        ``s.intersection(t)`` returns a new object with elements common
        to s and t.
        """
        self_copy = self.copy()
        self_copy.intersection_update(other)
        return self_copy

    def __and__(self, other):
        """
        Implements the & operator. So ``s & t`` returns a new object
        with elements common to s and t.
        """
        if not isinstance(other, RangeSetND):
            return NotImplemented
        return self.intersection(other)

    def intersection_update(self, other):
        """
        ``s.intersection_update(t)`` returns nodeset s keeping only
        elements also found in t.
        """
        if other is self:
            return

        tmp_rnd = RangeSetND()

        empty_rset = RangeSet()

        for rgvec in self._veclist:
            for ergvec in other._veclist:
                irgvec = [rg.intersection(erg) \
                            for rg, erg in zip(rgvec, ergvec)]
                if not empty_rset in irgvec:
                    tmp_rnd.update([irgvec])
        # substitute
        self.veclist = tmp_rnd.veclist

    def __iand__(self, other):
        """
        Implements the &= operator. So ``s &= t`` returns object s
        keeping only elements also found in t (Python 2.5+ required).
        """
        self._binary_sanity_check(other)
        self.intersection_update(other)
        return self

    def symmetric_difference(self, other):
        """
        ``s.symmetric_difference(t)`` returns the symmetric difference
        of two objects as a new RangeSetND.

        (ie. all items that are in exactly one of the RangeSetND.)
        """
        self_copy = self.copy()
        self_copy.symmetric_difference_update(other)
        return self_copy

    def __xor__(self, other):
        """
        Implement the ^ operator. So ``s ^ t`` returns a new RangeSetND
        with nodes that are in exactly one of the RangeSetND.
        """
        if not isinstance(other, RangeSetND):
            return NotImplemented
        return self.symmetric_difference(other)

    def symmetric_difference_update(self, other):
        """
        ``s.symmetric_difference_update(t)`` returns RangeSetND s
        keeping all nodes that are in exactly one of the objects.
        """
        diff2 = other.difference(self)
        self.difference_update(other)
        self.update(diff2)

    def __ixor__(self, other):
        """
        Implement the ^= operator. So ``s ^= t`` returns object s after
        keeping all items that are in exactly one of the RangeSetND
        (Python 2.5+ required).
        """
        self._binary_sanity_check(other)
        self.symmetric_difference_update(other)
        return self
