"""Application failures translated to gRPC status without exposing payloads."""
from __future__ import annotations


class NativeRpcError(Exception):
    def __init__(self, status: int, *, diagnostic=None):
        super().__init__('Local RPC rejected')
        self.status = status
        self.diagnostic = diagnostic
