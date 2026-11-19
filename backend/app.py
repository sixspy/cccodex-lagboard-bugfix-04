
import hashlib, secrets, sqlite3
from contextlib import contextmanager
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# ---- 分裂切断簿钉死值（见 split/LEDGER.md）----
CUT_PATH = "甲"           # 等级牌与输出牌同笔连坐；乙不采用
PINNED_ON = "2026-11-19"  # 钉死日，分裂当日禁止改口

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

@contextmanager
def write_tx():
    """甲路径连坐事务：BEGIN IMMEDIATE 即分裂锁；块内任何异常整体回滚，两侧同撤。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

MODE_OF = {"正常": "可写", "告警": "降级只读", "违约": "冻结"}

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

def init_db():
    schema = connect()
    ensure_users(schema)
    schema.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL,
          level TEXT NOT NULL DEFAULT '正常',
          version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL,
          current_level TEXT NOT NULL DEFAULT '正常',
          current_output_mode TEXT NOT NULL DEFAULT '可写',
          version INTEGER NOT NULL DEFAULT 1,
          updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event TEXT NOT NULL,
          source TEXT,
          detail TEXT NOT NULL,
          state_level TEXT,
          state_mode TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS split_meta(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS split_divergence(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
    schema.close()

    with write_tx() as conn:
        # 旧库补列（幂等）
        if "level" not in _columns(conn, "sources"):
            conn.execute("ALTER TABLE sources ADD COLUMN level TEXT NOT NULL DEFAULT '正常'")
            # 存量按输出态反推等级，避免迁移即分裂
            conn.execute("""UPDATE sources SET level=CASE output_mode
                WHEN '可写' THEN '正常' WHEN '降级只读' THEN '告警' WHEN '冻结' THEN '违约' ELSE '正常' END""")
        if "version" not in _columns(conn, "sources"):
            conn.execute("ALTER TABLE sources ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        for col, ddl in (
            ("current_level", "TEXT NOT NULL DEFAULT '正常'"),
            ("current_output_mode", "TEXT NOT NULL DEFAULT '可写'"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in _columns(conn, "arrivals"):
                conn.execute(f"ALTER TABLE arrivals ADD COLUMN {col} {ddl}")

        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO sources(name,sla_minutes,output_mode,level,version) VALUES (?,?,?,?,1)",
                [("订单源", 15, "可写", "正常"), ("支付源", 20, "降级只读", "告警"), ("物流源", 30, "冻结", "违约")],
            )
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,current_level,current_output_mode,version,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("支付源", "2026-11-16T08:00:00", "2026-11-16T08:35:00", 35, "告警", "告警", "降级只读", 1, ""))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,current_level,current_output_mode,version,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("物流源", "2026-11-16T07:00:00", "2026-11-16T09:10:00", 130, "违约", "违约", "冻结", 1, ""))
        # 存量到达卡片与源目录对齐（同态）
        conn.execute("""UPDATE arrivals SET
              current_level=(SELECT s.level FROM sources s WHERE s.name=arrivals.source),
              current_output_mode=(SELECT s.output_mode FROM sources s WHERE s.name=arrivals.source)
            WHERE source IN (SELECT name FROM sources)""")

        # 钉死分裂切断簿；已在库且不一致 → 留“分裂偏了”，拒绝改口
        tampered = None
        row = conn.execute("SELECT value FROM split_meta WHERE key='cut_path'").fetchone()
        if row is None:
            conn.execute("INSERT INTO split_meta(key,value) VALUES ('cut_path',?)", (CUT_PATH,))
            conn.execute("INSERT INTO split_meta(key,value) VALUES ('pinned_on',?)", (PINNED_ON,))
        elif row["value"] != CUT_PATH:
            tampered = row["value"]
    if tampered is not None:
        # 留痕须独立落库（不随回滚消失），然后拒绝启动
        with write_tx() as div:
            div.execute("INSERT INTO split_divergence(reason,created_at) VALUES (?,?)",
                        (f"分裂偏了：库内切断路径为 {tampered}，切断簿钉死为 {CUT_PATH}，分裂当日禁止改口",
                         datetime.now().isoformat(timespec="seconds")))
        raise RuntimeError("分裂偏了：切断路径被改口，拒绝启动")

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return MODE_OF[level]

def log_audit(conn, event, source=None, detail="", level=None, mode=None):
    conn.execute(
        "INSERT INTO audit(event,source,detail,state_level,state_mode,created_at) VALUES (?,?,?,?,?,?)",
        (event, source, detail, level, mode, datetime.now().isoformat(timespec="seconds")),
    )

class LevelContention(Exception):
    """分裂锁争用：后交者不落半截写，事务回滚后单独留痕并返回 409。"""

    def __init__(self, source, detail, level, mode, current_version):
        super().__init__(detail)
        self.source, self.detail, self.level, self.mode = source, detail, level, mode
        self.current_version = current_version

def _record_contention(exc: LevelContention):
    with write_tx() as c:
        log_audit(c, "等级分裂争用", exc.source, exc.detail, exc.level, exc.mode)

def _apply_joint(conn, source, level, reason):
    """甲路径：等级牌、目录输出牌、到达卡片、审计流水同笔连坐。任一步失败由事务整体同撤。"""
    mode = mode_of(level)
    src = conn.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
    if not src:
        raise HTTPException(404, "未知源")
    conn.execute("UPDATE sources SET level=?, output_mode=?, version=version+1 WHERE name=?", (level, mode, source))
    now = datetime.now().isoformat(timespec="seconds")
    # 到达卡片：同源所有卡片同步为源的当前态（同一快照语义）
    conn.execute(
        "UPDATE arrivals SET current_level=?, current_output_mode=?, version=version+1, updated_at=? WHERE source=?",
        (level, mode, now, source),
    )
    log_audit(conn, "等级变更", source, reason, level, mode)
    return mode

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str
    expected_version: int | None = None

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    """值班席：登记到达。只推进等级，不绕过补齐解锁违约源。"""
    require_user(authorization)
    def parse(t): return datetime.fromisoformat(t)
    lag = max(0, int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60))
    try:
        with write_tx() as c:
            src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
            if not src: raise HTTPException(404, "未知源")
            if body.expected_version is not None and body.expected_version != src["version"]:
                raise LevelContention(body.source,
                                      f"值班席版本 {body.expected_version} ≠ 当前 {src['version']}",
                                      src["level"], src["output_mode"], src["version"])
            lv = level_of(lag, src["sla_minutes"])
            # 违约源仅补齐可解冻：新到达可推进未冻结源，但不解除冻结
            if src["output_mode"] == "冻结" and MODE_OF[lv] != "冻结":
                target = "违约"
            else:
                target = lv
            cur = c.execute(
                "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,current_level,current_output_mode,version,updated_at) VALUES (?,?,?,?,?,?,?,1,?)",
                (body.source, body.source_clock, body.ingest_clock, lag, lv, target, mode_of(target),
                 datetime.now().isoformat(timespec="seconds")),
            ).lastrowid
            mode = _apply_joint(c, body.source, target, f"到达登记 lag={lag} 分")
            return {"arrival_id": cur, "lag_minutes": lag, "level": target, "output_mode": mode}
    except LevelContention as e:
        _record_contention(e)
        raise HTTPException(409, {"code": "等级分裂争用", "current_version": e.current_version})

class LevelIn(BaseModel):
    level: str
    arrival_id: int | None = None
    expected_version: int | None = None

@app.post("/api/levels/{source}")
def set_level(source: str, body: LevelIn, authorization: str | None = Header(default=None)):
    """策略席：直接改等级。与值班席共用分裂锁，后交只记争用。"""
    require_user(authorization)
    if body.level not in MODE_OF:
        raise HTTPException(422, "未知等级")
    try:
        with write_tx() as c:
            src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
            if not src: raise HTTPException(404, "未知源")
            if body.expected_version is not None and body.expected_version != src["version"]:
                raise LevelContention(source,
                                      f"策略席版本 {body.expected_version} ≠ 当前 {src['version']}",
                                      src["level"], src["output_mode"], src["version"])
            mode = _apply_joint(c, source, body.level, "策略席改级")
            return {"source": source, "level": body.level, "output_mode": mode}
    except LevelContention as e:
        _record_contention(e)
        raise HTTPException(409, {"code": "等级分裂争用", "current_version": e.current_version})

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    """补齐恢复：等级牌回正常、输出牌回可写，同笔连坐。"""
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with write_tx() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        c.execute("UPDATE sources SET level='正常', output_mode='可写', version=version+1 WHERE name=?", (source,))
        c.execute("UPDATE arrivals SET current_level='正常', current_output_mode='可写', version=version+1, updated_at=? WHERE source=?", (now, source))
        c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                  (source, "成功", "可写", "补齐后恢复可写", now))
        log_audit(c, "补齐恢复", source, "补齐后恢复可写", "正常", "可写")
        return {"ok": True, "output_mode": "可写"}

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

@app.get("/api/audit")
def audit_list(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()]}

@app.get("/api/overview")
def overview(authorization: str | None = Header(default=None)):
    """策略附页 + 水位读数，与目录/卡片共用同一快照。"""
    require_user(authorization)
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]
    water = {
        "sources": len(rows),
        "writable": sum(1 for s in rows if s["output_mode"] == "可写"),
        "degraded": sum(1 for s in rows if s["output_mode"] == "降级只读"),
        "frozen": sum(1 for s in rows if s["output_mode"] == "冻结"),
        "normal": sum(1 for s in rows if s["level"] == "正常"),
        "alert": sum(1 for s in rows if s["level"] == "告警"),
        "breach": sum(1 for s in rows if s["level"] == "违约"),
    }
    grading = [{"name": s["name"], "sla_minutes": s["sla_minutes"], "level": s["level"],
                "output_mode": s["output_mode"], "version": s["version"]} for s in rows]
    homogeneous = all(s["output_mode"] == MODE_OF[s["level"]] for s in rows)
    return {"grading": grading, "water": water, "homogeneous": homogeneous}

@app.post("/api/output")
def write_output(body: dict, authorization: str | None = Header(default=None)):
    """输出写：违约冻结 / 告警只读 一律 423，杜绝“徽标只读、目录仍可写”。"""
    require_user(authorization)
    source = body.get("source")
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
    if not src: raise HTTPException(404, "未知源")
    if src["output_mode"] != "可写":
        raise HTTPException(423, f"{source} 当前为{src['output_mode']}，禁止输出写")
    return {"ok": True, "source": source}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
