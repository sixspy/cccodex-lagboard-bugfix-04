# 分裂切断簿（等级牌 × 输出态）

> 本簿为分裂切断的唯一口径。写定即钉死，**分裂当日禁止改口**；后交偏离本簿即驳回，并留 `分裂偏了` 痕迹。

## 一、切断对象

缺陷：违约判定后，**源目录仍可写**，而徽标（等级牌）已变只读；等级牌与输出态各写各的，形成等级分裂。
本仓（`06-lagboard`，分裂仓区）只切断这一处，**不新开分裂台**，沿用时限台既有的违约分级菜单：

| 等级 | 输出态 |
|------|--------|
| 正常 | 可写 |
| 告警 | 降级只读 |
| 违约 | 冻结 |

## 二、选定路径（钉死）

- **路径：甲** —— 等级牌（`sources.level`）与目录输出牌（`sources.output_mode`）**同笔事务连坐改写**，任一侧（含审计落库）出错则**两侧同撤**。
- 乙路径（草稿核对后原子晋升）**不采用**。甲乙写法彼此不得混用；本仓代码、测试与页面均只认甲。
- 钉死值：`split_meta.cut_path = 甲`，`pinned_on = 2026-11-19`。任何改口尝试在 `init_db` 校验时报 `分裂偏了`，等级变更接口一律驳回。

## 三、分裂锁

- 值班席（到达登记）与策略席（`POST /api/levels/{source}`）同时改同一到达的等级时，须占**分裂锁**：SQLite `BEGIN IMMEDIATE` 写锁 + `expected_version` 乐观版本。
- 后交者不落任何半截写，只记一条审计 `等级分裂争用`（附当前态与后交态），并返回 `409`。
- 缺该争用留痕即判分裂未过。

## 四、五处同态（验收面）

1. 源目录徽标（`/api/sources`：`level` / `output_mode`）
2. 到达卡片（`/api/arrivals`：`current_level` / `current_output_mode`）
3. 策略附页（`/api/overview.grading`）
4. 水位读数（`/api/overview.water`）
5. 审计流水（`/api/audit`，每源最后一条 `state_level` / `state_mode`）

`/api/overview.homogeneous` 必须为 `true`。只改徽标配色不改变上述任何一处语义，判分裂未过。

## 五、口令与库位

- **口令沿用本仓分裂侧**：复用本仓既有 `admin / 123456` 登录签发的会话令牌（`Authorization: Bearer …`），不另立口令。
- **库文件放在分裂仓区**：`data/app.db`（本仓 `data/` 区内），不引外置库。
- 台账文件：`split/LEDGER.md`（本簿）；钉死值与库同仓。

## 六、执行顺序（留痕）

1. 先复现：违约后目录仍可写 → 红。
2. 落**连坐失败测** → 确认红。
3. 落**双路抢级失败测**（含争用留痕）→ 确认红。
4. 双红后才动分裂落库（甲路径）。
5. 全绿后核五处同态。

## 七、落地记录（2026-11-19）

- 红态复现：`tests/test_split_cut.py` 先写后跑，4 项失败（`KeyError: 'level'`、`no such table: audit`、`KeyError: 'arrival_id'`）。
- 甲路径落库：`backend/app.py` `write_tx()`（`BEGIN IMMEDIATE`）+ `_apply_joint()`，等级牌/输出牌/到达卡片/审计同笔；异常整体 `ROLLBACK`。
- 分裂锁：`LevelContention` → 独立事务补记 `等级分裂争用` → 409。
- 改口拦截：`init_db` 检出 `cut_path != 甲` → 独立事务落 `split_divergence` → `RuntimeError` 拒绝启动。
- 存量迁移：`ALTER TABLE` 补列后按输出态反推等级、到达卡片与源目录对齐；实测旧库迁移零分裂。
- 绿态：`pytest` 6 passed；注入旧写法（两笔分写）后 `test_dual_path_level_conflict_records_contention` 复红，恢复后全绿。
- 五处同态：TestClient 与真实 `uvicorn`（127.0.0.1:10829）双口径核验，`homogeneous=true`，到达卡片零错配。
