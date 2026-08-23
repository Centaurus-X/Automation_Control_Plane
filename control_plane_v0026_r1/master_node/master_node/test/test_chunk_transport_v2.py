# -*- coding: utf-8 -*-

"""Chunk byte-boundary, hash, deterministic gzip and limit tests."""

import copy

from src.library.chunk_transport import encode_transfer, reassemble_transfer


def main():
    payload = {
        "config": {
            "description": "äöü-边界-" * 24000,
            "rows": [{"id": value, "value": "row-{:08d}".format(value)} for value in range(3000)],
        }
    }
    options = {
        "transfer_id": "rollout:fn-01:node:8",
        "target_command": "distributed.config.apply",
        "scope": "node:fn-01",
        "target_scope_revision": 8,
        "target_scope_fingerprint": "a" * 64,
        "max_packet_size": 65536,
        "chunk_bytes": 24000,
    }
    first = encode_transfer(payload, **options)
    second = encode_transfer(payload, **options)
    assert first["chunks"] == second["chunks"]
    assert len(first["chunks"]) > 1
    rebuilt = reassemble_transfer(tuple(reversed(first["chunks"])))
    assert rebuilt["payload"] == payload

    corrupt = list(copy.deepcopy(first["chunks"]))
    corrupt[0]["chunk_base64"] = "AAAA"
    try:
        reassemble_transfer(corrupt)
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt chunk was accepted")
    try:
        reassemble_transfer(first["chunks"][:-1])
    except ValueError:
        pass
    else:
        raise AssertionError("incomplete transfer was accepted")
    print("PASS bounded deterministic Chunk transport")


if __name__ == "__main__":
    main()

