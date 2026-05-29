# Local AI Agent System — Spec
# USB-based, Remote GPU Execution
# Status: future project, not started

## 1. PRIMARY OBJECTIVE

Build a fully functional local AI chatbot system:
- Conversational chatbot with persistent long-term memory
- Multi-session continuity
- Persona / character-based interaction
- High coherence and consistency
- Maximize freedom of interaction within legal constraints
- Portable via USB SSD, remotely accessible via Tailscale/SSH

## 2. ARCHITECTURE

```
User Laptop
-> Agent Controller (Telegram or CLI or UI)
-> Remote Compute Node (RTX 3090)
-> LLM + Tools Layer
-> Memory System (USB SSD)
```

**A. Control Layer** — laptop/existing env: orchestration, monitoring, restarting services
**B. Compute Layer** — RTX 3090: LLM inference, optional image/video gen, embeddings
**C. State Layer** — USB SSD: all persistent state

## 3. USB STORAGE LAYOUT

```
/data/
  chats/
  memory/
  vectordb/
  characters/
  prompts/
  configs/
  logs/
  exports/
```

USB requirements: external SSD, NVMe preferred, min 2TB (rec 4TB), USB-C / USB 3.2 / Thunderbolt

On USB: chat logs, memory DB, embeddings, vector DB, character defs, prompts, agent state
On compute machine: LLM weights, image/video models (after initial setup)

## 4. MODEL REQUIREMENTS

Open-weight, minimal censorship, long context, good at roleplay/persona.

Primary: **Dolphin3.0-Mistral-24B** (Q4 quantization, ~14GB VRAM — fits RTX 3090 with headroom)
Secondary: Dolphin-Llama3-8B (faster, less coherent), DeepSeek R1 Distill
Backend: Ollama (preferred) or llama.cpp server or LM Studio

Rationale: Dolphin series specifically removes RLHF alignment/refusal. Best balance of freedom + roleplay coherence + fits single 24GB GPU.

## 5. MEMORY SYSTEM

### Layers
- Short-term: recent chat history (context window)
- Long-term: vector DB retrieval (semantic search)
- Summarized: compressed session summaries

### Layout on USB
```
memory/
  user_profile.json
  session_summaries/
  character_states/
  world_state/

vectordb/
  embeddings.index
  metadata.json
```

Vector DB options: Chroma, Qdrant, FAISS
Embedding model: bge-base, bge-large, or nomic-embed-text (local, no API needed)

## 6. CHARACTER SYSTEM

Up to 10 characters, one primary persona, others supporting.

```
characters/
  main_character.json   # speaks by default
  character_1.json
  character_2.json
```

Each character: personality, tone, role, relationships, memory references.
Others referenced in third person, activated only when needed.

## 7. FRONTEND

Preferred: SillyTavern
Alternative: Open WebUI

## 8. NETWORKING

- Tailscale (Tailnet) for laptop-to-compute connectivity
- SSH access to compute node
- HTTP API from laptop to LLM server on compute node
- Optional: Telegram bridge (already exists in relay_v2)

## 9. DEPLOYMENT PHASES

1. **Init** — detect USB mount, create folder structure, install dependencies
2. **Model setup** — download LLM to USB initially, later migrate to local SSD
3. **Backend** — install Ollama/llama.cpp, launch API server, verify endpoint
4. **Memory** — install vector DB, init embedding model, build indexing pipeline
5. **Frontend** — install SillyTavern, configure API endpoint, load character system
6. **Integration** — connect LLM with memory, connect frontend with backend, implement retrieval pipeline, implement summarization loop
7. **Testing** — chatbot responds, remembers facts across sessions, maintains persona consistency, data survives restart
8. **Optimization** — move models to internal SSD, keep state on USB, optimize prompt size, tune retrieval

## 10. CONSTRAINTS

Hard: must run locally, no cloud GPU dependency, all user data stays local
Soft: prefer open-weight models, prefer minimal filtering, prefer modular design

## 11. FAILURE MODES TO HANDLE

- model not loading
- API not responding
- vector database corruption
- memory not retrieved
- persona drift
- slow performance

## 12. SUCCESS CRITERIA

- user can chat interactively
- memory persists across sessions
- character is consistent
- system restarts without manual intervention
- all data on USB except optional models

## AGENT BUILD STRATEGY

Build minimal working version → test → iterate → improve memory and behavior.
