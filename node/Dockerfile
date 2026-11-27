# Runtime nodejs26 of OVC Function. The base is pinned by digest (the linux/amd64 manifest of node:26-slim).
# The customer's code is a layer under /function, added when a function image is composed (titan-ops
# features/ovc-function/design.md#code-source); this image holds the interpreter and the adapter.
FROM --platform=linux/amd64 node:26-slim@sha256:73c2e404ecdc6a8a8d7febe36ac62fab221fb61627659334a8855a2c198da7a0

COPY adapter.mjs /opt/titan/adapter.mjs

USER 1000:1000
ENTRYPOINT ["node", "/opt/titan/adapter.mjs"]
