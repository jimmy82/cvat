# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from cvat.apps.ml_processing.utils import sign_payload, verify_signature


def test_verify_signature_accepts_valid_signature():
    secret = "s3cr3t"
    body = b'{"status": "succeeded"}'
    signature = sign_payload(secret, body)
    assert signature.startswith("sha256=")
    assert verify_signature(secret, body, signature)


def test_verify_signature_rejects_missing_signature():
    assert not verify_signature("s3cr3t", b"{}", None)
    assert not verify_signature("s3cr3t", b"{}", "")


def test_verify_signature_rejects_tampered_body():
    secret = "s3cr3t"
    signature = sign_payload(secret, b'{"status": "succeeded"}')
    assert not verify_signature(secret, b'{"status": "failed"}', signature)


def test_verify_signature_rejects_wrong_secret():
    body = b'{"status": "succeeded"}'
    signature = sign_payload("secret-a", body)
    assert not verify_signature("secret-b", body, signature)


def test_verify_signature_rejects_missing_secret():
    body = b"{}"
    assert not verify_signature("", body, sign_payload("whatever", body))
