"""Manually import a local article from sources/ into the podcast queue."""

import argparse
import hashlib
from pathlib import Path

from podcast_sources import enqueue_source


BASE = Path(__file__).resolve().parent
SOURCE_DIR = BASE / "sources"


def import_file(path: Path, source_dir: Path = SOURCE_DIR):
    source_dir = source_dir.resolve()
    path = Path(path)
    if not path.is_absolute() and (Path.cwd() / path).resolve().is_relative_to(source_dir):
        path = (Path.cwd() / path).resolve()
    else:
        path = (source_dir / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(source_dir) or not path.is_file() or path.is_symlink():
        raise ValueError("Article must be a regular file inside sources/")
    if path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("Supported article formats: .md and .txt")
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError("Article is empty")
    relative = path.relative_to(source_dir).as_posix()
    source_id = "src-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    title = next((line.removeprefix("# ").strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
    return enqueue_source(source_id=source_id, title=title, author="", items=[{"type": "para", "text": text}])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("article", type=Path, help="Path to a .md or .txt article inside sources/")
    args = parser.parse_args()
    task = import_file(args.article)
    print(task["id"])


if __name__ == "__main__":
    main()
