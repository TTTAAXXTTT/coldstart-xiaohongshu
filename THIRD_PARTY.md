# 第三方组件与许可证

工作台代码沿用 MIT，见根目录与 coldstart/LICENSE。作者信息见 AUTHORS.md。

- 小红书服务及创作者桥基于 xpzouying/xiaohongshu-mcp v2.7.3，固定提交、改动及构建说明见 tools/creator-bridge/SOURCE.json 和 README.md，许可证为 Apache-2.0。
- Go 依赖的许可证副本见 tools/creator-bridge/third-party-licenses/INDEX.json 及同目录各 LICENSE 文件。
- 随仓库提供的 Windows 可执行文件与源码构建来源保留，校验值见 tools/sources.lock.json。桥接源码可按 tools/creator-bridge/build.ps1 重建。
- Python 从官方来源准备，Pillow 由 pip 安装；相关许可证随各自发行版提供。
- Windows 字体及模型服务不作为自有开源组件分发；模型凭据与调用费用由使用者自行管理。

原有许可证中的历史夹具路径若未包含在当前专版中，不代表本仓库包含该夹具。第三方版权和授权范围分别适用，不以工作台的 MIT 许可证覆盖第三方许可证。
