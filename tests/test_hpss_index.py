from datetime import datetime

from tools import hpss_index as h

LISTING = """/home/s/sku:
-rw-r-----    1 sku       sku      3015164482048 Jan 23  2024 n498pe_D3D_WPQH.tar
-rw-r-----    1 sku       sku         35609888 Jan 23  2024 n498pe_D3D_WPQH.tar.idx
-rw-r-----    1 sku       sku        451210240 Dec 20  2021 co432_deltaf_beta2.tar
-rw-r-----    1 sku       sku      70981141504 May 29  2024 n540_d3d_NT_no_field.tar
-rw-r-----    1 sku       sku      1500000000000 Jan 15  2026 n498po_copy.tar
drwxr-s---    3 sku       sku              512 Feb  5 10:00 restart_dir_pe459

/home/s/sku/restart_dir_pe459:
drwxr-s---    2 sku       sku             8704 Jul 14  2022 xgc.f0.bp.009.bp

/home/s/sku/restart_dir_pe459/xgc.f0.bp.009.bp:
-rw-r-----    1 sku       sku       1148328879 Jul 14  2022 data.0
-rw-r-----    1 sku       sku          6781434 Jul 14  2022 md.0
"""

RECORDS = [
    {"record_id": "n498pe", "simulation_id": "n498pe", "archive": {"path": None}},
    {"record_id": "n540pe", "simulation_id": "n540pe", "archive": {"path": None}},
    {"record_id": "n540po", "simulation_id": "n540po", "archive": {"path": None}},
    {"record_id": "co432", "legacy": {"shot": "co432"}, "archive": {"path": None}},
]


def test_parse_folds_bp_and_attaches_idx():
    entries = h.parse_listing(LISTING, now=datetime(2026, 10, 9))
    by_name = {e["name"]: e for e in entries}
    assert by_name["n498pe_D3D_WPQH.tar"]["htar_index"] is True
    assert "n498pe_D3D_WPQH.tar.idx" not in by_name
    assert by_name["xgc.f0.bp.009.bp"] == {
        "path": "/home/s/sku/restart_dir_pe459/xgc.f0.bp.009.bp", "name": "xgc.f0.bp.009.bp",
        "kind": "directory", "size": 1148328879 + 6781434, "files": 2, "modified": "2022-07-14",
    }
    assert by_name["n540_d3d_NT_no_field.tar"]["modified"] == "2024-05-29"


def test_match_suffix_exact_bare_number_fans_out():
    entries = h.match_records(h.parse_listing(LISTING), RECORDS)
    by_name = {e["name"]: e for e in entries}
    assert by_name["n498pe_D3D_WPQH.tar"]["records"] == ["n498pe"]
    assert by_name["n498po_copy.tar"]["records"] == []  # no n498po record: not n498pe
    assert by_name["n540_d3d_NT_no_field.tar"]["records"] == ["n540pe", "n540po"]
    assert by_name["co432_deltaf_beta2.tar"]["records"] == ["co432"]


def test_link_plan_unambiguous_only():
    index = h.build_index("perlmutter", LISTING)
    index["entries"] = h.match_records(index["entries"], RECORDS)
    plan = {p["record_id"]: p for p in h.link_plan(index, RECORDS)}
    assert plan["n498pe"]["action"] == "link"
    assert plan["n498pe"]["path"] == "/home/s/sku/n498pe_D3D_WPQH.tar"
    assert plan["n540pe"]["action"] == "link"  # one archive, two records: each record sees one
    assert "n498po" not in plan
