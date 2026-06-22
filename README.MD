# Claude Code Adapter

将 Claude Code CLI 的输出转换为 N.E.K.O 插件可消费的标准化事件流。

## 功能特性

- 实时解析 Claude Code 的 JSON 流输出
- 支持多种事件类型：assistant 消息、工具调用、权限请求、错误等
- 提供会话管理和命令执行接口
- 类型安全的错误处理

## 开发

此仓库设计为位于：

```text
N.E.K.O/plugin/plugins/claude_code_adapter
```

发布到插件市场时，使用此 GitHub 仓库名：

```text
n.e.k.o_plugin_claude_code_adapter
```

从 N.E.K.O 仓库根目录运行检查：

```bash
uv run python -m plugin.neko_plugin_cli.cli check claude_code_adapter
uv run python -m plugin.neko_plugin_cli.cli check -r claude_code_adapter
```

## 市场发布

推送与 `plugin.toml` 版本匹配的标签以创建 GitHub Release 资源：

```bash
git tag v0.1.0
git push origin v0.1.0
```

生成的 `.github/workflows/release.yml` 会上传 `claude_code_adapter.neko-plugin`。
在插件市场发布版本时使用该 GitHub Release URL。

## 入口点

```toml
entry = "plugins.claude_code_adapter:ClaudeCodeAdapterPlugin"
```
