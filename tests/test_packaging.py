"""包材核算：模板化导出 / 解码端到端测试。"""

import copy

from app.db import connect
from fastapi.testclient import TestClient


def _scan(scan_no, seq, child, parent, revision=1):
    return {"scan_no": scan_no, "seq": seq, "revision": revision,
            "child": child, "parent": parent}


def _seed_basic(client):
    """两个托盘：p1 两个同形双盒箱；p2 一个空箱 + 一个嵌套箱。"""

    scans = [
        _scan("S1", 1, "B:b1", "C:c1"),
        _scan("S2", 2, "B:b2", "C:c1"),
        _scan("S3", 3, "C:c1", "P:p1"),
        _scan("S4", 4, "B:b3", "C:c2"),
        _scan("S5", 5, "B:b4", "C:c2"),
        _scan("S6", 6, "C:c2", "P:p1"),
        _scan("S7", 7, "C:c3", "P:p2"),            # 空箱
        _scan("S8", 8, "B:b5", "C:c5"),
        _scan("S9", 9, "C:c5", "C:c4"),
        _scan("S10", 10, "C:c4", "P:p2"),
    ]
    r = client.post("/scans/batch", json={"scans": scans})
    assert r.status_code == 200, r.text


def _tpl_index(pkg):
    return {t["template_id"]: t for t in pkg["templates"]}


def _case_map(pkg):
    out = {}
    for seg in pkg["pallets"]:
        for inst in seg["cases"]:
            root = next(s["code"] for s in inst["slots"] if s["path"] == [])
            out[root] = inst
    return out


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def test_export_happy_path_and_structure(client):
    _seed_basic(client)
    r = client.post("/packages/export", json={"pallets": ["P:p1", "P:p2"]})
    assert r.status_code == 200, r.text
    pkg = r.json()

    # 4 种形状：双盒箱、空箱、单盒嵌套内箱、含一箱的外箱
    assert len(pkg["templates"]) == 4
    tids = _tpl_index(pkg)
    double = next(t for t in pkg["templates"]
                  if t["children"] == [{"kind": "B", "count": 2, "template_id": None}])
    empty = next(t for t in pkg["templates"] if t["children"] == [])
    assert double and empty

    # c1 / c2 同形共用同一模板编号
    cases = _case_map(pkg)
    assert cases["C:c1"]["template_id"] == cases["C:c2"]["template_id"] == double["template_id"]
    # 空箱单独一个模板
    assert cases["C:c3"]["template_id"] == empty["template_id"]

    # 交付包给出每个托盘的模板引用与逐槽位实物码
    seg1 = next(s for s in pkg["pallets"] if s["pallet"] == "P:p1")
    assert {i["template_id"] for i in seg1["cases"]} == {double["template_id"]}
    codes_c1 = {s["code"] for s in cases["C:c1"]["slots"]}
    assert codes_c1 == {"C:c1", "B:b1", "B:b2"}


def test_same_shape_ignores_codes_and_sibling_order(client):
    # 两个箱：盒码完全不同、扫描顺序不同，但都是 2 盒 -> 必须同模板
    client.post("/scans/batch", json={"scans": [
        _scan("A1", 1, "B:alpha", "C:ca"),
        _scan("A2", 2, "B:beta", "C:ca"),
        _scan("A3", 3, "C:ca", "P:pa"),
        _scan("A4", 4, "C:cb", "P:pb"),
        _scan("A5", 5, "B:y", "C:cb"),
        _scan("A6", 6, "B:x", "C:cb"),
    ]})
    r = client.post("/packages/export", json={"pallets": ["P:pa", "P:pb"]})
    assert r.status_code == 200, r.text
    cases = _case_map(r.json())
    assert cases["C:ca"]["template_id"] == cases["C:cb"]["template_id"]


def test_different_box_counts_are_different_templates(client):
    client.post("/scans/batch", json={"scans": [
        _scan("A1", 1, "B:b1", "C:c1"),
        _scan("A2", 2, "C:c1", "P:p1"),
        _scan("A3", 3, "B:b2", "C:c2"),
        _scan("A4", 4, "B:b3", "C:c2"),
        _scan("A5", 5, "C:c2", "P:p2"),
    ]})
    pkg = client.post("/packages/export",
                      json={"pallets": ["P:p1", "P:p2"]}).json()
    cases = _case_map(pkg)
    assert cases["C:c1"]["template_id"] != cases["C:c2"]["template_id"]


def test_empty_case_is_distinct_shape(client):
    client.post("/scans/batch", json={"scans": [
        _scan("A1", 1, "C:empty", "P:p1"),
        _scan("A2", 2, "B:b1", "C:c1"),
        _scan("A3", 3, "C:c1", "P:p2"),
    ]})
    pkg = client.post("/packages/export",
                      json={"pallets": ["P:p1", "P:p2"]}).json()
    cases = _case_map(pkg)
    assert cases["C:empty"]["template_id"] != cases["C:c1"]["template_id"]
    empty_tpl = _tpl_index(pkg)[cases["C:empty"]["template_id"]]
    assert empty_tpl["children"] == []
    # 空箱实例只有根槽位一个实物码
    assert cases["C:empty"]["slots"] == [
        {"path": [], "kind": "C", "code": "C:empty"}]


def test_pallet_without_case_rejects_whole_export(client):
    _seed_basic(client)
    # p3 完全不存在；p2 有箱 -> 整次仍报错
    r = client.post("/packages/export", json={"pallets": ["P:p2", "P:p3"]})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "pallet_without_case" and body["pallet"] == "P:p3"

    # 把 p2 下的箱全部拆走（c3 空箱、c4 含嵌套子树），托盘下不再有任何箱
    client.post("/scans/batch", json={"scans": [
        _scan("Z1", 11, "C:c3", None),
        _scan("Z2", 12, "C:c4", None),
    ]})
    r2 = client.post("/packages/export", json={"pallets": ["P:p2"]})
    assert r2.status_code == 409 and r2.json()["error"] == "pallet_without_case"

    # 失败不写入任何模板
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0


def test_export_validation_limits(client):
    # 非托盘码
    r = client.post("/packages/export", json={"pallets": ["C:c1"]})
    assert r.status_code == 409 and r.json()["error"] == "bad_pallet_code"
    # 0 个 / 21 个托盘 -> schema 层 422
    assert client.post("/packages/export", json={"pallets": []}).status_code == 422
    many = [f"P:p{i}" for i in range(21)]
    assert client.post("/packages/export", json={"pallets": many}).status_code == 422


def test_duplicate_pallet_in_request_rejected(client):
    _seed_basic(client)
    r = client.post("/packages/export", json={"pallets": ["P:p1", "P:p1"]})
    # 业务层去重校验（pydantic 列表允许重复）
    assert r.status_code == 409 and r.json()["error"] == "duplicate_pallet"


def test_template_ids_stable_across_exports_and_corrections(client):
    _seed_basic(client)
    pkg1 = client.post("/packages/export",
                       json={"pallets": ["P:p1"]}).json()
    id_first = _case_map(pkg1)["C:c1"]["template_id"]

    # 第二次导出另一个托盘，同形结构沿用同一编号
    client.post("/scans/batch", json={"scans": [
        _scan("N1", 11, "B:b9", "C:c9"),
        _scan("N2", 12, "B:b8", "C:c9"),
        _scan("N3", 13, "C:c9", "P:p9"),
    ]})
    pkg2 = client.post("/packages/export",
                       json={"pallets": ["P:p9"]}).json()
    assert _case_map(pkg2)["C:c9"]["template_id"] == id_first

    # 源库更正：c1 拆掉一盒后形状改变；旧形状编号保留，新形状拿新编号
    client.post("/scans/batch", json={"scans": [
        _scan("S1", 1, "B:b1", None, revision=2),
    ]})
    pkg3 = client.post("/packages/export",
                       json={"pallets": ["P:p1"]}).json()
    new_id = _case_map(pkg3)["C:c1"]["template_id"]
    assert new_id != id_first
    # 未受影响的 c2 仍沿用旧编号
    assert _case_map(pkg3)["C:c2"]["template_id"] == id_first
    with connect() as conn:
        rows = {r["template_id"] for r in conn.execute("SELECT template_id FROM templates")}
    assert id_first in rows and new_id in rows


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def test_decode_roundtrip_restores_direct_relations(client):
    _seed_basic(client)
    pkg = client.post("/packages/export",
                      json={"pallets": ["P:p1", "P:p2"]}).json()
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 200, r.text
    out = r.json()

    pallets = {p["code"]: p["children"] for p in out["pallets"]}
    assert pallets == {"P:p1": ["C:c1", "C:c2"], "P:p2": ["C:c3", "C:c4"]}
    cases = {c["code"]: c["children"] for c in out["cases"]}
    assert cases["C:c1"] == ["B:b2", "B:b1"]
    assert cases["C:c3"] == []                       # 空箱
    assert cases["C:c4"] == ["C:c5"]                # 嵌套
    assert cases["C:c5"] == ["B:b5"]
    assert {b["code"] for b in out["boxes"]} == {f"B:b{i}" for i in range(1, 6)}
    assert all(b["children"] == [] for b in out["boxes"])
    assert out["node_count"] == out["physical_code_count"] == 12


def test_decode_uses_only_package_data_after_source_correction(client):
    _seed_basic(client)
    pkg = client.post("/packages/export",
                      json={"pallets": ["P:p1", "P:p2"]}).json()

    # 源库彻底更正：拆光全部层级并清空表，旧包仍能解码还原
    client.post("/scans/batch", json={"scans": [
        _scan("W1", 11, "C:c1", None),
        _scan("W2", 12, "C:c2", None),
        _scan("W3", 13, "C:c3", None),
        _scan("W4", 14, "C:c4", None),
    ]})
    with connect() as conn:
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM templates")
        conn.commit()

    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 200, r.text
    pallets = {p["code"]: p["children"] for p in r.json()["pallets"]}
    assert pallets == {"P:p1": ["C:c1", "C:c2"], "P:p2": ["C:c3", "C:c4"]}


def test_decode_missing_template_rejected(client):
    _seed_basic(client)
    pkg = client.post("/packages/export", json={"pallets": ["P:p1"]}).json()
    # 删掉被引用的模板定义
    used = pkg["pallets"][0]["cases"][0]["template_id"]
    pkg["templates"] = [t for t in pkg["templates"] if t["template_id"] != used]
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 422 and r.json()["error"] == "missing_template"


def test_decode_template_cycle_rejected(client):
    pkg = {
        "format_version": "1.0",
        "templates": [
            {"template_id": "T:1", "kind": "C", "children": [
                {"kind": "C", "count": 1, "template_id": "T:2"}]},
            {"template_id": "T:2", "kind": "C", "children": [
                {"kind": "C", "count": 1, "template_id": "T:1"}]},
        ],
        "pallets": [{
            "pallet": "P:p1",
            "cases": [{
                "template_id": "T:1",
                "slots": [{"path": [], "kind": "C", "code": "C:c1"}],
            }],
        }],
    }
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 422 and r.json()["error"] == "template_cycle"


def test_decode_duplicate_physical_code_rejected(client):
    _seed_basic(client)
    pkg = client.post("/packages/export", json={"pallets": ["P:p1"]}).json()
    # 两个箱实例里出现同一个盒码
    for inst in pkg["pallets"][0]["cases"]:
        for s in inst["slots"]:
            if s["kind"] == "B":
                s["code"] = "B:shared"
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 422 and r.json()["error"] == "duplicate_physical_code"

    # 托盘段放非 P: 码：角色校验拒绝
    pkg2 = copy.deepcopy(pkg)
    pkg2["pallets"][0]["pallet"] = "C:c1"
    r2 = client.post("/packages/decode", json=pkg2)
    assert r2.status_code == 422 and r2.json()["error"] == "bad_pallet_code"

    # 两个托盘段同一托盘码：整包拒绝（先撞到实物码重复，两类均为整包拒绝错误）
    pkg3 = copy.deepcopy(pkg)
    pkg3["pallets"].append(copy.deepcopy(pkg3["pallets"][0]))
    r3 = client.post("/packages/decode", json=pkg3)
    assert r3.status_code == 422
    assert r3.json()["error"] in ("duplicate_pallet", "duplicate_physical_code")


def test_decode_slot_count_mismatch_rejected(client):
    _seed_basic(client)
    pkg = client.post("/packages/export", json={"pallets": ["P:p1"]}).json()
    # 多加一个模板未声明的槽位实物码（携带多于声明 -> 数量不符）
    inst = pkg["pallets"][0]["cases"][0]
    inst["slots"].append({"path": [9], "kind": "B", "code": "B:extra"})
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 422 and r.json()["error"] == "slot_count_mismatch"

    # 重复槽位路径单独报错
    pkg2 = client.post("/packages/export", json={"pallets": ["P:p1"]}).json()
    inst2 = pkg2["pallets"][0]["cases"][0]
    first = inst2["slots"][1]
    inst2["slots"].append({"path": list(first["path"]), "kind": "B", "code": "B:dup"})
    r2 = client.post("/packages/decode", json=pkg2)
    assert r2.status_code == 422 and r2.json()["error"] == "duplicate_slot"


def test_decode_slot_role_mismatch_rejected(client):
    _seed_basic(client)
    pkg = client.post("/packages/export", json={"pallets": ["P:p1"]}).json()
    # 把盒槽位的实物码换成箱码
    for s in pkg["pallets"][0]["cases"][0]["slots"]:
        if s["kind"] == "B":
            s["code"] = "C:fake"
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 422 and r.json()["error"] == "slot_role_mismatch"

    # 声明角色与模板不符
    pkg2 = client.post("/packages/export", json={"pallets": ["P:p1"]}).json()
    pkg2["pallets"][0]["cases"][0]["slots"][1]["kind"] = "C"
    r2 = client.post("/packages/decode", json=pkg2)
    assert r2.status_code == 422 and r2.json()["error"] == "slot_role_mismatch"


def test_decode_node_count_cannot_exceed_physical_codes(client):
    # 恶意包：模板自称有 100 个盒槽，但只给 2 个实物码
    pkg = {
        "format_version": "1.0",
        "templates": [
            {"template_id": "T:1", "kind": "C", "children": [
                {"kind": "B", "count": 100, "template_id": None}]},
        ],
        "pallets": [{
            "pallet": "P:p1",
            "cases": [{
                "template_id": "T:1",
                "slots": [
                    {"path": [], "kind": "C", "code": "C:c1"},
                    {"path": [0], "kind": "B", "code": "B:b1"},
                ],
            }],
        }],
    }
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 422 and r.json()["error"] == "node_limit_exceeded"
    assert r.json()["node_count"] == 102  # 1 托盘 + 1 箱 + 100 盒


def test_decode_duplicate_pallet_segment_rejected(client):
    pkg = {
        "format_version": "1.0",
        "templates": [
            {"template_id": "T:1", "kind": "C", "children": []},
        ],
        "pallets": [
            {"pallet": "P:p1", "cases": [{
                "template_id": "T:1",
                "slots": [{"path": [], "kind": "C", "code": "C:c1"}],
            }]},
            {"pallet": "P:p1", "cases": [{
                "template_id": "T:1",
                "slots": [{"path": [], "kind": "C", "code": "C:c2"}],
            }]},
        ],
    }
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 422 and r.json()["error"] == "duplicate_pallet"


def test_decode_duplicate_template_id_rejected(client):
    pkg = {
        "format_version": "1.0",
        "templates": [
            {"template_id": "T:1", "kind": "C", "children": []},
            {"template_id": "T:1", "kind": "C", "children": []},
        ],
        "pallets": [{
            "pallet": "P:p1",
            "cases": [{
                "template_id": "T:1",
                "slots": [{"path": [], "kind": "C", "code": "C:c1"}],
            }],
        }],
    }
    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 422 and r.json()["error"] == "duplicate_template"


def test_export_then_decode_with_nested_template_refs(client):
    # 三层嵌套：外箱 -> 2 个同形内箱 -> 各 1 盒
    client.post("/scans/batch", json={"scans": [
        _scan("K1", 1, "B:b1", "C:i1"),
        _scan("K2", 2, "B:b2", "C:i2"),
        _scan("K3", 3, "C:i1", "C:outer"),
        _scan("K4", 4, "C:i2", "C:outer"),
        _scan("K5", 5, "C:outer", "P:p1"),
    ]})
    pkg = client.post("/packages/export", json={"pallets": ["P:p1"]}).json()
    tpls = _tpl_index(pkg)
    # 两个同形内箱共用模板
    inner_ids = {t["template_id"] for t in pkg["templates"]
                 if t["children"] == [{"kind": "B", "count": 1, "template_id": None}]}
    assert len(inner_ids) == 1
    outer = next(t for t in pkg["templates"]
                 if any(g["kind"] == "C" and g["count"] == 2 for g in t["children"]))
    assert outer["children"][0]["template_id"] == next(iter(inner_ids))

    r = client.post("/packages/decode", json=pkg)
    assert r.status_code == 200, r.text
    cases = {c["code"]: c["children"] for c in r.json()["cases"]}
    assert set(cases["C:outer"]) == {"C:i1", "C:i2"}
    assert cases["C:i1"] == ["B:b1"] and cases["C:i2"] == ["B:b2"]
    assert r.json()["node_count"] == 6  # 1 托盘+1 外箱+2 内箱+2 盒
