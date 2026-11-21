from types import SimpleNamespace
from unittest import TestCase

from alembic.operations.ops import ExecuteSQLOp

from clickhouse_sqlalchemy.alembic import operations
from clickhouse_sqlalchemy.alembic.mvplan import (
    MaterializedViewRefreshPlan, MatViewPlanError, PlannedOp,
    TableAlter, ViewDefinition,
)


def v(name, inner, depends, selectable=None, **kwargs):
    return ViewDefinition(
        name=name,
        selectable=selectable or ('SELECT * FROM ' + depends[0]),
        inner_name=inner,
        depends_on=tuple(depends),
        kwargs=kwargs,
    )

# A three-layer graph over base table t:
#   t -> mv_a -> mv_b -> mv_c
# plus a sibling branch t -> mv_x (independent of the chain).
OLD_VIEWS = {
    'mv_a': v('mv_a', 'dst_a', ['t']),
    'mv_b': v('mv_b', 'dst_b', ['mv_a']),
    'mv_c': v('mv_c', 'dst_c', ['mv_b']),
    'mv_x': v('mv_x', 'dst_x', ['t']),
}


def _copy(views, **overrides):
    result = {}
    for name, view in views.items():
        result[name] = ViewDefinition(
            name=name,
            selectable=overrides.get(name, view.selectable) + ' -- new',
            inner_name=view.inner_name,
            depends_on=view.depends_on,
            kwargs=dict(view.kwargs),
        )
    return result


NEW_VIEWS = _copy(OLD_VIEWS)


def _names(planned):
    return [(step.kind, step.target) for step in planned]


class RefreshPlanTestCase(TestCase):
    def test_affected_closure_covers_all_downstream(self):
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
            base_tables=['t'],
        )
        self.assertEqual(
            plan.affected_views, frozenset(['mv_a', 'mv_b', 'mv_c'])
        )

    def test_unrelated_views_are_preserved(self):
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
            base_tables=['t'],
        )
        touched = {step.target for step in plan.upgrade_ops()}
        self.assertNotIn('mv_x', touched)
        self.assertNotIn('t', touched)

    def test_upgrade_ordering_drop_then_alter_then_create(self):
        alter = TableAlter(
            't',
            'ALTER TABLE t ADD COLUMN c UInt32',
            'ALTER TABLE t DROP COLUMN c',
        )
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
            table_alters=[alter], base_tables=['t'],
        )
        steps = _names(plan.upgrade_ops())
        # The altered table seeds the closure: both branches (chain and
        # sibling mv_x) are rebuilt, siblings in lexicographic order.
        self.assertEqual(steps, [
            ('drop_view', 'mv_x'),
            ('drop_view', 'mv_c'),
            ('drop_view', 'mv_b'),
            ('drop_view', 'mv_a'),
            ('alter_table', 't'),
            ('create_view', 'mv_a'),
            ('create_view', 'mv_b'),
            ('create_view', 'mv_c'),
            ('create_view', 'mv_x'),
        ])

    def test_upgrade_uses_old_definitions_for_drop_and_new_for_create(self):
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_b'],
            base_tables=['t'],
        )
        ops = plan.upgrade_ops()
        drops = [s for s in ops if s.kind == 'drop_view']
        creates = [s for s in ops if s.kind == 'create_view']

        for step in drops:
            self.assertNotIn('-- new', step.alembic_op.old_selectable)
        for step in creates:
            self.assertIn('-- new', step.alembic_op.selectable)

        # drop ops carry the old selectable for reversal.
        self.assertEqual(
            drops[0].alembic_op.old_selectable,
            OLD_VIEWS['mv_c'].selectable,
        )

    def test_downgrade_reverses_direction_and_uses_old_definitions(self):
        alter = TableAlter(
            't', 'ALTER TABLE t ADD COLUMN c UInt32',
            'ALTER TABLE t DROP COLUMN c',
        )
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
            table_alters=[alter], base_tables=['t'],
        )
        steps = plan.downgrade_ops()
        self.assertEqual(_names(steps), [
            ('drop_view', 'mv_x'),
            ('drop_view', 'mv_c'),
            ('drop_view', 'mv_b'),
            ('drop_view', 'mv_a'),
            ('alter_table', 't'),
            ('create_view', 'mv_a'),
            ('create_view', 'mv_b'),
            ('create_view', 'mv_c'),
            ('create_view', 'mv_x'),
        ])
        # downgrade recreate steps use the OLD selectables.
        creates = [s for s in steps if s.kind == 'create_view']
        for step in creates:
            self.assertNotIn('-- new', step.alembic_op.selectable)
        # downgrade runs the reverse alter SQL.
        downgrade_alter = next(
            s for s in steps if s.kind == 'alter_table'
        )
        self.assertEqual(
            downgrade_alter.alembic_op.sqltext,
            'ALTER TABLE t DROP COLUMN c',
        )

    def test_downgrade_drops_new_graph_and_creates_old_graph(self):
        # New graph has an extra dependency mv_c -> mv_x, so downgrade drops
        # must follow the new graph order and recreate the old graph order.
        new_views = _copy(OLD_VIEWS)
        # Rewire the new graph: mv_c reads from mv_x as well, which pulls
        # mv_x into the closure whenever mv_c is affected.
        new_views['mv_c'] = ViewDefinition(
            name='mv_c', inner_name='dst_c', depends_on=('mv_b', 'mv_x'),
            selectable='SELECT * FROM mv_b, mv_x -- new',
        )
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, new_views, rebuild_views=['mv_a'],
            base_tables=['t'],
        )
        downgrade = _names(plan.downgrade_ops())
        drop_targets = [t for kind, t in downgrade if kind == 'drop_view']
        create_targets = [t for kind, t in downgrade if kind == 'create_view']

        # mv_x is reached on the new graph via mv_c.
        self.assertIn('mv_x', plan.affected_views)
        # On the new graph mv_c depends on mv_x, so mv_c drops before mv_x.
        self.assertLess(drop_targets.index('mv_c'),
                        drop_targets.index('mv_x'))
        # Recreation follows the old graph topology (mv_x after the chain
        # because it is a topologically independent sibling there), using
        # the OLD definitions.
        self.assertEqual(create_targets, ['mv_a', 'mv_b', 'mv_c', 'mv_x'])
        for kind, target, step in (
            (k, t, s) for (k, t), s in zip(downgrade, plan.downgrade_ops())
        ):
            if kind == 'create_view':
                self.assertNotIn('-- new', step.alembic_op.selectable)

    def test_altered_table_seeds_downstream_closure(self):
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=[],
            table_alters=[TableAlter(
                't', 'ALTER TABLE t MODIFY COLUMN c UInt64',
                'ALTER TABLE t MODIFY COLUMN c UInt32',
            )],
            base_tables=['t'],
        )
        # Both branches reading from t are affected.
        self.assertEqual(
            plan.affected_views,
            frozenset(['mv_a', 'mv_b', 'mv_c', 'mv_x']),
        )

    def test_sibling_order_is_deterministic(self):
        views = {
            'mv_z': v('mv_z', 'd', ['t']),
            'mv_a': v('mv_a', 'd', ['t']),
            'mv_m': v('mv_m', 'd', ['t']),
        }
        plan = MaterializedViewRefreshPlan(
            views, _copy(views), rebuild_views=[],
            table_alters=[TableAlter('t', 'ALTER TABLE t ADD COLUMN y UInt8',
                                     'ALTER TABLE t DROP COLUMN y')],
            base_tables=['t'],
        )
        creates = [
            step.target for step in plan.upgrade_ops()
            if step.kind == 'create_view'
        ]
        # All three are independent siblings: lexicographic order.
        self.assertEqual(creates, ['mv_a', 'mv_m', 'mv_z'])

    def test_missing_dependency_fails_whole_plan(self):
        broken = dict(OLD_VIEWS)
        broken['mv_b'] = v('mv_b', 'dst_b', ['ghost'])
        with self.assertRaises(MatViewPlanError) as ctx:
            MaterializedViewRefreshPlan(
                broken, NEW_VIEWS, rebuild_views=['mv_a'],
                base_tables=['t'],
            )
        self.assertIn('missing dependencies', str(ctx.exception))
        self.assertIn('ghost', str(ctx.exception))

    def test_cycle_fails_whole_plan(self):
        cyclic = dict(OLD_VIEWS)
        cyclic['mv_a'] = v('mv_a', 'dst_a', ['mv_c'])
        with self.assertRaises(MatViewPlanError) as ctx:
            MaterializedViewRefreshPlan(
                cyclic, NEW_VIEWS, rebuild_views=['mv_b'],
                base_tables=['t'],
            )
        self.assertIn('cycle', str(ctx.exception))

    def test_unknown_rebuild_view_fails(self):
        with self.assertRaises(MatViewPlanError):
            MaterializedViewRefreshPlan(
                OLD_VIEWS, NEW_VIEWS, rebuild_views=['nope'],
                base_tables=['t'],
            )

    def test_unknown_altered_table_fails(self):
        with self.assertRaises(MatViewPlanError):
            MaterializedViewRefreshPlan(
                OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
                table_alters=[TableAlter('ghost', 'SELECT 1', 'SELECT 1')],
                base_tables=['t'],
            )

    def test_affected_view_missing_definition_fails(self):
        # mv_b stays downstream of rebuilt mv_a but is absent from the new
        # graph: rebuilding in place is impossible, so the whole plan fails.
        new_views = {n: view for n, view in NEW_VIEWS.items() if n != 'mv_b'}
        # mv_c must also be rewired away from mv_b for the new graph itself
        # to remain valid.
        new_views['mv_c'] = ViewDefinition(
            name='mv_c', inner_name='dst_c', depends_on=('mv_a',),
            selectable='SELECT * FROM mv_a -- new',
        )
        with self.assertRaises(MatViewPlanError) as ctx:
            MaterializedViewRefreshPlan(
                OLD_VIEWS, new_views, rebuild_views=['mv_a'],
                base_tables=['t'],
            )
        self.assertIn('mv_b', str(ctx.exception))

    def test_planned_ops_wrap_registered_alembic_ops(self):
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
            base_tables=['t'],
        )
        for step in plan.upgrade_ops():
            self.assertIsInstance(step, PlannedOp)
            if step.kind == 'drop_view':
                self.assertIsInstance(
                    step.alembic_op, operations.DropMatViewToTableOp
                )
            else:
                self.assertIsInstance(
                    step.alembic_op, operations.CreateMatViewToTableOp
                )

    def test_alter_steps_render_as_execute_sql_ops(self):
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
            table_alters=[TableAlter('t', 'ALTER TABLE t ADD COLUMN x UInt8',
                                     'ALTER TABLE t DROP COLUMN x')],
            base_tables=['t'],
        )
        alters = [s for s in plan.upgrade_ops() if s.kind == 'alter_table']
        self.assertEqual(len(alters), 1)
        self.assertIsInstance(alters[0].alembic_op, ExecuteSQLOp)

    def test_kwargs_are_forwarded_to_drop_and_create(self):
        old = {
            'mv_a': ViewDefinition(
                name='mv_a', inner_name='dst_a', depends_on=('t',),
                selectable='SELECT 1', kwargs={'on_cluster': 'c1'},
            )
        }
        new = {
            'mv_a': ViewDefinition(
                name='mv_a', inner_name='dst_a', depends_on=('t',),
                selectable='SELECT 2', kwargs={'on_cluster': 'c1'},
            )
        }
        plan = MaterializedViewRefreshPlan(
            old, new, rebuild_views=['mv_a'], base_tables=['t'],
        )
        drop = next(s for s in plan.upgrade_ops() if s.kind == 'drop_view')
        create = next(
            s for s in plan.upgrade_ops() if s.kind == 'create_view'
        )
        self.assertEqual(drop.alembic_op.kwargs['on_cluster'], 'c1')
        self.assertEqual(create.alembic_op.kwargs['on_cluster'], 'c1')

    def test_duplicate_dependencies_are_handled(self):
        views = {
            'mv_a': ViewDefinition(
                name='mv_a', inner_name='d', depends_on=('t', 't'),
                selectable='SELECT 1',
            ),
        }
        plan = MaterializedViewRefreshPlan(
            views, _copy(views), rebuild_views=['mv_a'],
            base_tables=['t'],
        )
        self.assertEqual(plan.affected_views, frozenset(['mv_a']))
        self.assertEqual(
            _names(plan.upgrade_ops()),
            [('drop_view', 'mv_a'), ('create_view', 'mv_a')],
        )

    def test_empty_plan_without_seeds_or_alters(self):
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=[], base_tables=['t'],
        )
        self.assertEqual(plan.affected_views, frozenset())
        self.assertEqual(plan.upgrade_ops(), [])
        self.assertEqual(plan.downgrade_ops(), [])

    def test_cycle_error_reports_the_cycle_path(self):
        cyclic = dict(OLD_VIEWS)
        cyclic['mv_a'] = ViewDefinition(
            name='mv_a', inner_name='dst_a', depends_on=('mv_c',),
            selectable='SELECT * FROM mv_c',
        )
        with self.assertRaises(MatViewPlanError) as ctx:
            MaterializedViewRefreshPlan(
                cyclic, NEW_VIEWS, rebuild_views=['mv_b'],
                base_tables=['t'],
            )
        message = str(ctx.exception)
        self.assertIn('cycle', message)
        for node in ('mv_a', 'mv_b', 'mv_c'):
            self.assertIn(node, message)

    def test_multiple_alters_keep_input_order_upgrade_reverse_downgrade(self):
        alters = [
            TableAlter('t', 'ALTER TABLE t ADD COLUMN a UInt8',
                       'ALTER TABLE t DROP COLUMN a'),
            TableAlter('t', 'ALTER TABLE t ADD COLUMN b UInt8',
                       'ALTER TABLE t DROP COLUMN b'),
        ]
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=[],
            table_alters=alters, base_tables=['t'],
        )
        upgrade_sql = [
            s.alembic_op.sqltext for s in plan.upgrade_ops()
            if s.kind == 'alter_table'
        ]
        downgrade_sql = [
            s.alembic_op.sqltext for s in plan.downgrade_ops()
            if s.kind == 'alter_table'
        ]
        self.assertEqual(upgrade_sql, [a.upgrade_sql for a in alters])
        self.assertEqual(downgrade_sql,
                         [a.downgrade_sql for a in reversed(alters)])

    def test_view_registered_under_mismatched_key_fails(self):
        views = {
            'wrong_key': ViewDefinition(
                name='mv_a', inner_name='d', depends_on=('t',),
                selectable='SELECT 1',
            ),
        }
        with self.assertRaises(MatViewPlanError):
            MaterializedViewRefreshPlan(
                views, views, rebuild_views=[], base_tables=['t'],
            )

    def test_to_alembic_op_wraps_the_same_plan(self):
        plan = MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
            base_tables=['t'],
        )
        op = plan.to_alembic_op()
        self.assertIsInstance(op, operations.RefreshMatViewsOp)
        self.assertIs(op.plan, plan)
        self.assertIsInstance(
            op.reverse(), operations.RefreshMatViewsReverseOp
        )
        self.assertIs(op.reverse().plan, plan)


class RefreshPlanRenderingTestCase(TestCase):
    @staticmethod
    def _context():
        return SimpleNamespace(
            opts={'alembic_module_prefix': 'op.'},
            _has_batch=False,
        )

    def _plan(self):
        return MaterializedViewRefreshPlan(
            OLD_VIEWS, NEW_VIEWS, rebuild_views=['mv_a'],
            table_alters=[TableAlter(
                't', "ALTER TABLE t ADD COLUMN c UInt32 DEFAULT 0",
                'ALTER TABLE t DROP COLUMN c',
            )],
            base_tables=['t'],
        )

    def test_upgrade_renders_only_primitive_statements(self):
        from clickhouse_sqlalchemy.alembic.renderers import (
            render_refresh_mat_views,
        )
        op = operations.RefreshMatViewsOp(self._plan())
        text = render_refresh_mat_views(self._context(), op)

        # Renderers emit multi-line statements; each statement starts at
        # column zero with an "op." primitive call.
        statement_starts = [
            line for line in text.splitlines()
            if line.startswith('op.')
        ]
        prefixes = {line.split('(', 1)[0] for line in statement_starts}
        self.assertEqual(
            prefixes,
            {'op.drop_mat_view_to_table', 'op.execute',
             'op.create_mat_view_to_table'},
        )
        # The alter on t reaches every view reading from it: four drops,
        # one alter, four creates, drops fully before creates.
        drop_idx = [i for i, line in enumerate(statement_starts)
                    if line.startswith('op.drop')]
        alter_idx = [i for i, line in enumerate(statement_starts)
                     if line.startswith('op.execute')]
        create_idx = [i for i, line in enumerate(statement_starts)
                      if line.startswith('op.create')]
        self.assertEqual(len(drop_idx), 4)
        self.assertEqual(alter_idx, [4])
        self.assertEqual(len(create_idx), 4)
        # Downstream-first drops, upstream-first creates.
        self.assertIn("drop_mat_view_to_table('mv_x')", text)
        self.assertIn("create_mat_view_to_table(\n    'mv_a'", text)

    def test_downgrade_renderer_uses_reverse_op(self):
        from clickhouse_sqlalchemy.alembic.renderers import (
            render_refresh_mat_views_reverse,
        )
        op = operations.RefreshMatViewsReverseOp(self._plan())
        text = render_refresh_mat_views_reverse(self._context(), op)
        # reverse alter SQL is rendered for downgrade.
        self.assertIn('ALTER TABLE t DROP COLUMN c', text)
        # old definitions (without "-- new") are recreated.
        create_part = text[text.index('op.create_mat_view_to_table'):]
        self.assertNotIn('-- new', create_part)

    def test_rendered_text_is_valid_python_with_alembic_prefix(self):
        from clickhouse_sqlalchemy.alembic.renderers import (
            render_refresh_mat_views,
        )
        op = operations.RefreshMatViewsOp(self._plan())
        text = render_refresh_mat_views(self._context(), op)
        # Smoke check: the rendered block parses as Python (with a dummy op).
        ns = {'op': SimpleNamespace(
            drop_mat_view_to_table=lambda *a, **k: None,
            create_mat_view_to_table=lambda *a, **k: None,
            execute=lambda *a, **k: None,
        )}
        exec(compile(text, '<rendered>', 'exec'), ns)
