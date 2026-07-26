"""The wire contract for the cluster-agent protocol.

Schemas live in `/proto`; `gen/` holds the committed bindings (regenerate with
`scripts/generate_proto.py`). Nothing transports these messages yet — M4 does.
The contract lands first so it can be reviewed on its own terms.
"""

from app.wire.codec import (
    WireDecodeError,
    WireEncodeError,
    WireError,
    decode_evidence,
    decode_resource_ref,
    encode_evidence,
    encode_resource_ref,
)

__all__ = [
    "WireDecodeError",
    "WireEncodeError",
    "WireError",
    "decode_evidence",
    "decode_resource_ref",
    "encode_evidence",
    "encode_resource_ref",
]
