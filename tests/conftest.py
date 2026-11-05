import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib

# Load the plugin modules as top-level siblings (the same fallback the
# plugin itself supports), then expose register() from __init__.
client = importlib.import_module("client")
hooks = importlib.import_module("hooks")
tools = importlib.import_module("tools")
schemas = importlib.import_module("schemas")
init = importlib.import_module("__init__") if False else None

import types

pkg = types.SimpleNamespace(
    register=None, client=client, hooks=hooks, tools=tools, schemas=schemas
)
