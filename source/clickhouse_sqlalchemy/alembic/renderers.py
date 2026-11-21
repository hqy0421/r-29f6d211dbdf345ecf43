from alembic.autogenerate import render
from alembic.autogenerate import renderers

from . import operations

indent = ' ' * 4


def escape(x):
    return x.replace("'", "\\'")


@renderers.dispatch_for(operations.CreateMatViewOp)
def render_create_mat_view(autogen_context, op):
    columns = [
        col
        for col in [
            render._render_column(col, autogen_context) for col in op.columns
        ]
        if col
    ]

    templ = (
        "{prefix}create_mat_view(\n"
        "{indent}'{name}',\n"
        "{indent}'{selectable}',\n"
        "{indent}'{engine}',\n"
        "{indent}{columns}\n"
        ")"
    )

    join_indent = ("'\n" + indent + "'")
    return templ.format(
        prefix=render._alembic_autogenerate_prefix(autogen_context),
        name=op.name,
        selectable=join_indent.join(escape(op.selectable).split('\n')),
        engine=join_indent.join(escape(op.engine.strip()).split('\n')),
        columns=(',\n' + indent).join(str(arg) for arg in columns),
        indent=indent
    )


@renderers.dispatch_for(operations.DropMatViewOp)
def render_drop_mat_view(autogen_context, op):
    return (
        render._alembic_autogenerate_prefix(autogen_context) +
        "drop_mat_view('" + op.name + "')"
    )


@renderers.dispatch_for(operations.CreateMatViewToTableOp)
def render_create_mat_view_to_table(autogen_context, op):
    templ = (
        "{prefix}create_mat_view_to_table(\n"
        "{indent}'{name}',\n"
        "{indent}'{selectable}',\n"
        "{indent}'{inner_name}'\n"
        ")"
    )

    join_indent = ("'\n" + indent + "'")
    return templ.format(
        prefix=render._alembic_autogenerate_prefix(autogen_context),
        name=op.name,
        selectable=join_indent.join(escape(op.selectable).split('\n')),
        inner_name=op.inner_name,
        indent=indent
    )


@renderers.dispatch_for(operations.DropMatViewToTableOp)
def render_drop_mat_view_to_table(autogen_context, op):
    return (
        render._alembic_autogenerate_prefix(autogen_context) +
        "drop_mat_view_to_table('" + op.name + "')"
    )


@renderers.dispatch_for(operations.AttachMatViewOp)
def render_attach_mat_view(autogen_context, op):
    columns = [
        col
        for col in [
            render._render_column(col, autogen_context) for col in op.columns
        ]
        if col
    ]

    templ = (
        "{prefix}attach_mat_view(\n"
        "{indent}'{name}',\n"
        "{indent}'{selectable}',\n"
        "{indent}'{engine}',\n"
        "{indent}{columns}\n"
        ")"
    )

    join_indent = ("'\n" + indent + "'")
    return templ.format(
        prefix=render._alembic_autogenerate_prefix(autogen_context),
        name=op.name,
        selectable=join_indent.join(escape(op.selectable).split('\n')),
        engine=join_indent.join(escape(op.engine.strip()).split('\n')),
        columns=(',\n' + indent).join(str(arg) for arg in columns),
        indent=indent
    )


@renderers.dispatch_for(operations.RefreshMatViewsOp)
def render_refresh_mat_views(autogen_context, op):
    return _render_refresh_steps(autogen_context, op.plan.upgrade_ops())


@renderers.dispatch_for(operations.RefreshMatViewsReverseOp)
def render_refresh_mat_views_reverse(autogen_context, op):
    return _render_refresh_steps(autogen_context, op.plan.downgrade_ops())


def _render_refresh_steps(autogen_context, planned_ops):
    # Every planned step wraps an already registered primitive operation, so
    # rendering reuses the standard drop/create/execute renderers and emits
    # only plain, executable migration statements.
    lines = []
    for planned in planned_ops:
        lines.extend(render.render_op(autogen_context, planned.alembic_op))
    return '\n'.join(lines)


@renderers.dispatch_for(operations.DetachMatViewOp)
def render_detach_mat_view(autogen_context, op):
    return (
        render._alembic_autogenerate_prefix(autogen_context) +
        "detach_mat_view('" + op.name + "')"
    )
