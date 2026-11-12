# 合作方门户浏览器采集

把运营在合作方数据门户里的手工导出，变成可执行、可追溯、可重复的浏览器自动化，并在确认文件属于本轮后才写入 DataHarbor。首版面向仓库内的合成演练门户（`portal/server.py`），不依赖任何真实账号或下载 API。

## 要解决的事故形态

运营切了机构却下载了上一轮的结果，而两份 CSV 列结构完全相同，质量校验无法区分。本采集器不依赖列结构或元素位置，而是：

1. 每轮先从 **页面实际 DOM** 重新确认机构、期间、口径三项；
2. 触发导出后，按本轮条目的 `data-export-id` 在下载列表中精确匹配，不碰其他条目；
3. 下载后核对 **CSV 文件自身** 的 `export_id/org/period/metric`，与本轮确认值逐项比对；
4. 只有来源校验通过，才调用 DataHarbor 桥接创建数据版本。

## 目录与不可信数据隔离

代码位于 `collector/` 与 `src/portalbridge.js`、`src/portal-cli.js`；下载文件是不可信输入，统一落在独立的状态根目录（默认 `.demo-state/collector/`，已在 `.gitignore` 忽略），与代码目录分离：

```
<state-root>/
  runs/<run-id>/
    input.json            本轮筛选与故障配置
    <export-id>.csv       浏览器实际接收的文件
    receive.json          传给 Harbor 桥接的输入
    result.json           成功/隔离结果（门户导出号 + Harbor 版本号）
    failure.json          失败解释（阶段、错误类型、上下文）
  quarantine/<run-id>-<export-id>.csv   来源校验失败、禁止入库的文件
  ledger.jsonl            每轮追加的运行台账
  drill-report.txt        端到端演练的完整文字记录
```

Python 一律以 `-I` 运行，Node 桥接以子进程方式调用且工作目录不在下载目录内，避免下载目录中被植入的模块被解释器加载。

## 浏览器侧的可靠性设计（`collector/browser.py`）

- **无固定 sleep、无位置点击**：所有等待都针对真实状态——导出按钮启用、`#selected` 文本等于目标筛选、下载列表出现新的 `data-export-id`、本轮条目自己的下载锚点可见。
- **联动下拉与延迟候选项**：机构切换会先禁用导出按钮并异步拉取候选项；采集器等按钮重新启用且页面确认值反映新机构后才继续，页面自身的 generation 防护与采集器的确认值双重避免旧响应覆盖。
- **后台生成条目**：下载条目按导出号精确匹配，上一轮迟到的条目即使已经变成可下载链接也不会被误取。
- **会话隔离**：每次 `collect()` 都新建独立浏览器上下文与 cookie，同一机构的两次运行不共享服务端会话选择状态；独立会话读取对方的导出号会得到 404。
- **可理解的停机点**：登录失效抛 `LoginExpired`（stage=session），筛选项撤回（服务端 422）抛 `FilterWithdrawn`（stage=filters），导出超时抛 `ExportTimeout`，全部带中文解释与上下文并写入 `failure.json`。

## DataHarbor 侧的来源契约（`src/portalbridge.js`）

- 固定列 `export_id,org,period,metric,record_id,amount`、显式逐列映射、固定 schema 与质量门禁（至少 1 行、拒绝率为 0、金额非负）。
- 入库前第二道来源校验 `verifyPortalCsv`：文件内导出号与冻结筛选不匹配直接拒绝。
- 确定性 `importId = portal-<org>-<period>-<metric>`：同一逻辑筛选批次的重试复用同一 importId。
- 权限沿用既有 `requireSteward`：只有 owner 机构的 steward 能入库，非授权 actor 在写版本前被拒。
- 映射、schema 校验、质量门禁、对象存储全部走既有 `Harbor.import` 路径，不另起一套。

### 重试为什么不会重复导入

门户每轮导出都有新的 `export_id`，因此字节级重放不成立。运行编排（`collector/runbook.py`）在入库前先用确定性 importId 查台账/版本：该筛选批次已有版本时直接复用，不再调用导入；首次入库则由 Harbor 内置的 replay/`import_conflict` 保护兜底。结果中 `replay=true, committed=false` 即表示未产生新版本。

### 失败文件与解释可查

- 来源校验失败：文件移入 `quarantine/`，`failure.json` 记录期望与实际导出号。
- 质量门禁失败：版本以 `quarantined` 状态创建，拒绝行证据可通过 `harbor.rejected` 查询，运行结果 `state=quarantined`。
- 登录失效、筛选撤回、中断：`state=failed` 并注明所处阶段，不产生任何版本。

## 追溯

- 运行 → 版本：`Runbook.trace_run(run_id)` 经台账把本轮导出号映射到 importId，再解析到不可变版本引用（如 `partner_metrics:2`），重试运行也能找到其实际复用的版本。
- 导出号 → 版本：`node src/portal-cli.js portal-trace --root <vault> --input <{"exportId":...}.json>` 直接按文件内导出号反查。

## 命令

```sh
# 端到端演练（自动在内置回环端口启动门户，九个场景，含断言）
python -I collector/drill.py
# 或指定已运行的门户
python -I collector/drill.py --portal-url http://127.0.0.1:8787

# 浏览器与来源校验测试
python -I tests_py/test_provenance.py
python -I tests_py/test_browser.py

# Harbor 桥接 CLI
node src/portal-cli.js portal-contract
node src/portal-cli.js portal-bootstrap --root .demo-state/vault
node src/portal-cli.js portal-receive --root .demo-state/vault --actor alice \
  --input receive.json --csv <run-dir>/<export-id>.csv
node src/portal-cli.js portal-trace --root .demo-state/vault --input trace.json
```

## 演练故障开关（仅演练用，生产保持关闭）

`run_once(..., faults={...})` / `Faults(...)` 支持：`expire_session`（登录失效）、`withdraw_filter`（筛选 422 撤回）、`export_delay`（后台生成晚到）、`fulfill_download`（替换或调包下载内容）、`interrupt_before_download`（下载前中断）。
