"""On the path only while build.sh builds gssapi and krb5 (SDIST_ONLY): makes Cython's type
inference deterministic, so the same sources give the same C every build.

Cython 3.3.0 resolves circular assignments (`value += ...` inside a loop) by iterating Python
sets of parse-tree nodes, which hash by memory address: which of two equally valid C helpers it
then picks depends on the build process's memory layout, which follows from what it did before
(which standard-library .pyc files existed, the order a directory was listed in). gssapi's
oids.pyx came out with __Pyx_PyNumber_InPlaceAdd_int_object in one build and ..._object_object
in the next, so the two AppImages differed. Here the sets of TypeInference.py, the plain-Python
module that does this, iterate in insertion order: the order of the assignments in the source.
(Turning type inference off instead doesn't build: both packages rely on it for C types.)"""


class _InsertionOrderedSet(dict):
    """The set operations TypeInference.infer_types uses, iterating in insertion order."""

    def __init__(self, items=()):
        super().__init__()
        self.update(items)

    def add(self, item):
        self[item] = None

    def update(self, *iterables):
        for iterable in iterables:
            for item in iterable:
                self[item] = None

    def difference_update(self, *iterables):
        for iterable in iterables:
            for item in iterable:
                self.pop(item, None)

    def discard(self, item):
        self.pop(item, None)

    def issuperset(self, other):
        return all(item in self for item in other)

    def issubset(self, other):
        return all(item in other for item in self)


try:
    from Cython.Compiler import TypeInference
except ImportError:        # a build step that doesn't use Cython
    pass
else:
    if TypeInference.__file__.endswith(".py"):      # compiled, it would ignore a module global
        TypeInference.set = _InsertionOrderedSet
