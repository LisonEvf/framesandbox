"""framesandbox —— 基于 framebt 的实盘时放沙盒。

实盘策略项目在这里运行，感知到的是**加速的历史时间**与**PIT 历史数据**：
盘中假信号（假突破 / 冲高回落）会真实地欺骗策略，而数据前视被结构性隔绝。

快速开始::

    from framesandbox import SandboxConfig, SandboxRunner
    from framesandbox.adapters.poslayer import PosLayerStrategy

    cfg = SandboxConfig(start="2024-01-01", end="2026-10-26",
                        resolution="daily", cost_preset="poslayer",
                        initial_cash=200_000)
    result = SandboxRunner(cfg, [PosLayerStrategy()]).run()
    print(result.summary())
"""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"

# framebt 引导：未 pip install 时自动把兄弟项目 framebt/src 加入 sys.path。
try:
    import framebt  # noqa: F401
except ImportError:
    _trade_root = Path(__file__).resolve().parents[3]
    _fb_src = _trade_root / "framebt" / "src"
    if _fb_src.is_dir():
        sys.path.insert(0, str(_fb_src))

from .broker import MarketEvent, PaperBroker, default_cost, poslayer_cost  # noqa: E402
from .clock import VirtualClock  # noqa: E402
from .deception import (  # noqa: E402
    DEFAULT_SPECS, AdversarialInjector, FakeSignalDetector, SignalEpisode, SignalSpec,
)
from .errors import (  # noqa: E402
    ClockWriteViolation, IsolationBreach, LookaheadViolation, SandboxError,
)
from .gateway import EngineView, PitGateway, Quote  # noqa: E402
from .isolation import Isolation  # noqa: E402
from .runner import SandboxConfig, SandboxResult, SandboxRunner  # noqa: E402
from .strategy_api import SandboxContext, SandboxStrategy  # noqa: E402

__all__ = [
    "SandboxConfig", "SandboxRunner", "SandboxResult",
    "SandboxStrategy", "SandboxContext",
    "PitGateway", "EngineView", "Quote",
    "PaperBroker", "MarketEvent", "default_cost", "poslayer_cost",
    "VirtualClock", "Isolation",
    "FakeSignalDetector", "AdversarialInjector", "SignalSpec", "SignalEpisode",
    "DEFAULT_SPECS",
    "LookaheadViolation", "ClockWriteViolation", "IsolationBreach", "SandboxError",
]
