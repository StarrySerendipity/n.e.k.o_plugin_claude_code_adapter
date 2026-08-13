"""Claude Code Adapter 插件冒烟测试。"""

import subprocess
import sys
from pathlib import Path


def test_plugin_toml_exists():
    """测试 plugin.toml 文件存在。"""
    plugin_toml = Path(__file__).parent.parent / "plugin.toml"
    assert plugin_toml.exists(), "plugin.toml 必须存在"


def test_plugin_toml_valid():
    """测试 plugin.toml 内容有效。"""
    plugin_toml = Path(__file__).parent.parent / "plugin.toml"
    content = plugin_toml.read_text(encoding="utf-8")

    assert "[plugin]" in content, "必须包含 [plugin] 段"
    assert 'id = "claude_code_adapter"' in content, "插件 ID 必须正确"
    assert "entry" in content, "必须包含 entry 字段"


def test_models_importable():
    """测试 models 模块可以独立导入（无相对导入依赖）。"""
    import importlib.util

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location("models", root / "models.py")
    assert spec is not None, "models.py should be loadable"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "AdapterConfig")
    assert hasattr(mod, "SessionRecord")
    assert hasattr(mod, "AssistantMessage")
    assert hasattr(mod, "ExecuteResult")
    assert hasattr(mod, "CLIInvocation")


def test_errors_importable():
    """测试 errors 模块可以独立导入（无相对导入依赖）。"""
    import importlib.util

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location("errors", root / "errors.py")
    assert spec is not None, "errors.py should be loadable"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "classify_error")
    assert hasattr(mod, "ClassifiedError")
    assert hasattr(mod, "is_retryable")


def test_parser_importable():
    """测试 parser 模块在设置包结构后可以导入。"""
    import importlib.util
    import sys
    import types
    from pathlib import Path

    root = Path(__file__).parent.parent

    # 创建一个虚拟包来支持相对导入
    pkg_name = "_test_claude_code_adapter"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(root)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg

        # 先加载 models（parser 依赖它）
        models_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.models", root / "models.py"
        )
        models_mod = importlib.util.module_from_spec(models_spec)
        models_mod.__package__ = pkg_name
        sys.modules[f"{pkg_name}.models"] = models_mod
        models_spec.loader.exec_module(models_mod)

    # 加载 parser
    parser_spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.parser", root / "parser.py"
    )
    parser_mod = importlib.util.module_from_spec(parser_spec)
    parser_mod.__package__ = pkg_name
    sys.modules[f"{pkg_name}.parser"] = parser_mod
    parser_spec.loader.exec_module(parser_mod)

    # 验证类存在
    assert hasattr(parser_mod, "ClaudeOutputParser")
    assert hasattr(parser_mod, "ParsedStream")


def test_session_importable():
    """测试 session 模块在设置包结构后可以导入。"""
    import importlib.util
    import sys
    import types
    from pathlib import Path

    root = Path(__file__).parent.parent

    # 创建一个虚拟包来支持相对导入
    pkg_name = "_test_claude_code_adapter"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(root)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg

        # 先加载 models
        models_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.models", root / "models.py"
        )
        models_mod = importlib.util.module_from_spec(models_spec)
        models_mod.__package__ = pkg_name
        sys.modules[f"{pkg_name}.models"] = models_mod
        models_spec.loader.exec_module(models_mod)

    # 加载 session
    session_spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.session", root / "session.py"
    )
    session_mod = importlib.util.module_from_spec(session_spec)
    session_mod.__package__ = pkg_name
    sys.modules[f"{pkg_name}.session"] = session_mod
    session_spec.loader.exec_module(session_mod)

    # 验证函数存在
    assert hasattr(session_mod, "compute_prompt_signature")
    assert hasattr(session_mod, "SessionManager")


def test_provider_manager_importable():
    """测试 provider_manager 模块可以独立导入（纯标准库，无相对导入）。"""
    import importlib.util

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "provider_manager", root / "provider_manager.py"
    )
    assert spec is not None, "provider_manager.py should be loadable"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["provider_manager"] = mod
    spec.loader.exec_module(mod)
    assert hasattr(mod, "Provider")
    assert hasattr(mod, "ProviderManager")
    assert hasattr(mod, "ALLOWED_ENV_KEYS")


def test_provider_manager_switch_and_mask(tmp_path):
    """测试供应商注册/切换/打码/认证键互斥逻辑。"""
    import importlib.util

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "provider_manager_t2", root / "provider_manager.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["provider_manager_t2"] = mod
    spec.loader.exec_module(mod)

    mgr = mod.ProviderManager(str(tmp_path))

    # 注册带双认证键的 provider
    mgr.add_or_update(
        mod.Provider(
            name="relay",
            display_name="中转",
            env={
                "ANTHROPIC_BASE_URL": "https://relay.example.com",
                "ANTHROPIC_AUTH_TOKEN": "sk-secret-token-12345",
                "ANTHROPIC_API_KEY": "sk-ant-other",
            },
        )
    )
    mgr.set_active("relay")

    # 认证键互斥：只保留 AUTH_TOKEN
    env = mgr.env_overrides()
    assert env["ANTHROPIC_BASE_URL"] == "https://relay.example.com"
    assert "ANTHROPIC_AUTH_TOKEN" in env
    assert "ANTHROPIC_API_KEY" not in env

    # 列表打码
    listed = mgr.list_providers()
    assert listed[0]["active"] is True
    assert "sk-secret-token-12345" not in str(listed[0]["env"])

    # 持久化：新实例能读回激活状态
    mgr2 = mod.ProviderManager(str(tmp_path))
    mgr2.load()
    assert mgr2.get_active_name() == "relay"

    # 清除激活 → 空覆盖
    mgr.set_active("")
    assert mgr.env_overrides() == {}


# ---------------------------------------------------------------------------
# 虚拟包加载辅助（支持相对导入模块的独立测试）
# ---------------------------------------------------------------------------

_PKG_NAME = "_test_claude_code_adapter"


def _load_pkg_module(filename: str, modname: str):
    """在虚拟包内加载插件源码模块（支持模块间相对导入）。"""
    import importlib.util
    import sys
    import types

    root = Path(__file__).parent.parent
    if _PKG_NAME not in sys.modules:
        pkg = types.ModuleType(_PKG_NAME)
        pkg.__path__ = [str(root)]
        pkg.__package__ = _PKG_NAME
        sys.modules[_PKG_NAME] = pkg

    full = f"{_PKG_NAME}.{modname}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, root / filename)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = _PKG_NAME
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def test_claude_sessions_importable():
    """测试 claude_sessions 模块可以独立导入（纯标准库，无相对导入）。"""
    import importlib.util

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "claude_sessions", root / "claude_sessions.py"
    )
    assert spec is not None, "claude_sessions.py should be loadable"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_sessions"] = mod
    spec.loader.exec_module(mod)
    assert hasattr(mod, "scan_sessions")
    assert hasattr(mod, "load_messages")
    assert hasattr(mod, "delete_session")
    assert hasattr(mod, "claude_projects_root")
    assert hasattr(mod, "normalize_session_ref")
    assert hasattr(mod, "find_session_by_id")


def test_claude_sessions_scan_load_delete(tmp_path):
    """测试 cc-switch 风格会话扫描/消息加载/删除（含 sidecar 与 ID 校验）。"""
    import json

    mod = _load_pkg_module("claude_sessions.py", "claude_sessions_t2")

    project_dir = tmp_path / ".claude" / "projects" / "-D-proj-demo"
    project_dir.mkdir(parents=True)
    session_file = project_dir / "sess-001.jsonl"

    lines = [
        # meta 行（应被跳过）
        {
            "type": "summary",
            "isMeta": True,
            "summary": "内部元数据",
            "timestamp": "2026-08-12T10:00:00.000Z",
        },
        # 系统 caveat（标题提取应跳过）
        {
            "type": "user",
            "sessionId": "sess-001",
            "cwd": "D:\\proj\\demo",
            "timestamp": "2026-08-12T10:00:01.000Z",
            "message": {
                "role": "user",
                "content": "<local-command-caveat>系统注入</local-command-caveat>",
            },
        },
        # slash command（标题提取应跳过）
        {
            "type": "user",
            "sessionId": "sess-001",
            "timestamp": "2026-08-12T10:00:02.000Z",
            "message": {
                "role": "user",
                "content": "<command-name>/clear</command-name>",
            },
        },
        # 首条真实用户消息
        {
            "type": "user",
            "sessionId": "sess-001",
            "timestamp": "2026-08-12T10:00:03.000Z",
            "message": {"role": "user", "content": "修复登录表单校验"},
        },
        # assistant 消息（content block 数组）
        {
            "type": "assistant",
            "sessionId": "sess-001",
            "timestamp": "2026-08-12T10:00:04.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "好的，正在修复"}],
            },
        },
        # 全为 tool_result 的 user 消息 → 应重分类为 tool
        {
            "type": "user",
            "sessionId": "sess-001",
            "timestamp": "2026-08-12T10:00:05.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "工具返回数据"}],
            },
        },
        # custom-title（标题最高优先级）
        {
            "type": "custom-title",
            "sessionId": "sess-001",
            "timestamp": "2026-08-12T10:00:06.000Z",
            "customTitle": "登录表单改造",
        },
    ]
    session_file.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in lines),
        encoding="utf-8",
    )

    # sidecar 目录（删除会话时应一并删除）
    sidecar = project_dir / "sess-001"
    sidecar.mkdir()
    (sidecar / "subagent.json").write_text("{}", encoding="utf-8")

    # agent- 前缀文件应被跳过
    agent_file = project_dir / "agent-sub-002.jsonl"
    agent_file.write_text(
        json.dumps({"sessionId": "agent-sub-002", "cwd": "D:\\proj\\demo"}),
        encoding="utf-8",
    )

    # 扫描：只有 1 个会话，标题取 custom-title
    sessions = mod.scan_sessions(home=str(tmp_path))
    assert len(sessions) == 1
    meta = sessions[0]
    assert meta["session_id"] == "sess-001"
    assert meta["title"] == "登录表单改造"
    assert meta["project_dir"] == "D:\\proj\\demo"
    assert "--resume sess-001" in meta["resume_command"]
    assert meta["last_active_at"] is not None

    # 消息加载：isMeta 跳过，tool_result 重分类
    msgs = mod.load_messages(str(session_file), expected_session_id="sess-001")
    assert [m["role"] for m in msgs] == ["user", "user", "user", "assistant", "tool"]
    assert msgs[3]["content"] == "好的，正在修复"
    assert msgs[4]["content"] == "工具返回数据"

    # session_id 不匹配 → 拒绝加载（防串读）
    assert mod.load_messages(str(session_file), expected_session_id="other") == []

    # 删除：ID 校验失败 → 不删
    assert mod.delete_session(str(session_file), "wrong-id") is False
    assert session_file.exists()

    # 删除：ID 一致 → jsonl 和 sidecar 一起删除
    assert mod.delete_session(str(session_file), "sess-001") is True
    assert not session_file.exists()
    assert not sidecar.exists()


def test_task_manager_wait_and_resume():
    """测试智能等待 wait_for 和失败自动续跑（--resume）逻辑。"""
    import asyncio
    import time

    models = _load_pkg_module("models.py", "models")
    errors = _load_pkg_module("errors.py", "errors")
    _load_pkg_module("parser.py", "parser")
    _load_pkg_module("executor.py", "executor")
    tm = _load_pkg_module("task_manager.py", "task_manager")

    class _FakeStream:
        def __init__(self, session_id: str):
            self.session_id = session_id
            self.messages = []
            self.final_text = "任务完成"
            self.result = None

    class _FakeExecutor:
        """首次执行返回可重试错误（但已捕获 session_id），第二次成功。"""

        def __init__(self):
            self.calls = []

        async def execute(self, invocation, parser):
            self.calls.append(invocation)
            if len(self.calls) == 1:
                err = errors.ClassifiedError(
                    kind=errors.TRANSIENT_UPSTREAM,
                    message="503 upstream",
                    retryable=True,
                )
                return _FakeStream("sess-abc"), err
            return _FakeStream("sess-abc"), None

    async def main():
        config = models.AdapterConfig(command="fake-claude")
        mgr = tm.TaskManager(_FakeExecutor(), config)

        # 提交任务 → 失败一次后自动用 session_id 续跑成功
        record = await mgr.submit("hello", cwd="")
        res = await mgr.wait_for(record.task_id, max_wait_sec=10.0, interval_sec=1.0)
        assert res["status"] == "done", res
        assert res["session_id"] == "sess-abc"
        assert res["result"]["is_error"] is False

        executor = mgr._executor
        assert len(executor.calls) == 2, "应重试一次"
        assert "--resume" not in executor.calls[0].cmd, "首次执行不带 --resume"
        assert "--resume" in executor.calls[1].cmd, "续跑应带 --resume"
        assert "sess-abc" in executor.calls[1].cmd

        # wait_for 未知任务 → error
        err = await mgr.wait_for("nope", max_wait_sec=1.0)
        assert "error" in err

        # 仍在运行的任务 → 超时返回 still_running + 建议续等时长
        rec2 = tm.TaskRecord(task_id="t2", prompt="p", cwd="")
        rec2.status = tm.TaskStatus.RUNNING
        rec2.started_at = time.time() - 5
        mgr._tasks["t2"] = rec2
        out = await mgr.wait_for("t2", max_wait_sec=1.2, interval_sec=1.0)
        assert out["still_running"] is True
        assert out["suggested_next_wait_sec"] >= 5.0
        assert "建议" in out["message"]

    asyncio.run(main())


def test_uuid_normalize_and_find(tmp_path):
    """测试 UUID 规范化（命令形态/大小写/前缀）与存档定位。"""
    import json

    mod = _load_pkg_module("claude_sessions.py", "claude_sessions_t3")

    uuid = "d7d17ec1-bd47-49f0-a845-dfa5df8c33a6"

    # 规范化：命令形态 / 大写 / 引号包裹
    assert mod.normalize_session_ref(f"claude --resume {uuid.upper()}") == uuid
    assert mod.normalize_session_ref(f"  '{uuid}' ") == uuid
    assert mod.normalize_session_ref("") == ""

    # 构造存档：<uuid>.jsonl 归属 D:\proj\demo
    proj = tmp_path / ".claude" / "projects" / "-D-proj-demo"
    proj.mkdir(parents=True)
    (proj / f"{uuid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": uuid,
                "cwd": "D:\\proj\\demo",
                "timestamp": "2026-08-12T10:00:03.000Z",
                "message": {"role": "user", "content": "修复登录表单校验"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 精确匹配（大写也可）
    meta = mod.find_session_by_id(uuid.upper(), home=str(tmp_path))
    assert meta is not None
    assert meta["session_id"] == uuid
    assert meta["project_dir"] == "D:\\proj\\demo"

    # 前缀匹配（面板截断形态）与命令形态
    meta2 = mod.find_session_by_id("d7d17ec1", home=str(tmp_path))
    assert meta2 is not None and meta2["session_id"] == uuid
    meta3 = mod.find_session_by_id(f"claude --resume {uuid}", home=str(tmp_path))
    assert meta3 is not None and meta3["session_id"] == uuid

    # 不存在 → None
    assert (
        mod.find_session_by_id(
            "ffffffff-0000-0000-0000-000000000000", home=str(tmp_path)
        )
        is None
    )


def test_resolve_resume_uuid_locates_project_dir(tmp_path):
    """测试 resume 模式：自动定位会话归属目录、cwd 冲突报错、宽容 UUID。"""
    import asyncio
    import json

    models = _load_pkg_module("models.py", "models")
    pm = _load_pkg_module("_plugin_main.py", "_plugin_main")
    cs = pm.claude_sessions

    uuid = "d7d17ec1-bd47-49f0-a845-dfa5df8c33a6"
    proj = tmp_path / ".claude" / "projects" / "-D-proj-demo"
    proj.mkdir(parents=True)
    (proj / f"{uuid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": uuid,
                "cwd": "D:\\proj\\demo",
                "timestamp": "2026-08-12T10:00:03.000Z",
                "message": {"role": "user", "content": "修复登录表单校验"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 将存档根指向临时目录
    orig_root = cs.claude_projects_root
    cs.claude_projects_root = lambda home=None: str(tmp_path / ".claude" / "projects")

    class _FakeMgr:
        async def find_resumable(self, cwd, signature):
            return None

    try:
        p = pm.ClaudeCodeAdapterPlugin(None)
        p._config = models.AdapterConfig()
        p._session_mgr = _FakeMgr()

        async def main():
            # 命令形态 UUID + 不传 cwd → 自动采用会话归属目录
            rid, rcwd, title, err = await p._resolve_resume_id(
                session_mode="resume",
                session_id=f"claude --resume {uuid}",
                cwd="",
                signature="sig",
            )
            assert err == ""
            assert rid == uuid
            assert rcwd == "D:\\proj\\demo"
            assert "登录表单" in title

            # 显式传不一致的 cwd → 清晰报错
            _, _, _, err2 = await p._resolve_resume_id(
                session_mode="resume",
                session_id=uuid,
                cwd="D:\\other\\dir",
                signature="sig",
            )
            assert "归属目录" in err2

            # 显式传一致的 cwd → 通过
            rid3, _, _, err3 = await p._resolve_resume_id(
                session_mode="resume",
                session_id=uuid,
                cwd="D:\\proj\\demo",
                signature="sig",
            )
            assert err3 == "" and rid3 == uuid

            # 未知 UUID → 报错并提示查看历史会话
            _, _, _, err4 = await p._resolve_resume_id(
                session_mode="resume",
                session_id="ffffffff-0000-0000-0000-000000000000",
                cwd="",
                signature="sig",
            )
            assert "未找到" in err4

            # resume 缺 session_id → 报错
            _, _, _, err5 = await p._resolve_resume_id(
                session_mode="resume", session_id="", cwd="", signature="sig"
            )
            assert err5

            # new / 非法模式
            assert await p._resolve_resume_id(
                session_mode="new", session_id="", cwd="", signature="sig"
            ) == ("", "", "", "")
            _, _, _, err6 = await p._resolve_resume_id(
                session_mode="weird", session_id="", cwd="", signature="sig"
            )
            assert "session_mode" in err6

        asyncio.run(main())
    finally:
        cs.claude_projects_root = orig_root


def test_followup_interrupt_and_resume(tmp_path):
    """测试执行中追加纠正指令：中断运行中任务→同会话续发；按 UUID 续发。"""
    import asyncio
    import json
    from unittest.mock import MagicMock

    models = _load_pkg_module("models.py", "models")
    _load_pkg_module("errors.py", "errors")
    _load_pkg_module("parser.py", "parser")
    _load_pkg_module("executor.py", "executor")
    tm = _load_pkg_module("task_manager.py", "task_manager")
    pm = _load_pkg_module("_plugin_main.py", "_plugin_main")
    cs = pm.claude_sessions

    class _FakeStream:
        def __init__(self, session_id: str):
            self.session_id = session_id
            self.messages = []
            self.final_text = "已按纠正指令继续"
            self.result = None

    class _GateExecutor:
        """首次调用：先吐 init 事件（早期捕获 session_id）再阻塞等待中断；
        后续调用：立即成功返回。"""

        def __init__(self):
            self.calls = []
            self.gate = asyncio.Event()

        async def execute(self, invocation, parser):
            self.calls.append(invocation)
            if len(self.calls) == 1:
                parser.parse_line(
                    json.dumps(
                        {
                            "type": "system",
                            "subtype": "init",
                            "session_id": "sess-live",
                            "cwd": "D:\\demo",
                        }
                    ).encode("utf-8")
                )
                await self.gate.wait()  # 阻塞直到被 cancel
                return _FakeStream("sess-live"), None
            return _FakeStream("sess-live"), None

    # 构造历史会话存档（供 session_id 路径定位归属目录）
    hist_uuid = "aaaa1111-2222-3333-4444-555566667777"
    proj = tmp_path / ".claude" / "projects" / "-D-proj-hist"
    proj.mkdir(parents=True)
    (proj / f"{hist_uuid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": hist_uuid,
                "cwd": "D:\\proj\\hist",
                "timestamp": "2026-08-12T10:00:03.000Z",
                "message": {"role": "user", "content": "旧会话首条指令"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    orig_root = cs.claude_projects_root
    cs.claude_projects_root = lambda home=None: str(tmp_path / ".claude" / "projects")

    try:

        async def main():
            config = models.AdapterConfig(command="fake-claude")
            executor = _GateExecutor()
            plugin = pm.ClaudeCodeAdapterPlugin(None)
            plugin._ready = True
            plugin._config = config
            plugin._executor = MagicMock()
            plugin._session_mgr = MagicMock()
            plugin._provider_mgr = None
            plugin._task_mgr = tm.TaskManager(executor, config)

            # 1. 提交一个长任务，等待进入 RUNNING 且 session_id 已早期捕获
            rec = await plugin._task_mgr.submit("实现功能", cwd="D:\\demo")
            for _ in range(200):
                if rec.status == tm.TaskStatus.RUNNING and rec.session_id:
                    break
                await asyncio.sleep(0.02)
            assert rec.session_id == "sess-live", "init 事件应被早期捕获"

            # 2. followup(task_id)：中断跑偏任务 → 同一会话续发
            out = await plugin.claude_code_followup(
                instruction="方向不对，改回方案 A", task_id=rec.task_id
            )
            assert isinstance(out, dict), out
            assert out["session_id"] == "sess-live"
            assert out.get("interrupted_task_id") == rec.task_id
            assert rec.status == tm.TaskStatus.CANCELLED
            executor.gate.set()  # 释放原任务协程退出
            await asyncio.sleep(0.05)

            res = await plugin._task_mgr.wait_for(
                out["task_id"], max_wait_sec=5.0, interval_sec=0.1
            )
            assert res["status"] == "done", res
            # 续发必须带 --resume 同一 UUID
            assert "--resume" in executor.calls[1].cmd
            assert "sess-live" in executor.calls[1].cmd
            assert executor.calls[1].cmd[-1] != "--resume"

            # 3. followup(session_id)：命令形态 UUID → 定位归属目录续发
            out2 = await plugin.claude_code_followup(
                instruction="继续修 bug",
                session_id=f"claude --resume {hist_uuid.upper()}",
            )
            assert isinstance(out2, dict), out2
            assert out2["session_id"] == hist_uuid
            res2 = await plugin._task_mgr.wait_for(
                out2["task_id"], max_wait_sec=5.0, interval_sec=0.1
            )
            assert res2["status"] == "done", res2
            assert executor.calls[2].cwd == "D:\\proj\\hist", "应自动切到会话归属目录"
            assert hist_uuid in executor.calls[2].cmd

            # 4. 既不给 task_id 也不给 session_id → 报错
            err = await plugin.claude_code_followup(instruction="x")
            assert isinstance(err, Exception)
            # instruction 为空 → 报错
            err2 = await plugin.claude_code_followup(
                instruction="", task_id=rec.task_id
            )
            assert isinstance(err2, Exception)
            # 未知 task_id → 报错
            err3 = await plugin.claude_code_followup(instruction="x", task_id="nope")
            assert isinstance(err3, Exception)

        asyncio.run(main())
    finally:
        cs.claude_projects_root = orig_root


if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"])
