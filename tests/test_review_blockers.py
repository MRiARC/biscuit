"""Regression coverage for the seven blocking findings on PR #23."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from biscuit.extract.client import LocalLLMClient
from biscuit.extract.pass2_slicing import Pass2DeliberationSlicing
from biscuit.extract.pass4_synthesis import Pass4SchemaSynthesis
from biscuit.extract.pipeline import (
    ExtractionPipeline,
    _is_affirmative_signoff,
    _verify_ruling_alternative_link,
)
from biscuit.extract.types import ProposalCandidate
from biscuit.provenance.types import EvidenceRole, EvidenceSpan, EvidenceStance
from biscuit.provenance.verifier import EvidenceVerifier


def _proposal(pid: str) -> ProposalCandidate:
    return ProposalCandidate(proposal_id=pid, name=f"Proposal {pid}", summary=pid)


def _client(proposals, arguments, alternatives):
    return LocalLLMClient(
        mock_mode=True,
        mock_responses={
            "llama3.2:3b": json.dumps({"proposals": proposals}),
            "phi3.5": json.dumps({"arguments": arguments}),
            "qwen2.5-coder:7b": json.dumps({
                "title": "Decision",
                "status": "REJECTED",
                "considered_alternatives": alternatives,
                "evidence_quotes": [],
            }),
        },
    )


def test_b2_idless_messages_have_canonical_ids():
    slicer = Pass2DeliberationSlicing()
    proposals = [_proposal("P1")]
    messages = [
        {"author": "alice", "content": "Proposal P1 introduces streaming."},
        {"author": "bob", "content": "Reply about memory bounds."},
    ]
    packet = slicer.slice_deliberation(messages, proposals)[0]
    assert packet.message_ids == ["msg-0", "msg-1"]
    assert len(packet.messages) == 2


def test_b2_mixed_ids_do_not_silently_drop_messages():
    slicer = Pass2DeliberationSlicing()
    messages = [
        {"message_id": "explicit", "content": "Proposal P1 was suggested."},
        {"author": "bob", "content": "Follow-up without message ID."},
        {"id": "alias", "content": "Third reply with alternate ID field."},
    ]
    packet = slicer.slice_deliberation(messages, [_proposal("P1")])[0]
    assert packet.message_ids == ["explicit", "msg-1", "alias"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("considered_alternatives", {"P1": "REJECTED"}),
        ("considered_alternatives", ["not a mapping"]),
        ("considered_alternatives", "wrong type"),
        ("evidence_quotes", {"quote_text": "invented"}),
        ("evidence_quotes", [3]),
        ("decision_outcome", ["wrong type"]),
    ],
)
def test_b3_malformed_pass4_fields_fail_closed(field, value):
    model_response = json.dumps({"status": "REJECTED", field: value})
    client = LocalLLMClient(
        mock_mode=True, mock_responses={"qwen2.5-coder:7b": model_response}
    )
    result = Pass4SchemaSynthesis(client).synthesize_madr(
        topic="T", decision_id="DEC-X", proposals=[_proposal("P1")], arguments=[]
    )
    assert result.status == "UNRESOLVED"
    assert result.validation_warnings
    assert all(isinstance(x, dict) for x in result.considered_alternatives)
    assert all(x.get("status") != "REJECTED" for x in result.considered_alternatives)


def test_b4_ruling_direction_is_independent_per_proposal():
    proposals = [_proposal("P1"), _proposal("P2")]
    quote = "I reject P1; P2 remains open."
    assert _verify_ruling_alternative_link(quote, quote, "P1", proposals)
    assert not _verify_ruling_alternative_link(quote, quote, "P2", proposals)
    assert not _verify_ruling_alternative_link(
        "I object to rejecting P1.", "I object to rejecting P1.", "P1", proposals
    )
    assert not _verify_ruling_alternative_link(
        "I do not reject P2.", "I do not reject P2.", "P2", proposals
    )


def test_b4_mixed_ruling_does_not_reject_open_proposal():
    quote = "I reject P1; P2 remains open."
    messages = [{
        "message_id": "m1",
        "source_message_version_id": uuid4(),
        "author": "guido",
        "author_role": "bdfl",
        "raw_content": quote,
    }]
    client = _client(
        [_proposal("P1").model_dump(), _proposal("P2").model_dump()],
        [{
            "argument_id": "A1", "proposal_id": "P1", "author": "guido",
            "author_role": "bdfl", "stance": "OBJECT", "is_ruling": True,
            "ruling_authority": "BDFL", "fatal_rejection": True,
            "quote_text": quote,
        }],
        [{"proposal_id": "P1", "status": "REJECTED"}, {"proposal_id": "P2", "status": "REJECTED"}],
    )
    result = ExtractionPipeline(client).run(
        thread_id="mixed", topic="Decision", messages=messages,
        alternative_ids={"P1": uuid4(), "P2": uuid4()},
        verified_project_roles={"guido": "bdfl"},
    )
    statuses = {a["proposal_id"]: a["status"] for a in result.madr.considered_alternatives}
    assert statuses == {"P1": "REJECTED", "P2": "DISPUTED"}


def test_b5_evidence_requires_exactly_one_target():
    kwargs = {
        "source_message_version_id": uuid4(),
        "exact_quote": "exact",
        "start_char": 0,
        "end_char": 5,
        "evidence_role": EvidenceRole.PROPOSAL,
        "stance": EvidenceStance.SUPPORT,
    }
    with pytest.raises(ValidationError):
        EvidenceSpan(**kwargs)
    with pytest.raises(ValidationError):
        EvidenceSpan(**kwargs, decision_id=uuid4(), alternative_id=uuid4())
    span = EvidenceSpan(**kwargs, decision_id=uuid4())
    assert EvidenceVerifier.verify_span(span, "exact")[0] is True


def test_authority_requires_verified_roles_and_does_not_leak_into_arguments():
    quote = "I reject P1."
    messages = [{
        "message_id": "m1", "source_message_version_id": uuid4(),
        "author": "alice", "author_role": "bdfl", "raw_content": quote,
    }]
    client = _client(
        [_proposal("P1").model_dump()],
        [{
            "argument_id": "A1", "proposal_id": "P1", "author": "alice",
            "author_role": "bdfl", "stance": "OBJECT", "is_ruling": True,
            "ruling_authority": "BDFL", "fatal_rejection": True, "quote_text": quote,
        }],
        [{"proposal_id": "P1", "status": "REJECTED"}],
    )
    pipeline = ExtractionPipeline(client)
    for grants in (None, {"alice": "contributor"}):
        result = pipeline.run(
            "authority", "Decision", messages,
            alternative_ids={"P1": uuid4()}, verified_project_roles=grants,
        )
        assert result.madr.status == "DISPUTED"
        assert result.arguments
        assert all(not a.is_ruling and a.ruling_authority is None for a in result.arguments)
        assert all(not a.fatal_rejection for a in result.arguments)

    trusted = pipeline.run(
        "authority", "Decision", messages,
        alternative_ids={"P1": uuid4()}, verified_project_roles={"alice": "bdfl"},
    )
    assert trusted.arguments[0].is_ruling is True
    assert trusted.madr.status == "REJECTED"


def test_consensus_signoff_requires_affirmative_agreement():
    p = [_proposal("P1")]
    assert _is_affirmative_signoff("I concur with rejecting P1.", "P1", p)
    assert not _is_affirmative_signoff("I object to rejecting P1.", "P1", p)
    assert not _is_affirmative_signoff("I reject P1 myself.", "P1", p)

    alice_quote = "I reject P1."
    bob_quote = "I object to rejecting P1."
    messages = [
        {
            "message_id": "alice-message", "source_message_version_id": uuid4(),
            "author": "alice", "author_role": "core maintainer", "raw_content": alice_quote,
        },
        {
            "message_id": "bob-message", "source_message_version_id": uuid4(),
            "author": "bob", "author_role": "core maintainer", "raw_content": bob_quote,
        },
    ]
    args = [
        {
            "argument_id": f"A{i}", "proposal_id": "P1", "author": author,
            "author_role": "core maintainer", "stance": "OBJECT",
            "is_ruling": True, "ruling_authority": "CORE_MAINTAINER_CONSENSUS",
            "fatal_rejection": True, "quote_text": quote,
        }
        for i, (author, quote) in enumerate(
            (("alice", alice_quote), ("bob", bob_quote)), start=1
        )
    ]
    client = _client(
        [_proposal("P1").model_dump()], args,
        [{"proposal_id": "P1", "status": "REJECTED"}],
    )
    result = ExtractionPipeline(client).run(
        "disagreement", "Decision", messages,
        alternative_ids={"P1": uuid4()},
        verified_project_roles={"alice": "core maintainer", "bob": "core maintainer"},
    )
    assert result.madr.status == "DISPUTED"
    assert result.madr.considered_alternatives[0]["status"] == "DISPUTED"
