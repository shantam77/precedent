FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY deploy/requirements.txt deploy/requirements.txt
RUN pip install --no-cache-dir -r deploy/requirements.txt
COPY precedent precedent
COPY deploy deploy
# The demo: mitmproxy at the commit our results were measured on.
ENV PRECEDENT_GRAPH=mitmproxy \
    PRECEDENT_REPO=/data/repo \
    PRECEDENT_REPO_URL=https://github.com/mitmproxy/mitmproxy \
    DEMO_REPO=https://github.com/mitmproxy/mitmproxy \
    DEMO_SHA=77355977e5ea540b51ed029a40ed1dc2f1c2ba7b \
    FALKOR_HOST=127.0.0.1
CMD ["sh", "deploy/start.sh"]
