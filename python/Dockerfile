# Runtime python3.15 of OVC Function. The base is pinned by digest (the linux/amd64 manifest of python:3.15-slim).
# The customer's code is a layer under /function, added when a function image is composed (titan-ops
# features/ovc-function/design.md#code-source); this image holds the interpreter and the adapter.
FROM --platform=linux/amd64 python:3.15-slim@sha256:3c5426211a36454d7a58b31e306cfefaac2c63b016e9f1a067b652ec602a4d15

# /function is read-only in the pod, so bytecode could not be written there anyway
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY adapter.py /opt/titan/adapter.py

USER 1000:1000
ENTRYPOINT ["python", "/opt/titan/adapter.py"]
