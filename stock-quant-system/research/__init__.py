"""Phase 1 研究层：因子、标签、验证切分、A股可成交约束。

本包的计算函数全部吃 pandas DataFrame / numpy，不打开 warehouse.duckdb，
因此可以在 Phase 0 财务批量抓取占用主库锁期间并行开发和单测。
真正扫全市场算因子，要等主库锁释放后再接数据读取层。
"""
