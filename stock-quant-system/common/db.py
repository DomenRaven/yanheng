"""DuckDB 连接与表结构初始化。

选择 DuckDB 而非 SQLite 的原因见计划文档 3.1 节：全市场多年日线是"宽表批量扫描"
场景，DuckDB 的列式存储 + 向量化执行对这类查询有明显优势，且仍是零配置单文件。
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

from common.config import get_config, resolve_path

# 并行旁路库：设置后所有 ingestion 写入该文件，不碰正在被占用的主库。
_DB_PATH_ENV = "STOCK_QUANT_DB"
_SIDECAR_LOCK = "data/SIDECAR_PARALLEL.lock"

_SCHEMA_SQL = """
-- 股票池：全市场股票基础信息与状态标记
CREATE TABLE IF NOT EXISTS universe (
    symbol          VARCHAR NOT NULL,   -- 6位代码，如 600519
    exchange        VARCHAR NOT NULL,   -- sh / sz / bj
    board           VARCHAR,            -- main / gem / star / bse
    name            VARCHAR,
    is_st           BOOLEAN DEFAULT FALSE,
    is_delisted     BOOLEAN DEFAULT FALSE,
    first_seen_date DATE,               -- 本系统首次观测到该股票的日期
    last_seen_date  DATE,                -- 最近一次在行情快照中观测到该股票的日期
    list_date       DATE,               -- 官方上市日期（Phase 0.5 由退市股清单/交易所数据回填，可能晚于first_seen_date）
    delist_date     DATE,               -- 官方退市日期（Phase 0.5 由退市股清单回填，精确值优先于is_delisted的粗略推断）
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol)
);

-- 财务公告日历：point-in-time对齐用，记录每只股票每个报告期"实际披露"日期。
-- 数据源：巨潮资讯预约披露接口，一次调用覆盖全市场一个报告期，非逐股抓取。
-- Phase 1 使用fundamentals表做因子/标签计算时，必须 JOIN 本表并按
-- announce_date <= 截面日期 过滤，不能直接假设report_date当天数据已公开。
CREATE TABLE IF NOT EXISTS disclosure_calendar (
    symbol          VARCHAR NOT NULL,
    report_date     DATE NOT NULL,      -- 报告期，如 2024-09-30
    announce_date   DATE,               -- 实际披露日期
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, report_date)
);

-- 行业分类（变动历史）：申万宏源官方分类，天然支持point-in-time
-- （每条记录的start_date即该分类生效起始日，同一股票可有多条历史记录）。
-- industry_code 为申万分类体系编码，人类可读名称解码留待后续按需补充，
-- Phase 1 行业中性化直接按 industry_code 分组即可。
CREATE TABLE IF NOT EXISTS industry_classification (
    symbol          VARCHAR NOT NULL,
    start_date      DATE NOT NULL,      -- 该行业分类生效起始日
    industry_code   VARCHAR,
    source_updated_at DATE,             -- 数据源自己记录的更新时间
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, start_date)
);

-- 全市场日线行情（前复权）
CREATE TABLE IF NOT EXISTS daily_quotes (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    volume      DOUBLE,       -- 成交量（手）
    amount      DOUBLE,       -- 成交额（元）
    turnover    DOUBLE,       -- 换手率（%），部分数据源缺失时为NULL
    pct_change  DOUBLE,       -- 涨跌幅（%）
    adjust      VARCHAR NOT NULL DEFAULT 'qfq',
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date, adjust)
);

-- 财务指标：采用 EAV（实体-属性-值）宽表设计而非固定列。
-- 原因：新浪财务指标接口 stock_financial_analysis_indicator 单只股票返回 80+ 个指标
-- （每股收益/ROE/各类周转率/资产负债结构等），且指标集合可能随数据源版本演进，
-- 固定列设计会导致频繁的表结构变更；EAV 设计把"选哪些指标做因子"的决策留给
-- Phase 1 因子研究层（用 PIVOT 展开成宽表），数据层只负责完整、干净地落地。
--
-- 已知限制（记录以便 Phase 1 处理 point-in-time 对齐时参考）：
-- 该接口只提供"报告期"(report_date)，未提供"公告日"(announce_date)。
-- 严格防止未来函数需要额外补充公告日历数据源，在此之前默认按
-- "报告期 + N个月延迟"的保守假设近似公告日，不能直接假设报告期当天已知晓该数据。
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol          VARCHAR NOT NULL,
    report_date     DATE NOT NULL,   -- 报告期（如 2024-09-30）
    indicator       VARCHAR NOT NULL, -- 指标名称（原始中文列名，如"净资产收益率(%)"）
    value           DOUBLE,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, report_date, indicator)
);

-- 交易日历：Phase 0.6，因子滚动窗口/标签期限必须按交易日而非自然日计算
CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date  DATE NOT NULL,
    PRIMARY KEY (trade_date)
);

-- 基准指数日线（新浪源，东财源在本机网络下不可用）：Beta/超额收益/相对强弱计算的基准
CREATE TABLE IF NOT EXISTS index_quotes (
    index_code  VARCHAR NOT NULL,   -- 如 sh000300（沪深300）
    trade_date  DATE NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    volume      DOUBLE,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (index_code, trade_date)
);

-- 指数成分股：只能拿到当前快照（无免费历史成分变更源），靠周期性重跑本表自然累积历史，
-- 开工前的历史成分归属留白（已知缺口，见 docs/phase0.6-secondary-gap-assessment.md 项R）
CREATE TABLE IF NOT EXISTS index_constituents (
    index_code    VARCHAR NOT NULL,
    snapshot_date DATE NOT NULL,
    symbol        VARCHAR NOT NULL,
    updated_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (index_code, snapshot_date, symbol)
);

-- 股本变动历史（巨潮资讯，与disclosure_calendar同源）：市值/规模因子的基础输入，
-- announce_date 支持 point-in-time 对齐，不能直接假设 change_date 当天已公开。
CREATE TABLE IF NOT EXISTS share_changes (
    symbol              VARCHAR NOT NULL,
    change_date         DATE NOT NULL,     -- 变动生效日（对应股本变动公告的报告期）
    announce_date       DATE,              -- 公告日期
    total_shares        DOUBLE,            -- 总股本（万股，原始单位保留，用时自行换算）
    circulating_shares   DOUBLE,            -- 已流通股份（万股）
    change_reason       VARCHAR,
    updated_at          TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, change_date)
);

-- 分红配股历史（巨潮资讯）：除权日用于识别"复权价格跳变但非涨跌停"的场景，
-- 也是验证前复权计算是否正确的外部基准。
CREATE TABLE IF NOT EXISTS dividends (
    symbol              VARCHAR NOT NULL,
    plan_announce_date  DATE NOT NULL,     -- 实施方案公告日期
    dividend_type       VARCHAR,           -- 年度分红/中期分红等
    bonus_ratio         DOUBLE,            -- 每10股送股比例
    transfer_ratio       DOUBLE,            -- 每10股转增比例
    cash_ratio          DOUBLE,            -- 每10股派息金额（元，含税）
    record_date         DATE,              -- 股权登记日
    ex_date             DATE,              -- 除权除息日
    payment_date        DATE,              -- 派息日
    report_period       VARCHAR,           -- 对应报告期，如"2025年报"
    updated_at          TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, plan_announce_date)
);

-- 利润表绝对值（新浪源，EAV设计，与fundamentals一致）：营业总收入/营业利润/净利润等
-- 绝对金额，用于PS估值、同行业体量对比；fundamentals表只有比率型指标，不含绝对金额。
CREATE TABLE IF NOT EXISTS income_statement (
    symbol          VARCHAR NOT NULL,
    report_date     DATE NOT NULL,
    indicator       VARCHAR NOT NULL,
    value           DOUBLE,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, report_date, indicator)
);

-- ============================================================
-- 2026-08-25 起：Tushare Pro 2000积分档（付费200元/年）补齐的数据表。
-- 解决此前两轮免费源评估文档记录的已知缺口：复权因子(S)、退市股历史行情(B残留)、
-- 指数历史成分(R)、停复牌历史(T)；额外提供官方涨跌停价格与Phase 2行为金融原始数据。
-- ============================================================

-- 复权因子：qfq_price = raw_price * adj_factor(当日) / adj_factor(最新一天)，
-- 用于从未复权价格重建复权序列，或反向由qfq价格还原未复权价格（除权日判断用）
CREATE TABLE IF NOT EXISTS adj_factor (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE NOT NULL,
    adj_factor  DOUBLE,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date)
);

-- 每日市值/估值指标（Tushare官方，含市值，替代/补强 share_changes 的市值推算）：
-- 总市值/流通市值/PE/PB/PS 均为交易所口径官方计算值，规模因子与估值因子的核心输入
CREATE TABLE IF NOT EXISTS daily_basic (
    symbol          VARCHAR NOT NULL,
    trade_date      DATE NOT NULL,
    close           DOUBLE,
    turnover_rate   DOUBLE,
    turnover_rate_f DOUBLE,
    volume_ratio    DOUBLE,
    pe              DOUBLE,
    pe_ttm          DOUBLE,
    pb              DOUBLE,
    ps              DOUBLE,
    ps_ttm          DOUBLE,
    dv_ratio        DOUBLE,
    dv_ttm          DOUBLE,
    total_share     DOUBLE,
    float_share     DOUBLE,
    free_share      DOUBLE,
    total_mv        DOUBLE,
    circ_mv         DOUBLE,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date)
);

-- 指数历史成分股权重（月度快照，point-in-time）：解决"某股票在T日是否属于沪深300"
-- 这类历史判断，此前免费源只能拿到当前快照，本表首次补上历史部分
CREATE TABLE IF NOT EXISTS index_weight (
    index_code  VARCHAR NOT NULL,
    symbol      VARCHAR NOT NULL,
    trade_date  DATE NOT NULL,     -- 月度快照日期
    weight      DOUBLE,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (index_code, symbol, trade_date)
);

-- 停复牌历史（官方）：解决"该股票在T日是否可交易"的精确判断，
-- 此前免费源用universe+daily_quotes缺失做近似推断，本表提供官方确认
CREATE TABLE IF NOT EXISTS suspend_calendar (
    symbol          VARCHAR NOT NULL,
    trade_date      DATE NOT NULL,
    suspend_type    VARCHAR,       -- S=停牌 R=复牌
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date)
);

-- 官方每日涨跌停价格：与 research/a_share_rules.py 自行计算的规则互相校验，
-- 也可直接作为回测引擎"该价位是否可成交"判断的权威数据源
CREATE TABLE IF NOT EXISTS limit_price (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE NOT NULL,
    up_limit    DOUBLE,
    down_limit  DOUBLE,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date)
);

-- ---- Phase 2 行为金融层原始数据预取（提前抓取，不代表提前开工建模） ----

-- 龙虎榜每日明细：大额异动交易席位数据，用户明确提到的"大单买入/小单卖出"场景原料
CREATE TABLE IF NOT EXISTS dragon_tiger_list (
    symbol          VARCHAR NOT NULL,
    trade_date      DATE NOT NULL,
    name            VARCHAR,
    close           DOUBLE,
    pct_change      DOUBLE,
    turnover_rate   DOUBLE,
    amount          DOUBLE,
    l_sell          DOUBLE,
    l_buy           DOUBLE,
    l_amount        DOUBLE,
    net_amount      DOUBLE,
    net_rate        DOUBLE,
    amount_rate     DOUBLE,
    float_values    DOUBLE,
    reason          VARCHAR,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date, reason)
);

-- 大宗交易：机构/大户折溢价交易明细，羊群/资金动向代理指标原料
CREATE TABLE IF NOT EXISTS block_trade (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE NOT NULL,
    price       DOUBLE,
    vol         DOUBLE,
    amount      DOUBLE,
    buyer       VARCHAR,
    seller      VARCHAR,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date, price, vol, buyer, seller)
);

-- 融资融券交易汇总（按交易所）：杠杆资金/风险偏好代理指标
CREATE TABLE IF NOT EXISTS margin_balance (
    exchange_id VARCHAR NOT NULL,
    trade_date  DATE NOT NULL,
    rzye        DOUBLE,   -- 融资余额
    rzmre       DOUBLE,   -- 融资买入额
    rzche       DOUBLE,   -- 融资偿还额
    rqye        DOUBLE,   -- 融券余额
    rqmcl       DOUBLE,   -- 融券卖出量
    rzrqye      DOUBLE,   -- 融资融券余额
    rqyl        DOUBLE,   -- 融券余量
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (exchange_id, trade_date)
);

-- 个股资金流向（大中小单分类）：羊群效应/资金动向代理指标核心数据
CREATE TABLE IF NOT EXISTS moneyflow (
    symbol          VARCHAR NOT NULL,
    trade_date      DATE NOT NULL,
    buy_sm_vol      DOUBLE,
    buy_sm_amount   DOUBLE,
    sell_sm_vol     DOUBLE,
    sell_sm_amount  DOUBLE,
    buy_md_vol      DOUBLE,
    buy_md_amount   DOUBLE,
    sell_md_vol     DOUBLE,
    sell_md_amount  DOUBLE,
    buy_lg_vol      DOUBLE,
    buy_lg_amount   DOUBLE,
    sell_lg_vol     DOUBLE,
    sell_lg_amount  DOUBLE,
    buy_elg_vol     DOUBLE,
    buy_elg_amount  DOUBLE,
    sell_elg_vol    DOUBLE,
    sell_elg_amount DOUBLE,
    net_mf_vol      DOUBLE,
    net_mf_amount   DOUBLE,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date)
);

-- 沪深港通资金流向（全市场按日一条）：北向/南向当日净流入
CREATE TABLE IF NOT EXISTS moneyflow_hsgt (
    trade_date      DATE NOT NULL,
    ggt_ss          DOUBLE,   -- 港股通（上海）
    ggt_sz          DOUBLE,   -- 港股通（深圳）
    hgt             DOUBLE,   -- 沪股通
    sgt             DOUBLE,   -- 深股通
    north_money     DOUBLE,   -- 北向资金
    south_money     DOUBLE,   -- 南向资金
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (trade_date)
);

-- 股权质押统计：治理风险/流动性风险代理指标
CREATE TABLE IF NOT EXISTS pledge_stat (
    symbol          VARCHAR NOT NULL,
    end_date        DATE NOT NULL,
    pledge_count    DOUBLE,
    unrest_pledge   DOUBLE,
    rest_pledge     DOUBLE,
    total_share     DOUBLE,
    pledge_ratio    DOUBLE,
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, end_date)
);

-- 股东人数：股权分散度变化，处置效应/散户集中度代理指标
CREATE TABLE IF NOT EXISTS holder_number (
    symbol      VARCHAR NOT NULL,
    ann_date    DATE NOT NULL,
    end_date    DATE,
    holder_num  DOUBLE,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, ann_date)
);

-- Phase 2 模型生命周期：每次 advice/scanner.py 打分都落一份记录，用于漂移监控
-- （累积足够交易日后，才能算"预测分数 vs 后续真实收益"的样本外IC）与建议可追溯性
-- （对应计划文档Phase4验收"任意建议可追溯到具体模型版本"）。
CREATE TABLE IF NOT EXISTS prediction_log (
    symbol        VARCHAR NOT NULL,
    trade_date    DATE NOT NULL,      -- 打分所用快照对应的交易日（point-in-time）
    model_run_id  VARCHAR NOT NULL,   -- 对应 mlops/registry/<run_id>/
    pred_score    DOUBLE,
    rank          INTEGER,
    is_tradable   BOOLEAN,
    scanned_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date, model_run_id)
);

-- Phase 4 持仓驾驶舱：手动持仓录入（本项目不接券商API，MVP路线图明确"账户导入/手动持仓"，
-- 见 docs/07-产品设计启示/03-MVP路线图.md Phase1条款；"全自动实盘无人值守"是明确延后项）。
-- 一只股票可能有多笔建仓记录（分批建仓），用 lot_id 区分，便于后续按批次计算止盈止损。
CREATE TABLE IF NOT EXISTS positions (
    lot_id      VARCHAR NOT NULL,   -- 用户输入或系统生成的建仓批次号
    symbol      VARCHAR NOT NULL,
    shares      DOUBLE NOT NULL,
    cost_price  DOUBLE NOT NULL,    -- 每股建仓成本（含费用摊入可选，此处存税费前价）
    opened_at   DATE NOT NULL,
    note        VARCHAR,
    is_closed   BOOLEAN DEFAULT FALSE,
    closed_at   DATE,
    updated_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (lot_id)
);

-- Phase 4 建议卡片留痕：对齐 docs/07-产品设计启示/02-场景化建议引擎.md 的建议卡片Schema，
-- "任意建议可追溯"——每次 advice/advice_engine.py 生成的卡片都落一条记录，可回放当时输入。
CREATE TABLE IF NOT EXISTS advice_log (
    advice_id     VARCHAR NOT NULL,
    as_of         DATE NOT NULL,
    symbol        VARCHAR NOT NULL,
    name          VARCHAR,            -- 股票简称，UI展示用，不参与任何判定逻辑
    action        VARCHAR NOT NULL,   -- watch|open|add|reduce|close|stop_loss|take_profit|rebalance
    confidence    DOUBLE,
    plain_summary VARCHAR,            -- 大白话一句话总结，供首页「今日决策速览」直接展示
    price_levels_json VARCHAR,        -- JSON对象：{last_close, cost_price, stop_loss_price, take_profit_price}
    reasons_json  VARCHAR,            -- JSON数组，可追溯的理由明细
    risks_json    VARCHAR,
    invalid_if_json VARCHAR,
    model_run_id  VARCHAR,            -- 对应 mlops/registry/<run_id>/，无模型参与时为空
    created_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (advice_id)
);

-- 抓取任务日志：用于断点续传、失败重试队列与运行审计
CREATE TABLE IF NOT EXISTS sync_log (
    task_name   VARCHAR NOT NULL,
    symbol      VARCHAR,
    status      VARCHAR NOT NULL,    -- success / failed
    message     VARCHAR,
    run_at      TIMESTAMP DEFAULT current_timestamp
);
"""


def get_db_path() -> Path:
    override = os.environ.get(_DB_PATH_ENV)
    if override:
        db_path = Path(override)
    else:
        db_path = resolve_path(get_config()["storage"]["duckdb_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def get_connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(get_db_path()))


def sidecar_parallel_active() -> bool:
    """auto_chain_runner 稍后启动的 Tushare 子进程读这个标志：旁路库已在并行抓，主库侧直接跳过。"""
    return resolve_path(_SIDECAR_LOCK).exists()



# 对已存在的表做增量列迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列，
# 之前 fundamentals 表 schema 漂移的教训（旧表结构长期与代码定义不一致却未被发现）
# 就是因为忽略了这一点。这里显式补齐，保证代码里定义的列在旧数据库上也会被加上。
_MIGRATIONS_SQL = [
    "ALTER TABLE universe ADD COLUMN IF NOT EXISTS list_date DATE",
    "ALTER TABLE universe ADD COLUMN IF NOT EXISTS delist_date DATE",
    "ALTER TABLE advice_log ADD COLUMN IF NOT EXISTS name VARCHAR",
    "ALTER TABLE advice_log ADD COLUMN IF NOT EXISTS plain_summary VARCHAR",
    "ALTER TABLE advice_log ADD COLUMN IF NOT EXISTS price_levels_json VARCHAR",
]


def init_schema(conn: duckdb.DuckDBPyConnection | None = None) -> None:
    own_conn = conn is None
    if conn is None:
        conn = get_connection()
    try:
        conn.execute(_SCHEMA_SQL)
        for stmt in _MIGRATIONS_SQL:
            conn.execute(stmt)
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    conn = get_connection()
    init_schema(conn)
    tables = conn.execute("SHOW TABLES").fetchall()
    print("已初始化的表:", [t[0] for t in tables])
    conn.close()
