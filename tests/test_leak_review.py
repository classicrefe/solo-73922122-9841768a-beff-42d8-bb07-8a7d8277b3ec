"""泄漏审查测试：关系路径、清单内部冲突、拒绝语义、只读性与失效竞态。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.leak_review import (  # noqa: E402
    ReviewRelative,
    SupplementCase,
    parse_supplement_cases_csv,
    parse_supplement_relatives_csv,
    review_leakage,
)
from app.models import ValidationError  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "leak.db"
    monkeypatch.setenv("SPLIT_DB_PATH", str(db_file))
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    main.db.close()


CASES = (
    "病例号,患者号,标签\n"
    "C1,P1,1\nC2,P1,0\nC3,P2,1\nC4,P2,0\n"
    "C5,P3,1\nC6,P3,0\nC7,P4,1\nC8,P4,0\n"
)
RELATIVES = "患者A,患者B\nP1,P2\n"
MANIFEST_RELATIVES = [  # 与列表页示例一致
    {"patient_a": "P1", "patient_b": "P2"},
]


def _publish(client, cases_csv=CASES, rel_csv=RELATIVES):
    r = client.post(
        "/api/upload/cases",
        files={"file": ("c.csv", cases_csv.encode(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/upload/relatives",
        files={"file": ("r.csv", rel_csv.encode(), "text/csv")},
    )
    assert r.status_code == 200, r.text
    r = client.put(
        "/api/quotas",
        json={"g1_label0": 2, "g1_label1": 2, "g2_label0": 2, "g2_label1": 2},
    )
    assert r.status_code == 200, r.text
    body = client.post("/api/solve").json()
    assert body["result"]["status"] == "sat", body
    pub = client.post("/api/publish", json={"preview_token": body["preview_token"]})
    assert pub.status_code == 200, pub.text


def _review(client, manifest_id, cases_csv, rel_csv=None):
    files = {"cases_file": ("supp.csv", cases_csv.encode(), "text/csv")}
    if rel_csv is not None:
        files["relatives_file"] = (
            "rel.csv", rel_csv.encode("utf-8"), "text/csv"
        )
    return client.post(
        "/api/leak-review", data={"manifest_id": manifest_id}, files=files
    )


def _manifest(client):
    manifests = client.get("/api/manifests").json()["manifests"]
    assert len(manifests) == 1
    return manifests[0]["manifest_id"]


# --------------------------------------------------------------------------- #
# 纯函数：路径与结论
# --------------------------------------------------------------------------- #
def _fake_manifest(assignments):
    cases = []
    for case_id, (patient_id, group) in assignments.items():
        cases.append({"case_id": case_id, "patient_id": patient_id})
    return {
        "input": {"cases": cases, "relatives": MANIFEST_RELATIVES},
        "result": {"assignments": {k: v[1] for k, v in assignments.items()}},
    }


def test_transitive_path_through_manifest_and_new_relations():
    # 清单：C1/C2@P1 组2，C3@P3 组1；亲属 P1–P2（清单），P2–P3（新增）
    manifest = {
        "input": {
            "cases": [
                {"case_id": "C1", "patient_id": "P1"},
                {"case_id": "C2", "patient_id": "P1"},
                {"case_id": "C3", "patient_id": "P3"},
            ],
            "relatives": [{"patient_a": "P1", "patient_b": "P2"}],
        },
        "result": {"assignments": {"C1": 2, "C2": 2, "C3": 1}},
    }
    result = review_leakage(
        manifest,
        [SupplementCase("N1", "P1", 2)],
        [ReviewRelative("P2", "P3")],
    )
    assert result["status"] == "leakage_and_manifest_conflict"
    affected = result["affected_supplement_cases"]
    assert len(affected) == 1
    paths = affected[0]["paths"]
    assert paths, "必须列出通向相反组病例的原始关系路径"
    # 通向 C3（组1）的路径由清单亲属对 + 新增亲属对两段构成
    multi_hop = [p for p in paths if p["to_case"]["case_id"] == "C3"]
    assert multi_hop, "必须给出跨多条原始关系的路径"
    path = multi_hop[0]
    assert [r["source"] for r in path["relations"]] == ["manifest", "supplement"]
    assert "C3" in path["text"] and "P2" in path["text"]
    assert path["to_case"]["group"] == 1
    assert result["has_manifest_conflict"] is True
    assert any(
        c["caused_by_new_relations"]
        for c in result["manifest_internal_conflicts"]
    )


def test_same_patient_leak_is_detected_without_relatives():
    manifest = _fake_manifest({"C1": ("P1", 2), "C2": ("P9", 1)})
    result = review_leakage(
        manifest,
        [
            SupplementCase("N1", "P1", 1),
            SupplementCase("N2", "P5", 1),
        ],
        [],
    )
    assert result["status"] == "leakage"
    affected = {
        c["case_id"] for c in result["affected_supplement_cases"]
    }
    clean = {c["case_id"] for c in result["clean_supplement_cases"]}
    assert affected == {"N1"}
    assert clean == {"N2"}
    first = result["affected_supplement_cases"][0]["paths"][0]
    assert first["relations"][0]["kind"] == "same_patient"


def test_new_relation_across_manifest_groups_reports_internal_conflict():
    # 清单里 P1(组2) 与 P4(组1) 本不连通；新增 P2–P4 把它们连成一组。
    manifest = _fake_manifest(
        {"C1": ("P1", 2), "C2": ("P2", 2), "C3": ("P4", 1)}
    )
    result = review_leakage(
        manifest,
        [SupplementCase("N1", "P9", 1)],
        [ReviewRelative("P2", "P4")],
    )
    assert result["has_leakage"] is False
    assert result["has_manifest_conflict"] is True
    assert result["status"] == "manifest_conflict"
    conflicts = result["manifest_internal_conflicts"]
    assert conflicts and conflicts[0]["caused_by_new_relations"] is True
    assert conflicts[0]["new_relations"][0]["patient_a"] in ("P2", "P4")
    assert result["clean_supplement_cases"][0]["case_id"] == "N1"


def test_duplicate_new_relation_does_not_count_as_new():
    manifest = _fake_manifest({"C1": ("P1", 2), "C2": ("P2", 1)})
    result = review_leakage(
        manifest,
        [SupplementCase("N1", "P9", 1)],
        [ReviewRelative("P1", "P2")],  # 与清单亲属对完全重复
    )
    # 该冲突来自清单本身（亲属对已存在于清单），不算“新增关系造成”
    assert result["has_manifest_conflict"] is False
    assert result["manifest_internal_conflicts"] == []
    assert result["stats"]["new_relative_count"] == 0


# --------------------------------------------------------------------------- #
# CSV 校验
# --------------------------------------------------------------------------- #
def test_supplement_case_csv_rejects_duplicate_and_bad_group_and_manifest_id():
    csv_bytes = (
        "病例号,患者号,目标分组\n"
        "N1,P9,1\nN1,P8,2\nN2,P7,3\nC1,P1,1\n"
    ).encode()
    with pytest.raises(ValidationError) as err:
        parse_supplement_cases_csv(csv_bytes, ["C1"])
    text = "\n".join(err.value.errors)
    assert "重复" in text and "目标分组" in text and "已存在于所选清单" in text


def test_supplement_relatives_rejects_unknown_endpoint():
    with pytest.raises(ValidationError) as err:
        parse_supplement_relatives_csv(
            "患者A,患者B\nP1,P999\n".encode(), ["P1"]
        )
    assert "P999" in err.value.errors[0]


def test_supplement_relatives_empty_is_allowed():
    assert parse_supplement_relatives_csv(b"", ["P1"]) == []
    assert parse_supplement_relatives_csv(
        "患者A,患者B\n".encode(), ["P1"]
    ) == []


def test_supplement_case_row_limit():
    body = "病例号,患者号,目标分组\n" + "\n".join(
        f"N{i},P{i},{i % 2 + 1}" for i in range(201)
    )
    with pytest.raises(ValidationError) as err:
        parse_supplement_cases_csv(body.encode(), [])
    assert "200" in err.value.errors[0]


# --------------------------------------------------------------------------- #
# API 全链路
# --------------------------------------------------------------------------- #
def test_api_review_is_read_only_and_lists_clean_cases(client):
    _publish(client)
    mid = _manifest(client)
    before = client.get("/api/state").json()

    r = _review(
        client,
        mid,
        "病例号,患者号,目标分组\nN1,P9,1\nN2,P10,2\n",
        None,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["status"] == "clean"
    assert {c["case_id"] for c in body["result"]["clean_supplement_cases"]} == {
        "N1", "N2",
    }
    assert body["supplement"]["relatives"] == []

    after = client.get("/api/state").json()
    # 只读：输入、版本、清单状态完全不变
    assert after["dataset_version"] == before["dataset_version"]
    assert after["input"] == before["input"]
    assert after["manifests"] == before["manifests"]
    # 审查记录单独存档
    history = client.get("/api/leak-reviews").json()["reviews"]
    assert len(history) == 1 and history[0]["review_id"] == body["review_id"]


def test_api_review_reports_paths_and_manifest_conflict(client):
    _publish(client)
    mid = _manifest(client)
    # P1(组2)→P2(清单亲属)→P4(组1 新增亲属) 造成清单内部冲突；
    # 补充病例 N1 患者 P2 目标组1，经 P2–P1 连通组2病例。
    r = _review(
        client,
        mid,
        "病例号,患者号,目标分组\nN1,P2,1\n",
        "患者A,患者B\nP2,P4\n",
    )
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["status"] == "leakage_and_manifest_conflict"
    assert res["has_leakage"] and res["has_manifest_conflict"]
    affected = res["affected_supplement_cases"]
    assert affected[0]["case_id"] == "N1"
    assert affected[0]["paths"]
    assert all(p["to_case"]["group"] == 2 for p in affected[0]["paths"])
    conflicts = res["manifest_internal_conflicts"]
    assert any(c["caused_by_new_relations"] for c in conflicts)


def test_api_review_rejects_invalid_manifest(client):
    _publish(client)
    mid = _manifest(client)
    # 输入变动 -> 清单失效
    client.put("/api/quotas", json={"g1_label0": 2, "g1_label1": 2,
                                    "g2_label0": 2, "g2_label1": 2})
    r = _review(client, mid, "病例号,患者号,目标分组\nN1,P9,1\n")
    assert r.status_code == 409
    assert "失效" in r.json()["detail"]
    # 不存在的清单
    assert _review(client, "nope", "病例号,患者号,目标分组\nN1,P9,1\n").status_code == 404
    # 未发布任何清单时也没有可用结论
    assert client.get("/api/leak-reviews").json()["reviews"] == []


def test_api_review_rejects_bad_supplement_and_leaves_state_untouched(client):
    _publish(client)
    mid = _manifest(client)
    before = client.get("/api/state").json()
    bad_cases = "病例号,患者号,目标分组\nN1,P9,1\nN1,P8,2\n"
    r = _review(client, mid, bad_cases, "患者A,患者B\nNOPE,P1\n")
    assert r.status_code == 400
    assert client.get("/api/state").json() == before
    assert client.get("/api/leak-reviews").json()["reviews"] == []


def test_api_review_aborts_when_manifest_invalidated_midway(client, monkeypatch):
    """审查计算完成后若清单已失效，不得返回可用结论，也不得存档。"""
    _publish(client)
    mid = _manifest(client)
    import app.main as main
    original = main.review_leakage
    calls = {"n": 0}

    def invalidate_then_return(manifest, cases, relatives):
        # 模拟另一窗口在审查计算期间修改输入（使清单失效）。
        # 直接调用同步的 DB 方法，避免在服务事件循环线程里再走 HTTP。
        main.db.update_quotas({(1, 0): 2, (1, 1): 2, (2, 0): 2, (2, 1): 2})
        calls["n"] += 1
        return original(manifest, cases, relatives)

    monkeypatch.setattr(main, "review_leakage", invalidate_then_return)
    r = _review(client, mid, "病例号,患者号,目标分组\nN1,P9,1\n")
    assert calls["n"] == 1
    assert r.status_code == 409
    assert "作废" in r.json()["detail"]
    assert client.get("/api/leak-reviews").json()["reviews"] == []


def test_review_templates_download(client):
    for path in (
        "/api/leak-review/template/cases",
        "/api/leak-review/template/relatives",
    ):
        r = client.get(path)
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
