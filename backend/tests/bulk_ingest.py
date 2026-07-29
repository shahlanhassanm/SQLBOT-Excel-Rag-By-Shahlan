"""Bulk-ingest a folder of Excel/CSV files as ONE datasource each — no UI, no
manual per-file work.

Replicates the bulk-upload endpoint (add_excel_datasource) in-process for every
file in a folder: island/header detection -> per-sheet PostgreSQL tables ->
datasource (+ optional embeddings). Writes a JSONL manifest that DOUBLES AS THE
DETERMINISTIC PARSING CHECK: for every file it records the tables and their
column names (in field_index order) and flags anomalies (zero tables, blank
'col_N'/'Unnamed' headers, single-column tables) — all without any LLM. So one
pass over 3000 files both ingests them and validates the dynamic parsing layer.

Usage (in container):
    docker cp backend/tests/bulk_ingest.py sqlbot:/tmp/bulk_ingest.py
    docker exec -e INGEST_DIR=/data/excels -e INGEST_EMBED=false sqlbot \
        sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/bulk_ingest.py"

Env:
    INGEST_DIR      folder to scan (recursively) for .xlsx/.xls/.csv  [required]
    INGEST_LIMIT    max files (0 = all)                               [0]
    INGEST_EMBED    also build embeddings (slow; only needed for      [false]
                    auto-ROUTING — skip it for a pure parse+eval run)
    INGEST_MANIFEST output JSONL path                       [/tmp/ingest_manifest.jsonl]
"""
import os
import sys
import glob
import json
import time
import uuid
import shutil
import hashlib
import asyncio
import traceback

sys.path.insert(0, "/opt/sqlbot/app")
import apps.chat.task.llm  # noqa: F401  (production import order)

from sqlmodel import Session
from sqlalchemy import and_
from common.core.db import engine
from common.core.config import settings
from common.core.deps import i18n as TRANS
from apps.datasource.models.datasource import (
    CoreDatasource, CreateDatasource, CoreTable, CoreField, SheetFields, FieldInfo)
from apps.datasource.utils.excel import detect_island_specs, parse_excel_preview
from apps.datasource.utils.header_detection import (
    detect_header_row, read_raw_full, CONFIDENCE_THRESHOLD, _is_csv)
from apps.datasource.utils.utils import aes_encrypt
import pandas as pd
from apps.datasource.crud.datasource import create_ds
from apps.datasource.api.datasource import _import_excel_sheets
from common.utils.embedding_threads import run_save_ds_embeddings
from apps.system.schemas.system_schema import UserInfoDTO

INGEST_DIR = os.environ.get("INGEST_DIR", "")
LIMIT = int(os.environ.get("INGEST_LIMIT", "0"))
EMBED = os.environ.get("INGEST_EMBED", "false").lower() in ("1", "true", "yes")
MANIFEST = os.environ.get("INGEST_MANIFEST", "/tmp/ingest_manifest.jsonl")
EXCEL_PATH = settings.EXCEL_PATH
OID = int(os.environ.get("INGEST_OID", "1"))
# Manifest REVIEW threshold for header confidence — deliberately HIGHER than the
# production LLM-trigger threshold (CONFIDENCE_THRESHOLD=0.62), because a header
# can misfire while still scoring moderately (e.g. ~0.65). Tune per your data.
REVIEW_CONF = float(os.environ.get("HEADER_REVIEW_CONF", "0.70"))


def _admin_user() -> UserInfoDTO:
    return UserInfoDTO(id=1, account="admin", name="admin", email="admin@sqlbot.local",
                       oid=OID, isAdmin=True, language="en")


def _sheets_spec(save_path):
    if settings.EXCEL_ISLAND_DETECTION_ENABLED:
        specs = detect_island_specs(save_path)
        return [SheetFields(sheetName=s['sheetName'], headerRow=s.get('headerRow'),
                            region=s.get('region'), regionIndex=s.get('regionIndex'),
                            fields=[FieldInfo(**fld) for fld in s['fields']]) for s in specs]
    preview = parse_excel_preview(save_path)
    return [SheetFields(sheetName=s['sheetName'], headerRow=s.get('headerRow'),
                        fields=[FieldInfo(**fld) for fld in s['fields']]) for s in preview]


def _header_confidences(save_path):
    """Per-sheet header-detection confidence (0..1). Below CONFIDENCE_THRESHOLD the
    detector is unsure which row is the header — the signal that catches a header
    MISFIRE even when the wrong row produced plausible-looking column names (which
    the col_N/Unnamed/single-col anomaly flags cannot see)."""
    confs = {}
    try:
        if _is_csv(save_path):
            raw = read_raw_full(save_path)
            if raw is not None and not raw.empty:
                confs["(csv)"] = round(float(detect_header_row(raw)[1]), 3)
            return confs
        for sn in pd.ExcelFile(save_path).sheet_names:
            try:
                raw = read_raw_full(save_path, sheet_name=sn)
                if raw is not None and not raw.empty:
                    confs[str(sn)] = round(float(detect_header_row(raw)[1]), 3)
            except Exception:
                pass
    except Exception:
        pass
    return confs


def _schema_snapshot(session, ds_id):
    """Post-ingest table/column snapshot (field_index order) + anomaly flags —
    the deterministic parse check, straight from what actually landed in PG."""
    tables = session.query(CoreTable).filter(CoreTable.ds_id == ds_id).all()
    out, anomalies = [], []
    for t in tables:
        cols = [c.field_name for c in session.query(CoreField).filter(
            CoreField.table_id == t.id).order_by(CoreField.field_index.asc()).all()]
        out.append({"table": t.table_name, "columns": cols})
        if any(str(c).startswith(("col_", "Unnamed")) for c in cols):
            anomalies.append(f"{t.table_name}: blank/auto header")
        if len(cols) <= 1:
            anomalies.append(f"{t.table_name}: single column")
    if not tables:
        anomalies.append("no tables produced")
    return out, anomalies


def ingest_one(session, user, src_path):
    stem, ext = os.path.splitext(os.path.basename(src_path))
    os.makedirs(EXCEL_PATH, exist_ok=True)
    filename = f"{stem}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}{ext}"
    save_path = os.path.join(EXCEL_PATH, filename)
    shutil.copyfile(src_path, save_path)

    sheets_spec = _sheets_spec(save_path)
    results = _import_excel_sheets(save_path, sheets_spec, TRANS)

    _oid = user.oid or 1
    base = (stem or "excel").strip()[:50] or "excel"
    name, n = base, 1
    while session.query(CoreDatasource).filter(
            and_(CoreDatasource.oid == _oid, CoreDatasource.name == name)).first() is not None:
        n += 1
        name = f"{base}_{n}"

    conf = aes_encrypt(json.dumps({"filename": filename, "sheets": results}))
    tables = [CoreTable(table_name=r["tableName"], table_comment="") for r in results]
    create_obj = CreateDatasource(name=name, description=f"Excel file: {os.path.basename(src_path)}",
                                  type="excel", configuration=conf, tables=tables)
    loop = asyncio.new_event_loop()
    try:
        ds = loop.run_until_complete(create_ds(session, TRANS, user, create_obj))
    finally:
        loop.close()
    if EMBED:
        run_save_ds_embeddings([ds.id])
    schema, anomalies = _schema_snapshot(session, ds.id)
    confs = _header_confidences(save_path)
    min_conf = min(confs.values()) if confs else None
    if min_conf is not None and min_conf < REVIEW_CONF:
        anomalies.append(f"low header confidence ({min_conf:.2f})")
    return {"ds_id": ds.id, "name": name, "tables": schema, "anomalies": anomalies,
            "header_conf": confs, "header_conf_min": min_conf}


def main():
    if not INGEST_DIR or not os.path.isdir(INGEST_DIR):
        print(f"ERROR: set INGEST_DIR to a valid folder (got {INGEST_DIR!r})")
        sys.exit(2)
    files = []
    for ext in ("xlsx", "xls", "csv"):
        files += glob.glob(os.path.join(INGEST_DIR, "**", f"*.{ext}"), recursive=True)
    files = sorted(set(files))
    if LIMIT:
        files = files[:LIMIT]
    print(f"[cfg] EMBED={EMBED} island_detect={settings.EXCEL_ISLAND_DETECTION_ENABLED} "
          f"files={len(files)} manifest={MANIFEST}")

    ok = err = flagged = 0
    t0 = time.time()
    with open(MANIFEST, "w", encoding="utf-8") as mf, Session(engine) as session:
        user = _admin_user()
        for i, src in enumerate(files, 1):
            rec = {"file": os.path.basename(src)}
            try:
                rec.update(ingest_one(session, user, src))
                rec["ok"] = True
                ok += 1
                if rec.get("anomalies"):
                    flagged += 1
            except Exception as e:
                session.rollback()
                rec["ok"] = False
                rec["error"] = str(e)[:200]
                err += 1
                traceback.print_exc()
            mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            mf.flush()
            if i % 25 == 0 or i == len(files):
                print(f"  {i}/{len(files)}  ok={ok} err={err} flagged={flagged} "
                      f"({(time.time()-t0)/i:.1f}s/file)")

    print("\n==== INGEST SUMMARY ====")
    print(f"  files      : {len(files)}")
    print(f"  ingested   : {ok}")
    print(f"  failed     : {err}")
    print(f"  parse-flag : {flagged}  (see 'anomalies' in {MANIFEST})")


if __name__ == "__main__":
    main()
