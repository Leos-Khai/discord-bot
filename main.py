import asyncio
import importlib.util
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"


def _load_bot_module():
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))

    spec = importlib.util.spec_from_file_location("discord_bot_main", SRC_DIR / "main.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load bot entry module.")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run():
    module = _load_bot_module()
    asyncio.run(module.main())


if __name__ == "__main__":
    run()
