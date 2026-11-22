"""业务规则。

关键约束：
- 同一盲码（跨所有场次）只能对应一种配方；
- 两表都有数据且盲码集合一致才可封存，封存后两表固定；
- 揭盲沿备样、评分两表里的盲码，找齐与发起场次直接或间接相连的场次；
- 能否发布、未封存名单和配方均分都取自同一份已提交数据；
- 汇总在只读事务中完成，发布只占一个短写事务；写事务内重新取同一份
  快照核对，关联或封存状态有变化就按新快照重来一轮；
- 关联反复变化、多轮都无法完成判定时，提示重新发起；
- 失败时不留下本次发布记录。
"""
import csv
import io
import math
import sqlite3
from typing import NamedTuple

# 汇总/核对最多进行的轮数。每轮只在只读事务中汇总，再用短写事务核对；
# 若期间关联或封存状态变化，就按最新快照重来。超过轮数则提示重新发起。
MAX_UNBLIND_ROUNDS = 3


class BizError(Exception):
    """业务校验失败，消息直接展示给用户。"""


# ---------------------------------------------------------------- CSV 解析

def _nonempty_rows(text):
    reader = csv.reader(io.StringIO(text))
    rows = []
    for raw in reader:
        cells = [c.strip() for c in raw]
        if any(cells):
            rows.append(cells)
    return rows


def parse_samples_csv(text):
    """解析备样 CSV（盲码,配方），返回去重后的 [(code, recipe)]。

    同一盲码在文件内对应多种配方 -> 整份退回。
    """
    rows = _nonempty_rows(text)
    if rows and any(c.lower() in ("盲码", "code", "配方", "recipe") for c in rows[0]):
        rows = rows[1:]
    if not rows:
        raise BizError("备样 CSV 没有数据行")
    mapping = {}
    for i, cells in enumerate(rows, start=1):
        if len(cells) < 2:
            raise BizError(f"备样 CSV 第 {i} 行列数不足（需要：盲码,配方），整份退回")
        code, recipe = cells[0], cells[1]
        if not code or not recipe:
            raise BizError(f"备样 CSV 第 {i} 行存在空的盲码或配方，整份退回")
        if code in mapping and mapping[code] != recipe:
            raise BizError(f"备样 CSV 内盲码「{code}」对应了多种配方，整份退回")
        mapping[code] = recipe
    return sorted(mapping.items())


def parse_scores_csv(text):
    """解析评分 CSV（盲码,评委号,分数），返回去重后的 [(code, judge, score)]。

    同一评委对同一盲码给出不同分数 -> 整份退回。
    """
    rows = _nonempty_rows(text)
    if rows and any(
        c.lower() in ("盲码", "code", "评委", "评委号", "judge", "分数", "score")
        for c in rows[0]
    ):
        rows = rows[1:]
    if not rows:
        raise BizError("评分 CSV 没有数据行")
    entries = {}
    for i, cells in enumerate(rows, start=1):
        if len(cells) < 3:
            raise BizError(f"评分 CSV 第 {i} 行列数不足（需要：盲码,评委号,分数），整份退回")
        code, judge, score_text = cells[0], cells[1], cells[2]
        if not code or not judge:
            raise BizError(f"评分 CSV 第 {i} 行存在空的盲码或评委号，整份退回")
        try:
            score = float(score_text)
        except ValueError:
            raise BizError(f"评分 CSV 第 {i} 行分数无效：{score_text!r}，整份退回")
        if not math.isfinite(score):
            raise BizError(f"评分 CSV 第 {i} 行分数无效：{score_text!r}，整份退回")
        key = (code, judge)
        if key in entries and entries[key] != score:
            raise BizError(
                f"评分 CSV 内评委「{judge}」对盲码「{code}」给出了多个不同分数，整份退回"
            )
        entries[key] = score
    return sorted((code, judge, score) for (code, judge), score in entries.items())


# ---------------------------------------------------------------- 场次

def get_session(db, session_id):
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise BizError("场次不存在")
    return row


def create_session(db, name):
    name = (name or "").strip()
    if not name:
        raise BizError("场次名称不能为空")
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute("INSERT INTO sessions (name) VALUES (?)", (name,))
        db.execute("COMMIT")
        return cur.lastrowid
    except sqlite3.IntegrityError:
        db.execute("ROLLBACK")
        raise BizError(f"场次名称「{name}」已存在")
    except Exception:
        db.execute("ROLLBACK")
        raise


def list_sessions(db):
    return db.execute(
        """SELECT s.*,
                  (SELECT COUNT(*) FROM samples sm WHERE sm.session_id = s.id) AS sample_count,
                  (SELECT COUNT(*) FROM scores sc WHERE sc.session_id = s.id) AS score_count
           FROM sessions s ORDER BY s.id"""
    ).fetchall()


def session_stats(db, session_id):
    sample_codes = {
        r["code"]
        for r in db.execute("SELECT code FROM samples WHERE session_id = ?", (session_id,))
    }
    score_rows = db.execute(
        "SELECT code, COUNT(*) AS n FROM scores WHERE session_id = ? GROUP BY code",
        (session_id,),
    ).fetchall()
    score_codes = {r["code"] for r in score_rows}
    return {
        "sample_count": len(sample_codes),
        "score_count": sum(r["n"] for r in score_rows),
        "score_codes": len(score_codes),
        "codes_match": sample_codes == score_codes,
    }


# ---------------------------------------------------------------- 上传（整份替换）

def replace_samples(db, session_id, pairs):
    """整份替换某场备样表；任何校验失败都整份退回。"""
    if not pairs:
        raise BizError("备样 CSV 没有数据行，整份退回")
    codes = [c for c, _ in pairs]
    marks = ",".join("?" for _ in codes)
    db.execute("BEGIN IMMEDIATE")
    try:
        sess = get_session(db, session_id)
        if sess["sealed_at"] is not None:
            raise BizError("该场已封存，备样表已固定，整份退回")
        # 含已揭盲的码 -> 整份退回
        published = db.execute(
            f"SELECT DISTINCT code FROM published_codes WHERE code IN ({marks})", codes
        ).fetchall()
        if published:
            shown = "、".join(f"「{r['code']}」" for r in published[:10])
            raise BizError(f"备样包含已揭盲的盲码{shown}，整份退回")
        # 同一盲码跨场次也只能对应一种配方
        conflicts = db.execute(
            f"""SELECT DISTINCT code, recipe FROM samples
                WHERE code IN ({marks}) AND session_id != ?""",
            (*codes, session_id),
        ).fetchall()
        new_recipe = dict(pairs)
        bad = [r for r in conflicts if new_recipe[r["code"]] != r["recipe"]]
        if bad:
            detail = "；".join(
                f"盲码「{r['code']}」已被其他场次对应到配方「{r['recipe']}」" for r in bad[:10]
            )
            raise BizError(f"同一盲码只能对应一种配方：{detail}，整份退回")
        db.execute("DELETE FROM samples WHERE session_id = ?", (session_id,))
        db.executemany(
            "INSERT INTO samples (session_id, code, recipe) VALUES (?, ?, ?)",
            [(session_id, c, r) for c, r in pairs],
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def replace_scores(db, session_id, entries):
    """整份替换某场评分表；任何校验失败都整份退回。"""
    if not entries:
        raise BizError("评分 CSV 没有数据行，整份退回")
    db.execute("BEGIN IMMEDIATE")
    try:
        sess = get_session(db, session_id)
        if sess["sealed_at"] is not None:
            raise BizError("该场已封存，评分表已固定，整份退回")
        db.execute("DELETE FROM scores WHERE session_id = ?", (session_id,))
        db.executemany(
            "INSERT INTO scores (session_id, code, judge, score) VALUES (?, ?, ?, ?)",
            [(session_id, c, j, s) for c, j, s in entries],
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 封存

def seal_session(db, session_id):
    """两表都有数据且包含同一批盲码时才可封存；封存后两表固定。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        sess = get_session(db, session_id)
        if sess["sealed_at"] is not None:
            raise BizError("该场已封存")
        sample_codes = {
            r["code"]
            for r in db.execute(
                "SELECT code FROM samples WHERE session_id = ?", (session_id,)
            )
        }
        score_codes = {
            r["code"]
            for r in db.execute(
                "SELECT code FROM scores WHERE session_id = ?", (session_id,)
            )
        }
        if not sample_codes or not score_codes:
            raise BizError("备样表和评分表都有数据后才可封存")
        if sample_codes != score_codes:
            parts = []
            only_sample = sorted(sample_codes - score_codes)
            only_score = sorted(score_codes - sample_codes)
            if only_sample:
                parts.append("仅备样表有：" + "、".join(f"「{c}」" for c in only_sample[:10]))
            if only_score:
                parts.append("仅评分表有：" + "、".join(f"「{c}」" for c in only_score[:10]))
            raise BizError("两表包含的盲码不一致，不能封存（" + "；".join(parts) + "）")
        db.execute(
            "UPDATE sessions SET sealed_at = datetime('now') WHERE id = ?", (session_id,)
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 分组与揭盲

def compute_group(db, session_id):
    """以盲码为边（备样、评分两表都算），求与 session_id 直接或间接相连的场次集合。"""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for row in db.execute("SELECT id FROM sessions"):
        find(row["id"])
    code_sessions = {}
    for row in db.execute(
        "SELECT code, session_id FROM samples UNION SELECT code, session_id FROM scores"
    ):
        code_sessions.setdefault(row["code"], []).append(row["session_id"])
    for sids in code_sessions.values():
        for other in sids[1:]:
            union(sids[0], other)
    root = find(session_id)
    return {sid for sid in parent if find(sid) == root}


def _unsealed(db, group):
    marks = ",".join("?" for _ in group)
    return db.execute(
        f"""SELECT id, name FROM sessions
            WHERE id IN ({marks}) AND sealed_at IS NULL ORDER BY id""",
        sorted(group),
    ).fetchall()


def _published(db, group):
    marks = ",".join("?" for _ in group)
    return db.execute(
        f"""SELECT id, name FROM sessions
            WHERE id IN ({marks}) AND publication_id IS NOT NULL ORDER BY id""",
        sorted(group),
    ).fetchall()


def _unsealed_error(unsealed):
    names = "、".join(f"「{name}」" for _, name in unsealed)
    return BizError(f"同组还有未封存场次：{names}，本次揭盲被拒绝")


def compute_results(db, group):
    """对全组（必须都已封存）计算：盲码->配方、各场配方均分。"""
    ids = sorted(group)
    marks = ",".join("?" for _ in ids)
    code_recipe = {}
    for row in db.execute(
        f"SELECT DISTINCT code, recipe FROM samples WHERE session_id IN ({marks})", ids
    ):
        if row["code"] in code_recipe and code_recipe[row["code"]] != row["recipe"]:
            raise BizError(f"盲码「{row['code']}」在组内对应了多种配方")
        code_recipe[row["code"]] = row["recipe"]
    averages = db.execute(
        f"""SELECT sc.session_id AS session_id, sm.recipe AS recipe,
                   AVG(sc.score) AS avg_score, COUNT(*) AS score_count
            FROM scores sc
            JOIN samples sm ON sm.session_id = sc.session_id AND sm.code = sc.code
            WHERE sc.session_id IN ({marks})
            GROUP BY sc.session_id, sm.recipe
            ORDER BY sc.session_id, sm.recipe""",
        ids,
    ).fetchall()
    return code_recipe, averages


class _Plan(NamedTuple):
    """一次判定所依据的完整事实，全部取自同一份已提交数据。

    只包含与本次揭盲分组有关的内容，因此不相连场次的保存不会改变它。
    """

    group: tuple            # 同组场次 id，升序
    unsealed: tuple         # 组内未封存场次 (id, name)
    published: tuple        # 组内已揭盲场次 (id, name)
    code_recipe: tuple      # (code, recipe)，按 code 升序
    averages: tuple         # (session_id, recipe, avg_score, score_count)


def _build_plan(db, session_id):
    """在当前连接当前快照下，把分组、封存状态与汇总一次算齐。"""
    group = compute_group(db, session_id)
    unsealed = tuple((r["id"], r["name"]) for r in _unsealed(db, group))
    published = tuple((r["id"], r["name"]) for r in _published(db, group))
    if unsealed or published:
        # 反正不会发布，先报封存/重复揭盲问题，不必再算均分。
        code_recipe, averages = {}, []
    else:
        code_recipe, averages = compute_results(db, group)
    return _Plan(
        group=tuple(sorted(group)),
        unsealed=unsealed,
        published=published,
        code_recipe=tuple(sorted(code_recipe.items())),
        averages=tuple(
            (r["session_id"], r["recipe"], r["avg_score"], r["score_count"])
            for r in averages
        ),
    )


def _rollback_quietly(db):
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _read_plan(db, session_id):
    """只读事务内取一份前后一致的快照，不持有写锁。"""
    db.execute("BEGIN")
    try:
        plan = _build_plan(db, session_id)
        db.execute("COMMIT")
        return plan
    except Exception:
        _rollback_quietly(db)
        raise


def _ensure_publishable(plan, session_id):
    if session_id in {sid for sid, _ in plan.published}:
        raise BizError("该场已揭盲，不能重复揭盲")
    if plan.unsealed:
        raise _unsealed_error(plan.unsealed)
    if plan.published:
        raise BizError("该场所在组包含已揭盲场次，不能重复揭盲")


def _insert_publication(db, plan):
    cur = db.execute("INSERT INTO publications (created_at) VALUES (datetime('now'))")
    pub_id = cur.lastrowid
    db.executemany(
        "INSERT INTO publication_sessions (publication_id, session_id) VALUES (?, ?)",
        [(pub_id, sid) for sid in plan.group],
    )
    db.executemany(
        "INSERT INTO published_codes (publication_id, code, recipe) VALUES (?, ?, ?)",
        [(pub_id, code, recipe) for code, recipe in plan.code_recipe],
    )
    db.executemany(
        """INSERT INTO published_averages
           (publication_id, session_id, recipe, avg_score, score_count)
           VALUES (?, ?, ?, ?, ?)""",
        [(pub_id, *row) for row in plan.averages],
    )
    marks = ",".join("?" for _ in plan.group)
    db.execute(
        f"UPDATE sessions SET publication_id = ? WHERE id IN ({marks})",
        (pub_id, *plan.group),
    )
    return pub_id


def unblind(db, session_id):
    """揭盲：整组发布盲码对应配方和各场配方均分。

    1. 只读事务中汇总：沿备样、评分两表里的盲码找出与发起场次直接或
       间接相连的场次，计算未封存名单、已揭盲场次和配方均分。只读快照
       不持有写锁，不相连场次在此期间仍可保存备样/评分。
    2. 短写事务内重新取同一份数据并核对：只有与只读汇总完全一致才据此
       作出发布判定并写入。不一致说明期间关联或封存状态有变化，回滚后
       按新的已提交数据重来（新连入的已封存场次也会被纳入汇总）。
       这样能否发布、未封存名单和配方均分都来自作出最终判定的那份数据。
    3. 关联反复变化、多轮仍无法稳定判定时，提示重新发起；任何失败都
       回滚，不留下本次发布记录。
    """
    sess = get_session(db, session_id)
    if sess["publication_id"] is not None:
        raise BizError("该场已揭盲，不能重复揭盲")

    for _ in range(MAX_UNBLIND_ROUNDS):
        planned = _read_plan(db, session_id)

        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise BizError("数据库正忙，请稍后重新发起揭盲") from exc
            raise
        try:
            current = _build_plan(db, session_id)
        except Exception:
            _rollback_quietly(db)
            raise
        if current != planned:
            # 期间关联或封存状态有变化：以最新已提交数据重新汇总。
            _rollback_quietly(db)
            continue

        try:
            _ensure_publishable(current, session_id)
            pub_id = _insert_publication(db, current)
            db.execute("COMMIT")
            return pub_id
        except Exception:
            _rollback_quietly(db)
            raise

    raise BizError("场次关联或封存状态在汇总期间反复变化，本次无法完成判定，请重新发起揭盲")


def get_publication(db, publication_id):
    pub = db.execute(
        "SELECT * FROM publications WHERE id = ?", (publication_id,)
    ).fetchone()
    if pub is None:
        raise BizError("揭盲记录不存在")
    sessions = db.execute(
        """SELECT s.id, s.name, s.sealed_at FROM sessions s
           JOIN publication_sessions ps ON ps.session_id = s.id
           WHERE ps.publication_id = ? ORDER BY s.id""",
        (publication_id,),
    ).fetchall()
    codes = db.execute(
        "SELECT code, recipe FROM published_codes WHERE publication_id = ? ORDER BY code",
        (publication_id,),
    ).fetchall()
    averages = db.execute(
        """SELECT pa.session_id, s.name AS session_name,
                  pa.recipe, pa.avg_score, pa.score_count
           FROM published_averages pa JOIN sessions s ON s.id = pa.session_id
           WHERE pa.publication_id = ?
           ORDER BY pa.session_id, pa.recipe""",
        (publication_id,),
    ).fetchall()
    return pub, sessions, codes, averages
