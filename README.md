# 悟真鉴引

面向学术写作的引文真实性核验与证据链分析工具。

在线演示（免登录静态单页）：<https://z108jw.github.io/wuzhen-jianyin/>

悟真鉴引从论文中提取参考文献与正文引用，通过公开学术数据源核对题录、识别字段冲突、检查撤稿状态，并尽可能把正文主张与摘要证据对应起来。系统强调证据可追溯，不把“未检索到”直接写成“文献不存在”，也不输出未经校准的“虚假置信度”。

## 目录

- [核心能力](#核心能力)
- [系统组成](#系统组成)
- [快速开始](#快速开始)
- [使用流程](#使用流程)
- [结果怎么读](#结果怎么读)
- [配置项](#配置项)
- [数据源与核验逻辑](#数据源与核验逻辑)
- [API](#api)
- [部署](#部署)
- [测试](#测试)
- [目录结构](#目录结构)
- [隐私与安全](#隐私与安全)
- [能力边界](#能力边界)

## 核心能力

- **多格式解析**：支持 PDF、DOCX、TXT、Markdown。PDF 需要带文字层；扫描版会明确提示先做 OCR。
- **中英文参考文献识别**：支持 `参考文献`、`References`、`Bibliography`、`Works Cited` 等分节标题，支持编号列表和无编号列表。
- **正文引用映射**：识别 `[1]`、`[1-3]`、`(1)`、`（1）`、`作者（年份）` 等常见形式，并检查正文引用缺失、文献表未引用和编号中断。
- **多源题录核验**：有 DOI 时查询 Crossref、DataCite、OpenAlex；无 DOI 时使用 Crossref 书目检索和 OpenAlex 题名检索。
- **撤稿检测**：读取 Crossref `updated-by` 的撤稿关系与 OpenAlex `is_retracted` 标记，把已撤稿文献列为高风险问题。
- **证据链判断**：只有取得摘要或开放全文证据时才判断正文支持度；没有证据时保持“证据不足”。
- **结构化报告**：结论卡、风险清单、逐条文献卡、来源证据矩阵、正文—证据链、质量画像和修订对照。
- **多格式导出**：Word、Excel、PDF、JSON。
- **分享与协作**：生成带有效期的脱敏报告链接和二维码；也可以生成完整应用入口。
- **两种运行形态**：FastAPI 完整版负责 PDF/DOCX、撤稿检测和导出；`docs/index.html` 是免登录静态单页版，可直接托管到 GitHub Pages。

## 系统组成

```text
浏览器
  → FastAPI 任务接口
  → 文档解析（PDF / DOCX / TXT / Markdown）
  → 参考文献与正文引用解析
  → Crossref / DataCite / OpenAlex 题录核验
  → 撤稿关系检测
  → 摘要或开放证据提取
  → 规则辅助或可选大模型支持度判断
  → 报告、导出与脱敏分享
```

更详细的技术设计见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

### 环境要求

- Python 3.11 及以上（推荐 3.12）
- Windows、macOS 或 Linux
- 可访问 Crossref、DataCite、OpenAlex 的网络

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1
```

打开 <http://127.0.0.1:8000>。

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

### Docker

```bash
docker build -t wuzhen-jianyin .
docker run --rm -p 8000:8000 wuzhen-jianyin
```

如需大模型或 PostgreSQL，把配置写入 `.env` 后追加 `--env-file .env`。

### 静态单页版

`docs/index.html` 是自包含的单文件网页，不依赖后端，可以直接双击打开，也可以托管到 GitHub Pages。它只做参考文献列表检查、DOI 解析和 Crossref/OpenAlex 公开源核验。

完整版运行时，也可以通过 <http://127.0.0.1:8000/standalone> 打开同一页面。

## 使用流程

### 1. 准备输入

有两种模式：

**上传论文**

- 支持 `.pdf`、`.docx`、`.txt`、`.md`、`.markdown`。
- 文件必须包含一个独立的参考文献分节标题，例如：
  - `参考文献`
  - `References`
  - `Bibliography`
  - `Works Cited`
- 只解析该标题之后的内容作为文献表。
- PDF 必须是带文字层的版本；扫描版请先 OCR。
- 默认单个文件最大 20 MB，可通过 `MAX_FILE_MB` 调整。

**粘贴文献列表**

- 不需要“参考文献”标题。
- 只检查文献列表本身的题录、重复、编号和 DOI，不判断正文语义。
- 适合快速检查别人发来的一组参考文献，或从数据库导出的文献列表。

### 2. 开始核验

点击“开始核验”后，系统按以下阶段推进：

1. 解析论文文件
2. 提取参考文献
3. 映射正文引用
4. 核验题录元数据
5. 判断正文与证据关系
6. 生成结构化报告

进度通过 SSE 实时推送；长文献列表会在固定高度区域内滚动，不会把页面无限拉长。

### 3. 查看报告

报告包含六个区域：

| 区域 | 用途 |
|---|---|
| 总览 | 结论卡、核验分布、优先处理的问题 |
| 逐条文献 | 搜索、筛选、展开每条文献的题录、风险与建议 |
| 证据矩阵 | 查看每个数据源返回了什么、成功还是失败 |
| 引文证据链 | 正文原句 → 引用编号 → 目标文献 → 证据原文 → 判断理由 |
| 质量画像 | DOI 覆盖、证据覆盖、引用映射、数据源成功率和字段完整度 |
| 修订对照 | 原文和建议动作并排展示 |

### 4. 补充 DOI

在本机运行时，可以对单条文献补填 DOI。系统会重新查询公开来源，并更新该文献的题录、撤稿状态、风险级别和报告派生数据。

> 为避免公网访问者滥用外部接口，“补充 DOI”默认只对本机请求开放。部署到公网时按钮会自动隐藏。

### 5. 导出与分享

- **复制报告**：复制 Markdown 报告。
- **分享结果**：生成脱敏、只读、带有效期的报告链接和二维码。
- **分享应用**：生成完整应用入口；需要先建立公网隧道或部署到云端。
- **打印 / PDF**：浏览器打印样式已做适配。
- **导出文件**：Word、Excel、PDF、JSON。

## 结果怎么读

### 题录状态

| 状态 | 含义 |
|---|---|
| 已证实 | 题名、作者、年份等字段与公开来源一致，或达到多字段匹配阈值 |
| 可能匹配 | 找到候选记录，但部分字段未完全对齐 |
| 信息冲突 | DOI 指向其他记录，或标题、作者、年份等存在明确冲突 |
| 暂未检出 | 当前可用公开来源没有找到对应记录，不等于文献不存在 |
| 覆盖不足 | 数据源未覆盖该文献，常见于中文无 DOI 文献或网络失败 |

### 正文支持度

| 状态 | 含义 |
|---|---|
| 支持 | 正文主张与摘要证据存在较高词汇重合 |
| 部分支持 | 主题相关，但证据不足以完全支持结论 |
| 不支持 | 仅在大模型明确给出否定判断且证据可核验时出现 |
| 证据不足 | 没有摘要或开放全文，不能仅凭标题判断 |
| 未判断 | 粘贴模式没有正文，或尚未执行判断 |

### 撤稿状态

撤稿状态独立于题录状态：

- 文献可以同时是“已证实”和“已撤稿”。
- 已撤稿文献会被标记为高风险，并生成“已撤稿文献”问题项。
- 系统不会因为撤稿而把文献改判为“不存在”。

## 配置项

复制 `.env.example` 为 `.env`，按需填写。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 空 | OpenAI 兼容接口密钥；为空时使用规则辅助 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 聊天补全接口地址 |
| `LLM_MODEL` | `deepseek-chat` | 模型名称 |
| `APP_BASE_URL` | `http://127.0.0.1:8000` | 对外基础地址；反向代理或隧道场景可留空由请求头推断 |
| `MAX_FILE_MB` | `20` | 上传文件大小上限 |
| `SOURCE_TIMEOUT_SECONDS` | `10` | 单个外部数据源超时 |
| `JOB_TTL_SECONDS` | `7200` | 任务与分享链接有效期 |
| `DATABASE_URL` | 空 | PostgreSQL 连接串；为空时使用内存存储 |
| `ALLOW_SHARED_CNKI` | `false` | 是否允许公网请求使用本机知网授权；公网部署应保持 `false` |
| `CONTACT_EMAIL` | 空 | Crossref/OpenAlex polite pool 联系邮箱，建议填写 |

### 大模型增强

配置 `LLM_API_KEY` 后，系统会用模型归纳结论和辅助判断正文支持度。模型输出必须是 JSON，后端会校验状态值，并要求引用的证据原文真实存在于已取得的证据中；模型失败或未配置时自动退回规则辅助，不影响解析、核验、映射和导出。

### PostgreSQL

设置 `DATABASE_URL` 后，任务与分享报告会写入 PostgreSQL，服务重启后仍可恢复。未设置时使用进程内存，服务重启后任务丢失。

## 数据源与核验逻辑

### 有 DOI

同时查询：

- Crossref：`/works/{doi}`
- DataCite：`/dois/{doi}`
- OpenAlex：`/works/https://doi.org/{doi}`

对返回的候选记录按题名、作者、年份、期刊和 DOI 进行多字段匹配。

### 无 DOI

同时执行：

- Crossref 书目检索：题名 + 第一作者 + 年份
- OpenAlex 题名检索

如果多个候选，则按标题相似度、作者重合度、年份和期刊综合排序。

### 中文文献

中文无 DOI 文献通常需要机构授权数据库（如知网）才能获得完整题录。系统在检测到本机环境且配置了授权会话时，会尝试知网检索；公网访问时自动关闭该能力，并将无法覆盖的中文文献标记为“覆盖不足”。

### 撤稿检测

- Crossref `updated-by` 中出现 `type: retraction`；
- 或 OpenAlex 返回 `is_retracted: true`。

任一来源命中即标记为已撤稿，并在报告中生成高风险问题。

### 支持度判断

规则辅助使用正文主张与证据文本的词汇重合度，中文按字符二元组（bigram）处理。没有证据时不会输出“支持”或“不支持”，只输出“证据不足”。

## API

完整版提供以下接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查、模型状态、公开源模式 |
| `POST` | `/api/jobs` | 创建核验任务（multipart） |
| `GET` | `/api/jobs/{job_id}` | 查询任务状态 |
| `GET` | `/api/jobs/{job_id}/events` | SSE 进度流 |
| `GET` | `/api/reports/{job_id}` | 获取结构化报告 |
| `POST` | `/api/reports/{job_id}/supplement` | 补充 DOI（仅本机） |
| `GET` | `/api/reports/{job_id}/export/{type}` | 导出 `docx` / `xlsx` / `pdf` / `json` |
| `POST` | `/api/share` | 创建脱敏分享 |
| `GET` | `/api/share/{token}` | 获取分享报告 |
| `GET` | `/api/share/{token}/qr` | 分享二维码 |
| `GET` | `/api/app-share` | 获取完整应用入口 |

创建任务示例：

```bash
curl -X POST http://127.0.0.1:8000/api/jobs \
  -F "mode=file" \
  -F "file=@paper.pdf"
```

查询任务：

```bash
curl http://127.0.0.1:8000/api/jobs/<job_id>
```

导出报告：

```bash
curl -o report.docx http://127.0.0.1:8000/api/reports/<job_id>/export/docx
```

## 部署

### Docker

```bash
docker build -t wuzhen-jianyin .
docker run -d --name wuzhen-jianyin -p 8000:8000 --env-file .env wuzhen-jianyin
```

### Render

项目包含 `render.yaml`：

1. 将仓库推送到 GitHub。
2. 在 Render 创建 Blueprint，选择该仓库。
3. 配置 `DATABASE_URL`（可选，建议使用免费 PostgreSQL）。
4. 如需大模型增强，再配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`。

公网部署必须保持 `ALLOW_SHARED_CNKI=false`，不要把机构授权会话复制到公网服务器。

### GitHub Pages（静态单页）

仓库包含 `.github/workflows/pages.yml`，`docs/` 目录会通过 GitHub Actions 自动发布为静态站点：

1. 打开仓库 `Settings` → `Pages`。
2. `Build and deployment` 的 `Source` 选择 `GitHub Actions`。
3. 向 `main` 分支推送 `docs/` 变更后，工作流会自动部署。
4. 部署完成后访问 `https://<用户名>.github.io/<仓库名>/`。

`docs/index.html` 是自包含页面，不需要构建步骤。

### Cloudflare 临时隧道

没有服务器和域名时，可以运行：

```powershell
.\share.ps1
```

脚本会启动应用、下载或调用 `cloudflared`，并生成 `trycloudflare.com` 临时地址。该地址依赖运行脚本的电脑和进程，电脑关机或进程退出后失效。

## 测试

```bash
python -m pytest -q
```

当前测试覆盖：

- 文档分节与中英文标题识别
- 编号列表与无编号列表解析
- 全角引用标记与编号区间
- 重复文献、编号中断、正文引用缺失
- DOI 跨行拼接
- Crossref 撤稿关系识别
- 分享链接过期
- DOCX、XLSX、PDF、JSON 导出
- 粘贴模式不输出正文语义判断

## 目录结构

```text
app.py                      FastAPI 路由、任务与分享
backend/
  agent.py                  核验编排、规则判断与报告生成
  documents.py              PDF/DOCX/TXT/Markdown 解析
  references.py             参考文献、引用与本地完整性检查
  sources.py                Crossref/DataCite/OpenAlex 与匹配逻辑
  cnki.py                   可选机构授权知网桥接
  exports.py                DOCX/XLSX/PDF/JSON 导出
  store.py                  内存/PostgreSQL 任务与分享存储
static/                     完整版前端
docs/
  index.html                免登录静态单页版（GitHub Pages）
  architecture.md           技术设计说明
examples/                   可复现测试样例
scripts/                    示例生成与知网桥接脚本
tests/                      离线核心测试
```

## 隐私与安全

- 上传文件只在内存中处理，不写入业务磁盘。
- 任务和分享链接默认两小时后失效。
- 分享报告默认隐藏论文来源名称，不包含原文件。
- 公网访问自动关闭机构授权数据库能力，避免借用本机账号权限。
- 系统不保存知网账号密码，只复用本机浏览器授权会话。
- 如果启用大模型，正文主张和摘要证据会发送给所配置的模型服务，请根据数据敏感程度决定是否启用。
- 公开部署时建议增加访问控制、限流和 HTTPS 反向代理。

## 能力边界

- “暂未检出”只表示当前来源没有找到对应记录，不等于文献不存在。
- 撤稿检测依赖 Crossref 与 OpenAlex 的撤稿登记信息；未登记撤稿的文献仍可能存在其他学术争议。
- 没有摘要或开放全文时，不将题名相似度当作内容支持度。
- 中文无 DOI 文献在公网免登录模式下可能显示“覆盖不足”。
- 系统不用于认定学术不端，不代替导师、编辑或评审专家的判断。
- 系统不自动下载受版权保护的全文。
- 引用格式修订目前以建议形式呈现，不自动重排为 GB/T 7714 或 APA 格式。
