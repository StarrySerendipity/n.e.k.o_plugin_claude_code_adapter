"""Claude Code Adapter 插件主类。

从 __init__.py 拆分出来，保证 entry 类位于模块顶层，
满足 neko-plugin release check 的 AST 静态校验要求。

工具集：
- claude_code_execute: 同步执行（短任务，最长 300s）
- claude_code_submit / claude_code_poll / claude_code_wait /
  claude_code_cancel: 异步提交 + 智能等待/轮询模式，
  避免 main_server 的 300 秒超时限制
- claude_code_check_health / claude_code_list_sessions /
  claude_code_clear_session / claude_code_get_config: 辅助工具
- claude_code_list_providers / claude_code_switch_provider /
  claude_code_remove_provider: cc-switch 风格的多供应商切换

会话控制（v0.4.0）：
execute / submit 支持 session_mode（auto/new/resume）+ session_id，
让猫娘能明确指定续跑同一会话或强制新开会话。
session_id 就是 Claude Code 原生会话 UUID（每个 Claude Code 会话唯一）：
- resume 模式支持裸 UUID、唯一前缀、'claude --resume <uuid>' 命令形态，
  并自动从 ~/.claude/projects 存档解析会话归属目录（Claude CLI 按 cwd
  索引会话，目录不对 --resume 必然失败），可随时回到任意历史会话。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)

from . import claude_sessions
from .errors import (
    ClassifiedError,
    is_retryable,
)
from .executor import (
    ClaudeCLIExecutor,
    build_cli_invocation,
    detect_claude_cli,
)
from .models import AdapterConfig, ExecuteResult
from .parser import ClaudeOutputParser
from .provider_manager import Provider, ProviderManager
from .session import SessionManager, compute_prompt_signature
from .task_manager import TaskManager


@neko_plugin
class ClaudeCodeAdapterPlugin(NekoPluginBase):
    """Claude Code 适配器插件。

    通过 @llm_tool 装饰器将 Claude Code CLI 的能力暴露给猫娘 LLM。
    猫娘可以在对话中调用这些工具，让 Claude Code 执行具体的编码任务。
    """

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        # 文件日志器（与 web_search 插件一致的模式）
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger

        # 运行时状态
        self._config: AdapterConfig = AdapterConfig()
        self._executor: Optional[ClaudeCLIExecutor] = None
        self._session_mgr: Optional[SessionManager] = None
        self._task_mgr: Optional[TaskManager] = None
        self._provider_mgr: Optional[ProviderManager] = None
        self._ready: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @lifecycle(id="startup")
    async def startup(self, **_) -> Any:
        """启动：加载配置、初始化执行器和会话管理器。"""
        try:
            # 1. 加载 plugin.toml 的 [claude_code] 节
            cfg_dict = await self._load_config_section("claude_code")
            self._config = AdapterConfig.from_config_dict(cfg_dict)

            # 2. 确保 PluginStore 已启用（基类在 __init__ 中已创建 self.store，
            #    但需要根据 plugin.toml 的 [plugin.store] enabled 决定是否可用）
            if not self.store.enabled:
                self.store.enabled = True
                self.logger.info("PluginStore auto-enabled for session persistence")

            # 3. 初始化会话管理器（使用基类的 self.store）
            self._session_mgr = SessionManager(self.store, logger=self.logger)
            await self._session_mgr.load()

            # 4. 初始化执行器
            self._executor = ClaudeCLIExecutor(self._config, logger=self.logger)

            # 5. 初始化供应商管理器（cc-switch 风格多供应商切换）
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            self._provider_mgr = ProviderManager(plugin_dir, logger=self.logger)
            self._provider_mgr.load()

            # 6. 初始化任务管理器（异步提交 + 轮询模式）
            self._task_mgr = TaskManager(
                self._executor,
                self._config,
                logger=self.logger,
                env_provider=self._active_provider_env,
            )
            await self._task_mgr.start()
            self.logger.info("TaskManager initialized and started")

            # 7. 检测 Claude CLI 是否可用
            cli_path = self._config.command or detect_claude_cli()
            cli_available = bool(cli_path)

            self._ready = True
            self.logger.info(
                "ClaudeCodeAdapter started: cli_available={} model={!r} cwd={!r} provider={!r}",
                cli_available,
                self._config.model,
                self._config.cwd or os.getcwd(),
                self._provider_mgr.get_active_name() or "(default)",
            )

            return Ok(
                {
                    "status": "ready",
                    "cli_available": cli_available,
                    "cli_path": cli_path or "",
                    "model": self._config.model,
                    "active_provider": self._provider_mgr.get_active_name(),
                    "sessions_loaded": len(self._session_mgr._sessions)
                    if self._session_mgr
                    else 0,
                }
            )
        except Exception as e:
            self.logger.exception("ClaudeCodeAdapter startup failed")
            return Err(SdkError(f"startup failed: {e}"))

    @lifecycle(id="shutdown")
    async def shutdown(self, **_) -> Any:
        """关闭：释放资源。"""
        self._ready = False
        if self._task_mgr:
            await self._task_mgr.stop()
            self.logger.info("TaskManager stopped")
        self.logger.info("ClaudeCodeAdapter shutdown")
        return Ok({"status": "shutdown"})

    # ------------------------------------------------------------------
    # 配置加载辅助
    # ------------------------------------------------------------------

    async def _load_config_section(self, section: str) -> dict[str, Any]:
        """从 plugin.toml 加载指定节。

        SDK 的 self.config.dump() 返回整个 plugin.toml 的字典。
        """
        try:
            cfg = await self.config.dump(timeout=5.0)
            if isinstance(cfg, dict):
                section_data = cfg.get(section)
                if isinstance(section_data, dict):
                    return section_data
        except Exception as e:
            self.logger.warning("Failed to load config section {}: {}", section, e)
        return {}

    def _ensure_ready(self) -> Optional[Any]:
        """检查插件是否就绪。返回 None 表示就绪，否则返回 Err。"""
        if not self._ready:
            return Err(
                SdkError(
                    "Claude Code Adapter not ready (startup not completed or failed)"
                )
            )
        if self._executor is None or self._session_mgr is None:
            return Err(SdkError("Claude Code Adapter internal state invalid"))
        return None

    def _active_provider_env(self) -> dict[str, str]:
        """返回激活 provider 的环境变量覆盖（供执行链路注入）。"""
        if self._provider_mgr is None:
            return {}
        try:
            return self._provider_mgr.env_overrides()
        except Exception:
            return {}

    async def _resolve_resume_id(
        self,
        *,
        session_mode: str,
        session_id: str,
        cwd: str,
        signature: str,
    ) -> tuple[str, str, str, str]:
        """根据会话模式解析要续跑的会话。

        Returns:
            (resume_id, resolved_cwd, session_title, error_message)。
            error_message 非空时调用方应直接返回。

        - auto: 自动查找可恢复会话（cwd + 提示包签名匹配）
        - new:  强制新开会话（resume_id = ""）
        - resume: 按 Claude Code 原生会话 UUID 强制续跑：
          * session_id 支持裸 UUID、``claude --resume <uuid>`` 命令形态、
            唯一 UUID 前缀（面板复制出的截断形态）
          * 自动从 ``~/.claude/projects`` 存档解析会话归属的项目目录。
            Claude CLI 按工作目录索引会话，cwd 不匹配时 --resume 会失败，
            因此调用方不传 cwd 时自动采用会话归属目录。
        """
        assert self._session_mgr is not None
        mode = (session_mode or "auto").strip().lower() or "auto"

        if mode not in ("auto", "new", "resume"):
            return (
                "",
                "",
                "",
                (
                    f"无效的 session_mode: {session_mode!r}，"
                    "只支持 'auto' / 'new' / 'resume'。"
                ),
            )
        if mode == "new":
            return "", "", "", ""
        if mode == "resume":
            ref = claude_sessions.normalize_session_ref(session_id)
            if not ref:
                return (
                    "",
                    "",
                    "",
                    (
                        "session_mode='resume' 需要同时提供 session_id（Claude Code 会话 UUID）。"
                        "可用 claude_code_list_sessions(include_history=true) 或插件面板查看可用会话。"
                    ),
                )
            try:
                meta = claude_sessions.find_session_by_id(ref)
            except Exception as e:
                self.logger.warning("find_session_by_id failed: {}", e)
                meta = None
            if meta is None:
                return (
                    "",
                    "",
                    "",
                    (
                        f"未找到 session_id={session_id!r} 对应的 Claude Code 会话。"
                        "请检查 UUID 是否正确（可用 claude_code_list_sessions(include_history=true) "
                        "或插件面板查看历史会话 UUID）。"
                    ),
                )
            full_id = str(meta.get("session_id") or ref)
            project_dir = str(meta.get("project_dir") or "")
            title = str(meta.get("title") or "")
            explicit_cwd = (cwd or "").strip()
            if explicit_cwd and project_dir:
                # Claude CLI 的 --resume 只在当前目录的会话索引里查找，
                # cwd 与会话归属目录不一致时必然失败 — 提前报清晰错误
                if os.path.normcase(os.path.normpath(explicit_cwd)) != os.path.normcase(
                    os.path.normpath(project_dir)
                ):
                    return (
                        "",
                        "",
                        "",
                        (
                            f"会话 {full_id} 归属目录为 {project_dir!r}，"
                            f"与传入的 cwd={explicit_cwd!r} 不一致。"
                            "请去掉 cwd 参数（自动采用会话归属目录），或传入正确的目录。"
                        ),
                    )
            return full_id, project_dir, title, ""
        # auto
        lookup_cwd = (cwd or "").strip() or self._config.cwd or os.getcwd()
        record = await self._session_mgr.find_resumable(lookup_cwd, signature)
        return (record.session_id if record else ""), "", "", ""

    # ------------------------------------------------------------------
    # 核心执行逻辑（内部方法，被 @llm_tool 方法调用）
    # ------------------------------------------------------------------

    async def _execute_with_retry(
        self,
        prompt: str,
        *,
        cwd: str = "",
        model: str = "",
        effort: str = "",
        max_turns: int = 0,
        session_mode: str = "auto",
        session_id: str = "",
    ) -> ExecuteResult:
        """执行 Claude Code 任务，支持自动重试。

        重试策略：
        - auto 模式：首次尝试自动恢复会话（如果存在可恢复的会话）
        - resume 模式：强制续跑指定 session_id，重试时仍续跑
        - new 模式：始终新开会话
        - 如果失败且错误可重试，auto/new 模式新建会话重试
        - 最多重试 self._config.max_retries 次
        """
        assert self._executor is not None and self._session_mgr is not None

        signature = compute_prompt_signature(
            system_prompt_file=self._config.append_system_prompt_file,
            skills_dir=self._config.skills_dir,
        )

        # 根据会话模式解析续跑会话（resume 模式返回会话归属目录）
        requested_cwd = (cwd or "").strip()
        (
            resume_id,
            resume_cwd,
            resume_title,
            resolve_err,
        ) = await self._resolve_resume_id(
            session_mode=session_mode,
            session_id=session_id,
            cwd=requested_cwd,
            signature=signature,
        )
        if resolve_err:
            return ExecuteResult(
                error_kind="invalid_argument",
                error_message=resolve_err,
            )
        # 工作目录优先级：调用方显式指定 > 会话归属目录(resume) > 配置默认 > 进程 cwd
        effective_cwd = requested_cwd or resume_cwd or self._config.cwd or os.getcwd()
        mode = (session_mode or "auto").strip().lower() or "auto"

        max_attempts = 1 + max(0, self._config.max_retries)
        last_error: Optional[ClassifiedError] = None

        for attempt in range(max_attempts):
            is_retry = attempt > 0
            if is_retry and mode != "resume":
                # auto/new 模式重试时新建会话；resume 模式保持续跑
                resume_id = ""
                self.logger.info(
                    "Retrying with new session (attempt {}/{}): prev_error={}",
                    attempt + 1,
                    max_attempts,
                    last_error.kind if last_error else "unknown",
                )

            # 构建 CLI 调用（注入激活 provider 的环境变量）
            invocation, build_err = build_cli_invocation(
                self._config,
                prompt=prompt,
                resume_session_id=resume_id,
                cwd=effective_cwd,
                model=model,
                effort=effort,
                max_turns=max_turns,
                extra_env=self._active_provider_env(),
            )
            if build_err is not None:
                last_error = build_err
                # CLI 未找到 — 不可重试
                if not is_retryable(build_err.kind):
                    return ExecuteResult(
                        error_kind=build_err.kind,
                        error_message=build_err.message,
                    )
                continue

            # 执行
            parser = ClaudeOutputParser()
            stream, exec_err = await self._executor.execute(invocation, parser)

            # 成功
            if exec_err is None:
                session_id = stream.session_id
                is_new_session = not bool(resume_id)

                # 更新会话记录
                if session_id:
                    try:
                        await self._session_mgr.upsert(
                            session_id, effective_cwd, signature
                        )
                        await self._session_mgr.touch(
                            session_id,
                            turn_count=stream.result.num_turns if stream.result else 0,
                        )
                    except Exception as e:
                        self.logger.warning("Failed to update session record: {}", e)

                # 构造结果
                result = ExecuteResult(
                    session_id=session_id,
                    session_title=resume_title if resume_id else "",
                    is_new_session=is_new_session,
                    messages=stream.messages,
                    final_text=stream.final_text,
                    total_cost_usd=stream.result.total_cost_usd
                    if stream.result
                    else 0.0,
                    duration_ms=stream.result.duration_ms if stream.result else 0,
                    num_turns=stream.result.num_turns if stream.result else 0,
                    raw_result=stream.result.raw if stream.result else {},
                )
                return result

            # 失败 — 记录错误并判断是否重试
            last_error = exec_err

            # 标记会话错误
            if resume_id:
                try:
                    await self._session_mgr.mark_error(resume_id, exec_err.kind)
                except Exception:
                    pass

            # 不可重试 — 立即返回
            if not is_retryable(exec_err.kind):
                return ExecuteResult(
                    session_id=resume_id,
                    error_kind=exec_err.kind,
                    error_message=exec_err.message,
                    final_text=stream.final_text,
                    duration_ms=stream.result.duration_ms if stream.result else 0,
                )

            # 可重试 — 继续下一轮
            continue

        # 所有重试都失败
        return ExecuteResult(
            error_kind=last_error.kind if last_error else "unknown",
            error_message=last_error.message if last_error else "all retries exhausted",
        )

    # ==================================================================
    # LLM 工具集（@llm_tool 装饰器注册）
    # ==================================================================

    @llm_tool(
        name="claude_code_execute",
        description=(
            "调用 Claude Code CLI 执行编码任务。Claude Code 是一个强大的 AI 编码助手，"
            "可以读写文件、运行命令、调试代码、写测试、查文档等。\n\n"
            "适用场景：\n"
            "- 写新功能、新文件\n"
            "- 修改现有代码、修 bug\n"
            "- 运行测试、构建项目\n"
            "- 代码审查、重构\n"
            "- 查阅项目文档、理解代码结构\n\n"
            "参数说明：\n"
            "- prompt: 详细描述要让 Claude Code 做什么。要具体、清晰，包含必要的上下文。\n"
            "- cwd: 工作目录（项目根目录的绝对路径）。同一目录的调用会自动复用会话上下文。\n"
            "- model: 模型 ID（可选）。留空使用适配器默认配置。\n"
            "- effort: 推理努力级别（可选）：'low' / 'medium' / 'high'。留空使用默认配置。\n"
            "- max_turns: 最大轮次（可选，0=使用默认值）。\n"
            "- session_mode: 会话控制（可选）：'auto'（默认，自动复用同目录会话）/ "
            "'new'（强制新开会话）/ 'resume'（强制续跑 session_id 指定的会话）。\n"
            "- session_id: 仅 session_mode='resume' 时需要。它就是 Claude Code 原生会话 UUID，"
            "可从本工具/submit 返回的 session_id 字段、claude_code_list_sessions(include_history=true) "
            "或插件面板获取；支持任意历史会话，插件会自动定位会话归属目录，无需同时传 cwd。\n\n"
            "返回：包含 Claude Code 的最终回复文本、会话 ID（session_id，即 Claude Code 原生 UUID，"
            "后续维持同一会话直接传它）、费用、轮次等信息的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "要交给 Claude Code 执行的任务描述。要具体、清晰，包含必要的项目上下文。",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录（项目根目录的绝对路径）。同一目录的调用会自动复用会话上下文。留空使用适配器默认配置。",
                },
                "model": {
                    "type": "string",
                    "description": "模型 ID（可选）。例如 'sonnet' / 'opus'。留空使用默认配置。",
                },
                "effort": {
                    "type": "string",
                    "description": "推理努力级别（可选）：'low' / 'medium' / 'high'。留空使用默认配置。",
                    "enum": ["", "low", "medium", "high"],
                },
                "max_turns": {
                    "type": "integer",
                    "description": "最大轮次（可选，0=使用默认值）。",
                },
                "session_mode": {
                    "type": "string",
                    "description": (
                        "会话控制：'auto'（默认，自动复用）/ 'new'（强制新会话）/ "
                        "'resume'（续跑 session_id 指定的会话，支持任意历史会话）。"
                    ),
                    "enum": ["", "auto", "new", "resume"],
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "仅 session_mode='resume' 时需要：Claude Code 原生会话 UUID。"
                        "取自之前执行返回的 session_id、claude_code_list_sessions(include_history=true) "
                        "或插件面板；支持完整 UUID、唯一前缀或 'claude --resume <uuid>' 命令形态。"
                    ),
                },
            },
            "required": ["prompt"],
        },
        timeout=300.0,
    )
    async def claude_code_execute(
        self,
        prompt: str = "",
        cwd: str = "",
        model: str = "",
        effort: str = "",
        max_turns: int = 0,
        session_mode: str = "auto",
        session_id: str = "",
        **_,
    ) -> dict[str, Any]:
        """执行 Claude Code 任务。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        if not prompt or not prompt.strip():
            return Err(SdkError("prompt 不能为空"))

        try:
            result = await self._execute_with_retry(
                prompt,
                cwd=cwd,
                model=model,
                effort=effort,
                max_turns=max_turns,
                session_mode=session_mode,
                session_id=session_id,
            )
            return Ok(result.to_llm_payload())
        except Exception as e:
            self.logger.exception("claude_code_execute failed")
            return Err(SdkError(f"执行失败: {e}"))

    @llm_tool(
        name="claude_code_submit",
        description=(
            "提交 Claude Code 编码任务到后台异步执行。适用于耗时较长的任务，"
            "避免 main_server 的 300 秒超时限制。\n\n"
            "使用流程（推荐）：\n"
            "1. 调用 claude_code_submit 提交任务，立即返回 task_id\n"
            "2. 根据任务复杂度预估耗时，调用 claude_code_wait 智能等待\n"
            "   （任务一完成立即返回；超时则返回进度和建议的下次等待时长）\n"
            "3. 也可用 claude_code_poll 单次查询；如需取消用 claude_code_cancel\n\n"
            "参数说明：\n"
            "- prompt: 详细描述要让 Claude Code 做什么。要具体、清晰，包含必要的上下文。\n"
            "- cwd: 工作目录（项目根目录的绝对路径）。\n"
            "- model: 模型 ID（可选）。留空使用适配器默认配置。\n"
            "- effort: 推理努力级别（可选）：'low' / 'medium' / 'high'。\n"
            "- max_turns: 最大轮次（可选，0=使用默认值）。\n"
            "- session_mode: 'auto'（默认）/ 'new'（强制新会话）/ 'resume'（续跑指定会话）。\n"
            "- session_id: 仅 session_mode='resume' 时需要。\n\n"
            "返回：包含 task_id 和 status 的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "要交给 Claude Code 执行的任务描述。要具体、清晰，包含必要的项目上下文。",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录（项目根目录的绝对路径）。留空使用适配器默认配置。",
                },
                "model": {
                    "type": "string",
                    "description": "模型 ID（可选）。例如 'sonnet' / 'opus'。留空使用默认配置。",
                },
                "effort": {
                    "type": "string",
                    "description": "推理努力级别（可选）：'low' / 'medium' / 'high'。留空使用默认配置。",
                    "enum": ["", "low", "medium", "high"],
                },
                "max_turns": {
                    "type": "integer",
                    "description": "最大轮次（可选，0=使用默认值）。",
                },
                "session_mode": {
                    "type": "string",
                    "description": (
                        "会话控制：'auto'（默认，自动复用）/ 'new'（强制新会话）/ "
                        "'resume'（续跑 session_id 指定的会话，支持任意历史会话）。"
                    ),
                    "enum": ["", "auto", "new", "resume"],
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "仅 session_mode='resume' 时需要：Claude Code 原生会话 UUID。"
                        "取自之前执行返回的 session_id、claude_code_list_sessions(include_history=true) "
                        "或插件面板；支持完整 UUID、唯一前缀或 'claude --resume <uuid>' 命令形态。"
                    ),
                },
            },
            "required": ["prompt"],
        },
        timeout=10.0,
    )
    async def claude_code_submit(
        self,
        prompt: str = "",
        cwd: str = "",
        model: str = "",
        effort: str = "",
        max_turns: int = 0,
        session_mode: str = "auto",
        session_id: str = "",
        **_,
    ) -> dict[str, Any]:
        """提交 Claude Code 任务到后台异步执行。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        if not prompt or not prompt.strip():
            return Err(SdkError("prompt 不能为空"))

        if self._task_mgr is None:
            return Err(SdkError("TaskManager not initialized"))

        try:
            assert self._session_mgr is not None
            # 提交时一次性解析续跑会话（TaskManager 不依赖 SessionManager）
            requested_cwd = (cwd or "").strip()
            signature = compute_prompt_signature(
                system_prompt_file=self._config.append_system_prompt_file,
                skills_dir=self._config.skills_dir,
            )
            (
                resume_id,
                resume_cwd,
                resume_title,
                resolve_err,
            ) = await self._resolve_resume_id(
                session_mode=session_mode,
                session_id=session_id,
                cwd=requested_cwd,
                signature=signature,
            )
            if resolve_err:
                return Err(SdkError(resolve_err))
            # 工作目录优先级：调用方显式指定 > 会话归属目录(resume) > 配置默认 > 进程 cwd
            effective_cwd = (
                requested_cwd or resume_cwd or self._config.cwd or os.getcwd()
            )

            record = await self._task_mgr.submit(
                prompt=prompt,
                cwd=effective_cwd,
                model=model,
                effort=effort,
                max_turns=max_turns,
                resume_session_id=resume_id,
            )
            payload: dict[str, Any] = {
                "task_id": record.task_id,
                "status": record.status.value,
                "resumed_session": resume_id,
                "message": "任务已提交。建议根据预估耗时调用 claude_code_wait 等待结果，"
                "或用 claude_code_poll 单次查询。",
            }
            if resume_title:
                payload["resumed_session_title"] = resume_title
            return Ok(payload)
        except Exception as e:
            self.logger.exception("claude_code_submit failed")
            return Err(SdkError(f"提交任务失败: {e}"))

    @llm_tool(
        name="claude_code_poll",
        description=(
            "查询 Claude Code 异步任务的执行状态和结果。\n\n"
            "使用流程：\n"
            "1. 调用 claude_code_submit 提交任务，获取 task_id\n"
            "2. 调用 claude_code_poll 查询任务状态\n"
            "3. 当 status 为 'done' 时，结果在 result 字段中\n"
            "4. 当 status 为 'error' 时，错误信息在 error 字段中\n\n"
            "参数说明：\n"
            "- task_id: 任务 ID（从 claude_code_submit 返回）\n\n"
            "返回：包含 task_id、status、result/error 的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "任务 ID（从 claude_code_submit 返回）。",
                },
            },
            "required": ["task_id"],
        },
        timeout=10.0,
    )
    async def claude_code_poll(
        self,
        task_id: str = "",
        **_,
    ) -> dict[str, Any]:
        """查询 Claude Code 任务状态。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        if not task_id or not task_id.strip():
            return Err(SdkError("task_id 不能为空"))

        if self._task_mgr is None:
            return Err(SdkError("TaskManager not initialized"))

        try:
            result = await self._task_mgr.poll(task_id)
            if "error" in result and result["error"].startswith("Task not found"):
                return Err(SdkError(result["error"]))
            return Ok(result)
        except Exception as e:
            self.logger.exception("claude_code_poll failed")
            return Err(SdkError(f"查询任务失败: {e}"))

    @llm_tool(
        name="claude_code_wait",
        description=(
            "智能等待 Claude Code 异步任务完成。在插件内部自动轮询，"
            "任务一完成立即返回结果，比反复手动调用 claude_code_poll 更高效。\n\n"
            "使用策略（重要）：\n"
            "- 提交任务后，先根据任务复杂度预估耗时：\n"
            "  简单任务（单文件小改）≈30-60s；中等任务（多文件修改/写测试）≈120s；\n"
            "  复杂任务（重构/多步骤工程）≈240s 以上\n"
            "- 按预估耗时设置 max_wait_sec；若超时返回 still_running，\n"
            "  按返回的 suggested_next_wait_sec 再次调用本工具即可\n\n"
            "参数说明：\n"
            "- task_id: 任务 ID（从 claude_code_submit 返回）\n"
            "- max_wait_sec: 本次最多等待的秒数（5-290，默认 60）\n"
            "- poll_interval: 内部轮询初始间隔秒数（可选，自适应退避，无需关心）\n\n"
            "返回：任务完成时返回完整结果；仍在运行时返回进度、"
            "still_running=true 和 suggested_next_wait_sec。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "任务 ID（从 claude_code_submit 返回）。",
                },
                "max_wait_sec": {
                    "type": "number",
                    "description": "本次最多等待的秒数（5-290）。根据预估任务耗时设置。",
                },
                "poll_interval": {
                    "type": "number",
                    "description": "内部轮询初始间隔秒数（可选，1-15，默认自适应）。",
                },
            },
            "required": ["task_id"],
        },
        timeout=295.0,
    )
    async def claude_code_wait(
        self,
        task_id: str = "",
        max_wait_sec: float = 60.0,
        poll_interval: float = 0.0,
        **_,
    ) -> dict[str, Any]:
        """智能等待任务完成。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        if not task_id or not task_id.strip():
            return Err(SdkError("task_id 不能为空"))

        if self._task_mgr is None:
            return Err(SdkError("TaskManager not initialized"))

        try:
            # 夹在安全区间：不超过 main_server 单次调用上限
            wait_sec = max(5.0, min(290.0, float(max_wait_sec or 60.0)))
            result = await self._task_mgr.wait_for(
                task_id, max_wait_sec=wait_sec, interval_sec=float(poll_interval or 0)
            )
            if "error" in result and result["error"].startswith("Task not found"):
                return Err(SdkError(result["error"]))
            return Ok(result)
        except Exception as e:
            self.logger.exception("claude_code_wait failed")
            return Err(SdkError(f"等待任务失败: {e}"))

    @llm_tool(
        name="claude_code_cancel",
        description=(
            "取消正在运行的 Claude Code 异步任务。\n\n"
            "适用场景：\n"
            "- 任务执行时间过长，想提前终止\n"
            "- 提交了错误的任务，需要取消\n"
            "- 需要释放资源\n\n"
            "参数说明：\n"
            "- task_id: 任务 ID（从 claude_code_submit 返回）\n\n"
            "返回：包含 task_id 和 status 的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "任务 ID（从 claude_code_submit 返回）。",
                },
            },
            "required": ["task_id"],
        },
        timeout=10.0,
    )
    async def claude_code_cancel(
        self,
        task_id: str = "",
        **_,
    ) -> dict[str, Any]:
        """取消 Claude Code 任务。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        if not task_id or not task_id.strip():
            return Err(SdkError("task_id 不能为空"))

        if self._task_mgr is None:
            return Err(SdkError("TaskManager not initialized"))

        try:
            result = await self._task_mgr.cancel(task_id)
            if "error" in result:
                return Err(SdkError(result["error"]))
            return Ok(result)
        except Exception as e:
            self.logger.exception("claude_code_cancel failed")
            return Err(SdkError(f"取消任务失败: {e}"))

    @llm_tool(
        name="claude_code_check_health",
        description=(
            "检查 Claude Code CLI 是否可用。返回 CLI 路径、版本信息和适配器状态。\n\n"
            "适用场景：\n"
            "- 在调用 claude_code_execute 之前确认环境就绪\n"
            "- 诊断 Claude Code 相关问题\n"
            "- 检查适配器配置是否正确\n\n"
            "返回：包含 cli_available、cli_path、config 等信息的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        timeout=10.0,
    )
    async def claude_code_check_health(self, **_) -> dict[str, Any]:
        """检查 Claude CLI 健康状态。"""
        try:
            cli_path = self._config.command or detect_claude_cli()
            cli_available = bool(cli_path)

            # 尝试获取版本（执行 claude --version）
            version = ""
            if cli_available:
                try:
                    import asyncio as _asyncio

                    proc = await _asyncio.create_subprocess_exec(
                        cli_path,
                        "--version",
                        stdout=_asyncio.subprocess.PIPE,
                        stderr=_asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, _ = await _asyncio.wait_for(
                            proc.communicate(), timeout=5.0
                        )
                        version = stdout.decode("utf-8", errors="replace").strip()
                    except _asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                except Exception as e:
                    version = f"(version check failed: {e})"

            return Ok(
                {
                    "cli_available": cli_available,
                    "cli_path": cli_path or "",
                    "version": version,
                    "ready": self._ready,
                    "config": self._config.to_dict(),
                    "sessions_count": len(self._session_mgr._sessions)
                    if self._session_mgr
                    else 0,
                }
            )
        except Exception as e:
            self.logger.exception("claude_code_check_health failed")
            return Err(SdkError(f"健康检查失败: {e}"))

    @llm_tool(
        name="claude_code_list_sessions",
        description=(
            "列出 Claude Code 会话记录。默认列出插件维护的活跃会话索引；"
            "include_history=true 时额外扫描 ~/.claude/projects 原生存档，"
            "列出所有历史会话（含 session_id/标题/归属目录/最近活跃时间）。\n\n"
            "适用场景：\n"
            "- 查看当前有哪些活跃的 Claude Code 会话\n"
            "- include_history=true：找回历史会话的 UUID，配合 "
            "claude_code_execute(session_mode='resume', session_id=<UUID>) 回到该会话的上下文\n\n"
            "返回：包含 sessions 列表的字典，每个会话含 session_id（Claude Code 原生 UUID）、"
            "cwd、turn_count 等字段；历史会话额外含 title、resume_command。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "include_history": {
                    "type": "boolean",
                    "description": (
                        "是否同时列出 ~/.claude/projects 中的所有历史会话（默认 false）。"
                        "需要找回旧会话 UUID 续跑时设为 true。"
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": "可选：只列出归属该工作目录的会话。",
                },
                "limit": {
                    "type": "integer",
                    "description": "历史会话最多返回条数（可选，默认 50，仅 include_history=true 时生效）。",
                },
            },
        },
        timeout=30.0,
    )
    async def claude_code_list_sessions(
        self,
        include_history: bool = False,
        cwd: str = "",
        limit: int = 50,
        **_,
    ) -> dict[str, Any]:
        """列出会话（可选含历史会话）。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        try:
            assert self._session_mgr is not None
            cwd_filter = os.path.normcase(os.path.normpath(cwd)) if cwd.strip() else ""

            records = await self._session_mgr.list_all()
            sessions: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for r in records:
                if (
                    cwd_filter
                    and os.path.normcase(os.path.normpath(r.cwd)) != cwd_filter
                ):
                    continue
                item = r.to_dict()
                item["source"] = "active"
                sessions.append(item)
                if r.session_id:
                    seen_ids.add(r.session_id.lower())

            history_count = 0
            if include_history:
                max_history = max(1, int(limit or 50))
                for meta in claude_sessions.scan_sessions(limit=0):
                    sid = str(meta.get("session_id") or "")
                    project_dir = str(meta.get("project_dir") or "")
                    if cwd_filter:
                        if (
                            not project_dir
                            or os.path.normcase(os.path.normpath(project_dir))
                            != cwd_filter
                        ):
                            continue
                    if sid.lower() in seen_ids:
                        continue
                    seen_ids.add(sid.lower())
                    sessions.append(
                        {
                            "source": "history",
                            "session_id": sid,
                            "title": meta.get("title", ""),
                            "cwd": project_dir,
                            "created_at": meta.get("created_at"),
                            "last_used_at": meta.get("last_active_at"),
                            "resume_command": meta.get("resume_command", ""),
                        }
                    )
                    history_count += 1
                    if history_count >= max_history:
                        break

            return Ok(
                {
                    "count": len(sessions),
                    "history_count": history_count,
                    "sessions": sessions,
                }
            )
        except Exception as e:
            self.logger.exception("claude_code_list_sessions failed")
            return Err(SdkError(f"列出会话失败: {e}"))

    @llm_tool(
        name="claude_code_clear_session",
        description=(
            "清除 Claude Code 会话记录。\n\n"
            "适用场景：\n"
            "- 会话上下文混乱，想从干净状态开始\n"
            "- 切换到不同的项目分支后清理旧上下文\n"
            "- 会话报错后强制重置\n\n"
            "参数：\n"
            "- cwd: 要清除的工作目录（绝对路径）。留空则清除所有会话。\n\n"
            "返回：包含 cleared_count 的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "cwd": {
                    "type": "string",
                    "description": "要清除的工作目录（绝对路径）。留空则清除所有会话。",
                },
            },
        },
        timeout=10.0,
    )
    async def claude_code_clear_session(self, cwd: str = "", **_) -> dict[str, Any]:
        """清除会话记录。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        try:
            assert self._session_mgr is not None
            target = cwd or None
            count = await self._session_mgr.clear(target)
            self.logger.info("Cleared {} session(s) (cwd={!r})", count, target)
            return Ok(
                {
                    "cleared_count": count,
                    "cwd": target or "(all)",
                }
            )
        except Exception as e:
            self.logger.exception("claude_code_clear_session failed")
            return Err(SdkError(f"清除会话失败: {e}"))

    @llm_tool(
        name="claude_code_get_config",
        description=(
            "获取 Claude Code 适配器的当前配置。\n\n"
            "适用场景：\n"
            "- 了解默认模型、超时、工作目录等设置\n"
            "- 诊断配置问题\n"
            "- 在调用 claude_code_execute 前确认参数默认值\n\n"
            "返回：包含完整适配器配置的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        timeout=5.0,
    )
    async def claude_code_get_config(self, **_) -> dict[str, Any]:
        """获取适配器配置。"""
        try:
            return Ok(
                {
                    "config": self._config.to_dict(),
                    "ready": self._ready,
                    "default_cwd": self._config.cwd or os.getcwd(),
                    "active_provider": self._provider_mgr.get_active_name()
                    if self._provider_mgr
                    else "",
                }
            )
        except Exception as e:
            return Err(SdkError(f"获取配置失败: {e}"))

    @llm_tool(
        name="claude_code_list_providers",
        description=(
            "列出所有已注册的 Claude Code 供应商（API 提供方），并标出当前激活项。\n\n"
            "供应商决定 Claude Code 走哪个 API 端点（官方 Anthropic / 第三方中转 / "
            "OpenRouter 等），切换后对后续所有执行生效。\n\n"
            "返回：包含 providers 列表（token 已打码）和 active_provider 的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        timeout=5.0,
    )
    async def claude_code_list_providers(self, **_) -> dict[str, Any]:
        """列出所有供应商。"""
        try:
            assert self._provider_mgr is not None
            return Ok(
                {
                    "active_provider": self._provider_mgr.get_active_name(),
                    "providers": self._provider_mgr.list_providers(),
                }
            )
        except Exception as e:
            self.logger.exception("claude_code_list_providers failed")
            return Err(SdkError(f"列出供应商失败: {e}"))

    @llm_tool(
        name="claude_code_switch_provider",
        description=(
            "切换 Claude Code 的 API 供应商（cc-switch 风格）。切换后对后续所有\n"
            "claude_code_execute / claude_code_submit 调用生效，不影响已在运行的任务。\n\n"
            "参数说明：\n"
            "- name: 供应商名称（从 claude_code_list_providers 获取）。\n"
            "  传空字符串表示清除激活状态，回退到用户全局 ~/.claude 配置。\n"
            "- base_url: （可选）新供应商的 API 端点。传入后会先注册/更新该供应商再激活。\n"
            "- auth_token: （可选）新供应商的认证 token。\n"
            "- display_name: （可选）展示名称。\n\n"
            "返回：包含 active_provider 和生效环境变量的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "供应商名称（小写标识）。传空字符串清除激活状态。",
                },
                "base_url": {
                    "type": "string",
                    "description": "API 端点（如 https://api.anthropic.com）。传入后会先注册/更新再激活。",
                },
                "auth_token": {
                    "type": "string",
                    "description": "认证 token（ANTHROPIC_AUTH_TOKEN）。可选。",
                },
                "display_name": {
                    "type": "string",
                    "description": "展示名称。可选。",
                },
            },
            "required": ["name"],
        },
        timeout=5.0,
    )
    async def claude_code_switch_provider(
        self,
        name: str = "",
        base_url: str = "",
        auth_token: str = "",
        display_name: str = "",
        **_,
    ) -> dict[str, Any]:
        """切换/注册供应商。"""
        if self._provider_mgr is None:
            return Err(SdkError("ProviderManager not initialized"))

        try:
            key = name.strip().lower()

            # 携带新配置 → 先注册/更新
            if key and (base_url or auth_token):
                env: dict[str, str] = {}
                if base_url.strip():
                    env["ANTHROPIC_BASE_URL"] = base_url.strip()
                if auth_token.strip():
                    env["ANTHROPIC_AUTH_TOKEN"] = auth_token.strip()
                self._provider_mgr.add_or_update(
                    Provider(name=key, display_name=display_name, env=env)
                )

            provider = self._provider_mgr.set_active(key)
            return Ok(
                {
                    "active_provider": self._provider_mgr.get_active_name(),
                    "display_name": provider.display_name or provider.name,
                    "env": {
                        k: (v[:4] + "****" if "TOKEN" in k or "KEY" in k else v)
                        for k, v in self._provider_mgr.env_overrides().items()
                    },
                    "message": "供应商已切换，对后续执行生效"
                    if key
                    else "已回退到用户全局配置",
                }
            )
        except KeyError:
            return Err(
                SdkError(
                    f"供应商不存在: {name}。请先用 claude_code_list_providers 查看，"
                    "或传入 base_url/auth_token 注册新供应商。"
                )
            )
        except Exception as e:
            self.logger.exception("claude_code_switch_provider failed")
            return Err(SdkError(f"切换供应商失败: {e}"))

    @llm_tool(
        name="claude_code_remove_provider",
        description=(
            "删除已注册的 Claude Code 供应商。\n\n"
            "参数说明：\n"
            "- name: 要删除的供应商名称。\n\n"
            "返回：包含 deleted 布尔值的字典。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要删除的供应商名称。",
                },
            },
            "required": ["name"],
        },
        timeout=5.0,
    )
    async def claude_code_remove_provider(self, name: str = "", **_) -> dict[str, Any]:
        """删除供应商。"""
        if self._provider_mgr is None:
            return Err(SdkError("ProviderManager not initialized"))

        if not name or not name.strip():
            return Err(SdkError("name 不能为空"))

        try:
            deleted = self._provider_mgr.remove(name)
            if not deleted:
                return Err(SdkError(f"供应商不存在: {name}"))
            return Ok(
                {
                    "deleted": True,
                    "name": name.strip().lower(),
                    "active_provider": self._provider_mgr.get_active_name(),
                }
            )
        except Exception as e:
            self.logger.exception("claude_code_remove_provider failed")
            return Err(SdkError(f"删除供应商失败: {e}"))

    # ==================================================================
    # 插件入口（供 UI / 其他插件调用，非 LLM 工具）
    # ==================================================================

    @plugin_entry(
        id="execute",
        name="执行 Claude Code 任务",
        description="执行 Claude Code 任务（与 claude_code_execute LLM 工具相同的功能，供 UI/其他插件调用）。",
        llm_result_fields=["output", "session_id", "num_turns"],
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "任务描述"},
                "cwd": {"type": "string", "description": "工作目录"},
                "model": {"type": "string", "description": "模型 ID"},
                "effort": {"type": "string", "description": "推理努力级别"},
                "max_turns": {"type": "integer", "description": "最大轮次"},
                "session_mode": {
                    "type": "string",
                    "description": "会话控制：auto / new / resume",
                },
                "session_id": {
                    "type": "string",
                    "description": "session_mode=resume 时要续跑的会话 ID",
                },
            },
            "required": ["prompt"],
        },
    )
    async def execute_entry(
        self,
        prompt: str = "",
        cwd: str = "",
        model: str = "",
        effort: str = "",
        max_turns: int = 0,
        session_mode: str = "auto",
        session_id: str = "",
        **_,
    ) -> Any:
        """插件入口（与 LLM 工具功能相同，供 UI/其他插件调用）。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        if not prompt or not prompt.strip():
            return Err(SdkError("prompt 不能为空"))

        try:
            result = await self._execute_with_retry(
                prompt,
                cwd=cwd,
                model=model,
                effort=effort,
                max_turns=max_turns,
                session_mode=session_mode,
                session_id=session_id,
            )
            return Ok(result.to_llm_payload())
        except Exception as e:
            self.logger.exception("execute_entry failed")
            return Err(SdkError(f"执行失败: {e}"))

    @plugin_entry(
        id="submit_task",
        name="提交 Claude Code 异步任务",
        description="提交 Claude Code 任务到后台异步执行（供 UI/其他插件调用）。",
        llm_result_fields=["task_id", "status"],
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "任务描述"},
                "cwd": {"type": "string", "description": "工作目录"},
                "model": {"type": "string", "description": "模型 ID"},
                "effort": {"type": "string", "description": "推理努力级别"},
                "max_turns": {"type": "integer", "description": "最大轮次"},
            },
            "required": ["prompt"],
        },
    )
    async def submit_task_entry(
        self,
        prompt: str = "",
        cwd: str = "",
        model: str = "",
        effort: str = "",
        max_turns: int = 0,
        **_,
    ) -> Any:
        """插件入口（提交异步任务，供 UI/其他插件调用）。"""
        not_ready = self._ensure_ready()
        if not_ready is not None:
            return not_ready

        if not prompt or not prompt.strip():
            return Err(SdkError("prompt 不能为空"))

        if self._task_mgr is None:
            return Err(SdkError("TaskManager not initialized"))

        try:
            record = await self._task_mgr.submit(
                prompt=prompt,
                cwd=cwd,
                model=model,
                effort=effort,
                max_turns=max_turns,
            )
            return Ok(
                {
                    "task_id": record.task_id,
                    "status": record.status.value,
                }
            )
        except Exception as e:
            self.logger.exception("submit_task_entry failed")
            return Err(SdkError(f"提交任务失败: {e}"))

    # ------------------------------------------------------------------
    # Claude 原生会话管理（供前端面板调用，照搬 cc-switch 会话管理器能力）
    # ------------------------------------------------------------------

    @plugin_entry(
        id="list_claude_sessions",
        name="列出 Claude 会话",
        description="扫描 ~/.claude/projects 下的所有 Claude Code 会话记录（标题/时间/项目目录），供前端面板展示。",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "最多返回的会话数（默认 200）",
                },
            },
        },
    )
    async def list_claude_sessions_entry(self, limit: int = 200, **_) -> Any:
        """扫描 Claude 原生会话存档。"""
        try:
            sessions = claude_sessions.scan_sessions(limit=int(limit or 200))
            return Ok({"count": len(sessions), "sessions": sessions})
        except Exception as e:
            self.logger.exception("list_claude_sessions_entry failed")
            return Err(SdkError(f"扫描会话失败: {e}"))

    @plugin_entry(
        id="get_claude_session",
        name="读取会话消息",
        description="读取指定 Claude 会话的完整消息列表（用户/AI/工具消息），供前端面板展示对话内容。",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "会话 ID",
                },
            },
            "required": ["session_id"],
        },
    )
    async def get_claude_session_entry(self, session_id: str = "", **_) -> Any:
        """读取会话消息。"""
        sid = (session_id or "").strip()
        if not sid:
            return Err(SdkError("session_id 不能为空"))
        try:
            # 定位会话文件（支持完整 UUID / 唯一前缀 / 命令形态）
            target = claude_sessions.find_session_by_id(sid)
            if target is None:
                return Err(SdkError(f"会话不存在: {sid}"))
            full_id = str(target.get("session_id") or sid)
            messages = claude_sessions.load_messages(
                target["source_path"], expected_session_id=full_id
            )
            return Ok(
                {
                    "session_id": full_id,
                    "title": target["title"],
                    "project_dir": target["project_dir"],
                    "created_at": target["created_at"],
                    "last_active_at": target["last_active_at"],
                    "resume_command": target.get("resume_command", ""),
                    "message_count": len(messages),
                    "messages": messages,
                }
            )
        except Exception as e:
            self.logger.exception("get_claude_session_entry failed")
            return Err(SdkError(f"读取会话失败: {e}"))

    @plugin_entry(
        id="delete_claude_session",
        name="删除 Claude 会话",
        description="删除指定的 Claude 会话文件及同名 sidecar 目录（与 cc-switch 删除行为一致）。",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "要删除的会话 ID",
                },
            },
            "required": ["session_id"],
        },
    )
    async def delete_claude_session_entry(self, session_id: str = "", **_) -> Any:
        """删除会话。"""
        sid = (session_id or "").strip()
        if not sid:
            return Err(SdkError("session_id 不能为空"))
        try:
            target = claude_sessions.find_session_by_id(sid)
            if target is None:
                return Err(SdkError(f"会话不存在: {sid}"))
            full_id = str(target.get("session_id") or sid)
            deleted = claude_sessions.delete_session(target["source_path"], full_id)
            if not deleted:
                return Err(SdkError(f"删除失败（ID 不匹配或文件占用）: {full_id}"))
            self.logger.info("Deleted Claude session: {}", full_id)
            return Ok({"deleted": True, "session_id": full_id})
        except Exception as e:
            self.logger.exception("delete_claude_session_entry failed")
            return Err(SdkError(f"删除会话失败: {e}"))


__all__ = ["ClaudeCodeAdapterPlugin"]
