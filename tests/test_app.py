"""端到端测试：上传 -> 封存 -> 分组揭盲 -> 发布后约束。"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db import connect  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    db_path = str(tmp_path / "test.db")
    app = create_app({"DATABASE": db_path, "SECRET_KEY": "test", "TESTING": True})
    with app.test_client() as client:
        yield client, db_path


def make_session(client, name):
    resp = client.post("/sessions", data={"name": name})
    assert resp.status_code == 302
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


def upload(client, sid, kind, text):
    data = {"file": (io.BytesIO(text.encode("utf-8")), f"{kind}.csv")}
    return client.post(
        f"/sessions/{sid}/{kind}",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def body(resp):
    return resp.data.decode("utf-8")


def test_seal_requires_both_tables_and_same_codes(env):
    client, _ = env
    s1 = make_session(client, "第一场")
    # 两表都有数据才可封存
    assert "都有数据后才可封存" in body(client.post(f"/sessions/{s1}/seal", follow_redirects=True))
    upload(client, s1, "samples", "盲码,配方\nA,甲\nB,乙\n")
    assert "都有数据后才可封存" in body(client.post(f"/sessions/{s1}/seal", follow_redirects=True))
    # 盲码集合不一致不可封存
    upload(client, s1, "scores", "盲码,评委号,分数\nA,j1,80\n")
    page = body(client.post(f"/sessions/{s1}/seal", follow_redirects=True))
    assert "盲码不一致" in page and "B" in page
    # 一致后封存成功
    upload(client, s1, "scores", "盲码,评委号,分数\nA,j1,80\nB,j1,90\n")
    assert "已封存" in body(client.post(f"/sessions/{s1}/seal", follow_redirects=True))
    # 封存后两表固定
    assert "已封存" in body(upload(client, s1, "samples", "A,甲\nB,乙\n"))
    assert "已封存" in body(upload(client, s1, "scores", "A,j1,80\nB,j1,90\n"))


def test_same_code_single_recipe(env):
    client, _ = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    # 文件内同一码多种配方 -> 整份退回
    page = body(upload(client, s1, "samples", "A,甲\nA,乙\n"))
    assert "多种配方" in page
    # 跨场次同一码也只能对应一种配方
    upload(client, s1, "samples", "A,甲\n")
    page = body(upload(client, s2, "samples", "A,乙\n"))
    assert "只能对应一种配方" in page
    page = body(upload(client, s2, "samples", "A,甲\n"))
    assert "已保存" in page


def test_scores_csv_validation(env):
    client, _ = env
    s1 = make_session(client, "第一场")
    assert "分数无效" in body(upload(client, s1, "scores", "A,j1,abc\n"))
    assert "多个不同分数" in body(upload(client, s1, "scores", "A,j1,80\nA,j1,90\n"))
    # 完全相同的重复行去重后接受
    assert "已保存" in body(upload(client, s1, "scores", "A,j1,80\nA,j1,80\n"))


def test_group_unblind_flow(env):
    client, db_path = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")  # 与第一场共用盲码 B
    s3 = make_session(client, "第三场")  # 不相连

    upload(client, s1, "samples", "盲码,配方\nA,甲\nB,乙\n")
    upload(client, s1, "scores", "盲码,评委号,分数\nA,j1,80\nB,j1,90\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)

    upload(client, s2, "samples", "盲码,配方\nB,乙\nC,甲\n")
    upload(client, s2, "scores", "盲码,评委号,分数\nB,j2,70\nC,j2,60\n")

    upload(client, s3, "samples", "盲码,配方\nD,丙\n")
    upload(client, s3, "scores", "盲码,评委号,分数\nD,j3,50\n")
    client.post(f"/sessions/{s3}/seal", follow_redirects=True)

    # 组内有未封存场次（第二场）-> 列出并拒绝，整组不发布
    page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    assert "未封存场次" in page and "第二场" in page
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM publications").fetchone()["c"] == 0
    conn.close()

    # 封存第二幕后揭盲成功，整组发布
    client.post(f"/sessions/{s2}/seal", follow_redirects=True)
    resp = client.post(f"/sessions/{s1}/unblind", follow_redirects=True)
    page = body(resp)
    assert "盲码对应配方" in page
    for code, recipe in [("A", "甲"), ("B", "乙"), ("C", "甲")]:
        assert code in page and recipe in page
    # 各场配方均分：第一场 甲=80 乙=90；第二场 乙=70 甲=60
    for avg in ("80.000", "90.000", "70.000", "60.000"):
        assert avg in page

    # 不相连的第三场不受影响，仍未揭盲，且页面不泄露配方内容
    page3 = body(client.get(f"/sessions/{s3}"))
    assert "未揭盲" in page3 and "丙" not in page3 and "D" not in page3

    # 已揭盲的码后续备样整份退回
    s4 = make_session(client, "第四场")
    page = body(upload(client, s4, "samples", "A,甲\nE,丁\n"))
    assert "已揭盲" in page and "整份退回" in page
    page4 = body(client.get(f"/sessions/{s4}"))
    assert "未录入" in page4  # 整份退回，什么都没保存

    # 不相连场次的备样/评分照常可保存（S4 未封存且不含已揭盲的码）
    assert "已保存" in body(upload(client, s4, "samples", "E,丁\n"))
    assert "已保存" in body(upload(client, s4, "scores", "E,j4,55\n"))
    # 已揭盲场次不能重复揭盲
    page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    assert "已揭盲" in page


def test_indirect_connection_groups_together(env):
    client, _ = env
    # 一、二场共用 B，二、三场共用 C -> 一、三间接相连，同组
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    s3 = make_session(client, "第三场")
    upload(client, s1, "samples", "A,甲\nB,乙\n")
    upload(client, s1, "scores", "A,j1,80\nB,j1,85\n")
    upload(client, s2, "samples", "B,乙\nC,丙\n")
    upload(client, s2, "scores", "B,j2,75\nC,j2,70\n")
    upload(client, s3, "samples", "C,丙\nD,丁\n")
    upload(client, s3, "scores", "C,j3,65\nD,j3,60\n")
    for sid in (s1, s2, s3):
        client.post(f"/sessions/{sid}/seal", follow_redirects=True)
    resp = client.post(f"/sessions/{s1}/unblind", follow_redirects=True)
    page = body(resp)
    assert "第一场" in page and "第二场" in page and "第三场" in page
    for code in ("A", "B", "C", "D"):
        assert code in page
