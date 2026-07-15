import json
import os
import filelock

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


def _path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


def read_all(filename: str) -> list:
    path = _path(filename)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_all(filename: str, data: list) -> None:
    path = _path(filename)
    lock = filelock.FileLock(path + ".lock")
    with lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def find_by_id(filename: str, record_id: str) -> dict | None:
    return next((r for r in read_all(filename) if r["id"] == record_id), None)


def update_by_id(filename: str, record_id: str, updates: dict) -> dict | None:
    data = read_all(filename)
    for i, record in enumerate(data):
        if record["id"] == record_id:
            data[i] = {**record, **updates}
            write_all(filename, data)
            return data[i]
    return None


def append(filename: str, record: dict) -> dict:
    data = read_all(filename)
    data.append(record)
    write_all(filename, data)
    return record


def delete_by_id(filename: str, record_id: str) -> bool:
    data = read_all(filename)
    remaining = [r for r in data if r["id"] != record_id]
    if len(remaining) == len(data):
        return False
    write_all(filename, remaining)
    return True
