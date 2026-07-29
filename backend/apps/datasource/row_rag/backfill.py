# backend/apps/datasource/row_rag/backfill.py
"""Re-embed already-imported tables. Run manually in-container after enabling
ROW_RAG_ENABLED. Reads each table back from Postgres and calls embed_and_store_df.
"""
import pandas as pd

from apps.datasource.row_rag.ingest import embed_and_store_df
from common.utils.utils import SQLBotLogUtil


def backfill_tables(engine, table_names) -> dict:
    """Embed each table in table_names. Returns {table_name: rows_stored}."""
    out = {}
    for tn in table_names:
        try:
            df = pd.read_sql(f'SELECT * FROM "{tn}"', engine)
            out[tn] = embed_and_store_df(df, tn, engine)
        except Exception as e:
            SQLBotLogUtil.error(f"backfill failed for {tn}: {e}")
            out[tn] = 0
    return out
