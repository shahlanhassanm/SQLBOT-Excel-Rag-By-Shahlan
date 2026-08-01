import json
from typing import List, Optional

from sqlalchemy import and_
from sqlbot_xpack.permissions.api.permission import transRecord2DTO
from sqlbot_xpack.permissions.models.ds_permission import DsPermission, PermissionDTO
from sqlbot_xpack.permissions.models.ds_rules import DsRules

from apps.datasource.crud.row_permission import transFilterTree
from apps.datasource.models.datasource import CoreDatasource, CoreField, CoreTable
from common.core.deps import CurrentUser, SessionDep


def get_row_permission_filters(session: SessionDep, current_user: CurrentUser, ds: CoreDatasource,
                               tables: Optional[list] = None, single_table: Optional[CoreTable] = None):
    if single_table:
        table_list = [session.get(CoreTable, single_table.id)]
    else:
        table_list = session.query(CoreTable).filter(
            and_(CoreTable.ds_id == ds.id, CoreTable.table_name.in_(tables))
        ).all()

    filters = []
    if is_normal_user(current_user):
        contain_rules = session.query(DsRules).all()
        for table in table_list:
            row_permissions = session.query(DsPermission).filter(
                and_(DsPermission.table_id == table.id, DsPermission.type == 'row')).all()
            res: List[PermissionDTO] = []
            if row_permissions is not None:
                for permission in row_permissions:
                    # check permission and user in same rules
                    flag = False
                    for r in contain_rules:
                        p_list = json.loads(r.permission_list)
                        u_list = json.loads(r.user_list)
                        if p_list is not None and u_list is not None and permission.id in p_list and (
                                current_user.id in u_list or f'{current_user.id}' in u_list):
                            flag = True
                            break
                    if flag:
                        res.append(transRecord2DTO(session, permission))
            where_str = transFilterTree(session, current_user, res, ds)
            if where_str:
                filters.append({"table": table.table_name, "filter": where_str})
    return filters


def get_column_permission_fields(session: SessionDep, current_user: CurrentUser, table: CoreTable,
                                 fields: list[CoreField], contain_rules: list[DsRules]):
    if is_normal_user(current_user):
        column_permissions = session.query(DsPermission).filter(
            and_(DsPermission.table_id == table.id, DsPermission.type == 'column')).all()
        if column_permissions is not None:
            for permission in column_permissions:
                # check permission and user in same rules
                # obj = session.query(DsRules).filter(
                #     and_(DsRules.permission_list.op('@>')(cast([permission.id], JSONB)),
                #          or_(DsRules.user_list.op('@>')(cast([f'{current_user.id}'], JSONB)),
                #              DsRules.user_list.op('@>')(cast([current_user.id], JSONB))))
                # ).first()
                flag = False
                for r in contain_rules:
                    p_list = json.loads(r.permission_list)
                    u_list = json.loads(r.user_list)
                    if p_list is not None and u_list is not None and permission.id in p_list and (
                            current_user.id in u_list or f'{current_user.id}' in u_list):
                        flag = True
                        break
                if flag:
                    permission_list = json.loads(permission.permissions)
                    fields = filter_list(fields, permission_list)
    return fields


def collect_row_filters(session: SessionDep, current_user: CurrentUser, ds: CoreDatasource,
                        tables: list[str] | None = None) -> dict[str, str]:
    """``{table_name: where_fragment}`` for the caller's row-level rules.

    Thin adapter over ``get_row_permission_filters`` (which returns a list of
    ``{'table', 'filter'}``) for the read paths that need to look a filter up by
    table name: table sampling and value linking (AUDIT D-02). Returns ``{}``
    for users who bypass row rules, exactly as the list form does.
    """
    filters = get_row_permission_filters(session=session, current_user=current_user,
                                         ds=ds, tables=tables)
    out: dict[str, str] = {}
    for item in filters or []:
        table = item.get('table')
        where = item.get('filter')
        if table and where and str(where).strip():
            out[table] = where
    return out


def is_normal_user(current_user: CurrentUser) -> bool:
    """True when this caller IS subject to row and column permissions.

    Was `current_user.id != 1`, so the permission layer answered "is this caller
    privileged?" with an id comparison while the rest of the system used
    `isAdmin` -- a fourth definition of privileged in a codebase that already
    had three (AUDIT D-05/D-37). Anything holding id 1 got a full bypass,
    including DTOs built by internal services that never set isAdmin.

    `isAdmin` is assigned as `id == 1 and account == 'admin'` (see
    `system/crud/user.py` and `mcp.py`), a strict SUBSET of `id == 1`, so this
    can only tighten: the genuine administrator still bypasses, and everything
    that merely held id 1 no longer does.

    `getattr` with a False default so an object without the attribute is treated
    as NOT privileged -- failing open here would reintroduce the bypass.
    """
    return not getattr(current_user, 'isAdmin', False)


def filter_list(list_a, list_b):
    id_to_invalid = {}
    for b in list_b:
        if not b['enable']:
            id_to_invalid[b['field_id']] = True

    return [a for a in list_a if not id_to_invalid.get(a.id, False)]
