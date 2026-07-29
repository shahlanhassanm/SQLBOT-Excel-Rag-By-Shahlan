"""Run in-container:
    docker cp tests/test_read_raw_full.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_read_raw_full.py -q"
"""
import pandas as pd

from apps.datasource.utils.header_detection import read_raw, read_raw_full


def test_read_raw_full_reads_beyond_25_rows(tmp_path):
    df = pd.DataFrame({"a": list(range(40)), "b": list(range(40))})
    p = tmp_path / "big.xlsx"
    df.to_excel(str(p), index=False)

    full = read_raw_full(str(p))
    capped = read_raw(str(p))

    assert full.shape[0] == 41          # 1 header row + 40 data rows, no cap
    assert capped.shape[0] <= 25        # existing detector cap unchanged
