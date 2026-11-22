import os

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db as db_module
from . import services


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.environ.get(
            "DATABASE", os.path.join(app.instance_path, "app.db")
        ),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # CSV 上限 16MB
    )
    if test_config:
        app.config.update(test_config)

    db_dir = os.path.dirname(app.config["DATABASE"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db_module.init_db(app.config["DATABASE"])
    db_module.init_app(app)

    @app.route("/")
    def index():
        db = db_module.get_db()
        sessions = services.list_sessions(db)
        publications = db.execute(
            "SELECT * FROM publications ORDER BY id DESC"
        ).fetchall()
        return render_template(
            "index.html", sessions=sessions, publications=publications
        )

    @app.post("/sessions")
    def create_session():
        db = db_module.get_db()
        try:
            session_id = services.create_session(db, request.form.get("name"))
        except services.BizError as e:
            flash(str(e), "error")
            return redirect(url_for("index"))
        return redirect(url_for("session_detail", session_id=session_id))

    @app.route("/sessions/<int:session_id>")
    def session_detail(session_id):
        db = db_module.get_db()
        sess = db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if sess is None:
            abort(404)
        # 未揭盲场次只展示录入和封存状态，不展示任何盲码/配方/分数内容
        stats = services.session_stats(db, session_id)
        return render_template("session.html", sess=sess, stats=stats)

    @app.post("/sessions/<int:session_id>/samples")
    def upload_samples(session_id):
        return _upload(session_id, kind="samples")

    @app.post("/sessions/<int:session_id>/scores")
    def upload_scores(session_id):
        return _upload(session_id, kind="scores")

    def _upload(session_id, kind):
        db = db_module.get_db()
        back = redirect(url_for("session_detail", session_id=session_id))
        file = request.files.get("file")
        if file is None or file.filename == "":
            flash("请选择要上传的 CSV 文件", "error")
            return back
        try:
            text = file.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("CSV 文件需要是 UTF-8 编码，整份退回", "error")
            return back
        try:
            if kind == "samples":
                pairs = services.parse_samples_csv(text)
                services.replace_samples(db, session_id, pairs)
                flash(f"备样表已保存，共 {len(pairs)} 个盲码", "ok")
            else:
                entries = services.parse_scores_csv(text)
                services.replace_scores(db, session_id, entries)
                flash(f"评分表已保存，共 {len(entries)} 条评分", "ok")
        except services.BizError as e:
            flash(str(e), "error")
        return back

    @app.post("/sessions/<int:session_id>/seal")
    def seal_session(session_id):
        db = db_module.get_db()
        try:
            services.seal_session(db, session_id)
            flash("已封存，两表固定", "ok")
        except services.BizError as e:
            flash(str(e), "error")
        return redirect(url_for("session_detail", session_id=session_id))

    @app.post("/sessions/<int:session_id>/unblind")
    def unblind_session(session_id):
        db = db_module.get_db()
        try:
            pub_id = services.unblind(db, session_id)
        except services.BizError as e:
            flash(str(e), "error")
            return redirect(url_for("session_detail", session_id=session_id))
        flash("揭盲成功，整组结果已发布", "ok")
        return redirect(url_for("publication_detail", publication_id=pub_id))

    @app.route("/publications/<int:publication_id>")
    def publication_detail(publication_id):
        db = db_module.get_db()
        try:
            pub, sessions, codes, averages = services.get_publication(
                db, publication_id
            )
        except services.BizError:
            abort(404)
        return render_template(
            "publication.html",
            pub=pub,
            sessions=sessions,
            codes=codes,
            averages=averages,
        )

    return app
