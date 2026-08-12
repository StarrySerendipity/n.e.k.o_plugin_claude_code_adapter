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


if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"])
