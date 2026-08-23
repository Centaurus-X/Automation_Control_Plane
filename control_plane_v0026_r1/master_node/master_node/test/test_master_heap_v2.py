# -*- coding: utf-8 -*-

"""Concurrent atomic heap pointer-swap and snapshot-isolation test."""

import threading

from src.library.master_heap_store import create_master_heap_store


def main():
    heap = create_master_heap_store()
    errors = []

    def writer():
        for revision in range(1, 301):
            heap["replace"](
                {"global_revision": revision, "fingerprint": str(revision),
                 "config_data": {"revision": revision}},
                {"node:fn-01": {
                    "scope": "node:fn-01", "scope_revision": revision,
                    "fingerprint": str(revision), "config": {"revision": revision},
                }},
            )

    def reader():
        for _iteration in range(1000):
            global_snapshot = heap["get_global"]()
            scope_snapshot = heap["get_scope"]("node:fn-01")
            if global_snapshot:
                revision = global_snapshot["global_revision"]
                if global_snapshot["config_data"]["revision"] != revision:
                    errors.append("torn global snapshot")
            if scope_snapshot:
                revision = scope_snapshot["scope_revision"]
                if scope_snapshot["config"]["revision"] != revision:
                    errors.append("torn scope snapshot")

    threads = [threading.Thread(target=writer, name="heap-writer")]
    threads.extend(
        threading.Thread(target=reader, name="heap-reader-" + str(index))
        for index in range(4))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert heap["snapshot"]()["global_revision"] == 300
    print("PASS no-GIL-safe atomic Master heap snapshots")


if __name__ == "__main__":
    main()
