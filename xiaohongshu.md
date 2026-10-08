# 小红书话题 view_num 抓取

`fetch_xhs_topic.py` + `.github/workflows/collect_data.yaml`（与 douyin 合流在同一流水线）用于追踪小红书创作者端的话题真实阅读量。它绕过 PC Web 端 SSR 注入的 "999万+" 封顶字符串，直接从 API 响应里的 `view_num` 字段取出原始整数。

签名基于 [`xhshow`](https://pypi.org/project/xhshow/)（独立 PyPI 包，专做小红书 XYS_ + x-s-common 签名），不再手工拼 MD5+AES。

数据累加文件是 `xhs_topic.json`，每次跑成功就 append 一条记录。脚本内一次跑 3 次 search：

| keyword   | topic_id                     | JSON key            |
|-----------|------------------------------|---------------------|
| 尚公主     | `5fb167d700000000010072c4`   | `sgz_tag_view_num`  |
| 昀牵孟绕   | `660796630000000002024e5a`   | `yqmr_tag_view_num` |
| 扶摇直尚   | `67d0f2b3000000000401ed01`   | `fyzs_tag_view_num` |

3 个 `view_num` 写在同一条记录里：`{ts, sgz_tag_view_num, yqmr_tag_view_num, fyzs_tag_view_num}`。任一 topic 校验失败（topic_id 不匹配 / view_num 无效 / 网络错）就**整次运行 abort**，不写入部分数据。

---

## 1. 需要手动获取的信息

唯一凭据是一段 **Cookie 字符串**。创作者端 galaxy 网关的签名 (`x-s-common`) 会用**所有** cookie 字段，所以 DevTools → Network 里能看到多少 cookie 都得带上，不能只取 `a1` 和 `web_session`。

最少需要这些字段（其余也能增加签名稳定性，全部带上最稳）：

| 字段 | 必需性 | 作用 |
|------|--------|------|
| `a1` | 必须 | 设备指纹种子 |
| `web_session` | 必须 | 登录态 |
| `webId` | 推荐 | 浏览器会话标识 |
| `webBuild` | 推荐 | 客户端版本号（`x-s-common` 模板里要用） |
| `xsecappid=ugc` | 推荐 | 标识来源 app |
| `loadts` | 推荐 | 首次加载时间戳 |

### 获取方式 A：浏览器 DevTools（最稳）

1. Chrome 隐身窗口打开 <https://creator.xiaohongshu.com> 并扫码登录
2. DevTools → Network 面板，过滤 `Fetch/XHR`
3. 触发一次搜索（发布页 → 添加话题 → 输入"尚公主"任意字符）
4. 在 Network 列表里点开 `search/topics` 那条请求
5. 右键 → **Copy → Copy as cURL (bash)**
6. 把复制内容里 `Cookie:` 那一长串（`a1=...; web_session=...; ...`）单独拎出来，就是 `XHS_COOKIE` 的值

### 获取方式 B：DevTools → Application → Cookies

1. DevTools → Application → Cookies → `https://creator.xiaohongshu.com`
2. 全选所有行，右键 → **Copy all**（macOS Chrome 没有 "Copy all"，逐条复制或走方式 A）

> **有效期**：`a1` 默认 1 个月左右，`web_session` 几小时到几天。任一过期都会让接口返回空/401，需要重新走上面任一方式。

---

## 2. GitHub 仓库配置

### 2.1 写入 Secret

仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：

- **Name**：`XHS_COOKIE`
- **Value**：上面获取的整段 cookie 字符串

### 2.2 触发工作流

首次建议手动触发一次，确认链路通：

仓库 → **Actions** → **Collect video.qq + douyin + xiaohongshu data** → **Run workflow**

正常情况下：

- stdout 单行 JSON：`{"ts": ..., "sgz_tag_view_num": N, "yqmr_tag_view_num": N, "fyzs_tag_view_num": N}`
- 仓库根目录的 `xhs_topic.json`（不存在则创建）追加一条对应记录

之后会跟 douyin 同步：受 `DOUYIN_INTERVAL_HOURS`（默认 4 小时）+ 5 分钟容差控制，由 `douyin_check` 步统一判断 due 才真正发请求——和 douyin 的间隔完全一致。

### 2.3 topic_id 校验

脚本对每个 keyword 都会比对接口返回的 `topic_info_dto.id` 是否等于脚本里 TOPICS 写死的规范 ID（见表格）。**任一不匹配 → 整次运行 abort、退出码 1、workflow 失败**——这是为了确保我们记录的是想要的精确话题，而不是被搜索引擎联想到的"尚公主开机"、"尚公主路透"、"电视剧尚公主"等几十个长尾词。

---

## 3. 本地运行

### 3.1 准备环境

```bash
# Python 3.10+ 必需（xhshow 要求）
python3 -m pip install "xhshow>=0.1.9"
```

macOS 自带 Python 若报 `CERTIFICATE_VERIFY_FAILED`：

- 用 pyenv 装的 Python（带 `--with-ensurepip`）
- 或 `pip install certifi` 后 `export SSL_CERT_FILE=$(python3 -m certifi)`
- 或简单跑时设 `XHS_SSL_VERIFY=0`

### 3.2 直接跑

```bash
XHS_COOKIE="a1=...;web_session=...;webId=...;webBuild=...;..." python3 fetch_xhs_topic.py
```

期望 stderr（每个 topic 一行进度）：

```
'尚公主' (5fb167d700000000010072c4) → sgz_tag_view_num=461448567
'昀牵孟绕' (660796630000000002024e5a) → yqmr_tag_view_num=...
'扶摇直尚' (67d0f2b3000000000401ed01) → fyzs_tag_view_num=...
```

期望 stdout（一行 JSON）：

```json
{"ts": 1762492800, "sgz_tag_view_num": 461448567, "yqmr_tag_view_num": ..., "fyzs_tag_view_num": ...}
```

退出码：

| 退出码 | 含义 |
|--------|------|
| `0` | 成功（已 append 一条记录到 `xhs_topic.json`，3 个 topic 全 OK） |
| `1` | 业务错误（任一 topic：响应空 / view_num 无效 / topic_id 不匹配 / 名字不匹配 / 网络错） |
| `2` | 环境错误（cookie 未设） |

跑两次的话，`xhs_topic.json` 会变成：

```json
[
  {"ts": 1762492800, "sgz_tag_view_num": 461448567, "yqmr_tag_view_num": ..., "fyzs_tag_view_num": ...},
  {"ts": 1762507200, "sgz_tag_view_num": 461512893, "yqmr_tag_view_num": ..., "fyzs_tag_view_num": ...}
]
```

### 3.3 调试模式

- **macOS 上关掉 TLS 校验**（仅本地）：
  ```bash
  XHS_COOKIE="..." XHS_SSL_VERIFY=0 python3 fetch_xhs_topic.py
  ```
  不要把 `XHS_SSL_VERIFY=0` 写到 GitHub Actions 里。
- **改 User-Agent / 超时**：
  ```bash
  XHS_COOKIE="..." UA="Mozilla/..." TIMEOUT=60 python3 fetch_xhs_topic.py
  ```
- **只生成签名不真请求**（验证签名计算是否走通）：
  ```bash
  python3 - <<'PY'
  import os
  from xhshow import Xhshow
  cookies = dict(p.strip().split("=", 1)
                 for p in os.environ["XHS_COOKIE"].split(";") if "=" in p)
  c = Xhshow()
  sig = c.sign_headers_post(
      uri="https://creator.xiaohongshu.com/api/galaxy/v2/creator/servicegw/v2/search/topics",
      cookies=cookies,
      payload={"keyword": "尚公主", "topic_round_start_time": 0, "content": "aaa"},
  )
  import json; print(json.dumps(sig, indent=2))
  PY
  ```
  应该看到 `x-s`、`x-s-common`、`x-t`、`x-b3-traceid`、`x-xray-traceid`、`x-mns`、`xy-direction` 7 个 header 都填了非空值。

---

## 4. 常见失败排查

| stderr | 原因 | 处理 |
|--------|------|------|
| `ERROR: XHS_COOKIE not set` | 环境变量未注入 | 检查 export / Actions Secret |
| `ERROR: XHS_COOKIE contains no key=value pairs` | 字符串解析失败（不是合法 cookie） | 检查 Secret 内容里是不是有非法字符 |
| `FETCH_ERROR (尚公主): HTTP Error 401 / 403` | cookie 过期或缺失关键字段 | 重新走第 1 节，建议用方式 A（Copy as cURL） |
| `FETCH_ERROR (尚公主): HTTP Error 404` | endpoint 已失效（XHS 改路径了） | 浏览器抓新的请求 URL 贴进 `fetch_xhs_topic.py` 的 `ENDPOINT` |
| `NOT_FOUND: response=...` | 接口返回了但响应里没 topic 数组 | 看 stderr 末尾 500 字符的响应 JSON，可能是鉴权失败返回空 `{success: false}` |
| `INVALID_VIEW_NUM: 'null'` | view_num 不是数字 | 看完整响应，可能 endpoint 拿错导致拿到别的列表 |
| `NAME_MISMATCH: got 'xxx', expected '尚公主'` | 搜索接口没精确匹配，脚本退而求其次 | 检查 keyword 写法（中文/标点） |
| `TOPIC_ID_MISMATCH: got 'xxx', expected '5fb167d700000000010072c4'` | 匹配到的 topic 不是规范 尚公主（很可能是长尾词如"尚公主开机"等） | 检查 keyword 写法 |
| `TOPIC_ID_MISMATCH: got 'xxx', expected '660796630000000002024e5a'` | 匹配到的 topic 不是规范 昀牵孟绕 | 同上 |
| `TOPIC_ID_MISMATCH: got 'xxx', expected '67d0f2b3000000000401ed01'` | 匹配到的 topic 不是规范 扶摇直尚 | 同上 |

---

## 5. 文件清单

| 路径 | 说明 |
|------|------|
| `fetch_xhs_topic.py` | 主脚本（stdlib + `xhshow`），自己 append 到 `xhs_topic.json` |
| `.github/workflows/collect_data.yaml` | GitHub Actions 流水线（XHS 步骤挂在 douyin 后面，共享 `douyin_check` 的 due 判断和 `DOUYIN_INTERVAL_HOURS`） |
| `xhs_topic.json` | 累积数据，JSON list，每条 `{"ts": ..., "sgz_tag_view_num": ..., "yqmr_tag_view_num": ..., "fyzs_tag_view_num": ...}` |

---

## 6. 接入 Grafana

小红书数据通过 `push_to_grafana.py xhs-topic` 子命令推到 Grafana Cloud Prometheus。WCF 已经自带，无需手动配置：

```bash
# 单点推送（workflow 用法）：只推 xhs_topic.json 数组的最后一条记录
python3 push_to_grafana.py xhs-topic --record-file xhs_topic.json --last

# 全量回填：推整个 xhs_topic.json
python3 push_to_grafana.py xhs-topic --record-file xhs_topic.json

# 回填过老样本时（err-mimir-sample-timestamp-too-old）配合 --max-age-days
python3 push_to_grafana.py xhs-topic --record-file xhs_topic.json --max-age-days 30
```

字段 → metric 映射（在 `push_to_grafana.py` 的 `XHS_TOPIC_SUFFIX_MAP` 里）：

| JSON 字段 | Prometheus metric | 标签 |
|----------|------------------|------|
| `sgz_tag_view_num`  | `xhs_topic_view_count` | `tag=sgz`  |
| `yqmr_tag_view_num` | `xhs_topic_view_count` | `tag=yqmr` |
| `fyzs_tag_view_num` | `xhs_topic_view_count` | `tag=fyzs` |

全部 metric 还会自动带上 `job=xhs_collector` 默认标签。标签 `tag` 与 `douyin_tag_view_count` / `douyin_tag_user_count` 对齐，dashboard 里可以并列比对同主题的跨平台数据。

Workflow 上对应步骤（`collect_data.yaml` 的 `Push xiaohongshu topic view to Grafana Cloud Prometheus`）在 XHS 抓取之后立即执行，受同一个 `douyin_check.outputs.due` 控制；`FORCE=true` 也会强制推送。