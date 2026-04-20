FROM python:3.14-slim AS builder

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

FROM python:3.14-slim

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ ./src/

# Non-root user
RUN groupadd -r mcp && useradd -r -g mcp mcp
USER mcp

EXPOSE 8080

ENTRYPOINT ["python", "-m", "src.server"]
