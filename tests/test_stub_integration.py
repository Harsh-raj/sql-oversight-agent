"""
Stub-mode integration tests (closes the Stage 7 gap: "stub-mode
integration tests" was in the original roadmap description but never
built). These run the FULL graph through the REAL `ollama` client
library's HTTP request/response code path, against a lightweight fake
server that speaks Ollama's actual API — not a Python-level mock.

Why this matters beyond test_graph_integration.py: that suite patches
`ollama.chat` at the Python function level, which never exercises the
real client's request serialization or response-schema validation. A
mismatch there (e.g. a field name change in a future `ollama` package
version) would pass every test in that suite and only surface against a
live Ollama instance. This suite catches that class of bug in CI,
without needing a real, heavyweight, slow local LLM.

Important implementation detail, confirmed by reading the ollama
package's source rather than assumed: `ollama.chat`/`ollama.embeddings`
are bound to a client instance created ONCE at import time
(`_client = Client()` in ollama/__init__.py), which reads OLLAMA_HOST
at that moment. Setting the env var in a fixture does NOT retroactively
change an already-imported client. Instead, we build a fresh
`ollama.Client(host=stub_url)` and monkeypatch the module-level
`ollama.chat`/`ollama.embeddings` attributes directly to that client's
bound methods, for the duration of each test.

These tests carry no external dependency (the stub server is pure
stdlib) and run fast, so unlike `@pytest.mark.integration` (which
requires a genuinely live Ollama instance and is excluded from CI),
these run as part of the normal `pytest -m "not integration"` CI suite.
"""

import ollama
import pytest

from src.graph import build_graph
from tests.fake_ollama_server import start_fake_ollama_server


@pytest.fixture
def fake_ollama(monkeypatch):
    server, port = start_fake_ollama_server()
    stub_client = ollama.Client(host=f"http://127.0.0.1:{port}")

    monkeypatch.setattr(ollama, "chat", stub_client.chat)
    monkeypatch.setattr(ollama, "embeddings", stub_client.embeddings)

    yield stub_client

    server.shutdown()


@pytest.mark.stub_integration
def test_full_graph_against_real_http_stub_server(fake_ollama):
    """Runs the complete graph through the REAL ollama.Client HTTP code
    path — request serialization, response parsing via ChatResponse —
    against our stub server, not a Python-level mock."""
    graph = build_graph()
    final_state = graph.invoke(
        {"question": "How many Design courses are there?", "generation_attempt": 0}
    )

    assert not final_state.get("escalated")
    assert final_state["step_results"][0]["sql_result"][0]["COUNT(*)"] == 1189


@pytest.mark.stub_integration
def test_correction_memory_embeddings_call_against_real_http_stub_server(fake_ollama):
    """Confirms the embeddings path (used by correction memory) also
    round-trips correctly through the real client against the stub
    server, independent of the chat path."""
    from src.memory.correction_store import CorrectionStore

    store = CorrectionStore.__new__(CorrectionStore)
    store.collection = "test_stub_collection"

    embedding = store._embed("How many Design courses are there?")
    assert isinstance(embedding, list)
    assert len(embedding) == 384
