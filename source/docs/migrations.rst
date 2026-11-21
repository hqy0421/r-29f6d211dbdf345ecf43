Migrations
==========

Since version 0.1.10 clickhouse-sqlalchemy has alembic support. This support
allows autogenerate migrations from source code with some limitations.
This is the main advantage comparing to other migration tools when you need to
write plain migrations by yourself.

.. note::

    It is necessary to notice that ClickHouse doesn't have transactions.
    Therefore migration will not rolled back after some command with an error.
    And schema will remain in partially migrated state.

Autogenerate **will detect**:

* Table and materialized view additions, removals.
* Column additions, removals.
* Column and table comment additions, removals.

Example project with migrations https://github.com/xzkostyan/clickhouse-sqlalchemy-alembic-example.

Requirements
------------

Minimal versions:

* ClickHouse server 21.11.11.1
* clickhouse-sqlalchemy 0.1.10
* alembic 1.5.x

You can always write you migrations with pure alembic's ``op.execute`` if
autogenerate is not possible for your schema objects or your are using
``clickhouse-sqlalchemy<0.1.10``.

Limitations
-----------

Common limitations:

* Engines are not added into ``op.create_table``.
* ``Nullable(T)`` columns generation via ``Column(..., nullable=True)`` is not
  supported.

Currently ``ATTACH MATERIALIZED VIEW`` with modified ``SELECT`` statement
doesn't work for ``Atomic`` engine.

Migration adjusting
-------------------

You can and should adjust migrations after autogeneration.

Following parameters can be specified for:

* ``op.detach_mat_view``: ``if_exists``, ``on_cluster``, ``permanently``.
* ``op.attach_mat_view``: ``if_not_exists``, ``on_cluster``.
* ``op.create_mat_view``: ``if_not_exists``, ``on_cluster``, ``populate``.

See ClickHouse's Materialized View documentation.

For ``op.add_column`` you can add:

* ``AFTER name_after``: ``op.add_column(..., sa.Column(..., clickhouse_after=sa.text('my_column')))``.

Rebuilding materialized views around a table structure change
--------------------------------------------------------------

When a target table of a multi-layer ``TO`` materialized view has to change
its structure (during a maintenance window with writes paused), every view
that reads from the changed objects has to be dropped before the
``ALTER TABLE`` and recreated afterwards.  The optional planner in
``clickhouse_sqlalchemy.alembic`` builds this migration for you, without
executing anything and without touching the table data or unrelated views.

The planner is given both dependency graphs - the views as they exist before
and after the change - plus the views that must be rebuilt and the confirmed
data-preserving table alterations::

    from clickhouse_sqlalchemy.alembic import (
        MaterializedViewRefreshPlan, ViewDefinition, TableAlter,
    )

    old_views = {
        'mv_a': ViewDefinition(
            name='mv_a', selectable='SELECT x FROM events',
            inner_name='mv_a_dst', depends_on=('events',),
        ),
        'mv_b': ViewDefinition(
            name='mv_b', selectable='SELECT * FROM mv_a',
            inner_name='mv_b_dst', depends_on=('mv_a',),
        ),
    }
    new_views = {
        'mv_a': ViewDefinition(
            name='mv_a', selectable='SELECT x, y FROM events',
            inner_name='mv_a_dst', depends_on=('events',),
        ),
        'mv_b': ViewDefinition(
            name='mv_b', selectable='SELECT * FROM mv_a',
            inner_name='mv_b_dst', depends_on=('mv_a',),
        ),
    }

    plan = MaterializedViewRefreshPlan(
        old_views=old_views,
        new_views=new_views,
        rebuild_views=['mv_a'],
        table_alters=[
            TableAlter(
                table='events',
                upgrade_sql='ALTER TABLE events ADD COLUMN y UInt32',
                downgrade_sql='ALTER TABLE events DROP COLUMN y',
            ),
        ],
        base_tables=['events'],
    )

``MaterializedViewRefreshPlan`` finds every view affected by the change on
either the old or the new graph: the explicitly listed rebuild views, their
whole connected view chain (upstream and downstream), and every downstream
view of an altered table.  Views outside this closure, and the data inside
the target tables, are preserved.

The rendered upgrade follows the old graph downstream-first when dropping,
runs the alterations in the middle, then follows the new graph
upstream-first when recreating.  The downgrade runs the reverse direction
and recreates the old view definitions::

    def upgrade():
        op.refresh_mat_views(plan)


    def downgrade():
        op.refresh_mat_views(plan).reverse()

Autogenerate renders the container operation into plain, executable
primitives (``op.drop_mat_view_to_table``, ``op.execute`` and
``op.create_mat_view_to_table`` calls), so the generated migration stays
self-contained.

Planning fails for the whole migration before any operation is returned if:

* a view depends on an object absent from the graph (missing dependency);
* a graph contains a dependency cycle;
* a rebuild view or altered table is unknown;
* an affected view lacks either its old or its new definition.

Same-level (sibling) views are always ordered by name, so the generated
migration is deterministic.  The existing autogenerate behavior is unchanged
when the planner is not used.
