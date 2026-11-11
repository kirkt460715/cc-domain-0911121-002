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
合并运行：
    sh demo_support/verify.sh

输入协议、样本与故障开关见 demo_support/协议.md。输入端检查只证明本地来源的约定行为；业务演示展示当前已有功能。调用者可以在独立临时数据目录重复运行，原始样本不会被覆盖。
