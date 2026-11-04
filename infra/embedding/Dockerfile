FROM ollama/ollama:0.40.10@sha256:4c5b27871a3239a717048f9b93057680a51b60c8119a7016aebb57f938e25e35

RUN groupadd --gid 10001 embedding \
    && useradd --uid 10001 --gid 10001 --home-dir /home/embedding --create-home embedding \
    && mkdir -p /models /opt/embedding \
    && chown 10001:10001 /models
COPY --chmod=444 model-lock.env /opt/embedding/model-lock.env
COPY --chmod=555 bootstrap.sh health.sh /opt/embedding/

ENV HOME=/home/embedding \
    OLLAMA_MODELS=/models \
    OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_NO_CLOUD=1 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_MAX_LOADED_MODELS=1 \
    OLLAMA_MAX_QUEUE=8 \
    OLLAMA_CONTEXT_LENGTH=2048 \
    OLLAMA_KEEP_ALIVE=10m
USER 10001:10001
ENTRYPOINT ["/opt/embedding/bootstrap.sh"]
HEALTHCHECK --interval=15s --timeout=5s --start-period=10m --retries=3 CMD ["/opt/embedding/health.sh"]
