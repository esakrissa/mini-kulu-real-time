# The hosted web client: token_server.py serving web/ and POST /api/token on Cloud Run.
# Only the token server's few dependencies, not the agent's: a ~60 MB image.
#   docker build -f client.Dockerfile -t mini-kulu-client .
FROM python:3.12.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/agent
COPY agent/requirements-client.txt .
RUN pip install --require-hashes -r requirements-client.txt

COPY agent/token_server.py agent/access.py agent/tokens.py ./
COPY web/index.html web/app.js /app/web/

# Cloud Run sets PORT (8080). HOST=0.0.0.0 to listen beyond the container's loopback.
ENV HOST=0.0.0.0
USER nobody
CMD ["python", "token_server.py"]
