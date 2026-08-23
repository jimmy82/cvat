# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""
HMAC-SHA256 signing/verification helpers, replicating the exact scheme used by
`cvat.apps.webhooks.utils.perform_webhook_request`: header name `X-Signature-256`,
value `"sha256=" + hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()`.

For outbound requests (`rq.py`), we sign the exact bytes we send.
For inbound callback verification (`views.py`), the signature MUST be computed over
the raw `request.body` bytes -- not a re-serialized `json.dumps(request.data)`, which
can differ in key order/whitespace from what the sender actually signed and would
silently break verification.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Signature-256"


def sign_payload(secret: str, body_bytes: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body_bytes: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header:
        return False

    expected = sign_payload(secret, body_bytes)
    return hmac.compare_digest(expected, signature_header)
