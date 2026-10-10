# AI 联合用药安全评估系统

规则引擎 + RAG + 大模型的 DDI（药物-药物相互作用）风险分级。

**核心设计**：规则引擎负责安全底线，RAG 负责精准检索，大模型负责解释与生成。
禁忌级组合由规则引擎硬拦截，**不经过大模型**。

前后端分离：后端是 Flask API，前端是 `web/` 下的静态页面（无需构建）。

---

## 快速开始

```bash
cd ddi
pip install -e ".[dev]"

cp config.example.yaml config.yaml   # 填 llm.api_key
python scripts/fetch_model.py        # 取 m3e 权重（390MB，不入库）
python scripts/load_seed.py          # 种子数据 → SQLite
python scripts/build_index.py        # 构建 FAISS + BM25 索引
python -m pytest -q                  # 跑测试
python scripts/run_api.py            # 启动服务，访问 http://127.0.0.1:5000
```

三个自检脚本，**排查问题时先跑这三个，不要在业务链路里猜**：

```bash
python scripts/check_config.py   # 配置读对了吗（含供应商切换预览、嵌入后端）
python scripts/check_data.py     # 数据能不能对外
python scripts/smoke_llm.py      # 大模型连得上吗
```

调一次接口：

```bash
curl -X POST http://127.0.0.1:5000/api/v1/assess \
  -H 'Content-Type: application/json' \
  --data-binary '{"drugs":["芬必得","华法林钠片"],"profile":{"age":68,"hepatic":"mild"}}'
```

> Git Bash 下 `-d '中文...'` 会因引号处理把 UTF-8 弄坏，用 `--data-binary` 或
> 写进文件再 `--data-binary @file`。

---

## 配置

**所有可调项、包括密钥，都放在 `config.yaml` 一个文件里。**
该文件已在 `.gitignore` 中，不会入库。模板见 `config.example.yaml`。

```yaml
llm:
  provider: minimax      # ★ 换供应商只改这一行
  api_key: "sk-..."      # ★ 唯一需要填密钥的地方
```

### 换大模型供应商

三家都提供 **Anthropic 兼容端点**，所以换供应商不用改任何代码：

| provider | 端点 | 默认模型 |
|---|---|---|
| `minimax` | `https://api.minimax.cn/anthropic` | `MiniMax-M3` |
| `deepseek` | `https://api.deepseek.com/anthropic` | `deepseek-v4-pro` |
| `qwen` | `https://dashscope.aliyuncs.com/apps/anthropic` | `qwen3.8-max` |
| `custom` | 自填 | 自填 |

```bash
python scripts/check_config.py --list           # 看预设
python scripts/check_config.py --try deepseek   # 预览切换后的实际端点，不改文件
```

**供应商名字拼错会直接报错，不会静默回退。** 悄悄连回原供应商会让人以为
配置生效了，排查时被误导很久。

`base_url` 结尾**不要带 `/v1`** —— SDK 会自己拼 `/v1/messages`。

部署时也可以用环境变量覆盖（优先级：环境变量 > `config.yaml` > 默认值）：

| 变量 | 对应配置 |
|---|---|
| `DDI_LLM_PROVIDER` | `llm.provider` |
| `DDI_LLM_API_KEY` | `llm.api_key` |
| `DDI_LLM_BASE_URL` | `llm.base_url` |
| `DDI_LLM_MODEL` | `llm.model` |
| `DDI_LLM_THINKING` | `llm.thinking` |
| `DDI_EMBED_BACKEND` | `embed.backend` |
| `DDI_EMBED_MODEL_PATH` | `embed.model_path` |
| `DDI_DB_PATH` | `database.path` |
| `DDI_API_PORT` | `api.port` |
| `DDI_API_DEBUG` | `api.debug` |
| `DDI_API_CORS_ORIGINS` | `api.cors_origins`（逗号分隔） |

旧版不带 `DDI_` 前缀的名字（`LLM_API_KEY` 等）仍然兼容。

### 管理后台

后台页面在 `/admin`（子路径部署则为 `/syc/admin`），提供六个标签页：

| 标签页 | 能力 | 接口 |
|---|---|---|
| 总览 | 规则状态/严重度分布、溯源覆盖、药品/成分/日志计数、LLM 与检索状态 | `GET /api/v1/admin/stats` |
| 规则库 | 筛选/搜索/新建/编辑/删除 + **勾选批量改状态、批量删除**（审核流 draft→reviewed→published 一次走完），改动即时热重载 | `GET/POST/PUT/DELETE /api/v1/admin/rules`、`POST /api/v1/admin/rules/batch` |
| 药品数据 | 搜索、**新建药品**（按名挂成分，库里没有的成分自动创建）、查看明细、匹配测试 | `GET/POST /api/v1/admin/drugs` |
| 智能导入 | **粘贴/上传文本 → 大模型解析成结构化草稿（DDI 规则 + 药品两类）→ 待确认列表 → 人工核对编辑 → 确认入库或丢弃**；确认时未知成分自动创建、规则溯源同（标题,年份）幂等复用、药品确认后归一化索引即时重载 | `POST /api/v1/admin/import/parse`、`GET/PUT/DELETE /api/v1/admin/import/drafts`、`POST /api/v1/admin/import/drafts/<id>/confirm` |
| 评估记录 | 查看每次评估的输入、风险分级、耗时、降级原因 | `GET /api/v1/admin/logs` |
| 配置 | 改供应商/模型/密钥等，LLM 部分热替换，其余提示需重启 | `GET/PUT /api/v1/admin/config` |

智能导入的边界：解析失败（模型未配置/输出不可解析）**一行不写**并返回 502；
草稿确认前不碰任何正式表；输入上限 8000 字，单次最多 20 条候选，
超长文本请拆分分批解析。

成分选择器的数据源是 `GET/POST /api/v1/admin/ingredients`（新建规则时也可直接快速创建成分）。

**启用**：在 `config.yaml` 填 `admin.token`（留空 = 后台整体 403 禁用）：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

登录后令牌存在浏览器 `sessionStorage`，请求带 `Authorization: Bearer <token>`。
密钥类字段（`llm.api_key`、`admin.token`）在 GET 配置时只回 `*_set: true`，**从不回显明文**。
写/删规则后自动 `reload_rules()`，新建药品后自动 `reload_drugs()`，均无需重启服务。
新建规则时成分对由服务端规范成 `ing_a_id < ing_b_id`，重复成分对返回 409。

隐私边界：评估日志只存年龄/性别/肝肾/孕期等白名单字段，**过敏史原文永不落库**。

---

## 数据流

```
用户输入药名 + 患者画像
      ↓
M1 归一化    别名/商品名/拼音/模糊匹配 → 药品 → 拆解为【成分】
      ↓
M2 规则引擎  两两配对查 DDI + 人群禁忌 → 分级
      ↓
      ├── 命中禁忌级 ──→ ★ 硬拦截，直接返回，不进 LLM
      ↓
M3 RAG       向量 + BM25 → RRF 融合 → 检索补充片段
      ↓
M4 生成      LLM 只能引用检索片段 → 输出 JSON
      ↓
      ★ 二次校验：再跑一遍规则引擎，等级不一致则整体回退规则答案
      ↓
输出：风险等级 + 机制 + 建议 + 原文依据（source.excerpt）
```

---

## 目录结构

```
ddi/
├── config.example.yaml     ★ 配置模板（入库）；config.yaml 是本地实际配置（不入库）
├── data/seed/              ★ 核心资产：手工维护的规则库（YAML）
│   ├── drugs.yaml          66 成分（含 CAS/ATC）/ 124 药品
│   │                       （57 条剂型扩充由 scripts/augment_drugs.py 生成）
│   ├── ddi_*.yaml          947 条 DDI 规则 = 182 条手工 + 765 条 DDInter 导入
│   │                       （ddi_ddinter.yaml 由 scripts/import_ddinter.py 生成）
│   └── population.yaml     69 条人群禁忌（年龄/肝肾/妊娠/过敏）
├── src/ddi/
│   ├── config.py           配置装配（默认值 → config.yaml → 环境变量）
│   ├── providers.py        大模型供应商预设表
│   ├── db/                 schema.sql / loader.py / session.py
│   ├── normalize/          M1
│   ├── rules/              M2
│   ├── rag/                M3（embedder / index / retriever）
│   ├── generate/           M4（llm_client / prompts / generator）
│   ├── api/                Flask
│   ├── eval/               指标
│   └── service.py          端到端编排
├── models/                 嵌入模型权重（.gitignore，部署时单独取）
├── web/                    ★ 前端（纯静态，无需构建）
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   └── config.js           前端唯一的配置项：后端 API 地址
├── scripts/                可执行入口
└── tests/                  217 项测试
```

---

## 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 规则挂在**成分**还是商品 | **成分** | 否则"芬必得"和"布洛芬"要各写一遍规则 |
| 规则引擎与 LLM 的先后 | **规则引擎在前，硬拦截** | 禁忌级结论不允许被模型改写 |
| LLM 的权限 | **只能引用检索片段** | 从机制上压制幻觉，而不是靠提示词祈祷 |
| 二次校验 | LLM 输出**再跑一遍规则引擎** | 等级不一致 → 整体回退规则答案 |
| 条目字段归属 | **可执行文本一律取自规则库** | `title`/`mechanism`/`consequence`/`suggestion`/`sources` 来自规则库；模型只写 `explanation`（通俗复述）与跨条目的 `summary`/`patient_note`。药师照着执行的内容不能是模型现场措辞 |
| 向量库 | **FAISS IndexFlatIP** | 数据量 <10 万，精确检索足够，无需调参 |
| 融合策略 | **RRF (k=60)** | 用排名而非分数融合，避开两路分数不可比的问题 |
| 供应商切换 | **预设表 + 单行配置** | 三家都兼容 Anthropic 协议，无需第二套客户端 |
| 供应商名拼错 | **报错退出** | 静默回退会让人以为配置生效了 |
| 前端技术栈 | **纯静态，无构建** | 与"Python 单栈"一致；部署 = 拷文件，不需要 Node 工具链 |
| 外部数据接入 | **映射式导入 + 保守定级** | DDInter 等外部库只取能映射到本库成分的 pair，且**最高定到 caution** —— 禁忌级（硬拦截）只允许人工核验后的结论 |

---

## ⚠️ 数据状态：基本全为 `draft`

**这批种子数据未经执业药师核验，不得用于任何真实用药决策。**

跑 `python scripts/check_data.py` 看当前实际状态。实测结果（2026-10）：

| 项 | 实测 | 说明 |
|---|---|---|
| 规则总数 | **947**（182 手工 + 765 DDInter 导入） | 严重度：禁忌 51 / 慎用 139 / 关注 757 |
| 规则状态 | 946 `draft` / 1 `published` | 核验流程基本未走 |
| **溯源覆盖** | **802 / 947（84.7%）** | 剩余 145 条手工规则无 `rule_source` |
| 成分 CAS | 66 / 66（来自 PubChem） | 客观字段，已全量补齐 |
| 药品数 | **124**（67 手工 + 57 剂型扩充） | 扩充条目未填规格/商品名/批准文号，待核验 |
| 药品批准文号 | 0 / 124 已填 | `approval_no` 基本为空 |

### 关于溯源覆盖：84.7% 的构成

- **手工 182 条**里有 37 条录入了说明书/指南摘录，其余 145 条没有 ——
  补齐需要**逐条对照纸质/官方电子说明书**，是数据整理工作，脚本绕不过。
- **DDInter 导入的 765 条**自带来源（`excerpt` 是原始 Level 标记）。
  注意 DDInter 的 CSV 只有成分对 + 等级，**没有机制/后果原文**，
  这些字段在规则详情里为空，待人工核验时补充。

前端「查看依据」只对有依据的规则显示，其余会明确标注
「本条规则暂未录入溯源依据」—— 不会假装有出处。

### 其他数据说明

1. **手工规则的 `excerpt` 是要义转述，不是逐字原文。**
   各规则文件头部的来源摘录为依据说明书/指南内容**整理的要义**，
   不是从纸质说明书画下来的原文。

2. **审核流尚未走完。** 设计中的流程是 `draft → reviewed → published`，
   对应"双执业药师交叉审核"。DDInter 导入的规则**一律 draft**，
   等级最高只到 `caution` —— 机器导入的数据不允许触发禁忌级硬拦截。

3. **中成药未收录。** `is_tcm` 字段与成分拆解逻辑已实现，
   但缺少中成药成分表，当前 `drugs.yaml` 中没有中成药条目。

---

## 数据来源

### 已接入

| 来源 | 许可 | 接入内容 | 接入方式 |
|---|---|---|---|
| 手工整理（说明书 / 指南要义） | 项目自有 | 182 条规则主体、67 药品、69 条人群禁忌 | `data/seed/` 直接维护 |
| 剂型扩充（自有整理） | 项目自有 | +57 个剂型条目（成分已有，补常见剂型：片/胶囊/注射液等） | `scripts/augment_drugs.py` 追加到 `drugs.yaml`，只填客观字段 |
| **[DDInter 2.0](https://ddinter2.scbdd.com)** | **CC BY-NC-SA 4.0**（署名·非商业·同许可） | +765 条成分对规则（映射 57/66 个成分；Major→caution，其余→monitor） | `scripts/import_ddinter.py` 从 `data/raw/ddinter/*.csv`（13 MB，**不入库**）生成 `ddi_ddinter.yaml` |
| **[PubChem](https://pubchem.ncbi.nlm.nih.gov)** PUG-REST | 公有领域（美国政府作品） | 66 个成分的 CAS 号 | `scripts/fetch_cas.py` 写回 `drugs.yaml` |

两条红线（外部数据接入脚本也遵守）：

1. **不生成 contraindicated**：禁忌级会触发评估硬拦截，只能来自人工核验；
   DDInter 的 Major 也只映射到 `caution`，核验后可在后台升级。
2. **只保留两端都能映射到本库成分的 pair**，映射不上的丢弃并计数报告，
   不猜、不补全。

### 评估过但未接入

| 来源 | 不接入的原因 |
|---|---|
| DrugBank | 需学术许可，且官方 2026-05 起暂停下载，恢复时间未定 |
| KEGG | 完整数据需订阅；REST API 只适合查询不适合批量入库 |
| DailyMed DDI（Zenodo） | 英文 FDA 标签衍生数据，成分名与中文库映射困难，暂缓 |
| HODDI / TWOSIDES / OGB-DDI | 图机器学习预测基准，不是知识库，无严重度/机制字段 |
| openFDA / ChEMBL / OnSIDES | 原料型英文数据，暂无直接映射路径，留待后续 |
| WHO ATC/DDD | ATC 已在手工数据中填全（66/66），无需重复接入 |
| 国内公开渠道（NMPA 等） | 无结构化开放许可数据可用 |

重新获取 DDInter 原始 CSV 的命令见 `scripts/import_ddinter.py` 的 docstring；
转换是幂等的，改完重新跑 `python scripts/load_seed.py` 即可入库。

---

## 已知限制（不是 bug，是尚未完成的工作）

| 项 | 现状 | 说明 |
|---|---|---|
| **溯源覆盖** | 84.7% | 剩余 145 条手工规则缺依据；DDInter 条目的 excerpt 只有 Level 标记，无机制原文 |
| **评测数字** | 上限估计 | 负样本是"规则库未收录"而非"临床无相互作用"，精确率被高估。需要独立验证集 |
| **独立验证集** | 未构建 | 必须来自说明书/指南，且**不得参与种子录入**。这是 W14–16 的关键交付物 |
| **用户系统 / 用药档案** | 未开始 | MVP 范围外 |
| **药品覆盖率** | 124 个药品 | 57 个是剂型扩充（无规格/批准文号，待核验）；中文药品无开放批量数据源，持续扩充靠后台新建与智能导入 |
| **并发** | 按单并发设计 | 未做压力测试，也未做多进程共享状态的处理。当前定位是单实例小流量 |

### 延迟实测

本机实测（预热后 5 次取中位数，MiniMax-M3，`llm.thinking: false`）：

| 路径 | 中位数 | 区间 |
|---|---|---|
| 命中禁忌级（不走 LLM） | **2 ms** | 2–3 ms |
| 走 LLM（慎用/关注级） | **3.2 s** | 2.9–4.5 s |

关于 `llm.thinking`：

| 配置 | 单次请求延迟 | 输出 tokens | JSON 可解析 |
|---|---|---|---|
| `true`（自适应思考） | 8.5 s | 357 | ✅ |
| `false`（默认） | 2.6 s | 159 | ✅ |

自适应思考提升了措辞质量，但会把端到端推到 5 秒目标之外，因此默认关闭。

> **每个新进程的首次 LLM 调用会明显偏慢**（实测 4.7 s vs 后续 1.0–1.3 s），
> 因为要建立 TLS 连接。让服务进程常驻即可（连接会复用），
> 不要每次请求起新进程。

### 二次校验的实际触发率

二次校验会拿模型输出的 `level` 和规则引擎的真值比对，不一致就整体回退规则答案。
这个机制**确实会触发**，不是摆设。

早期 prompt 只在规则结论里给中文等级（`关注`），却要求模型填英文枚举（`monitor`），
逼模型自己做一次翻译 —— 实测**约 1/3 的请求**因此把 `monitor` 写成 `caution` 被打回，
白白浪费一次调用。

修正方式有两处：

1. 在规则结论里直接把英文枚举值摆出来（`（level 字段必须原样填 "monitor"）`），
   并明确禁止"因为患者情况看起来更严重就升一级"
2. `_attach_sources` 的 `level` / `level_cn` 一律取自规则引擎，不取模型输出 ——
   让"等级由规则决定"成为**结构上的事实**，而不是依赖校验逻辑没有漏洞

修正后实测 **20/20 次二次校验通过**（修正前约 2/3）。

> 这个数字来自单一药品组合、单次会话的连续调用，样本量小，只应作为
> "修正有效"的证据，不能当作线上通过率承诺。真实通过率需要更大规模的
> 离线评测来测。

---

## 评测

```bash
python scripts/run_eval.py --out eval_results.json
```

当前基线（**种子数据 v0.1，182 条规则时期**测得；DDInter 扩到 947 条后
尚未重跑 —— 语料变大后 Recall@k 分母变严，重跑 `run_eval.py` 前不要引用
这组检索数字作为现状）：

**检测质量**（规则引擎，与嵌入后端无关）

| 指标 | 值 | 口径 |
|---|---|---|
| 召回率 | 1.000 | 正样本取自规则库，衡量链路是否通畅 |
| 精确率 | 1.000 | **上限估计**（见下） |
| 禁忌级漏检率 | **0.000** | 安全底线 |

**检索质量**（RAG，仅对正样本）

| 指标 | hash | **m3e** |
|---|---|---|
| Recall@5 | 0.967 | **1.000** |
| MRR | 0.878 | **0.963** |
| Recall@k（禁忌级子集） | 0.961 | **1.000** |

换成 m3e 后 MRR 从 0.878 提到 0.963 —— 不只是"找得到"，而是**排得更靠前**。

**关于"精确率 1.000"**：负样本取自规则库中不存在的成分对，而规则库本身是
评测正样本的来源——这是同义反复。真实精确率必然更低。
可对外引用的数字必须来自独立验证集。

**关于 RAG 的定位**：RAG **不参与**风险判定，这是架构的有意设计。
早期版本曾让 RAG 充当检测器，精确率从 1.00 掉到 0.50——因为它按字面重合
召回，会把不相关的组合也判为有风险。现在 RAG 只负责为已知风险找回可引用的原文片段。

---

## 嵌入模型

检索层的语义能力由嵌入模型决定。三种后端：

| 后端 | 维度 | 权重 | 依赖 | 用途 |
|---|---|---|---|---|
| **`m3e`** | 768 | 390 MB | torch + transformers | **推荐**。中文短文本 |
| `bge-m3` | 1024 | 2.2 GB | + FlagEmbedding | 多语言 / 长文本 |
| `hash` | 1024 | 0 | numpy | 兜底，**不是语义检索** |

选 m3e 而不是 bge-m3 的理由：本项目语料是**中文短文本**（药名 + 机制 + 建议，
几十字），m3e 完全够用，权重小 5 倍，且不需要 FlagEmbedding 那一层依赖。
bge-m3 的优势在长文本（8192 token）和多语言，本项目用不上。

### 实现方式

用**原生 `transformers`** 实现，不依赖 `sentence-transformers` ——
这个模型的句子编码就是「mean pooling + L2 归一化」，十行代码，
没必要为它多装一个库（sentence-transformers 还会再拖进 datasets 等一串依赖）。

⚠️ 池化方式取自模型自带的 `1_Pooling/config.json`（`pooling_mode_mean_tokens`）。
**换模型前先看这个文件** —— 池化选错不会报错，只会让检索结果悄悄变烂。

⚠️ **mean pooling 必须用 attention_mask 排除 padding**，否则短句会被 padding
token 稀释，句子越短向量越偏。

### 冷启动 41 秒

实测（本机 CPU）：

| 阶段 | 耗时 |
|---|---|
| `import torch` + `transformers` | 16.7 s |
| 加载权重 + 首次编码 | 24.5 s |
| **合计冷启动** | **~41 s** |
| 单条查询编码（热） | **89 ms** |
| 947 篇文档批量编码 | **~4 min**（182 篇时约 90 s，随语料线性增长） |

所以 **API 在 `create_app()` 里预载模型**，日志会打 `检索就绪，耗时 41.6s`。
不在启动时付这个成本，就得让第一个发请求的用户付 —— 而他会以为服务挂了。

单条查询只要 89ms，这是一次性成本。进程常驻就不会反复付。

### 权重不入库

`models/` 已在 `.gitignore` 中（几百 MB 的二进制进 git，历史里永远删不掉）。
部署到新机器时二选一：

```bash
cp -r <已有环境>/m3e-base models/m3e-base   # 从别处拷
python scripts/fetch_model.py               # 或从 ModelScope 拉
```

`fetch_model.py` 默认走 **ModelScope** 而不是 HuggingFace —— 国内访问 HF 经常超时。

---

## 测试

```bash
python -m pytest -q          # 217 项
```

几项值得单独说明的：

- **`test_rules.py::test_every_contraindicated_pair_is_detected`**
  对规则库里**每一条**禁忌级规则构造输入并断言必被检出。安全底线。
- **`test_item_contract.py`**
  断言 API 返回的条目在**所有路径**（硬拦截 / 降级 / LLM）下字段一致。
  这条是为了防住一类具体回归：禁忌级路径不经过大模型，如果生成层换一套字段名，
  前端会在最重要的一条路径上拿到缺字段的结果，而其他测试全绿。
- **`test_config.py::test_unknown_provider_raises_not_falls_back`**
  供应商名拼错必须报错。

---

## 部署

本仓库只提供**应用本身**，不含 Nginx / systemd / Docker 配置 —— 那些按你的
服务器环境自己配。这里只列应用侧需要注意的几点。

### 跑起来

```bash
python scripts/run_api.py --prod            # waitress，生产用
python scripts/run_api.py                   # Flask 开发服务器，仅本机调试
```

`--prod` 用 waitress 而不是 Flask 自带的开发服务器。开发服务器有调试器、
单线程、无超时保护，不该对外。

启动会**预载 m3e 嵌入模型，约 41 秒**。日志出现 `检索就绪，耗时 41.6s` 才算
真正可用 —— 进程守护的启动超时要给够，否则会被反复杀掉重启。

### 单并发定位

当前按**单实例、小流量**设计：没有做压力测试，也没有处理多进程共享状态
（SQLite 写入、BM25 索引都是进程内内存）。真要多实例，先解决这两点。

### 反代到本服务

前端是纯静态文件，后端是 HTTP 服务，Nginx 那边大致是：

```nginx
location /        { root /path/to/ddi/web; try_files $uri $uri/ /index.html; }
location /api/    { proxy_pass http://127.0.0.1:5000; proxy_read_timeout 120s; }
```

`proxy_read_timeout` 要给到 120s —— 大模型调用实测 3–10 秒，首次建 TLS 更久，
默认 60s 会偶发 504。

**建议加按 IP 限流**：一次评估花一次大模型调用，不限流等于公开送额度。

前后端同域时用不上 CORS；分域部署则把 `api.cors_origins` 写成前端实际域名。

### 上线前

- [ ] `api.debug: false`
- [ ] `api.cors_origins` 写具体域名，不要 `*`
- [ ] `embed.backend` 是 `m3e`，权重已就位
- [ ] 索引是用 m3e 建的（768 维）
- [ ] `scripts/check_data.py` 的严重项已处理
- [ ] `config.yaml` 权限收紧，且不在 git 里
- [ ] 备份 `config.yaml` 和 `data/seed/`（仅有的两份不可再生资产；
      `models/` 能重下，`data/build/` 能重建，都不用备份）

---

## 许可与合规

- 本项目代码：待定
- `M3E`：Apache 2.0 ｜ `FAISS`：MIT ｜ `rank_bm25`：Apache 2.0
- **公开数据集**：DrugBank 商用需授权；DDInter 为 CC BY-NC（**非商用**）。
  若后期引入，务必先确认许可
- 调用第三方大模型 API 属《个人信息保护法》下的**委托处理**，
  需签订数据处理协议并开展 **PIPIA**
- 产品定位为"辅助评估工具"，不替代医师诊断
