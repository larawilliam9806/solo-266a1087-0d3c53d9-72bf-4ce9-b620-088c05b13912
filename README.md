# 食品盲评揭盲系统

复评场次会沿用盲码。本系统按场管理备样与评分，封存后按"共用盲码相连"的分组整组揭盲发布。

技术栈：Flask + SQLite（WAL），gunicorn 运行，docker compose 一键启动。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在命名卷 `blind_data` 的 SQLite 文件里（容器内 `/data/app.db`）。

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
4. **揭盲**：任选一场发起。系统把共用盲码直接或间接相连的场次算作一组：
   - 组内有未封存场次 → 列出这些场次并拒绝本次揭盲；
   - 全部已封存 → 整组发布：盲码对应的配方、各场的配方均分。

## 规则说明

- **同一码只能对应一种配方**：文件内、跨场次（含已发布结果）都强制；冲突即整份退回。
- **整份退回**：CSV 有任何一行不合法、或含已揭盲的盲码，整份不保存。
- **未揭盲场次只显示录入和封存状态**：页面不展示任何盲码、配方、分数内容；揭盲后只显示已发布的结果。
- **不相连场次无需等待**：揭盲的分组与汇总在只读阶段完成，发布只占用一个短写事务（SQLite WAL 模式下读写互不阻塞），不相连场次的备样/评分保存不用等汇总结束。
- **以实际发布时为准**：写事务内会重新核对场次关系和封存状态，有变化按当前状态重新分组计算；任何一步失败都回滚，整组结果都不发布。

## 页面

| 路径 | 说明 |
| --- | --- |
| `GET /` | 场次列表（录入/封存/揭盲状态）、新建场次 |
| `GET /sessions/<id>` | 单场状态、上传、封存、揭盲入口 |
| `POST /sessions/<id>/samples` | 上传备样 CSV |
| `POST /sessions/<id>/scores` | 上传评分 CSV |
| `POST /sessions/<id>/seal` | 封存 |
| `POST /sessions/<id>/unblind` | 揭盲（整组发布） |
| `GET /publications/<id>` | 已发布结果：同组场次、盲码→配方、各场配方均分 |

## 环境变量

- `DATABASE`：SQLite 文件路径（默认 `instance/app.db`，compose 中为 `/data/app.db`）
- `SECRET_KEY`：Flask session 密钥，生产环境请设置随机长字符串
