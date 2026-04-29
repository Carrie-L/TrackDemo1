#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_FIELDS = {
    "cardType": "cards.cardType",
    "onPlanID": "res.planId",
    "resID": "res.id",
    "operationCardId": "cards.cardId",
    "resName": "res.planName",
    "dismissLogic": "items.disappearType",
    "contentPoolId": "items.contentPool.contentPoolId",
    "channelId": "items.link.channelId",
    "subChannelId": "items.link.subChannelId",
}

EVENT_RE = re.compile(r"Event:\s*id:\s*(?P<event_id>[^\s]+)\s+data:\s*(?P<data>.+)")


@dataclass
class ParsedEvent:
    event_id: str
    event_name: str
    params: dict[str, str]
    raw_line: str


@dataclass
class CheckResult:
    field: str
    log_value: str | None
    expected_path: str
    expected_values: set[str]
    matched: bool
    detail: str


@dataclass
class EventReport:
    event: ParsedEvent
    event_label: str
    results: list[CheckResult]
    passed: bool


@dataclass
class MissingEvent:
    event_id: str
    event_label: str


def configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def normalize_event_file(data: Any) -> dict[str, str]:
    events: dict[str, str] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            event_id = item.get("eventId") or item.get("event_id") or item.get("id")
            event_name = item.get("eventName") or item.get("event_name") or item.get("name") or ""
            if event_id is not None:
                events[as_text(event_id)] = as_text(event_name)
    elif isinstance(data, dict):
        if "events" in data:
            events.update(normalize_event_file(data["events"]))
        else:
            for key, value in data.items():
                if isinstance(value, dict):
                    event_name = value.get("eventName") or value.get("name") or ""
                    events[as_text(key)] = as_text(event_name)
                else:
                    events[as_text(key)] = as_text(value)
    return events


def load_events(path: Path | None, cli_event_ids: list[str]) -> dict[str, str]:
    events: dict[str, str] = {}
    if path and path.exists():
        events.update(normalize_event_file(read_json(path)))
    for event_id in cli_event_ids:
        events[as_text(event_id)] = events.get(as_text(event_id), "")
    return events


def load_field_config(path: Path | None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "fields": dict(DEFAULT_FIELDS),
        "fieldsByEventId": {},
        "fieldMergeMode": "override",
        "caseGroups": [],
        "logParser": {},
        "lineFilters": {},
        "valueAliases": {},
    }
    if not path or not path.exists():
        return config

    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"Field mapping must be a JSON object: {path}")

    if (
        "fields" in data
        or "fieldsByEventId" in data
        or "fieldMergeMode" in data
        or "valueAliases" in data
        or "caseGroups" in data
        or "logParser" in data
        or "lineFilters" in data
    ):
        if isinstance(data.get("fields"), dict):
            config["fields"] = {as_text(k): as_text(v) for k, v in data["fields"].items()}
        if isinstance(data.get("fieldsByEventId"), dict):
            config["fieldsByEventId"] = data["fieldsByEventId"]
        if as_text(data.get("fieldMergeMode")):
            config["fieldMergeMode"] = as_text(data.get("fieldMergeMode"))
        if isinstance(data.get("caseGroups"), list):
            config["caseGroups"] = data["caseGroups"]
        if isinstance(data.get("logParser"), dict):
            config["logParser"] = data["logParser"]
        if isinstance(data.get("lineFilters"), dict):
            config["lineFilters"] = data["lineFilters"]
        if isinstance(data.get("valueAliases"), dict):
            config["valueAliases"] = data["valueAliases"]
    else:
        config["fields"] = {as_text(k): as_text(v) for k, v in data.items()}
    return config


def split_params(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None

    for char in text:
        if quote:
            buf.append(char)
            if char == quote:
                quote = None
            continue

        if char in ("'", '"'):
            quote = char
            buf.append(char)
            continue

        if char in "([{":
            depth += 1
            buf.append(char)
            continue

        if char in ")]}":
            depth = max(0, depth - 1)
            buf.append(char)
            continue

        if char == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue

        buf.append(char)

    if buf:
        parts.append("".join(buf).strip())
    return parts


def group_value(match: re.Match[str], group_name: str) -> str:
    if not group_name or group_name not in match.groupdict():
        return ""
    value = match.group(group_name)
    return value.strip() if value is not None else ""


def parser_option(parser_config: Any, key: str, default: str) -> str:
    if not isinstance(parser_config, dict):
        return default
    value = as_text(parser_config.get(key))
    return value if value else default


def parse_event_line(line: str, parser_config: Any = None) -> ParsedEvent | None:
    line = line.lstrip("\ufeff")
    line_regex = parser_option(parser_config, "lineRegex", "")
    if line_regex:
        try:
            pattern = re.compile(line_regex)
        except re.error as exc:
            raise ValueError(f"Invalid logParser.lineRegex {line_regex!r}: {exc}") from exc
        match = pattern.search(line)
    else:
        match = EVENT_RE.search(line)
    if not match:
        return None

    event_id_group = parser_option(parser_config, "eventIdGroup", "event_id")
    data_group = parser_option(parser_config, "dataGroup", "data")
    event_name_group = parser_option(parser_config, "eventNameGroup", "event_name")
    params_group = parser_option(parser_config, "paramsGroup", "params")
    key_value_separator = parser_option(parser_config, "keyValueSeparator", "=")

    event_id = group_value(match, event_id_group)
    if not event_id:
        raise ValueError(
            f"logParser did not capture event id. "
            f"Expected named group {event_id_group!r} in regex."
        )

    data = group_value(match, data_group)
    captured_event_name = group_value(match, event_name_group)
    captured_params = group_value(match, params_group)
    event_name = data
    param_text = captured_params

    left = data.find("(")
    right = data.rfind(")")
    if captured_event_name:
        event_name = captured_event_name
    elif left != -1 and right > left:
        event_name = data[:left].strip()
    elif not event_name:
        event_name = "UnknownEvent"

    if not param_text and left != -1 and right > left:
        param_text = data[left + 1 : right]
    elif not param_text and data and key_value_separator in data:
        param_text = data

    params: dict[str, str] = {}
    for part in split_params(param_text):
        if key_value_separator not in part:
            bare_key = part.strip()
            if bare_key:
                params[bare_key] = ""
            continue
        key, value = part.split(key_value_separator, 1)
        params[key.strip()] = value.strip()

    return ParsedEvent(event_id=event_id, event_name=event_name, params=params, raw_line=line.rstrip("\n"))


def text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [as_text(item) for item in value if as_text(item)]
    text = as_text(value)
    return [text] if text else []


def event_specific_patterns(raw: Any, event_id: str) -> list[str]:
    if not isinstance(raw, dict):
        return []
    return text_list(raw.get(event_id))


def any_regex_matches(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        try:
            if re.search(pattern, text):
                return pattern
        except re.error as exc:
            raise ValueError(f"Invalid regex {pattern!r}: {exc}") from exc
    return None


def event_passes_line_filters(
    event: ParsedEvent,
    line_filters: Any,
    cli_include: list[str],
    cli_exclude: list[str],
) -> tuple[bool, str]:
    if not isinstance(line_filters, dict):
        line_filters = {}

    raw_line = event.raw_line
    global_includes = text_list(line_filters.get("includeRegex")) + cli_include
    global_excludes = text_list(line_filters.get("excludeRegex")) + cli_exclude
    event_includes = event_specific_patterns(line_filters.get("includeByEventId"), event.event_id)
    event_excludes = event_specific_patterns(line_filters.get("excludeByEventId"), event.event_id)

    if global_includes and not any_regex_matches(global_includes, raw_line):
        return False, "global-include-not-matched"
    if event_includes and not any_regex_matches(event_includes, raw_line):
        return False, "event-include-not-matched"

    matched_global_exclude = any_regex_matches(global_excludes, raw_line)
    if matched_global_exclude:
        return False, f"global-exclude-matched:{matched_global_exclude}"

    matched_event_exclude = any_regex_matches(event_excludes, raw_line)
    if matched_event_exclude:
        return False, f"event-exclude-matched:{matched_event_exclude}"

    return True, "matched"


def get_path_value(node: Any, dotted_path: str) -> Any:
    tokens = [token for token in dotted_path.split(".") if token]

    def flatten(values: list[Any]) -> list[Any]:
        flat: list[Any] = []
        for value in values:
            if isinstance(value, list):
                flat.extend(value)
            elif value is not None:
                flat.append(value)
        return flat

    def walk(current: Any, index: int) -> Any:
        if index >= len(tokens):
            return current
        token = tokens[index]
        if isinstance(current, dict):
            if token not in current:
                return None
            return walk(current[token], index + 1)
        if isinstance(current, list):
            return flatten([walk(item, index) for item in current])
        return None

    return walk(node, 0)


def find_named_list_items(root: Any, list_key: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == list_key and isinstance(value, list):
                    found.extend(item for item in value if isinstance(item, dict))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(root)
    return found


def as_case_nodes(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        nodes: list[Any] = []
        for item in value:
            if isinstance(item, list):
                nodes.extend(item)
            else:
                nodes.append(item)
        return nodes
    return [value]


def build_configured_cases(root: Any, raw_case_groups: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_case_groups, list):
        return []

    all_cases: list[dict[str, Any]] = []
    for group in raw_case_groups:
        if not isinstance(group, dict):
            continue
        roots = group.get("roots")
        if not isinstance(roots, list) or not roots:
            continue

        group_cases: list[dict[str, Any]] = [{}]
        for root_def in roots:
            if not isinstance(root_def, dict):
                continue
            alias = as_text(root_def.get("alias"))
            if not alias:
                continue

            next_cases: list[dict[str, Any]] = []
            parent_alias = as_text(root_def.get("from"))
            path = as_text(root_def.get("path"))
            find_list_key = as_text(root_def.get("findListKey") or root_def.get("listKey"))
            optional = bool(root_def.get("optional", False))

            for case in group_cases:
                if parent_alias:
                    parent = case.get(parent_alias)
                    nodes = as_case_nodes(get_path_value(parent, path)) if path else []
                elif find_list_key:
                    nodes = find_named_list_items(root, find_list_key)
                elif path:
                    nodes = as_case_nodes(get_path_value(root, path))
                else:
                    nodes = []

                if not nodes and optional:
                    next_cases.append(dict(case))
                    continue

                for node in nodes:
                    next_case = dict(case)
                    next_case[alias] = node
                    next_cases.append(next_case)

            group_cases = next_cases
            if not group_cases:
                break

        all_cases.extend(group_cases)
    return all_cases


def build_default_expected_cases(root: Any) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for res in find_named_list_items(root, "res"):
        cards = get_path_value(res, "body.cards")
        if not isinstance(cards, list) or not cards:
            cases.append({"res": res})
            continue

        for card in cards:
            if not isinstance(card, dict):
                continue
            items = card.get("items")
            if not isinstance(items, list) or not items:
                cases.append({"res": res, "cards": card})
                continue
            for item in items:
                if isinstance(item, dict):
                    cases.append({"res": res, "cards": card, "items": item})
                else:
                    cases.append({"res": res, "cards": card})
    return cases


def build_expected_cases(root: Any, field_config: dict[str, Any]) -> list[dict[str, Any]]:
    configured_cases = build_configured_cases(root, field_config.get("caseGroups"))
    if configured_cases:
        return configured_cases
    return build_default_expected_cases(root)


def collect_suffix_values(root: Any, dotted_path: str) -> set[str]:
    tokens = dotted_path.split(".")
    values: set[str] = set()

    def match_from(node: Any, remaining: list[str]) -> list[Any]:
        if not remaining:
            return [node]
        token = remaining[0]
        if isinstance(node, dict):
            if token not in node:
                return []
            return match_from(node[token], remaining[1:])
        if isinstance(node, list):
            result: list[Any] = []
            for item in node:
                result.extend(match_from(item, remaining))
            return result
        return []

    def walk(node: Any) -> None:
        for value in match_from(node, tokens):
            values.add(as_text(value))
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(root)
    return values


def expected_values_for_case(case: dict[str, Any], path: str) -> set[str]:
    tokens = path.split(".")
    if not tokens:
        return set()
    root_key = tokens[0]
    if root_key not in case:
        return set()
    value = get_path_value(case[root_key], ".".join(tokens[1:])) if len(tokens) > 1 else case[root_key]
    if isinstance(value, list):
        return {as_text(item) for item in value}
    if value is None:
        return set()
    return {as_text(value)}


def alias_groups(raw_aliases: Any, field: str) -> list[set[str]]:
    if not isinstance(raw_aliases, dict):
        return []
    field_aliases = raw_aliases.get(field, {})
    if not isinstance(field_aliases, dict):
        return []

    groups: list[set[str]] = []
    for key, values in field_aliases.items():
        group = {as_text(key)}
        if isinstance(values, list):
            group.update(as_text(value) for value in values)
        else:
            group.add(as_text(values))
        groups.append(group)
    return groups


def values_match(log_value: str, expected_values: set[str], raw_aliases: Any, field: str) -> tuple[bool, str]:
    if log_value in expected_values:
        return True, "literal"
    for group in alias_groups(raw_aliases, field):
        if log_value in group and group.intersection(expected_values):
            return True, "alias"
    return False, "mismatch"


def check_against_case(
    event: ParsedEvent,
    field_map: dict[str, str],
    case: dict[str, Any],
    raw_aliases: Any,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for field, path in field_map.items():
        expected_values = expected_values_for_case(case, path)
        log_value = event.params.get(field)
        matched = False
        detail = "missing-log-field"
        if log_value is not None and expected_values:
            matched, detail = values_match(log_value, expected_values, raw_aliases, field)
        elif log_value is not None and not expected_values:
            detail = "missing-expected-value"
        results.append(
            CheckResult(
                field=field,
                log_value=log_value,
                expected_path=path,
                expected_values=expected_values,
                matched=matched,
                detail=detail,
            )
        )
    return results


def choose_best_results(
    event: ParsedEvent,
    root: Any,
    cases: list[dict[str, Any]],
    field_map: dict[str, str],
    raw_aliases: Any,
) -> list[CheckResult]:
    if cases:
        all_results = [check_against_case(event, field_map, case, raw_aliases) for case in cases]
        return max(all_results, key=lambda results: sum(1 for result in results if result.matched))

    fallback: list[CheckResult] = []
    for field, path in field_map.items():
        expected_values = collect_suffix_values(root, path)
        log_value = event.params.get(field)
        matched = False
        detail = "missing-log-field"
        if log_value is not None and expected_values:
            matched, detail = values_match(log_value, expected_values, raw_aliases, field)
        elif log_value is not None and not expected_values:
            detail = "missing-expected-value"
        fallback.append(CheckResult(field, log_value, path, expected_values, matched, detail))
    return fallback


def field_map_for_event(config: dict[str, Any], event_id: str) -> dict[str, str]:
    by_event = config.get("fieldsByEventId", {})
    fields = config.get("fields", DEFAULT_FIELDS)
    base_fields = {as_text(k): as_text(v) for k, v in fields.items()}
    if isinstance(by_event, dict) and isinstance(by_event.get(event_id), dict):
        event_fields = {as_text(k): as_text(v) for k, v in by_event[event_id].items()}
        if as_text(config.get("fieldMergeMode")) == "merge":
            return {**base_fields, **event_fields}
        return event_fields
    return base_fields


def short_values(values: set[str], limit: int = 4) -> str:
    ordered = sorted(values)
    if len(ordered) <= limit:
        return ", ".join(repr(value) for value in ordered)
    head = ", ".join(repr(value) for value in ordered[:limit])
    return f"{head}, ... (+{len(ordered) - limit})"


def print_event_result(event: ParsedEvent, event_label: str, results: list[CheckResult]) -> bool:
    ok = all(result.matched for result in results)
    status = "PASS" if ok else "FAIL"
    label = f" {event_label}" if event_label else ""
    print(f"\n[{status}] eventId={event.event_id}{label} event={event.event_name}")
    for result in results:
        mark = "OK" if result.matched else "NO"
        log_value = "<missing>" if result.log_value is None else repr(result.log_value)
        expected = short_values(result.expected_values) if result.expected_values else "<missing>"
        print(
            f"  [{mark}] {result.field}: log={log_value} "
            f"expected({result.expected_path})={expected} [{result.detail}]"
        )
    return ok


def html_values(values: set[str], limit: int = 8) -> str:
    if not values:
        return "<span class=\"muted\">&lt;missing&gt;</span>"
    ordered = sorted(values)
    visible = ordered[:limit]
    text = ", ".join(repr(value) for value in visible)
    if len(ordered) > limit:
        text += f", ... (+{len(ordered) - limit})"
    return html.escape(text)


def write_html_report(
    path: Path,
    reports: list[EventReport],
    missing_events: list[MissingEvent],
    summary: dict[str, int],
    args: argparse.Namespace,
    expected_case_count: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    passed_rows = sum(1 for report in reports if report.passed)
    failed_rows = sum(1 for report in reports if not report.passed)

    def esc(value: Any) -> str:
        return html.escape(as_text(value))

    sections: list[str] = []
    for index, report in enumerate(reports, start=1):
        status = "PASS" if report.passed else "FAIL"
        rows = []
        for result in report.results:
            row_status = "PASS" if result.matched else "FAIL"
            log_value = "&lt;missing&gt;" if result.log_value is None else esc(repr(result.log_value))
            rows.append(
                "<tr class=\"{row_class}\">"
                "<td>{status}</td>"
                "<td>{field}</td>"
                "<td><code>{log_value}</code></td>"
                "<td><code>{path}</code></td>"
                "<td><code>{expected}</code></td>"
                "<td>{detail}</td>"
                "</tr>".format(
                    row_class="ok" if result.matched else "bad",
                    status=row_status,
                    field=esc(result.field),
                    log_value=log_value,
                    path=esc(result.expected_path),
                    expected=html_values(result.expected_values),
                    detail=esc(result.detail),
                )
            )
        sections.append(
            f"""
            <section class="event-card {'pass' if report.passed else 'fail'}">
              <div class="event-head">
                <div>
                  <h2>#{index} {status} · eventId={esc(report.event.event_id)}</h2>
                  <p>{esc(report.event_label)} · {esc(report.event.event_name)}</p>
                </div>
                <span class="pill {'pill-pass' if report.passed else 'pill-fail'}">{status}</span>
              </div>
              <pre>{esc(report.event.raw_line)}</pre>
              <table>
                <thead>
                  <tr>
                    <th>结果</th>
                    <th>日志字段</th>
                    <th>Logcat 值</th>
                    <th>JSON 路径</th>
                    <th>预期值</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {''.join(rows)}
                </tbody>
              </table>
            </section>
            """
        )

    missing_html = ""
    if missing_events:
        rows = "".join(
            f"<tr class=\"bad\"><td>MISS</td><td><code>{esc(event.event_id)}</code></td><td>{esc(event.event_label)}</td></tr>"
            for event in missing_events
        )
        missing_html = f"""
        <section class="event-card fail">
          <div class="event-head">
            <div>
              <h2>未捕获事件</h2>
              <p>这些事件在本次 Logcat 输入里没有出现。</p>
            </div>
            <span class="pill pill-fail">MISS</span>
          </div>
          <table>
            <thead><tr><th>结果</th><th>eventId</th><th>事件名</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </section>
        """

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Android 埋点校验报告</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #18202a;
      --muted: #667085;
      --border: #d9dee7;
      --pass: #0f8a5f;
      --pass-bg: #e8f7ef;
      --fail: #c53b3b;
      --fail-bg: #fff0f0;
      --code: #f2f4f7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      line-height: 1.5;
    }}
    header {{
      padding: 28px 32px 18px;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
    }}
    h1, h2 {{ margin: 0; line-height: 1.2; }}
    h1 {{ font-size: 26px; }}
    h2 {{ font-size: 18px; }}
    p {{ margin: 6px 0 0; color: var(--muted); }}
    main {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .metric, .event-card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    .metric {{ padding: 14px 16px; }}
    .metric strong {{ display: block; font-size: 24px; }}
    .metric span {{ color: var(--muted); }}
    .event-card {{ margin: 16px 0; overflow: hidden; }}
    .event-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      padding: 16px;
      border-bottom: 1px solid var(--border);
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 12px;
    }}
    .pill-pass {{ background: var(--pass-bg); color: var(--pass); }}
    .pill-fail {{ background: var(--fail-bg); color: var(--fail); }}
    pre {{
      margin: 0;
      padding: 14px 16px;
      overflow-x: auto;
      background: #101828;
      color: #eef2f6;
      font-size: 12px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      padding: 10px 12px;
      text-align: left;
      border-top: 1px solid var(--border);
      vertical-align: top;
      font-size: 13px;
    }}
    th {{ background: #f8fafc; color: #344054; }}
    tr.ok td:first-child {{ color: var(--pass); font-weight: 700; }}
    tr.bad td:first-child {{ color: var(--fail); font-weight: 700; }}
    code {{
      display: inline-block;
      max-width: 420px;
      padding: 2px 5px;
      background: var(--code);
      border-radius: 4px;
      white-space: normal;
      word-break: break-word;
    }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
  <header>
    <h1>Android 埋点校验报告</h1>
    <p>生成时间：{esc(generated_at)} · 预期候选记录：{expected_case_count}</p>
  </header>
  <main>
    <div class="summary">
      <div class="metric"><strong>{summary['checked']}</strong><span>已检查事件行</span></div>
      <div class="metric"><strong>{passed_rows}</strong><span>匹配成功事件</span></div>
      <div class="metric"><strong>{failed_rows}</strong><span>匹配失败事件</span></div>
      <div class="metric"><strong>{len(missing_events)}</strong><span>未捕获事件</span></div>
      <div class="metric"><strong>{summary.get('skipped', 0)}</strong><span>过滤跳过行</span></div>
      <div class="metric"><strong>{summary['failed']}</strong><span>失败计数</span></div>
    </div>
    <section class="event-card">
      <div class="event-head">
        <div>
          <h2>本次配置</h2>
          <p>脚本只读取本地 JSON 和 Logcat，不修改 Android 项目。</p>
        </div>
      </div>
      <table>
        <tbody>
          <tr><th>expected-json</th><td><code>{esc(args.expected_json)}</code></td></tr>
          <tr><th>events</th><td><code>{esc(args.events)}</code></td></tr>
          <tr><th>field-map</th><td><code>{esc(args.field_map)}</code></td></tr>
          <tr><th>log-file</th><td><code>{esc(args.log_file or '')}</code></td></tr>
        </tbody>
      </table>
    </section>
    {''.join(sections)}
    {missing_html}
  </main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def iter_log_lines(args: argparse.Namespace):
    if args.log_file:
        with args.log_file.open("r", encoding="utf-8-sig", errors="replace") as f:
            yield from f
        return

    if args.stdin:
        yield from sys.stdin
        return

    if args.clear_logcat:
        subprocess.run(["adb", "logcat", "-c"], check=False)

    command = ["adb", "logcat", "-v", args.logcat_format]
    if args.adb_filter:
        command.extend(args.adb_filter)

    print(f"Listening: {' '.join(command)}")
    print("Operate the app manually. Press Ctrl+C to stop and print summary.")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            yield line
    finally:
        process.terminate()


def build_arg_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Check Android tracking logcat events against a local JSON payload.",
    )
    parser.add_argument("--expected-json", type=Path, default=script_dir / "expected-data.json")
    parser.add_argument("--events", type=Path, default=script_dir / "events.json")
    parser.add_argument("--field-map", type=Path, default=script_dir / "field_mapping.json")
    parser.add_argument("--event-id", action="append", default=[], help="Extra event id to check. Can repeat.")
    parser.add_argument("--log-file", type=Path, help="Read logcat text from a file instead of adb logcat.")
    parser.add_argument("--stdin", action="store_true", help="Read logcat text from stdin.")
    parser.add_argument("--clear-logcat", action="store_true", help="Run 'adb logcat -c' before listening.")
    parser.add_argument("--allow-missing-events", action="store_true", help="Do not fail when a configured event is not seen.")
    parser.add_argument("--max-matches", type=int, default=0, help="Stop after N checked event lines. 0 means keep listening.")
    parser.add_argument("--html-report", type=Path, default=script_dir / "tracking-report.html", help="Write an HTML report.")
    parser.add_argument("--no-html-report", action="store_true", help="Do not write an HTML report.")
    parser.add_argument("--include-line-regex", action="append", default=[], help="Only check lines matching this regex. Can repeat.")
    parser.add_argument("--exclude-line-regex", action="append", default=[], help="Skip lines matching this regex. Can repeat.")
    parser.add_argument("--logcat-format", default="time")
    parser.add_argument("--adb-filter", nargs="*", help="Extra adb logcat filters, for example: *:I")
    return parser


def main() -> int:
    configure_stdout()
    args = build_arg_parser().parse_args()

    if not args.expected_json.exists():
        print(f"Expected JSON not found: {args.expected_json}", file=sys.stderr)
        return 2

    expected_root = read_json(args.expected_json)
    field_config = load_field_config(args.field_map if args.field_map.exists() else None)
    events = load_events(args.events if args.events.exists() else None, args.event_id)
    cases = build_expected_cases(expected_root, field_config)

    print(f"Expected JSON: {args.expected_json}")
    print(f"Events: {args.events if args.events.exists() else '<all events>'}")
    print(f"Field map: {args.field_map if args.field_map.exists() else '<built-in defaults>'}")
    print(f"Expected cases: {len(cases)}")

    checked = 0
    passed = 0
    failed = 0
    skipped = 0
    seen_event_ids: set[str] = set()
    reports: list[EventReport] = []

    try:
        for line in iter_log_lines(args):
            event = parse_event_line(line, field_config.get("logParser", {}))
            if not event:
                continue
            if events and event.event_id not in events:
                continue
            passes_filter, filter_reason = event_passes_line_filters(
                event=event,
                line_filters=field_config.get("lineFilters", {}),
                cli_include=args.include_line_regex,
                cli_exclude=args.exclude_line_regex,
            )
            if not passes_filter:
                skipped += 1
                continue

            seen_event_ids.add(event.event_id)
            field_map = field_map_for_event(field_config, event.event_id)
            results = choose_best_results(
                event=event,
                root=expected_root,
                cases=cases,
                field_map=field_map,
                raw_aliases=field_config.get("valueAliases", {}),
            )
            ok = print_event_result(event, events.get(event.event_id, ""), results)
            reports.append(EventReport(event=event, event_label=events.get(event.event_id, ""), results=results, passed=ok))
            checked += 1
            if ok:
                passed += 1
            else:
                failed += 1
            if args.max_matches and checked >= args.max_matches:
                break
    except KeyboardInterrupt:
        print("\nStopped by user.")

    missing = sorted(set(events) - seen_event_ids)
    missing_events = [MissingEvent(event_id=event_id, event_label=events[event_id]) for event_id in missing]
    if missing and not args.allow_missing_events:
        failed += len(missing)
        print("\nMissing configured events:")
        for missing_event in missing_events:
            label = f" {missing_event.event_label}" if missing_event.event_label else ""
            print(f"  [NO] eventId={missing_event.event_id}{label}")

    print(f"\nSummary: checked={checked}, passed={passed}, failed={failed}, skippedByFilter={skipped}")
    if not args.no_html_report:
        write_html_report(
            path=args.html_report,
            reports=reports,
            missing_events=missing_events,
            summary={"checked": checked, "passed": passed, "failed": failed, "skipped": skipped},
            args=args,
            expected_case_count=len(cases),
        )
        print(f"HTML report: {args.html_report}")
    return 0 if failed == 0 and checked > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
