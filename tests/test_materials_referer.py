"""T01b: ``download_video`` referer is now parameterisable.

The Referer header used to be hard-coded to Douyin's.  These tests pin the two
contracts that matter:

* the *default* path is unchanged -- a Douyin Referer is still sent, byte for
  byte, for every existing caller;
* an explicit ``referer=`` overrides it, so a non-Douyin CDN (e.g. YouTube) is
  not 403'd by a mismatched referer.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from douyin_intelligence import materials
from douyin_intelligence.config import load_config


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._sent = False
        self.headers: dict[str, str] = {}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._body


def _install(monkeypatch) -> dict:
    captured: dict[str, str] = {}

    def fake_urlopen(request, timeout=None):
        captured.update(dict(request.header_items()))
        return _FakeResponse(b"\x00" * 4096)

    monkeypatch.setattr(materials.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_referer_defaults_to_douyin(monkeypatch, tmp_path: Path) -> None:
    captured = _install(monkeypatch)
    materials.download_video("https://signed.example/x", tmp_path / "v.mp4", load_config())
    assert captured["Referer"] == "https://www.douyin.com/"


def test_explicit_referer_overrides_default(monkeypatch, tmp_path: Path) -> None:
    captured = _install(monkeypatch)
    materials.download_video(
        "https://signed.example/x",
        tmp_path / "v.mp4",
        load_config(),
        referer="https://www.youtube.com/",
    )
    assert captured["Referer"] == "https://www.youtube.com/"


def test_referer_is_an_optional_keyword_only_param() -> None:
    signature = inspect.signature(materials.download_video)
    assert "referer" in signature.parameters
    assert signature.parameters["referer"].default is None
    # The historical positional order is untouched.
    names = list(signature.parameters)
    assert names[:3] == ["url", "destination", "config"]
