# The cluster agent, as something you can actually install.
#
# Until now the agent was a binary you built and copied by hand, which is a
# reasonable answer for one cluster and no answer at all for a fleet. The
# console's onboarding page generates a manifest that references this image.
#
# Distroless and non-root on purpose: this runs inside a customer's cluster,
# and the smallest thing that can serve reads is the right thing to ask them to
# accept. There is no shell in the final image.

FROM golang:1.26-alpine AS build

WORKDIR /src

# Dependencies first, so a source-only change does not re-download the module
# graph — client-go is not small.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# Static, stripped, reproducible enough to compare across builds.
ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux go build \
    -trimpath \
    -ldflags="-s -w -X main.version=${VERSION}" \
    -o /out/agent ./cmd/agent

FROM gcr.io/distroless/static-debian12:nonroot

# 65532 is distroless' `nonroot`. The manifest's securityContext names the same
# uid, and the identity volume is chowned to it.
USER 65532:65532

COPY --from=build /out/agent /usr/local/bin/agent

# Where the agent keeps the private key it generates and never transmits.
VOLUME ["/var/lib/k8s-ops-agent"]

ENTRYPOINT ["/usr/local/bin/agent"]
