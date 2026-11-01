"""The local adapters as a separate program, the way a third-party adapter would run. Test helper."""
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anton.adapters.local import LocalCodeHost, LocalTracker  # noqa: E402
from anton.ports import AdapterError, PrRef  # noqa: E402


def plain(value):
    if is_dataclass(value):
        return asdict(value)
    return [plain(v) for v in value] if isinstance(value, list) else value


def main(root: str, bare: str) -> None:
    tracker, host = LocalTracker(Path(root)), LocalCodeHost(Path(root), Path(bare))
    for line in sys.stdin:
        request = json.loads(line)
        method, params = request["method"], request["params"]
        if method == "crash":
            sys.exit(1)
        if method == "garbage":
            print("this is no JSON", flush=True)
            continue
        if method == "hang":
            sys.stdin.read()
        if "pr" in params:
            params["pr"] = PrRef(**params["pr"])
        target = tracker if hasattr(tracker, method) else host
        try:
            response = {"id": request["id"], "result": plain(getattr(target, method)(**params))}
        except AdapterError as e:
            response = {"id": request["id"], "error": {"kind": "adapter", "message": str(e)}}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
