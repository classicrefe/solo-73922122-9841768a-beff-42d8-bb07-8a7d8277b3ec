"""跨训练集/评估集的亲属泄漏审查（纯读计算，绝不写库）。

审查把以下三部分合并成一张“患者关系图”：

1. 已发布清单中的病例（含其已发布分组）与同患者关系；
2. 已发布清单输入依据中的亲属对；
3. 本次上传的补充病例（病例号、患者号、目标分组）与新增亲属对。

同一患者天然同组；亲属对连通分量内只要同时出现训练集（组 1）与评估集（组 2），
即构成跨组泄漏。对每个受影响的补充病例，输出通向相反组病例的最短“原始关系
路径”（每一步都标明是清单亲属对、补充亲属对还是同一患者）。

另外单独提示：新增亲属关系若把清单中原本分属两组的病例连通，会造成清单内部
跨组冲突（清单本身仍保持已发布状态，审查只报告、不修改）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    MAX_CASE_ROWS,
    MAX_RELATIVE_ROWS,
    ValidationError,
    _read_csv_rows,
)

GROUP_NAMES = {1: "训练集", 2: "评估集"}

# 单次响应中有意设置的上限：避免极端情况下把数十万条路径塞进网页。
MAX_PATHS_PER_SUPPLEMENT_CASE = 100
MAX_MANIFEST_CONFLICT_COMPONENTS = 100

SUPPLEMENT_CASE_HEADER_ALIASES = {
    "病例号": "case_id",
    "case_id": "case_id",
    "caseid": "case_id",
    "id": "case_id",
    "患者号": "patient_id",
    "patient_id": "patient_id",
    "patientid": "patient_id",
    "目标分组": "target_group",
    "目标组": "target_group",
    "target_group": "target_group",
    "targetgroup": "target_group",
    "分组": "target_group",
    "group": "target_group",
}

SUPPLEMENT_REL_HEADER_ALIASES = {
    "患者a": "patient_a",
    "患者_a": "patient_a",
    "patient_a": "patient_a",
    "患者b": "patient_b",
    "患者_b": "patient_b",
    "patient_b": "patient_b",
}

_GROUP_ALIASES = {
    "1": 1,
    "2": 2,
    "训练集": 1,
    "评估集": 2,
    "训练": 1,
    "评估": 2,
    "train": 1,
    "training": 1,
    "eval": 2,
    "evaluation": 2,
}


@dataclass(frozen=True)
class SupplementCase:
    case_id: str
    patient_id: str
    target_group: int


@dataclass(frozen=True)
class ReviewRelative:
    patient_a: str
    patient_b: str


# --------------------------------------------------------------------------- #
# CSV 解析与校验（失败整体拒绝，不产生任何结论）
# --------------------------------------------------------------------------- #
def _is_blank_csv(raw: bytes) -> bool:
    try:
        return raw.decode("utf-8-sig").strip() == ""
    except UnicodeDecodeError:
        return False


def _group_value(raw: str) -> Optional[int]:
    return _GROUP_ALIASES.get(raw.strip().lower())


def parse_supplement_cases_csv(
    raw: bytes, manifest_case_ids: Iterable[str]
) -> List[SupplementCase]:
    """解析补充病例 CSV（病例号, 患者号, 目标分组）。"""
    header, body = _read_csv_rows(raw)
    cols = [SUPPLEMENT_CASE_HEADER_ALIASES.get(h) for h in header]
    missing = [
        name
        for name in ("case_id", "patient_id", "target_group")
        if name not in (cols or [])
    ]
    if missing:
        raise ValidationError([
            "补充病例 CSV 表头缺少列：" + "、".join(missing)
            + f"。当前表头：{', '.join(header)}"
        ])
    if len(body) > MAX_CASE_ROWS:
        raise ValidationError([
            f"补充病例最多 {MAX_CASE_ROWS} 行，当前 {len(body)} 行。"
        ])
    if not body:
        raise ValidationError(["补充病例 CSV 没有数据行。"])

    idx = {
        name: cols.index(name)
        for name in ("case_id", "patient_id", "target_group")
    }
    manifest_ids = {str(cid) for cid in manifest_case_ids}
    cases: List[SupplementCase] = []
    seen: Dict[str, int] = {}
    errors: List[str] = []
    for line_no, row in enumerate(body, start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        case_id = row[idx["case_id"]].strip()
        patient_id = row[idx["patient_id"]].strip()
        group_raw = row[idx["target_group"]].strip()
        if not case_id:
            errors.append(f"第 {line_no} 行：病例号为空")
            continue
        if not patient_id:
            errors.append(f"第 {line_no} 行：患者号为空（病例 {case_id}）")
            continue
        group = _group_value(group_raw)
        if group is None:
            errors.append(
                f"第 {line_no} 行：目标分组必须是 1（训练集）或 2（评估集），"
                f"实际为 “{group_raw}”（病例 {case_id}）"
            )
            continue
        if case_id in seen:
            errors.append(
                f"第 {line_no} 行：病例号 {case_id} 与第 {seen[case_id]} 行重复"
            )
            continue
        if case_id in manifest_ids:
            errors.append(
                f"第 {line_no} 行：病例号 {case_id} 已存在于所选清单中，"
                "补充病例号必须唯一"
            )
            continue
        seen[case_id] = line_no
        cases.append(
            SupplementCase(
                case_id=case_id, patient_id=patient_id, target_group=group
            )
        )
    if errors:
        raise ValidationError(errors)
    return cases


def parse_supplement_relatives_csv(
    raw: bytes, known_patients: Iterable[str]
) -> List[ReviewRelative]:
    """解析新增亲属对 CSV。空文件/仅表头均视为“没有新增亲属对”。"""
    if _is_blank_csv(raw):
        return []
    header, body = _read_csv_rows(raw)
    cols = [SUPPLEMENT_REL_HEADER_ALIASES.get(h) for h in header]
    missing = [
        name
        for name in ("patient_a", "patient_b")
        if name not in (cols or [])
    ]
    if missing:
        raise ValidationError([
            "新增亲属对 CSV 表头缺少列：" + "、".join(missing)
            + f"。当前表头：{', '.join(header)}"
        ])
    if len(body) > MAX_RELATIVE_ROWS:
        raise ValidationError([
            f"新增亲属对最多 {MAX_RELATIVE_ROWS} 行，当前 {len(body)} 行。"
        ])
    if not body:
        return []

    ia, ib = cols.index("patient_a"), cols.index("patient_b")
    known = {str(p) for p in known_patients}
    pairs: List[ReviewRelative] = []
    seen = set()
    errors: List[str] = []
    for line_no, row in enumerate(body, start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        a, b = row[ia].strip(), row[ib].strip()
        if not a or not b:
            errors.append(f"第 {line_no} 行：患者号不能为空")
            continue
        if a == b:
            errors.append(
                f"第 {line_no} 行：亲属对两端不能是同一患者（{a}）"
            )
            continue
        unknown = sorted({p for p in (a, b) if p not in known})
        if unknown:
            errors.append(
                f"第 {line_no} 行：患者 {'、'.join(unknown)} "
                "不存在于清单或补充病例中"
            )
            continue
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        pairs.append(ReviewRelative(patient_a=a, patient_b=b))
    if errors:
        raise ValidationError(errors)
    return pairs


# --------------------------------------------------------------------------- #
# 内部图结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _CaseRef:
    case_id: str
    patient_id: str
    group: int
    origin: str  # manifest | supplement


@dataclass(frozen=True)
class _Edge:
    patient_a: str
    patient_b: str
    source: str  # manifest | supplement


def _case_json(case: _CaseRef) -> dict:
    return {
        "case_id": case.case_id,
        "patient_id": case.patient_id,
        "group": case.group,
        "group_name": GROUP_NAMES[case.group],
        "origin": case.origin,
    }


def _case_label(case: _CaseRef) -> str:
    if case.origin == "supplement":
        return f"补充病例 {case.case_id}（{GROUP_NAMES[case.group]}）"
    return f"清单病例 {case.case_id}（{GROUP_NAMES[case.group]}）"


def _case_text(case: _CaseRef) -> str:
    if case.origin == "supplement":
        return (
            f"补充病例 {case.case_id}（患者 {case.patient_id}，"
            f"目标{GROUP_NAMES[case.group]}）"
        )
    return (
        f"清单病例 {case.case_id}（患者 {case.patient_id}，"
        f"{GROUP_NAMES[case.group]}）"
    )


class _PathIndex:
    """按起点惰性计算 BFS 最短路；结果确定性（邻接表已排序）。"""

    def __init__(self, adjacency: Dict[str, List[Tuple[str, _Edge]]]):
        self._adjacency = adjacency
        self._parents: Dict[str, Dict[str, Tuple[str, _Edge]]] = {}

    def _build(self, start: str) -> Dict[str, Tuple[str, _Edge]]:
        parents: Dict[str, Tuple[str, _Edge]] = {}
        visited = {start}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for neighbour, edge in self._adjacency.get(node, []):
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                parents[neighbour] = (node, edge)
                queue.append(neighbour)
        return parents

    def edges_to(self, start: str, target: str) -> Optional[List[_Edge]]:
        if start == target:
            return []
        parents = self._parents.get(start)
        if parents is None:
            parents = self._build(start)
            self._parents[start] = parents
        if target not in parents:
            return None
        edges: List[_Edge] = []
        current = target
        while current != start:
            previous, edge = parents[current]
            edges.append(edge)
            current = previous
        edges.reverse()
        return edges


def _build_path(
    from_case: _CaseRef,
    to_case: _CaseRef,
    edges: List[_Edge],
) -> dict:
    relations: List[dict] = []
    if not edges:
        relations.append({
            "kind": "same_patient",
            "patient_id": from_case.patient_id,
            "source": "case_patient",
            "source_name": "同一患者",
        })
        text = (
            f"{_case_text(from_case)} 与 {_case_text(to_case)}"
            f" 同属患者 {from_case.patient_id}"
        )
    else:
        parts: List[str] = [f"{_case_text(from_case)}"]
        current = from_case.patient_id
        for edge in edges:
            if edge.patient_a == current:
                arrival = edge.patient_b
            elif edge.patient_b == current:
                arrival = edge.patient_a
            else:  # 防御：BFS 不应产生断裂
                arrival = edge.patient_b
            source_name = (
                "清单亲属对" if edge.source == "manifest" else "补充亲属对"
            )
            relations.append({
                "kind": "relative",
                "patient_a": edge.patient_a,
                "patient_b": edge.patient_b,
                "from_patient": current,
                "to_patient": arrival,
                "source": edge.source,
                "source_name": source_name,
            })
            parts.append(
                f"—[{source_name} {edge.patient_a}–{edge.patient_b}]→"
                f" 患者 {arrival}"
            )
            current = arrival
        if to_case.patient_id != current:
            parts.append(f"→ 患者 {to_case.patient_id}")
        parts.append(f"→ {_case_label(to_case)}")
        text = " ".join(parts)
    return {
        "from_case": _case_json(from_case),
        "to_case": _case_json(to_case),
        "relations": relations,
        "text": text,
        "step_count": len(relations),
    }


def _new_relations_on_path(edges: Sequence[_Edge]) -> List[dict]:
    out = []
    seen = set()
    for edge in edges:
        if edge.source != "supplement":
            continue
        key = tuple(sorted((edge.patient_a, edge.patient_b)))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "patient_a": edge.patient_a,
            "patient_b": edge.patient_b,
            "source": "supplement",
            "source_name": "补充亲属对",
        })
    return out


# --------------------------------------------------------------------------- #
# 审查主体
# --------------------------------------------------------------------------- #
def review_leakage(
    manifest: dict,
    supplement_cases: Sequence[SupplementCase],
    supplement_relatives: Sequence[ReviewRelative],
) -> dict:
    """对一份已发布清单做跨组亲属泄漏审查。纯函数，不接触数据库。"""
    manifest_input = manifest.get("input") or {}
    assignments = (manifest.get("result") or {}).get("assignments") or {}

    cases: List[_CaseRef] = []
    for item in manifest_input.get("cases", []):
        case_id = item["case_id"]
        group = assignments.get(case_id)
        if group not in (1, 2):
            raise ValidationError([
                f"清单中病例 {case_id} 缺少有效已发布分组，无法审查。"
            ])
        cases.append(
            _CaseRef(
                case_id=case_id,
                patient_id=item["patient_id"],
                group=int(group),
                origin="manifest",
            )
        )
    for item in supplement_cases:
        cases.append(
            _CaseRef(
                case_id=item.case_id,
                patient_id=item.patient_id,
                group=item.target_group,
                origin="supplement",
            )
        )

    cases_by_patient: Dict[str, List[_CaseRef]] = {}
    for case in cases:
        cases_by_patient.setdefault(case.patient_id, []).append(case)
    for values in cases_by_patient.values():
        values.sort(key=lambda c: (c.case_id, c.origin))

    # 亲属边：清单亲属对优先；与清单重复的“新增”亲属对不算新增关系。
    edge_map: Dict[Tuple[str, str], _Edge] = {}
    for item in manifest_input.get("relatives", []):
        a, b = item["patient_a"], item["patient_b"]
        if a == b:
            continue
        edge_map[tuple(sorted((a, b)))] = _Edge(a, b, "manifest")
    new_relation_keys = set()
    for item in supplement_relatives:
        key = tuple(sorted((item.patient_a, item.patient_b)))
        if key in edge_map:
            continue
        edge_map[key] = _Edge(item.patient_a, item.patient_b, "supplement")
        new_relation_keys.add(key)

    patients = set(cases_by_patient)
    for edge in edge_map.values():
        patients.add(edge.patient_a)
        patients.add(edge.patient_b)

    adjacency: Dict[str, List[Tuple[str, _Edge]]] = {p: [] for p in patients}
    for edge in edge_map.values():
        adjacency[edge.patient_a].append((edge.patient_b, edge))
        adjacency[edge.patient_b].append((edge.patient_a, edge))
    for node in adjacency:
        adjacency[node].sort(
            key=lambda item: (
                item[0],
                item[1].source,
                item[1].patient_a,
                item[1].patient_b,
            )
        )

    # 连通分量
    component_of: Dict[str, int] = {}
    components: List[List[str]] = []
    for start in sorted(patients):
        if start in component_of:
            continue
        index = len(components)
        members: List[str] = []
        queue = deque([start])
        component_of[start] = index
        while queue:
            node = queue.popleft()
            members.append(node)
            for neighbour, _edge in adjacency.get(node, []):
                if neighbour not in component_of:
                    component_of[neighbour] = index
                    queue.append(neighbour)
        components.append(members)

    component_groups: List[set] = [set() for _ in components]
    component_manifest_groups: List[set] = [set() for _ in components]
    component_cases: List[List[_CaseRef]] = [[] for _ in components]
    for case in cases:
        index = component_of[case.patient_id]
        component_groups[index].add(case.group)
        component_cases[index].append(case)
        if case.origin == "manifest":
            component_manifest_groups[index].add(case.group)

    path_index = _PathIndex(adjacency)

    # 1) 受影响的补充病例：所在连通分量同时含两组
    affected: List[dict] = []
    clean: List[dict] = []
    for supplement in sorted(supplement_cases, key=lambda c: c.case_id):
        source = next(
            case
            for case in cases
            if case.origin == "supplement" and case.case_id == supplement.case_id
        )
        index = component_of[source.patient_id]
        if len(component_groups[index]) < 2:
            clean.append({
                "case_id": source.case_id,
                "patient_id": source.patient_id,
                "target_group": source.group,
                "target_group_name": GROUP_NAMES[source.group],
            })
            continue

        opposite = [
            case
            for case in component_cases[index]
            if case.group != source.group
        ]
        opposite.sort(key=lambda c: c.case_id)
        built: List[Tuple[int, str, dict]] = []
        for target in opposite:
            edges = path_index.edges_to(source.patient_id, target.patient_id)
            if edges is None:
                continue
            built.append(
                (len(edges), target.case_id, _build_path(source, target, edges))
            )
        built.sort(key=lambda item: (item[0], item[1]))
        kept = [item[2] for item in built[:MAX_PATHS_PER_SUPPLEMENT_CASE]]
        manifest_opposite = sum(1 for c in opposite if c.origin == "manifest")
        supplement_opposite = len(opposite) - manifest_opposite
        affected.append({
            "case_id": source.case_id,
            "patient_id": source.patient_id,
            "target_group": source.group,
            "target_group_name": GROUP_NAMES[source.group],
            "opposite_group": 3 - source.group,
            "opposite_group_name": GROUP_NAMES[3 - source.group],
            "opposite_case_count": len(opposite),
            "opposite_manifest_case_count": manifest_opposite,
            "opposite_supplement_case_count": supplement_opposite,
            "paths": kept,
            "paths_truncated": len(built) > len(kept),
            "omitted_path_count": max(0, len(built) - len(kept)),
        })

    # 2) 清单内部跨组冲突：按连通分量归并（同一批病例对只报一次）
    manifest_conflicts: List[dict] = []
    new_conflict_component_count = 0
    total_conflict_pair_count = 0
    for index, groups in enumerate(component_manifest_groups):
        if not {1, 2}.issubset(groups):
            continue
        group1 = [c for c in component_cases[index]
                  if c.origin == "manifest" and c.group == 1]
        group2 = [c for c in component_cases[index]
                  if c.origin == "manifest" and c.group == 2]
        candidates: List[Tuple[int, str, str, List[_Edge], List[dict]]] = []
        for a in group1:
            for b in group2:
                edges = path_index.edges_to(a.patient_id, b.patient_id)
                if edges is None:
                    continue
                new_relations = _new_relations_on_path(edges)
                candidates.append(
                    (len(edges), a.case_id, b.case_id, edges, new_relations)
                )
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        caused_by_new = [item for item in candidates if item[4]]
        if not caused_by_new:
            continue
        new_conflict_component_count += 1
        pick = caused_by_new[0]
        by_case = {c.case_id: c for c in component_cases[index]}
        manifest_conflicts.append({
            "case_a": _case_json(by_case[pick[1]]),
            "case_b": _case_json(by_case[pick[2]]),
            "path": _build_path(by_case[pick[1]], by_case[pick[2]], pick[3]),
            "caused_by_new_relations": True,
            "new_relations": pick[4],
            "manifest_case_count": len(group1) + len(group2),
            "conflicting_pair_count": len(group1) * len(group2),
        })
    manifest_conflicts.sort(
        key=lambda item: (item["case_a"]["case_id"], item["case_b"]["case_id"])
    )
    manifest_conflicts_truncated = (
        len(manifest_conflicts) > MAX_MANIFEST_CONFLICT_COMPONENTS
    )
    manifest_conflicts = manifest_conflicts[:MAX_MANIFEST_CONFLICT_COMPONENTS]

    has_leakage = bool(affected)
    has_manifest_conflict = new_conflict_component_count > 0
    if has_leakage and has_manifest_conflict:
        status = "leakage_and_manifest_conflict"
    elif has_leakage:
        status = "leakage"
    elif has_manifest_conflict:
        status = "manifest_conflict"
    else:
        status = "clean"

    if status == "leakage_and_manifest_conflict":
        conclusion = (
            f"发现跨组泄漏：{len(affected)} 个补充病例可经同患者/亲属关系"
            f"连通到相反组病例；另有 {new_conflict_component_count} 个"
            "清单连通分量因新增亲属关系而跨组冲突。"
        )
    elif status == "leakage":
        conclusion = (
            f"发现跨组泄漏：{len(affected)} 个补充病例可经同患者/亲属关系"
            "连通到相反组病例。"
        )
    elif status == "manifest_conflict":
        conclusion = (
            "补充病例未发现跨组泄漏；但新增亲属关系造成 "
            f"{new_conflict_component_count} 个清单连通分量跨组冲突，"
            "该清单已不再满足分组约束。"
        )
    else:
        conclusion = (
            f"未发现跨组泄漏：{len(clean)} 个补充病例均未被同患者或"
            "亲属关系连通到相反组病例。"
        )

    leak_component_count = sum(
        1 for groups in component_groups if {1, 2}.issubset(groups)
    )

    return {
        "status": status,
        "conclusion": conclusion,
        "has_leakage": has_leakage,
        "has_manifest_conflict": has_manifest_conflict,
        "leakage_case_count": len(affected),
        "clean_case_count": len(clean),
        "affected_supplement_cases": affected,
        "manifest_internal_conflicts": manifest_conflicts,
        "manifest_conflict_component_count": new_conflict_component_count,
        "manifest_conflict_pair_count": total_conflict_pair_count,
        "manifest_conflicts_truncated": manifest_conflicts_truncated,
        "clean_supplement_cases": clean,
        "stats": {
            "manifest_case_count": sum(
                1 for c in cases if c.origin == "manifest"
            ),
            "supplement_case_count": len(supplement_cases),
            "supplement_relative_count": len(supplement_relatives),
            "new_relative_count": len(new_relation_keys),
            "patient_count": len(patients),
            "component_count": len(components),
            "leak_component_count": leak_component_count,
        },
    }
