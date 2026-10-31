"""
Biscuit Extract Module: 4-Pass Local LLM Extraction Pipeline.
"""

from .client import LocalLLMClient
from .pass1_discovery import Pass1ProposalDiscovery
from .pass2_slicing import Pass2DeliberationSlicing
from .pass3_arguments import Pass3ArgumentMining
from .pass4_synthesis import Pass4SchemaSynthesis
from .pipeline import ExtractionPipeline
from .prompts import (
    PASS1_PROPOSAL_DISCOVERY_PROMPT,
    PASS3_ARGUMENT_MINING_PROMPT,
    PASS4_SCHEMA_SYNTHESIS_PROMPT,
    get_prompt_templates_hash,
)
from .types import (
    ArgumentClassification,
    DeliberationPacket,
    ExtractedMADR,
    ExtractionRunResult,
    ProposalCandidate,
)

__all__ = [
    "PASS1_PROPOSAL_DISCOVERY_PROMPT",
    "PASS3_ARGUMENT_MINING_PROMPT",
    "PASS4_SCHEMA_SYNTHESIS_PROMPT",
    "ArgumentClassification",
    "DeliberationPacket",
    "ExtractedMADR",
    "ExtractionPipeline",
    "ExtractionRunResult",
    "LocalLLMClient",
    "Pass1ProposalDiscovery",
    "Pass2DeliberationSlicing",
    "Pass3ArgumentMining",
    "Pass4SchemaSynthesis",
    "ProposalCandidate",
    "get_prompt_templates_hash",
]
