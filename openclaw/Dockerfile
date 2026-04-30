FROM node:24-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g openclaw@2026.4.27

ENV OPENCLAW_STATE_DIR=/openclaw-state

EXPOSE 18789

ENTRYPOINT ["openclaw"]
CMD ["gateway", "--bind", "lan", "--force"]
