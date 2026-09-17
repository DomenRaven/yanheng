# 交接笔记 —— 写给下一个接手的Agent（2026-08-26）

> 这份文档是给下个月接手的Grok/Composer等模型看的"最小必读"，目的是让你不用重新探索
> 全仓库就能知道现在在哪、下一步能干什么、有哪些坑不要再踩一遍。详细历史见下面列出的
> 各文档，不在这里重复贴长内容。

## 1. 项目是什么、现在到哪一步了

个人炒股辅助本地应用「研衡 YanHeng」，A股量化研究+决策辅助工具，单用户本地Streamlit
应用，不接券商实盘交易。**Phase 0~4 全部完成，且完成后又做了两轮UI/UX打磨**：

- Phase 0~4 验收报告：`docs/phase0-acceptance-report.md` ~ `docs/phase4-acceptance-report.md`
- 第一轮UI/UX + exe打包：`docs/ux-upgrade-notes.md` 第1-5节
- 第二轮设计理论复核 + 全流程边界测试：`docs/ux-upgrade-notes.md` 第6-7节，`README.md` 第13节
- 顶层任务清单/进度：仓库根目录 `个人炒股辅助本地应用_76077b1a.plan.md`（**不要编辑这个
  plan文件本身**，todos状态在别处维护）
- Git：已推送到 `https://github.com/DomenRaven/yanheng` main分支，最新commit `19a5961`，
  working tree干净。**本仓库本地git身份是仓库级配置**（`git config user.name/user.email`
  只在这个repo里设成了`DomenRaven <DomenRaven@users.noreply.github.com>`，不是全局配置）。

## 2. 必须先读的强制规则（每个任务都适用，不是可选参考）

- `.cursor/rules/quant-dev-loop.mdc`：8闸门工作流（读文档对齐→定框架→开工→测试打分→
  验收→文档留痕→清理→微调下一任务），**禁止跳步**。
- `.cursor/rules/vibe-coding-ethics.mdc`：八荣八耻道德约束，核心是"不臆猜接口""不脑补
  业务""不懂装懂要如实说""不做省略校验的批量乱改"。
- **同内容 Skill**：`.cursor/skills/yanheng-dev-loop/`（给后续 Agent 加载；八句话原文禁止改写）。
  人工第二轮测试清单：`stock-quant-system/docs/manuals/人工可用性测试指南.md`。
  名称搜代码：`common/symbol_lookup.py`（首页/持仓/K线/AI解释）。

## 3. 当前系统边界与已知局限（如实列清单，不要重新发现一遍）

- 不接券商API，持仓手动录入，无真实成交滑点/费率。
- 组合优化assumed_IC=0.03是保守拍定值，非逐日重估；不含行业/风格中性化约束。
- LLM解释层默认用DashScope(通义千问)，走`.env`里的`DASHSCOPE_API_KEY`（**该文件不在
  git里，换机器需要用户自己填**）。聊天模型没有实时日历，会用训练记忆里的旧年份
  （常见是停在 2024）——这不是断网。`explain_assistant` 把本机 `date.today()` 和
  东方财富新闻的 `publish_time` 写进 system/user 提示；**不要为这件事改成 DeepSeek**
  （config 已支持，但换模型解决不了对时）。也不要默认打开 `enable_search`：
  OpenAI 兼容接口不返回搜索来源，破坏「只翻译已提供输入」边界。
- `历史建议复盘`页是简化统计(发出日收盘价→最新收盘价涨跌幅)，不是严格回测。
- exe(`研衡启动器.exe`)本身不含Python依赖，只是拉起`.venv`里的streamlit，换新机器仍需
  先跑一遍`pip install -r requirements.txt`。
- 已知但暂不影响功能的技术债：Streamlit `1.62.0`对`use_container_width`参数发了
  deprecation警告（建议迁移成`width='stretch'/'content'`），目前**仍然正常工作**，
  只是控制台有警告噪音，不是bug，优先级很低，有闲置额度时再顺手改。
- DuckDB 在 Windows 上写者独占 `.duckdb` 文件（只读连接同样打不开，已用临时库验证）。
  逐股抓取必须用 `common.db.write_session()` 短连接（进程内还有 `_WRITE_LOCK`，多线程
  HTTP 时打开库必须排队）；前端用 `connect_warehouse()`，锁冲突时显示中文提示而不是
  traceback。`scripts/test_duckdb_lock.py` 是回归。
- 2026-08-27 起：quotes / 新浪财务 / 巨潮公司行为走 HTTP 线程池；Tushare 步 `workers=1`。
  **不要恢复 sidecar 双库。** 也不要把 `reference_data` / `market_data` 与行情重叠——
  那些模块仍在 HTTP 期间 `get_connection()`，会把文件锁死。详见
  `docs/parallel-http-ingestion.md`。
- Phase3的深度学习挑战者模型（GAT-like）RankIC更好但回测收益没跑赢LightGBM冠军，
  按冠军-挑战者门禁保留LightGBM为生产模型，结论记录在`docs/phase3-acceptance-report.md`
  和`mlops/registry/promotion_log.jsonl`。

## 4. 环境操作坑（真实踩过，直接抄结论）

- **PowerShell不支持bash heredoc**（`$(cat <<'EOF' ... EOF)`会报语法错误）。git commit
  信息、多行文本一律先用 Write 工具写临时文件，再 `git commit -F 文件路径`，用完删除。
- **中文路径/内容在PowerShell终端里回显是乱码**（GBK控制台编码问题），但文件本身编码
  是对的——不要因为看到乱码就以为文件坏了，用 Read 工具读取确认，不要在shell里用
  `python -c "...中文..."`跑一次性检查脚本，一律写temp .py文件再执行、跑完删除。
- Streamlit dev server重启：先 `netstat -ano | findstr :8501` 找PID，
  `Stop-Process -Id <pid> -Force`，等1-2秒确认端口释放，再
  `.venv\Scripts\python.exe -m streamlit run app.py --server.port 8501 --server.headless true`。
  测试完记得 `Stop-Process -Force` 关掉，不要让dev server常驻占用端口。
- 改完UI相关代码后必须重启Streamlit进程才生效（Python模块缓存），刷新浏览器不够。
- `.venv`、`.env`、`data/*.duckdb`、`mlops/registry/*`(除了`champion.json`和
  `promotion_log.jsonl`)、`*.exe`、`build/`、`dist/`都在`.gitignore`里，正常不会误提交，
  但commit前建议 `git status` 快速扫一眼确认没有意外把大文件/密钥带进去。

## 5. Phase 5（进行中方向）：完整散户链 + 模拟盘

用户已确认要做 **完整方向 1**（选股/择时/仓位/买卖建议 + 模拟盘，年底约 1 万实操、不自动券商下单）。
**新窗口开工请先读**（按这个顺序）：

1. `docs/9.16-散户决策链需求规格.md` —— 2026-09-16 需求正文 + 与现有代码逐项对照 + 相对对接文档的微调  
2. `docs/9.16-项目状态报告.md` —— 当日进度（含数据更新监测、未实现项）  
3. `docs/phase5-development.md` + `docs/phase5-constraints.md` —— 模块/表/测试与硬约束  
4. `docs/retail-complete-phase5-handoff.md` —— 阶段 A–D、表结构草案、待决默认  
5. 理论补齐对照：`../../docs/00-总览/08-第四轮散户决策链补齐对照.md`  

实现未开工。阶段顺序仍是 A 模拟盘 → B 股数 → C 择时 → D 复盘。开发期不要占用正在跑的 `warehouse.duckdb` 写者。

## 6. 如果用户下次说"继续"，大概率会问的方向（按可能性排序，仅供参考不是任务清单）

1. 要把行情与日历类步骤重叠，先把 `reference_data` / `market_data` / 仍握连接的
   `tushare_*` 改成 fetch-then-`write_session`，不要开 sidecar 双库。见
   `docs/parallel-http-ingestion.md`。
2. 顺手清一下`use_container_width`的deprecation警告（低成本，见上面第3节）。
3. 真实使用一段时间后，`历史建议复盘`页数据攒够了，可能想看更严谨的复盘统计。
4. 可能会问要不要接入券商API做半自动下单（目前是明确的"不做"边界，如果用户要做需要
   重新评估合规/风控，不要贸然实现）。
5. 可能会有新的数据缺口/规则变化，先查`docs/03-量化方法/`和`research/a_share_rules.py`
   对齐，不要凭印象改规则相关代码。
6. 模型该重新训练/退役了？先看`mlops/drift_monitor.py`的漂移检测结果和
   `mlops/registry/promotion_log.jsonl`里上次训练/晋升的时间，不要凭感觉决定要不要重训。

## 7. 一句话总结给下一个Agent

Phase 0–4 功能完整、已验收。Phase 5 需求已落在 `docs/9.16-散户决策链需求规格.md`，
代码尚未动。用户再说「开工」时对照该规格的 Must 与对接文档阶段 A，**不要**在没有点名
阶段的情况下并行改建议引擎和择时。数据全量更新若仍在跑，只用只读监测，不杀写库进程。
