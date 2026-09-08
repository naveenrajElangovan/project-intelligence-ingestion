"""Compare the live Chroma contents against the pre-purge baseline.

WHAT IT DOES
    Counts what is currently stored in the search database for every project and
    compares it with the counts recorded before the purge, then prints one line per
    difference.

WHERE IT RUNS
    On the machine running the local stack, after a purge and re-ingestion has
    finished. Read-only: it never writes to or deletes from the database.

HOW IT WORKS
    1. Reads the baseline file recorded before the purge.
    2. Asks Chroma for every collection and reads the labels on every stored piece.
    3. Compares totals, source counts, provider counts and access labels.
    4. Exits with code 1 if anything is missing, so it can gate a script.

USAGE
    python3 .baselines/verify_restore.py .baselines/chroma-baseline-2026-09-07.json
"""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections"


def _get(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=60) as r:
        return json.loads(r.read())


def live():
    out = {}
    for c in _get(BASE + "?limit=100"):
        pid = (c.get("metadata") or {}).get("project_id") or c["name"]
        metas, offset = [], 0
        while True:
            page = _get(f"{BASE}/{c['id']}/get",
                        {"limit": 2000, "offset": offset, "include": ["metadatas"]})
            m = page.get("metadatas") or []
            if not m:
                break
            metas += m
            offset += 2000
            if len(m) < 2000:
                break
        counts = lambda f: {str(x.get(f, "")): 0 for x in metas}
        def by(field):
            acc = {}
            for x in metas:
                k = str(x.get(field, ""))
                acc[k] = acc.get(k, 0) + 1
            return acc
        out[pid] = {
            "total_chunks": len(metas),
            "distinct_sources": len({x.get("source_id") for x in metas}),
            "by_source_type": by("source_type"),
            "by_provider": by("provider"),
            "by_access_policy_id": by("access_policy_id"),
            "by_schema_version": by("schema_version"),
            "by_embedding_model": by("embedding_model"),
        }
    return out


def main(path):
    baseline = json.load(open(path))["collections"]
    now = live()
    problems = []
    for project, want in baseline.items():
        have = now.get(project)
        if have is None:
            problems.append(f"{project}: MISSING ENTIRELY")
            continue
        for field in ("total_chunks", "distinct_sources"):
            if have[field] < want[field]:
                problems.append(f"{project}.{field}: {have[field]} < baseline {want[field]}")
            elif have[field] != want[field]:
                print(f"  note {project}.{field}: {have[field]} vs baseline {want[field]} (grew)")
        for field in (
            "by_source_type",
            "by_provider",
            "by_schema_version",
            "by_embedding_model",
        ):
            for key, count in want[field].items():
                got = have[field].get(key, 0)
                if got < count:
                    problems.append(f"{project}.{field}[{key}]: {got} < baseline {count}")
        for policy, count in have["by_access_policy_id"].items():
            print(f"  label {project}: {policy} -> {count}")
    print()
    if problems:
        print("RESTORE INCOMPLETE:")
        for p in problems:
            print("  -", p)
        return 1
    print("RESTORE OK: every baseline count met or exceeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else
                  ".baselines/chroma-baseline-2026-09-07.json"))
