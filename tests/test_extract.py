"""
Unit and integration tests for the 4-Pass Local LLM Extraction Pipeline.
Conforms to Issue #19 and Stream 3 requirements.
Runs completely offline in mock mode (BISCUIT_MOCK_LLM=1) with zero cloud dependencies.
"""

import json
from uuid import uuid4

import pytest

from biscuit.extract.client import LocalLLMClient
from biscuit.extract.pass1_discovery import Pass1ProposalDiscovery
from biscuit.extract.pass2_slicing import Pass2DeliberationSlicing
from biscuit.extract.pass3_arguments import Pass3ArgumentMining
from biscuit.extract.pass4_synthesis import Pass4SchemaSynthesis
from biscuit.extract.pipeline import ExtractionPipeline
from biscuit.extract.prompts import (
    get_prompt_templates_hash,
)
from biscuit.extract.types import (
    ArgumentClassification,
    DeliberationPacket,
    ExtractedMADR,
    ExtractionRunResult,
    ProposalCandidate,
)
from biscuit.provenance.types import (
    EvidenceRole,
    EvidenceStance,
    VerificationStatus,
    compute_sha256,
)


def test_prompt_templates_hash_deterministic():
    """Verify get_prompt_templates_hash produces a canonical 64-char SHA-256 hex string."""
    hash1 = get_prompt_templates_hash()
    hash2 = get_prompt_templates_hash()
    assert isinstance(hash1, str)
    assert len(hash1) == 64
    assert hash1 == hash2


def test_local_llm_client_mock_replay_and_keep_alive():
    """Verify LocalLLMClient operates in mock mode and records keep_alive: 0 calls."""
    client = LocalLLMClient(mock_mode=True)
    client.set_mock_response("llama3.2:3b", '{"proposals": []}')

    resp = client.generate(model="llama3.2:3b", prompt="Hello", keep_alive=0)
    assert resp == '{"proposals": []}'
    assert len(client.call_history) == 1
    assert client.call_history[0]["keep_alive"] == 0
    assert client.call_history[0]["model"] == "llama3.2:3b"

    # Verify explicit unload_model
    assert client.unload_model("llama3.2:3b") is True
    assert client.loaded_model is None


def test_pass1_proposal_discovery():
    """Verify Pass 1 extracts candidate proposals and handles markdown fences."""
    raw_json = """```json
    {
      "proposals": [
        {
          "proposal_id": "P1",
          "name": "Unary Safe Navigation (?.)",
          "summary": "Introduce ?. operator for None-coalescing property access.",
          "proponent": "markshannon",
          "syntax_or_api": "a?.b"
        },
        {
          "proposal_id": "P2",
          "name": "Null Coalescing (??)",
          "summary": "Introduce ?? operator returning right side if left is None.",
          "proponent": "gvanrossum",
          "syntax_or_api": "a ?? b"
        }
      ]
    }
    ```"""
    client = LocalLLMClient(mock_mode=True, mock_responses={"llama3.2:3b": raw_json})
    pass1 = Pass1ProposalDiscovery(client=client, model="llama3.2:3b")

    proposals = pass1.discover("Thread discussing Python None-aware operators")
    assert len(proposals) == 2
    assert proposals[0].proposal_id == "P1"
    assert proposals[0].name == "Unary Safe Navigation (?.)"
    assert proposals[0].syntax_or_api == "a?.b"
    assert proposals[1].proposal_id == "P2"


def test_pass1_proposal_discovery_malformed_json():
    """Verify Pass 1 gracefully handles malformed or empty output without crashing."""
    client = LocalLLMClient(
        mock_mode=True, mock_responses={"llama3.2:3b": "I am not returning JSON today!"}
    )
    pass1 = Pass1ProposalDiscovery(client=client, model="llama3.2:3b")

    proposals = pass1.discover("Some thread text")
    assert proposals == []


def test_pass2_deliberation_slicing_deterministic():
    """Verify Pass 2 performs deterministic graph and keyword slicing with word bounding."""
    slicer = Pass2DeliberationSlicing(target_word_limit=50)

    proposals = [
        ProposalCandidate(
            proposal_id="P1", name="Safe Navigation", summary="Add ?. operator", syntax_or_api="?."
        )
    ]

    messages = [
        {
            "message_id": "m1",
            "author": "alice",
            "content": "Let's propose ?. safe navigation for Python.",
        },
        {
            "message_id": "m2",
            "author": "bob",
            "content": "I object to ?. because it complicates grammar.",
        },
        {
            "message_id": "m3",
            "author": "charlie",
            "content": "Completely unrelated message about packaging.",
        },
    ]

    edges = [{"source_message_id": "m2", "target_message_id": "m1", "edge_type": "OBJECTS"}]

    packets = slicer.slice_deliberation(messages=messages, proposals=proposals, edges=edges)
    assert len(packets) == 1
    pkt = packets[0]
    assert pkt.proposal_id == "P1"
    assert "m1" in pkt.message_ids
    assert "m2" in pkt.message_ids
    assert pkt.word_count > 0


def test_pass3_argument_mining():
    """Verify Pass 3 extracts arguments and separates objections from rulings."""
    raw_json = """{
      "arguments": [
        {
          "argument_id": "ARG-1",
          "proposal_id": "P1",
          "author": "gvanrossum",
          "author_role": "bdfl",
          "stance": "OBJECT",
          "fatal_rejection": true,
          "fatal_reason": "Unary ? collides with ternary operator syntax",
          "rejection_category": "SYNTAX_AMBIGUITY",
          "is_ruling": true,
          "ruling_authority": "BDFL",
          "quote_text": "I am rejecting the unary ? proposal due to ternary collision."
        },
        {
          "argument_id": "ARG-2",
          "proposal_id": "P1",
          "author": "contributor_steve",
          "author_role": "contributor",
          "stance": "OBJECT",
          "fatal_rejection": false,
          "fatal_reason": null,
          "rejection_category": "COMPLEXITY",
          "is_ruling": false,
          "ruling_authority": null,
          "quote_text": "I dislike the extra punctuation."
        }
      ]
    }"""
    client = LocalLLMClient(mock_mode=True, mock_responses={"phi3.5": raw_json})
    pass3 = Pass3ArgumentMining(client=client, model="phi3.5")

    packet = DeliberationPacket(
        proposal_id="P1",
        proposal_name="Unary Safe Navigation",
        message_ids=["m1"],
        messages=[
            {
                "message_id": "m1",
                "author": "gvanrossum",
                "author_role": "bdfl",
                "content": "I am rejecting the unary ? proposal due to ternary collision.",
            }
        ],
    )

    args = pass3.mine_arguments(packet)
    assert len(args) == 2
    assert args[0].is_ruling is True
    assert args[0].ruling_authority == "BDFL"
    assert args[0].fatal_rejection is True
    assert args[1].is_ruling is False
    assert args[1].fatal_rejection is False


def test_pass3_argument_mining_model_inferred_invariant():
    """
    NON-NEGOTIABLE INVARIANT TEST:
    MODEL_INFERRED can NEVER act as an authoritative ruling authority.
    Direct Pydantic instantiation must raise ValueError.
    Pass3ArgumentMining must strip MODEL_INFERRED if emitted by the LLM.
    """
    # 1. Direct validation failure test
    with pytest.raises(
        ValueError, match="MODEL_INFERRED can never act as an authoritative ruling authority"
    ):
        ArgumentClassification(
            argument_id="ARG-BAD",
            proposal_id="P1",
            author="ai_assistant",
            author_role="bot",
            stance=EvidenceStance.OBJECT,
            fatal_rejection=True,
            is_ruling=True,
            ruling_authority="MODEL_INFERRED",
            quote_text="I decided this proposal is rejected.",
        )

    # 2. Pipeline sanitization test: Pass 3 strips MODEL_INFERRED from LLM output
    llm_output_with_violation = """{
      "arguments": [
        {
          "argument_id": "ARG-LLM",
          "proposal_id": "P1",
          "author": "bot",
          "author_role": "contributor",
          "stance": "OBJECT",
          "fatal_rejection": true,
          "is_ruling": true,
          "ruling_authority": "MODEL_INFERRED",
          "quote_text": "I think this should be rejected."
        }
      ]
    }"""
    client = LocalLLMClient(mock_mode=True, mock_responses={"phi3.5": llm_output_with_violation})
    pass3 = Pass3ArgumentMining(client=client, model="phi3.5")

    packet = DeliberationPacket(
        proposal_id="P1",
        proposal_name="Test",
        message_ids=["m1"],
        messages=[
            {"message_id": "m1", "author": "bot", "content": "I think this should be rejected."}
        ],
    )

    cleaned_args = pass3.mine_arguments(packet)
    assert len(cleaned_args) == 1
    # Authority invariant must be enforced:
    assert cleaned_args[0].ruling_authority is None
    assert cleaned_args[0].is_ruling is False
    assert cleaned_args[0].fatal_rejection is False


def test_pass4_schema_synthesis():
    """Verify Pass 4 produces valid ExtractedMADR structure."""
    raw_json = """{
      "decision_id": "DEC-PEP505",
      "title": "PEP 505: None-aware operators",
      "status": "REJECTED",
      "context_and_problem": "Deliberation over introducing ?., ??, and ?[] into Python syntax.",
      "considered_alternatives": [
        {
          "proposal_id": "P1",
          "name": "Unary Safe Navigation",
          "status": "REJECTED",
          "rejection_reason": "Syntax ambiguity with ternary operator"
        }
      ],
      "decision_outcome": {
        "chosen_proposal_id": null,
        "rationale": "Rejected by BDFL",
        "constraints": "Syntax freeze on question mark operators"
      },
      "evidence_quotes": [
        {
          "quote_text": "I am rejecting this proposal.",
          "author": "gvanrossum",
          "role": "RULING",
          "message_index": 1
        }
      ]
    }"""
    client = LocalLLMClient(mock_mode=True, mock_responses={"qwen2.5-coder:7b": raw_json})
    pass4 = Pass4SchemaSynthesis(client=client, model="qwen2.5-coder:7b")

    proposals = [
        ProposalCandidate(proposal_id="P1", name="Unary Safe Navigation", summary="?. operator")
    ]
    arguments = [
        ArgumentClassification(
            argument_id="ARG-1",
            proposal_id="P1",
            author="gvanrossum",
            author_role="bdfl",
            stance=EvidenceStance.OBJECT,
            is_ruling=True,
            ruling_authority="BDFL",
            quote_text="I am rejecting this proposal.",
        )
    ]

    madr = pass4.synthesize_madr(
        topic="PEP 505: None-aware operators",
        decision_id="DEC-PEP505",
        proposals=proposals,
        arguments=arguments,
    )

    assert isinstance(madr, ExtractedMADR)
    assert madr.decision_id == "DEC-PEP505"
    assert madr.status == "REJECTED"
    assert len(madr.considered_alternatives) == 1
    assert len(madr.evidence_quotes) == 1


def test_extraction_pipeline_end_to_end_with_exact_verification():
    """
    End-to-end integration test:
    Simulates a full 4-pass run on a synthetic PEP 505 deliberation thread.
    Verifies that extracted quotes are located and promoted to VERIFIED_EXACT
    with byte-for-byte substring equality and SHA-256 match.
    """
    thread_id = "thread-pep-505"
    topic = "PEP 505 -- None-aware operators"

    # Verbatim source messages with exact substrings
    quote1 = "I am rejecting the unary ? proposal due to ternary collision."
    quote2 = "Syntax ? adds unneeded ambiguity to expression evaluation."

    msg1_content = f"Hello python-ideas. {quote2} We should discuss alternatives."
    msg2_content = f"After reviewing community feedback: {quote1} Thanks, Guido."

    v1_id = uuid4()
    v2_id = uuid4()
    messages = [
        {
            "message_id": "msg-001",
            "source_message_version_id": v1_id,
            "author": "steve_dower",
            "author_role": "contributor",
            "raw_content": msg1_content,
        },
        {
            "message_id": "msg-002",
            "source_message_version_id": v2_id,
            "author": "gvanrossum",
            "author_role": "bdfl",
            "raw_content": msg2_content,
        },
    ]

    # Mock responses for Pass 1, Pass 3, and Pass 4
    pass1_resp = json.dumps(
        {
            "proposals": [
                {
                    "proposal_id": "P1",
                    "name": "Unary Safe Navigation (?.)",
                    "summary": "Introduce ?. operator",
                    "proponent": "steve_dower",
                    "syntax_or_api": "?.",
                }
            ]
        }
    )

    pass3_resp = json.dumps(
        {
            "arguments": [
                {
                    "argument_id": "ARG-1",
                    "proposal_id": "P1",
                    "author": "gvanrossum",
                    "author_role": "bdfl",
                    "stance": "OBJECT",
                    "fatal_rejection": True,
                    "fatal_reason": "Ternary operator collision",
                    "rejection_category": "SYNTAX_AMBIGUITY",
                    "is_ruling": True,
                    "ruling_authority": "BDFL",
                    "quote_text": quote1,
                }
            ]
        }
    )

    pass4_resp = json.dumps(
        {
            "decision_id": "DEC-PEP505",
            "title": topic,
            "status": "REJECTED",
            "context_and_problem": "Deliberation on None-aware operators for Python.",
            "considered_alternatives": [
                {"proposal_id": "P1", "name": "Unary Safe Navigation (?.)", "status": "REJECTED"}
            ],
            "decision_outcome": {"chosen_proposal_id": None, "rationale": "Rejected by BDFL"},
            "evidence_quotes": [
                {"quote_text": quote2, "author": "steve_dower", "role": "OBJECTION"}
            ],
        }
    )

    client = LocalLLMClient(
        mock_mode=True,
        mock_responses={
            "llama3.2:3b": pass1_resp,
            "phi3.5": pass3_resp,
            "qwen2.5-coder:7b": pass4_resp,
        },
    )

    pipeline = ExtractionPipeline(client=client)
    result: ExtractionRunResult = pipeline.run(
        thread_id=thread_id, topic=topic, messages=messages, decision_id="DEC-PEP505"
    )

    # 1. Assert overall pipeline execution status
    assert result.run_status == "SUCCESS"
    assert len(result.run_id) > 0
    assert result.thread_id == thread_id
    assert len(result.prompt_templates_hash) == 64

    # 2. Assert Pass outputs
    assert len(result.proposals) == 1
    assert result.proposals[0].proposal_id == "P1"

    assert len(result.deliberation_packets) == 1
    assert result.deliberation_packets[0].proposal_id == "P1"

    assert len(result.arguments) == 1
    assert result.arguments[0].is_ruling is True
    assert result.arguments[0].ruling_authority == "BDFL"

    assert result.madr.status == "REJECTED"
    assert result.madr.decision_id == "DEC-PEP505"

    # 3. Assert Exact Evidence Verification
    assert len(result.evidence_spans) == 2
    assert len(result.verified_spans) == 2

    # Check that both spans were promoted to VERIFIED_EXACT
    for v_span in result.verified_spans:
        assert v_span.verification_status == VerificationStatus.VERIFIED_EXACT
        assert v_span.content_hash == compute_sha256(v_span.exact_quote)

    # Check specific quote offsets
    ruling_span = next(s for s in result.verified_spans if s.exact_quote == quote1)
    assert ruling_span.start_char == msg2_content.find(quote1)
    assert ruling_span.end_char == ruling_span.start_char + len(quote1)
    assert msg2_content[ruling_span.start_char : ruling_span.end_char] == quote1

    # 4. Assert Audit Metadata
    audit = result.audit_metadata
    assert audit["pipeline_version"] == "1.0.0"
    assert audit["total_messages"] == 2
    assert audit["total_proposals"] == 1
    assert audit["verified_spans_count"] == 2


def test_supportive_and_neutral_arguments_role_validity():
    """
    Regression Test 1 (Reviewer Issue 1):
    Ensure supportive and neutral arguments do not crash looking for EvidenceRole.CONTEXT.
    They must be assigned valid EvidenceRole enum members.
    """
    thread_id = "thread-support"
    topic = "Memory bounding in streaming"
    quote_support = "Pre-allocating circular buffers reduces allocation spikes by 40%."
    quote_neutral = "We should benchmark chunk sizes between 8KB and 64KB."

    msg_content = f"Proposal analysis: {quote_support} Note that {quote_neutral}"
    version_id = uuid4()
    messages = [
        {
            "message_id": "msg-101",
            "source_message_version_id": version_id,
            "author": "perf_lead",
            "author_role": "contributor",
            "raw_content": msg_content,
        }
    ]

    pass1_resp = json.dumps(
        {
            "proposals": [
                {
                    "proposal_id": "P1",
                    "name": "Circular Buffer Bounding",
                    "summary": "Bounded circular buffers",
                    "proponent": "perf_lead",
                    "syntax_or_api": "CircularBuffer(capacity=64KB)",
                }
            ]
        }
    )

    pass3_resp = json.dumps(
        {
            "arguments": [
                {
                    "argument_id": "ARG-SUP",
                    "proposal_id": "P1",
                    "author": "perf_lead",
                    "author_role": "contributor",
                    "stance": "SUPPORT",
                    "fatal_rejection": False,
                    "is_ruling": False,
                    "ruling_authority": None,
                    "quote_text": quote_support,
                },
                {
                    "argument_id": "ARG-NEU",
                    "proposal_id": "P1",
                    "author": "perf_lead",
                    "author_role": "contributor",
                    "stance": "NEUTRAL",
                    "fatal_rejection": False,
                    "is_ruling": False,
                    "ruling_authority": None,
                    "quote_text": quote_neutral,
                },
            ]
        }
    )

    pass4_resp = json.dumps(
        {
            "decision_id": "DEC-STREAM",
            "title": topic,
            "status": "UNRESOLVED",
            "context_and_problem": "Streaming buffer bounding",
            "considered_alternatives": [
                {"proposal_id": "P1", "name": "Circular Buffer Bounding", "status": "DISPUTED"}
            ],
            "evidence_quotes": [],
        }
    )

    client = LocalLLMClient(
        mock_mode=True,
        mock_responses={
            "llama3.2:3b": pass1_resp,
            "phi3.5": pass3_resp,
            "qwen2.5-coder:7b": pass4_resp,
        },
    )

    pipeline = ExtractionPipeline(client=client)
    result = pipeline.run(thread_id=thread_id, topic=topic, messages=messages)

    assert result.run_status == "SUCCESS"
    assert len(result.verified_spans) == 2
    for span in result.verified_spans:
        # Check valid EvidenceRole (must be in enum, not CONTEXT)
        assert span.evidence_role in (EvidenceRole.PROPOSAL, EvidenceRole.OBJECTION)
        # Check single target binding
        assert span.argument_id is not None
        assert span.decision_id is None
        assert span.alternative_id is None


def test_hallucinated_rejected_status_downgraded_to_disputed():
    """
    Regression Test 2 (Reviewer Issue 2):
    If Pass 4 outputs REJECTED but there is no VERIFIED_EXACT human ruling
    authored by an authorized maintainer/BDFL, the status MUST be downgraded to DISPUTED.
    """
    thread_id = "thread-hallucinated"
    topic = "Syntax extension"
    quote_obj = "I really dislike question marks in Python."
    msg_content = f"Community feedback: {quote_obj}"

    # Author is only a contributor (not BDFL / maintainer)
    messages = [
        {
            "message_id": "msg-201",
            "source_message_version_id": uuid4(),
            "author": "random_contributor",
            "author_role": "contributor",
            "raw_content": msg_content,
        }
    ]

    pass1_resp = json.dumps(
        {
            "proposals": [
                {
                    "proposal_id": "P1",
                    "name": "Question Mark Syntax",
                    "summary": "Syntax proposal",
                    "proponent": "someone",
                    "syntax_or_api": "?",
                }
            ]
        }
    )

    # Pass 3 classifies as contributor objection
    pass3_resp = json.dumps(
        {
            "arguments": [
                {
                    "argument_id": "ARG-CONTRIB",
                    "proposal_id": "P1",
                    "author": "random_contributor",
                    "author_role": "contributor",
                    "stance": "OBJECT",
                    "fatal_rejection": False,
                    "is_ruling": False,
                    "ruling_authority": None,
                    "quote_text": quote_obj,
                }
            ]
        }
    )

    # Pass 4 hallucinates an authoritative REJECTED status
    pass4_resp = json.dumps(
        {
            "decision_id": "DEC-HALLUCINATED",
            "title": topic,
            "status": "REJECTED",
            "context_and_problem": "Syntax change",
            "considered_alternatives": [
                {
                    "proposal_id": "P1",
                    "name": "Question Mark Syntax",
                    "status": "REJECTED",
                    "rejection_reason": "Model decided it was rejected",
                }
            ],
            "evidence_quotes": [],
        }
    )

    client = LocalLLMClient(
        mock_mode=True,
        mock_responses={
            "llama3.2:3b": pass1_resp,
            "phi3.5": pass3_resp,
            "qwen2.5-coder:7b": pass4_resp,
        },
    )

    pipeline = ExtractionPipeline(client=client)
    result = pipeline.run(thread_id=thread_id, topic=topic, messages=messages)

    # Invariant: Must be downgraded to DISPUTED
    assert result.madr.status == "DISPUTED"
    assert result.madr.considered_alternatives[0]["status"] == "DISPUTED"
    assert len(result.audit_metadata["downgraded_rejections"]) > 0


def test_quoted_evidence_attributed_to_original_author_not_reply():
    """
    Regression Test 3 (Reviewer Issue 3 & 4):
    When a contributor replies and blockquotes a maintainer's statement,
    the quote must be attributed to the original maintainer, NOT to the quoting contributor.
    """
    guido_quote = "I am rejecting this PEP due to grammatical ambiguity."
    msg1_content = f"Decision summary: {guido_quote} Regards, Guido."
    msg2_content = f"> {guido_quote}\nI disagree with Guido's decision, we should reconsider!"

    packet = DeliberationPacket(
        proposal_id="P1",
        proposal_name="Grammar change",
        message_ids=["msg-guido", "msg-bob"],
        messages=[
            {
                "message_id": "msg-guido",
                "author": "gvanrossum",
                "author_role": "bdfl",
                "content": msg1_content,
            },
            {
                "message_id": "msg-bob",
                "author": "bob_contributor",
                "author_role": "contributor",
                "content": msg2_content,
            },
        ],
    )

    # Model returned the quote with author claimed as Guido
    pass3_resp = json.dumps(
        {
            "arguments": [
                {
                    "argument_id": "ARG-RULING",
                    "proposal_id": "P1",
                    "author": "gvanrossum",
                    "author_role": "bdfl",
                    "stance": "OBJECT",
                    "fatal_rejection": True,
                    "is_ruling": True,
                    "ruling_authority": "BDFL",
                    "quote_text": guido_quote,
                }
            ]
        }
    )

    client = LocalLLMClient(mock_mode=True, mock_responses={"phi3.5": pass3_resp})
    pass3 = Pass3ArgumentMining(client=client, model="phi3.5")

    args = pass3.mine_arguments(packet)
    assert len(args) == 1
    # Verify attribution: Guido is the author, NOT Bob
    assert args[0].author == "gvanrossum"
    assert args[0].author_role == "bdfl"
    assert args[0].is_ruling is True
    assert args[0].ruling_authority == "BDFL"


def test_missing_source_message_version_ids_skips_span_creation():
    """
    Regression Test 4 (Reviewer Issue 5):
    When source_message_version_id is absent or not a valid UUID,
    the pipeline must NOT fabricate synthetic UUIDs.
    """
    thread_id = "thread-no-version"
    topic = "Unpersisted message test"
    quote = "Some unpersisted quote."

    # Missing source_message_version_id
    messages = [
        {
            "message_id": "msg-unpersisted",
            "author": "alice",
            "author_role": "contributor",
            "raw_content": f"Here is {quote}",
        }
    ]

    pass1_resp = json.dumps(
        {"proposals": [{"proposal_id": "P1", "name": "P1", "summary": "P1", "syntax_or_api": "x"}]}
    )
    pass3_resp = json.dumps(
        {
            "arguments": [
                {
                    "argument_id": "ARG-1",
                    "proposal_id": "P1",
                    "author": "alice",
                    "author_role": "contributor",
                    "stance": "OBJECT",
                    "quote_text": quote,
                }
            ]
        }
    )
    pass4_resp = json.dumps(
        {
            "decision_id": "DEC-TEST",
            "title": topic,
            "status": "UNRESOLVED",
            "considered_alternatives": [],
            "evidence_quotes": [],
        }
    )

    client = LocalLLMClient(
        mock_mode=True,
        mock_responses={
            "llama3.2:3b": pass1_resp,
            "phi3.5": pass3_resp,
            "qwen2.5-coder:7b": pass4_resp,
        },
    )

    pipeline = ExtractionPipeline(client=client)
    result = pipeline.run(thread_id=thread_id, topic=topic, messages=messages)

    # Evidence spans must NOT be fabricated
    assert len(result.evidence_spans) == 0
    assert len(result.verified_spans) == 0
    # Audit metadata should record warnings about unanchored evidence
    assert len(result.audit_metadata["unanchored_warnings"]) > 0


def test_slicer_preserves_late_maintainer_ruling_in_long_thread():
    """
    Regression Test 5 (Reviewer Issue 6):
    When a thread contains extensive debate that exceeds target_word_limit,
    the slicer must not break early and discard the late maintainer ruling.
    """
    slicer = Pass2DeliberationSlicing(target_word_limit=300)

    proposal = ProposalCandidate(
        proposal_id="P1",
        name="Async Streaming",
        summary="Introduce async streaming response",
        syntax_or_api="AsyncStreaming()",
    )

    messages = []
    # Seed message introducing the proposal
    messages.append(
        {
            "message_id": "msg-000",
            "author": "alice",
            "author_role": "contributor",
            "content": "Proposal P1: Async Streaming syntax AsyncStreaming() for large workloads.",
        }
    )

    # Intermediate long contributor chatter (20 messages of 40 words each = 800 words)
    for i in range(1, 21):
        messages.append(
            {
                "message_id": f"msg-{i:03d}",
                "author": f"contributor_{i}",
                "author_role": "contributor",
                "content": f"Discussion turn {i}: We are debating buffer bounds and socket allocation details in depth.",
            }
        )

    # Final BDFL ruling at the very end of the thread
    ruling_quote = "I am officially rejecting proposal P1 due to unbounded memory overhead."
    messages.append(
        {
            "message_id": "msg-099",
            "author": "guido",
            "author_role": "bdfl",
            "content": f"Final determination: {ruling_quote}",
        }
    )

    packets = slicer.slice_deliberation(messages=messages, proposals=[proposal])
    assert len(packets) == 1
    packet = packets[0]

    # Verify both opening seed and closing BDFL ruling are preserved
    assert "msg-000" in packet.message_ids
    assert "msg-099" in packet.message_ids
    # Verify the late ruling content is present in packet messages
    assert any("rejecting proposal P1" in m["content"] for m in packet.messages)
