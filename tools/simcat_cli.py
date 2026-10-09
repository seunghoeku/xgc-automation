"""``xgc-sim`` -- the command line over the simulation catalog.

This exists so that finding, recording, and reviewing simulations never depends
on an LLM being available, willing, or correct. The MCP tools call the same
functions; this is the same system with a keyboard instead of a model.

    xgc-sim list --status completed --machine perlmutter
    xgc-sim search "asdex i-mode"
    xgc-sim show n645pe
    xgc-sim note n645pe "growth rate matches the GENE benchmark"
    xgc-sim ai-note n645pe "restart 3 reached gstep 900; relaxing"
    xgc-sim archive n645pe --system hpss --path /home/s/sku/n645pe.tar
    xgc-sim import-notebook --confirm
    xgc-sim sync-sheet --confirm
    xgc-sim sync-notebook --confirm
    xgc-sim adopt-remote polaris /grand/.../n648po_dAdt_ecbc --confirm
    xgc-sim campaign n645pe               # plan the HPC-Campaign archive
    xgc-sim hpss perlmutter               # list the tape index (kb/archives/)
    xgc-sim hpss perlmutter --refresh     # hsi ls -lR on the machine, rewrite the index
    xgc-sim hpss perlmutter --link --confirm   # copy unambiguous tape paths into records
    xgc-sim campaign n645pe --confirm     # start it on the login node
    xgc-sim campaign n645pe --status      # collect the result

Every subcommand prints a table by default and JSON with ``--json``, so it is
equally usable by a person and by a script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from tools import campaign as campaign_tool
from tools import catalog_sync
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
    ("simulation_tag", 12),
    ("sub_tag", 28),
    ("machine", 11),
    ("status", 10),
    ("origin", 10),
    ("kind", 10),
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
    print(
        f"{record.get('record_id')}  [{record.get('status')}]  "
        f"origin={record.get('origin')}  kind={summary.get('kind')}"
    )
    for label, key in (
        ("simulation tag", "simulation_tag"),
        ("sub tag", "sub_tag"),
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
    from tools import hpss_index

    for entry in hpss_index.entries_for(str(record.get("record_id"))):
        flag = "" if entry["path"] == archive.get("path") else "  (not in archive.path)"
        print(f"  {'tape':<13} {entry['machine']}:{entry['path']}  {hpss_index.human_size(entry['size'])}  {entry['modified']}{flag}")

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

    if record.get("ai_note"):
        print(f"  ai note: {record['ai_note']}")
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


def _campaign_line(item: dict[str, Any]) -> str:
    state = item.get("state") or ("error" if item.get("error") else "unknown")
    if item.get("stale"):
        state += ", stale"
    detail = item.get("reason") or item.get("error") or item.get("archive") or ""
    return f"    {item.get('record_id', ''):<34} campaign {state}  {detail}".rstrip()


def _print_campaigns(payload: dict[str, Any]) -> None:
    campaigns = payload.get("campaigns") or []
    if campaigns:
        print("\ncampaign archives:")
        for item in campaigns:
            print(_campaign_line(item))


def _print_refresh(payload: dict[str, Any]) -> None:
    counts = payload["counts"]
    if not counts["polled"]:
        print("nothing in flight; the catalog is already up to date")
        _print_campaigns(payload)
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
    _print_campaigns(payload)


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
        simulation_tag=args.simulation_tag,
        sub_tag=args.sub_tag,
        origin=args.origin,
        kind=args.kind,
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


@catalog_sync.synced(lambda result, args: f"xgc-sim {args.command} {getattr(args, 'record_id', None) or getattr(args, 'remote_dir', None) or getattr(args, 'machine', None) or ''}".rstrip())
def _cmd_note(args: argparse.Namespace) -> int:
    record = catalog.set_note(args.record_id, args.note, catalog_dir=_catalog_dir(args))
    if args.json:
        _print_json(catalog.summarize(record))
    else:
        print(f"{record['record_id']}: note updated")
    return 0


@catalog_sync.synced(lambda result, args: f"xgc-sim {args.command} {getattr(args, 'record_id', None) or getattr(args, 'remote_dir', None) or getattr(args, 'machine', None) or ''}".rstrip())
def _cmd_ai_note(args: argparse.Namespace) -> int:
    record = catalog.set_ai_note(args.record_id, args.ai_note, catalog_dir=_catalog_dir(args))
    if args.json:
        _print_json(catalog.summarize(record))
    else:
        print(f"{record['record_id']}: ai note updated")
    return 0


@catalog_sync.synced(lambda result, args: f"xgc-sim {args.command} {getattr(args, 'record_id', None) or getattr(args, 'remote_dir', None) or getattr(args, 'machine', None) or ''}".rstrip())
def _cmd_status(args: argparse.Namespace) -> int:
    record = catalog.update_record(
        args.record_id, {"status": args.status}, catalog_dir=_catalog_dir(args)
    )
    if args.json:
        _print_json(catalog.summarize(record))
    else:
        print(f"{record['record_id']}: status -> {record['status']}")
    return 0


def _hpss_message(result: Any, args: argparse.Namespace) -> str:
    if getattr(args, "link", False) and getattr(args, "confirm", False):
        return f"xgc-sim hpss {args.machine} --link"
    if getattr(args, "refresh", False):
        return f"xgc-sim hpss {args.machine} --refresh"
    return ""


@catalog_sync.synced(_hpss_message)
def _cmd_hpss(args: argparse.Namespace) -> int:
    from tools import hpss_index

    try:
        if args.refresh:
            text = Path(args.from_file).read_text(encoding="utf-8") if args.from_file else None
            result = hpss_index.refresh(args.machine, text=text)
            totals = result["totals"]
            print(f"{args.machine}: {totals['archives']} archives, {hpss_index.human_size(totals['bytes'])}, "
                  f"{totals['matched']} entries matched to records, {totals['unmatched']} unmatched -> {result['path']}")
        index = hpss_index.load_index(args.machine)
    except hpss_index.HPSSIndexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.link:
        plan = hpss_index.link_plan(index)
        if args.json:
            _print_json(plan)
        else:
            for item in plan:
                mark = "link     " if item["action"] == "link" else "AMBIGUOUS"
                print(f"  {mark} {item['record_id']:<10} {item['status'] or '':<12} " + "; ".join(item["archives"]))
            print(f"{sum(i['action'] == 'link' for i in plan)} to link, "
                  f"{sum(i['action'] == 'ambiguous' for i in plan)} ambiguous (left for a human)")
        if args.confirm:
            linked = hpss_index.apply_links(plan, catalog_dir=_catalog_dir(args))
            print(f"linked {len(linked)} record(s)")
        else:
            print("plan only; add --confirm to write archive.path into the records")
        return 0
    if not args.refresh or args.json:
        rows = hpss_index.summarize(index)
        if args.json:
            _print_json({"machine": index["machine"], "fetched_at": index["fetched_at"], "totals": index["totals"], "entries": rows})
        else:
            print(f"{index['machine']} {index['system']} {index.get('root')}  fetched {index['fetched_at']}")
            for row in rows:
                print(f"  {hpss_index.human_size(row['size']):>10}  {row['modified']}  {row['records']:<12} {row['path']}")
            t = index["totals"]
            print(f"{t['archives']} archives, {hpss_index.human_size(t['bytes'])}; {t['matched']} matched, {t['unmatched']} unmatched")
    return 0


@catalog_sync.synced(lambda result, args: f"xgc-sim {args.command} {getattr(args, 'record_id', None) or getattr(args, 'remote_dir', None) or getattr(args, 'machine', None) or ''}".rstrip())
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


@catalog_sync.synced(lambda result, args: f"xgc-sim {args.command} {getattr(args, 'record_id', None) or getattr(args, 'remote_dir', None) or getattr(args, 'machine', None) or ''}".rstrip())
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


@catalog_sync.synced(lambda result, args: f"xgc-sim {args.command} {getattr(args, 'record_id', None) or getattr(args, 'remote_dir', None) or getattr(args, 'machine', None) or ''}".rstrip())
def _cmd_adopt_remote(args: argparse.Namespace) -> int:
    from tools import remote_adoption

    try:
        payload = remote_adoption.adopt_remote(
            args.machine,
            args.remote_dir,
            confirm=args.confirm,
            tags=args.tag or [],
            case=args.case,
            note=args.note or "",
            overwrite=args.overwrite,
            catalog_dir=_catalog_dir(args),
        )
    except remote_adoption.RemoteAdoptionError as exc:
        print(f"xgc-sim: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(payload)
        return 0
    if payload.get("adopted"):
        print(
            f"adopted {payload['record_id']} [{payload['status']}]: {payload['runs']} run(s), "
            f"{payload['banners']} banner(s); record {payload['record_path']}"
        )
        backup = payload["input_backup"]
        print(f"input backup: {backup.get('path') or backup.get('reason')}")
        for item in payload["support_files"]:
            print(f"    {item}")
        return 0
    record = payload["record"]
    print(f"would adopt {record['record_id']} [{record['status']}] from {record['remote_dirs']}")
    print(f"  input sha256 {record['input_sha256']}")
    for run in record["runs"]:
        build = run.get("build") or {}
        commit = (build.get("git") or {}).get("commit", "")
        print(
            f"  job {run['job_id']:<45} {run.get('state'):<10} exit {run.get('exit_code')!s:<5} "
            f"nodes {run.get('nodes')!s:<4} used {run.get('walltime_used') or '-':<9} "
            f"banner {commit[:9] or '-'}"
        )
    refs = record.get("referenced_inputs") or {}
    print(f"  referenced files hashed: {len(refs.get('files') or {})}, missing: {refs.get('missing')}")
    print(f"  progress: {record.get('progress')}")
    if payload.get("blocked"):
        print(f"  BLOCKED: {payload['blocked']}")
    elif payload.get("existing"):
        print(f"  note: an adopted record already exists; --overwrite refreshes it")
    print("\nnothing was written; rerun with --confirm to apply")
    return 0


@catalog_sync.synced(lambda result, args: f"xgc-sim {args.command} {getattr(args, 'record_id', None) or getattr(args, 'remote_dir', None) or getattr(args, 'machine', None) or ''}".rstrip())
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


def _cmd_campaign(args: argparse.Namespace) -> int:
    try:
        if args.status:
            payload = campaign_tool.status(args.record_id, catalog_dir=_catalog_dir(args))
        else:
            payload = campaign_tool.build(
                args.record_id,
                confirm=args.confirm,
                replace=args.replace,
                catalog_dir=_catalog_dir(args),
            )
    except campaign_tool.CampaignError as exc:
        print(f"xgc-sim: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(payload)
        return 0
    if payload.get("confirm_required"):
        print(f"{payload['record_id']}: would package run {payload['for_run_id']} on {payload['machine']}")
        print(f"    run dir: {payload['run_dir']}")
        print(f"    archive: {payload['archive']}{'  (REPLACES the existing file)' if payload['replace'] else ''}")
        print(f"    command: {payload['command']}")
        print("\nnothing was started; rerun with --confirm")
        return 0
    print(_campaign_line(payload).strip())
    return 0 if payload.get("state") != "failed" else 1


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


@catalog_sync.synced(lambda result, args: f"xgc-sim {args.command} {getattr(args, 'record_id', None) or getattr(args, 'remote_dir', None) or getattr(args, 'machine', None) or ''}".rstrip())
def _cmd_sync_notebook(args: argparse.Namespace) -> int:
    from tools import notebook_csv

    payload = notebook_csv.sync(
        path=Path(args.path) if args.path else None,
        catalog_dir=_catalog_dir(args),
        pull_notes=not args.no_pull,
        confirm=args.confirm,
    )
    if args.json:
        _print_json(payload)
    else:
        print(f"notebook:       {payload['path']}")
        _print_sync(payload)
    return 0


def _cmd_init_catalog(args: argparse.Namespace) -> int:
    from tools import catalog_init

    try:
        payload = catalog_init.init_catalog(
            args.github_id,
            repo=Path(args.repo).expanduser(),
            data_dir=Path(args.data_dir).expanduser() if args.data_dir else None,
            remote=args.remote,
            write_config=not args.no_config,
            force_config=args.force_config,
        )
    except catalog_init.CatalogInitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(payload)
        return 0
    print(f"catalog:   {payload['repo']}")
    print(f"user dir:  {payload['user_dir']}   (catalog_dir)")
    print(f"data dir:  {payload['data_dir']}")
    for item in payload["created"]:
        print(f"  + {item}")
    for item in payload["kept"]:
        print(f"  = {item} (kept)")
    print("next:")
    for step in payload["next_steps"]:
        print(f"  - {step}")
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    from tools import catalog_init, paths

    rows = paths.layout()
    check = catalog_init.layout_check()
    if args.json:
        _print_json({
            "state_root_source": paths.STATE_ROOT_SOURCE,
            "data_root_source": paths.DATA_ROOT_SOURCE,
            "layout": check,
            "paths": rows,
        })
        return 0
    print(f"state root decided by: {paths.STATE_ROOT_SOURCE}")
    print(f"data root decided by:  {paths.DATA_ROOT_SOURCE}")
    print(f"config file:           {paths.config_file()}")
    if check["canonical"]:
        print(f"layout:                canonical (users/{check['user']})")
    else:
        print(f"layout:                NOT canonical: {check['detail']}")
    width = max(len(row["name"]) for row in rows)
    for row in rows:
        marker = " " if row["exists"] else "!"
        print(f"{marker} {row['name']:<{width}}  {row['kind']:<6}  {row['path']}")
    print("(! = does not exist yet)")
    return 0


def _parse_columns(items: list[str] | None) -> dict[str, str]:
    columns: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--column expects name=meaning, got {item!r}")
        name, meaning = item.split("=", 1)
        columns[name.strip()] = meaning.strip()
    return columns


@catalog_sync.synced(lambda result, args: f"xgc-sim shot {args.shot_command} {getattr(args, 'device', '')} {getattr(args, 'shot', '')}".rstrip())
def _cmd_shot(args: argparse.Namespace) -> int:
    from tools import shots

    try:
        if args.shot_command == "add":
            entry = shots.register_file(
                args.device, args.shot, args.file, kind=args.kind, diagnostic=args.diagnostic or "",
                quantity=args.quantity or "", columns=_parse_columns(args.column),
                origin=args.origin or "", note=args.note or "", copy=not args.no_copy,
            )
            if args.description is not None or args.time_ms is not None:
                shots.describe(args.device, args.shot, description=args.description, time_ms=args.time_ms)
            linked = shots.link()
            payload = {"registered": entry, "used_by": linked["linked"].get(
                f"{shots.shot_key(args.device, args.shot)}/{entry['name']}", [])}
        elif args.shot_command == "describe":
            payload = shots.describe(args.device, args.shot, description=args.description,
                                     time_ms=args.time_ms, note=args.note)
        elif args.shot_command == "link":
            payload = shots.link()
        elif args.shot_command == "show":
            payload = shots.load(args.device, args.shot)
        else:  # list
            payload = [shots.summarize(s) for s in shots.iter_shots()]
    except shots.ShotIndexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json or args.shot_command in ("show", "add", "describe", "link"):
        _print_json(payload)
    else:
        for row in payload:
            users = ",".join(row["used_by"]) or "-"
            print(f"{row['device']:<8} {str(row['shot']):<8} {row['files']:>3} file(s)  used by {users}  {row['description']}")
    return 0


def _print_mesh_plan(plan: dict[str, Any]) -> None:
    print(f"mesh {plan['mesh_id']} from {plan['base']} on {plan['machine']}: {plan['work_dir']}")
    print(f"tomms: {plan['tomms'].get('exe')} ({plan['tomms'].get('version')})")
    print("parameter changes vs base:")
    for key, (before, after) in plan["parameters_diff"].items():
        print(f"  {key:<34} {before!s:>24} -> {after}")
    print("inputs:")
    for item in plan["stage"]:
        print(f"  {item['role']:<12} {item['name']:<40} {item['from']}")
    if plan["missing"]:
        print(f"MISSING: {plan['missing']}")
    print(f"command: {plan['command']}")
    print(plan["note"])


@catalog_sync.synced(lambda result, args: f"xgc-sim mesh {args.mesh_command} {getattr(args, 'mesh_id', '') or ''}".rstrip())
def _cmd_mesh(args: argparse.Namespace) -> int:
    from tools import meshes
    from tools import ssh

    cmd = args.mesh_command
    try:
        if cmd == "import":
            payload = meshes.import_from_remote(
                args.machine, root=args.root, since=args.since,
                copy_equilibrium=not args.no_equilibrium, dry_run=args.dry_run,
            )
        elif cmd == "list":
            payload = meshes.search(args.query or "", device=args.device, status=args.status)
        elif cmd == "show":
            payload = meshes.load(args.mesh_id)
        elif cmd == "link":
            payload = meshes.link()
        elif cmd == "note":
            payload = meshes.annotate(
                args.mesh_id, note=args.note, ai_note=args.ai_note, description=args.description,
                device=args.device, shot=args.shot, status=args.status,
            )
        elif cmd == "generate":
            plan = meshes.plan_generation(
                args.mesh_id, base=args.base, overrides=_parse_columns(args.set),
                machine=args.machine, description=args.description or "",
            )
            if args.confirm:
                payload = meshes.start_generation(plan, confirm=True, replace=args.replace)
            else:
                if not args.json:
                    _print_mesh_plan(plan)
                    return 0
                payload = plan
        elif cmd == "status":
            payload = meshes.check_generation(args.mesh_id)
        elif cmd == "preview":
            payload = meshes.preview(args.mesh_id, max_bytes=int(args.max_mb) << 20)
        else:  # deploy
            payload = meshes.deploy(args.mesh_id, args.machine, args.directory, confirm=args.confirm)
    except (meshes.MeshIndexError, ssh.SSHError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json or cmd != "list":
        _print_json(payload)
    else:
        for row in payload:
            users = ",".join(row["used_by"]) or "-"
            print(f"{row['mesh_id']:<50} {row['status']:<8} {str(row['device']):<7} {str(row['shot'] or ''):<8} "
                  f"nphi={str(row['planes'] or '?'):<4} nodes={str(row['nodes'] or '?'):<8} "
                  f"{(row['generated_at'] or '')[:10]:<10} {row['attribution'] or '':<14} used by {users}")
    return 0


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--machine")
    parser.add_argument("--status", choices=list(catalog.STATUSES))
    parser.add_argument("--tag")
    parser.add_argument("--simulation-tag")
    parser.add_argument("--sub-tag")
    parser.add_argument("--origin", choices=list(catalog.ORIGINS))
    parser.add_argument(
        "--kind",
        choices=list(catalog.KINDS),
        help="production (n-numbered physics runs) or dev (d-numbered test runs)",
    )
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
        "--no-sync",
        action="store_true",
        default=argparse.SUPPRESS,
        help="do not git pull/commit/push the catalog repository around this command",
    )
    shared.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output",
    )
    shared.add_argument(
        "--catalog-dir",
        default=argparse.SUPPRESS,
        help="read another catalog's kb/simulations (e.g. a clone of someone else's) instead of your own",
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

    ai_note_parser = add("ai-note", help="set the AI note (the agent's own field)")
    ai_note_parser.add_argument("record_id")
    ai_note_parser.add_argument("ai_note")
    ai_note_parser.set_defaults(func=_cmd_ai_note)

    status_parser = add("status", help="set the simulation status")
    status_parser.add_argument("record_id")
    status_parser.add_argument("status", choices=list(catalog.STATUSES))
    status_parser.set_defaults(func=_cmd_status)

    archive_parser = add("archive", help="record where output was archived")
    archive_parser.add_argument("record_id")
    archive_parser.add_argument("--system", default="hpss")
    archive_parser.add_argument("--path", required=True)
    archive_parser.set_defaults(func=_cmd_archive)

    hpss_parser = add("hpss", help="tape (HPSS) index of a machine, joined to the records")
    hpss_parser.add_argument("machine")
    hpss_parser.add_argument("--refresh", action="store_true", help="run hsi ls -lR on the machine and rewrite kb/archives/<machine>_hpss.yaml")
    hpss_parser.add_argument("--from-file", help="with --refresh: parse this saved hsi listing instead of connecting")
    hpss_parser.add_argument("--link", action="store_true", help="plan copying unambiguous tape paths into records without archive.path")
    hpss_parser.add_argument("--confirm", action="store_true", help="with --link: write the plan")
    hpss_parser.set_defaults(func=_cmd_hpss)

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

    remote_parser = add(
        "adopt-remote",
        help="adopt a hand-submitted run from its remote directory (input, jobs, banners)",
    )
    remote_parser.add_argument("machine")
    remote_parser.add_argument("remote_dir")
    remote_parser.add_argument("--tag", action="append", help="extra tag (repeatable)")
    remote_parser.add_argument("--case", help="kb case the run started from")
    remote_parser.add_argument("--note", help="human note for the record")
    remote_parser.add_argument(
        "--overwrite", action="store_true", help="refresh an existing adopted record"
    )
    remote_parser.add_argument("--confirm", action="store_true")
    remote_parser.set_defaults(func=_cmd_adopt_remote)

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

    campaign_parser = add(
        "campaign",
        help="build or check a simulation's HPC-Campaign (.aca) archive",
    )
    campaign_parser.add_argument("record_id")
    campaign_parser.add_argument(
        "--status", action="store_true", help="report the state; collect a finished build"
    )
    campaign_parser.add_argument(
        "--replace", action="store_true", help="overwrite an existing archive (with --confirm)"
    )
    campaign_parser.add_argument("--confirm", action="store_true")
    campaign_parser.set_defaults(func=_cmd_campaign)

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

    notebook_parser = add(
        "sync-notebook",
        help="rewrite kb/simulation_notebook.csv from the catalog (notes pull back)",
    )
    notebook_parser.add_argument("--path", help="CSV to write (default kb/simulation_notebook.csv)")
    notebook_parser.add_argument(
        "--no-pull", action="store_true", help="push only; ignore notes edited in the CSV"
    )
    notebook_parser.add_argument("--confirm", action="store_true")
    notebook_parser.set_defaults(func=_cmd_sync_notebook)

    paths_parser = add(
        "paths",
        help="show where shared files and this user's state resolve (XGC_CATALOG_DIR)",
    )
    paths_parser.set_defaults(func=_cmd_paths)

    init_parser = add(
        "init-catalog",
        help="create your catalog in the canonical layout (users/<github-id>/) and point the tools at it",
    )
    init_parser.add_argument("github_id", help="your GitHub id; names your directory users/<id>/")
    init_parser.add_argument("--repo", default="~/git/xgc-catalog", help="catalog repository (default ~/git/xgc-catalog)")
    init_parser.add_argument("--data-dir", help="products directory outside git (default ~/xgc-data)")
    init_parser.add_argument("--remote", help="git remote URL to add as origin")
    init_parser.add_argument("--no-config", action="store_true", help="do not write ~/.config/xgc-automation/config.yaml")
    init_parser.add_argument("--force-config", action="store_true", help="overwrite an existing config file and USER.md link")
    init_parser.set_defaults(func=_cmd_init_catalog)

    shot_parser = add(
        "shot",
        help="index of experimental shot data: files, column meanings, which simulations used them",
    )
    shot_commands = shot_parser.add_subparsers(dest="shot_command", required=True)

    def shot_add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        sub = shot_commands.add_parser(name, parents=[shared], **kwargs)
        sub.set_defaults(func=_cmd_shot)
        return sub

    s_add = shot_add("add", help="register one file (copied into <data_dir>/shots/<device>/<shot>/)")
    s_add.add_argument("device", help="tcv, d3d, asdexu, kstar, ...")
    s_add.add_argument("shot")
    s_add.add_argument("file")
    s_add.add_argument("--kind", choices=("profile", "equilibrium", "mesh", "diagnostic", "other"), default="other")
    s_add.add_argument("--diagnostic", help="instrument or source, e.g. 'CXRS system 3', 'Thomson'")
    s_add.add_argument("--quantity", help="what the file holds, e.g. T_i, n_e, geqdsk")
    s_add.add_argument("--column", action="append", metavar="NAME=MEANING",
                       help="column meaning and unit; repeat per column")
    s_add.add_argument("--origin", help="where it came from (e-mail, paper, MDSplus tree)")
    s_add.add_argument("--note")
    s_add.add_argument("--description", help="shot-level description")
    s_add.add_argument("--time-ms", type=float, help="shot-level reference time")
    s_add.add_argument("--no-copy", action="store_true", help="index the file where it is instead of copying")
    s_desc = shot_add("describe", help="set shot-level description, time, note")
    s_desc.add_argument("device")
    s_desc.add_argument("shot")
    s_desc.add_argument("--description")
    s_desc.add_argument("--time-ms", type=float)
    s_desc.add_argument("--note")
    s_show = shot_add("show", help="one shot's index in full")
    s_show.add_argument("device")
    s_show.add_argument("shot")
    shot_add("list", help="every indexed shot")
    shot_add("link", help="recompute used_by from the catalog's input hashes")

    mesh_parser = add(
        "mesh",
        help="mesh recipes and records: what each mesh was made from, TOMMS runs, deployments, which simulations used it",
    )
    mesh_commands = mesh_parser.add_subparsers(dest="mesh_command", required=True)

    def mesh_add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        sub = mesh_commands.add_parser(name, parents=[shared], **kwargs)
        sub.set_defaults(func=_cmd_mesh)
        return sub

    m_import = mesh_add("import", help="catalogue the meshes in a machine's mesh work tree (machines.yaml <machine>.mesh.root)")
    m_import.add_argument("--machine", help="machine with mesh.root (default: the first one that has it)")
    m_import.add_argument("--root", help="override the work tree to scan")
    m_import.add_argument("--since", default="2y", help="only directories touched since: 2y, 90d, YYYY-MM-DD, or 'all'")
    m_import.add_argument("--no-equilibrium", action="store_true", help="record equilibria by hash only, do not copy them into the data directory")
    m_import.add_argument("--dry-run", action="store_true", help="scan and report, write nothing")
    m_list = mesh_add("list", help="every mesh record")
    m_list.add_argument("query", nargs="?", default="")
    m_list.add_argument("--device")
    m_list.add_argument("--status", choices=("ok", "failed", "running", "unknown"))
    m_show = mesh_add("show", help="one mesh record in full")
    m_show.add_argument("mesh_id")
    mesh_add("link", help="recompute used_by from the catalog's input hashes")
    m_note = mesh_add("note", help="set the human fields of a record (note, description, device/shot corrections, status)")
    m_note.add_argument("mesh_id")
    m_note.add_argument("--note")
    m_note.add_argument("--ai-note")
    m_note.add_argument("--description")
    m_note.add_argument("--device")
    m_note.add_argument("--shot")
    m_note.add_argument("--status", choices=("ok", "failed", "running", "unknown"))
    m_gen = mesh_add("generate", help="plan (and with --confirm start) a tomms_meshgen run from an existing record's recipe")
    m_gen.add_argument("mesh_id", help="name of the new mesh; also the output file prefix")
    m_gen.add_argument("--base", required=True, help="existing mesh record whose recipe is the starting point")
    m_gen.add_argument("--set", action="append", metavar="KEY=VALUE", help="xgc_mesh_in override; repeat per key")
    m_gen.add_argument("--machine")
    m_gen.add_argument("--description")
    m_gen.add_argument("--confirm", action="store_true", help="stage the recipe and start tomms_meshgen (after reviewing the plan)")
    m_gen.add_argument("--replace", action="store_true", help="reuse an existing work directory")
    m_status = mesh_add("status", help="poll a running generation; records the outputs when it has finished")
    m_status.add_argument("mesh_id")
    m_prev = mesh_add("preview", help="pull node+ele into the data directory and draw the mesh (needs matplotlib)")
    m_prev.add_argument("mesh_id")
    m_prev.add_argument("--max-mb", type=int, default=200, help="refuse to pull outputs larger than this")
    m_dep = mesh_add("deploy", help="copy the outputs to a simulation machine and verify the hashes there")
    m_dep.add_argument("mesh_id")
    m_dep.add_argument("machine")
    m_dep.add_argument("directory")
    m_dep.add_argument("--confirm", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "no_sync", False):
        catalog_sync.disable_for_process()
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
