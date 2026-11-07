"""
Prompt templates for the 4-Pass Local LLM Extraction Pipeline.
Maintains deterministic prompt template hashing for auditability.
"""

import hashlib

PASS1_PROPOSAL_DISCOVERY_PROMPT = """You are analyzing an open-source technical discussion to discover candidate proposals.
Analyze the following thread messages and extract all distinct technical proposals, alternative syntaxes, or architectural approaches discussed.
Ignore greetings, administrative banter, and unrelated off-topic comments.

Thread Content:
{thread_text}

Respond ONLY with valid JSON in this schema:
{{
  "proposals": [
    {{
      "proposal_id": "P1",
      "name": "Short Technical Name",
      "summary": "Technical explanation of what was proposed",
      "proponent": "handle_or_name",
      "syntax_or_api": "relevant code/flag or null"
    }}
  ]
}}
"""

PASS3_ARGUMENT_MINING_PROMPT = """You are a senior compiler and software architect evaluating a deliberation packet for:
Proposal: {proposal_name} (ID: {proposal_id})

Deliberation Packet:
{packet_text}

Analyze the comments critiquing this proposal.
Differentiate between:
1. Individual contributor objections (stance: OBJECT, fatal_rejection: false, is_ruling: false)
2. Authoritative maintainer rulings (is_ruling: true, ruling_authority: BDFL or CORE_MAINTAINER_CONSENSUS)

CRITICAL INVARIANT: You are a model and cannot act as a ruling authority yourself. Do NOT output ruling_authority='MODEL_INFERRED'.
If there is no consensus ruling signed by an identified maintainer, mark is_ruling=false.

Respond ONLY with valid JSON in this schema:
{{
  "arguments": [
    {{
      "argument_id": "ARG-1",
      "proposal_id": "{proposal_id}",
      "author": "author_handle",
      "author_role": "maintainer or contributor",
      "stance": "SUPPORT or OBJECT or NEUTRAL",
      "fatal_rejection": true or false,
      "fatal_reason": "Specific technical failure (e.g. deadlock, OOM, ABI break) or null",
      "rejection_category": "PERFORMANCE or SYNTAX_AMBIGUITY or BACKWARD_COMPAT or SECURITY or COMPLEXITY or null",
      "is_ruling": true or false,
      "ruling_authority": "BDFL or CORE_MAINTAINER_CONSENSUS or null",
      "quote_text": "Exact verbatim quote from the message containing the argument"
    }}
  ]
}}
"""

PASS4_SCHEMA_SYNTHESIS_PROMPT = """You are synthesizing an Architectural Decision Record (MADR 3.0) from analyzed proposals and arguments.

Topic: {topic}
Proposals:
{proposals_json}

Arguments & Rulings:
{arguments_json}

Synthesize the formal MADR 3.0 document.
Extract all verifiable evidence quotes and cite the exact message text.

Respond ONLY with valid JSON in this schema:
{{
  "decision_id": "{decision_id}",
  "title": "{topic}",
  "status": "ACCEPTED or REJECTED or DISPUTED or UNRESOLVED",
  "context_and_problem": "Technical problem statement and context",
  "considered_alternatives": [
    {{
      "proposal_id": "P1",
      "name": "...",
      "status": "REJECTED or ACCEPTED or DISPUTED",
      "rejection_reason": "...",
      "fatal_objection_by": "..."
    }}
  ],
  "decision_outcome": {{
    "chosen_proposal_id": "P3",
    "rationale": "Why this approach was chosen",
    "constraints": "Future constraints or caveats"
  }},
  "evidence_quotes": [
    {{
      "quote_text": "Exact verbatim quote",
      "author": "...",
      "role": "RULING or OBJECTION or PROPOSAL",
      "message_index": 1
    }}
  ]
}}
"""

ALL_PROMPT_TEMPLATES = [
    ("PASS1_PROPOSAL_DISCOVERY_PROMPT", PASS1_PROPOSAL_DISCOVERY_PROMPT),
    ("PASS3_ARGUMENT_MINING_PROMPT", PASS3_ARGUMENT_MINING_PROMPT),
    ("PASS4_SCHEMA_SYNTHESIS_PROMPT", PASS4_SCHEMA_SYNTHESIS_PROMPT),
]


def get_prompt_templates_hash() -> str:
    """
    Computes a canonical SHA-256 hash over all extraction prompt templates.
    Ensures prompt versioning and run auditability per Section 7 contract.
    """
    hasher = hashlib.sha256()
    for name, template in sorted(ALL_PROMPT_TEMPLATES, key=lambda x: x[0]):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"::")
        hasher.update(template.strip().encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()
