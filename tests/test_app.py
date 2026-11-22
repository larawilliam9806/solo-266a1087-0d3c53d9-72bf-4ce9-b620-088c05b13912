"""端到端测试：上传 -> 封存 -> 分组揭盲 -> 发布后约束。"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import services  # noqa: E402
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


def test_detached_unsealed_session_no_longer_blocks(env):
    """另一场换交 CSV 与本场断开后，不再以该场未封存为由拒绝。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    upload(client, s1, "samples", "A,甲\nB,乙\n")
    upload(client, s1, "scores", "A,j1,80\nB,j1,85\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)
    upload(client, s2, "samples", "B,乙\nC,丙\n")
    upload(client, s2, "scores", "B,j2,75\nC,j2,70\n")

    # 第二场未封存且与本场相连 -> 拒绝
    page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    assert "未封存场次" in page and "第二场" in page

    # 第二场换交完全不同的 CSV，与本场断开关联，但仍未封存
    upload(client, s2, "samples", "X,丁\nY,戊\n")
    upload(client, s2, "scores", "X,j2,10\nY,j2,20\n")
    resp = client.post(f"/sessions/{s1}/unblind", follow_redirects=True)
    page = body(resp)
    assert "盲码对应配方" in page
    assert "未封存场次" not in page
    # 已脱离的第二场不进入本次汇总
    assert "第二场" not in page
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM publications").fetchone()["c"] == 1
    conn.close()


def test_newly_connected_sealed_session_is_included(env):
    """换交 CSV 后新连入且已封存的场次要纳入本次汇总。"""
    client, _ = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    upload(client, s1, "samples", "A,甲\n")
    upload(client, s1, "scores", "A,j1,80\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)
    upload(client, s2, "samples", "Z,癸\n")
    upload(client, s2, "scores", "Z,j2,10\n")
    # 第二场换交为与本场共用的盲码 A 后封存 -> 新连入且已封存
    upload(client, s2, "samples", "A,甲\n")
    upload(client, s2, "scores", "A,j2,40\n")
    client.post(f"/sessions/{s2}/seal", follow_redirects=True)
    page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    assert "第一场" in page and "第二场" in page
    # 两场都计入均分
    assert "80.000" in page and "40.000" in page


def _disconnect(db_path, session_id, code, recipe="丁", judge="jx", score=10.0):
    """用另一个连接把某场换交为完全不相连的数据。"""
    conn = connect(db_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM samples WHERE session_id = ?", (session_id,))
    conn.execute(
        "INSERT INTO samples (session_id, code, recipe) VALUES (?, ?, ?)",
        (session_id, code, recipe),
    )
    conn.execute("DELETE FROM scores WHERE session_id = ?", (session_id,))
    conn.execute(
        "INSERT INTO scores (session_id, code, judge, score) VALUES (?, ?, ?, ?)",
        (session_id, code, judge, score),
    )
    conn.execute("COMMIT")
    conn.close()


def test_unrelated_saves_do_not_invalidate(env):
    """汇总期间不相连场次的备样/评分保存，不影响本次判定。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    s3 = make_session(client, "第三场")  # 不相连
    upload(client, s1, "samples", "A,甲\n")
    upload(client, s1, "scores", "A,j1,80\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)
    upload(client, s2, "samples", "A,甲\n")
    upload(client, s2, "scores", "A,j2,60\n")
    client.post(f"/sessions/{s2}/seal", follow_redirects=True)
    upload(client, s3, "samples", "Q,辛\n")
    upload(client, s3, "scores", "Q,j3,30\n")

    original = services._read_plan
    state = {"n": 0}

    def hooked(db, session_id):
        plan = original(db, session_id)
        state["n"] += 1
        if state["n"] == 1:
            # 只读汇总之后、写事务核对之前，另一请求保存不相连场次
            _disconnect(db_path, s3, "Q", recipe="辛", judge="j3", score=88.0)
        return plan

    services._read_plan = hooked
    try:
        page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    finally:
        services._read_plan = original
    assert "盲码对应配方" in page
    assert "重新发起" not in page
    conn = connect(db_path)
    assert (
        conn.execute("SELECT score FROM scores WHERE session_id = ?", (s3,)).fetchone()[
            "score"
        ]
        == 88.0
    )
    conn.close()


def test_repeated_relation_changes_ask_to_retry(env):
    """关联反复变化、始终无法稳定判定时，提示重新发起且不留下发布记录。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    upload(client, s1, "samples", "A,甲\n")
    upload(client, s1, "scores", "A,j1,80\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)
    upload(client, s2, "samples", "B,乙\n")
    upload(client, s2, "scores", "B,j2,60\n")

    original = services._read_plan
    state = {"n": 0}

    def hooked(db, session_id):
        plan = original(db, session_id)
        state["n"] += 1
        # 每轮汇总后都改动第二场与本场的关联，使核对时数据已变
        if state["n"] % 2:
            _disconnect(db_path, s2, "A", recipe="甲", judge="j2", score=60.0)
        else:
            _disconnect(db_path, s2, "B", recipe="乙", judge="j2", score=60.0)
        return plan

    services._read_plan = hooked
    try:
        page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    finally:
        services._read_plan = original
    assert "重新发起" in page
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM publications").fetchone()["c"] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM sessions WHERE publication_id IS NOT NULL"
        ).fetchone()["c"]
        == 0
    )
    conn.close()


def test_stale_unsealed_member_is_not_used_for_rejection(env):
    """只读汇总看到相连未封存场次、核对时已脱离，应据提交数据放行。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    upload(client, s1, "samples", "A,甲\nB,乙\n")
    upload(client, s1, "scores", "A,j1,80\nB,j1,85\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)
    upload(client, s2, "samples", "B,乙\nC,丙\n")
    upload(client, s2, "scores", "B,j2,75\nC,j2,70\n")

    original = services._read_plan
    state = {"n": 0}

    def hooked(db, session_id):
        plan = original(db, session_id)
        state["n"] += 1
        if state["n"] == 1:
            # 只读汇总之后、写事务核对之前，第二场换交并断开关联
            _disconnect(db_path, s2, "X")
        return plan

    services._read_plan = hooked
    try:
        page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    finally:
        services._read_plan = original
    assert "未封存场次" not in page
    assert "盲码对应配方" in page


def test_sealed_session_connected_mid_flight_is_included(env):
    """汇总期间新连入且已封存的场次，重算后纳入本次汇总。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    upload(client, s1, "samples", "A,甲\n")
    upload(client, s1, "scores", "A,j1,80\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)

    original = services._read_plan
    state = {"n": 0}

    def hooked(db, session_id):
        plan = original(db, session_id)
        state["n"] += 1
        if state["n"] == 1:
            # 汇总与最终核对之间，新建一个已封存、与本场共用盲码的场次
            conn = connect(db_path)
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO sessions (name, sealed_at) VALUES (?, datetime('now'))",
                ("中途连入场",),
            )
            new_id = cur.lastrowid
            conn.execute(
                "INSERT INTO samples (session_id, code, recipe) VALUES (?, ?, ?)",
                (new_id, "A", "甲"),
            )
            conn.execute(
                "INSERT INTO scores (session_id, code, judge, score) VALUES (?, ?, ?, ?)",
                (new_id, "A", "j9", 20),
            )
            conn.execute("COMMIT")
            conn.close()
        return plan

    services._read_plan = hooked
    try:
        page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    finally:
        services._read_plan = original
    assert "中途连入场" in page
    assert "20.000" in page


def test_unsealed_group_member_leaves_no_publication(env):
    """组内仍有未封存场次时拒绝发布，且不留下任何发布记录。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    upload(client, s1, "samples", "A,甲\n")
    upload(client, s1, "scores", "A,j1,80\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)
    upload(client, s2, "samples", "A,甲\n")
    upload(client, s2, "scores", "A,j2,70\n")

    page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    assert "未封存场次" in page and "第二场" in page
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM publications").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM published_codes").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM published_averages").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM publication_sessions").fetchone()["c"] == 0
    conn.close()


def test_failure_rolls_back_publication(env):
    """发布写入中途失败时不留下任何本次发布记录。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    upload(client, s1, "samples", "A,甲\n")
    upload(client, s1, "scores", "A,j1,80\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)

    original = services._insert_publication

    def failing(db, plan):
        original(db, plan)  # 先写入部分记录
        raise RuntimeError("写入中途失败")

    services._insert_publication = failing
    try:
        with pytest.raises(RuntimeError):
            conn = connect(db_path)
            try:
                services.unblind(conn, s1)
            finally:
                conn.close()
    finally:
        services._insert_publication = original

    conn = connect(db_path)
    for table in ("publications", "publication_sessions", "published_codes", "published_averages"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0
    assert (
        conn.execute("SELECT publication_id FROM sessions WHERE id = ?", (s1,)).fetchone()[
            "publication_id"
        ]
        is None
    )
    conn.close()


def test_unrelated_save_during_write_check_does_not_fail(env):
    """写事务核对期间，不相连场次保存成功且不改变本次判定。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    s3 = make_session(client, "第三场")
    upload(client, s1, "samples", "A,甲\n")
    upload(client, s1, "scores", "A,j1,80\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)
    upload(client, s2, "samples", "A,甲\n")
    upload(client, s2, "scores", "A,j2,60\n")
    client.post(f"/sessions/{s2}/seal", follow_redirects=True)
    upload(client, s3, "samples", "Q,辛\n")
    upload(client, s3, "scores", "Q,j3,30\n")

    original = services._read_plan
    state = {"n": 0}

    def hooked(db, session_id):
        plan = original(db, session_id)
        state["n"] += 1
        if state["n"] == 1:
            # 只读汇总之后、写事务核对之前，另一请求保存不相连场次
            conn = connect(db_path)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM scores WHERE session_id = ?", (s3,))
            conn.execute(
                "INSERT INTO scores (session_id, code, judge, score) VALUES (?, ?, ?, ?)",
                (s3, "Q", "j3", 88),
            )
            conn.execute("COMMIT")
            conn.close()
        return plan

    services._read_plan = hooked
    try:
        page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    finally:
        services._read_plan = original
    assert "盲码对应配方" in page
    assert "重新发起" not in page
    conn = connect(db_path)
    assert conn.execute(
        "SELECT score FROM scores WHERE session_id = ?", (s3,)
    ).fetchone()["score"] == 88.0
    conn.close()


def test_stale_unsealed_list_is_rechecked_before_rejecting(env):
    """只读阶段若读到过期的未封存名单，最终判定须按已提交数据重新核对。"""
    client, db_path = env
    s1 = make_session(client, "第一场")
    s2 = make_session(client, "第二场")
    upload(client, s1, "samples", "A,甲\n")
    upload(client, s1, "scores", "A,j1,80\n")
    client.post(f"/sessions/{s1}/seal", follow_redirects=True)
    upload(client, s2, "samples", "A,甲\n")
    upload(client, s2, "scores", "A,j2,60\n")
    client.post(f"/sessions/{s2}/seal", follow_redirects=True)

    original = services._unsealed
    state = {"first": True}

    def stale(db, group):
        rows = original(db, group)
        if state["first"]:
            state["first"] = False
            # 模拟只读阶段读到旧快照：第二场当时还与本场相连且未封存
            return [{"id": s2, "name": "第二场"}]
        return rows

    services._unsealed = stale
    try:
        page = body(client.post(f"/sessions/{s1}/unblind", follow_redirects=True))
    finally:
        services._unsealed = original
    assert "盲码对应配方" in page
    assert "未封存场次" not in page
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM publications").fetchone()["c"] == 1
    conn.close()
