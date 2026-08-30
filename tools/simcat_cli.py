"""``xgc-sim`` -- the command line over the simulation catalog.

This exists so that finding, recording, and reviewing simulations never depends
on an LLM being available, willing, or correct. The MCP tools call the same
functions; this is the same system with a keyboard instead of a model.

    xgc-sim list --status completed --machine perlmutter
    xgc-sim search "asdex i-mode"
    xgc-sim show n645pe
    xgc-sim note n645pe "growth rate matches the GENE benchmark"
    xgc-sim archive n645pe --system hpss --path /home/s/sku/n645pe.tar
    xgc-sim import-notebook --confirm
    xgc-sim sync-sheet --confirm

Every subcommand prints a table by default and JSON with ``--json``, so it is
equally usable by a person and by a script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from tools import legacy_import
from tools import refresh as refresh_tool
from tools import run_adoption
from tools import simulation_catalog as catalog

# Columns shown by `list` and `search`, with their display width. record_id is
# wide because adopted records are keyed by directory name, which is long and
# is the only thing identifying them. Note is last and unbounded because it is
# the part a human actually reads.
_TABLE_COLUMNS = (
    ("record_id", 34),
    ("machine", 11),
    ("status", 10),
    ("origin", 10),
    ("short_name", 28),
)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _truncate(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    if isinstance(value, (list, tuple)):
        text = ",".join(str(item) for item in value)
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 1] + "\u2026"


def _print_table(rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        print("no matching simulations")
        return
    header = " ".join(name.ljust(width) for name, width in _TABLE_COLUMNS) + " note"
    print(header)
    print("-" * len(header))
    for row in rows:
        line = " ".join(_truncate(row.get(name), width) for name, width in _TABLE_COLUMNS)
        note = str(row.get("note") or "").replace("\n", " ")
        print(f"{line} {note}".rstrip())
    print(f"\n{len(rows)} simulation(s)")


def _print_show(payload: dict[str, Any]) -> None:
    record = payload["record"]
    summary = payload["summary"]
    print(f"{record.get('record_id')}  [{record.get('status')}]  origin={record.get('origin')}")
    for label, key in (
        ("number", "number"),
        ("short name", "short_name"),
        ("machines", "machines"),
        ("case", "case"),
        ("tags", "tags"),
        ("directory", "directory_name"),
        ("input sha256", "input_sha256"),
        ("created", "created_at"),
        ("updated", "updated_at"),
    ):
        value = record.get(key)
        if value:
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            print(f"  {label:<13} {value}")

    for machine, path in (record.get("remote_dirs") or {}).items():
        print(f"  {'remote':<13} {machine}: {path}")

    archive = record.get("archive") or {}
    if archive.get("path") or archive.get("system"):
        print(f"  {'archive':<13} {archive.get('system')}: {archive.get('path')}")

    legacy = record.get("legacy")
    if isinstance(legacy, dict):
        print("  notebook:")
        for key in ("shot", "sheet", "date", "code", "geometry", "description", "note", "data", "outcome"):
            value = legacy.get(key)
            if value:
                print(f"    {key:<12} {value}")

    runs = [run for run in (record.get("runs") or []) if isinstance(run, dict)]
    if runs:
        print(f"  runs ({len(runs)}):")
        for run in runs:
            state = run.get("state") or "?"
            exit_code = run.get("exit_code")
            suffix = f" exit={exit_code}" if exit_code is not None else ""
            print(
                f"    {run.get('run_id')}  job={run.get('job_id')} "
                f"machine={run.get('machine')} state={state}{suffix}"
            )
            if run.get("provenance"):
                print(f"      provenance: {run['provenance']}")

    if record.get("note"):
        print(f"  note: {record['note']}")
    if summary.get("runs") == 0 and record.get("origin") == "automation":
        print("  (no scheduler runs linked yet)")


def _print_stats(payload: dict[str, Any]) -> None:
    print(f"catalog: {payload['catalog_dir']}")
    print(f"total:   {payload['total']}")
    for label, key in (("status", "by_status"), ("machine", "by_machine"), ("origin", "by_origin")):
        print(f"  by {label}:")
        for name, count in payload[key].items():
            print(f"    {name:<14} {count}")


def _print_import(payload: dict[str, Any]) -> None:
    counts = payload["counts"]
    verb = "imported" if payload.get("imported") else "would import"
    print(f"notebook rows read: {payload['notes_read']}")
    print(f"{verb}: {counts['create']} new, {counts['update']} refreshed")
    print(f"skipped: {counts['skip']}")
    reasons: dict[str, int] = {}
    for entry in payload["skip"]:
        key = str(entry.get("reason"))
        reasons[key] = reasons.get(key, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1])[:8]:
        print(f"    {count:>4}  {reason}")
    if not payload.get("imported"):
        print("\nnothing was written; rerun with --confirm to apply")


def _print_adopt(payload: dict[str, Any]) -> None:
    counts = payload["counts"]
    verb = "adopted" if payload.get("adopted") else "would adopt"
    print(f"orphan runs found: {payload['orphan_runs']}")
    print(
        f"{verb}: {counts['create']} new simulation(s), "
        f"{counts['update']} refreshed, covering {counts['runs_adopted']} run(s)"
    )
    for entry in payload["create"] + payload["update"]:
        print(f"    {entry['record_id']:<34} {entry['runs']} run(s)  {entry['directory']}")
    if counts["skip"]:
        print(f"skipped: {counts['skip']}")
        reasons: dict[str, int] = {}
        for entry in payload["skip"]:
            key = str(entry.get("reason"))
            reasons[key] = reasons.get(key, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda item: -item[1])[:8]:
            print(f"    {count:>4}  {reason}")
    if not payload.get("adopted"):
        print("\nnothing was written; rerun with --confirm to apply")


def _print_refresh_plan(payload: dict[str, Any]) -> None:
    counts = payload["counts"]
    print(
        f"would poll {counts['runs']} run(s) across "
        f"{counts['simulations']} simulation(s)"
    )
    for target in payload["targets"]:
        print(f"    {target['record_id']:<34} [{target['status']}]")
        for run in target["runs"]:
            print(
                f"        {run['machine']} job {run['job_id']} "
                f"({run['state'] or 'no state yet'})"
            )
    print("\nnothing was contacted or written")


def _print_refresh(payload: dict[str, Any]) -> None:
    counts = payload["counts"]
    if not counts["polled"]:
        print("nothing in flight; the catalog is already up to date")
        return

    print(f"polled {counts['polled']} run(s) across {counts['simulations']} simulation(s)")
    for item in payload["updated"]:
        arrow = (
            f"{item['status_before']} -> {item['status_after']}"
            if item["status_before"] != item["status_after"]
            else item["status_after"]
        )
        exit_code = item.get("exit_code")
        suffix = f" exit={exit_code}" if exit_code is not None else ""
        print(
            f"    {item['record_id']:<34} job {item['job_id']} "
            f"{item['run_state']}{suffix}  [{arrow}]"
        )
    if not payload["updated"]:
        print("    no state changed")
    if payload["errors"]:
        print(f"\ncould not reach {len(payload['errors'])} run(s):")
        for item in payload["errors"]:
            print(f"    {item['record_id']} job {item.get('job_id')}: {item['error']}")


def _print_sync(payload: dict[str, Any]) -> None:
    counts = payload["counts"]
    print(f"catalog rows:   {payload['rows']}")
    print(f"push new:       {counts['push_new']}")
    print(f"push changed:   {counts['push_changed']}")
    print(f"notes to pull:  {counts['note_pulls']}")
    for pull in payload["note_pulls"]:
        print(f"    {pull['record_id']}: {pull['catalog_note']!r} -> {pull['sheet_note']!r}")
    if counts["sheet_only"]:
        print(f"rows only in the sheet (left alone): {counts['sheet_only']}")
        for key in payload["sheet_only"][:10]:
            print(f"    {key}")
    if payload.get("synced"):
        print(f"\npushed {payload['pushed_rows']} row(s); pulled {len(payload['pulled_notes'])} note(s)")
    else:
        print("\nnothing was written; rerun with --confirm to apply")


def _catalog_dir(args: argparse.Namespace) -> Path | None:
    return Path(args.catalog_dir) if getattr(args, "catalog_dir", None) else None


def _cmd_list(args: argparse.Namespace) -> int:
    rows = catalog.search(
        args.query or "",
        machine=args.machine,
        status=args.status,
        tag=args.tag,
        origin=args.origin,
        case=args.case,
        after=args.after,
        before=args.before,
        limit=args.limit,
        catalog_dir=_catalog_dir(args),
    )
    _print_json(rows) if args.json else _print_table(rows)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    payload = catalog.show(args.record_id, catalog_dir=_catalog_dir(args))
    _print_json(payload) if args.json else _print_show(payload)
    return 0


def _cmd_note(args: argparse.Namespace) -> int:
    record = catalog.set_note(args.record_id, args.note, catalog_dir=_catalog_dir(args))
    if args.json:
        _print_json(catalog.summarize(record))
    else:
        print(f"{record['record_id']}: note updated")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    record = catalog.update_record(
        args.record_id, {"status": args.status}, catalog_dir=_catalog_dir(args)
    )
    if args.json:
        _print_json(catalog.summarize(record))
    else:
        print(f"{record['record_id']}: status -> {record['status']}")
    return 0


def _cmd_archive(args: argparse.Namespace) -> int:
    record = catalog.set_archive(
        args.record_id,
        system=args.system,
        path=args.path,
        catalog_dir=_catalog_dir(args),
    )
    if args.json:
        _print_json(catalog.summarize(record))
    else:
        archive = record["archive"]
        print(f"{record['record_id']}: archived to {archive['system']}:{archive['path']}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    payload = catalog.stats(catalog_dir=_catalog_dir(args))
    _print_json(payload) if args.json else _print_stats(payload)
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    payload = legacy_import.import_notebook(
        notebook=Path(args.notebook) if args.notebook else None,
        catalog_dir=_catalog_dir(args),
        overwrite=args.overwrite,
        confirm=args.confirm,
    )
    if args.json:
        for entry in payload["create"] + payload["update"]:
            entry.pop("record", None)
        _print_json(payload)
    else:
        _print_import(payload)
    return 0


def _cmd_adopt(args: argparse.Namespace) -> int:
    payload = run_adoption.adopt_runs(
        runs_dir=Path(args.runs_dir) if args.runs_dir else None,
        catalog_dir=_catalog_dir(args),
        include_smoke_tests=args.include_smoke_tests,
        overwrite=args.overwrite,
        confirm=args.confirm,
    )
    if args.json:
        for entry in payload["create"] + payload["update"]:
            entry.pop("record", None)
        _print_json(payload)
    else:
        _print_adopt(payload)
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    if args.dry_run:
        payload = refresh_tool.plan_refresh(
            catalog_dir=_catalog_dir(args),
            machine=args.machine,
            record_id=args.record_id,
        )
        _print_json(payload) if args.json else _print_refresh_plan(payload)
        return 0

    payload = refresh_tool.refresh(
        catalog_dir=_catalog_dir(args),
        machine=args.machine,
        record_id=args.record_id,
    )
    _print_json(payload) if args.json else _print_refresh(payload)
    # A machine that could not be reached is worth a non-zero exit, so a cron
    # entry or a script notices instead of assuming the catalog is current.
    return 1 if payload["errors"] else 0


def _cmd_sync_sheet(args: argparse.Namespace) -> int:
    # Imported lazily so the rest of the CLI works without the optional
    # Google dependencies installed.
    from tools import sheet_sync

    if args.check:
        settings = sheet_sync.resolve_settings(
            credentials=args.credentials,
            spreadsheet_id=args.spreadsheet_id,
            worksheet=args.worksheet,
        )
        _print_json(settings) if args.json else print(
            f"configured: {settings['configured']}\n"
            f"credentials: {settings['credentials']} "
            f"(exists: {settings['credentials_exist']})\n"
            f"spreadsheet: {settings['spreadsheet_id']}\n"
            f"worksheet:  {settings['worksheet']}"
        )
        return 0 if settings["configured"] else 1

    worksheet = sheet_sync.open_worksheet(
        credentials=args.credentials,
        spreadsheet_id=args.spreadsheet_id,
        worksheet=args.worksheet,
    )
    payload = sheet_sync.sync(
        worksheet=worksheet,
        catalog_dir=_catalog_dir(args),
        pull_notes=not args.no_pull,
        confirm=args.confirm,
    )
    _print_json(payload) if args.json else _print_sync(payload)
    return 0


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--machine")
    parser.add_argument("--status", choices=list(catalog.STATUSES))
    parser.add_argument("--tag")
    parser.add_argument("--origin", choices=list(catalog.ORIGINS))
    parser.add_argument("--case")
    parser.add_argument("--after", help="ISO date lower bound, e.g. 2025-01-01")
    parser.add_argument("--before", help="ISO date upper bound")
    parser.add_argument("--limit", type=int)


def _global_options() -> argparse.ArgumentParser:
    """Options accepted both before and after the subcommand.

    Writing `xgc-sim list --json` is the obvious thing to try, so accepting it
    only as `xgc-sim --json list` is a trap. Sharing one parent parser means
    either position works.

    The defaults are SUPPRESS so that an option given before the subcommand
    survives: without it the subparser would write its own default over the
    value the main parser already stored.
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output",
    )
    shared.add_argument(
        "--catalog-dir",
        default=argparse.SUPPRESS,
        help="override kb/simulations (testing)",
    )
    return shared


def _parser() -> argparse.ArgumentParser:
    shared = _global_options()
    parser = argparse.ArgumentParser(
        prog="xgc-sim",
        description="Search and maintain the XGC simulation catalog.",
        parents=[shared],
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return commands.add_parser(name, parents=[shared], **kwargs)

    list_parser = add("list", help="list simulations")
    list_parser.add_argument("query", nargs="?", default="")
    _add_filters(list_parser)
    list_parser.set_defaults(func=_cmd_list)

    search_parser = add("search", help="free-text search over every field")
    search_parser.add_argument("query")
    _add_filters(search_parser)
    search_parser.set_defaults(func=_cmd_list)

    show_parser = add("show", help="show one simulation in full")
    show_parser.add_argument("record_id")
    show_parser.set_defaults(func=_cmd_show)

    note_parser = add("note", help="set the human note")
    note_parser.add_argument("record_id")
    note_parser.add_argument("note")
    note_parser.set_defaults(func=_cmd_note)

    status_parser = add("status", help="set the simulation status")
    status_parser.add_argument("record_id")
    status_parser.add_argument("status", choices=list(catalog.STATUSES))
    status_parser.set_defaults(func=_cmd_status)

    archive_parser = add("archive", help="record where output was archived")
    archive_parser.add_argument("record_id")
    archive_parser.add_argument("--system", default="hpss")
    archive_parser.add_argument("--path", required=True)
    archive_parser.set_defaults(func=_cmd_archive)

    stats_parser = add("stats", help="catalog counts by status/machine/origin")
    stats_parser.set_defaults(func=_cmd_stats)

    import_parser = add(
        "import-notebook",
        help="import the historical simulation notebook into the catalog",
    )
    import_parser.add_argument("--notebook", help="path to the .xlsx notebook")
    import_parser.add_argument(
        "--overwrite", action="store_true", help="refresh records already imported"
    )
    import_parser.add_argument("--confirm", action="store_true")
    import_parser.set_defaults(func=_cmd_import)

    adopt_parser = add(
        "adopt-runs",
        help="add pre-catalog runs under runs/ to the catalog, grouped by directory",
    )
    adopt_parser.add_argument("--runs-dir", help="override runs/ (testing)")
    adopt_parser.add_argument(
        "--include-smoke-tests",
        action="store_true",
        help="also adopt scheduler smoke tests",
    )
    adopt_parser.add_argument(
        "--overwrite", action="store_true", help="refresh records already adopted"
    )
    adopt_parser.add_argument("--confirm", action="store_true")
    adopt_parser.set_defaults(func=_cmd_adopt)

    refresh_parser = add(
        "refresh",
        help="ask the schedulers about every in-flight run and record the answers",
    )
    refresh_parser.add_argument("--machine", help="poll only this machine")
    refresh_parser.add_argument("--record-id", help="poll only this simulation")
    refresh_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be polled without contacting anything",
    )
    refresh_parser.set_defaults(func=_cmd_refresh)

    sheet_parser = add(
        "sync-sheet", help="mirror the catalog to Google Sheets (notes pull back)"
    )
    sheet_parser.add_argument("--credentials")
    sheet_parser.add_argument("--spreadsheet-id")
    sheet_parser.add_argument("--worksheet")
    sheet_parser.add_argument(
        "--no-pull", action="store_true", help="push only; ignore notes edited in the sheet"
    )
    sheet_parser.add_argument(
        "--check", action="store_true", help="report configuration without connecting"
    )
    sheet_parser.add_argument("--confirm", action="store_true")
    sheet_parser.set_defaults(func=_cmd_sync_sheet)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # The shared options use SUPPRESS so either position wins, which means an
    # unused option leaves no attribute behind at all.
    for name, default in (("json", False), ("catalog_dir", None)):
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        return int(args.func(args))
    except (
        catalog.SimulationCatalogError,
        legacy_import.LegacyImportError,
        run_adoption.RunAdoptionError,
    ) as exc:
        print(f"xgc-sim: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # sheet_sync errors are only importable lazily
        if type(exc).__name__ == "SheetSyncError":
            print(f"xgc-sim: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
