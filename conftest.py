"""Root conftest — mock N.E.K.O SDK and prevent root __init__.py collection."""

import sys
from unittest.mock import MagicMock

# Prevent pytest from collecting root Python files as test modules
# Use relative paths from conftest.py location
collect_ignore = [
    "__init__.py",
    "executor.py",
    "models.py",
    "parser.py",
    "session.py",
    "errors.py",
]

# Mock N.E.K.O SDK before any plugin import
if "plugin" not in sys.modules:
    mock_plugin_module = MagicMock()
    mock_plugin_module.NekoPluginBase = MagicMock
    mock_plugin_module.neko_plugin = lambda x: x
    mock_plugin_module.plugin_entry = lambda **kwargs: lambda x: x
    mock_plugin_module.lifecycle = lambda **kwargs: lambda x: x
    mock_plugin_module.llm_tool = lambda **kwargs: lambda x: x
    mock_plugin_module.Ok = lambda x: x
    mock_plugin_module.Err = lambda x: x
    mock_plugin_module.SdkError = Exception

    sys.modules["plugin"] = MagicMock()
    sys.modules["plugin.sdk"] = MagicMock()
    sys.modules["plugin.sdk.plugin"] = mock_plugin_module
