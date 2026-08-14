"""Тесты для раунда 27 - что dialog_manager РЕАЛЬНО пишет в gap_log в трёх
точках трения: LLM недоступна целиком, инструмент упал с исключением,
исчерпан лимит шагов (hop limit). Путь gap_log подменяется на временный -
реальный data/gaps.jsonl проекта не трогается.
"""
import json
import sys
import types

import pytest

sys.path.insert(0, ".")

import gap_log  # noqa: E402
from agents.dialog_manager import MAX_TOOL_HOPS  # noqa: E402
from test_abort_scenarios import make_chunk, tool_call_delta, FakeClient, make_manager  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    log_file = tmp_path / "gaps.jsonl"
    monkeypatch.setattr(gap_log, "_log_path", lambda: log_file)
    yield log_file


def _read_entries(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class FailingClient:
    def __init__(self):
        self.call_count = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.call_count += 1
        raise ConnectionError("облако недоступно")


def test_llm_unavailable_logs_gap(_isolated_log):
    mgr = make_manager(FailingClient())  # без fallback - облако единственное

    mgr.handle("привет, как дела")

    entries = _read_entries(_isolated_log)
    kinds = [e["kind"] for e in entries]
    assert "llm_unavailable" in kinds
    entry = next(e for e in entries if e["kind"] == "llm_unavailable")
    assert entry["user_text"] == "привет, как дела"


def test_tool_exception_logs_gap(_isolated_log, monkeypatch):
    from agents.tools import TOOL_IMPLEMENTATIONS

    def broken_tool():
        raise RuntimeError("непредвиденный краш инструмента")

    monkeypatch.setitem(TOOL_IMPLEMENTATIONS, "get_current_datetime", broken_tool)

    hop0 = [
        make_chunk(tool_call_delta=tool_call_delta(index=0, call_id="c1",
                                                    name="get_current_datetime", arguments="{}")),
        make_chunk(finish_reason="tool_calls"),
    ]
    hop1 = [make_chunk(content="Не смог узнать время.", finish_reason="stop")]
    client = FakeClient(chunks_by_hop=[hop0, hop1])
    mgr = make_manager(client)

    mgr.handle("сколько времени")

    entries = _read_entries(_isolated_log)
    kinds = [e["kind"] for e in entries]
    assert "tool_exception" in kinds
    entry = next(e for e in entries if e["kind"] == "tool_exception")
    assert "get_current_datetime" in entry["detail"]
    assert "непредвиденный краш инструмента" in entry["detail"]


def test_hop_limit_logs_gap(_isolated_log):
    # Модель бесконечно просит вызвать один и тот же безобидный инструмент,
    # никогда не отдавая finish_reason="stop" - агент обязан сдаться после
    # MAX_TOOL_HOPS шагов.
    hops = []
    for i in range(MAX_TOOL_HOPS):
        hops.append([
            make_chunk(tool_call_delta=tool_call_delta(
                index=0, call_id=f"c{i}", name="get_current_datetime", arguments="{}")),
            make_chunk(finish_reason="tool_calls"),
        ])
    client = FakeClient(chunks_by_hop=hops)
    mgr = make_manager(client)

    result = mgr.handle("сделай что-нибудь бесконечно сложное")

    assert "шагов" in result or "переформулировать" in result
    entries = _read_entries(_isolated_log)
    kinds = [e["kind"] for e in entries]
    assert "hop_limit_reached" in kinds
    entry = next(e for e in entries if e["kind"] == "hop_limit_reached")
    assert entry["user_text"] == "сделай что-нибудь бесконечно сложное"


def test_normal_successful_call_does_not_log_anything(_isolated_log):
    client = FakeClient(chunks_by_hop=[[make_chunk(content="Привет!", finish_reason="stop")]])
    mgr = make_manager(client)

    mgr.handle("привет")

    assert _read_entries(_isolated_log) == []
