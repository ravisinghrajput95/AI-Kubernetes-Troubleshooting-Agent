"""The agent gateway: where cluster agents connect.

Loaded only when `AGENT_GATEWAY_PORT` is set, so a deployment using the local
kubeconfig never imports grpc.
"""
