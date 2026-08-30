"""Mirror the simulation catalog to a Google Sheet.

The spreadsheet was the original system of record: a human ran a simulation and
typed its number, machine, and outcome into a row. People still want that view,
and it is how the work gets shared and skimmed. What it should stop being is the
place the data lives, because a hand-typed row cannot be verified against the
job that produced it.

So the direction is fixed:

    catalog (kb/simulations/*.yaml)  --push-->  Google Sheet     (machine facts)
    catalog                          <--pull--  Google Sheet     (note column only)

Everything the automation can determine -- number, machine, status, case, job
id, directory, archive location -- is pushed and overwrites whatever the sheet
says. The single ``note`` column belongs to the human and is pulled back, so
editing a comment in the browser still works the way it always did.

The sheet is a replica. Losing it costs nothing; the catalog rebuilds it. That
is deliberate: it means a network failure or a revoked credential can never
block a simulation.

Setup (once):

  1. Create a Google Cloud service account and download its JSON key.
  2. Share the target spreadsheet with the service account's email as Editor.
  3. Point the toolchain at both, either through the environment
     (``XGC_SHEET_CREDENTIALS``, ``XGC_SHEET_ID``) or the ``common`` section of
     ``machines.local.yaml``:

         common:
           sheet_sync:
             credentials: ~/.config/xgc/sheet-service-account.json
             spreadsheet_id: 1AbC...
             worksheet: simulations

The credential file is personal and must stay out of the repository.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Protocol

from tools import machines as machine_config
from tools import simulation_catalog as catalog

# Column order in the sheet. The first entry is the join key, and `note` is the
# only column the human owns, so it sits at the end where it is easy to edit.
COLUMNS = (
    "record_id",
    "number",
    "short_name",
    "machine",
    "status",
    "origin",
    "case",
    "tags",
    "runs",
    "latest_job_id",
    "latest_state",
    "submitted_at",
    "archive",
    "updated_at",
    "note",
)

KEY_COLUMN = "record_id"
HUMAN_COLUMN = "note"

DEFAULT_WORKSHEET = "simulations"
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)


class SheetSyncError(RuntimeError):
    """The sheet cannot be reached, or the request is unsafe."""


class Worksheet(Protocol):
    """The slice of the gspread worksheet API this module uses.

    Declared as a Protocol so the sync logic can be tested against a fake sheet
    without a network or a credential.
    """

    def get_all_values(self) -> list[list[str]]: ...

    # gspread 6 takes values first and range_name second (it was the other way
    # round in gspread 5), so both are always passed by keyword here.
    def update(self, values: list[list[Any]], range_name: str) -> Any: ...

    def resize(self, rows: int, cols: int) -> Any: ...


def _config() -> dict[str, Any]:
    """Read sheet settings from the machines config, overridden by env vars.

    Settings live under ``common.sheet_sync`` because the overlay loader only
    accepts top-level keys that machines.yaml already defines.
    """
    settings: dict[str, Any] = {}
    try:
        raw = (machine_config.load_machines().get("common") or {}).get("sheet_sync")
    except Exception:  # pragma: no cover - config problems surface elsewhere
        raw = None
    if isinstance(raw, dict):
        settings.update(raw)

    for env_key, key in (
        ("XGC_SHEET_CREDENTIALS", "credentials"),
        ("XGC_SHEET_ID", "spreadsheet_id"),
        ("XGC_SHEET_WORKSHEET", "worksheet"),
    ):
        value = os.environ.get(env_key)
        if value:
            settings[key] = value
    return settings


def resolve_settings(
    *,
    credentials: str | None = None,
    spreadsheet_id: str | None = None,
    worksheet: str | None = None,
) -> dict[str, Any]:
    """Resolve credentials, spreadsheet, and worksheet, explaining any gap."""
    settings = _config()
    resolved = {
        "credentials": credentials or settings.get("credentials"),
        "spreadsheet_id": spreadsheet_id or settings.get("spreadsheet_id"),
        "worksheet": worksheet or settings.get("worksheet") or DEFAULT_WORKSHEET,
    }
    missing = [
        key for key in ("credentials", "spreadsheet_id") if not resolved.get(key)
    ]
    resolved["missing"] = missing
    resolved["configured"] = not missing
    if resolved["credentials"]:
        path = Path(str(resolved["credentials"])).expanduser()
        resolved["credentials"] = str(path)
        resolved["credentials_exist"] = path.is_file()
    else:
        resolved["credentials_exist"] = False
    return resolved


def open_worksheet(
    *,
    credentials: str | None = None,
    spreadsheet_id: str | None = None,
    worksheet: str | None = None,
) -> Worksheet:
    """Open the target worksheet, creating it if the spreadsheet lacks it."""
    settings = resolve_settings(
        credentials=credentials,
        spreadsheet_id=spreadsheet_id,
        worksheet=worksheet,
    )
    if settings["missing"]:
        raise SheetSyncError(
            f"Google Sheets sync is not configured; missing "
            f"{', '.join(settings['missing'])}. Set them under sheet_sync in "
            f"machines.local.yaml or via XGC_SHEET_CREDENTIALS/XGC_SHEET_ID."
        )
    if not settings["credentials_exist"]:
        raise SheetSyncError(
            f"service account key not found: {settings['credentials']}"
        )

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise SheetSyncError(
            "Google Sheets sync needs the optional dependencies; install them "
            "with: uv sync --extra sheets"
        ) from exc

    try:
        creds = Credentials.from_service_account_file(
            settings["credentials"], scopes=list(SCOPES)
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(settings["spreadsheet_id"])
    except Exception as exc:  # gspread raises a wide range of API errors
        raise SheetSyncError(f"cannot open spreadsheet: {exc}") from exc

    try:
        return spreadsheet.worksheet(settings["worksheet"])
    except Exception:
        try:
            return spreadsheet.add_worksheet(
                title=settings["worksheet"],
                rows=200,
                cols=len(COLUMNS),
            )
        except Exception as exc:
            raise SheetSyncError(
                f"cannot open or create worksheet "
                f"{settings['worksheet']!r}: {exc}"
            ) from exc


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _row_for(summary: dict[str, Any]) -> list[str]:
    return [_cell(summary.get(column)) for column in COLUMNS]


def read_sheet(worksheet: Worksheet) -> dict[str, dict[str, str]]:
    """Read the sheet keyed by record id, tolerating reordered columns."""
    try:
        values = worksheet.get_all_values()
    except Exception as exc:
        raise SheetSyncError(f"cannot read worksheet: {exc}") from exc
    if not values:
        return {}
    header = [str(cell).strip() for cell in values[0]]
    if KEY_COLUMN not in header:
        raise SheetSyncError(
            f"worksheet has no {KEY_COLUMN!r} column; found {header}"
        )
    key_index = header.index(KEY_COLUMN)
    rows: dict[str, dict[str, str]] = {}
    for row in values[1:]:
        if key_index >= len(row):
            continue
        key = str(row[key_index]).strip().lower()
        if not key:
            continue
        rows[key] = {
            column: str(row[index]).strip() if index < len(row) else ""
            for index, column in enumerate(header)
        }
    return rows


def plan_sync(
    *,
    worksheet: Worksheet | None = None,
    records: Iterable[dict[str, Any]] | None = None,
    catalog_dir: Path | None = None,
    pull_notes: bool = True,
) -> dict[str, Any]:
    """Diff the catalog against the sheet. Writes nothing anywhere.

    Reports the rows that would be pushed and, separately, the notes a human
    changed in the browser that would be pulled back into the catalog.
    """
    summaries = (
        [catalog.summarize(record) for record in records]
        if records is not None
        else [
            catalog.summarize(record)
            for record in catalog.iter_records(catalog_dir=catalog_dir)
        ]
    )
    existing = read_sheet(worksheet) if worksheet is not None else {}

    push_new: list[str] = []
    push_changed: list[str] = []
    note_pulls: list[dict[str, str]] = []

    for summary in summaries:
        key = str(summary.get("record_id") or "").lower()
        row = existing.get(key)
        if row is None:
            push_new.append(key)
            continue

        sheet_note = row.get(HUMAN_COLUMN, "").strip()
        local_note = str(summary.get("note") or "").strip()
        if pull_notes and sheet_note and sheet_note != local_note:
            # The human edited the browser copy; that wins for this one field.
            note_pulls.append(
                {"record_id": key, "sheet_note": sheet_note, "catalog_note": local_note}
            )
            summary = dict(summary)
            summary["note"] = sheet_note

        if _row_for(summary) != [row.get(column, "") for column in COLUMNS]:
            push_changed.append(key)

    catalog_keys = {str(item.get("record_id") or "").lower() for item in summaries}
    orphans = sorted(set(existing) - catalog_keys)

    return {
        "rows": len(summaries),
        "push_new": push_new,
        "push_changed": push_changed,
        "note_pulls": note_pulls,
        "sheet_only": orphans,
        "counts": {
            "push_new": len(push_new),
            "push_changed": len(push_changed),
            "note_pulls": len(note_pulls),
            "sheet_only": len(orphans),
        },
        "writes": False,
    }


def sync(
    *,
    worksheet: Worksheet,
    catalog_dir: Path | None = None,
    pull_notes: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Pull human notes, then push the whole catalog to the sheet.

    Notes are pulled first so a note edited in the browser survives the push
    that immediately follows it. The push rewrites the sheet from the catalog,
    which is what keeps the two from drifting: the sheet never accumulates
    values nothing can explain.
    """
    records = list(catalog.iter_records(catalog_dir=catalog_dir))
    plan = plan_sync(
        worksheet=worksheet,
        records=records,
        catalog_dir=catalog_dir,
        pull_notes=pull_notes,
    )
    if not confirm:
        plan["synced"] = False
        plan["reason"] = (
            "confirmation required; review the diff and resubmit with confirm=True"
        )
        return plan

    pulled: list[str] = []
    if pull_notes:
        for pull in plan["note_pulls"]:
            catalog.set_note(
                pull["record_id"], pull["sheet_note"], catalog_dir=catalog_dir
            )
            pulled.append(pull["record_id"])
        if pulled:
            records = list(catalog.iter_records(catalog_dir=catalog_dir))

    values = [list(COLUMNS)]
    values.extend(_row_for(catalog.summarize(record)) for record in records)

    try:
        # Resize first so a shrinking catalog cannot leave stale rows behind
        # below the data the push writes.
        worksheet.resize(rows=max(len(values), 2), cols=len(COLUMNS))
        worksheet.update(values=values, range_name="A1")
    except Exception as exc:
        raise SheetSyncError(f"cannot write worksheet: {exc}") from exc

    plan["synced"] = True
    plan["pulled_notes"] = pulled
    plan["pushed_rows"] = len(values) - 1
    plan["writes"] = True
    return plan
