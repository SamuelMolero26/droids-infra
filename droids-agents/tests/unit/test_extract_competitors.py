"""extract_competitors with injected stub client."""

from __future__ import annotations

from types import SimpleNamespace

from droids_agents import router


class _StubMessages:
    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)])


class _StubClient:
    def __init__(self, text: str) -> None:
        self.messages = _StubMessages(text)


def test_extracts_two_names() -> None:
    out = router.extract_competitors(
        "Research API cost between OpenAI and Anthropic",
        client=_StubClient('{"competitors": ["OpenAI", "Anthropic"]}'),
    )
    assert out == ["OpenAI", "Anthropic"]


def test_preserves_casing() -> None:
    out = router.extract_competitors(
        "Compare HubSpot vs Salesforce",
        client=_StubClient('{"competitors": ["HubSpot", "Salesforce"]}'),
    )
    assert out == ["HubSpot", "Salesforce"]


def test_returns_empty_when_none_identifiable() -> None:
    out = router.extract_competitors(
        "How does machine learning work?",
        client=_StubClient('{"competitors": []}'),
    )
    assert out == []


def test_extracts_from_markdown_fenced_json() -> None:
    """Haiku often wraps JSON in ```json fences — must still parse."""
    fenced = '```json\n{"competitors": ["OpenAI", "Anthropic"]}\n```'
    out = router.extract_competitors(
        "Research API costs between OpenAI and Anthropic",
        client=_StubClient(fenced),
    )
    assert out == ["OpenAI", "Anthropic"]


def test_extracts_from_json_with_surrounding_prose() -> None:
    noisy = 'Here are the competitors:\n{"competitors": ["Mistral"]}\nHope that helps!'
    out = router.extract_competitors("x", client=_StubClient(noisy))
    assert out == ["Mistral"]


def test_returns_empty_on_bad_json() -> None:
    out = router.extract_competitors("anything", client=_StubClient("not json"))
    assert out == []


def test_returns_empty_on_missing_key() -> None:
    out = router.extract_competitors("anything", client=_StubClient('{"steps": []}'))
    assert out == []


def test_strips_whitespace_from_names() -> None:
    out = router.extract_competitors(
        "Compare X vs Y",
        client=_StubClient('{"competitors": ["  OpenAI  ", " Anthropic"]}'),
    )
    assert out == ["OpenAI", "Anthropic"]


def test_filters_empty_strings() -> None:
    out = router.extract_competitors(
        "Compare X vs Y",
        client=_StubClient('{"competitors": ["OpenAI", "", "  "]}'),
    )
    assert out == ["OpenAI"]
