# 依赖与来源

- 自写工作台代码：MIT，见 `LICENSE`。
- 小红书服务与创作者会话桥：基于 `xpzouying/xiaohongshu-mcp` v2.7.3，提交 `3b352aa075d63cd5ebae546c9fe7d7d9b81fa7c9`，Apache-2.0；桥接源文件、修改、构建步骤及许可证位于 `tools/creator-bridge`。
- 桥接可执行文件及小红书服务均按 `tools/sources.lock.json` 的 SHA256 校验。桥接工具的协议版本独立于工作台版本。
- 桥接所用 Go 模块的实际许可证副本与索引位于 `tools/creator-bridge/third-party-licenses`。
- Pillow 13.0.0 用于本地 PNG 文字卡片排版；其发行许可证随安装依赖提供。
- Python 运行时由官方 python.org 来源准备，版本、来源和校验流程见来源锁与引导脚本。
- 模型服务由用户自行选择并配置，凭据、授权及账单由用户账户管理。豆包搜索仅接纳属于小红书域名且有实际引用标记的来源。

分发不包含账号 Cookie、创作者网页状态、API Key 或本机数据库。
