import datetime
import json
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, text
from sqlbot_xpack.permissions.models.ds_rules import DsRules
from sqlmodel import select

from apps.datasource.crud.permission import collect_row_filters, get_column_permission_fields, \
    get_row_permission_filters, is_normal_user
from apps.datasource.embedding.table_embedding import calc_table_embedding
from apps.datasource.utils.utils import aes_decrypt
from apps.db.constant import DB
from apps.db.db import get_tables, get_fields, exec_sql, check_connection
from apps.db.engine import get_engine_config, get_engine_conn
from apps.system.schemas.auth import CacheName, CacheNamespace
from common.core.config import settings
from common.core.deps import SessionDep, CurrentUser, Trans
from common.utils.embedding_threads import run_save_table_embeddings, run_save_ds_embeddings
from common.utils.utils import SQLBotLogUtil, deepcopy_ignore_extra, equals_ignore_case
from common.core.sqlbot_cache import cache, clear_cache
from .table import get_tables_by_ds_id
from ..crud.field import delete_field_by_ds_id, update_field
from ..crud.table import delete_table_by_ds_id, update_table
from ..models.datasource import CoreDatasource, CreateDatasource, CoreTable, CoreField, ColumnSchema, TableObj, \
    DatasourceConf, TableAndFields


def get_datasource_list(session: SessionDep, user: CurrentUser, oid: Optional[int] = None) -> List[CoreDatasource]:
    current_oid = user.oid if user.oid is not None else 1
    if user.isAdmin and oid:
        current_oid = oid
    return session.exec(
        select(CoreDatasource).where(CoreDatasource.oid == int(current_oid)).order_by(CoreDatasource.name)).all()


def get_ds(session: SessionDep, id: int):
    statement = select(CoreDatasource).where(CoreDatasource.id == id)
    datasource = session.exec(statement).first()
    return datasource


def check_status_by_id(session: SessionDep, trans: Trans, ds_id: int, is_raise: bool = False):
    ds = session.get(CoreDatasource, ds_id)
    if ds is None:
        if is_raise:
            raise HTTPException(status_code=500, detail=trans('i18n_ds_invalid'))
        return False
    return check_status(session, trans, ds, is_raise)


def check_status(session: SessionDep, trans: Trans, ds: CoreDatasource, is_raise: bool = False):
    return check_connection(trans, ds, is_raise)


def check_name(session: SessionDep, trans: Trans, user: CurrentUser, ds: CoreDatasource):
    if ds.id is not None:
        ds_list = session.query(CoreDatasource).filter(
            and_(CoreDatasource.name == ds.name, CoreDatasource.id != ds.id, CoreDatasource.oid == user.oid)).all()
        if ds_list is not None and len(ds_list) > 0:
            raise HTTPException(status_code=500, detail=trans('i18n_ds_name_exist'))
    else:
        ds_list = session.query(CoreDatasource).filter(
            and_(CoreDatasource.name == ds.name, CoreDatasource.oid == user.oid)).all()
        if ds_list is not None and len(ds_list) > 0:
            raise HTTPException(status_code=500, detail=trans('i18n_ds_name_exist'))


@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.DS_ID_LIST, keyExpression="user.oid")
async def create_ds(session: SessionDep, trans: Trans, user: CurrentUser, create_ds: CreateDatasource):
    ds = CoreDatasource()
    deepcopy_ignore_extra(create_ds, ds)
    check_name(session, trans, user, ds)
    ds.create_time = datetime.datetime.now()
    # status = check_status(session, ds)
    ds.create_by = user.id
    ds.oid = user.oid if user.oid is not None else 1
    ds.status = "Success"
    ds.type_name = DB.get_db(ds.type).db_name
    record = CoreDatasource(**ds.model_dump())
    session.add(record)
    session.flush()
    session.refresh(record)
    ds.id = record.id
    session.commit()

    # save tables and fields
    sync_table(session, ds, create_ds.tables)
    updateNum(session, ds)
    return ds


def chooseTables(session: SessionDep, trans: Trans, id: int, tables: List[CoreTable]):
    ds = session.query(CoreDatasource).filter(CoreDatasource.id == id).first()
    check_status(session, trans, ds, True)
    sync_table(session, ds, tables)
    updateNum(session, ds)


def update_ds(session: SessionDep, trans: Trans, user: CurrentUser, ds: CoreDatasource):
    ds.id = int(ds.id)
    check_name(session, trans, user, ds)
    # status = check_status(session, trans, ds)
    ds.status = "Success"
    record = session.exec(select(CoreDatasource).where(CoreDatasource.id == ds.id)).first()
    update_data = ds.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(record, field, value)
    session.add(record)
    session.commit()

    run_save_ds_embeddings([ds.id])
    return ds


def update_ds_recommended_config(session: SessionDep, datasource_id: int, recommended_config: int):
    record = session.exec(select(CoreDatasource).where(CoreDatasource.id == datasource_id)).first()
    record.recommended_config = recommended_config
    session.add(record)
    session.commit()


async def delete_ds(session: SessionDep, id: int):
    term = session.exec(select(CoreDatasource).where(CoreDatasource.id == id)).first()
    if term.type == "excel":
        # drop all tables for current datasource
        engine = get_engine_conn()
        conf = DatasourceConf(**json.loads(aes_decrypt(term.configuration)))
        with engine.connect() as conn:
            for sheet in conf.sheets:
                conn.execute(text(f'DROP TABLE IF EXISTS "{sheet["tableName"]}"'))
            conn.commit()

    session.delete(term)
    session.commit()
    delete_table_by_ds_id(session, id)
    delete_field_by_ds_id(session, id)
    if term:
        await clear_ws_ds_cache(term.oid)
    return {
        "message": f"Datasource with ID {id} deleted successfully."
    }


def getTables(session: SessionDep, id: int):
    ds = session.exec(select(CoreDatasource).where(CoreDatasource.id == id)).first()
    tables = get_tables(ds)
    return tables


def getTablesByDs(session: SessionDep, ds: CoreDatasource):
    # check_status(session, ds, True)
    tables = get_tables(ds)
    return tables


def getFields(session: SessionDep, id: int, table_name: str):
    ds = session.exec(select(CoreDatasource).where(CoreDatasource.id == id)).first()
    fields = get_fields(ds, table_name)
    return fields


def getFieldsByDs(session: SessionDep, ds: CoreDatasource, table_name: str):
    fields = get_fields(ds, table_name)
    return fields


def execSql(session: SessionDep, id: int, sql: str):
    ds = session.exec(select(CoreDatasource).where(CoreDatasource.id == id)).first()
    return exec_sql(ds, sql, True)


def sync_single_fields(session: SessionDep, trans: Trans, id: int):
    table = session.query(CoreTable).filter(CoreTable.id == id).first()
    ds = session.query(CoreDatasource).filter(CoreDatasource.id == table.ds_id).first()

    tables = getTablesByDs(session, ds)
    t_name = []
    for _t in tables:
        t_name.append(_t.tableName)

    if not table.table_name in t_name:
        raise HTTPException(status_code=500, detail=trans('i18n_table_not_exist'))

    # sync field
    fields = getFieldsByDs(session, ds, table.table_name)
    sync_fields(session, ds, table, fields)

    # A single-table column refresh can rename/add/drop a key column; the
    # cached join graph would serve the old shape for up to the TTL.
    try:
        from apps.datasource.relations import clear_cache
        clear_cache(ds.id)
    except Exception:
        pass

    # do table embedding
    run_save_table_embeddings([table.id])
    run_save_ds_embeddings([ds.id])


def sync_table(session: SessionDep, ds: CoreDatasource, tables: List[CoreTable]):
    # The join graph is cached per datasource; a sync can add or drop tables, so
    # drop the cached copy rather than serving a stale one for up to the TTL.
    try:
        from apps.datasource.relations import clear_cache
        clear_cache(ds.id)
    except Exception:
        pass

    id_list = []
    for item in tables:
        statement = select(CoreTable).where(and_(CoreTable.ds_id == ds.id, CoreTable.table_name == item.table_name))
        record = session.exec(statement).first()
        # update exist table, only update table_comment
        if record is not None:
            item.id = record.id
            id_list.append(record.id)

            record.table_comment = item.table_comment
            session.add(record)
            session.commit()
        else:
            # save new table
            table = CoreTable(ds_id=ds.id, checked=True, table_name=item.table_name, table_comment=item.table_comment,
                              custom_comment=item.table_comment)
            session.add(table)
            session.flush()
            session.refresh(table)
            item.id = table.id
            id_list.append(table.id)
            session.commit()

        # sync field
        fields = getFieldsByDs(session, ds, item.table_name)
        sync_fields(session, ds, item, fields)

    if len(id_list) > 0:
        session.query(CoreTable).filter(and_(CoreTable.ds_id == ds.id, CoreTable.id.not_in(id_list))).delete(
            synchronize_session=False)
        session.query(CoreField).filter(and_(CoreField.ds_id == ds.id, CoreField.table_id.not_in(id_list))).delete(
            synchronize_session=False)
        session.commit()
    else:  # delete all tables and fields in this ds
        session.query(CoreTable).filter(CoreTable.ds_id == ds.id).delete(synchronize_session=False)
        session.query(CoreField).filter(CoreField.ds_id == ds.id).delete(synchronize_session=False)
        session.commit()

    # do table embedding
    run_save_table_embeddings(id_list)
    run_save_ds_embeddings([ds.id])


def sync_fields(session: SessionDep, ds: CoreDatasource, table: CoreTable, fields: List[ColumnSchema]):
    id_list = []
    for index, item in enumerate(fields):
        statement = select(CoreField).where(
            and_(CoreField.table_id == table.id, CoreField.field_name == item.fieldName))
        record = session.exec(statement).first()
        if record is not None:
            item.id = record.id
            id_list.append(record.id)

            record.field_comment = item.fieldComment
            record.field_index = index
            record.field_type = item.fieldType
            session.add(record)
            session.commit()
        else:
            field = CoreField(ds_id=ds.id, table_id=table.id, checked=True, field_name=item.fieldName,
                              field_type=item.fieldType, field_comment=item.fieldComment,
                              custom_comment=item.fieldComment, field_index=index)
            session.add(field)
            session.flush()
            session.refresh(field)
            item.id = field.id
            id_list.append(field.id)
            session.commit()

    if len(id_list) > 0:
        session.query(CoreField).filter(and_(CoreField.table_id == table.id, CoreField.id.not_in(id_list))).delete(
            synchronize_session=False)
        session.commit()


def update_table_and_fields(session: SessionDep, data: TableObj):
    update_table(session, data.table)
    for field in data.fields:
        update_field(session, field)

    # do table embedding
    run_save_table_embeddings([data.table.id])
    run_save_ds_embeddings([data.table.ds_id])


def updateTable(session: SessionDep, table: CoreTable):
    update_table(session, table)

    # do table embedding
    run_save_table_embeddings([table.id])
    run_save_ds_embeddings([table.ds_id])


def updateField(session: SessionDep, field: CoreField):
    update_field(session, field)

    # do table embedding
    run_save_table_embeddings([field.table_id])
    run_save_ds_embeddings([field.ds_id])


def preview(session: SessionDep, current_user: CurrentUser, id: int, data: TableObj):
    ds = session.query(CoreDatasource).filter(CoreDatasource.id == id).first()
    # check_status(session, ds, True)

    # ignore data's fields param, query fields from database
    if not data.table.id:
        return {"fields": [], "data": [], "sql": ''}

    fields = session.query(CoreField).filter(CoreField.table_id == data.table.id).order_by(
        CoreField.field_index.asc()).all()

    if fields is None or len(fields) == 0:
        return {"fields": [], "data": [], "sql": ''}

    where = ''
    f_list = [f for f in fields if f.checked]
    if is_normal_user(current_user):
        # column is checked, and, column permission for data.fields
        contain_rules = session.query(DsRules).all()
        f_list = get_column_permission_fields(session=session, current_user=current_user, table=data.table,
                                              fields=f_list, contain_rules=contain_rules)

        # row permission tree
        where_str = ''
        filter_mapping = get_row_permission_filters(session=session, current_user=current_user, ds=ds, tables=None,
                                                    single_table=data.table)
        if filter_mapping:
            mapping_dict = filter_mapping[0]
            where_str = mapping_dict.get('filter')
        where = (' where ' + where_str) if where_str is not None and where_str != '' else ''

    fields = [f.field_name for f in f_list]
    if fields is None or len(fields) == 0:
        return {"fields": [], "data": [], "sql": ''}

    conf = DatasourceConf(**json.loads(aes_decrypt(ds.configuration))) if ds.type != "excel" else get_engine_config()
    sql: str = ""
    if ds.type == "mysql" or ds.type == "doris" or ds.type == "starrocks" or ds.type == "hive":
        sql = f"""SELECT `{"`, `".join(fields)}` FROM `{data.table.table_name}` 
            {where} 
            LIMIT 100"""
    elif ds.type == "sqlServer":
        sql = f"""SELECT TOP 100 [{"], [".join(fields)}] FROM [{conf.dbSchema}].[{data.table.table_name}]
            {where} 
            """
    elif ds.type == "pg" or ds.type == "excel" or ds.type == "redshift" or ds.type == "kingbase":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{data.table.table_name}" 
            {where} 
            LIMIT 100"""
    elif ds.type == "oracle":
        # sql = f"""SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{data.table.table_name}"
        #     {where}
        #     ORDER BY "{fields[0]}"
        #     OFFSET 0 ROWS FETCH NEXT 100 ROWS ONLY"""
        sql = f"""SELECT * FROM
                    (SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{data.table.table_name}"
                    {where} 
                    ORDER BY "{fields[0]}")
                    WHERE ROWNUM <= 100
                    """
    elif ds.type == "ck":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{data.table.table_name}" 
            {where} 
            LIMIT 100"""
    elif ds.type == "dm":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{data.table.table_name}"
            {where}
            LIMIT 100"""
    elif ds.type == "es":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{data.table.table_name}"
            {where}
            LIMIT 100"""
    elif ds.type == "sqlite":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{data.table.table_name}"
            {where}
            LIMIT 100"""
    return exec_sql(ds, sql, True)


def fieldEnum(session: SessionDep, id: int):
    field = session.query(CoreField).filter(CoreField.id == id).first()
    if field is None:
        return []
    table = session.query(CoreTable).filter(CoreTable.id == field.table_id).first()
    if table is None:
        return []
    ds = session.query(CoreDatasource).filter(CoreDatasource.id == table.ds_id).first()
    if ds is None:
        return []

    db = DB.get_db(ds.type)
    sql = f"""SELECT DISTINCT {db.prefix}{field.field_name}{db.suffix} FROM {db.prefix}{table.table_name}{db.suffix}"""
    res = exec_sql(ds, sql, True)
    return [item.get(res.get('fields')[0]) for item in res.get('data')]


def updateNum(session: SessionDep, ds: CoreDatasource):
    all_tables = get_tables(ds) if ds.type != 'excel' else json.loads(aes_decrypt(ds.configuration)).get('sheets')
    selected_tables = get_tables_by_ds_id(session, ds.id)
    num = f'{len(selected_tables)}/{len(all_tables)}'

    record = session.exec(select(CoreDatasource).where(CoreDatasource.id == ds.id)).first()
    update_data = ds.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(record, field, value)
    record.num = num
    session.add(record)
    session.commit()


def get_table_obj_by_ds(session: SessionDep, current_user: CurrentUser, ds: CoreDatasource) -> List[TableAndFields]:
    _list: List = []
    tables = session.query(CoreTable).filter(
        and_(CoreTable.ds_id == ds.id, CoreTable.checked == True)
    ).all()
    conf = DatasourceConf(**json.loads(aes_decrypt(ds.configuration))) if ds.type != "excel" else get_engine_config()
    schema = conf.dbSchema if conf.dbSchema is not None and conf.dbSchema != "" else conf.database

    # get all field
    table_ids = [table.id for table in tables]
    # Order by field_index so the schema shown to the model preserves the
    # ORIGINAL column order. Without this the rows come back in an arbitrary
    # order, which breaks adjacency-based reasoning (e.g. pairing a repeated
    # attribute column with the wrong sibling entity column).
    all_fields = session.query(CoreField).filter(
        and_(CoreField.table_id.in_(table_ids), CoreField.checked == True)
    ).order_by(CoreField.table_id.asc(), CoreField.field_index.asc()).all()
    # build dict
    fields_dict = {}
    for field in all_fields:
        if fields_dict.get(field.table_id):
            fields_dict.get(field.table_id).append(field)
        else:
            fields_dict[field.table_id] = [field]

    contain_rules = session.query(DsRules).all()
    for table in tables:
        # fields = session.query(CoreField).filter(and_(CoreField.table_id == table.id, CoreField.checked == True)).all()
        fields = fields_dict.get(table.id)

        # do column permissions, filter fields
        fields = get_column_permission_fields(session=session, current_user=current_user, table=table, fields=fields,
                                              contain_rules=contain_rules)
        _list.append(TableAndFields(schema=schema, table=table, fields=fields))
    return _list


def _column_value_profile(rows: list, per_col: int, value_maxlen: int) -> str:
    """Per-column distinct values from already-fetched rows (no extra queries).

    A column whose distinct values fit within ``per_col`` is emitted as a closed
    set ("one of: ..."); anything wider is emitted as examples. The closed-set
    case is what lets the model filter on values rather than only on column
    names, which is the only way a pivoted table is answerable: its measures
    live in a label column, not in the schema.
    """
    lines = []
    for col in rows[0].keys():
        seen = []
        seen_set = set()  # O(1) dedup membership (order preserved by `seen`)
        overflow = False
        numeric_only = True
        for row in rows:
            v = row.get(col)
            if v is None:
                continue
            # Purely numeric columns are measures, not filter values; listing
            # them would imply a closed category set and waste prompt budget.
            if not isinstance(v, bool) and isinstance(v, (int, float)):
                continue
            numeric_only = False
            s = str(v).strip()
            if not s:
                continue
            if len(s) > value_maxlen:
                s = s[:value_maxlen]
            if s in seen_set:
                continue
            if len(seen) >= per_col:
                overflow = True
                break
            seen_set.add(s)
            seen.append(s)
        if not seen or numeric_only:
            continue
        rendered = ", ".join('"' + x + '"' for x in seen)
        label = "e.g." if overflow else "one of:"
        lines.append(f"  {col} ({label} {rendered})")
    return "\n".join(lines)


def get_table_sample_data(ds: CoreDatasource, table_name: str, fields: list,
                          row_filter: str = None) -> str:
    """Sample a table for the model: JSON rows plus a per-column value profile.

    The shape of the table decides how much is shown, so this works the same for
    a 7-row pivoted financial sheet and a million-row fact table. One bounded
    ``LIMIT`` query is issued; rows are emitted until TABLE_SAMPLE_CHAR_BUDGET is
    reached, then every probed row still feeds the value profile. Short tables
    are therefore reproduced in full, while wide/long ones degrade to a few rows
    plus the distinct values that make their columns filterable.

    TABLE_SAMPLE_CHAR_BUDGET bounds the WHOLE returned block (rows + value
    profile), not just the rows — see the trim at the end of this function.
    """
    if not fields:
        return ""

    probe_rows = settings.TABLE_SAMPLE_PROBE_ROWS
    budget = settings.TABLE_SAMPLE_CHAR_BUDGET
    value_maxlen = settings.TABLE_SAMPLE_VALUE_MAXLEN

    db = DB.get_db(ds.type)
    # Get prefix/suffix for identifier quoting
    prefix = db.prefix if hasattr(db, 'prefix') else '"'
    suffix = db.suffix if hasattr(db, 'suffix') else '"'

    # Every column is selected; width is bounded by the character budget below
    # rather than by a column cap, which used to hide trailing total columns.
    field_names = [f"{prefix}{field.field_name}{suffix}" for field in fields]

    # Row-level permission predicate (AUDIT D-02). These rows go verbatim into
    # the model prompt and the UI execution log, so a restricted user must not
    # be shown rows their own generated SQL would be filtered away from.
    # Oracle/DM already carry a WHERE (ROWNUM), so the fragment is ANDed there
    # and introduced with WHERE everywhere else.
    _rf = str(row_filter).strip() if row_filter and str(row_filter).strip() else ''
    _where = f" WHERE ({_rf})" if _rf else ""
    _and = f" AND ({_rf})" if _rf else ""

    # Build LIMIT query based on database type
    if equals_ignore_case(ds.type, "sqlServer"):
        query = (f"SELECT TOP {probe_rows} {','.join(field_names)} "
                 f"FROM {prefix}{table_name}{suffix}{_where}")
    elif equals_ignore_case(ds.type, "ck"):
        query = f"SELECT {','.join(field_names)} FROM {table_name}{_where} LIMIT {probe_rows}"
    elif equals_ignore_case(ds.type, "hive"):
        query = f"SELECT {','.join(field_names)} FROM {table_name}{_where} LIMIT {probe_rows}"
    elif equals_ignore_case(ds.type, "oracle"):
        query = (f"SELECT {','.join(field_names)} FROM \"{table_name}\" "
                 f"WHERE ROWNUM <= {probe_rows}{_and}")
    elif equals_ignore_case(ds.type, "dm"):
        query = (f"SELECT {','.join(field_names)} FROM \"{table_name}\" "
                 f"WHERE ROWNUM <= {probe_rows}{_and}")
    else:
        query = (f"SELECT {','.join(field_names)} FROM {prefix}{table_name}{suffix}"
                 f"{_where} LIMIT {probe_rows}")

    try:
        result = exec_sql(ds=ds, sql=query, origin_column=True)
        rows = (result or {}).get('data') or []
        if not rows:
            return ""

        import json
        # Truncate long string values for readability
        clean_rows = []
        for row in rows:
            truncated_row = {}
            for key, value in row.items():
                if value is None:
                    truncated_row[key] = None
                elif isinstance(value, str):
                    # Truncate long strings
                    if len(value) > value_maxlen:
                        value = value[:value_maxlen] + '...'
                    truncated_row[key] = value.replace('\n', ' ').replace('\r', ' ')
                else:
                    truncated_row[key] = value
            clean_rows.append(truncated_row)

        # Emit rows until the budget runs out; always keep at least one so a very
        # wide table still shows the model what a record looks like.
        shown = []
        used = 0
        for row in clean_rows:
            size = len(json.dumps(row, ensure_ascii=False, default=str))
            if shown and used + size > budget:
                break
            shown.append(row)
            used += size

        # One compact object per line. `used` above is measured on the compact dump,
        # so pretty-printing here (indent=2 previously) inflated the emitted block to
        # ~3x what the budget accounted for — the single largest reason prompts ran
        # into the context ceiling. One row per line stays readable to the model.
        parts = ["[\n" + ",\n".join(
            json.dumps(r, ensure_ascii=False, default=str) for r in shown) + "\n]"]
        if len(shown) < len(clean_rows):
            parts.append(f"# {len(shown)} of {len(clean_rows)} sampled rows shown; "
                         f"column values below cover all {len(clean_rows)}")
        profile = _column_value_profile(
            clean_rows, settings.TABLE_SAMPLE_DISTINCT_PER_COL, value_maxlen)
        if profile:
            # `budget` above bounds only the JSON rows; the profile used to be
            # appended unaccounted, so a table with budget=1200 really emitted
            # ~4200 chars and the block was ~3.5x its nominal size. Trim the
            # profile to the remaining budget so the setting bounds the whole
            # per-table block. Whole lines only (a half-written value list would
            # read as a real value), and at least one line always survives.
            remaining = budget - sum(len(p) for p in parts) - len("# Column values:\n")
            lines = profile.split("\n")
            if remaining < len(profile):
                kept, acc = [], 0
                for line in lines:
                    if kept and acc + len(line) + 1 > max(remaining, 0):
                        break
                    kept.append(line)
                    acc += len(line) + 1
                if len(kept) < len(lines):
                    kept.append(f"  ... {len(lines) - len(kept)} more column(s) omitted")
                profile = "\n".join(kept)
            parts.append("# Column values:\n" + profile)
        return "\n".join(parts)
    except Exception:
        pass
    return ""


def get_tables_sample_data(session: SessionDep, current_user: CurrentUser, ds: CoreDatasource,
                           table_list: list[str] = None) -> str:
    """Sample data for all selected tables, bounded in TOTAL as well as per table.

    TABLE_SAMPLE_CHAR_BUDGET is per table, so the combined block grows with the
    number of tables: measured at up to ~30k chars, which pushed 22% of LLM calls
    into the model's 32k context ceiling and silently truncated the prompt (empty
    answers rather than an error). TABLE_SAMPLE_TOTAL_CHAR_BUDGET caps the whole
    block. The first table is always included even if it alone exceeds the cap, so
    this can never return nothing.
    """
    table_objs = get_table_obj_by_ds(session=session, current_user=current_user, ds=ds)
    if len(table_objs) == 0:
        return ""

    # Row-level permissions (AUDIT D-02). Column permissions are already applied
    # by get_table_obj_by_ds; row rules were not applied anywhere on this path,
    # so a restricted user's prompt contained up to TABLE_SAMPLE_PROBE_ROWS raw
    # rows of every selected table. Fails CLOSED: if the rules cannot be read for
    # a restricted user we emit no samples rather than unfiltered ones.
    _wanted = [obj.table.table_name for obj in table_objs
               if table_list is None or obj.table.table_name in table_list]
    try:
        row_filters = collect_row_filters(session, current_user, ds, _wanted)
    except Exception:
        SQLBotLogUtil.exception('row-permission lookup failed while sampling tables')
        if is_normal_user(current_user):
            SQLBotLogUtil.info(
                'table sampling skipped: row-permission lookup failed for a '
                'restricted user (fail-closed)')
            return ""
        row_filters = {}

    total_budget = settings.TABLE_SAMPLE_TOTAL_CHAR_BUDGET
    sample_data_parts = []
    used = 0
    truncated = 0
    for obj in table_objs:
        if table_list is not None and obj.table.table_name not in table_list:
            continue
        if not obj.fields:
            continue
        sample = get_table_sample_data(ds, obj.table.table_name, obj.fields,
                                       row_filters.get(obj.table.table_name))
        if not sample:
            continue
        block = f"# Table: {obj.table.table_name}\n{sample}"
        if total_budget > 0 and sample_data_parts and used + len(block) > total_budget:
            truncated += 1
            continue
        sample_data_parts.append(block)
        used += len(block)
    if truncated:
        SQLBotLogUtil.info(
            f'sample data capped at {total_budget} chars: {truncated} table(s) omitted '
            f'({len(sample_data_parts)} included, {used} chars)')
    return "\n".join(sample_data_parts)


def get_table_schema(session: SessionDep, current_user: CurrentUser, ds: CoreDatasource, question: str,
                     embedding: bool = True, table_list: list[str] = None) -> tuple[str, list]:
    schema_str = ""
    table_objs = get_table_obj_by_ds(session=session, current_user=current_user, ds=ds)
    if len(table_objs) == 0:
        return schema_str, []
    db_name = table_objs[0].schema
    schema_str += f"【DB_ID】 {db_name}\n"
    # Physical table names are generated (name + hash), so a question that names
    # the source file ("... in Financial_Report_5") matches nothing in the schema
    # and the model reports the data as absent. State the datasource identity so
    # the user's name for it resolves to these tables.
    if ds.name:
        schema_str += (f"【Datasource】 {ds.name}\n"
                       f"The tables below are the contents of \"{ds.name}\". When the question "
                       f"refers to \"{ds.name}\" (or the file/dataset/report of that name), it "
                       f"means exactly these tables — treat it as present, not missing.\n")
    schema_str += "【Schema】\n"
    tables = []
    all_tables = []  # temp save all tables
    table_name_list = []

    # Join graph. Without it the model has to guess how tables relate, which is
    # the largest measured error source; see apps/datasource/relations.py. Best
    # effort by design -- a missing hint costs accuracy, an exception here would
    # break SQL generation outright.
    relations = {}
    try:
        from apps.datasource.relations import get_relations
        relations = get_relations(
            ds, db_name,
            {obj.table.table_name: [f.field_name for f in (obj.fields or [])]
             for obj in table_objs})
    except Exception:
        relations = {}

    for obj in table_objs:
        # 如果传入了table_list，则只处理在列表中的表
        if table_list is not None and obj.table.table_name not in table_list:
            continue

        schema_table = ''
        no_schema_types = ["mysql", "es", "sqlite", "hive", "doris", "starrocks"]
        schema_table += f"# Table: {db_name}.{obj.table.table_name}" if ds.type not in no_schema_types and db_name else f"# Table: {obj.table.table_name}"
        table_comment = (obj.table.custom_comment or '').strip()
        if table_comment == '':
            schema_table += '\n[\n'
        else:
            schema_table += f", {table_comment}\n[\n"

        # Canonical M-Schema puts the primary key inline in the column tuple
        # ("(col:type, Primary Key, comment)") and foreign keys in the trailing
        # 【Foreign keys】 block — the formats the specialist models were
        # trained on. See apps/datasource/relations.py.
        rel = relations.get(obj.table.table_name)
        pk_cols = set(rel.primary_key) if rel else set()

        if obj.fields:
            field_list = []
            for field in obj.fields:
                parts = [f"{field.field_name}:{field.field_type}"]
                if field.field_name in pk_cols:
                    parts.append("Primary Key")
                if field.custom_comment and field.custom_comment.strip():
                    parts.append(field.custom_comment.strip())
                field_list.append("(" + ", ".join(parts) + ")")
            schema_table += ",\n".join(field_list)
        schema_table += '\n]\n'

        t_obj = {"id": obj.table.id, "table_name": obj.table.table_name, "schema_table": schema_table,
                 "embedding": obj.table.embedding}
        tables.append(t_obj)
        all_tables.append(t_obj)

    # 如果没有符合过滤条件的表，直接返回
    if not tables:
        return schema_str, []

    # do table embedding
    if embedding and tables and settings.TABLE_EMBEDDING_ENABLED:
        tables = calc_table_embedding(tables, question)
    # splice schema
    if tables:
        for s in tables:
            schema_str += s.get('schema_table')
            table_name_list.append(s.get('table_name'))

    # Discovered join graph -> canonical 【Foreign keys】 lines. A kept table's
    # edge may point at a table the embedding cut dropped; the join target is
    # then almost certainly needed, so recover it into the schema (same "lost
    # table" treatment the hand-configured relation path below has always done).
    fk_lines = []
    if relations:
        kept = set(table_name_list)
        by_name = {s.get('table_name'): s for s in all_tables}
        changed = True
        while changed:                      # a recovered table can itself reference another
            changed = False
            for tname in list(kept):
                tr = relations.get(tname)
                if not tr:
                    continue
                for r in tr.foreign_keys:
                    if r.ref_table not in kept and r.ref_table in by_name:
                        schema_str += by_name[r.ref_table].get('schema_table')
                        table_name_list.append(r.ref_table)
                        kept.add(r.ref_table)
                        changed = True
        for tname in table_name_list:
            tr = relations.get(tname)
            if not tr:
                continue
            for r in tr.foreign_keys:
                if r.ref_table not in kept:
                    continue
                line = f"{r.table}.{r.column}={r.ref_table}.{r.ref_column}"
                if r.source == "inferred":
                    # M-Schema has no notion of a probabilistic FK — label
                    # heuristic edges so the model can weigh them.
                    line += f" (inferred, {r.confidence:.0%} value overlap)"
                fk_lines.append(line)

    # field relation
    if tables and ds.table_relation:
        manual_edges = list(filter(lambda x: x.get('shape') == 'edge', ds.table_relation))
        if manual_edges:
            # Complete the missing table
            # get tables in relation, remove irrelevant relation
            embedding_table_ids = [s.get('id') for s in tables]
            all_relations = list(
                filter(lambda x: x.get('source').get('cell') in embedding_table_ids or x.get('target').get(
                    'cell') in embedding_table_ids, manual_edges))

            # get relation table ids, sub embedding table ids
            relation_table_ids = []
            for r in all_relations:
                relation_table_ids.append(r.get('source').get('cell'))
                relation_table_ids.append(r.get('target').get('cell'))
            relation_table_ids = list(set(relation_table_ids))
            # get table dict
            table_records = session.query(CoreTable).filter(CoreTable.id.in_(list(map(int, relation_table_ids)))).all()
            table_dict = {}
            for ele in table_records:
                table_dict[ele.id] = ele.table_name

            # get lost table ids
            lost_table_ids = list(set(relation_table_ids) - set(embedding_table_ids))
            # get lost table schema and splice it
            lost_tables = list(filter(lambda x: x.get('id') in lost_table_ids, all_tables))
            if lost_tables:
                for s in lost_tables:
                    schema_str += s.get('schema_table')
                    table_name_list.append(s.get('table_name'))

            # get field dict
            relation_field_ids = []
            for relation in all_relations:
                relation_field_ids.append(relation.get('source').get('port'))
                relation_field_ids.append(relation.get('target').get('port'))
            relation_field_ids = list(set(relation_field_ids))
            field_records = session.query(CoreField).filter(CoreField.id.in_(list(map(int, relation_field_ids)))).all()
            field_dict = {}
            for ele in field_records:
                field_dict[ele.id] = ele.field_name

            for ele in all_relations:
                fk_lines.append(
                    f"{table_dict.get(int(ele.get('source').get('cell')))}.{field_dict.get(int(ele.get('source').get('port')))}={table_dict.get(int(ele.get('target').get('cell')))}.{field_dict.get(int(ele.get('target').get('port')))}")

    # One canonical block for every source of edges (discovered + hand-drawn),
    # deduplicated — the same FK stated twice reads as two different facts.
    if fk_lines:
        seen = set()
        schema_str += '【Foreign keys】\n'
        for line in fk_lines:
            if line not in seen:
                seen.add(line)
                schema_str += line + '\n'

    return schema_str, table_name_list


@cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.DS_ID_LIST, keyExpression="oid")
async def get_ws_ds(session, oid) -> list:
    stmt = select(CoreDatasource.id).distinct().where(CoreDatasource.oid == oid)
    db_list = session.exec(stmt).all()
    return db_list


@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.DS_ID_LIST, keyExpression="oid")
async def clear_ws_ds_cache(oid):
    SQLBotLogUtil.info(f"ds cache for ws [{oid}] has been cleaned")
