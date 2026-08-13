# Claude Code Adapter

让猫娘指挥 Claude Code 干活的 N.E.K.O 插件。把 Claude Code CLI 注册为猫娘可调用的工具集，支持同步/异步执行、智能等待、会话续跑控制、cc-switch 风格多供应商切换，并内置 cc-switch 风格的会话管理面板。

## 前置条件

本机已安装 Claude Code CLI：

```bash
npm install -g @anthropic-ai/claude-code
```

## 使用教程：怎么和猫娘说

你不需要记任何工具名，用自然语言跟猫娘说即可，下面是推荐话术：

### 1. 短任务（预计 5 分钟内完成）

> 用 Claude Code 帮我把 `D:\myproject` 里的登录页加上表单校验

猫娘会调用 `claude_code_execute` 同步执行（上限 300 秒），完成后直接回复 Claude Code 的结果、会话 ID、费用和轮次。

### 2. 长任务（超过 5 分钟）

> 提交一个 Claude Code 后台任务：把整个项目的测试覆盖率补到 80%

猫娘会：
1. `claude_code_submit` 提交任务拿到 `task_id`；
2. 根据任务复杂度**自行预估耗时**，调用 `claude_code_wait` 智能等待——插件内部自适应退避轮询，任务一完成立即返回，不空转；
3. 若一次没等到，按返回的 `suggested_next_wait_sec` 继续等；
4. 完成后主动把结果汇报给你。

你也可以随时问「刚才那个任务跑得怎么样了」，猫娘会用 `claude_code_poll` 单次查询；想中止就说「取消那个任务」。

### 3. 会话控制（重点）

Claude Code 的「会话」决定上下文是否延续。本插件提供三档明确的控制，猫娘不会再「想续会话却开了新会话」：

| 你想做什么 | 怎么跟猫娘说 | 背后行为 |
|---|---|---|
| 接着上次继续（默认） | 「继续刚才的项目改一下 xxx」 | `session_mode=auto`，自动复用同目录会话 |
| 强制开新会话 | 「开个新会话来做这个」 | `session_mode=new` |
| 接回指定的旧会话 | 「继续这个会话：`<UUID>`」 | `session_mode=resume` + `session_id` |

会话 UUID 从哪来？打开插件面板的**会话管理**页，左侧列表点选会话，右侧标题栏有 UUID，点击即可复制。把 UUID 发给猫娘即可精确续跑。

任务失败时插件会自动用 `session_id` 以 `--resume` 续跑而非重开新会话，尽量保留上下文。

### 4. 供应商切换（cc-switch 风格）

> 切换 Claude 供应商到 relay

- `claude_code_list_providers`：列出所有供应商（token 打码显示）
- `claude_code_switch_provider`：切换/注册供应商；传空名称则回退用户全局 `~/.claude` 配置
- `claude_code_remove_provider`：删除供应商

原理与 cc-switch 相同：通过 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 等环境变量切换 API 端点，但**只注入 CLI 子进程**，不写你的全局 `~/.claude/settings.json`，随时可回退。自定义供应商写入插件目录下的 `providers.json`（已 gitignore），模板见 `providers.example.json`。

## 前端面板：会话管理

面板界面照搬 cc-switch 的「会话管理」页：

- **左侧**：会话列表（标题 + 项目名 + 最近活跃时间），按最近活跃排序
- **右侧**：会话详情——标题、创建时间、**会话 UUID（点击复制）**、项目目录、完整对话内容（用户/AI/工具消息分色气泡展示）
- **删除会话**：右上角红色按钮，确认后删除会话文件及附属数据

数据源是 Claude CLI 原生的会话存档（`~/.claude/projects/**/*.jsonl`），因此不仅猫娘发起的会话，你自己在终端里跑 `claude` 的会话也能看到。

## 工具全集

| 工具 | 用途 |
|---|---|
| `claude_code_execute` | 同步执行（≤300s），支持 session_mode/session_id |
| `claude_code_submit` | 提交异步任务，返回 task_id，支持 session_mode/session_id |
| `claude_code_wait` | 智能等待任务完成（自适应退避，超时给出续等建议） |
| `claude_code_poll` | 单次查询任务状态 |
| `claude_code_cancel` | 取消任务 |
| `claude_code_check_health` | 检查 CLI 是否可用、版本、配置 |
| `claude_code_list_sessions` | 列出插件维护的可恢复会话索引 |
| `claude_code_clear_session` | 清除会话索引（按目录或全部） |
| `claude_code_get_config` | 查看当前适配器配置 |
| `claude_code_list_providers` | 列出供应商 |
| `claude_code_switch_provider` | 切换/注册供应商 |
| `claude_code_remove_provider` | 删除供应商 |

## 配置（plugin.toml `[claude_code]` 节）

```toml
command = ""                      # CLI 路径，留空自动检测
model = ""                        # 默认模型，留空用 CLI 默认
effort = ""                       # "" | "low" | "medium" | "high"
max_turns_per_run = 0             # 单次最大轮次，0=默认
dangerously_skip_permissions = true
timeout_sec = 300                 # 单次执行超时
cwd = ""                          # 默认工作目录
append_system_prompt_file = ""    # 附加系统提示文件
skills_dir = ""                   # 技能目录（--add-dir）
max_retries = 1                   # 失败自动重试次数
```

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

本地测试：

```bash
python -m pytest tests/ -v
ruff check . && ruff format --check .
```

## 市场发布

推送与 `plugin.toml` 版本匹配的标签以创建 GitHub Release 资源：

```bash
git tag v0.3.0
git push origin v0.3.0
```

`.github/workflows/release.yml` 会上传 `claude_code_adapter.neko-plugin`，在插件市场发布版本时使用该 GitHub Release URL。

## 入口点

```toml
entry = "plugins.claude_code_adapter:ClaudeCodeAdapterPlugin"
```

## 版本历史

### v0.3.0 (2026-08-13)
- 新增 `claude_code_wait` 智能等待工具：插件内部自适应退避轮询，任务完成立即返回，超时给出 `suggested_next_wait_sec` 续等建议
- 新增会话控制：execute/submit 支持 `session_mode`（auto/new/resume）+ `session_id`，明确控制续跑或新开会话
- 异步任务失败时自动用 `session_id` 续跑而非重开新会话
- 新增 cc-switch 风格的会话管理面板：会话列表、对话详情、UUID 复制、删除会话（数据源为 `~/.claude/projects` 原生存档）
- poll/wait 返回中携带 `session_id`，方便猫娘记忆与续跑
- plugin.toml 描述升级为完整使用教程

### v0.2.0 (2026-08-12)
- 借鉴 cc-switch 新增多供应商切换（list/switch/remove_provider）
- provider env 经子进程注入，不污染全局配置

### v0.1.0 (2026-07-27)
- 初始版本发布
- 通过 @llm_tool 将 Claude Code CLI 注册为猫娘可调用的工具集
- 支持执行项目开发任务：写代码、改 bug、跑测试、查文档
- 支持会话恢复、技能注入、流式输出解析和错误自动恢复
