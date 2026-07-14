# **PBC Email Agent MVP — Work Trial Brief**

## **Objective**

We're evaluating whether you can **architect and ship agentic AI systems in a regulated domain**, creating an easy to use, **auditable** and extensible agentic system for a simple application with an intuitive UI. This should take about 3 days.

## **Context**

We deploy AI agents into small and mid-size US accounting and audit firms. One workflow partners hate: chasing PBC (prepared-by-client) items during a financial statement audit. Over 4–8 weeks a senior chases a 40-item list across 30+ email threads with attachments named `scan001.pdf` and `Final_v3_REAL.xlsx`. Nobody knows what's received, outstanding, latest version, or defensible to a PCAOB inspector.

Our wedge: an **agent** that lives on the audit inbox and keeps the tracker current, with reasoning traces a partner could defend to a peer reviewer.

## **The task**

Build an agent that ingests a messy PBC email thread \+ attachments and produces a live, structured audit-request tracker.

**We give you:**

* **At handout (sample, for building):** 15 emails / 4 threads / 8 attachments, the real 30-item PBC list PDF, and a dummy client profile (entity, fiscal year-end, subsidiaries)  
* Data: [File](https://drive.google.com/file/d/11v21vzTTowqtbHOnHJY0Z10KPZ-Ss14s/view?usp=sharing)  
* **At the review call (held-out, for live evaluation):** \~90 emails / \~12 threads / \~40 attachments — same shape and category mix as the sample, unseen by you.

## **Deliverables**

1. The agent (Python, any framework), The UI.  
2. **1-page README:** architecture diagram, model choices per step, tool schemas, hallucination guardrails, eval strategy, measured cost per PBC list processed in USD, what breaks at 10 and 100 concurrent audits.  
3. **10-minute Loom:** code walkthrough \+ demo on the sample. Focus on the agent loop.

## **Appendix**

## *"Preferred: an agent loop written directly against Anthropic or OpenAI's native tool-use API — typically 100–300 lines of Python without an agent framework on top. Frameworks (LangGraph, PydanticAI, OpenAI Agents SDK, DSPy) are allowed, but you'll walk the tool-selection code live and we prefer submissions where the loop is inspectable in one file. LangChain chains and prompt-stitching pipelines are not agents and will not pass."*

## **What the agent must do**

1. **Plan explicitly.** On each new email, decide what to do (parse, re-classify, request clarification, do nothing). Plan trace visible in the UI.  
2. **Use tools.** Ingestion, OCR, Excel parsing, embedding search, field extraction, drafting — all as tools the agent calls.  
3. **Route models by cost.** Embeddings for candidate matching  
4. **Verify.** A distinct verifier step checks that extracted evidence actually satisfies the line item's acceptance criteria.   
5. **Extract with citations.** Period, entity, date, key totals — each with a page/cell/bbox citation.   
6. **Detect insufficiency.** Flag when the client claims to answer an item but the attachment doesn't (Q2 sent when Q3 asked, wrong entity, threshold not met).  
7. **Draft grouped follow-ups.** One clean email per recipient/topic, not 15 separate ones.  
8. **Version documents.** `Final_v3_REAL.xlsx` supersedes v1/v2 when semantically the same. Keep the lineage. Don't over-engineer this.

**1\. Dynamic control flow, not a hardcoded pipeline.**

The agent must decide, per email, what to do next: parse, re-classify, request clarification, run OCR, escalate to a bigger model, or do nothing. We will inspect the trace and ask you to show us **two runs on similar-looking emails where the agent took different paths and why.**

**2\. Tool-calling as the abstraction, native to the LLM.**

Ingestion, OCR, Excel parsing, embedding search, field extraction, drafting — all as tools the model chooses to call via native tool-use (OpenAI function calling / Anthropic tool use / equivalent). Not as sequential Python function calls wrapped around one LLM prompt. 

**3\. Evals as first-class, not bonus.**

5–10 labeled examples minimum. Report precision, recall, insufficiency-detection F1, expected tool-call sequence match, and cost.

**Minimal UI**

* **Tracker view:** row per line item — status (`Not started` / `Received` / `Under review` / `Insufficient` / `Complete`), latest version, source email, confidence.  
* **Agent trace view:** for any item, the full plan \+ tool-call trace \+ verifier verdict. This is where we spend most of the review.  
* **Follow-up review:** approve / edit / reject each draft. "Send" is mocked.

## **Hard constraints**

* **Runs end-to-end on the held-out mailbox live at the review**, cold, from a clean checkout.  
* **Every status decision has a full agent trace** — plan, tool calls, verifier verdict — defensible to a PCAOB inspector.   
* **$5 max per PBC list processed** in real LLM/API spend, measured.   
* **No hardcoded client- or engagement-specific logic.** The PBC list is the config. We'll swap it at the review.  
* **Deterministic tests** around non-LLM logic (parsing, versioning, state).

## **Bonus (strongly encouraged)**

* **Eval harness:** 5–10 labeled examples with expected classifications, insufficiency flags, *and expected tool-call sequences*. Script reports precision, recall, cost.

## **Bonus** 

* **Extending it** to multitenancy review model

