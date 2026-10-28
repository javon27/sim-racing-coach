"""An API failure the driver can act on reaches them as a plain CoachUserError,
not the masked "coach error" (rig 2026-10-27: an expired key read as "coach error")."""
import anthropic
import httpx
import pytest

from simcoach.model.base import CoachUserError
from simcoach.model.claude import ClaudeModel

_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(status, kind, message="boom"):
    body = {"type": "error", "error": {"type": kind, "message": message}}
    resp = httpx.Response(status, request=_REQ)
    return anthropic.Anthropic(api_key="x")._make_status_error(
        str(body), body=body, response=resp)


class _RaisingMessages:
    def __init__(self, exc): self.exc = exc
    def stream(self, **kw): raise self.exc


def _run(exc):
    m = ClaudeModel(api_key="x")
    m.client = type("C", (), {"messages": _RaisingMessages(exc)})()
    return list(m.run_stream("sys", [{"role": "user", "content": "q"}], [],
                             lambda n, i: ""))


@pytest.mark.parametrize("exc, needle", [
    (_status_error(401, "authentication_error"), "API key"),
    (_status_error(400, "invalid_request_error",
                   "Your credit balance is too low to access the Anthropic API."),
     "out of credit"),
    (_status_error(403, "permission_error"), "isn't allowed"),
    (_status_error(429, "rate_limit_error"), "rate limit"),
    (_status_error(529, "overloaded_error"), "having trouble"),
    # mid-stream error event: the stream's HTTP 200 + an error body
    (_status_error(200, "overloaded_error"), "having trouble"),
    (_status_error(200, "authentication_error"), "API key"),
    (anthropic.APIConnectionError(request=_REQ), "internet"),
    (anthropic.APITimeoutError(request=_REQ), "internet"),
])
def test_actionable_api_error_is_curated(exc, needle):
    with pytest.raises(CoachUserError, match=needle) as ei:
        _run(exc)
    assert ei.value.__cause__ is exc


def test_other_api_error_stays_masked():
    exc = _status_error(404, "not_found_error")
    with pytest.raises(anthropic.NotFoundError) as ei:
        _run(exc)
    assert ei.value is exc
