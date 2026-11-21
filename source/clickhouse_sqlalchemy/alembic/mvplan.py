"""Optional planner for rebuilding multi-layer TO materialized views around
a target-table structure change.

The planner never executes anything and never introspects a database.  It is
given both dependency graphs (before and after the change), the list of
materialized views that have to be rebuilt and the (data-preserving) table
alter operations.  It computes every downstream materialized view affected by
the change and produces an ordered, renderable migration plan:

* upgrade: drop affected views downstream-first on the *old* graph, run the
  table alterations, then recreate the views upstream-first on the *new*
  graph;
* downgrade: the same steps in reverse direction, using the *old* view
  definitions.

Target tables keep their data and views outside the affected closure are not
touched.  Missing dependencies or a dependency cycle make the whole planning
fail before any operation is returned.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set, Tuple

from . import operations


class MatViewPlanError(ValueError):
    """Raised when a refresh plan cannot be built (missing dependency,
    unknown object or dependency cycle)."""


@dataclass(frozen=True)
class ViewDefinition:
    """Definition of a TO materialized view.

    :param name: view name.
    :param selectable: ``AS SELECT`` body of the view.
    :param inner_name: target (``TO``) table name.
    :param depends_on: names of the views/tables this view reads from.
    :param kwargs: extra options (e.g. ``on_cluster``, ``if_exists``) used
        when dropping/recreating the view.
    """

    name: str
    selectable: str
    inner_name: str
    depends_on: Sequence[str] = ()
    kwargs: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TableAlter:
    """A confirmed data-preserving alteration of a target table.

    :param table: table name.
    :param upgrade_sql: SQL executed during upgrade, after the old views are
        dropped.
    :param downgrade_sql: SQL executed during downgrade, after the new views
        are dropped.
    """

    table: str
    upgrade_sql: str
    downgrade_sql: str


@dataclass(frozen=True)
class PlannedOp:
    """One step of a rendered plan.

    ``kind`` is ``'drop_view'``, ``'create_view'`` or ``'alter_table'``.
    """

    kind: str
    target: str
    alembic_op: object


def _build_graph(views: Dict[str, ViewDefinition],
                 base_tables: Sequence[str], label: str
                 ) -> Tuple[Dict[str, List[str]], Set[str]]:
    known = set(views) | set(base_tables)
    edges: Dict[str, List[str]] = {}

    for name, view in views.items():
        if view.name != name:
            raise MatViewPlanError(
                '%s view %r is registered under name %r'
                % (label, view.name, name)
            )
        missing = [d for d in view.depends_on if d not in known]
        if missing:
            raise MatViewPlanError(
                '%s graph has missing dependencies for view %r: %s'
                % (label, name, ', '.join(sorted(set(missing))))
            )
        # Deterministic order of same-level siblings; duplicate references
        # to the same dependency count once.
        edges[name] = sorted(set(view.depends_on))

    for table in base_tables:
        edges.setdefault(table, [])

    _ensure_acyclic(edges, label)
    return edges, known


def _ensure_acyclic(edges: Dict[str, List[str]], label: str) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in edges}
    stack: List[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in edges[node]:
            if color[dep] == GRAY:
                cycle_start = stack.index(dep)
                cycle = stack[cycle_start:] + [dep]
                raise MatViewPlanError(
                    '%s graph contains a dependency cycle: %s'
                    % (label, ' -> '.join(cycle))
                )
            if color[dep] == WHITE:
                visit(dep)
        stack.pop()
        color[node] = BLACK

    for node in sorted(edges):
        if color[node] == WHITE:
            visit(node)


def _topological_order(edges: Dict[str, List[str]]) -> List[str]:
    """Kahn's algorithm with lexicographic tie-breaking.

    Independent (same-level) nodes are therefore always emitted in a fixed
    order.
    """
    indegree = {node: 0 for node in edges}
    for node, deps in edges.items():
        for dep in deps:
            indegree[node] += 1

    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    order: List[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        dependents = sorted(
            other for other, deps in edges.items() if node in deps
        )
        for dependent in dependents:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                # Keep the queue sorted instead of appending at the end.
                ready.append(dependent)
                ready.sort()

    if len(order) != len(edges):
        # Defensive: cycles are already rejected while building the graph.
        raise MatViewPlanError('graph contains a dependency cycle')
    return order


def _downstream_views(edges: Dict[str, List[str]],
                      view_names: Set[str],
                      seeds: Sequence[str]) -> Set[str]:
    """Views reached by following directed "is read by" edges from the
    seeds (transitive downstream), restricted to view nodes."""
    dependents: Dict[str, List[str]] = {node: [] for node in edges}
    for node, deps in edges.items():
        for dep in deps:
            dependents[dep].append(node)

    seen: Set[str] = set()
    stack = list(seeds)
    while stack:
        node = stack.pop()
        for dependent in dependents.get(node, ()):
            if dependent not in seen:
                seen.add(dependent)
                stack.append(dependent)
    return {name for name in seen if name in view_names}


def _affected_closure(edges: Dict[str, List[str]],
                      view_names: Set[str],
                      seeds: Sequence[str]) -> Set[str]:
    """All views connected to any seed through view-to-view dependency
    edges, ignoring edge direction.

    A view has to be rebuilt not only when a rebuilt view feeds it
    (downstream), but also when it feeds a rebuilt view (upstream): recreating
    one view in the middle of a chain requires every view attached to that
    chain to be dropped and recreated together, otherwise the remaining views
    would point at a temporarily missing object.  Base tables are persistent
    objects and are intentionally *not* treated as propagation paths, so an
    unrelated view branch reading from the same table stays untouched.
    """
    neighbors: Dict[str, Set[str]] = {name: set() for name in view_names}
    for view in view_names:
        for dep in edges[view]:
            if dep in neighbors:
                neighbors[view].add(dep)
                neighbors[dep].add(view)

    seen: Set[str] = set()
    stack = [seed for seed in seeds if seed in neighbors]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(neighbors[node])
    return seen


class MaterializedViewRefreshPlan:
    """Ordered, renderable plan for rebuilding TO materialized views.

    :param old_views: views existing before the change, keyed by name.
    :param new_views: views existing after the change, keyed by name.
    :param rebuild_views: views that must be rebuilt.  Every materialized
        view downstream of any of them (on either graph) is rebuilt too.
    :param table_alters: confirmed alterations of the target tables.  Every
        materialized view downstream of an altered table is rebuilt too.
    :param base_tables: names of tables that are valid dependency roots but
        are not themselves altered.
    """

    def __init__(self,
                 old_views: Dict[str, ViewDefinition],
                 new_views: Dict[str, ViewDefinition],
                 rebuild_views: Sequence[str],
                 table_alters: Sequence[TableAlter] = (),
                 base_tables: Sequence[str] = ()):
        self.old_views = dict(old_views)
        self.new_views = dict(new_views)
        self.table_alters = list(table_alters)

        self._old_edges, old_known = _build_graph(
            self.old_views, base_tables, 'old'
        )
        self._new_edges, new_known = _build_graph(
            self.new_views, base_tables, 'new'
        )

        rebuild_views = list(rebuild_views)
        unknown = [
            name for name in rebuild_views
            if name not in self.old_views or name not in self.new_views
        ]
        if unknown:
            raise MatViewPlanError(
                'unknown rebuild view(s): %s' % ', '.join(sorted(unknown))
            )

        for alter in self.table_alters:
            if alter.table not in old_known or alter.table not in new_known:
                raise MatViewPlanError(
                    'altered table %r is missing from the view dependency '
                    'graphs' % alter.table
                )

        alter_tables = [alter.table for alter in self.table_alters]

        # View seeds: every view connected to them through view-to-view
        # edges, on either graph.
        affected = _affected_closure(
            self._old_edges, set(self.old_views), rebuild_views
        )
        affected |= _affected_closure(
            self._new_edges, set(self.new_views), rebuild_views
        )

        # Table seeds: only downstream views that read (directly or
        # transitively) from the altered table.
        affected |= _downstream_views(
            self._old_edges, set(self.old_views), alter_tables
        )
        affected |= _downstream_views(
            self._new_edges, set(self.new_views), alter_tables
        )

        # Explicitly requested views are affected even if isolated.
        affected.update(rebuild_views)
        self.affected_views = frozenset(
            name for name in affected
            if name in self.old_views or name in self.new_views
        )

        for name in sorted(self.affected_views):
            if name not in self.old_views:
                raise MatViewPlanError(
                    'affected view %r has no old definition and cannot be '
                    'rebuilt in place' % name
                )
            if name not in self.new_views:
                raise MatViewPlanError(
                    'affected view %r has no new definition and cannot be '
                    'rebuilt in place' % name
                )

        self.old_order = [
            name for name in _topological_order(self._old_edges)
            if name in self.affected_views
        ]
        self.new_order = [
            name for name in _topological_order(self._new_edges)
            if name in self.affected_views
        ]

    # -- operation factories ------------------------------------------------

    def _drop_op(self, view: ViewDefinition) -> PlannedOp:
        return PlannedOp(
            'drop_view', view.name,
            operations.DropMatViewToTableOp(
                view.name, view.selectable, view.inner_name, **view.kwargs
            )
        )

    def _create_op(self, view: ViewDefinition) -> PlannedOp:
        return PlannedOp(
            'create_view', view.name,
            operations.CreateMatViewToTableOp(
                view.name, view.selectable, view.inner_name, **view.kwargs
            )
        )

    @staticmethod
    def _alter_op(alter: TableAlter, sql: str) -> PlannedOp:
        from alembic.operations.ops import ExecuteSQLOp
        return PlannedOp('alter_table', alter.table, ExecuteSQLOp(sql))

    # -- public API ---------------------------------------------------------

    def upgrade_ops(self) -> List[PlannedOp]:
        """Drop (old graph, downstream first) -> alter -> create (new graph,
        upstream first)."""
        result = [
            self._drop_op(self.old_views[name])
            for name in reversed(self.old_order)
        ]
        result.extend(
            self._alter_op(alter, alter.upgrade_sql)
            for alter in self.table_alters
        )
        result.extend(
            self._create_op(self.new_views[name])
            for name in self.new_order
        )
        return result

    def downgrade_ops(self) -> List[PlannedOp]:
        """Drop (new graph, downstream first) -> reverse alters -> create
        with old definitions (old graph, upstream first)."""
        result = [
            self._drop_op(self.new_views[name])
            for name in reversed(self.new_order)
        ]
        result.extend(
            self._alter_op(alter, alter.downgrade_sql)
            for alter in reversed(self.table_alters)
        )
        result.extend(
            self._create_op(self.old_views[name])
            for name in self.old_order
        )
        return result

    def to_alembic_op(self) -> 'operations.RefreshMatViewsOp':
        """Wrap the plan into a renderable Alembic migration operation."""
        return operations.RefreshMatViewsOp(self)
