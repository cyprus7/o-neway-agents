#!/bin/sh
set -eu
. /opt/embedding/model-lock.env

model_dir=${OLLAMA_MODELS:-/models}
manifest="$model_dir/manifests/registry.ollama.ai/library/qwen3-embedding/0.6b"
ready=/tmp/embedding-ready
rm -f "$ready"

if [ "$(id -u)" != 10001 ] || [ "$(id -g)" != 10001 ]; then
    echo 'Embedding runtime requires uid/gid 10001.' >&2
    exit 1
fi
if [ ! -w "$model_dir" ]; then
    echo 'Embedding model volume is not writable by uid 10001.' >&2
    exit 1
fi

/bin/ollama serve &
server_pid=$!
stop_server() {
    rm -f "$ready"
    kill -TERM "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
}
trap 'stop_server; exit 143' TERM INT HUP
trap 'stop_server' EXIT

attempt=0
until OLLAMA_HOST=127.0.0.1:11434 /bin/ollama list >/dev/null 2>&1; do
    if ! kill -0 "$server_pid" 2>/dev/null || [ "$attempt" -ge 30 ]; then
        echo 'Embedding daemon did not start.' >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done

# Never refresh an already installed mutable tag. A changed manifest fails closed.
if [ ! -f "$manifest" ]; then
    echo 'Downloading the locked public embedding model into its persistent volume.'
    if ! timeout 600 /bin/ollama pull "$EMBEDDING_MODEL" >/tmp/embedding-pull.log 2>&1; then
        echo 'Embedding model download failed or timed out; inspect its local pull log.' >&2
        exit 1
    fi
fi
printf '%s  %s\n' "$EMBEDDING_INSTALLED_MANIFEST_SHA256" "$manifest" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$EMBEDDING_MANIFEST_SHA256" "$model_dir/blobs/sha256-$EMBEDDING_MANIFEST_SHA256" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$EMBEDDING_MODEL_SHA256" "$model_dir/blobs/sha256-$EMBEDDING_MODEL_SHA256" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$EMBEDDING_CONFIG_SHA256" "$model_dir/blobs/sha256-$EMBEDDING_CONFIG_SHA256" | sha256sum -c - >/dev/null
touch "$ready"
echo 'Locked local embedding model is ready.'
wait "$server_pid"
