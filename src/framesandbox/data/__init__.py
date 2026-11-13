"""数据层：引擎侧全量读取 + 回放剧本 + 确定性合成日内路径。

策略侧唯一合法入口是 ``framesandbox.gateway.PitGateway``；本包内的全量数据
对象（``SandboxData`` / ``ReplayTimeline``）不得直接暴露给策略。
"""

from .replay import MinutePath, QuoteState, ReplayDay, ReplayTimeline, stable_seed
from .synth import synth_auction_path, synth_minute_path
from .warehouse import SandboxData

__all__ = [
    "SandboxData", "ReplayTimeline", "ReplayDay", "QuoteState",
    "MinutePath", "synth_minute_path", "synth_auction_path", "stable_seed",
]
