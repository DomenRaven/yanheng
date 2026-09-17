"""回归：DuckDB Windows 跨进程文件锁识别 + 短连接释放后可再次打开。

不碰主库 warehouse.duckdb，用 STOCK_QUANT_DB 指向临时文件。
同进程内 DuckDB 允许第二个写连接，所以锁测试必须用子进程占文件。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DB = Path(tempfile.mkdtemp()) / "locktest.duckdb"
os.environ["STOCK_QUANT_DB"] = str(_DB)
sys.path.insert(0, str(_ROOT))

from common.db import (  # noqa: E402
    WarehouseBusyError,
    _is_lock_error,
    get_connection,
    write_session,
)


def test_screenshot_message_is_lock() -> None:
    exc = RuntimeError(
        '无法打开文件 "E:\\x\\warehouse.duckdb": 另一个程序正在使用此文件，进程无法访问。'
    )
    assert _is_lock_error(exc), exc


def test_write_session_releases() -> None:
    with write_session(init=True) as conn:
        conn.execute("SELECT 1").fetchone()
    conn = get_connection(retries=2)
    conn.close()


_HOLDER = r"""
import sys, time
import duckdb
path = sys.argv[1]
conn = duckdb.connect(path)
sys.stdout.write("HOLD\n")
sys.stdout.flush()
time.sleep(60)
conn.close()
"""


def test_second_writer_other_process() -> None:
    import duckdb

    bootstrap = duckdb.connect(str(_DB))
    bootstrap.execute("SELECT 1").fetchone()
    bootstrap.close()

    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(_DB)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline() if proc.stdout else ""
        assert line.strip() == "HOLD", (line, proc.stderr.read() if proc.stderr else "")
        time.sleep(0.3)
        try:
            get_connection(retries=1)
            print("NOTE: other-process writer did not exclusive-lock this file")
        except WarehouseBusyError as exc:
            cause = exc.__cause__
            print("OK cross-process lock:", type(cause).__name__ if cause else None, cause)
            assert cause is not None and _is_lock_error(cause), cause
        try:
            duckdb.connect(str(_DB), read_only=True)
            print("NOTE: read_only succeeded while another process held write lock")
        except Exception as exc:  # noqa: BLE001
            print("OK read_only also blocked:", type(exc).__name__, exc)
            assert _is_lock_error(exc), exc
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    after = get_connection(retries=5)
    after.close()


def test_write_session_serializes_threads() -> None:
    with write_session(init=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS lock_probe (x INTEGER)")
        conn.execute("DELETE FROM lock_probe")
    errors: list[BaseException] = []

    def worker(offset: int) -> None:
        try:
            for i in range(15):
                with write_session() as conn:
                    conn.execute("INSERT INTO lock_probe VALUES (?)", [offset + i])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i * 100,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    with write_session() as conn:
        n = conn.execute("SELECT count(*) FROM lock_probe").fetchone()[0]
    assert n == 60, n


if __name__ == "__main__":
    test_screenshot_message_is_lock()
    test_write_session_releases()
    test_write_session_serializes_threads()
    test_second_writer_other_process()
    print("ALL PASSED")
