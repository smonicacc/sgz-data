# sgz-data

个人向的小型数据观测仓库。围绕"尚公主（sgz）/ 昀牵孟绕（yqmr）/ 扶摇直尚（fyzs）"三个核心话题，以及"孟子义李昀锐（mzylyr）/ 李昀锐孟子义（lyrmzy）"两个双人组合话题，从腾讯视频、抖音、小红书三个平台自动按周期抓真实阅读量 / 粉丝 / 帖子互动数据，落盘为 JSON 后推到 Grafana Cloud。

每条记录按平台 `*_topic.json` / `*_user.json` 累积，单次 run 写一条 record（一个 `ts` + 多个指标字段），与 `process.py` 的分层归档对齐。

---

## 1. 数据文件

| 文件 | 内容 | 每条 record 结构 |
|------|------|----------------|
| `data.json` / `data-raw.json` | 腾讯视频《尚公主》单点 `attent_num`（`hot_num` 仅入 `data-raw.json`） | `{ts, value}`；`data-raw.json` 开启 `COLLECT_HOT_NUM` 后额外带 `hot_num`（经 `process.py` 分层归档） |
| `douyin.json` / `douyin-raw.json` | 抖音话题 + 用户粉丝 | `{ts, sgz_follower_count, sgz_total_favorited, <alias>_douyin_tag_view_count, <alias>_douyin_tag_user_count}`；`douyin-raw.json` 全量、`douyin.json` 经 `downsample_douyin.py` BJ-时分桶降采样 |
| `xhs_topic.json` | 小红书 5 个话题阅读量 | `{ts, sgz_tag_view_num, yqmr_tag_view_num, fyzs_tag_view_num, mzylyr_tag_view_num, lyrmzy_tag_view_num}` |
| `data/user_posts.json` | 抖音创作者最近帖子快照 | `{fetched_at, aweme_list:[{aweme_id, statistics:{...}}]}` |
| `data/user_posts_history.json` | 抖音创作者帖子历史 | `{aweme_id: [{ts, digg_count, comment_count, share_count}, ...]}` |
| `delta.md` | 北京日期 vs 增量的人工表格 | — |

每条记录按时间累积，append-only；任何脚本出错都不会回退既有数据。

---

## 2. 抓取脚本

| 脚本 | 用途 |
|------|------|
| [`fetch_douyin.py`](fetch_douyin.py) | 抓取一个抖音话题的 `viewCount` / `userCount`（CLI 接收 `HID` + `TAG_NAME` 环境变量） |
| [`fetch_user_posts.py`](fetch_user_posts.py) | 抓取抖音创作者最近帖子快照 + 写入 history |
| [`fetch_xhs_topic.py`](fetch_xhs_topic.py) | 抓取小红书 5 个话题 `view_num`（TOPICS 列表写死在脚本里，详见 [xiaohongshu.md](xiaohongshu.md)） |
| [`process.py`](process.py) | 把 `data.json` 的单点 `value` 按时间做分层归档（`data-raw.json` = 原始，`data.json` = 降采样后；env `RECORD_HOT_NUM` 非空时额外写入 `hot_num` 字段 — **仅入 `data-raw.json`，不进 `data.json`**） |
| [`downsample_douyin.py`](downsample_douyin.py) | 把 `douyin.json` 的多字段记录按 BJ 时间分桶降采样：`<=3d` 全保留 / `3d<age<=7d` 按 4 小时桶（00/04/08/12/16/20）/ `>7d` 按天桶（BJ 0:00）；`douyin-raw.json` 全量、`douyin.json` 降采样后 |

---

## 3. GitHub Actions 调度

只有一个工作流：`.github/workflows/collect_data.yaml`（`workflow_dispatch` 触发）。

> 该工作流没有顶层 `schedule:` 触发器，靠外部（如 GitHub UI 手动 / webhook / 第三方 cron）按需 dispatch。每次运行内部自判断"是否 due"，到点了才真正发请求。

手动触发参数：

手动触发参数：

| 输入 | 默认 | 说明 |
|------|------|------|
| `VID` | `l4101w6a4vt` | 腾讯视频 vid |
| `CID` | `mzc002001w361jz` | 腾讯频道 cid |
| `DOUYIN_INTERVAL_HOURS` | `4` | douyin 采集间隔（小时）。`0` = 永远 due |
| `FORCE` | `false` | 跳过 due 检查，强制跑 douyin + xhs |
| `COLLECT_HOT_NUM` | `false` | 在同一次 QQ 视频请求里顺带抓 `hot_num`，仅写进 `data-raw.json`（`data.json` 不带）；不影响 `attent_num` 主流 |
| `DRY_RUN` | `false` | 只采集、不 commit/push |

### 3.1 双条件 due 判断（`douyin_check` step）

判断"是否 due"时满足以下**任一**条件即触发：

1. **距上次 fetch 已到 `DOUYIN_INTERVAL_HOURS`**（5 分钟容差）
   读 `douyin.json` 取最新 `ts`，`age = now - latest_ts`，当 `age ≥ interval - 5min` 即 due。文件缺失 / 不可读 / 不是 list 也直接走 due（异常路径）。
2. **墙钟调度点已过（容差窗内）**
   以 unix epoch 为锚，按 `DOUYIN_INTERVAL_HOURS` 等分调度点（4h → `00:00 / 04:00 / 08:00 ...` UTC）。这一条看的是"刚过上一个调度点多久"——`seconds_since_boundary ≤ 5min` 即 due。这一条**与 `douyin.json` 无关**——`FORCE=true` 触发后即使刚 fetch 过，墙钟到了下一个调度点仍然会再触发一次，避免节奏被手动操作带偏。

> 容差方向专门为**右漂**留缓冲：GitHub Actions cron / webhook 实际执行一般比整点晚 1-2 分钟，5min 容差就吃下这种 delay；提前 5min（早于调度点）不算 due，那边由 cond1 的浮动容差管。

`reason` 输出会标明实际是哪一条 / 两条同时命中，例如 `cond2: next scheduled boundary in 180s (≤ 300s tolerance)`。

### 3.2 步骤总览

`collect_data.yaml` 的执行顺序：

1. `douyin_check` — 双条件 due 判断，输出 `due` / `reason`
2. `Build request body` / `Call target API` / `Parse attent_num + optional hot_num` — 腾讯视频；`hot_num` 仅在 `COLLECT_HOT_NUM=true` 时解析，缺字段时 warn 不 fail
3. `Push single point to Grafana Cloud Prometheus`（`qq-video`）
4. `Run Python downsample logic`（`process.py`）
5. `Fetch douyin user profile`（仅 `FORCE || due`）
6. `Run fetch_douyin.py for each hashtag`（仅 `FORCE || due`，5 个话题循环，列表在 `collect_data.yaml` 的 `PAIRS` 数组里维护）
7. `Append to douyin.json` — 在 inline Python 里构建单条 record（ts + sgz_follower/sgz_total_favorited + 各 alias 的 view/user_count），写到 `new_douyin_record.json`，再交给 [`downsample_douyin.py`](downsample_douyin.py) 处理：`douyin-raw.json` append-only 全量，`douyin.json` 按 BJ 时间分桶降采样（仅 `FORCE || due`）
8. `Push douyin user profile to Grafana Cloud Prometheus`（`douyin-user`，仅 `FORCE || due`）
9. `Fetch xiaohongshu topic view_num and append to xhs_topic.json`（仅 `FORCE || due`）
10. `Push xiaohongshu topic view to Grafana Cloud Prometheus`（`xhs-topic`，仅 `FORCE || due`）
11. `Commit and push updated data`（`DRY_RUN != 'true'`）

`git add` 列表：`data.json data-raw.json douyin.json douyin-raw.json xhs_topic.json`。

---

## 4. Grafana 推送（`push_to_grafana.py`）

依赖：`prometheus-remote-writer`。需要环境变量 `GRAFANA_REMOTE_WRITE_URL` / `GRAFANA_USER` / `GRAFANA_TOKEN`（GitHub Actions Secret）。

| 子命令 | 输入 | 输出 metric |
|--------|------|------------|
| `qq-video --val N [--hot-num H]` | 单点；可选 `--hot-num` 顺带推热度 | `qq_video_subscribe`、可选 `qq_video_hot_num` |
| `qq-video --records-file data.json` | 多点回填（自动按 record 字段映射：含 `hot_num` 的 record 同时推两条 metric） | `qq_video_subscribe` + `qq_video_hot_num`（仅当 record 里出现 `hot_num`） |
| `douyin-user --record-file douyin.json [--last]` | 单/多点 | `douyin_user_follower_count{account=sgz}`、`douyin_user_total_favorited{account=sgz}`、`douyin_tag_view_count{tag=…}`、`douyin_tag_user_count{tag=…}` |
| `douyin-posts --snapshot-file data/user_posts.json` | 单点（`fetched_at` 时间戳） | `douyin_user_post_<stem>{aweme_id=…}` |
| `douyin-posts --history-file data/user_posts_history.json` | 多点 | `douyin_user_post_<stem>{aweme_id=…}` |
| `xhs-topic --record-file xhs_topic.json [--last]` | 单/多点 | `xhs_topic_view_count{tag=sgz\|yqmr\|fyzs\|mzylyr\|lyrmzy}` |

全局 flag：`--dry-run` / `--print-only`（只打 JSON 不发请求）；`--max-age-days N`（回填过老样本时跳过，`err-mimir-sample-timestamp-too-old` 修复用）。

metric 前缀遵循 `平台域_实体_属性` 的小写下划线风格；`douyin_tag_*` 与 `xhs_topic_view_count` 共享 `tag` 标签，dashboard 里可并列展示同一话题的跨平台对比。

---

## 5. Secrets（仓库 → Settings → Secrets）

| Name | 用途 |
|------|------|
| `GRAFANA_REMOTE_WRITE_URL` | Grafana Cloud Remote Write endpoint（含 `/api/prom/push`） |
| `GRAFANA_USER` | Grafana Cloud Instance ID |
| `GRAFANA_TOKEN` | Grafana Cloud API Token |
| `DOUYIN_PROFILE_URL` | 抖音创作者 profile JSON endpoint |
| `DOUYIN_PROFILE_COOKIE` | 抖音创作者 profile 完整 Cookie |
| `XHS_COOKIE` | 小红书创作者端完整 Cookie（`a1 / webId / webBuild / web_session / ...`，详见 [xiaohongshu.md §1](xiaohongshu.md#1-需要手动获取的信息)） |

---

## 6. 本地开发

```bash
# qq-video 一次性单点推送
python3 push_to_grafana.py qq-video --val 123456

# 推 xhs 单点（dry-run 看 JSON）
python3 push_to_grafana.py xhs-topic --record-file xhs_topic.json --last --dry-run

# 跑一遍 xhs 抓取
XHS_COOKIE="a1=...;web_session=...;webId=...;webBuild=...;..." \
  python3 fetch_xhs_topic.py

# macOS 自带 Python 报 CERTIFICATE_VERIFY_FAILED 时
XHS_COOKIE="..." XHS_SSL_VERIFY=0 python3 fetch_xhs_topic.py
```

---

## 7. 相关文档

- [xiaohongshu.md](xiaohongshu.md) — 小红书抓取流程、Cookie 获取、本地调试、字段映射、Grafana 推送细节