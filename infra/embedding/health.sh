#!/bin/sh
set -eu
. /opt/embedding/model-lock.env
[ -f /tmp/embedding-ready ]
manifest="${OLLAMA_MODELS:-/models}/manifests/registry.ollama.ai/library/qwen3-embedding/0.6b"
printf '%s  %s\n' "$EMBEDDING_INSTALLED_MANIFEST_SHA256" "$manifest" | sha256sum -c - >/dev/null
timeout 3 env OLLAMA_HOST=127.0.0.1:11434 /bin/ollama list >/dev/null 2>&1
