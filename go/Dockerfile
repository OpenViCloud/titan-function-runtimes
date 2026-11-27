# Runtime go of OVC Function: no interpreter and no Go toolchain. The customer's `bootstrap` (a static binary built with
# the function package of this repository) is a layer under /function, added when a function image is composed (titan-ops
# features/ovc-function/design.md#code-source). The base is pinned by digest (the linux/amd64 manifest of
# distroless/static-debian13:nonroot): CA certificates and time zone data, no shell.
FROM --platform=linux/amd64 gcr.io/distroless/static-debian13@sha256:89e00a7e78df216bcbf495e7f43135f03ddbd3e98024a4104731872d6c8cf3c6

USER 1000:1000
ENTRYPOINT ["/function/bootstrap"]
