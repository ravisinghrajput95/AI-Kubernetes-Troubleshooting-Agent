"""Cryptographic identity for cluster agents.

Imported only by the gateway and the `agentctl` CLI, both of which are opt-in,
so a deployment reading a local kubeconfig never loads any of it.
"""
