"""沙盒异常类型。

设计原则与 framebt 一致：**让错误结构性地发生不了，发生了就大声报错并留痕**。

  - ``LookaheadViolation``  —— 策略试图访问虚拟当前时刻之后的数据（前视）。
  - ``ClockWriteViolation`` —— 策略试图篡改/回拨权威虚拟时钟。
  - ``IsolationBreach``     —— 策略试图绕过隔离（真实网络取数、真实时间源等）。
  - ``SandboxError``        —— 沙盒内部不变式被破坏（理论上不应出现，属引擎 bug）。

所有违规都会同时写入网关审计日志（``gateway.audit``），供报告与测试断言。
"""

from __future__ import annotations


class SandboxError(RuntimeError):
    """沙盒内部不变式错误（引擎侧 bug，不是策略的错）。"""


class LookaheadViolation(SandboxError):
    """前视访问：请求的数据时间戳晚于权威虚拟时间。"""

    def __init__(self, message: str, *, api: str = "", requested=None, now=None):
        super().__init__(message)
        self.api = api
        self.requested = requested
        self.now = now


class ClockWriteViolation(SandboxError):
    """试图通过公开 API 篡改权威虚拟时钟（I5 同构）。"""


class IsolationBreach(SandboxError):
    """试图绕过沙盒隔离（例如在沙盒内访问真实行情网络）。"""

    def __init__(self, message: str, *, target: str = ""):
        super().__init__(message)
        self.target = target
