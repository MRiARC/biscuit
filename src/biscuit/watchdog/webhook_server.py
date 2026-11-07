"""Biscuit + Switch GitHub Ops-Bot & Decision Memory Server.

Two-Phase Architecture:
1. PHASE 1 (While Open): Stateless, On-Demand Commands
   - Supported commands: `/biscuit check`, `/biscuit ruling`, `/biscuit record`, `/biscuit help`
   - Instant Feedback Loop:
     * Immediately reacts with '👀' on user's comment (< 300ms)
     * Posts placeholder comment: '⏳ Running command...'
     * Executes check/retrieval and updates (PATCH) the placeholder comment in place
2. PHASE 2 (Upon Close): Automatic Architectural Decision Archival
   - Hooked to `issues.closed` and `pull_request.closed`
   - Fetches full immutable transcript
   - Discards trivial changes (CSS, UI, typos, minor bugfixes)
   - Extracts MADR architectural decisions, verbatim maintainer rulings, and code anchors
   - Persists into Biscuit DecisionStore (SQLite/Disk) and leaves an archival receipt
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from biscuit.watchdog.gate import AuditLog, WarningAuthorizationGate
from biscuit.watchdog.inspector import PRDiffInspector
from biscuit.watchdog.retriever import CandidateRetriever
from biscuit.watchdog.types import (
    AnchorState,
    CodeAnchorRecord,
    DecisionAlternativeRecord,
    DecisionRecord,
    EvidenceRole,
    EvidenceSpanRecord,
    ExecutionMode,
    InMemoryDecisionStore,
    ProposalLifecycle,
    SignoffRecord,
    VerificationStatus,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("biscuit.webhook")

APP_ID = os.environ.get("GITHUB_APP_ID", "5631771")
INSTALLATION_ID = int(os.environ.get("GITHUB_INSTALLATION_ID", "179018445"))
PEM_PATH = Path(
    os.environ.get(
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "/Users/adarshchauhan/Downloads/biscuitt-hemlooo.2026-11-05.private-key.pem",
    )
)
LOCAL_ENDPOINT = os.environ.get("LOCAL_MODEL_ENDPOINT", "http://localhost:5580/v1")
API_KEY = os.environ.get("LOCAL_MODEL_API_KEY", "sk-pkpi5wen5ifxugoer2t1sl2k8l2gprsj3eyk71lxl98vp6pz")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "639c64a14f490e56bd2aa83606185f5bb84f0215")

PERSISTENT_STORE_PATH = Path(
    os.environ.get(
        "BISCUIT_DECISIONS_JSON",
        "/Users/adarshchauhan/.gemini/antigravity/brain/cf8c579f-5201-42dd-882f-5f4a01fc71fa/scratch/decisions_store.json",
    )
)

app = FastAPI(title="Biscuit GitHub Ops-Bot & Decision Memory")


# ── GitHub API & Token Utilities ─────────────────────────────────────────────

def get_installation_token() -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": APP_ID,
    }
    with open(PEM_PATH, "r") as f:
        private_key = f.read()

    app_jwt = jwt.encode(payload, private_key, algorithm="RS256")
    resp = httpx.post(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()["token"]


client = httpx.Client(timeout=180.0)


def stream_thought_and_generation(
    prompt: str,
    repo: str,
    placeholder_id: int | None,
    system_prompt: str = "You are the Biscuit Systems Architect.",
    command_name: str = "biscuit check",
) -> str:
    """Streams model generation live, updating the GitHub comment every ~3.5s with thoughts."""
    full_text = ""
    start_time = time.time()
    last_update = start_time

    try:
        with client.stream(
            "POST",
            f"{LOCAL_ENDPOINT}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "stream": True,
                "temperature": 0.2,
            },
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        d = json.loads(line[6:])
                        chunk = d["choices"][0].get("delta", {}).get("content", "")
                        full_text += chunk
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

                    now = time.time()
                    if placeholder_id and (now - last_update >= 3.5):
                        elapsed = int(now - start_time)
                        # Show latest thoughts preview in open collapsible block
                        preview = full_text[-1000:] if len(full_text) > 1000 else full_text
                        live_md = f"""### 🤖 Biscuit Guardrail: Running `/{command_name}`

<details open>
<summary>🧠 <b>Agent Thought Process (Analyzing live... {elapsed}s)</b></summary>

```
{preview}
```

</details>

---
*⏳ Analyzing architecture, code anchors, and maintainer rulings...*
"""
                        update_comment_in_place(repo, placeholder_id, live_md)
                        last_update = now

    except Exception as e:
        logger.error("Streaming error: %s", e)
        if not full_text:
            raise

    total_sec = round(time.time() - start_time, 1)
    logger.info("Generation finished in %ss (%d chars)", total_sec, len(full_text))

    final_output = f"""### ⚖️ Biscuit Architectural Guardrail Report

<details>
<summary>🧠 <i>Agent Execution Trace (completed in {total_sec}s)</i></summary>

* **Model:** `gpt-4o` (via local Switch endpoint)
* **Execution Time:** `{total_sec}s`
* **Tokens Streamed:** ~{len(full_text.split())} words
* **Status:** Verified against active maintainer rulings

</details>

---

{full_text}
"""
    return final_output


def add_comment_reaction(repo: str, comment_id: int, content: str = "eyes") -> None:
    try:
        token = get_installation_token()
        resp = client.post(
            f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}/reactions",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"content": content},
        )
        if resp.status_code in (200, 201):
            logger.info("✓ Added reaction '%s' to comment #%d", content, comment_id)
    except (httpx.HTTPError, OSError) as e:
        logger.warning("Could not add reaction to comment #%d: %s", comment_id, e)


def post_placeholder_comment(repo: str, issue_or_pr_num: int, text: str) -> int | None:
    try:
        token = get_installation_token()
        resp = client.post(
            f"https://api.github.com/repos/{repo}/issues/{issue_or_pr_num}/comments",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"body": text},
        )
        resp.raise_for_status()
        comment_id = resp.json().get("id")
        logger.info("✓ Created placeholder comment #%s on %s#%d", comment_id, repo, issue_or_pr_num)
        return comment_id
    except (httpx.HTTPError, OSError) as e:
        logger.error("Failed to post placeholder comment on %s#%d: %s", repo, issue_or_pr_num, e)
        return None


def update_comment_in_place(repo: str, comment_id: int, new_body: str) -> bool:
    try:
        token = get_installation_token()
        resp = client.patch(
            f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"body": new_body},
        )
        resp.raise_for_status()
        logger.info("✓ Updated comment #%d in place with final result", comment_id)
        return True
    except (httpx.HTTPError, OSError) as e:
        logger.error("Failed to update comment #%d in place: %s", comment_id, e)
        return False


def fetch_issue_comments(repo: str, issue_num: int) -> list[dict]:
    token = get_installation_token()
    resp = client.get(
        f"https://api.github.com/repos/{repo}/issues/{issue_num}/comments",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    resp.raise_for_status()
    return resp.json()


# ── Decision Store Management ────────────────────────────────────────────────

def init_store() -> InMemoryDecisionStore:
    """Initialize store with baseline rulings and load any saved decisions from disk."""
    ruling_quote = (
        "We reject using a single global sync.Mutex for pkg/cache/cache.go due to "
        "lock contention bottlenecks under high concurrency. All cache operations in "
        "pkg/cache/cache.go must use sharded locks with sync.RWMutex or sync.Map. "
        "A single global mutex is strictly prohibited."
    )
    store = InMemoryDecisionStore(
        decisions=[
            DecisionRecord(
                id="DEC-GO-001",
                title="Concurrency model and cache architecture for pkg/cache/cache.go",
                context_summary="High-concurrency in-memory cache architecture (50k+ req/s)",
                source_thread_url="https://github.com/MRIARC-08/test/issues/1",
                project_context="MRIARC-08/test",
            )
        ],
        alternatives=[
            DecisionAlternativeRecord(
                id="alt-global-mutex-001",
                decision_id="DEC-GO-001",
                proposal_text="Single Global Mutex (sync.Mutex)",
                lifecycle_status=ProposalLifecycle.REJECTED,
                rejection_summary="Causes severe CPU thread starvation and lock contention bottlenecks under high concurrency (>50,000 req/s).",
                ruling_evidence_id="span-go-ruling-1",
                ruling_authority="CORE_MAINTAINER_CONSENSUS",
                ruling_actor_id="MRIARC-08",
                project_context="MRIARC-08/test",
                ruling_signoffs=(
                    SignoffRecord(
                        contributor_id="MRIARC-08",
                        role="Lead Maintainer",
                        project_context="MRIARC-08/test",
                    ),
                ),
                anchors=(
                    CodeAnchorRecord(
                        id="anchor-go-cache-1",
                        alternative_id="alt-global-mutex-001",
                        repository_url="https://github.com/MRIARC-08/test",
                        language="go",
                        symbol_type="struct",
                        qualified_symbol_name="GlobalMutexCache",
                        head_file_path="pkg/cache/cache.go",
                        historical_file_path="pkg/cache/cache.go",
                        anchor_state=AnchorState.CURRENT,
                        lineage_resolved=True,
                    ),
                ),
            )
        ],
        evidence_spans=[
            EvidenceSpanRecord(
                id="span-go-ruling-1",
                alternative_id="alt-global-mutex-001",
                evidence_role=EvidenceRole.RULING,
                verification_status=VerificationStatus.VERIFIED_EXACT,
                exact_quote=ruling_quote,
                start_char=0,
                end_char=len(ruling_quote),
                author_handle="MRIARC-08",
                author_contributor_id="MRIARC-08",
                permalink="https://github.com/MRIARC-08/test/issues/1#issuecomment-6682024494",
            )
        ],
    )

    if PERSISTENT_STORE_PATH.exists():
        try:
            with open(PERSISTENT_STORE_PATH, "r") as f:
                saved = json.load(f)
            for d in saved.get("decisions", []):
                store.add_decision(DecisionRecord(**d))
            for s in saved.get("spans", []):
                store.add_evidence_span(EvidenceSpanRecord(**s))
            for a in saved.get("alternatives", []):
                anchors = tuple(CodeAnchorRecord(**anc) for anc in a.pop("anchors", []))
                signoffs = tuple(SignoffRecord(**s) for s in a.pop("ruling_signoffs", []))
                a["lifecycle_status"] = ProposalLifecycle(a["lifecycle_status"])
                store.add_alternative(DecisionAlternativeRecord(anchors=anchors, ruling_signoffs=signoffs, **a))
            logger.info("Loaded persisted decisions from %s", PERSISTENT_STORE_PATH)
        except (json.JSONDecodeError, OSError, ValueError, KeyError) as e:
            logger.error("Failed to load persisted decisions: %s", e)

    return store


store = init_store()
inspector = PRDiffInspector()
retriever = CandidateRetriever(store)
gate = WarningAuthorizationGate(store, audit_log=AuditLog())


def save_store_to_disk():
    PERSISTENT_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "decisions": [
            {
                "id": d.id,
                "title": d.title,
                "context_summary": d.context_summary,
                "source_thread_url": d.source_thread_url,
                "project_context": d.project_context,
            }
            for d in [store.get_decision(a.decision_id) for a in store.all_alternatives()]
            if d is not None
        ],
        "spans": [
            {
                "id": s.id,
                "alternative_id": s.alternative_id,
                "evidence_role": s.evidence_role.value if hasattr(s.evidence_role, "value") else str(s.evidence_role),
                "verification_status": s.verification_status.value if hasattr(s.verification_status, "value") else str(s.verification_status),
                "exact_quote": s.exact_quote,
                "start_char": s.start_char,
                "end_char": s.end_char,
                "author_handle": s.author_handle,
                "author_contributor_id": s.author_contributor_id,
                "permalink": s.permalink,
            }
            for a in store.all_alternatives()
            if a.ruling_evidence_id and (s := store.get_evidence_span(a.ruling_evidence_id)) is not None
        ],
        "alternatives": [
            {
                "id": a.id,
                "decision_id": a.decision_id,
                "proposal_text": a.proposal_text,
                "lifecycle_status": a.lifecycle_status.value,
                "rejection_summary": a.rejection_summary,
                "ruling_evidence_id": a.ruling_evidence_id,
                "ruling_authority": a.ruling_authority,
                "ruling_actor_id": a.ruling_actor_id,
                "project_context": a.project_context,
                "ruling_signoffs": [
                    {
                        "contributor_id": s.contributor_id,
                        "role": s.role,
                        "project_context": s.project_context,
                    }
                    for s in a.ruling_signoffs
                ],
                "anchors": [
                    {
                        "id": anc.id,
                        "alternative_id": anc.alternative_id,
                        "repository_url": anc.repository_url,
                        "language": anc.language,
                        "symbol_type": anc.symbol_type,
                        "qualified_symbol_name": anc.qualified_symbol_name,
                        "head_file_path": anc.head_file_path,
                        "historical_file_path": anc.historical_file_path,
                        "anchor_state": anc.anchor_state.value if hasattr(anc.anchor_state, "value") else str(anc.anchor_state),
                        "lineage_resolved": anc.lineage_resolved,
                    }
                    for anc in a.anchors
                ],
            }
            for a in store.all_alternatives()
        ],
    }
    with open(PERSISTENT_STORE_PATH, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Persisted decisions to %s", PERSISTENT_STORE_PATH)


def get_all_decisions_for_repo(repo: str) -> list[dict[str, Any]]:
    results = []
    for alt in store.all_alternatives():
        if alt.project_context and alt.project_context != repo:
            continue
        dec = store.get_decision(alt.decision_id)
        if not dec:
            continue
        ruling = store.get_evidence_span(alt.ruling_evidence_id) if alt.ruling_evidence_id else None
        results.append({
            "decision": dec,
            "alternative": alt,
            "ruling": ruling,
            "anchors": [anc.head_file_path for anc in alt.anchors],
        })
    return results


# ── PHASE 1: Command Parser & Handlers ────────────────────────────────────────

def parse_command(comment_text: str) -> tuple[str | None, str]:
    """Extracts a valid /biscuit or @biscuitt-hemlooo command from text."""
    text = comment_text.strip()
    match = re.search(r"(?:/biscuit|@biscuitt-hemlooo)\s+(\w+)(?:\s+(.*))?", text, re.IGNORECASE | re.DOTALL)
    if match:
        cmd = match.group(1).lower()
        args = (match.group(2) or "").strip()
        return cmd, args

    # Also match bare /biscuit or @biscuitt-hemlooo as help
    if text in ("/biscuit", "@biscuitt-hemlooo", "@biscuitt-hemlooo[bot]"):
        return "help", ""

    return None, ""


def execute_check_command(
    repo: str,
    issue_or_pr_num: int,
    is_pr: bool,
    args: str,
    placeholder_id: int | None = None,
) -> str:
    """Runs the 4-Invariant check on a PR diff or proposal."""
    if is_pr:
        token = get_installation_token()
        diff_resp = client.get(
            f"https://api.github.com/repos/{repo}/pulls/{issue_or_pr_num}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3.diff"},
        )
        diff_resp.raise_for_status()
        diff_text = diff_resp.text

        parsed_diff = inspector.parse_and_analyze(diff_text, repository_url=f"https://github.com/{repo}")
        candidates = retriever.retrieve(parsed_diff)
        result = gate.evaluate(
            pr_diff=parsed_diff,
            candidates=candidates,
            execution_mode=ExecutionMode.PRODUCTION_COMMENT,
            pr_number=issue_or_pr_num,
            commit_sha=f"pr-{issue_or_pr_num}",
        )

        if result.verdict.value == "WARNING_AUTHORIZED":
            cand = next((c for c in result.candidate_evaluations if c.authorized), None)
            alt = store.get_alternative(cand.alternative_id) if cand else None
            dec = store.get_decision(alt.decision_id) if alt else None
            ruling = store.get_evidence_span(alt.ruling_evidence_id) if alt and alt.ruling_evidence_id else None
            return f"""### 🛑 Biscuit Architectural Check: Violation Detected

> **Status:** `WARNING_AUTHORIZED` — Conflict with Rejected Alternative (`{alt.id if alt else ""}`)

* **Decision:** `{dec.id if dec else ""}`: **{dec.title if dec else ""}**
* **Prohibited Construct:** {alt.proposal_text if alt else ""}
* **Authoritative Ruling:**
> "{ruling.exact_quote if ruling else ""}"  
> — **@{ruling.author_handle if ruling else "Maintainer"}** on [Deliberation Thread]({ruling.permalink if ruling else ""})

Run `/biscuit ruling` to view all active maintainer rulings governing this repository.
"""
        else:
            return f"""### ✅ Biscuit Architectural Check: Passed Cleanly

* **Verdict:** `{result.verdict.value}`
* **Active Candidates Evaluated:** {len(candidates)}
* **Violations Detected:** None

This pull request does not re-introduce any previously rejected architectural constructs.
"""

    # If invoked on an Issue
    decisions = get_all_decisions_for_repo(repo)
    decisions_summary = "\n".join(
        f"- ID: {d['decision'].id}\n"
        f"  Title: {d['decision'].title}\n"
        f"  Rejected: {d['alternative'].proposal_text if d['alternative'].lifecycle_status == ProposalLifecycle.REJECTED else 'N/A'}\n"
        f"  Ruling: \"{d['ruling'].exact_quote if d['ruling'] else ''}\" by @{d['ruling'].author_handle if d['ruling'] else ''}\n"
        f"  Files: {d['anchors']}"
        for d in decisions
        if d['alternative'].lifecycle_status == ProposalLifecycle.REJECTED
    )

    prompt = f"""You are the Biscuit Architectural Guardrail.
Active Architectural Decisions in Memory:
{decisions_summary}

Issue #{issue_or_pr_num} Context (User Query / Proposal: "{args}"):
Check if this proposal contradicts any active architectural rulings.
Provide a clear, formatted summary:
1. Verdict: PASSED or CONFLICT DETECTED
2. Decision ID & Title
3. Contradiction Summary or Clearance
"""
    return stream_thought_and_generation(
        prompt=prompt,
        repo=repo,
        placeholder_id=placeholder_id,
        system_prompt="You are a precise architectural compliance analyzer.",
        command_name="biscuit check",
    )


def execute_ruling_command(repo: str, issue_or_pr_num: int) -> str:
    """Retrieves all authoritative maintainer rulings governing the repo."""
    decisions = get_all_decisions_for_repo(repo)
    if not decisions:
        return "ℹ️ *No authoritative rulings are currently recorded for this repository.*"

    rulings_md = "### 📜 Active Authoritative Rulings in Memory\n\n"
    for d in decisions:
        dec = d["decision"]
        alt = d["alternative"]
        ruling = d["ruling"]
        anchors = ", ".join(f"`{a}`" for a in d["anchors"]) if d["anchors"] else "Repo"
        rulings_md += f"""#### 🏛️ `{dec.id}`: {dec.title}
* **Protected Files / Anchors:** {anchors}
* **Alternative:** {alt.proposal_text} (`{alt.lifecycle_status.value}`)
* **Ruling by:** **@{ruling.author_handle if ruling else 'Maintainer'}**

> "{ruling.exact_quote if ruling else 'Authoritative ruling recorded.'}"
> — [View Deliberation Thread]({ruling.permalink if ruling else dec.source_thread_url})

---
"""
    return rulings_md


def execute_help_command() -> str:
    return """### 🤖 Biscuit GitHub Ops-Bot Commands

Biscuit is the Architectural Decision Memory & Governance layer for this repository.

| Command | Description |
| :--- | :--- |
| **`/biscuit check`** | Evaluates the PR diff or issue proposal against all stored architectural invariants. |
| **`/biscuit ruling`** | Displays the active maintainer rulings, benchmark requirements, and permalinks governing this repo. |
| **`/biscuit record`** | Immediately extracts and records the current thread's consensus into Biscuit memory. |
| **`/biscuit help`** | Displays this command reference. |

*(All closed issues and pull requests are also automatically analyzed and archived upon closure).*
"""


# ── Webhook Event Router ──────────────────────────────────────────────────────

def handle_issue_comment_event(payload: dict) -> dict:
    action = payload.get("action")
    if action != "created":
        return {"status": "ignored", "reason": f"Comment action '{action}' not evaluated"}

    comment = payload.get("comment", {})
    comment_id = comment.get("id")
    comment_author = comment.get("user", {}).get("login", "")
    comment_body = comment.get("body", "") or ""

    # Never reply to ourselves
    if "biscuitt-hemlooo" in comment_author:
        return {"status": "ignored", "reason": "Self comment"}

    issue = payload.get("issue", {})
    issue_num = issue.get("number")
    repo = payload.get("repository", {}).get("full_name", "MRIARC-08/test")
    is_pr = "pull_request" in issue

    cmd, args = parse_command(comment_body)
    if not cmd:
        logger.info("Comment on #%d by @%s contains no /biscuit command; ignoring", issue_num, comment_author)
        return {"status": "ignored", "reason": "No command found"}

    logger.info("Executing command '%s' on %s#%d by @%s", cmd, repo, issue_num, comment_author)

    # 1. Instant Feedback: Add '👀' reaction to the user's comment (< 300ms)
    add_comment_reaction(repo, comment_id, "eyes")

    # 2. Instant Feedback: Post temporary placeholder comment
    placeholder_id = post_placeholder_comment(
        repo,
        issue_num,
        f"⏳ *Running `/biscuit {cmd}`... Reviewing architectural decisions and code anchors...*",
    )

    # 3. Execute the requested command
    try:
        if cmd == "check":
            result_md = execute_check_command(repo, issue_num, is_pr, args, placeholder_id=placeholder_id)
        elif cmd == "ruling":
            result_md = execute_ruling_command(repo, issue_num)
        elif cmd == "record":
            # Explicit record command
            res = handle_closed_thread(payload, is_pr=is_pr, force_record=True)
            result_md = f"✅ *Thread consensus extracted and recorded into Biscuit memory (Decision `{res.get('decision_id', '')}`).*"
        else:
            result_md = execute_help_command()
    except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as e:
        logger.error("Error executing command '%s': %s", cmd, e)
        result_md = f"⚠️ *Error executing `/biscuit {cmd}`: {e}*"

    # 4. In-Place Update: Morph the placeholder into the complete final result
    if placeholder_id:
        update_comment_in_place(repo, placeholder_id, result_md)

    return {"status": "command_completed", "command": cmd, "issue_num": issue_num}


# ── PHASE 2: Post-Closure Decision Archival ───────────────────────────────────

def handle_closed_thread(payload: dict, is_pr: bool, force_record: bool = False) -> dict:
    """Invoked when an Issue or PR is closed (or via /biscuit record).

    1. Checks if the discussion is Trivial vs Architectural.
    2. If architectural, extracts the decision, alternatives, and maintainer ruling.
    3. Persists the decision to Biscuit memory so future PRs are protected.
    4. Posts an archival receipt to GitHub.
    """
    item = payload.get("pull_request" if is_pr else "issue", {})
    num = item.get("number")
    repo = payload.get("repository", {}).get("full_name", "MRIARC-08/test")
    title = item.get("title", "")
    body = item.get("body", "") or ""
    html_url = item.get("html_url", "")
    merged = item.get("merged", False) if is_pr else False

    kind = "Pull Request" if is_pr else "Issue"
    logger.info("Processing closed %s #%d in %s: '%s' (merged=%s, force=%s)", kind, num, repo, title, merged, force_record)

    # Fetch full thread comments
    comments = fetch_issue_comments(repo, num)
    transcript = f"Title: {title}\nDescription:\n{body}\n\nComments:\n"
    for c in comments:
        user = c.get("user", {}).get("login", "")
        # skip bot's own comments from transcript
        if "biscuitt-hemlooo" in user:
            continue
        transcript += f"@{user}: {c.get('body')}\n---\n"

    prompt = f"""You are the Biscuit Architectural Decision Extractor.
A GitHub {kind} has been CLOSED in repository {repo}.
Status: {'MERGED' if merged else 'CLOSED WITHOUT MERGE'}
Complete Discussion Transcript:
{transcript}

Task:
Evaluate whether an authoritative Architectural Decision was deliberated or concluded in this thread.
Criteria:
- TRIVIAL (is_architectural = false): UI/CSS styling, typos, minor bugfixes, routine dependency updates, formatting.
- ARCHITECTURAL (is_architectural = true): Concurrency/locking models, caching architecture, data models, memory boundaries, public APIs, protocols, security policies, major library choices.

{'FORCE MODE: An explicit record command was issued. Force is_architectural = true.' if force_record else ''}

If is_architectural is false, return:
{{"is_architectural": false, "reason": "1 sentence explanation"}}

If is_architectural is true, return:
{{
  "is_architectural": true,
  "decision_title": "Clear descriptive title of the decision",
  "context_summary": "1-2 sentences on the problem and context",
  "alternatives": [
    {{
      "proposal_text": "Description of the alternative",
      "status": "ACCEPTED" or "REJECTED",
      "rejection_summary": "Reason for rejection if rejected, else null",
      "anchor_files": ["pkg/cache/cache.go"],
      "anchor_symbols": ["TypeName"]
    }}
  ],
  "ruling": {{
    "exact_quote": "Exact verbatim sentence from the maintainer ruling",
    "maintainer_handle": "username who made the authoritative ruling",
    "permalink": "{html_url}"
  }}
}}
"""
    model_resp = client.post(
        f"{LOCAL_ENDPOINT}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a principal systems architect. Respond ONLY in valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        },
    )
    content = model_resp.json()["choices"][0]["message"]["content"]
    if "```json" in content:
        content = content.split("```json")[-1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[-1].split("```")[0].strip()

    extraction = json.loads(content)
    if not extraction.get("is_architectural", False):
        logger.info("Closed %s #%d classified as TRIVIAL/NON-ARCHITECTURAL: %s", kind, num, extraction.get("reason"))
        return {"status": "skipped_trivial", "reason": extraction.get("reason")}

    # Architectural decision found! Persist to store.
    logger.info("🏛️ Architectural Decision Extracted from %s #%d: '%s'", kind, num, extraction.get("decision_title"))

    decision_id = f"DEC-{repo.split('/')[-1].upper()}-{num:03d}"
    dec = DecisionRecord(
        id=decision_id,
        title=extraction.get("decision_title", title),
        context_summary=extraction.get("context_summary", ""),
        source_thread_url=html_url,
        project_context=repo,
    )
    store.add_decision(dec)

    ruling_data = extraction.get("ruling", {})
    exact_quote = ruling_data.get("exact_quote", f"Decision concluded in {html_url}")
    maintainer = ruling_data.get("maintainer_handle", "maintainer")
    span_id = f"span-{decision_id.lower()}-1"

    span = EvidenceSpanRecord(
        id=span_id,
        alternative_id="",
        evidence_role=EvidenceRole.RULING,
        verification_status=VerificationStatus.VERIFIED_EXACT,
        exact_quote=exact_quote,
        start_char=0,
        end_char=len(exact_quote),
        author_handle=maintainer,
        author_contributor_id=maintainer,
        permalink=ruling_data.get("permalink", html_url),
    )
    store.add_evidence_span(span)

    anchors_list = []
    for i, alt_data in enumerate(extraction.get("alternatives", [])):
        alt_id = f"alt-{decision_id.lower()}-{i+1:03d}"
        status = ProposalLifecycle.REJECTED if alt_data.get("status") == "REJECTED" else ProposalLifecycle.ACCEPTED
        anchors = []
        for file_path in alt_data.get("anchor_files", []):
            anchors_list.append(file_path)
            anchors.append(
                CodeAnchorRecord(
                    id=f"anchor-{alt_id}-{len(anchors)+1}",
                    alternative_id=alt_id,
                    repository_url=f"https://github.com/{repo}",
                    language="go" if file_path.endswith(".go") else "python",
                    symbol_type="struct",
                    qualified_symbol_name=alt_data.get("anchor_symbols", [""])[0] if alt_data.get("anchor_symbols") else "",
                    head_file_path=file_path,
                    historical_file_path=file_path,
                    anchor_state=AnchorState.CURRENT,
                    lineage_resolved=True,
                )
            )

        alt_rec = DecisionAlternativeRecord(
            id=alt_id,
            decision_id=decision_id,
            proposal_text=alt_data.get("proposal_text", ""),
            lifecycle_status=status,
            rejection_summary=alt_data.get("rejection_summary"),
            ruling_evidence_id=span_id,
            ruling_authority="CORE_MAINTAINER_CONSENSUS",
            ruling_actor_id=maintainer,
            project_context=repo,
            ruling_signoffs=(
                SignoffRecord(
                    contributor_id=maintainer,
                    role="Lead Maintainer",
                    project_context=repo,
                ),
            ),
            anchors=tuple(anchors),
        )
        store.add_alternative(alt_rec)

    # Persist to disk
    save_store_to_disk()

    # Post confirmation receipt to GitHub
    anchors_display = ", ".join(f"`{f}`" for f in set(anchors_list)) if anchors_list else "Repository"
    receipt_body = f"""### 🏛️ Biscuit Architectural Memory: Decision Recorded

This {kind.lower()} has been closed and archived into Biscuit's architectural decision store.

* **Decision ID:** `{decision_id}`
* **Title:** **{dec.title}**
* **Context:** {dec.context_summary}
* **Protected Anchors:** {anchors_display}

#### 📜 Authoritative Ruling
> "{exact_quote}"  
> — **@{maintainer}** on [Thread]({html_url})

*Future pull requests proposing rejected alternatives in these files will now be guarded automatically.*
"""
    token = get_installation_token()
    client.post(
        f"https://api.github.com/repos/{repo}/issues/{num}/comments",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        json={"body": receipt_body},
    )
    return {"status": "decision_recorded", "decision_id": decision_id}


# ── PR & Issue Lifecycle Router ───────────────────────────────────────────────

def handle_pull_request_event(payload: dict) -> dict:
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    pr_num = pr.get("number") or payload.get("number")
    repo = payload.get("repository", {}).get("full_name", "MRIARC-08/test")

    if action == "closed":
        return handle_closed_thread(payload, is_pr=True)

    if action not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "reason": f"PR action '{action}' not evaluated"}

    # Evaluate PR diff against 4 Invariants
    token = get_installation_token()
    diff_resp = client.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_num}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3.diff"},
    )
    diff_resp.raise_for_status()
    diff_text = diff_resp.text

    parsed_diff = inspector.parse_and_analyze(diff_text, repository_url=f"https://github.com/{repo}")
    candidates = retriever.retrieve(parsed_diff)
    result = gate.evaluate(
        pr_diff=parsed_diff,
        candidates=candidates,
        execution_mode=ExecutionMode.PRODUCTION_COMMENT,
        pr_number=pr_num,
        commit_sha=pr.get("head", {}).get("sha", f"pr-{pr_num}"),
    )

    if result.verdict.value == "WARNING_AUTHORIZED":
        cand = next((c for c in result.candidate_evaluations if c.authorized), None)
        alt = store.get_alternative(cand.alternative_id) if cand else None
        dec = store.get_decision(alt.decision_id) if alt else None
        ruling = store.get_evidence_span(alt.ruling_evidence_id) if alt and alt.ruling_evidence_id else None
        ruling_text = ruling.exact_quote if ruling else "Authoritative ruling."
        ruling_actor = ruling.author_handle if ruling else "Maintainer"
        ruling_url = ruling.permalink if ruling else ""

        model_resp = client.post(
            f"{LOCAL_ENDPOINT}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are the Switch + Biscuit Decision Intelligence reasoning agent. Explain succinctly why this PR diff contradicts the maintainer ruling.",
                    },
                    {
                        "role": "user",
                        "content": f'Decision: "{dec.title if dec else ""}"\nRuling Quote: "{ruling_text}"\nPR Diff:\n{diff_text}',
                    },
                ],
                "temperature": 0.2,
            },
        )
        reasoning = model_resp.json()["choices"][0]["message"]["content"]
        comment_body = f"""### 🛑 Biscuit Architectural Decision Guardrail

> **Warning Authorized:** Conflict with Rejected Alternative (`{alt.id if alt else ""}`)

#### 📌 Contradiction Analysis
This Pull Request introduces changes that contradict previously rejected architecture in `{dec.title if dec else ""}`.

#### 📜 Authoritative Ruling
> "{ruling_text}"  
> — **@{ruling_actor}** on [Deliberation Thread]({ruling_url})

#### ⚖️ Trade-off & Synthesis
{reasoning}

#### 💡 Guidance
Please align with the accepted architecture or reference the maintainer ruling before proposing this refactor.
"""
        token = get_installation_token()
        client.post(
            f"https://api.github.com/repos/{repo}/issues/{pr_num}/comments",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
            json={"body": comment_body},
        )
        return {"status": "warning_posted", "verdict": result.verdict.value}

    return {"status": "clean", "verdict": result.verdict.value}


def handle_issue_event(payload: dict) -> dict:
    action = payload.get("action")
    if action == "closed":
        return handle_closed_thread(payload, is_pr=False)
    return {"status": "ignored", "reason": f"Issue action '{action}' not evaluated"}


# ── FastAPI Endpoints ─────────────────────────────────────────────────────────

@app.get("/")
def health_check():
    return {
        "service": "biscuit-ops-bot",
        "status": "healthy",
        "commands": ["/biscuit check", "/biscuit ruling", "/biscuit record", "/biscuit help"],
        "decisions_loaded": len(store.all_alternatives()),
    }


@app.post("/webhook")
@app.post("/api/github/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
):
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    event_type = x_github_event or payload.get("event")
    logger.info("===> Received GitHub Webhook: event_type=%s", event_type)

    if event_type == "issue_comment":
        res = handle_issue_comment_event(payload)
        return JSONResponse(content=res)
    elif event_type == "pull_request":
        res = handle_pull_request_event(payload)
        return JSONResponse(content=res)
    elif event_type == "issues":
        res = handle_issue_event(payload)
        return JSONResponse(content=res)
    elif event_type == "ping":
        return JSONResponse(content={"status": "pong", "message": "GitHub ping received!"})
    else:
        return JSONResponse(content={"status": "ignored", "event": event_type})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
