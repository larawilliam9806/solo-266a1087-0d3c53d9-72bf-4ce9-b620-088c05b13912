# 食品盲评揭盲系统

复评场次会沿用盲码。本系统按场管理备样与评分，封存后按"共用盲码相连"的分组整组揭盲发布。

技术栈：Flask + SQLite（WAL），gunicorn 运行，docker compose 一键启动。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在命名卷 `blind_data` 的 SQLite 文件里（容器内 `/data/app.db`），
重建容器不会丢数据。如需更换 `SECRET_KEY`：

```bash
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  docker compose up -d --build
```

容器带 `/healthz` 健康检查；`docker compose ps` 可看到 healthy 状态。

停止与清理：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 连数据卷一起删除（数据不可恢复）
```

本地开发：

```bash
pip install -r requirements.txt
DATABASE=/tmp/app.db flask --app wsgi run
# 或 gunicorn --bind 0.0.0.0:8000 wsgi:app
```

运行测试：

```bash
python3 -m pytest tests/
```

## 使用流程

1. **新建场次**：首页输入场次名称。
2. **按场上传 CSV**（未封存时可重复上传，保存后整份替换本场现有数据）：
   - 备样 CSV：`盲码,配方`（首行表头可省略；UTF-8 编码）
   - 评分 CSV：`盲码,评委号,分数`
3. **封存**：备样表和评分表都有数据、且两表包含的盲码集合完全一致时才可封存；封存后两表固定，不能再改。
4. **揭盲**：任选一场发起。系统沿备样、评分两表里的盲码，把与发起场次直接或间接相连的场次算作一组：
   - 组内有未封存场次 → 列出这些场次并拒绝本次揭盲；
   - 全部已封存 → 整组发布：盲码对应的配方、各场的配方均分。

## 规则说明

- **同一码只能对应一种配方**：文件内、跨场次（含已发布结果）都强制；冲突即整份退回。
- **整份退回**：CSV 有任何一行不合法、或含已揭盲的盲码，整份不保存。
- **未揭盲场次只显示录入和封存状态**：页面不展示任何盲码、配方、分数内容；揭盲后只显示已发布的结果。
- **以实际发布时的已提交数据为准**：揭盲先在只读事务中汇总分组、未封存名单与配方均分，再在短写事务内用同一套查询重新取数核对；只有两次结果完全一致才据此发布，否则按最新已提交数据重新汇总。因此能否发布、未封存名单和均分都来自作出最终判定的那份数据。
- **换交 CSV 即时生效**：某场换交后与发起场次断开关联，就不再以"该场未封存"为由拒绝；反之新连入且已封存的场次会纳入本次汇总。
- **不相连场次无需等待**：只读汇总不持有写锁（SQLite WAL 下读写互不阻塞），期间不相连场次的备样/评分保存照常进行；它们不改变本组汇总，也不会使本次判定失效。
- **反复变化则重试**：关联或封存状态在汇总期间反复变化、多轮仍无法稳定判定时，提示重新发起揭盲。
- **失败不留记录**：任何一步失败都回滚，本次发布记录（含场次关联、盲码、均分）都不会留下。

## 页面

| 路径 | 说明 |
| --- | --- |
| `GET /` | 场次列表（录入/封存/揭盲状态）、新建场次 |
| `GET /healthz` | 健康检查（容器 healthcheck 使用） |
| `GET /sessions/<id>` | 单场状态、上传、封存、揭盲入口 |
| `POST /sessions/<id>/samples` | 上传备样 CSV |
| `POST /sessions/<id>/scores` | 上传评分 CSV |
| `POST /sessions/<id>/seal` | 封存 |
| `POST /sessions/<id>/unblind` | 揭盲（整组发布） |
| `GET /publications/<id>` | 已发布结果：同组场次、盲码→配方、各场配方均分 |

## 环境变量

- `DATABASE`：SQLite 文件路径（默认 `instance/app.db`，compose 中为 `/data/app.db`）
- `SECRET_KEY`：Flask session 密钥，生产环境请设置随机长字符串
