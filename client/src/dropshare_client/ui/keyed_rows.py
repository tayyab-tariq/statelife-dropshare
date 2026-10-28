"""Bring a table model's rows up to date by identity (ui.md §4: fine-grained signals, stable
identity), instead of resetting it: the selection, the current row and the scroll position
survive a refresh, and screen readers aren't told the whole table changed."""

from collections.abc import Callable, Hashable

from PySide6.QtCore import QAbstractTableModel, QModelIndex


def update_rows[T](
    model: QAbstractTableModel,
    rows: list[T],
    new: list[T],
    key: Callable[[T], Hashable],
) -> None:
    """Change `rows` (the model's own list) in place into `new`, telling `model` each step:
    removals, then a reorder if needed, then inserts, then in-place changes."""
    wanted = {key(row) for row in new}
    for number in range(len(rows) - 1, -1, -1):  # removals, from the end
        if key(rows[number]) not in wanted:
            model.beginRemoveRows(QModelIndex(), number, number)
            del rows[number]
            model.endRemoveRows()

    kept = {key(row) for row in rows}
    order = [key(row) for row in new if key(row) in kept]
    if [key(row) for row in rows] != order:  # same items, new order (a rename re-sorts)
        model.layoutAboutToBeChanged.emit()
        position = {k: n for n, k in enumerate(order)}
        by_key = {key(row): row for row in rows}
        old_positions = [position[key(row)] for row in rows]
        moved_from = model.persistentIndexList()
        moved_to = [
            model.index(old_positions[index.row()], index.column())
            if 0 <= index.row() < len(old_positions)
            else QModelIndex()
            for index in moved_from
        ]
        rows[:] = [by_key[k] for k in order]
        model.changePersistentIndexList(moved_from, moved_to)
        model.layoutChanged.emit()

    for number, row in enumerate(new):  # inserts, in order
        if number < len(rows) and key(rows[number]) == key(row):
            continue
        model.beginInsertRows(QModelIndex(), number, number)
        rows.insert(number, row)
        model.endInsertRows()

    columns = model.columnCount()
    for number, row in enumerate(new):  # in-place changes
        if rows[number] != row:
            rows[number] = row
            model.dataChanged.emit(model.index(number, 0), model.index(number, columns - 1))
