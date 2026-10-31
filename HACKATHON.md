# Hackathon Project Submission

## Team Information
- **Team Name:** Chole Bhature
- **Team Members:**
  - Adarsh Chauhan ([@mriarc-08](https://github.com/mriarc-08))
  - Dakshesh Sharma ([@daksheshsharma2409](https://github.com/daksheshsharma2409))
  - Utkarsh Singh ([@utksh1](https://github.com/utksh1))

---

## Selected Problem Statement
- **Problem ID:** Problem 06
- **Title:** Open Source Decision Intelligence and Knowledge Recovery
- **Category:** Knowledge Management | Developer Intelligence | Information Retrieval

---

## AI Models Used & Technical Justification

In adherence to the hackathon's open-source and lightweight model constraints, our solution employs the following models selected from the approved model families:

### 1. Qwen 2.5-Coder-7B-Instruct (Family: Qwen)
- **Role:** Deliberation Extraction, Architectural Decision Record (ADR/MADR) Synthesis, and Code-to-Decision Grounding.
- **Justification:**
  - **Tool-Calling Parity:** Switch's Model Context Protocol (FastMCP) requires reliable function-calling without hallucinated syntax. Qwen 2.5-Coder is among the top open-weights models for function calling.
  - **Code & AST Comprehension:** It understands AST structures, Git diffs, and Python syntax, allowing historical decisions to be linked to code even when files are refactored or moved.
  - **Strict Schema Enforcement:** Accurately extracts structured JSON/YAML records containing accepted decisions, trade-offs, and specifically **rejected alternatives with justifications**.

### 2. Llama 3.2-3B-Instruct (Family: Llama)
- **Role:** High-Throughput Thread Summarization and Real-Time Chat Interception.
- **Justification:**
  - **Low Latency & Memory Efficiency:** At only 3.2B parameters, it runs easily on CPU/edge devices (<2.5GB RAM) with 80+ tokens/sec throughput.
  - **Conversational & Thread Filtering:** Ideal for rapidly processing noisy discussion threads (filtering out conversational banter across 100+ comments) and powering real-time chat responses in messaging bridges (Slack/Discord).

### 3. Phi-3.5-mini-instruct (Family: Phi)
- **Role:** Complex Argumentation Mining and Long-Context Deliberation Analysis.
- **Justification:**
  - **128k Context Window:** Long mailing list debates and Discourse topics (such as Python PEP discussions) often span thousands of lines. Phi-3.5-mini handles massive context lengths without losing focus.
  - **High-Density Reasoning:** Trained heavily on synthetic reasoning datasets, making it effective at parsing multi-party debates to discern subtle causal reasons for idea rejection.
