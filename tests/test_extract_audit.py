"""Mock integration tests for chunking, unload verification and extraction audit."""

import io
import json
import urllib.error
from uuid import UUID, uuid4

import pytest

from biscuit.extract.audit import PostgresExtractionAuditWriter, sha256_json
from biscuit.extract.client import LocalLLMClient
from biscuit.extract.pass1_discovery import Pass1ProposalDiscovery
from biscuit.extract.pass3_arguments import Pass3ArgumentMining
from biscuit.extract.pass4_synthesis import Pass4SchemaSynthesis
from biscuit.extract.pipeline import ExtractionPipeline
from biscuit.extract.types import DeliberationPacket


class SequenceClient(LocalLLMClient):
    def __init__(self, responses):
        super().__init__(mock_mode=True)
        self.responses = iter(responses)

    def generate(self, model, prompt, temperature=0, keep_alive=0):
        self.call_history.append({"model": model, "prompt": prompt, "keep_alive": keep_alive})
        return next(self.responses)


def test_pass1_message_chunks_are_bounded_and_candidates_merge():
    responses = [
        json.dumps({"proposals": [
            {"proposal_id": "P1", "name": "Alpha", "summary": "a", "syntax_or_api": "A()"}
        ]}),
        json.dumps({"proposals": [
            {"proposal_id": "P1", "name": "Beta", "summary": "b", "syntax_or_api": "B()"}
        ]}),
        json.dumps({"proposals": [
            {"proposal_id": "P1", "name": "Alpha", "summary": "a", "syntax_or_api": "A()"}
        ]}),
    ]
    client = SequenceClient(responses)
    discovery = Pass1ProposalDiscovery(client, chunk_word_limit=10)
    text = "\n".join(
        f"[m{i}] @someone (contributor): words one two three four"
        for i in range(3)
    )
    chunks = discovery._chunks(text)
    assert len(chunks) == 3
    assert all(len(chunk.split()) <= 10 for chunk in chunks)
    found = discovery.discover(text)
    assert [(p.proposal_id, p.name) for p in found] == [("P1", "Alpha"), ("P2", "Beta")]
    assert len(client.call_history) == 3
    assert all(call["keep_alive"] == 0 for call in client.call_history)


class RefusingUnload(LocalLLMClient):
    def __init__(self):
        super().__init__(mock_mode=True, mock_responses={
            "llama3.2:3b": '{"proposals":[]}',
            "phi3.5": '{"arguments":[]}',
            "qwen2.5-coder:7b": '{}',
        })

    def unload_model(self, model):
        return False


@pytest.mark.parametrize("pass_number", [1, 3, 4])
def test_unload_failure_aborts_every_model_pass(pass_number):
    client = RefusingUnload()
    with pytest.raises(RuntimeError, match="could not be unloaded"):
        if pass_number == 1:
            Pass1ProposalDiscovery(client).discover("proposal discussion")
        elif pass_number == 3:
            Pass3ArgumentMining(client).mine_arguments(
                DeliberationPacket(proposal_id="P1", proposal_name="P1", messages=[])
            )
        else:
            Pass4SchemaSynthesis(client).synthesize_madr(
                "topic", "DEC-1", proposals=[], arguments=[]
            )


def test_live_ollama_offload_requires_actual_absence(monkeypatch):
    calls = []

    def mock_urlopen(req, timeout):
        calls.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/api/ps"):
            payload = {"models": []}
        else:
            payload = {}
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    client = LocalLLMClient(mock_mode=False)
    assert client.unload_model("phi3.5") is True
    assert calls == [
        ("POST", "http://localhost:11434/api/generate"),
        ("GET", "http://localhost:11434/api/ps"),
    ]


def test_live_ollama_offload_fails_closed_on_status_error(monkeypatch):
    def broken_urlopen(req, timeout):
        if req.full_url.endswith("/api/ps"):
            raise urllib.error.URLError("status endpoint unavailable")
        return io.BytesIO(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", broken_urlopen)
    assert LocalLLMClient(mock_mode=False).unload_model("phi3.5") is False


class RecordingWriter:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(dict(record))


REVISIONS = {"pass1": "sha256:111", "pass3": "sha256:222",
             "pass4": "sha256:333", "embedding": "sha256:444"}


def test_audit_manifest_is_hashed_and_persisted_with_status_transitions():
    writer = RecordingWriter()
    pipe = ExtractionPipeline(client=LocalLLMClient(mock_mode=True), audit_writer=writer)
    result = pipe.run(
        thread_id="discussion", topic="Decision",
        messages=[{"message_id": "m1", "content": "Open proposal"}],
        source_thread_id=uuid4(),
        environment_lock_sha256="a" * 64,
        model_revisions=REVISIONS,
    )
    assert [record["run_status"] for record in writer.records] == ["RUNNING", "SUCCESS"]
    assert writer.records[0]["id"] == UUID(result.run_id)
    assert writer.records[1]["records_extracted"] == 0
    assert writer.records[0]["run_config_sha256"] == sha256_json(writer.records[0]["run_config"])
    assert len(writer.records[0]["source_thread_snapshot_sha256"]) == 64
    assert result.audit_metadata["audit_persisted"] is True


def test_audit_writer_requires_real_db_provenance_and_does_not_claim_persistence():
    writer = RecordingWriter()
    pipe = ExtractionPipeline(client=LocalLLMClient(mock_mode=True), audit_writer=writer)
    with pytest.raises(ValueError, match="source_thread_id"):
        pipe.run("topic", "decision", [], model_revisions=REVISIONS,
                 environment_lock_sha256="a" * 64)
    assert writer.records == []
    without_writer = ExtractionPipeline(client=LocalLLMClient(mock_mode=True))
    res = without_writer.run("topic", "decision", [])
    assert res.audit_metadata["audit_persisted"] is False
    assert res.audit_metadata["environment_lock_sha256"] is None


def test_audit_records_failed_transition_on_inference_exception():
    class BrokenClient(LocalLLMClient):
        def generate(self, model, prompt, temperature=0, keep_alive=0):
            raise RuntimeError("inference failed")

    writer = RecordingWriter()
    pipe = ExtractionPipeline(client=BrokenClient(mock_mode=True), audit_writer=writer)
    with pytest.raises(RuntimeError, match="inference failed"):
        pipe.run(
            thread_id="t", topic="Decision", messages=[{"content": "proposal"}],
            source_thread_id=uuid4(), model_revisions=REVISIONS,
            environment_lock_sha256="b" * 64,
        )
    assert [row["run_status"] for row in writer.records] == ["RUNNING", "FAILED"]


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.closed = False

    def execute(self, sql, args):
        self.executed.append((sql, args))

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_postgres_writer_uses_parameterized_extraction_runs_upsert():
    conn = FakeConnection()
    record = {
        "id": uuid4(), "source_thread_id": uuid4(), "pipeline_version": "1.0.0",
        "schema_version": "MADR-3.0", "run_config": {"mock_mode": True},
        "run_config_sha256": "a" * 64, "environment_lock_sha256": "b" * 64,
        "model_pass1_name": "model1", "model_pass1_revision": "revision1",
        "model_pass3_name": "model3", "model_pass3_revision": "revision3",
        "model_pass4_name": "model4", "model_pass4_revision": "revision4",
        "embedding_model_name": "modelE", "embedding_model_revision": "revisionE",
        "prompt_templates_hash": "c" * 64,
        "source_thread_snapshot_sha256": "d" * 64,
        "run_status": "RUNNING", "records_extracted": 0,
    }
    PostgresExtractionAuditWriter(conn).write(record)
    assert conn.commits == 1 and conn.rollbacks == 0
    assert conn.cursor_obj.closed is True
    sql, args = conn.cursor_obj.executed[0]
    assert "ON CONFLICT" in sql and "extraction_runs" in sql
    assert len(args) == 20
    assert args[4] == '{"mock_mode":true}'
