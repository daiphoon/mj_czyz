"""仅登记派发阶段与数值用量；不记录URL、请求正文、认证头或响应原文。"""
from contextlib import contextmanager
from contextvars import ContextVar
import math

_current = ContextVar("sqmy_http_audit", default=None)


class TransportAudit:
    def __init__(self, *, tracked=False, cache=False, checkpoint=None):
        self.coverage = "cache" if cache else "tracked_http" if tracked else "unknown"
        self.dispatches = 0 if tracked or cache else None
        self.responses = 0 if tracked or cache else None
        self.phase = "cache_reuse" if cache else "provider_setup" if tracked else "unknown"
        self.failure_stage = None
        self.http_status = None
        self.reported_credits = None
        self.checkpoint = checkpoint

    def snapshot(self):
        return dict(version=1, coverage=self.coverage, dispatch_attempts=self.dispatches,
                    responses_received=self.responses, phase=self.phase, failure_stage=self.failure_stage,
                    http_status=self.http_status, reported_credits=self.reported_credits)

    def before_dispatch(self):
        old = self.snapshot()
        self.coverage = "tracked_http"
        self.dispatches = (self.dispatches or 0) + 1
        self.responses = self.responses or 0
        self.phase = "dispatching"
        try:
            if self.checkpoint:
                self.checkpoint(self.snapshot())  # .open之前持久化；崩溃窗口的送达状态仍未知。
        except BaseException:
            self.dispatches = old["dispatch_attempts"]
            self.responses = old["responses_received"]
            self.phase = "audit_checkpoint"
            raise

    def response(self, status):
        self.responses = (self.responses or 0) + 1
        self.http_status = int(status) if isinstance(status, int) else None
        self.phase = "response_read"

    def usage(self, data):
        value = data.get("usage", {}).get("credits") if isinstance(data.get("usage"), dict) else None
        if type(value) in {int, float} and value >= 0:
            try:
                if math.isfinite(value):
                    self.reported_credits = value
            except OverflowError:
                pass  # 畸形供应商数值不冒充可计量用量。


@contextmanager
def observing(audit):
    token = _current.set(audit)
    try:
        yield audit
    except BaseException:
        audit.failure_stage = audit.phase
        raise
    finally:
        _current.reset(token)


def current_audit():
    return _current.get()


def phase(value):
    if (audit := current_audit()) is not None:
        audit.phase = value


def before_dispatch():
    if (audit := current_audit()) is not None:
        audit.before_dispatch()


def response(status):
    if (audit := current_audit()) is not None:
        audit.response(status)


def usage(data):
    if (audit := current_audit()) is not None:
        audit.usage(data)
