"""影子仓农场回归：幂等按账户、半仓可成交、horizons 可算、CLI 可跑通。"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from advice.advice_engine import load_latest_advice_cards, parse_as_of_date
from advice.paper_broker import (
    _advice_already_filled,
    create_paper_account,
    get_cash,
    last_quote_date,
)
from advice.shadow_farm import (
    STRATEGY_SPECS,
    _horizon_returns,
    cards_for_strategy,
    ensure_shadow_accounts,
    load_latest_shadow_report,
    run_shadow_farm_once,
)
from common.db import write_session


def _ok(msg: str) -> None:
    print(f"OK  {msg}")


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def test_idempotency_scoped_to_account() -> None:
    """不打真实下单：直接写 paper_trades，验证幂等按账户隔离。"""
    a1 = create_paper_account("live_prep_10k", note="shadow-test-a1")
    a2 = create_paper_account("live_prep_10k", note="shadow-test-a2")
    aid = "shadow-test-idem-shared"
    with write_session(init=True) as conn:
        for acc in (a1, a2):
            conn.execute(
                """
                INSERT INTO paper_trades
                  (trade_id, account_id, advice_id, symbol, side, shares, price,
                   notional, fees, trade_date, source, reject_reason)
                VALUES (?, ?, ?, '000001', 'buy', 100, 10.0, 1000.0, 0.0, ?, 'sim', NULL)
                """,
                [f"t-{acc[-8:]}", acc, aid, dt.date.today()],
            )
        assert _advice_already_filled(conn, aid, a1)
        assert _advice_already_filled(conn, aid, a2)
        assert _advice_already_filled(conn, aid)  # 全局仍能看到（兼容旧调用）
        assert not _advice_already_filled(conn, "no-such-advice", a1)
    _ok("idempotency scoped to account")


def test_half_deploy_not_empty() -> None:
    from advice.paper_broker import mark_to_market_nav

    accounts = ensure_shadow_accounts()
    with write_session(init=True) as conn:
        as_of_s, cards = load_latest_advice_cards(conn, pool_id="hs")
        q = last_quote_date(conn)
        assert as_of_s and cards and q
        spec = next(s for s in STRATEGY_SPECS if s.strategy_id == "hs_top10_half")
        aid = accounts["hs_top10_half"]
        nav = mark_to_market_nav(conn, aid, as_of=q)
        cash = float(nav["cash_cny"])
        mv = float(nav["market_value_cny"])
        total = float(nav["nav_cny"]) or (cash + mv)
        budget = max(0.0, min(cash, total * float(spec.deploy_pct) - mv))
        # 已接近半仓目标、半仓预算买不起 1 手时，空列表是正确行为
        if budget < 500:
            _ok(f"half deploy skipped (budget={budget:.0f} cash={cash:.0f} mv={mv:.0f})")
            return
        out = cards_for_strategy(
            conn,
            cards,
            spec,
            signal_date=parse_as_of_date(as_of_s),
            account_id=aid,
            quote_asof=q,
        )
        assert out, "hs_top10_half 应在等权失败后贪心产出至少 1 张可模拟卡"
        assert all(str(c["advice_id"]).startswith("shadow:hs_top10_half:") for c in out)
    _ok(f"half deploy cards={len(out)}")


def test_horizons_math() -> None:
    from advice.shadow_farm import cohort_id_for_date, ensure_shadow_cohort

    cid = cohort_id_for_date(dt.date.today())
    accounts = ensure_shadow_cohort(cid)
    aid = accounts["hs_todos"]
    with write_session(init=True) as conn:
        q = last_quote_date(conn)
        assert q
        prev = q - dt.timedelta(days=1)
        conn.execute(
            "DELETE FROM shadow_nav_daily WHERE account_id = ? AND as_of = ?",
            [aid, prev],
        )
        conn.execute(
            """
            INSERT INTO shadow_nav_daily
              (account_id, cohort_id, strategy_id, as_of, cash_cny, market_value_cny, nav_cny,
               filled, pending, rejected, skipped)
            VALUES (?, ?, 'hs_todos', ?, 10000, 0, 10000, 0, 0, 0, 0)
            """,
            [aid, cid, prev],
        )
        row = conn.execute(
            "SELECT nav_cny FROM shadow_nav_daily WHERE account_id = ? AND as_of = ?",
            [aid, q],
        ).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO shadow_nav_daily
                  (account_id, cohort_id, strategy_id, as_of, cash_cny, market_value_cny, nav_cny,
                   filled, pending, rejected, skipped)
                VALUES (?, ?, 'hs_todos', ?, 1000, 9200, 10200, 0, 0, 0, 0)
                """,
                [aid, cid, q],
            )
        hz = _horizon_returns(conn, aid, q)
        assert hz["ret_1d"] is not None, hz
        _ok(f"horizons ret_1d={hz['ret_1d']:.4f}")


def test_run_once_and_report() -> None:
    report = run_shadow_farm_once(open_new_cohort=True)
    assert report.get("status") in ("ok", "skipped_data"), report
    assert len(report.get("strategies") or []) == 8
    assert report.get("cohort_id")
    assert report.get("retention_days") == 60
    assert report.get("max_concurrent_accounts") == 64
    assert "horizons" in (report["strategies"][0] or {})
    loaded = load_latest_shadow_report()
    assert loaded and loaded.get("run_id") == report.get("run_id")
    half = next(s for s in report["strategies"] if s["strategy_id"] == "hs_top10_half")
    note = half.get("note") or ""
    assert ("无可模拟卡片" not in note) or half.get("market_value_cny", 0) > 0 or half.get("filled", 0) > 0, half
    exp = report.get("monitoring_export") or {}
    assert exp.get("exported_accounts", 0) >= 8, exp
    from advice.shadow_farm import ACCOUNTS_DIR, REPORT_DIR

    assert (REPORT_DIR / "panel_latest.csv").exists()
    assert (REPORT_DIR / "panel_latest.json").exists()
    assert (REPORT_DIR / "accounts_index.json").exists()
    aid = report["strategies"][0]["account_id"]
    acc_dir = ACCOUNTS_DIR / aid
    assert (acc_dir / "nav_daily.csv").exists(), acc_dir
    assert (acc_dir / "meta.json").exists(), acc_dir
    _ok(
        f"run_once status={report['status']} cohort={report.get('cohort_id')} "
        f"active={report.get('active_accounts')} exported={exp.get('exported_accounts')}"
    )


def test_weekly_cohort_is_not_singleton() -> None:
    """本周队列 8 户；强制再建不应重复 strategy；账户总数可随周增长。"""
    from advice.shadow_farm import cohort_id_for_date, ensure_shadow_cohort
    from common.db import write_session

    cid = cohort_id_for_date(dt.date.today())
    a1 = ensure_shadow_cohort(cid)
    a2 = ensure_shadow_cohort(cid)
    assert a1 == a2
    assert len(a1) == 8
    with write_session(init=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM paper_account WHERE coalesce(kind,'human')='shadow' AND cohort_id=?",
            [cid],
        ).fetchone()[0]
    assert n == 8
    _ok(f"cohort {cid} stable at 8 accounts")


def main() -> int:
    print("test_shadow_farm starting", flush=True)
    with write_session(init=True):
        pass
    print("schema ok", flush=True)
    test_idempotency_scoped_to_account()
    test_half_deploy_not_empty()
    test_horizons_math()
    test_weekly_cohort_is_not_singleton()
    test_run_once_and_report()
    print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
