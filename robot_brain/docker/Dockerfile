FROM docker.io/vllm/vllm-openai-rocm:latest

USER root

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      tmux \
      git \
      openssh-server \
      htop \
      tree \
      net-tools \
      nano \
      curl \
      jq \
      sqlite3 \
      procps \
      psmisc \
      lsof \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd

# vLLM's ROCm image already provides vLLM, FastAPI, Uvicorn, PyTorch and ROCm.
# Install only Robot Brain orchestration / validation dependencies.
RUN python3 -m pip install --no-cache-dir \
      'langgraph>=1.0,<2.0' \
      'PyYAML>=6.0,<7.0' \
      'defusedxml>=0.7,<1.0' \
      'huggingface_hub>=0.34' \
      'pytest>=8,<10'

# Manta may override WORKDIR; all scripts still use an absolute PROJECT_ROOT.
WORKDIR /mlsteam/workspace

# The base vLLM image has a serving entrypoint. This project is intentionally
# started manually after entering the Manta LAB.
ENTRYPOINT []
CMD ["sleep", "infinity"]
