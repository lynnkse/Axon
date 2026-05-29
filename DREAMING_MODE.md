# Dreaming Mode / Memory Consolidation – Future R&D Direction

We are building an agent that operates inside a single cloud code session and progressively builds and improves itself. The agent already has a task / todo flow, and we are now adding Supabase as a long-term memory backend. A possible next research and development step is to add a "dreaming mode" on top of this memory system.

## What this means

Dreaming mode is a background memory-consolidation process that runs when the agent is idle or during scheduled low-activity periods. Instead of only storing raw memories, logs, decisions, and traces, the agent revisits accumulated data and reorganizes it. The goal is to extract durable facts, repeated patterns, procedures, constraints, and cross-session insights, and to gradually turn noisy history into structured long-term knowledge.

## Why this matters

A basic long-term memory store is useful, but by itself it can become large, flat, noisy, and hard to retrieve from correctly. Dreaming mode is intended to improve memory quality over time, not just memory quantity. The expected value is:
- better retrieval of important context
- less duplication and contradiction
- stronger persistence of repeated lessons and user preferences
- emergence of higher-level abstractions from repeated experience
- better long-term agent continuity across sessions

## Conceptual architecture

1. **Raw memory layer**
   - session logs, tool outputs, decisions, task history, user corrections, important observations

2. **Short-term candidate memory layer**
   - extracted memory atoms from recent sessions
   - tagged as facts, preferences, procedures, open threads, warnings, patterns
   - tracked with metadata: timestamp, source, recurrence count, recall frequency

3. **Durable memory layer**
   - curated long-term memory safe to inject back into agent context
   - small, high-signal, explainable

4. **Structured knowledge layer** (optional, later)
   - organizes durable memory into hierarchy: projects, entities, concepts, procedures, rules, syntheses

## Suggested dream cycle

**A. Light phase**
- scan recent sessions and newly stored memory
- extract candidate memory items
- deduplicate and cluster related items
- do not yet promote into durable memory

**B. REM / reflection phase**
- detect repeated themes, lessons, cross-session links
- generate higher-level summaries ("recurring issue", "stable preference", "reliable workflow", "important unresolved thread")
- strengthen candidates that appear in multiple contexts

**C. Deep phase**
- score candidates: recurrence, relevance, diversity of contexts, recency, usefulness
- re-check source evidence before promotion
- promote only high-confidence items into durable memory
- optionally update structured knowledge pages or indexes

## Relation to Supabase

Supabase serves as the persistence layer for:
- raw event log
- candidate memory items
- durable memory records
- embeddings / search indexes
- provenance metadata
- promotion history
- contradiction / staleness markers

## Two implementation options

**Option 1: Supabase only**
- use Supabase as both storage and retrieval layer
- simplest starting point
- good for validating whether long-term memory already gives enough benefit

**Option 2: Supabase + Dreaming mode**
- Supabase remains the storage backend
- dreaming mode becomes a scheduled consolidation process over stored memory
- more complex, but potentially much higher quality over time

## Main design principles

- durable memory must stay small and high-signal
- promotion should be evidence-based, not automatic by default
- all promoted memory should keep provenance to source sessions or artifacts
- system should separate raw history from curated memory
- system should support rollback, review, and safe reprocessing
- untrusted or sensitive data must not be promoted without filtering

## Main risks

- promoting hallucinated or weak information into long-term memory
- creating self-reinforcing but incorrect beliefs
- over-complicating the memory system too early
- increased compute cost from background consolidation
- security / privacy issues if logs are reprocessed without sanitization

## Recommended evaluation path

1. First implement plain Supabase long-term memory
2. Measure actual retrieval usefulness and failure modes
3. Add a preview-only dream cycle that proposes promotions without auto-applying them
4. Review whether proposed consolidations are genuinely useful
5. Only then consider automatic promotion and hierarchical knowledge generation

## Current conclusion

Dreaming mode should be treated as a future enhancement layer, not the first milestone. The immediate milestone is stable long-term memory in Supabase. After that, evaluate whether a background consolidation system meaningfully improves retrieval quality, reduces noise, and helps the agent accumulate durable knowledge over time.

**Working assumption:**
- Supabase memory = foundation (build now)
- Dreaming mode = later research step for consolidation, restructuring, and abstraction
