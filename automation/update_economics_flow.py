#!/usr/bin/env python3
"""Fetch economics and investing feeds and write Obsidian notes.

The script intentionally uses only Python standard-library modules.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_write_text(path: Path, content: str) -> None:
    """Write a complete file and atomically replace the previous version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def slugify(value: str, max_len: int = 90) -> str:
    value = clean_text(value)
    value = re.sub(r"[\\/:*?\"<>|#^\[\]]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return (value or "untitled")[:max_len].strip()


def fetch(url: str, timeout: int = 25) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Obsidian-Economics-Investing-InfoFlow/1.0 (+local research workflow)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


FETCH_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    OSError,
    ET.ParseError,
    ValueError,
)


class SourceFetchError(RuntimeError):
    def __init__(self, source_name: str, attempts: int, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.source_name = source_name
        self.attempts = attempts
        self.cause = cause


def retryable_fetch_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 425, 429} or exc.code >= 500
    return True


def fetch_source(
    source: dict[str, Any],
    *,
    timeout: int,
    attempts: int,
    retry_delay: float,
) -> tuple[list[dict[str, Any]], int]:
    attempts = max(1, attempts)
    source_name = source.get("name", "Unknown source")
    for attempt in range(1, attempts + 1):
        try:
            return parse_feed(fetch(source["url"], timeout=timeout), source), attempt
        except FETCH_ERRORS as exc:
            if attempt >= attempts or not retryable_fetch_error(exc):
                raise SourceFetchError(source_name, attempt, exc) from exc
            delay = retry_delay * (2 ** (attempt - 1))
            print(
                f"Retrying {source_name} after attempt {attempt}/{attempts}: {exc}; "
                f"sleeping {delay:g}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    return [], attempts


def first_text(node: ET.Element, paths: list[str]) -> str:
    for path in paths:
        found = node.find(path, NS)
        if found is not None and found.text:
            return clean_text(found.text)
    return ""


def first_link_atom(entry: ET.Element) -> str:
    for link in entry.findall("atom:link", NS):
        href = link.attrib.get("href", "")
        rel = link.attrib.get("rel", "alternate")
        if href and rel == "alternate":
            return href
    link = entry.find("atom:link", NS)
    return link.attrib.get("href", "") if link is not None else ""


def parse_feed(raw: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    items: list[dict[str, Any]] = []
    source_name = source["name"]

    if root.tag.endswith("feed"):
        for entry in root.findall("atom:entry", NS):
            title = first_text(entry, ["atom:title"])
            link = first_link_atom(entry)
            published = first_text(entry, ["atom:published", "atom:updated"])
            summary = first_text(entry, ["atom:summary", "atom:content"])
            authors = [
                clean_text(author.findtext("atom:name", default="", namespaces=NS))
                for author in entry.findall("atom:author", NS)
            ]
            items.append(
                {
                    "title": title,
                    "url": link,
                    "published": published,
                    "summary": summary,
                    "authors": [a for a in authors if a],
                    "source": source_name,
                    "source_weight": int(source.get("weight", 1)),
                }
            )
        return items

    channel = root.find("channel")
    rss_items = channel.findall("item") if channel is not None else root.findall(".//item")
    for item in rss_items:
        title = first_text(item, ["title"])
        link = first_text(item, ["link", "guid"])
        published = first_text(item, ["pubDate", "dc:date"])
        summary = first_text(item, ["description", "content:encoded"])
        creator = first_text(item, ["dc:creator", "author"])
        items.append(
            {
                "title": title,
                "url": link,
                "published": published,
                "summary": summary,
                "authors": [creator] if creator else [],
                "source": source_name,
                "source_weight": int(source.get("weight", 1)),
            }
        )
    return items


def item_id(item: dict[str, Any]) -> str:
    key = item.get("url") or f"{item.get('source')}:{item.get('title')}:{item.get('published')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def score_item(item: dict[str, Any], ranking_terms: dict[str, int]) -> int:
    haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    score = int(item.get("source_weight", 1))
    for term, weight in ranking_terms.items():
        if term.lower() in haystack:
            score += int(weight)
    return score


def concept_links(
    item: dict[str, Any],
    concept_map: dict[str, list[str]],
    default_concept: str,
) -> list[str]:
    haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    links = []
    for note, terms in concept_map.items():
        if any(term.lower() in haystack for term in terms):
            links.append(note)
    return links or [default_concept]


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def note_body(item: dict[str, Any], concepts: list[str], score: int, run_date: str) -> str:
    title = item["title"]
    concept_line = ", ".join(f"[[{concept}]]" for concept in concepts)
    authors = ", ".join(item.get("authors") or [])
    summary = textwrap.fill(item.get("summary") or "暂无摘要。", width=88)
    return f"""---
type: update-item
tags: [update, economics, investing]
source: {yaml_string(item.get("source", ""))}
url: {yaml_string(item.get("url", ""))}
published: {yaml_string(item.get("published", ""))}
score: {score}
created: {run_date}
concepts: [{", ".join(yaml_string(c) for c in concepts)}]
---

# {title}

## 为什么重要

自动筛选分数：{score}

连接概念：{concept_line}

## 摘要

{summary}

## 来源

- Source: {item.get("source", "")}
- URL: {item.get("url", "")}
{f"- Authors: {authors}" if authors else ""}
- Published: {item.get("published", "")}

## 我的判断

- [ ] 是否改变 [[宏观指标仪表盘]] 的状态？
- [ ] 是否影响 [[资产配置]] 或 [[债券久期与利率风险]]？
- [ ] 是否只是短期噪声？

## 后续追踪

- 下一次数据发布日期：
- 需要更新的核心节点：
- 对 [[个人投资政策声明 IPS]] 的影响：
"""


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} {counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def write_update_notes(
    vault: Path,
    selected: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    run_id: str,
    candidate_count: int,
    failures: list[str],
) -> list[Path]:
    today = dt.date.today().isoformat()
    item_dir = vault / "30_Updates" / today
    if selected:
        item_dir.mkdir(parents=True, exist_ok=True)
    digest_title = config.get("digest_title", "经济投资简报")
    written: list[Path] = []
    run_status = "failed" if failures and candidate_count == 0 else "degraded" if failures else "success"
    digest_lines = [
        "---",
        "type: daily-update",
        "tags: [update, economics, investing]",
        f"created: {today}",
        f"run_id: {run_id}",
        f"status: {run_status}",
        f"candidate_count: {candidate_count}",
        f"selected_count: {len(selected)}",
        f"failed_source_count: {len(failures)}",
        "---",
        "",
        f"# {today} {digest_title}",
        "",
        "## 抓取状态",
        "",
        f"- 候选条目：{candidate_count}",
        f"- 入选条目：{len(selected)}",
        f"- 失败信息源：{len(failures)}",
    ]
    if failures:
        digest_lines.extend(["", *[f"  - {failure}" for failure in failures]])
    digest_lines.extend(
        [
            "",
        "## 高价值条目",
        "",
        ]
    )

    for item in selected:
        score = item["score"]
        concepts = item["concepts"]
        safe_title = slugify(item["title"])
        note_path = item_dir / f"{safe_title} [{item['id']}].md"
        atomic_write_text(note_path, note_body(item, concepts, score, today))
        written.append(note_path)
        concept_line = " ".join(f"[[{concept}]]" for concept in concepts)
        link_path = f"30_Updates/{today}/{note_path.stem}"
        digest_lines.extend(
            [
                f"### [[{link_path}|{item['title']}]]",
                "",
                f"- Score: {score}",
                f"- Source: {item.get('source', '')}",
                f"- Concepts: {concept_line}",
                f"- URL: {item.get('url', '')}",
                "",
            ]
        )

    if not selected:
        if failures and candidate_count == 0:
            digest_lines.extend(["> 本次所有信息源均抓取失败，自动化将按退避策略重试。", ""])
        else:
            digest_lines.extend(["> 本次抓取成功，但没有达到阈值且尚未收录的新条目。", ""])

    digest_lines.extend(
        [
            "## 复盘",
            "",
            "- 哪些信息改变了宏观六格？",
            "- 哪些信息只适合观察，不应触发交易？",
            "- 是否需要更新 IPS、资产配置或再平衡阈值？",
            "",
        ]
    )

    digest_path = vault / "30_Updates" / f"{today} {digest_title}.md"
    atomic_write_text(digest_path, "\n".join(digest_lines))
    written.append(digest_path)
    return written


def validate_daily_report(
    digest_path: Path,
    selected: list[dict[str, Any]],
    *,
    run_id: str,
    candidate_count: int,
) -> None:
    if not digest_path.exists() or digest_path.stat().st_size == 0:
        raise ValueError(f"Daily report is missing or empty: {digest_path}")
    content = digest_path.read_text(encoding="utf-8")
    required_lines = {
        f"run_id: {run_id}",
        f"candidate_count: {candidate_count}",
        f"selected_count: {len(selected)}",
    }
    missing = sorted(line for line in required_lines if line not in content)
    if missing:
        raise ValueError(f"Daily report metadata mismatch: {', '.join(missing)}")
    for item in selected:
        safe_title = slugify(item["title"])
        item_path = digest_path.parent / dt.date.today().isoformat() / f"{safe_title} [{item['id']}].md"
        if not item_path.exists() or item_path.stat().st_size == 0:
            raise ValueError(f"Linked item note is missing or empty: {item_path}")
        expected_link = f"[[30_Updates/{dt.date.today().isoformat()}/{item_path.stem}|"
        if expected_link not in content:
            raise ValueError(f"Daily report is missing item link: {item_path.stem}")


def update_index(vault: Path) -> None:
    moc = vault / "10_MOCs" / "MOC - 经济学与投资.md"
    updates = sorted((vault / "30_Updates").glob("* 经济投资简报*.md"), reverse=True)
    existing = moc.read_text(encoding="utf-8") if moc.exists() else "# MOC - 经济学与投资\n"
    marker = "\n## 最近更新\n"
    base = existing.split(marker)[0].rstrip()
    lines = [base, "", "## 最近更新", ""]
    for path in updates[:20]:
        lines.append(f"- [[{path.stem}]]")
    lines.append("")
    atomic_write_text(moc, "\n".join(lines))


def run_locked(args: argparse.Namespace, vault: Path) -> int:
    config_path = Path(args.config).expanduser().resolve() if args.config else vault / "40_Sources" / "economics_sources.json"
    state_path = vault / "state" / "seen_economics.json"
    run_state_path = vault / "state" / "economics_last_run.json"
    started_at = dt.datetime.now().astimezone()
    run_id = f"{started_at.strftime('%Y%m%dT%H%M%S%z')}-{os.getpid()}"
    run_record: dict[str, Any] = {
        "run_id": run_id,
        "run_date": started_at.date().isoformat(),
        "status": "running",
        "stage": "config",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": None,
        "source_attempts": {},
        "candidate_count": 0,
        "selected_count": 0,
        "top_title": "",
        "failures": [],
        "output_path": None,
        "validated": False,
        "error": "",
    }

    def persist_run_record() -> bool:
        if args.dry_run:
            return True
        try:
            write_json(run_state_path, run_record)
            return True
        except OSError as exc:
            print(f"Run-state persistence failed: {exc}", file=sys.stderr)
            return False

    config = read_json(config_path, {})
    if not config:
        error = f"Missing config: {config_path}"
        run_record.update(
            status="failed",
            stage="config",
            finished_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            error=error,
        )
        persist_run_record()
        print(error, file=sys.stderr)
        return 2

    state = read_json(state_path, {"seen": {}})
    seen: dict[str, Any] = state.setdefault("seen", {})
    failures: list[str] = []
    candidates: list[dict[str, Any]] = []
    sources = config.get("feeds", [])
    source_attempts: dict[str, int] = {}
    run_record["stage"] = "fetch"
    if not persist_run_record():
        return 1

    for source in sources:
        source_name = source.get("name", "Unknown source")
        try:
            source_items, attempts = fetch_source(
                source,
                timeout=args.timeout,
                attempts=args.retries,
                retry_delay=args.retry_delay,
            )
            candidates.extend(source_items)
            source_attempts[source_name] = attempts
            time.sleep(args.sleep)
        except SourceFetchError as exc:
            source_attempts[source_name] = exc.attempts
            failures.append(f"{source_name}: {exc.cause} (after {exc.attempts} attempts)")

    ranking_terms = config.get("ranking_terms", {})
    concept_map = config.get("concept_links", {})
    default_concept = config.get("default_concept", "经济学与投资核心知识地图")
    min_score = int(args.min_score or config.get("min_score", 3))
    max_items = int(args.max_items or config.get("max_items_per_run", 20))
    selected = []
    same_day_fallback = []
    selected_ids = set()
    today = dt.date.today().isoformat()

    for item in candidates:
        if not item.get("title"):
            continue
        haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        if any(term.lower() in haystack for term in config.get("exclude_terms", [])):
            continue
        ident = item_id(item)
        if ident in selected_ids:
            continue
        score = score_item(item, ranking_terms)
        if score < min_score:
            continue
        item["id"] = ident
        item["score"] = score
        item["concepts"] = concept_links(item, concept_map, default_concept)
        if ident in seen and not args.include_seen:
            last_included = str(seen[ident].get("last_included") or seen[ident].get("first_seen") or "")
            if last_included.startswith(today):
                same_day_fallback.append(item)
                selected_ids.add(ident)
            continue
        selected.append(item)
        selected_ids.add(ident)

    selected.sort(key=lambda item: item["score"], reverse=True)
    same_day_fallback.sort(key=lambda item: item["score"], reverse=True)
    if not selected and same_day_fallback and not args.include_seen:
        selected = same_day_fallback
    selected = selected[:max_items]

    all_sources_failed = bool(sources) and len(failures) == len(sources)
    run_status = "failed" if all_sources_failed else "degraded" if failures else "success"
    run_record.update(
        stage="ranking",
        source_attempts=source_attempts,
        candidate_count=len(candidates),
        selected_count=len(selected),
        top_title=selected[0]["title"] if selected else "",
        failures=failures,
    )

    if args.dry_run:
        for item in selected:
            print(f"{item['score']:>3} | {item['source']} | {item['title']}")
        print(f"Selected {len(selected)} items from {len(candidates)} candidates.")
        if failures:
            print("\nFailures:", file=sys.stderr)
            for failure in failures:
                print(f"- {failure}", file=sys.stderr)
        return 0

    now = dt.datetime.now().isoformat(timespec="seconds")

    def fail_run(stage: str, error: str, *, output_path: str | None = None, code: int = 1) -> int:
        state.update(
            {
                "last_run": now,
                "last_run_id": run_id,
                "last_status": "failed",
                "last_error": error,
                "last_failures": failures,
                "last_source_count": len(sources),
                "last_failed_source_count": len(failures),
                "last_source_attempts": source_attempts,
                "last_candidate_count": len(candidates),
                "last_selected_count": 0,
                "last_top_title": "",
                "last_top_score": None,
                "last_output_path": output_path,
                "last_validated": False,
            }
        )
        try:
            write_json(state_path, state)
        except OSError as state_exc:
            print(f"State persistence also failed: {state_exc}", file=sys.stderr)
        run_record.update(
            status="failed",
            stage=stage,
            finished_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            output_path=output_path,
            validated=False,
            error=error,
        )
        persist_run_record()
        print(error, file=sys.stderr)
        return code

    run_record["stage"] = "write"
    persist_run_record()
    try:
        written = write_update_notes(
            vault,
            selected,
            config,
            run_id=run_id,
            candidate_count=len(candidates),
            failures=failures,
        )
    except OSError as exc:
        return fail_run("write", f"Writing daily report failed: {exc}")

    index_error = ""
    run_record["stage"] = "index"
    persist_run_record()
    for index_attempt in range(1, 3):
        try:
            update_index(vault)
            index_error = ""
            break
        except OSError as exc:
            index_error = f"Index update failed: {exc}"
            if index_attempt < 2:
                time.sleep(2)
    if index_error:
        return fail_run("index", index_error, output_path=str(written[-1]))

    run_record["stage"] = "validate"
    persist_run_record()
    try:
        validate_daily_report(
            written[-1],
            selected,
            run_id=run_id,
            candidate_count=len(candidates),
        )
    except (OSError, ValueError) as exc:
        return fail_run("validate", f"Daily report validation failed: {exc}", output_path=str(written[-1]))

    for item in selected:
        previous = seen.get(item["id"], {})
        seen[item["id"]] = {
            "title": item["title"],
            "url": item.get("url", ""),
            "source": item.get("source", ""),
            "first_seen": previous.get("first_seen", now),
            "last_included": now,
            "score": item["score"],
        }
    state.update(
        {
            "last_run": now,
            "last_run_id": run_id,
            "last_status": run_status,
            "last_error": "",
            "last_failures": failures,
            "last_source_count": len(sources),
            "last_failed_source_count": len(failures),
            "last_source_attempts": source_attempts,
            "last_candidate_count": len(candidates),
            "last_selected_count": len(selected),
            "last_top_title": selected[0]["title"] if selected else "",
            "last_top_score": selected[0]["score"] if selected else None,
            "last_output_path": str(written[-1]),
            "last_validated": True,
            "last_warnings": [],
        }
    )
    try:
        write_json(state_path, state)
    except OSError as exc:
        return fail_run("state", f"State persistence failed: {exc}", output_path=str(written[-1]))

    run_record.update(
        status=run_status,
        stage="complete",
        finished_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        output_path=str(written[-1]),
        validated=True,
        error="",
    )
    if not persist_run_record():
        return 1

    print(f"Selected {len(selected)} items from {len(candidates)} candidates.")
    for path in written:
        print(path)
    if failures:
        print("\nFeed failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
    if all_sources_failed:
        return os.EX_TEMPFAIL
    return 0


def run(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    lock_path = vault / "state" / "economics_daily.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"Another economics update is already running: {lock_path}", file=sys.stderr)
        return os.EX_TEMPFAIL
    except OSError as exc:
        print(f"Cannot acquire economics update lock {lock_path}: {exc}", file=sys.stderr)
        return 1

    try:
        return run_locked(args, vault)
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", default=".", help="Obsidian vault path")
    parser.add_argument("--config", default="", help="Optional economics_sources.json path")
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--min-score", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--sleep", type=float, default=0.8, help="Pause between feeds")
    parser.add_argument("--retries", type=int, default=3, help="Total attempts per feed")
    parser.add_argument("--retry-delay", type=float, default=3.0, help="Initial retry delay in seconds")
    parser.add_argument("--include-seen", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
