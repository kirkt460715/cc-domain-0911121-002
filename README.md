# DataHarbor 数据产品管理

这是一个可独立运行的本地业务项目。核心模块保留数据接收、领域规则、持久化记录及查询接口；演练数据均为合成资料，不需要真实账号或外部业务服务。

## 运行环境

Node.js 22，核心业务不依赖第三方包。
门户检查另外使用 Python 与 Playwright 1.56.0，浏览器为该版本安装的 Chromium。依赖与浏览器仅在镜像构建时安装。
Dockerfile 固定运行时版本。首次准备执行 docker build -t local-product .，之后以 docker run --rm --network none local-product 运行完整检查；所有 HTTP 演练只在容器内部回环地址通信。

## 开发与运行

业务测试：
    npm test
编译或语法检查：
    npm run build
现有业务演示：
    npm run acceptance
输入端演练检查：
    python demo_support/check.py
门户采集单元与浏览器测试：
    npm run portal-test
门户采集端到端演练（候选项刷新、旧下载晚到、中断续跑、失败隔离与追溯）：
    npm run portal-drill
合并运行：
    sh demo_support/verify.sh

输入协议、样本与故障开关见 demo_support/协议.md。输入端检查只证明本地来源的约定行为；业务演示展示当前已有功能。调用者可以在独立临时数据目录重复运行，原始样本不会被覆盖。

## 合作方门户采集自动化

`collector/` 与 `src/portalbridge.js` 把门户手工导出做成可执行的浏览器自动化：每轮先确认页面实际选中的机构、期间、口径，再按本轮导出号精确接收下载，核对 CSV 自身的导出标识与筛选信息，确认属于本轮后才经现有映射、schema、质量门禁与权限检查写入 DataHarbor；同筛选批次重试不重复导入，失败文件与解释在 `.demo-state/collector/` 可查，运行与门户导出号、DataHarbor 版本双向可追溯。设计与命令见 collector/README.md。
