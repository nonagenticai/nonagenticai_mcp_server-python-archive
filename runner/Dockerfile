ARG BASE_REGISTRY=docker.io
FROM ${BASE_REGISTRY}/library/python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/mcp
COPY runner/__init__.py runner/entrypoint.py runner/sdk.py runner/protocol.py ./runner/
USER 1000:1000
ENTRYPOINT ["python", "-I", "-c", "import sys; sys.path.insert(0, '/opt/mcp'); from runner.entrypoint import main; main()"]
