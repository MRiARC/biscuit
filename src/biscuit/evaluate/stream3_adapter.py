"""Contract adapter for Stream 3 extraction without exposing benchmark answers.

This module intentionally has no import of the extraction branch: callers inject a
configured ExtractionPipeline instance after Stream 3 is available. An adapter result
is an independent prediction, not a replay of held-out fixture labels.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any


def stream3_predictor(
    pipeline: Any,
    *,
    grounder: Callable[[Any, dict[str, Any]], list[dict[str, Any]]] | None = None,
    watchdog: Callable[[Any, list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    trusted_project_roles: Mapping[str, str] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Convert a Stream 3 run to the Stream 5 prediction schema.

    Unavailable grounding, embedding and watchdog inputs stay absent. No hidden
    truth, fixture labels, synthetic warnings, or authorization keys are invented.
    Source version UUIDs must have come from ingestion, never generated here.
    """

    def predict(source: dict[str, Any]) -> dict[str, Any]:
        messages = [
            {
                "message_id": row["message_id"],
                "author": row["author_handle"],
                "author_role": "contributor",  # Do not trust source-asserted authority.
                "content": row["raw_text"],
                "raw_content": row["raw_text"],
                **(
                    {"source_message_version_id": row["source_message_version_id"]}
                    if row.get("source_message_version_id") else {}
                ),
            }
            for row in source["messages"]
        ]
        version_to_message = {
            str(row["source_message_version_id"]): str(row["message_id"])
            for row in source["messages"] if row.get("source_message_version_id")
        }
        run = pipeline.run(
            thread_id=str(source["thread_id"]),
            topic=str(source.get("topic") or source["thread_id"]),
            messages=messages,
            edges=[],
            verified_project_roles=(
                dict(trusted_project_roles) if trusted_project_roles is not None else None
            ),
        )
        if getattr(run, "run_status", "SUCCESS") != "SUCCESS":
            raise ValueError("Stream 3 extraction did not complete successfully")

        status_by_id = {
            str(row["proposal_id"]): str(row["status"]).upper()
            for row in run.madr.considered_alternatives
            if "proposal_id" in row and "status" in row
        }
        proposals = []
        for proposal in run.proposals:
            pid = str(proposal.proposal_id)
            item: dict[str, Any] = {"proposal_id": pid, "name": str(proposal.name)}
            if pid in status_by_id:
                item["status"] = status_by_id[pid]
            if grounder is not None:
                anchors = grounder(proposal, source)
                if not isinstance(anchors, list):
                    raise TypeError("Grounder must return a list of historical anchors")
                item["code_anchors"] = anchors
            # Summaries are not substituted for independently extracted rationale.
            proposals.append(item)

        evidence = []
        for span in run.evidence_spans:
            version_id = str(span.source_message_version_id)
            message_id = version_to_message.get(version_id)
            if message_id is None:
                # The evaluator must not silently claim an untraceable quote.
                evidence.append({
                    "message_id": "UNVERIFIABLE_SOURCE_VERSION:" + version_id,
                    "start_char": span.start_char, "end_char": span.end_char,
                    "quote": span.exact_quote, "sha256": span.content_hash,
                })
            else:
                evidence.append({
                    "message_id": message_id,
                    "start_char": span.start_char, "end_char": span.end_char,
                    "quote": span.exact_quote,
                    "sha256": span.content_hash or hashlib.sha256(
                        span.exact_quote.encode("utf-8")
                    ).hexdigest(),
                })

        warning_records = watchdog(run, source["pr_cases"]) if watchdog else []
        if not isinstance(warning_records, list):
            raise TypeError("Watchdog must return a list of warning records")
        return {
            "proposals": proposals,
            "evidence": evidence,
            "warning_records": warning_records,
            "warned_pr_ids": [str(record["case_id"]) for record in warning_records],
            "prediction_origin": "STREAM3_PIPELINE_NOT_SYNTHETIC_REPLAY",
            "extraction_run_id": str(run.run_id),
        }

    return predict
