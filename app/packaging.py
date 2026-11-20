"""包材核算：同形箱模板的导出建包与自描述解码。

设计要点
--------
1. **结构判同**：以箱（C:）为根的子树，只看箱盒层级、空箱与子项份数；
   实物码与同层顺序不参与判同。结构规范化后取 SHA-256 指纹 ``shape_hash``。
2. **模板编号稳定**：同形模板只在首次出现时分配 ``T:000001`` 风格编号并落库，
   跨托盘、跨次导出沿用同一编号。
3. **交付包自描述**：模板结构定义内嵌在包内，解码不查源库——源库更正扫描后，
   旧包仍可凭包内数据还原原托盘/箱/盒的直属关系。
4. **整包拒绝**：缺模板、模板成环、实物码重复、编码数量或角色与模板不符，
   以及展开节点数超过实物码数，任一不满足都拒绝整包。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .db import transaction, write_lock
from .schemas import ExportIn, PackageIn

TEMPLATE_PREFIX = "T:"


class ExportError(Exception):
    """导出阶段错误（HTTP 409）：如托盘当前无箱。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class PackageError(Exception):
    """解码阶段整包校验错误（HTTP 422）。"""

    def __init__(self, error_code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = error_code
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------- #
# 结构规范化（纯函数）
# --------------------------------------------------------------------------- #
def _canonical_shape(shape: Dict[str, Any]) -> str:
    """结构对象规范化为稳定字符串，作为指纹输入。"""

    return json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def shape_hash_of(shape: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_shape(shape).encode("utf-8")).hexdigest()


def _build_shape(
    case_code: str,
    children_of: Dict[str, List[str]],
    memo: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """自底向上构造箱根子树的规范化结构（不含任何实物码）。

    同层顺序被忽略：同角色子项按 (角色, 子模板指纹) 聚合成份数。
    """

    if case_code in memo:
        return memo[case_code]
    # 防御性环检测：正常重放已保证层级无环，这里兜底避免无限递归
    memo[case_code] = {"kind": "C", "children": [], "_cycle": True}

    grouped: Dict[Tuple[str, Optional[str]], int] = defaultdict(int)
    for child in sorted(children_of.get(case_code, [])):
        if child.startswith("B:"):
            grouped[("B", None)] += 1
        elif child.startswith("C:"):
            child_shape = _build_shape(child, children_of, memo)
            if child_shape.get("_cycle"):
                raise ExportError(
                    "bad_hierarchy", f"包装层级在 {case_code} -> {child} 处存在环"
                )
            grouped[("C", shape_hash_of(child_shape))] += 1
        else:  # pragma: no cover - 重放规则已保证托盘不会作为子项
            raise ExportError("bad_hierarchy", f"箱 {case_code} 下出现非法子项 {child}")

    children = [
        {
            "kind": kind,
            "count": count,
            "template_id": None,
            # C 类子项的 template_id 在所有结构建完后统一回填
            "_child_hash": child_hash,
        }
        for (kind, child_hash), count in sorted(grouped.items(), key=lambda kv: kv[0])
    ]
    shape: Dict[str, Any] = {"kind": "C", "children": children}
    memo[case_code] = shape
    return shape


def _iter_template_nodes(shape: Dict[str, Any], tpl_map: Dict[str, Dict[str, Any]]):
    """迭代 DFS 展开模板结构，yield (槽位路径 tuple, 节点角色)。

    根槽位路径为 ()；子项按模板 children 顺序、份内下标展开。
    """

    stack: List[Tuple[Tuple[int, ...], Dict[str, Any]]] = [((), shape)]
    while stack:
        path, node = stack.pop()
        yield path, node["kind"]
        idx = 0
        # 逆序压栈以保持 DFS 顺序与 children 声明顺序一致
        for group in reversed(node["children"]):
            for _ in range(group["count"]):
                if group["kind"] == "B":
                    child_node = {"kind": "B", "children": []}
                else:
                    child_node = tpl_map[group["template_id"]]
                stack.append((path + (idx,), child_node))
                idx += 1


def _template_slot_count(
    shape: Dict[str, Any], tpl_map: Dict[str, Dict[str, Any]]
) -> int:
    """模板展开后的槽位数（含箱根）。"""

    return sum(1 for _ in _iter_template_nodes(shape, tpl_map))


# --------------------------------------------------------------------------- #
# 导出建包
# --------------------------------------------------------------------------- #
def _load_edges(conn) -> Dict[str, str]:
    return {row["child"]: row["parent"] for row in conn.execute("SELECT child, parent FROM edges")}


def _validate_export_request(pallets: List[str]) -> None:
    seen = set()
    for pallet in pallets:
        if not pallet.startswith("P:"):
            raise ExportError("bad_pallet_code", f"{pallet} 不是 P: 开头的托盘码", pallet=pallet)
        if pallet in seen:
            raise ExportError("duplicate_pallet", f"托盘码重复: {pallet}", pallet=pallet)
        seen.add(pallet)


def export_package(req: ExportIn) -> Dict[str, Any]:
    """按托盘导出交付包；任一托盘当前无箱则整次报错，不产生任何写入。"""

    _validate_export_request(req.pallets)

    with write_lock, transaction() as conn:
        edges = _load_edges(conn)
        children_of: Dict[str, List[str]] = defaultdict(list)
        for child, parent in edges.items():
            children_of[parent].append(child)

        # 防御性：确认当前层级无环（重放已保证），并收集各托盘直属箱
        pallet_cases: Dict[str, List[str]] = {}
        for pallet in req.pallets:
            cases = sorted(c for c in children_of.get(pallet, []) if c.startswith("C:"))
            if not cases:
                raise ExportError(
                    "pallet_without_case",
                    f"托盘 {pallet} 当前没有任何箱，整次导出拒绝",
                    pallet=pallet,
                )
            pallet_cases[pallet] = cases
        # 1) 自底向上构造所有箱根子树结构
        shape_memo: Dict[str, Dict[str, Any]] = {}
        case_shapes: Dict[str, Dict[str, Any]] = {}
        for pallet, cases in pallet_cases.items():
            for case_code in cases:
                case_shapes[case_code] = _build_shape(case_code, children_of, shape_memo)

        # 2) 结构指纹 -> 模板编号（先查库，未见过的形状分配新编号）
        row = conn.execute("SELECT MAX(template_id) AS max_id FROM templates").fetchone()
        max_seq = int(row["max_id"][len(TEMPLATE_PREFIX):]) if row["max_id"] else 0

        hash_to_id: Dict[str, str] = {}
        stored_rows = conn.execute("SELECT template_id, shape_hash FROM templates").fetchall()
        for r in stored_rows:
            hash_to_id[r["shape_hash"]] = r["template_id"]

        # 所有出现在导出子树中的箱形（含嵌套箱），按指纹排序分配，保证编号确定
        all_shapes: Dict[str, Dict[str, Any]] = {}
        for shape in shape_memo.values():
            all_shapes.setdefault(shape_hash_of(shape), shape)
        new_shapes: Dict[str, Dict[str, Any]] = {}
        for shape_hash in sorted(all_shapes):
            if shape_hash not in hash_to_id:
                max_seq += 1
                hash_to_id[shape_hash] = f"{TEMPLATE_PREFIX}{max_seq:06d}"
                new_shapes[shape_hash] = all_shapes[shape_hash]

        # 3) 回填结构中的子模板编号，落库新模板
        def resolve(shape: Dict[str, Any]) -> Dict[str, Any]:
            children = []
            for group in shape["children"]:
                item: Dict[str, Any] = {
                    "kind": group["kind"],
                    "count": group["count"],
                    "template_id": (
                        hash_to_id[group["_child_hash"]] if group["kind"] == "C" else None
                    ),
                }
                children.append(item)
            return {"kind": "C", "children": children}

        insert_rows = []
        for shape_hash, raw_shape in sorted(
            new_shapes.items(), key=lambda kv: hash_to_id[kv[0]]
        ):
            final_shape = resolve(raw_shape)
            insert_rows.append(
                (hash_to_id[shape_hash], shape_hash, _canonical_shape(final_shape))
            )
        if insert_rows:
            conn.executemany(
                "INSERT INTO templates(template_id, shape_hash, shape_json) VALUES (?, ?, ?)",
                insert_rows,
            )

        # 已存模板也需要结构定义（内嵌交付包），从库中读回
        tpl_def_rows = conn.execute(
            "SELECT template_id, shape_json FROM templates"
        ).fetchall()
        tpl_defs: Dict[str, Dict[str, Any]] = {
            r["template_id"]: json.loads(r["shape_json"]) for r in tpl_def_rows
        }

        # 4) 为每个箱根实例生成槽位实物码
        def build_slots(case_code: str, shape: Dict[str, Any]) -> List[Dict[str, Any]]:
            slots: List[Dict[str, Any]] = []

            def walk(code: str, node_shape: Dict[str, Any], path: List[int]) -> None:
                slots.append({"path": list(path), "kind": "C", "code": code})
                actual = children_of.get(code, [])
                slot_idx = 0
                for group in node_shape["children"]:
                    if group["kind"] == "B":
                        kids = sorted(c for c in actual if c.startswith("B:"))
                    else:
                        wanted_hash = group["_child_hash"]
                        kids = sorted(
                            c
                            for c in actual
                            if c.startswith("C:")
                            and shape_hash_of(shape_memo[c]) == wanted_hash
                        )
                    if len(kids) != group["count"]:  # pragma: no cover - 结构由实际边生成
                        raise ExportError(
                            "slot_count_mismatch",
                            f"箱 {code} 的 {group['kind']} 类子项份数与结构不符",
                        )
                    for kid in kids:
                        child_path = path + [slot_idx]
                        if group["kind"] == "B":
                            slots.append({"path": child_path, "kind": "B", "code": kid})
                        else:
                            walk(kid, shape_memo[kid], child_path)
                        slot_idx += 1

            walk(case_code, shape, [])
            return slots

        # 本次导出实际涉及的模板（含嵌套引用），结构定义全部内嵌交付包
        used_template_ids = sorted(
            {hash_to_id[h] for h in all_shapes}
        )
        templates_out = [
            {
                "template_id": tid,
                "kind": "C",
                "children": tpl_defs[tid]["children"],
            }
            for tid in used_template_ids
        ]

        pallets_out = []
        for pallet in req.pallets:
            cases_out = []
            for case_code in pallet_cases[pallet]:
                shape = case_shapes[case_code]
                tid = hash_to_id[shape_hash_of(shape)]
                cases_out.append(
                    {
                        "template_id": tid,
                        "slots": build_slots(case_code, shape),
                    }
                )
            pallets_out.append({"pallet": pallet, "cases": cases_out})

        return {
            "format_version": "1.0",
            "package_id": f"PKG:{uuid.uuid4().hex}",
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "templates": templates_out,
            "pallets": pallets_out,
        }


# --------------------------------------------------------------------------- #
# 解码（只读包内数据，不查源库）
# --------------------------------------------------------------------------- #
def _validate_templates(templates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """校验模板表：编号唯一、角色/份数合法、引用存在且无环。"""

    tpl_map: Dict[str, Dict[str, Any]] = {}
    for tpl in templates:
        tid = tpl["template_id"]
        if tid in tpl_map:
            raise PackageError("duplicate_template", f"模板编号重复: {tid}", template_id=tid)
        if tpl["kind"] != "C":
            raise PackageError("bad_template_kind", f"模板 {tid} 根必须为箱(C)", template_id=tid)
        for group in tpl["children"]:
            if group["kind"] not in ("B", "C"):
                raise PackageError(
                    "bad_template_kind",
                    f"模板 {tid} 含非法子项角色 {group['kind']}",
                    template_id=tid,
                )
            if group["kind"] == "B" and group["template_id"] is not None:
                raise PackageError(
                    "bad_template_role",
                    f"模板 {tid} 的盒(B)子项不得引用模板",
                    template_id=tid,
                )
            if group["kind"] == "C":
                ref = group["template_id"]
                if ref is None:
                    raise PackageError(
                        "missing_template_reference",
                        f"模板 {tid} 的箱(C)子项缺少模板引用",
                        template_id=tid,
                    )
        tpl_map[tid] = tpl

    # 引用必须存在
    for tid, tpl in tpl_map.items():
        for group in tpl["children"]:
            if group["kind"] == "C" and group["template_id"] not in tpl_map:
                raise PackageError(
                    "missing_template",
                    f"模板 {tid} 引用了包内不存在的模板 {group['template_id']}",
                    template_id=tid,
                    missing=group["template_id"],
                )

    # 环检测（三色 DFS，迭代实现）
    color: Dict[str, int] = {tid: 0 for tid in tpl_map}  # 0 白 1 灰 2 黑
    for root in tpl_map:
        if color[root] != 0:
            continue
        stack: List[Tuple[str, int]] = [(root, 0)]
        while stack:
            tid, next_child = stack[-1]
            if next_child == 0:
                color[tid] = 1
            refs = [g["template_id"] for g in tpl_map[tid]["children"] if g["kind"] == "C"]
            if next_child < len(refs):
                stack[-1] = (tid, next_child + 1)
                ref = refs[next_child]
                if color[ref] == 1:
                    raise PackageError(
                        "template_cycle",
                        f"模板引用在 {tid} -> {ref} 处形成环",
                        template_id=tid,
                    )
                if color[ref] == 0:
                    stack.append((ref, 0))
            else:
                color[tid] = 2
                stack.pop()
    return tpl_map


def decode_package(pkg: PackageIn) -> Dict[str, Any]:
    """仅凭包内数据还原直属关系；任何不一致都整包拒绝。"""

    data = pkg.model_dump()
    tpl_map = _validate_templates(data["templates"])

    pallets_in = data["pallets"]
    seen_pallets = set()
    physical_codes: set[str] = set()
    pallet_children: Dict[str, List[str]] = {}
    case_children: Dict[str, List[str]] = {}
    box_nodes: List[Dict[str, Any]] = []
    total_nodes = 0

    # 第一遍：逐实例核对模板引用、槽位数量与角色（此时得到精确的实物码集合）；
    # 第二遍依据已验证槽位还原直属关系。
    instance_roots: List[Tuple[str, str, Dict[Tuple[int, ...], Dict[str, Any]], str]] = []

    # 上限预判：任一实例按模板声明需要的槽位多于其实际携带的槽位码，
    # 即结构放大攻击 —— 直接拒绝，避免按恶意模板展开巨量节点。
    # 声明与携带相等时的缺槽位/错槽位由后续逐槽位校验精确报错。
    declared_nodes = len(pallets_in)
    for seg in pallets_in:
        for inst in seg["cases"]:
            if inst["template_id"] in tpl_map:
                declared_nodes += _template_slot_count(
                    tpl_map[inst["template_id"]], tpl_map
                )
    carried_codes = len(pallets_in) + sum(
        len(inst["slots"]) for seg in pallets_in for inst in seg["cases"]
    )
    amplification = any(
        inst["template_id"] in tpl_map
        and _template_slot_count(tpl_map[inst["template_id"]], tpl_map)
        > len(inst["slots"])
        for seg in pallets_in
        for inst in seg["cases"]
    )
    if amplification:
        raise PackageError(
            "node_limit_exceeded",
            "模板展开的包装节点数超过包内实物码数，整包拒绝",
            node_count=declared_nodes,
            physical_code_count=carried_codes,
        )

    for seg in pallets_in:
        pallet = seg["pallet"]
        if not pallet.startswith("P:"):
            raise PackageError(
                "bad_pallet_code",
                f"托盘段编码 {pallet} 不是 P: 开头的托盘码",
                pallet=pallet,
            )
        if pallet in seen_pallets:
            raise PackageError("duplicate_pallet", f"包内托盘码重复: {pallet}", pallet=pallet)
        seen_pallets.add(pallet)
        if pallet in physical_codes:
            raise PackageError(
                "duplicate_physical_code", f"实物码重复: {pallet}", duplicate=pallet
            )
        physical_codes.add(pallet)
        total_nodes += 1

        direct_cases: List[str] = []
        for inst in seg["cases"]:
            tid = inst["template_id"]
            if tid not in tpl_map:
                raise PackageError(
                    "missing_template",
                    f"箱实例引用了包内不存在的模板 {tid}",
                    missing=tid,
                )
            shape = tpl_map[tid]
            slots_by_path: Dict[Tuple[int, ...], Dict[str, Any]] = {}
            for slot in inst["slots"]:
                path_tuple = tuple(slot["path"])
                if path_tuple in slots_by_path:
                    raise PackageError(
                        "duplicate_slot",
                        f"模板 {tid} 的槽位 {list(path_tuple)} 重复提供实物码",
                        template_id=tid,
                    )
                slots_by_path[path_tuple] = slot

            expected_paths = list(_iter_template_nodes(shape, tpl_map))
            if len(slots_by_path) != len(expected_paths):
                raise PackageError(
                    "slot_count_mismatch",
                    f"模板 {tid} 需要 {len(expected_paths)} 个槽位实物码，"
                    f"实际 {len(slots_by_path)} 个",
                    template_id=tid,
                    expected=len(expected_paths),
                    actual=len(slots_by_path),
                )
            for path_tuple, expected_kind in expected_paths:
                slot = slots_by_path.get(path_tuple)
                if slot is None:
                    raise PackageError(
                        "slot_count_mismatch",
                        f"模板 {tid} 缺少槽位 {list(path_tuple)} 的实物码",
                        template_id=tid,
                    )
                if slot["kind"] != expected_kind:
                    raise PackageError(
                        "slot_role_mismatch",
                        f"槽位 {list(path_tuple)} 角色应为 {expected_kind}，"
                        f"实际声明 {slot['kind']}",
                        template_id=tid,
                    )
                expected_prefix = expected_kind + ":"
                if not slot["code"].startswith(expected_prefix):
                    raise PackageError(
                        "slot_role_mismatch",
                        f"槽位 {list(path_tuple)} 实物码 {slot['code']} 与角色 {expected_kind} 不符",
                        template_id=tid,
                    )
                code = slot["code"]
                if code in physical_codes:
                    raise PackageError(
                        "duplicate_physical_code",
                        f"实物码重复: {code}",
                        duplicate=code,
                    )
                physical_codes.add(code)
                total_nodes += 1

            root_code = slots_by_path[()]["code"]
            direct_cases.append(root_code)
            case_children.setdefault(root_code, [])
            instance_roots.append((pallet, root_code, slots_by_path, tid))

        pallet_children[pallet] = direct_cases

    # 节点上限：解码展开的包装节点数不得超过包内实物码数（精确判定）
    if total_nodes > len(physical_codes):
        raise PackageError(
            "node_limit_exceeded",
            "解码展开的包装节点数超过包内实物码数，整包拒绝",
            node_count=total_nodes,
            physical_code_count=len(physical_codes),
        )

    # 第二遍：依据已验证槽位还原直属关系
    for _pallet, root_code, slots_by_path, tid in instance_roots:
        shape = tpl_map[tid]
        for path_tuple, expected_kind in _iter_template_nodes(shape, tpl_map):
            if not path_tuple:
                continue
            parent_path = path_tuple[:-1]
            parent_code = slots_by_path[parent_path]["code"]
            child_code = slots_by_path[path_tuple]["code"]
            if expected_kind == "B":
                box_nodes.append({"code": child_code, "kind": "B", "children": []})
            if parent_code.startswith("C:"):
                case_children.setdefault(parent_code, []).append(child_code)

    pallets_out = [
        {"code": p, "kind": "P", "children": pallet_children[p]}
        for p in sorted(seen_pallets)
    ]
    cases_out = [
        {"code": c, "kind": "C", "children": case_children[c]}
        for c in sorted(case_children)
    ]
    boxes_out = sorted(box_nodes, key=lambda b: b["code"])
    return {
        "package_id": data.get("package_id"),
        "format_version": data.get("format_version", "1.0"),
        "pallets": pallets_out,
        "cases": cases_out,
        "boxes": boxes_out,
        "physical_code_count": len(physical_codes),
        "node_count": total_nodes,
    }
