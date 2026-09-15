#!/usr/bin/python3
"""Optional synthetic Stage 6 search benchmark; not discovered by unittest."""

from pathlib import Path
import sys
import tempfile
import time

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "helper"))

from omatree_core.sqlite_query import SQLiteSnapshotReader
from omatree_core.sqlite_snapshot import SQLiteSnapshotWriter


def record(path, parent, name, size, children=0):
    return {"path": path, "parentPath": parent, "name": name, "bytes": size,
            "directFilesBytes": 0, "childDirectoryCount": children,
            "warningCount": 0}


def timed(call):
    started = time.perf_counter()
    value = call()
    return value, (time.perf_counter() - started) * 1000


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 50_000
    children = count - 1
    with tempfile.TemporaryDirectory(prefix="omatree-search-benchmark-") as runtime:
        root = "/synthetic"
        writer = SQLiteSnapshotWriter.create(Path(runtime))
        build_started = time.perf_counter()
        for index in range(children):
            name = f"common-{index:07d}"
            if index == children - 1:
                name = "rare-target-at-canonical-end"
            writer.insert_directory(record(
                f"{root}/{name}", root, name, children - index
            ))
        writer.insert_directory(record(root, None, "common-synthetic", count, children))
        build_ms = (time.perf_counter() - build_started) * 1000
        finalize_started = time.perf_counter()
        snapshot = writer.finalize(root, count)
        finalize_ms = (time.perf_counter() - finalize_started) * 1000
        with SQLiteSnapshotReader.open(snapshot.path) as reader:
            common, common_ms = timed(lambda: reader.search("common"))
            later, later_ms = timed(
                lambda: reader.search("common", common.continuation)
            )
            rare, rare_ms = timed(lambda: reader.search("rare-target"))
            no_match, no_match_ms = timed(lambda: reader.search("does-not-exist"))
            reveal, reveal_ms = timed(
                lambda: reader.children_at(f"{root}/rare-target-at-canonical-end")
            )
        size = snapshot.path.stat().st_size
        snapshot.delete()
    print(
        f"rows={count} dbBytes={size} buildMs={build_ms:.3f} finalizeMs={finalize_ms:.3f} commonFirstMs={common_ms:.3f} "
        f"commonLaterMs={later_ms:.3f} rareEndMs={rare_ms:.3f} "
        f"noMatchMs={no_match_ms:.3f} revealLateMs={reveal_ms:.3f} "
        f"pageRows={len(common.rows)}/{len(later.rows)} "
        f"rareRows={len(rare.rows)} noMatchRows={len(no_match.rows)} "
        f"revealRows={len(reveal.rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
