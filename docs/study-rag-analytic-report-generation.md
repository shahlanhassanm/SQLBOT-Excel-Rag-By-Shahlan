# Study Report — RAG That Turns Uploaded Documents/Data into Analytic Reports

**Date:** 2026-06-26
**Question studied:** "Upload documents into a RAG system → it produces an analytic report." Does an identical model/system already exist (papers + repos + open-weight models), and what open-source base should we build on?

---

## 1. Bottom line (verdict)

**Yes — near-identical systems already exist, and they are open-source.** Your idea is not novel as a *category*; it is an active, well-funded research area (there is an entire **ACL 2026 shared task** dedicated to "RAG for Report Generation"). What is *not* yet solved off-the-shelf is your **specific combination**: RAG report generation that is **scheduled**, **runs on freshly-added data**, and is **grounded in your own structured business data**. That gap is where your value is.

The field splits into **two lineages**, and which one is "identical" to your idea depends on what you upload:

| If you upload… | The matching lineage | Closest existing open-source thing |
|---|---|---|
| **Unstructured docs** (PDF, Word, reports, notes) | "Deep research / report-writing agent" | **GPT Researcher**, **STORM**, **Mind2Report** (paper) |
| **Structured data** (CSV, Excel, DB tables) | "Autonomous data-analyst agent" | **DeepAnalyze** (open 8B model + code), **Auto-Analyst** |

For SQLBot (structured business data, NL→SQL already built), the **data-analyst lineage is the closer match** — and **DeepAnalyze** is the single most "identical" open-source artifact to "auto-analyze data → one-click professional report."

---

## 2. The problem, decomposed

The idea is a composite of four separable capabilities. No single repo nails all four; each is well-covered individually.

1. **Ingestion + retrieval** — get uploaded docs/data into a searchable index (vector / BM25 / graph / SQL).
2. **Analysis** — compute the actual findings (stats over structured data, nugget extraction over text).
3. **Report synthesis** — an agent plans an outline, retrieves, and writes long-form grounded prose with citations.
4. **Freshness / scheduling** — re-run on newly added data on a cadence (cron) and reflect "what changed."

Capabilities 1–3 are mature open-source. Capability 4 is the thin, mostly-unbuilt part (plain orchestration + delta detection), and is the differentiator.

---

## 3. Research papers (closest first)

| Paper | Date | Why it matters to you | Link |
|---|---|---|---|
| **RAG4Reports — 1st Workshop on RAG for Report Generation (ACL 2026)** | ACL 2026 | The academic anchor. Defines "report generation" as a long-form RAG task with **strict grounding + citation (attestation)**. Eval via **Auto-ARGUE** (scores whether information "nuggets" are present and correctly cited). 4M multilingual corpus. This is the rubric your output should be judged against. | https://rag4reports.github.io/ |
| **Mind2Report: Cognitive Deep Research Agent for Expert-Level Commercial Report Synthesis** | Jan 2026 | The most on-point paper for *business* reports. Emulates a commercial analyst: probes intent → searches sources → iteratively synthesizes with **dynamic memory**. Directly models "expert-level business report." | https://hf.co/papers/2601.04879 |
| **AgentCPM-Report: Interleaving Drafting and Deepening for Open-Ended Deep Research** | Feb 2026 | **Most relevant for self-hosting.** A *lightweight, local* report-generation agent using small models (WARP = "Writing As Reasoning Policy", multi-stage agentic RL). Shows how to get good reports without a frontier API. | https://hf.co/papers/2602.06540 |
| **From Facts to Insights: Generation & Evaluation of Analytical Reports for Earnings Calls** | Oct 2024 | Multi-agent generation **and evaluation** of analytical reports from a business source. Good template for "insight vs. summary" quality measurement. | https://hf.co/papers/2410.01039 |
| **Insight Agents: LLM Multi-Agent System for Data Insights** | Feb 2026 | Plan-and-execute multi-agent over e-commerce business data → personalized insights with OOD detection. Close to "analytic report from business data." | https://hf.co/papers/2601.20048 |
| **FinTeam: Multi-Agent Collaborative System for Financial Scenarios** | Jul 2025 | Specialized LLM agents collaborating to produce comprehensive **financial reports**. | https://hf.co/papers/2507.10448 |
| **FALM — Harnessing Business and Media Insights with LLMs (Fortune Analytics LM)** | Jun 2024 | **Time-aware reasoning**, thematic trend analysis, and **content referencing** for trustworthy business analysis — directly relevant to "report on data that updates over time." | https://hf.co/papers/2406.06559 |
| **LitLLM: Toolkit for Scientific Literature Review** | Mar 2025 | RAG + prompting to auto-generate a literature review (a report) from documents. Good engineering reference for the doc→report path. | https://hf.co/papers/2402.01788 |
| **DailyQA** (freshness benchmark) | May 2025 | How to *evaluate* whether your pipeline actually reflects newly-added data — the "daily update" half. | https://arxiv.org/pdf/2505.17162 |
| **Advancing RAG for Structured Enterprise & Internal Data** | Jul 2025 | Hybrid retrieval + metadata filtering over tabular data, with the retrieval-quality metrics (Precision@5, MRR, Faithfulness) you'd want in production. | https://hf.co/papers/2507.12425 |

---

## 4. Open-source repositories (closest first)

### 4a. Document → report ("deep research" agents)
| Repo | Stars | License | Fit | Link |
|---|---|---|---|---|
| **GPT Researcher** (assafelovic) | ~28k | Apache-2.0 | **Closest doc→report match.** Runs research over **local uploaded docs** (PDF/txt/CSV/Excel/MD/PPT/Word), writes a cited report (PDF/Word/MD). Works with **local LLMs** via `OPENAI_BASE_URL` → fully self-hostable. | https://github.com/assafelovic/gpt-researcher |
| **STORM / Co-STORM** (stanford-oval) | high | MIT | Generates long-form, **Wikipedia-quality cited reports**; **VectorRM** grounds on your uploaded documents. Strong outline/perspective engine. | https://github.com/stanford-oval/storm |

### 4b. Data → analytic report (data-analyst agents) — **closest to SQLBot**
| Repo | Stars | License | Fit | Link |
|---|---|---|---|---|
| **DeepAnalyze** (ruc-datalab) | ~4.3k | MIT | **The single most "identical" artifact.** "First agentic LLM for autonomous data science." Upload CSV/Excel/DB/JSON/XML/txt → end-to-end prep, analysis, modeling, **visualization, and one-click professional report (PDF)**. Open **model + code + training data**. | https://github.com/ruc-datalab/DeepAnalyze |
| **Auto-Analyst** (FireBird-Technologies) | — | open | Modular open AI data-science platform: cleaning → stats → ML → viz. Good architecture reference. | https://github.com/FireBird-Technologies/Auto-Analyst |
| **e2b ai-analyst** (e2b-dev) | — | open | Minimal: upload CSV → Llama 3.1 → interactive charts. Good sandbox-execution pattern. | https://github.com/e2b-dev/ai-analyst |

### 4c. RAG engines (the ingestion/retrieval substrate to build on)
| Repo | License | Fit | Link |
|---|---|---|---|
| **RAGFlow** (infiniflow) | Apache-2.0 | Leading RAG engine with **deep document understanding** (tables/layout) + agent capabilities. Strongest substrate for messy real-world docs. | https://github.com/infiniflow/ragflow |
| **kotaemon** (Cinnamon) | open | Clean self-hosted "chat with your docs" UI, multi-user, collections. Fast to stand up. | https://github.com/Cinnamon/kotaemon |
| **RAG-Anything** (HKUDS) | open | All-in-one **multimodal** RAG: interleaved text, tables, figures, equations in one interface. Good for financial reports/PDFs. | https://github.com/HKUDS/RAG-Anything |
| **LightRAG** (HKUDS, EMNLP 2025) | open | Lightweight **graph + vector** RAG; good when relationships across the corpus matter. | https://github.com/HKUDS/LightRAG |

---

## 5. Open-weight models you can actually run

| Model | Size | License | Use it for | Link |
|---|---|---|---|---|
| **DeepAnalyze-8B** (RUC-DataLab) | 8B | MIT | The whole **data→report** task in one fine-tuned model (base: DeepSeek-R1-0528-Qwen3-8B). Drop-in if your input is tabular. | https://huggingface.co/RUC-DataLab/DeepAnalyze-8B |
| **AgentCPM-Report** (OpenBMB lineage) | small | open | Local **deep-research report** generation tuned to run without a frontier API. | (see paper 2602.06540) |
| **Qwen3 / DeepSeek-R1 distills / Llama 3.x / gpt-oss** | various | open | General **report-writer / reasoner** if you build the agent yourself rather than use a task-specific model. | Hugging Face Hub |
| **BGE-M3** (embeddings) | — | open | Multilingual dense+sparse retrieval for the RAG index. | https://huggingface.co/BAAI/bge-m3 |

---

## 6. Recommendation

**There is no need to train a new model.** Two viable paths, depending on your priority:

### Path A — Fastest "it works" (reuse a task-specific model)
- **If input is structured data:** deploy **DeepAnalyze-8B** + its repo. It already does upload-data → analytic-report-with-charts, MIT-licensed, self-hostable. Wrap it with a scheduler for the "daily/weekly" cadence.
- **If input is unstructured docs:** deploy **GPT Researcher** (local-docs mode) or **STORM (VectorRM)**, point it at a local LLM, and schedule it.

### Path B — Best fit for *your* stack (recommended, given SQLBot)
You already have the hard part — a deterministic agentic **NL→SQL** pipeline with grounding and tests. Don't bolt on a generic data-analyst model that re-derives that badly. Instead:

1. **Retrieval substrate:** your existing SQLBot datasources + (optionally) **RAGFlow** for any unstructured PDFs/docs that come with the data.
2. **Analysis:** your existing SQL pipeline computes the numbers deterministically (this is your edge — numbers come from SQL, not from an LLM).
3. **Report synthesis:** borrow the **outline→retrieve→write→verify** loop from **GPT Researcher / Mind2Report**, with a **strict grounding contract** (every figure traces to a computed value; reject/regenerate otherwise — the Auto-ARGUE/attestation standard).
4. **Freshness + schedule:** the one genuinely-unbuilt piece — delta detection over newly-added data + a cron/APScheduler trigger. This is plain orchestration, and it is exactly what differentiates you from every repo above (they are all on-demand, not scheduled-on-fresh-data).

**Net:** Use DeepAnalyze as the reference implementation to study, GPT Researcher/STORM as the report-agent pattern, RAGFlow as the doc substrate, and the Auto-ARGUE grounding standard as your quality bar — but keep SQLBot's SQL layer as the analysis engine and build the scheduled-delta layer yourself.

---

## 7. The gap nobody fills (your differentiator)
Every system found is **on-demand and stateless** — you ask, it researches, it writes. None natively does **"watch a datasource, and on a schedule emit a report about what changed since last time."** That scheduled, delta-aware, grounded-in-your-own-data behavior is the novel, defensible part — and it is engineering (scheduler + watermark/delta provider + grounding validator), not a research problem.

---

## Sources
- ACL 2026 RAG4Reports shared task — https://rag4reports.github.io/ and https://www.aclweb.org/portal/content/shared-task-1st-workshop-retrieval-augmented-generation-report-generation-acl-2026
- Mind2Report — https://hf.co/papers/2601.04879
- AgentCPM-Report — https://hf.co/papers/2602.06540
- From Facts to Insights (earnings calls) — https://hf.co/papers/2410.01039
- Insight Agents — https://hf.co/papers/2601.20048
- FinTeam — https://hf.co/papers/2507.10448
- FALM (Fortune Analytics LM) — https://hf.co/papers/2406.06559
- LitLLM — https://hf.co/papers/2402.01788
- DailyQA — https://arxiv.org/pdf/2505.17162
- Advancing RAG for Structured Enterprise Data — https://hf.co/papers/2507.12425
- GPT Researcher — https://github.com/assafelovic/gpt-researcher
- STORM — https://github.com/stanford-oval/storm
- DeepAnalyze — https://github.com/ruc-datalab/DeepAnalyze and https://huggingface.co/RUC-DataLab/DeepAnalyze-8B
- Auto-Analyst — https://github.com/FireBird-Technologies/Auto-Analyst
- e2b ai-analyst — https://github.com/e2b-dev/ai-analyst
- RAGFlow — https://github.com/infiniflow/ragflow
- kotaemon — https://github.com/Cinnamon/kotaemon
- RAG-Anything — https://github.com/HKUDS/RAG-Anything
- LightRAG — https://github.com/HKUDS/LightRAG
