"""可复现随机流派生：用 BLAKE2b 命名空间哈希生成相互独立的随机种子。

同一 base_seed 下，不同命名空间（到达流、各工位服务流、不同 replication）
得到互不相关的种子，保证实验可复现且随机流互不干扰。
"""
from __future__ import annotations

import hashlib


def derive_seed(base_seed: int, namespace: str) -> int:
    """Derive a stable 64-bit seed for an independent random stream."""

    payload = f"{base_seed}\0{namespace}".encode()
    digest = hashlib.blake2b(
        payload,
        digest_size=8,
        person=b"simlab-rng-v1",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)
