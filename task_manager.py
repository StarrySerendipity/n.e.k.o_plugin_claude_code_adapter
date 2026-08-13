"""
Claude Code 异步任务管理器

v0.6.0 起插件全异步化：所有执行一律后台任务，无超时限制
（Claude Code 常执行半小时到一小时以上的长任务）。
任务完成后通过 on_complete 回调主动推送结果给调用方（插件
再经 push_message 注入对话频道，猫娘无需一直轮询）。

其他能力：
- 失败时自动用 session_id 续跑（--resume）而非重开新会话
- wait_for 智能等待（自适应间隔），供猫娘想提前盯时使用
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .errors import is_retryable
from .executor import build_cli_invocation
from .models import ExecuteResult
from .parser import ClaudeOutputParser

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """任务状态"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class TaskRecord:
    """任务记录"""

    task_id: str
    prompt: str
    cwd: str
    model: str = ""
    effort: str = ""
    max_turns: int = 0
    resume_session_id: str = ""  # 提交时指定的续跑会话（空=新会话）
    session_id: str = ""  # 执行过程中从 CLI 流中捕获的会话 ID
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[ExecuteResult] = None
    error_message: Optional[str] = None
    _task: Optional[asyncio.Task] = field(default=None, repr=False)
    _parser: Optional[Any] = field(default=None, repr=False)  # 运行中实时进度来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于返回给调用方）"""
        elapsed = 0.0
        if self.started_at:
            end_time = self.finished_at or time.time()
            elapsed = end_time - self.started_at

        data = {
            "task_id": self.task_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "elapsed": round(elapsed, 2),
        }

        if self.session_id:
            data["session_id"] = self.session_id

        # 运行中：附带实时进度（已输出的 assistant 消息数与最近动态），
        # 便于猫娘向用户汇报「任务在干吗」而不是干等
        if self.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            progress: Dict[str, Any] = {"message_count": 0}
            parser = self._parser
            if parser is not None:
                try:
                    messages = getattr(parser, "messages", None) or []
                    progress["message_count"] = len(messages)
                    for msg in reversed(messages):
                        if getattr(msg, "text", ""):
                            progress["last_activity"] = msg.text[:300]
                            break
                except Exception:
                    pass
            data["progress"] = progress

        if self.status == TaskStatus.DONE and self.result:
            data["result"] = self.result.to_llm_payload()
        elif self.status == TaskStatus.ERROR:
            data["error"] = self.error_message or "Unknown error"
        elif self.status == TaskStatus.CANCELLED:
            data["error"] = "Task was cancelled"

        return data


class TaskManager:
    """
    Claude Code 任务管理器

    功能：
    - 提交任务到后台执行（asyncio.create_task），无超时限制
    - 查询任务状态和结果（运行中含实时进度）
    - 取消正在运行的任务
    - 任务到达终态（完成/失败）时触发 on_complete 回调，
      供插件主动 push 结果给猫娘（无需轮询到底）
    - 自动清理过期任务（结果保留 1 小时）
    - 并发控制（最多同时 3 个任务）
    """

    def __init__(
        self,
        executor,
        config,
        *,
        logger=None,
        env_provider=None,
        on_complete: Optional[Callable[["TaskRecord"], None]] = None,
    ):
        self._executor = executor
        self._config = config
        self.logger = logger or logging.getLogger(__name__)
        # env_provider: 无参 callable，返回激活 provider 的环境变量覆盖
        self._env_provider = env_provider
        # on_complete: 任务到达终态（DONE/ERROR）时的回调，入参为 TaskRecord。
        # 回调内部异常不影响任务状态（推送失败只记日志）。
        self._on_complete = on_complete
        self._tasks: Dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._max_concurrent = 3  # 最大并发任务数
        self._result_ttl = 3600  # 结果保留时间（秒）：长任务完成后猫娘可能过一阵才来取

    async def start(self):
        """启动清理任务"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self.logger.info("TaskManager started")

    async def stop(self):
        """停止所有任务"""
        # 取消清理任务
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        # 取消所有未完成任务（PENDING 在队列中、RUNNING 在执行中，
        # 都必须取消，避免插件关闭后留下无人管控的 CLI 进程）
        async with self._lock:
            for record in self._tasks.values():
                if (
                    record.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
                    and record._task
                ):
                    record._task.cancel()
                    try:
                        await record._task
                    except (asyncio.CancelledError, Exception):
                        pass
            self._tasks.clear()

        self.logger.info("TaskManager stopped")

    async def submit(
        self,
        prompt: str,
        cwd: str = "",
        model: str = "",
        effort: str = "",
        max_turns: int = 0,
        resume_session_id: str = "",
    ) -> TaskRecord:
        """
        提交任务到后台执行

        Parameters
        ----------
        resume_session_id:
            由调用方（插件主类）根据 session_mode 预先解析：
            非空时任务会以 ``--resume <id>`` 续跑该会话；空则新开会话。

        Returns:
            TaskRecord: 任务记录（包含 task_id 和初始状态）
        """
        # 在锁内检查并发限制并创建任务记录
        async with self._lock:
            running_count = sum(
                1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING
            )
            if running_count >= self._max_concurrent:
                raise RuntimeError(
                    f"已达到最大并发任务数 {self._max_concurrent}，请等待现有任务完成后再提交"
                )

            task_id = str(uuid.uuid4())[:8]
            record = TaskRecord(
                task_id=task_id,
                prompt=prompt,
                cwd=cwd,
                model=model,
                effort=effort,
                max_turns=max_turns,
                resume_session_id=resume_session_id,
                status=TaskStatus.PENDING,
            )
            self._tasks[task_id] = record

        # 在锁外启动后台任务，避免死锁
        record._task = asyncio.create_task(self._execute_task(record))
        self.logger.info(f"Task submitted: {task_id}")

        return record

    async def poll(self, task_id: str) -> Dict[str, Any]:
        """
        查询任务状态

        Returns:
            任务状态字典
        """
        async with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return {"error": f"Task not found: {task_id}"}
            self._reconcile_stuck_record(record)
            return record.to_dict()

    @staticmethod
    def _reconcile_stuck_record(record: TaskRecord) -> None:
        """防御性核对：任务协程已结束但状态仍是 PENDING/RUNNING 时强制纠正。

        正常路径下 _execute_task 总会把状态推进到终态；但若协程被
        意外吞掉（如事件循环关闭、未预期的异常逃逸），状态会永远停在
        running，猫娘轮询永远拿不到结果。查询时发现协程已死则直接
        标记 ERROR，让调用方拿到确定性结果（可用 session_id 续跑）。
        """
        if record.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
            return
        task = record._task
        if task is not None and task.done():
            record.status = TaskStatus.ERROR
            record.error_message = (
                "任务执行协程已意外结束但未写入结果，已强制标记为失败；"
                "若会话已有进展，可用 session_id 续跑（--resume）。"
            )
            record.finished_at = record.finished_at or time.time()

    def get_record(self, task_id: str) -> Optional[TaskRecord]:
        """返回任务记录对象（不存在时返回 None）。供插件主类编排用。"""
        return self._tasks.get(task_id)

    async def wait_for(
        self,
        task_id: str,
        max_wait_sec: float = 60.0,
        interval_sec: float = 0.0,
    ) -> Dict[str, Any]:
        """提前盯进度：在插件内部轮询任务直到完成或本次等待超时。

        定位：任务完成后插件会经 on_complete 主动推送结果，本方法
        只供猫娘想提前盯进度时使用：
        - 任务一旦完成立即返回，不白等
        - 自适应退避间隔（3s → 5s → 8s → 封顶 15s），避免空转
        - 超时后返回实时进度和建议的下次等待时长，供猫娘向用户汇报

        Returns:
            完成/失败时返回完整结果；仍在运行时返回进度 +
            ``still_running=True`` 和 ``suggested_next_wait_sec``。
        """
        deadline = time.time() + max(1.0, max_wait_sec)
        # 自适应间隔：斐波那契式退避，封顶 15s
        interval = interval_sec if interval_sec > 0 else 3.0
        interval = max(1.0, min(interval, 15.0))

        while True:
            async with self._lock:
                record = self._tasks.get(task_id)
                if not record:
                    return {"error": f"Task not found: {task_id}"}
                self._reconcile_stuck_record(record)
                if record.status in (
                    TaskStatus.DONE,
                    TaskStatus.ERROR,
                    TaskStatus.CANCELLED,
                ):
                    return record.to_dict()
                elapsed = time.time() - (record.started_at or record.created_at)

            if time.time() >= deadline:
                data = record.to_dict()
                data["still_running"] = True
                # 建议下次等待时长：夹在 [5, 55]（单次工具调用受网关 ~60s 超时限制）
                data["suggested_next_wait_sec"] = min(
                    55.0, max(5.0, round(max_wait_sec * 1.0))
                )
                data["message"] = (
                    f"任务仍在运行（已耗时 {elapsed:.0f}s，任务本身无超时限制）。"
                    f"任务完成后插件会自动推送结果；若想继续盯，"
                    f"建议 {data['suggested_next_wait_sec']:.0f}s 后再次调用 claude_code_wait。"
                )
                return data

            await asyncio.sleep(min(interval, max(0.1, deadline - time.time())))
            interval = min(15.0, interval * 1.6)

    async def cancel(self, task_id: str) -> Dict[str, Any]:
        """
        取消任务

        Returns:
            取消结果
        """
        async with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return {"error": f"Task not found: {task_id}"}

            if record.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
                return {"error": f"Task already finished: {record.status.value}"}

            # 取消任务
            task = record._task
            record.status = TaskStatus.CANCELLED
            record.finished_at = time.time()

        # 在锁外取消任务
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        self.logger.info(f"Task cancelled: {task_id}")
        return {"status": "cancelled", "task_id": task_id}

    async def _execute_task(self, record: TaskRecord):
        """后台执行任务。

        失败处理策略（v0.3.0）：
        - 若错误可重试且已从流中捕获到 session_id，自动用
          ``--resume <session_id>`` 续跑一次，而非重开新会话（保留上下文）。
        - 续跑仍失败才标记为 ERROR。
        """
        try:
            record.status = TaskStatus.RUNNING
            record.started_at = time.time()

            # 提交时确定的续跑会话
            resume_id = record.resume_session_id

            result, exec_err = await self._run_once(record, resume_id)

            # 可重试失败 + 已有会话 → 用 session_id 续跑一次
            if (
                exec_err is not None
                and is_retryable(getattr(exec_err, "kind", ""))
                and record.session_id
            ):
                self.logger.info(
                    f"Task {record.task_id}: retryable failure "
                    f"({getattr(exec_err, 'kind', 'unknown')}), "
                    f"resuming session {record.session_id}"
                )
                result, exec_err = await self._run_once(record, record.session_id)

            if exec_err:
                raise RuntimeError(f"Execution failed: {exec_err}")

            record.result = result
            record.status = TaskStatus.DONE
            record.finished_at = time.time()
            self.logger.info(f"Task completed: {record.task_id}")
            self._fire_complete(record)

        except asyncio.CancelledError:
            record.status = TaskStatus.CANCELLED
            record.finished_at = time.time()
            self.logger.info(f"Task cancelled: {record.task_id}")
            raise

        except Exception as e:
            record.status = TaskStatus.ERROR
            record.error_message = str(e)
            record.finished_at = time.time()
            self.logger.error(f"Task failed: {record.task_id}, error: {e}")
            self._fire_complete(record)

    def _fire_complete(self, record: TaskRecord) -> None:
        """触发完成回调（推送结果给猫娘）。回调异常不影响任务本身。"""
        if self._on_complete is None:
            return
        try:
            self._on_complete(record)
        except Exception as e:
            self.logger.warning(f"on_complete callback failed: {record.task_id}: {e}")

    async def _run_once(self, record: TaskRecord, resume_id: str):
        """执行一次 CLI 调用，返回 (ExecuteResult | None, error | None)。"""
        # 构建 CLI 调用（注入激活 provider 的环境变量，提交时取值）
        extra_env = self._env_provider() if callable(self._env_provider) else {}
        invocation, build_err = build_cli_invocation(
            self._config,
            prompt=record.prompt,
            resume_session_id=resume_id,
            cwd=record.cwd,
            model=record.model,
            effort=record.effort,
            max_turns=record.max_turns,
            extra_env=extra_env,
        )

        if build_err:
            raise RuntimeError(f"Failed to build CLI invocation: {build_err}")

        # session_id 早期捕获：init 事件到达时立即写入 record，
        # 使任务执行中途也能感知会话 UUID（供中断后同会话续发）
        def _capture_session_id(sid: str) -> None:
            if sid:
                record.session_id = sid

        parser = ClaudeOutputParser(on_session_id=_capture_session_id)
        # 挂载到 record，供 poll/wait 在运行中读取实时进度
        record._parser = parser
        stream, exec_err = await self._executor.execute(invocation, parser)

        # 捕获会话 ID（无论成败，供续跑和 UI 展示）
        if stream.session_id:
            record.session_id = stream.session_id

        if exec_err:
            return None, exec_err

        result = ExecuteResult(
            session_id=stream.session_id,
            is_new_session=not bool(resume_id),
            messages=stream.messages,
            final_text=stream.final_text,
            total_cost_usd=stream.result.total_cost_usd if stream.result else 0.0,
            duration_ms=stream.result.duration_ms if stream.result else 0,
            num_turns=stream.result.num_turns if stream.result else 0,
            raw_result=stream.result.raw if stream.result else {},
        )
        return result, None

    async def _cleanup_loop(self):
        """定期清理过期任务"""
        try:
            while True:
                await asyncio.sleep(60)  # 每分钟检查一次
                await self._cleanup_expired()
        except asyncio.CancelledError:
            pass

    async def _cleanup_expired(self):
        """清理过期的已完成任务"""
        async with self._lock:
            now = time.time()
            expired_ids = []

            for task_id, record in self._tasks.items():
                if record.status in (
                    TaskStatus.DONE,
                    TaskStatus.ERROR,
                    TaskStatus.CANCELLED,
                ):
                    if (
                        record.finished_at
                        and (now - record.finished_at) > self._result_ttl
                    ):
                        expired_ids.append(task_id)

            for task_id in expired_ids:
                del self._tasks[task_id]
                self.logger.debug(f"Cleaned up expired task: {task_id}")

    def get_running_count(self) -> int:
        """获取当前运行中的任务数"""
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        """获取所有任务的状态（用于调试/监控）"""
        for record in self._tasks.values():
            self._reconcile_stuck_record(record)
        return [record.to_dict() for record in self._tasks.values()]
