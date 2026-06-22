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


def test_imports():
    """测试核心模块可以导入。"""
    try:
        from plugin.plugins.claude_code_adapter import executor
        from plugin.plugins.claude_code_adapter import parser
        from plugin.plugins.claude_code_adapter import session
        assert True
    except ImportError as e:
        # 如果不在 N.E.K.O 环境中，跳过
        if "plugin.sdk" in str(e):
            pass
        else:
            raise


def test_parser_basic():
    """测试解析器基本功能。"""
    from plugin.plugins.claude_code_adapter.parser import ClaudeCodeParser
    
    parser = ClaudeCodeParser()
    assert parser is not None


def test_session_basic():
    """测试会话管理基本功能。"""
    from plugin.plugins.claude_code_adapter.session import SessionManager
    
    manager = SessionManager()
    assert manager is not None


if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"])
