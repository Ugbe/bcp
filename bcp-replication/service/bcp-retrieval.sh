#!/bin/bash

utils=/opt/supervisor-scripts/utils
. "${utils}/logging.sh"
. "${utils}/cleanup_generic.sh"
. "${utils}/environment.sh"
. "${utils}/exit_portal.sh" "BrowseComp Retrieval"

REPO="${WORKSPACE:-/workspace}/BrowseComp-Plus"

source /venv/main/bin/activate

# Pyserini talks to Lucene over a JVM bridge and needs JAVA_HOME resolved here --
# supervisor services do not get an interactive login shell.
export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"

cd "${REPO}"

pty python searcher/search_r1_server.py \
    --searcher-type hybrid \
    --host 127.0.0.1 \
    --port __INTERNAL_PORT__ \
    --k 10 \
    --snippet-max-tokens 512 \
    --index-path "${REPO}/indexes/qwen3-embedding-8b-finetuned-v2/corpus.shard*.pkl" \
    --bm25-index-path "${REPO}/indexes/bm25" \
    `# 100 candidates from each leg, and rerank every fused candidate (union <= 200)` \
    `# down to the final top 10 -- nothing retrieved is dropped before ranking.` \
    --candidates-k 100 \
    --rerank-depth 200 \
    `# Cascade rerank: score all 200 fused candidates cheaply at 512 tokens, then` \
    `# rescore the top 20 at the full 8192. Scoring all 200 at full length cost` \
    `# ~716k tokens/query and was ~85% of end-to-end latency; the documents that` \
    `# reach the top 10 are still judged at full length.` \
    --rerank-stage1-length 512 \
    --rerank-stage1-keep 20 \
    `# Pre-tokenized corpus (scripts_build_index/build_rerank_token_cache.py). The` \
    `# document half of the rerank prompt is query-independent, so it is tokenized` \
    `# once offline instead of ~2.2s/query in a single-threaded loop.` \
    --rerank-token-cache "${REPO}/indexes/rerank-token-cache" \
    --rerank-token-budget 32768 \
    --rerank-max-batch 64 \
    --attn-implementation flash_attention_2 \
    `# Passed explicitly, though they are also the defaults, so that the documented` \
    `# "ps aux | grep search_r1_server" integrity check shows which checkpoints are live.` \
    --embedding-model "Qwen/Qwen3-Embedding-8B" \
    --embedding-lora "DanielTobi0/qwen3-embedding-8b-browsecomp-lora-v2" \
    --reranker-model "Qwen/Qwen3-Reranker-0.6B" \
    --reranker-lora "DanielTobi0/qwen3-reranker-0.6b-browsecomp-lora-v3" \
    2>&1
