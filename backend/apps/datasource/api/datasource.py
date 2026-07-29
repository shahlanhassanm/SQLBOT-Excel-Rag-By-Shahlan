import asyncio
import hashlib
import io
import json
import os
import traceback
import uuid
from io import StringIO
from typing import List
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, File, UploadFile, HTTPException, Path
from fastapi.responses import StreamingResponse
from psycopg2 import sql
from sqlalchemy import and_

from apps.db.db import get_schema
from apps.db.engine import get_engine_conn
from apps.swagger.i18n import PLACEHOLDER_PREFIX
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import LogConfig, system_log
from common.core.config import settings
from common.core.deps import SessionDep, CurrentUser, Trans
from common.utils.utils import SQLBotLogUtil
from ..crud.datasource import get_datasource_list, check_status, create_ds, update_ds, delete_ds, getTables, getFields, \
    update_table_and_fields, getTablesByDs, chooseTables, preview, updateTable, updateField, get_ds, fieldEnum, \
    check_status_by_id, sync_single_fields
from ..crud.field import get_fields_by_table_id
from ..crud.table import get_tables_by_ds_id
from ..models.datasource import CoreDatasource, CreateDatasource, TableObj, CoreTable, CoreField, FieldObj, \
    TableSchemaResponse, ColumnSchemaResponse, PreviewResponse, ImportRequest, SheetFields, FieldInfo
from ..utils.excel import parse_excel_preview, reparse_sheet, USER_TYPE_TO_PANDAS, detect_island_specs
from ..utils.utils import aes_encrypt
from common.utils.embedding_threads import run_save_ds_embeddings
from ..utils.header_detection import (
    load_header_meta,
    read_raw,
    read_raw_full,
    read_sheet,
    resolve_header_row,
    save_header_meta,
)
from ..utils.island_detection import region_columns

router = APIRouter(tags=["Datasource"], prefix="/datasource")
path = settings.EXCEL_PATH


@router.get("/ws/{oid}", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def query_by_oid(session: SessionDep, user: CurrentUser, oid: int) -> List[CoreDatasource]:
    return get_datasource_list(session=session, user=user, oid=oid)


@router.get("/list", response_model=List[CoreDatasource], summary=f"{PLACEHOLDER_PREFIX}ds_list",
            description=f"{PLACEHOLDER_PREFIX}ds_list_description")
async def datasource_list(session: SessionDep, user: CurrentUser):
    return get_datasource_list(session=session, user=user)


@router.post("/get/{id}", response_model=CoreDatasource, summary=f"{PLACEHOLDER_PREFIX}ds_get")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], keyExpression="id", type='ds'))
async def get_datasource(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    return get_ds(session, id)


@router.post("/check", response_model=bool, summary=f"{PLACEHOLDER_PREFIX}ds_check")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def check(session: SessionDep, trans: Trans, ds: CoreDatasource):
    def inner():
        return check_status(session, trans, ds, True)

    return await asyncio.to_thread(inner)


@router.get("/check/{ds_id}", response_model=bool, summary=f"{PLACEHOLDER_PREFIX}ds_check")
async def check_by_id(session: SessionDep, trans: Trans,
                      ds_id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    def inner():
        return check_status_by_id(session, trans, ds_id, True)

    return await asyncio.to_thread(inner)


@router.post("/add", response_model=CoreDatasource, summary=f"{PLACEHOLDER_PREFIX}ds_add")
@system_log(LogConfig(operation_type=OperationType.CREATE, module=OperationModules.DATASOURCE, result_id_expr="id"))
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def add(session: SessionDep, trans: Trans, user: CurrentUser, ds: CreateDatasource):
    return await create_ds(session, trans, user, ds)


@router.post("/chooseTables/{id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_choose_tables")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def choose_tables(session: SessionDep, trans: Trans, tables: List[CoreTable],
                        id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    def inner():
        chooseTables(session, trans, id, tables)

    await asyncio.to_thread(inner)


@router.post("/update", response_model=CoreDatasource, summary=f"{PLACEHOLDER_PREFIX}ds_update")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="ds.id"))
@system_log(
    LogConfig(operation_type=OperationType.UPDATE, module=OperationModules.DATASOURCE, resource_id_expr="ds.id"))
async def update(session: SessionDep, trans: Trans, user: CurrentUser, ds: CoreDatasource):
    def inner():
        return update_ds(session, trans, user, ds)

    return await asyncio.to_thread(inner)


@router.post("/delete/{id}/{name}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_delete")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
@system_log(LogConfig(operation_type=OperationType.DELETE, module=OperationModules.DATASOURCE, resource_id_expr="id",
                      ))
async def delete(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id"), name: str = None):
    return await delete_ds(session, id)


@router.post("/getTables/{id}", response_model=List[TableSchemaResponse], summary=f"{PLACEHOLDER_PREFIX}ds_get_tables")
@require_permissions(permission=SqlbotPermission(type='ds', keyExpression="id"))
async def get_tables(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    return getTables(session, id)


@router.post("/getTablesByConf", response_model=List[TableSchemaResponse], summary=f"{PLACEHOLDER_PREFIX}ds_get_tables")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def get_tables_by_conf(session: SessionDep, trans: Trans, ds: CoreDatasource):
    try:
        def inner():
            return getTablesByDs(session, ds)

        return await asyncio.to_thread(inner)
    except Exception as e:
        # check ds status
        def inner():
            return check_status(session, trans, ds, True)

        status = await asyncio.to_thread(inner)
        if status:
            SQLBotLogUtil.error(f"get table failed: {e}")
            raise HTTPException(status_code=500, detail=f'Get table Failed: {e.args}')


@router.post("/getSchemaByConf", response_model=List[str], summary=f"{PLACEHOLDER_PREFIX}ds_get_schema")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def get_schema_by_conf(session: SessionDep, trans: Trans, ds: CoreDatasource):
    try:
        def inner():
            return get_schema(ds)

        return await asyncio.to_thread(inner)
    except Exception as e:
        # check ds status
        def inner():
            return check_status(session, trans, ds, True)

        status = await asyncio.to_thread(inner)
        if status:
            SQLBotLogUtil.error(f"get table failed: {e}")
            raise HTTPException(status_code=500, detail=f'Get table Failed: {e.args}')


@router.post("/getFields/{id}/{table_name}", response_model=List[ColumnSchemaResponse],
             summary=f"{PLACEHOLDER_PREFIX}ds_get_fields")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def get_fields(session: SessionDep,
                     id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id"),
                     table_name: str = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_table_name")):
    return getFields(session, id, table_name)


@router.post("/syncFields/{id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_sync_fields")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def sync_fields(session: SessionDep, trans: Trans,
                      id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_table_id")):
    return sync_single_fields(session, trans, id)


from pydantic import BaseModel


class TestObj(BaseModel):
    sql: str = None


# not used, just do test
""" @router.post("/execSql/{id}", include_in_schema=False)
async def exec_sql(session: SessionDep, id: int, obj: TestObj):
    def inner():
        data = execSql(session, id, obj.sql)
        try:
            data_obj = data.get('data')
            # print(orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS).decode())
            print(orjson.dumps(data_obj).decode())
        except Exception:
            traceback.print_exc()

        return data

    return await asyncio.to_thread(inner) """


@router.post("/tableList/{id}", response_model=List[CoreTable], summary=f"{PLACEHOLDER_PREFIX}ds_table_list")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def table_list(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    return get_tables_by_ds_id(session, id)


@router.post("/fieldList/{id}", response_model=List[CoreField], summary=f"{PLACEHOLDER_PREFIX}ds_field_list")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def field_list(session: SessionDep, field: FieldObj,
                     id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_table_id")):
    return get_fields_by_table_id(session, id, field)


@router.post("/editLocalComment", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def edit_local(session: SessionDep, data: TableObj):
    update_table_and_fields(session, data)


@router.post("/editTable", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_edit_table")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def edit_table(session: SessionDep, table: CoreTable):
    updateTable(session, table)


@router.post("/editField", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_edit_field")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def edit_field(session: SessionDep, field: CoreField):
    updateField(session, field)


@router.post("/previewData/{id}", response_model=PreviewResponse, summary=f"{PLACEHOLDER_PREFIX}ds_preview_data")
@require_permissions(permission=SqlbotPermission(type='ds', keyExpression="id"))
async def preview_data(session: SessionDep, trans: Trans, current_user: CurrentUser, data: TableObj,
                       id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    def inner():
        try:
            return preview(session, current_user, id, data)
        except Exception as e:
            ds = session.query(CoreDatasource).filter(CoreDatasource.id == id).first()
            # check ds status
            status = check_status(session, trans, ds, True)
            if status:
                SQLBotLogUtil.error(f"Preview failed: {e}")
                raise HTTPException(status_code=500, detail=f'Preview Failed: {e.args}')

    return await asyncio.to_thread(inner)


# not used
@router.post("/fieldEnum/{id}", include_in_schema=False)
async def field_enum(session: SessionDep, id: int):
    def inner():
        return fieldEnum(session, id)

    return await asyncio.to_thread(inner)


# @router.post("/uploadExcel")
# async def upload_excel(session: SessionDep, file: UploadFile = File(...)):
#     ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}
#     if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
#         raise HTTPException(400, "Only support .xlsx/.xls/.csv")
#
#     os.makedirs(path, exist_ok=True)
#     filename = f"{file.filename.split('.')[0]}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}.{file.filename.split('.')[1]}"
#     save_path = os.path.join(path, filename)
#     with open(save_path, "wb") as f:
#         f.write(await file.read())
#
#     def inner():
#         sheets = []
#         with get_data_engine() as conn:
#             if filename.endswith(".csv"):
#                 df = pd.read_csv(save_path, engine='c')
#                 tableName = f"sheet1_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
#                 sheets.append({"tableName": tableName, "tableComment": ""})
#                 column_len = len(df.dtypes)
#                 fields = []
#                 for i in range(column_len):
#                     # build fields
#                     fields.append({"name": df.columns[i], "type": str(df.dtypes[i]), "relType": ""})
#                 # create table
#                 create_table(conn, tableName, fields)
#
#                 data = [
#                     {df.columns[i]: None if pd.isna(row[i]) else (int(row[i]) if "int" in str(df.dtypes[i]) else row[i])
#                      for i in range(len(row))}
#                     for row in df.values
#                 ]
#                 # insert data
#                 insert_data(conn, tableName, fields, data)
#             else:
#                 excel_engine = 'xlrd' if filename.endswith(".xls") else 'openpyxl'
#                 df_sheets = pd.read_excel(save_path, sheet_name=None, engine=excel_engine)
#                 # build columns and data to insert db
#                 for sheet_name, df in df_sheets.items():
#                     tableName = f"{sheet_name}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
#                     sheets.append({"tableName": tableName, "tableComment": ""})
#                     column_len = len(df.dtypes)
#                     fields = []
#                     for i in range(column_len):
#                         # build fields
#                         fields.append({"name": df.columns[i], "type": str(df.dtypes[i]), "relType": ""})
#                     # create table
#                     create_table(conn, tableName, fields)
#
#                     data = [
#                         {df.columns[i]: None if pd.isna(row[i]) else (
#                             int(row[i]) if "int" in str(df.dtypes[i]) else row[i])
#                          for i in range(len(row))}
#                         for row in df.values
#                     ]
#                     # insert data
#                     insert_data(conn, tableName, fields, data)
#
#         os.remove(save_path)
#         return {"filename": filename, "sheets": sheets}
#
#     return await asyncio.to_thread(inner)


# deprecated
@router.post("/uploadExcel", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_upload_excel")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def upload_excel(session: SessionDep, file: UploadFile = File(..., description=f"{PLACEHOLDER_PREFIX}ds_excel")):
    ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}
    if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        raise HTTPException(400, "Only support .xlsx/.xls/.csv")

    os.makedirs(path, exist_ok=True)
    filename = f"{file.filename.split('.')[0]}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}.{file.filename.split('.')[1]}"
    save_path = os.path.join(path, filename)
    with open(save_path, "wb") as f:
        f.write(await file.read())

    def inner():
        sheets = []
        engine = get_engine_conn()
        if filename.endswith(".csv"):
            df = pd.read_csv(save_path, engine='c')
            tableName = f"sheet1_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
            sheets.append({"tableName": tableName, "tableComment": ""})
            insert_pg(df, tableName, engine)
        else:
            sheet_names = pd.ExcelFile(save_path).sheet_names
            for sheet_name in sheet_names:
                tableName = f"{sheet_name}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
                sheets.append({"tableName": tableName, "tableComment": ""})
                # df_temp = pd.read_excel(save_path, nrows=5)
                # non_empty_cols = df_temp.columns[df_temp.notna().any()].tolist()
                df = pd.read_excel(save_path, sheet_name=sheet_name, engine='calamine')
                insert_pg(df, tableName, engine)

        # os.remove(save_path)
        return {"filename": filename, "sheets": sheets}

    return await asyncio.to_thread(inner)


def insert_pg(df, tableName, engine):
    # fix field type
    for i in range(len(df.dtypes)):
        if str(df.dtypes[i]) == 'uint64':
            df[str(df.columns[i])] = df[str(df.columns[i])].astype('string')

    conn = engine.raw_connection()
    cursor = conn.cursor()
    try:
        df.to_sql(
            tableName,
            engine,
            if_exists='replace',
            index=False
        )
        # trans csv
        output = StringIO()
        df.to_csv(output, sep='\t', header=False, index=False)
        # output.seek(0)

        # pg copy
        query = sql.SQL("COPY {} FROM STDIN WITH CSV DELIMITER E'\t'").format(
            sql.Identifier(tableName)
        )
        cursor.copy_expert(sql=query.as_string(cursor.connection), file=output)
        conn.commit()
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(400, str(e))
    finally:
        cursor.close()
        conn.close()


t_sheet = "数据表列表"
t_s_col = "Sheet名称"
t_n_col = "表名"
t_c_col = "表备注"
f_n_col = "字段名"
f_c_col = "字段备注"


@router.get("/exportDsSchema/{id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_export_ds_schema")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def export_ds_schema(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    # {
    #     'sheet':'', sheet name
    #     'c1_h':'', column1 column name
    #     'c2_h':'', column2 column name
    #     'c1':[], column1 data
    #     'c2':[], column2 data
    # }
    def inner():
        if id == 0:  # download template
            file_name = '批量上传备注'
            df_list = [
                {'sheet': t_sheet, 'c0_h': t_s_col, 'c1_h': t_n_col, 'c2_h': t_c_col, 'c0': ["数据表1", "数据表2"],
                 'c1': ["user", "score"],
                 'c2': ["用来存放用户信息的数据表", "用来存放用户课程信息的数据表"]},
                {'sheet': '数据表1', 'c1_h': f_n_col, 'c2_h': f_c_col, 'c1': ["id", "name"],
                 'c2': ["用户id", "用户姓名"]},
                {'sheet': '数据表2', 'c1_h': f_n_col, 'c2_h': f_c_col, 'c1': ["course", "user_id", "score"],
                 'c2': ["课程名称", "用户ID", "课程得分"]},
            ]
        else:
            ds = session.query(CoreDatasource).filter(CoreDatasource.id == id).first()
            file_name = ds.name
            tables = session.query(CoreTable).filter(CoreTable.ds_id == id).order_by(
                CoreTable.table_name.asc()).all()
            if len(tables) == 0:
                raise HTTPException(400, "No tables")

            df_list = []
            df1 = {'sheet': t_sheet, 'c0_h': t_s_col, 'c1_h': t_n_col, 'c2_h': t_c_col, 'c0': [], 'c1': [], 'c2': []}
            df_list.append(df1)
            for index, table in enumerate(tables):
                df1['c0'].append(f"Sheet{index}")
                df1['c1'].append(table.table_name)
                df1['c2'].append(table.custom_comment)

                fields = session.query(CoreField).filter(CoreField.table_id == table.id).order_by(
                    CoreField.field_index.asc()).all()
                df_fields = {'sheet': f"Sheet{index}", 'c1_h': f_n_col, 'c2_h': f_c_col, 'c1': [], 'c2': []}
                for field in fields:
                    df_fields['c1'].append(field.field_name)
                    df_fields['c2'].append(field.custom_comment)
                df_list.append(df_fields)

        # build dataframe and export
        output = io.BytesIO()

        with (pd.ExcelWriter(output, engine='xlsxwriter') as writer):
            for index, df in enumerate(df_list):
                if index == 0:
                    pd.DataFrame({df['c0_h']: df['c0'], df['c1_h']: df['c1'], df['c2_h']: df['c2']}
                                 ).to_excel(writer, sheet_name=df['sheet'], index=False)
                else:
                    pd.DataFrame({df['c1_h']: df['c1'], df['c2_h']: df['c2']}).to_excel(writer, sheet_name=df['sheet'],
                                                                                        index=False)

        output.seek(0)

        filename = f'{file_name}.xlsx'
        encoded_filename = quote(filename)
        return io.BytesIO(output.getvalue())

    # headers = {
    #     'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}"
    # }

    result = await asyncio.to_thread(inner)
    return StreamingResponse(
        result,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@router.post("/uploadDsSchema/{id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_upload_ds_schema")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def upload_ds_schema(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id"),
                           file: UploadFile = File(...)):
    ALLOWED_EXTENSIONS = {"xlsx", "xls"}
    if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        raise HTTPException(400, "Only support .xlsx/.xls")

    try:
        contents = await file.read()
        excel_file = io.BytesIO(contents)

        sheet_names = pd.ExcelFile(excel_file, engine="openpyxl").sheet_names

        excel_file.seek(0)

        field_sheets = []
        table_sheet = None  # []
        for sheet in sheet_names:
            df = pd.read_excel(excel_file, sheet_name=sheet, engine="openpyxl").fillna('')
            if sheet == t_sheet:
                table_sheet = df.where(pd.notnull(df), None).to_dict(orient="records")
            else:
                field_sheets.append(
                    {'sheet_name': sheet, 'data': df.where(pd.notnull(df), None).to_dict(orient="records")})

        # print(field_sheets)

        # sheet table mapping
        sheet_table_map = {}

        # get data and update
        # update table comment
        if table_sheet and len(table_sheet) > 0:
            for table in table_sheet:
                sheet_table_map[table[t_s_col]] = table[t_n_col]
                session.query(CoreTable).filter(
                    and_(CoreTable.ds_id == id, CoreTable.table_name == table[t_n_col])).update(
                    {'custom_comment': table[t_c_col]})

        # update field comment
        if field_sheets and len(field_sheets) > 0:
            for fields in field_sheets:
                if len(fields['data']) > 0:
                    # get table id
                    table_name = sheet_table_map.get(fields['sheet_name'])
                    table = session.query(CoreTable).filter(
                        and_(CoreTable.ds_id == id, CoreTable.table_name == table_name)).first()
                    if table:
                        for field in fields['data']:
                            session.query(CoreField).filter(
                                and_(CoreField.ds_id == id,
                                     CoreField.table_id == table.id,
                                     CoreField.field_name == field[f_n_col])).update(
                                {'custom_comment': field[f_c_col]})
        session.commit()

        return True
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parse Excel Failed: {str(e)}")


@router.post("/parseExcel", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_parse_excel")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def parse_excel(file: UploadFile = File(..., description=f"{PLACEHOLDER_PREFIX}ds_excel")):
    ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}
    if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        raise HTTPException(400, "Only support .xlsx/.xls/.csv")

    os.makedirs(path, exist_ok=True)
    filename = f"{file.filename.split('.')[0]}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}.{file.filename.split('.')[1]}"
    save_path = os.path.join(path, filename)
    with open(save_path, "wb") as f:
        f.write(await file.read())

    def inner():
        sheets_data = parse_excel_preview(save_path)
        return {
            "filePath": filename,
            "data": sheets_data
        }

    return await asyncio.to_thread(inner)


class ReparseSheetRequest(BaseModel):
    filePath: str
    sheetName: str
    headerRow: int


@router.post("/reparseExcel", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_reparse_excel")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def reparse_excel(req: ReparseSheetRequest):
    """Re-preview a single sheet with a user-chosen header row.

    Called by the preview dialog when the user picks a different row in the
    "Header row" dropdown. Updates the sidecar so the subsequent import uses
    the same row even if the override field isn't echoed back.
    """
    save_path = os.path.join(path, req.filePath)
    if not os.path.exists(save_path):
        raise HTTPException(400, "File not found")
    if req.headerRow < 0:
        raise HTTPException(400, "headerRow must be >= 0")

    def inner():
        return reparse_sheet(save_path, req.sheetName, req.headerRow)

    return await asyncio.to_thread(inner)


def _insert_df_to_pg(df, table_name, engine):
    """Create ``table_name`` from ``df``'s schema and bulk-load its rows via COPY.

    Schema is created with an EMPTY frame (no row insert), then a single rewound
    COPY loads the data. Fixes the previous code where the StringIO was never
    rewound, so COPY read nothing and the slow to_sql silently did the insert.
    """
    for i in range(len(df.dtypes)):
        if str(df.dtypes[i]) == 'uint64':
            df[str(df.columns[i])] = df[str(df.columns[i])].astype('string')

    df.head(0).to_sql(table_name, engine, if_exists='replace', index=False)

    conn = engine.raw_connection()
    cursor = conn.cursor()
    try:
        output = StringIO()
        df.to_csv(output, sep='\t', header=False, index=False)
        output.seek(0)  # CRITICAL: rewind so COPY reads the rows (was missing)
        # NULL '' maps empty/NaN cells to SQL NULL (sensible for spreadsheet data).
        query = sql.SQL("COPY {} FROM STDIN WITH CSV DELIMITER E'\t' NULL ''").format(
            sql.Identifier(table_name)
        )
        cursor.copy_expert(sql=query.as_string(cursor.connection), file=output)
        conn.commit()
    except Exception:
        # to_sql already committed the (empty) table on its own connection, so a
        # failed COPY would leave a phantom 0-row table behind — drop it.
        conn.rollback()
        try:
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table_name)))
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        cursor.close()
        conn.close()


def _create_fts_indexes(df, table_name, engine):
    """Best-effort GIN full-text indexes on the TEXT columns of an imported table.

    `to_tsvector('simple', col)` (2-arg form) is IMMUTABLE, so it is index-eligible.
    'simple' is language-agnostic (no locale/stemming-dictionary dependency).
    """
    from common.core.config import settings
    if not settings.EXCEL_FTS_ENABLED:
        return
    text_cols = [str(df.columns[i]) for i in range(len(df.dtypes))
                 if str(df.dtypes[i]) in ('object', 'string')]
    if not text_cols:
        return
    conn = engine.raw_connection()
    cursor = conn.cursor()
    try:
        for col in text_cols:
            idx = "ftsidx_" + hashlib.sha256((table_name + '::' + col).encode()).hexdigest()[:16]
            stmt = sql.SQL(
                "CREATE INDEX IF NOT EXISTS {idx} ON {tbl} "
                "USING gin (to_tsvector('simple', {col}))"
            ).format(idx=sql.Identifier(idx), tbl=sql.Identifier(table_name),
                     col=sql.Identifier(col))
            try:
                cursor.execute(stmt)
                conn.commit()
            except Exception:
                conn.rollback()
                continue
    finally:
        cursor.close()
        conn.close()


def _import_excel_sheets(save_path: str, sheets: List[SheetFields], trans: Trans) -> list:
    """Import each spec in ``sheets`` into PostgreSQL.

    A spec with ``region`` set is a detected island: the table is built from a
    raw slice of the sheet. A spec with ``region is None`` keeps the original
    whole-sheet behavior. Shared by /importToDb and /addExcelDatasource.
    """
    engine = get_engine_conn()
    results = []
    is_csv = save_path.lower().endswith(".csv")
    header_meta = load_header_meta(save_path)
    raw_cache: dict = {}  # sheet_arg -> full header=None frame (region path only)

    for sheet_info in sheets:
        sheet_name = sheet_info.sheetName
        fields = sheet_info.fields

        field_mapping = {f.fieldName: f.fieldType for f in fields}
        dtype_dict = {
            col: USER_TYPE_TO_PANDAS.get(field_mapping.get(col, 'string'), 'string')
            for col in field_mapping.keys()
        }

        region = sheet_info.region
        if region is not None:
            sheet_arg = None if is_csv else sheet_name
            if sheet_arg not in raw_cache:
                raw_cache[sheet_arg] = read_raw_full(save_path, sheet_arg)
            raw_full = raw_cache[sheet_arg]

            min_row, max_row, min_col, max_col = region
            block = raw_full.iloc[min_row:max_row + 1,
                                  min_col:max_col + 1].reset_index(drop=True)
            abs_hr = sheet_info.headerRow if sheet_info.headerRow is not None else min_row
            rel_hr = abs_hr - min_row
            # Guard against malformed API input (headerRow outside the region):
            # a negative rel_hr would silently pick a wrong row via pandas
            # negative indexing rather than failing clearly.
            if rel_hr < 0 or rel_hr >= len(block):
                raise HTTPException(
                    400, f"headerRow {abs_hr} is outside region "
                         f"[{min_row},{max_row}] for sheet {sheet_name}")
            df = block.iloc[rel_hr + 1:].reset_index(drop=True)
            df.columns = region_columns(block.iloc[rel_hr].tolist())
            for col, dt in dtype_dict.items():
                if col in df.columns:
                    try:
                        df[col] = df[col].astype(dt)
                    except Exception:
                        pass

            ridx = sheet_info.regionIndex if sheet_info.regionIndex is not None else 0
            table_name = f"{sheet_name}_r{ridx}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
        else:
            sheet_key = "Sheet1" if is_csv else sheet_name
            header_row = sheet_info.headerRow
            if header_row is None:
                header_row = header_meta.get(sheet_key)
            if header_row is None:
                raw = read_raw(save_path, None if is_csv else sheet_name)
                header_row, _conf = resolve_header_row(
                    raw, context=f"{save_path}::{sheet_key}")
            header_meta[sheet_key] = header_row
            save_header_meta(save_path, header_meta)

            try:
                if is_csv:
                    df = read_sheet(save_path, None, header_row, dtype=dtype_dict)
                    sheet_name = "Sheet1"
                else:
                    df = read_sheet(save_path, sheet_name, header_row, dtype=dtype_dict)
            except Exception as e:
                raise HTTPException(500, f"{trans('i18n_ds_upload_error')}: {str(e)}")
            table_name = f"{sheet_name}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"

        try:
            _insert_df_to_pg(df, table_name, engine)
            _create_fts_indexes(df, table_name, engine)
            if settings.ROW_RAG_ENABLED:
                from apps.datasource.row_rag.ingest import embed_and_store_df
                embed_and_store_df(df, table_name, engine)
            results.append({
                "sheetName": sheet_name,
                "tableName": table_name,
                "tableComment": "",
                "rows": len(df),
            })
        except Exception as e:
            raise HTTPException(500, f"Insert data failed for {table_name}: {str(e)}")

    return results


@router.post("/importToDb", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_import_to_db")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def import_to_db(session: SessionDep, trans: Trans, import_req: ImportRequest):
    save_path = os.path.join(path, import_req.filePath)
    if not os.path.exists(save_path):
        raise HTTPException(400, "File not found")

    def inner():
        sheets = import_req.sheets
        # Apply island detection in the interactive wizard path too (not just the
        # bulk endpoint), so multi-table sheets split here as well. Detection does
        # its own per-island header resolution; we fall back to the user-confirmed
        # sheets if detection finds nothing.
        if settings.EXCEL_ISLAND_DETECTION_ENABLED:
            try:
                specs = detect_island_specs(save_path)
            except Exception:
                specs = []
            if specs:
                sheets = [SheetFields(sheetName=s['sheetName'],
                                      headerRow=s.get('headerRow'),
                                      region=s.get('region'),
                                      regionIndex=s.get('regionIndex'),
                                      fields=[FieldInfo(**fld) for fld in s['fields']])
                          for s in specs]
        return {"filename": import_req.filePath,
                "sheets": _import_excel_sheets(save_path, sheets, trans)}

    return await asyncio.to_thread(inner)


@router.post("/addExcelDatasource", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_add_excel_ds")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def add_excel_datasource(session: SessionDep, trans: Trans, user: CurrentUser,
                               file: UploadFile = File(..., description=f"{PLACEHOLDER_PREFIX}ds_excel")):
    """One-shot: turn one uploaded Excel/CSV file into a complete datasource.

    Saves the file, auto-detects header rows, imports every sheet as its own
    PostgreSQL table, creates the datasource (name = file stem, deduplicated
    per workspace) and triggers embeddings. The bulk-upload UI calls this once
    per selected file, so N files become N independent datasources that the
    agentic finder can route between at question time."""
    ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}
    if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        raise HTTPException(400, "Only support .xlsx/.xls/.csv")

    os.makedirs(path, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(file.filename))
    filename = f"{stem}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}{ext}"
    save_path = os.path.join(path, filename)
    with open(save_path, "wb") as f:
        f.write(await file.read())

    def inner():
        from common.core.config import settings
        if settings.EXCEL_ISLAND_DETECTION_ENABLED:
            specs = detect_island_specs(save_path)
            sheets_spec = [SheetFields(sheetName=s['sheetName'],
                                       headerRow=s.get('headerRow'),
                                       region=s.get('region'),
                                       regionIndex=s.get('regionIndex'),
                                       fields=[FieldInfo(**fld) for fld in s['fields']])
                           for s in specs]
        else:
            preview_sheets = parse_excel_preview(save_path)
            sheets_spec = [SheetFields(sheetName=s['sheetName'],
                                       headerRow=s.get('headerRow'),
                                       fields=[FieldInfo(**fld) for fld in s['fields']])
                           for s in preview_sheets]
        return _import_excel_sheets(save_path, sheets_spec, trans)

    results = await asyncio.to_thread(inner)

    # unique datasource name from the original file stem (workspace-scoped)
    _oid = user.oid if user.oid is not None else 1
    base_name = (stem or 'excel').strip()[:50] or 'excel'
    name = base_name
    n = 1
    while session.query(CoreDatasource).filter(
            and_(CoreDatasource.oid == _oid, CoreDatasource.name == name)).first() is not None:
        n += 1
        name = f"{base_name}_{n}"

    conf = aes_encrypt(json.dumps({'filename': filename, 'sheets': results}))
    tables = [CoreTable(table_name=r['tableName'], table_comment='') for r in results]
    create_obj = CreateDatasource(name=name, description=f"Excel file: {file.filename}",
                                  type='excel', configuration=conf, tables=tables)
    ds = await create_ds(session, trans, user, create_obj)
    run_save_ds_embeddings([ds.id])
    return {"id": ds.id, "name": name, "sheets": results}
