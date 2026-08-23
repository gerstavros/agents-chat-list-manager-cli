#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.claude_settings import ensure_claude_cleanup_period
from core.config import AppConfig
from core.export import export_conversation
from core.models import ConversationMeta
from core.registry import build_adapters, get_adapter_classes
from core.service import ConversationService

PROG = "chatlistctl"


def build_service(config: AppConfig, overrides: dict[str, str | None]) -> ConversationService:
    merged = AppConfig(language=config.language, path_overrides=dict(config.path_overrides))
    for tool_id, path in overrides.items():
        if path:
            merged.path_overrides[tool_id] = path
    return ConversationService(build_adapters(merged))


def resolve_conversation(service: ConversationService, id_arg: str) -> ConversationMeta:
    all_convs = service.list_all()

    if ":" in id_arg:
        for conv in all_convs:
            if conv.iid == id_arg:
                return conv
        raise SystemExit(f"error: no conversation with id '{id_arg}'")

    exact = [c for c in all_convs if c.session_id == id_arg]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _print_ambiguous(id_arg, exact)
        raise SystemExit(2)

    prefix_matches = [c for c in all_convs if c.session_id.startswith(id_arg)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        _print_ambiguous(id_arg, prefix_matches)
        raise SystemExit(2)

    raise SystemExit(f"error: no conversation found matching '{id_arg}'")


def _print_ambiguous(id_arg: str, matches: list[ConversationMeta]) -> None:
    print(f"error: '{id_arg}' matches {len(matches)} conversations, specify tool_id:session_id:", file=sys.stderr)
    for c in matches:
        print(f"  {c.iid}  {c.title}", file=sys.stderr)


def _truncate(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def cmd_list(args, service: ConversationService) -> int:
    convs = service.list_all()

    if args.tool:
        convs = [c for c in convs if c.tool_id == args.tool]
    if args.search:
        query = args.search.lower()
        convs = [c for c in convs if query in c.title.lower() or query in c.project_path.lower()]
    if args.days is not None:
        now = datetime.now(timezone.utc)
        def within(c):
            if c.updated_at is None:
                return False
            dt = c.updated_at if c.updated_at.tzinfo else c.updated_at.replace(tzinfo=timezone.utc)
            return (now - dt).days <= args.days
        convs = [c for c in convs if within(c)]

    min_dt = datetime.min.replace(tzinfo=timezone.utc)
    key_funcs = {
        "updated": lambda c: c.updated_at or min_dt,
        "title": lambda c: c.title.lower(),
        "messages": lambda c: c.message_count,
        "tool": lambda c: c.tool_id,
    }
    convs.sort(key=key_funcs[args.sort], reverse=not args.asc)

    if args.json:
        payload = [
            {
                "id": c.iid,
                "tool": c.tool_id,
                "session_id": c.session_id,
                "title": c.title,
                "project": c.project_path,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                "messages": c.message_count,
            }
            for c in convs
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not convs:
        print("No conversations found.")
        return 0

    header = f"{'TOOL':<13} {'UPDATED':<17} {'MSGS':>5}  {'TITLE':<40} {'PROJECT':<40} ID"
    print(header)
    print("-" * len(header))
    for c in convs:
        updated = c.updated_at.strftime("%Y-%m-%d %H:%M") if c.updated_at else "-"
        print(
            f"{c.tool_id:<13} {updated:<17} {c.message_count:>5}  "
            f"{_truncate(c.title, 40):<40} {_truncate(c.project_path, 40):<40} {c.session_id}"
        )
    print(f"\n{len(convs)} conversation(s)")
    return 0


def cmd_show(args, service: ConversationService) -> int:
    meta = resolve_conversation(service, args.id)
    messages = service.load(meta)
    if args.tail:
        messages = messages[-args.tail:]

    print(f"# {meta.title}")
    print(f"tool: {meta.tool_id}  project: {meta.project_path}  messages: {meta.message_count}")
    print(f"file: {meta.primary_file}")
    print("-" * 60)
    for msg in messages:
        ts = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S") if msg.timestamp else "--"
        marker = " [tool call]" if msg.has_tool_call else ""
        print(f"\n[{ts}] {msg.role}{marker}")
        print(msg.text)
        if args.raw:
            print(json.dumps(msg.raw, indent=2, ensure_ascii=False))
    return 0


def cmd_export(args, service: ConversationService) -> int:
    meta = resolve_conversation(service, args.id)
    messages = service.load(meta)
    output = Path(args.output)
    fmt = args.format or ("markdown" if output.suffix.lower() == ".md" else "text")
    try:
        export_conversation(meta, messages, output, fmt)
    except OSError as exc:
        print(f"error: could not write '{output}': {exc}", file=sys.stderr)
        return 1
    print(f"exported to {output}")
    return 0


def cmd_delete(args, service: ConversationService) -> int:
    meta = resolve_conversation(service, args.id)
    if not args.yes:
        answer = input(f"Delete '{meta.title}' ({meta.iid})? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted")
            return 1
    try:
        service.delete(meta)
    except OSError as exc:
        print(f"error: could not delete: {exc}", file=sys.stderr)
        return 1
    print(f"deleted {meta.iid}")
    return 0


def cmd_paths(args, service: ConversationService, config: AppConfig) -> int:
    for adapter in service.adapters():
        override = config.path_overrides.get(adapter.tool_id)
        source = "override" if override else ("env" if adapter.env_var and os.environ.get(adapter.env_var) else "default")
        status = "found" if adapter.is_available() else "missing"
        print(f"{adapter.tool_id:<14} {str(adapter.base_dir):<45} [{source:<8}] {status}")
    return 0


def cmd_config_set_path(args, config: AppConfig) -> int:
    valid_ids = {cls.tool_id for cls in get_adapter_classes()}
    if args.tool not in valid_ids:
        print(f"error: unknown tool '{args.tool}', expected one of {sorted(valid_ids)}", file=sys.stderr)
        return 1
    config.set_path_override(args.tool, args.path)
    config.save()
    print(f"saved override for {args.tool}: {args.path}")
    return 0


def cmd_browse(args, service: ConversationService) -> int:
    import curses

    from tui_app import run_tui

    curses.wrapper(run_tui, service)
    return 0


def cmd_config_reset_path(args, config: AppConfig) -> int:
    config.set_path_override(args.tool, None)
    config.save()
    print(f"reset {args.tool} to default")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Manage Claude Code / Qwen Code / codewhale-tui / opencode conversations from the terminal.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_override_args(p):
        p.add_argument("--claude-dir", help="override Claude Code storage dir for this run")
        p.add_argument("--qwen-dir", help="override Qwen Code storage dir for this run")
        p.add_argument("--codewhale-dir", help="override codewhale-tui storage dir for this run")
        p.add_argument("--opencode-dir", help="override opencode data dir for this run")

    p_list = sub.add_parser("list", help="list conversations")
    p_list.add_argument("--tool", choices=[c.tool_id for c in get_adapter_classes()])
    p_list.add_argument("--search", help="filter by substring in title/project")
    p_list.add_argument("--days", type=int, help="only conversations updated in the last N days")
    p_list.add_argument("--sort", choices=["updated", "title", "messages", "tool"], default="updated")
    p_list.add_argument("--asc", action="store_true", help="sort ascending (default: descending)")
    p_list.add_argument("--json", action="store_true", help="output JSON instead of a table")
    add_override_args(p_list)

    p_show = sub.add_parser("show", help="print a conversation transcript")
    p_show.add_argument("id", help="session id, id prefix, or tool_id:session_id")
    p_show.add_argument("--raw", action="store_true", help="also print raw JSON for each message")
    p_show.add_argument("--tail", type=int, help="only show the last N messages")
    add_override_args(p_show)

    p_export = sub.add_parser("export", help="export a conversation to .md/.txt")
    p_export.add_argument("id", help="session id, id prefix, or tool_id:session_id")
    p_export.add_argument("-o", "--output", required=True, help="output file path")
    p_export.add_argument("--format", choices=["markdown", "text"], help="default: inferred from output extension")
    add_override_args(p_export)

    p_delete = sub.add_parser("delete", help="delete a conversation")
    p_delete.add_argument("id", help="session id, id prefix, or tool_id:session_id")
    p_delete.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    add_override_args(p_delete)

    p_paths = sub.add_parser("paths", help="show detected/overridden storage paths per tool")
    add_override_args(p_paths)

    p_browse = sub.add_parser("browse", help="interactive TUI: arrows to move, Enter to view, d to delete, / to search")
    add_override_args(p_browse)

    p_config = sub.add_parser("config", help="manage persisted path overrides")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_set = config_sub.add_parser("set-path", help="persist a storage path override for a tool")
    p_set.add_argument("tool", choices=[c.tool_id for c in get_adapter_classes()])
    p_set.add_argument("path")
    p_reset = config_sub.add_parser("reset-path", help="remove a persisted override, revert to default")
    p_reset.add_argument("tool", choices=[c.tool_id for c in get_adapter_classes()])

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["browse"]

    parser = build_parser()
    args = parser.parse_args(argv)
    status = ensure_claude_cleanup_period()
    if status in ("created", "updated"):
        print("note: ensured cleanupPeriodDays=36500 in ~/.claude/settings.json", file=sys.stderr)
    config = AppConfig.load()

    if args.command == "config":
        if args.config_command == "set-path":
            return cmd_config_set_path(args, config)
        if args.config_command == "reset-path":
            return cmd_config_reset_path(args, config)
        parser.error("unknown config subcommand")

    overrides = {
        "claude_code": getattr(args, "claude_dir", None),
        "qwen_code": getattr(args, "qwen_dir", None),
        "codewhale_tui": getattr(args, "codewhale_dir", None),
        "opencode": getattr(args, "opencode_dir", None),
    }
    service = build_service(config, overrides)

    if args.command == "list":
        return cmd_list(args, service)
    if args.command == "show":
        return cmd_show(args, service)
    if args.command == "export":
        return cmd_export(args, service)
    if args.command == "delete":
        return cmd_delete(args, service)
    if args.command == "paths":
        return cmd_paths(args, service, config)
    if args.command == "browse":
        return cmd_browse(args, service)

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
