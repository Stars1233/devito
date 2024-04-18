from collections import defaultdict

from sympy import And

from devito.exceptions import CompilationError
from devito.ir import (Forward, GuardBoundNext, Vector, WaitLock, WithLock,
                       FetchUpdate, PrefetchUpdate, ReleaseLock, normalize_syncs)
from devito.passes.clusters.utils import bind_critical_regions, is_memcpy
from devito.symbolics import IntDiv, uxreplace
from devito.tools import OrderedSet, as_mapper, is_integer, timed_pass
from devito.types import CustomDimension, Lock

__all__ = ['tasking', 'memcpy_prefetch']


@timed_pass(name='tasking')
def tasking(clusters, key, sregistry):
    candidates = as_mapper(clusters, key)
    candidates.pop(None, None)

    if len(candidates) == 0:
        return clusters
    elif len(candidates) > 1:
        raise CompilationError("Cannot handle multiple tasking dimensions")

    d, candidates = candidates.popitem()

    locks = {}
    waits = defaultdict(OrderedSet)
    tasks = defaultdict(list)
    for c0 in candidates:
        # Prevent future writes to interfere with a task by waiting on a lock
        may_require_lock = {f for f in c0.scope.reads if f.is_AbstractFunction}

        # Sort for deterministic code generation
        may_require_lock = sorted(may_require_lock, key=lambda i: i.name)

        protected = defaultdict(set)
        for c1 in clusters:
            offset = int(clusters.index(c1) <= clusters.index(c0))

            for target in may_require_lock:
                try:
                    writes = c1.scope.writes[target]
                except KeyError:
                    # No read-write dependency, ignore
                    continue

                try:
                    if all(w.aindices[d].is_Stepping for w in writes) or \
                       all(w.aindices[d].is_Modulo for w in writes):
                        sz = target.shape_allocated[d]
                        assert is_integer(sz)
                        ld = CustomDimension(name='ld', symbolic_size=sz, parent=d)
                    elif all(w[d] == 0 for w in writes):
                        # Special case, degenerates to scalar lock
                        raise KeyError
                    else:
                        # Functions over non-stepping Dimensions need no lock
                        continue
                except (AttributeError, KeyError):
                    # Would degenerate to a scalar, but we rather use a lock
                    # of size 1 for simplicity
                    ld = CustomDimension(name='ld', symbolic_size=1, parent=d)

                try:
                    lock = locks[target]
                except KeyError:
                    name = sregistry.make_name(prefix='lock')
                    lock = locks[target] = Lock(name=name, dimensions=ld)

                for w in writes:
                    try:
                        index = w[d]
                        logical_index = index + offset
                    except TypeError:
                        assert ld.symbolic_size == 1
                        index = 0
                        logical_index = 0

                    if logical_index in protected[target]:
                        continue

                    waits[c1].add(WaitLock(lock[index], target))
                    protected[target].add(logical_index)

        # Taskify `c0`
        for target in protected:
            lock = locks[target]

            indices = sorted({r[d] for r in c0.scope.reads[target]})
            if indices == [None]:
                # `lock` is protecting a Function which isn't defined over `d`
                # E.g., `d=time` and the protected function is `a(x, y)`
                assert lock.size == 1
                indices = [0]

            if wraps_memcpy(c0):
                e = c0.exprs[0]
                function = e.lhs.function
                findex = e.lhs.indices[d]
            else:
                # Only for backwards compatibility (e.g., tasking w/o buffering)
                function = None
                findex = None

            for i in indices:
                tasks[c0].append(ReleaseLock(lock[i], target))
                tasks[c0].append(WithLock(lock[i], target, i, function, findex, d))

    # CriticalRegions preempt WaitLocks, by definition
    mapper = bind_critical_regions(clusters)
    for c in clusters:
        for c1 in mapper.get(c, []):
            waits[c].update(waits.pop(c1, []))

    processed = []
    for c in clusters:
        if waits[c] or tasks[c]:
            processed.append(c.rebuild(syncs={d: list(waits[c]) + tasks[c]}))
        else:
            processed.append(c)

    return processed


@timed_pass(name='memcpy_prefetch')
def memcpy_prefetch(clusters, key, sregistry):
    actions = defaultdict(Actions)

    for c in clusters:
        d = key(c)
        if d is None:
            continue

        if d.is_Custom and is_integer(c.ispace[d].size):
            if wraps_memcpy(c):
                _actions_from_init(c, d, actions)
            else:
                raise NotImplementedError

        elif wraps_memcpy(c):
            _actions_from_update_memcpy(c, d, clusters, actions, sregistry)

    # Attach the computed Actions
    processed = []
    for c in clusters:
        v = actions[c]

        if v.drop:
            assert not v.syncs
            continue
        elif v.syncs:
            processed.append(c.rebuild(syncs=normalize_syncs(c.syncs, v.syncs)))
        else:
            processed.append(c)

        if v.insert:
            processed.extend(v.insert)

    return processed


def _actions_from_init(c, d, actions):
    i = c.ispace.index(d)
    if i > 0:
        pd = c.ispace[i].dim
    else:
        pd = None

    e = c.exprs[0]
    function = e.rhs.function
    target = e.lhs.function

    findex = e.rhs.indices[d]

    size = d.symbolic_size
    assert is_integer(size)

    actions[c].syncs[pd].append(
        FetchUpdate(None, target, 0, function, findex, d, size)
    )


def _actions_from_update_memcpy(c, d, clusters, actions, sregistry):
    direction = c.ispace[d].direction

    # Prepare the data to instantiate a PrefetchUpdate SyncOp
    e = c.exprs[0]
    function = e.rhs.function
    target = e.lhs.function

    fetch = e.rhs.indices[d]
    if direction is Forward:
        findex = fetch + 1
    else:
        findex = fetch - 1

    # If fetching into e.g. `ub[sb1]` we'll need to prefetch into e.g. `ub[sb0]`
    tindex0 = e.lhs.indices[d]
    if is_integer(tindex0) or isinstance(tindex0, IntDiv):
        tindex = tindex0
    else:
        assert tindex0.is_Modulo
        subiters = [i for i in c.sub_iterators[d] if i.parent is tindex0.parent]
        osubiters = sorted(subiters, key=lambda i: Vector(i.offset))
        n = osubiters.index(tindex0)
        if direction is Forward:
            tindex = osubiters[(n + 1) % len(osubiters)]
        else:
            tindex = osubiters[(n - 1) % len(osubiters)]

    # We need a lock to synchronize the copy-in
    name = sregistry.make_name(prefix='lock')
    ld = CustomDimension(name='ld', symbolic_size=1, parent=d)
    lock = Lock(name=name, dimensions=ld)
    handle = lock[0]

    # Turn `c` into a prefetch Cluster `pc`
    expr = uxreplace(e, {tindex0: tindex, fetch: findex})

    guards = {d: And(
        c.guards.get(d, True),
        GuardBoundNext(function.indices[d], direction),
    )}

    syncs = {d: [
        ReleaseLock(handle, target),
        PrefetchUpdate(handle, target, tindex, function, findex, d, 1, e.rhs)
    ]}

    pc = c.rebuild(exprs=expr, guards=guards, syncs=syncs)

    # Since we're turning `e` into a prefetch, we need to:
    # 1) attach a WaitLock SyncOp to the first Cluster accessing `target`
    # 2) insert the prefetch Cluster right after the last Cluster accessing `target`
    # 3) drop the original Cluster performing a memcpy-like fetch
    n = clusters.index(c)
    first = None
    last = None
    for c1 in clusters[n+1:]:
        if target in c1.scope.reads:
            if first is None:
                first = c1
            last = c1
    assert first is not None
    assert last is not None

    actions[first].syncs[d].append(WaitLock(handle, target))
    actions[last].insert.append(pc)
    actions[c].drop = True

    return last, pc


class Actions:

    def __init__(self, drop=False, syncs=None, insert=None):
        self.drop = drop
        self.syncs = syncs or defaultdict(list)
        self.insert = insert or []


def wraps_memcpy(cluster):
    if len(cluster.exprs) != 1:
        return False

    return is_memcpy(cluster.exprs[0])
