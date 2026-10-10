-- AI 联合用药安全评估系统 · 数据库结构
-- 目标库：SQLite（开发） / MySQL 8（生产），字段类型保持兼容
-- 核心原则：DDI 规则挂在【成分】上，不挂在【商品】上

PRAGMA foreign_keys = ON;

-- ── 成分（原子级）────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingredient (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name_cn     TEXT NOT NULL UNIQUE,
  name_en     TEXT,
  cas         TEXT,
  atc         TEXT,
  category    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ing_category ON ingredient(category);

-- ── 药品（商品级）────────────────────────────────────
CREATE TABLE IF NOT EXISTS drug (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name_cn     TEXT NOT NULL,              -- 通用名
  trade_names TEXT DEFAULT '[]',          -- JSON 数组
  aliases     TEXT DEFAULT '[]',          -- JSON 数组
  pinyin      TEXT,                       -- 全拼，用于模糊匹配
  pinyin_abbr TEXT,                       -- 首字母缩写：fbd
  dosage_form TEXT,
  is_otc      INTEGER NOT NULL DEFAULT 0,
  is_tcm      INTEGER NOT NULL DEFAULT 0, -- 中成药
  approval_no TEXT,
  UNIQUE(name_cn, dosage_form)
);
CREATE INDEX IF NOT EXISTS idx_drug_name   ON drug(name_cn);
CREATE INDEX IF NOT EXISTS idx_drug_pinyin ON drug(pinyin);

-- ── 药品 ↔ 成分 ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS drug_ingredient (
  drug_id       INTEGER NOT NULL REFERENCES drug(id) ON DELETE CASCADE,
  ingredient_id INTEGER NOT NULL REFERENCES ingredient(id) ON DELETE CASCADE,
  strength      TEXT,
  PRIMARY KEY (drug_id, ingredient_id)
);

-- ── ★ DDI 规则（核心壁垒）────────────────────────────
CREATE TABLE IF NOT EXISTS ddi_rule (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ing_a_id       INTEGER NOT NULL REFERENCES ingredient(id),
  ing_b_id       INTEGER NOT NULL REFERENCES ingredient(id),
  severity       TEXT NOT NULL CHECK (severity IN ('contraindicated','caution','monitor')),
  mechanism      TEXT,
  consequence    TEXT,
  suggestion     TEXT,
  evidence_level TEXT,                     -- A/B/C/D
  status         TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','reviewed','published')),
  reviewed_by    TEXT,
  CHECK (ing_a_id < ing_b_id)              -- 规范化：小 id 在前，保证唯一性
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ddi_pair ON ddi_rule(ing_a_id, ing_b_id);
CREATE INDEX IF NOT EXISTS idx_ddi_ing_a ON ddi_rule(ing_a_id);
CREATE INDEX IF NOT EXISTS idx_ddi_ing_b ON ddi_rule(ing_b_id);
-- 后台按状态/严重度筛选与批量审核流走这两个索引（当前 182 条不痛，
-- 规则库按目标是数千条准备）
CREATE INDEX IF NOT EXISTS idx_ddi_status ON ddi_rule(status);
CREATE INDEX IF NOT EXISTS idx_ddi_severity ON ddi_rule(severity);

-- ── 人群禁忌 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS population_rule (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id   INTEGER NOT NULL REFERENCES ingredient(id),
  condition_type  TEXT NOT NULL
                  CHECK (condition_type IN ('age','hepatic','renal','pregnancy','allergy')),
  condition_value TEXT NOT NULL,           -- ">=80" / "mild" / "severe"
  severity        TEXT NOT NULL CHECK (severity IN ('contraindicated','caution','monitor')),
  note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_pop_ing ON population_rule(ingredient_id);

-- ── 来源 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS source (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  source_type TEXT,                        -- 药典 | 说明书 | 临床指南 | 文献
  publisher   TEXT,
  year        INTEGER,
  url         TEXT,
  UNIQUE(title, year)
);

-- ── 规则 ↔ 来源（excerpt 是"可溯源"的兑现方式）──────
CREATE TABLE IF NOT EXISTS rule_source (
  rule_id   INTEGER NOT NULL REFERENCES ddi_rule(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES source(id),
  excerpt   TEXT,                          -- 原文摘录，前端"查看依据"直接展示
  PRIMARY KEY (rule_id, source_id)
);

-- ── LLM 智能导入暂存（待确认）──────────────────────────
-- 流程：粘贴文本 → 大模型解析 → 草稿落本表（pending）→
-- 人工核对/编辑 → 确认后才写入 ddi_rule 等正式表。
-- 驳回 = 硬删除（没有 rejected 状态）；confirmed 保留作轻量审计。
-- kind 的 CHECK 预留了 drug / population_rule —— 本版只产出 ddi_rule，
-- 但把枚举一次写全，以后放开不用重建表（SQLite 改 CHECK 要整表迁移）。
CREATE TABLE IF NOT EXISTS import_draft (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL
              CHECK (kind IN ('ddi_rule','drug','population_rule')),
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','confirmed')),
  fields      TEXT NOT NULL,               -- JSON：结构化草稿载荷（可编辑）
  raw_span    TEXT,                        -- LLM 标注的原文片段
  warnings    TEXT DEFAULT '[]',           -- JSON 数组：模型不确定之处
  batch_id    TEXT,                        -- 一次解析调用的分组 id
  source_name TEXT,                        -- 上传文件名/来源标签（选填）
  target_id   INTEGER,                     -- 确认后写入的正式表主键
  created_at  TEXT NOT NULL,               -- ISO8601 UTC
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_import_draft_status ON import_draft(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_import_draft_batch  ON import_draft(batch_id);

-- ── 评估请求日志（管理后台用）──────────────────────────
-- 隐私红线：只存白名单字段（age/sex/hepatic/renal/pregnancy），
-- **绝不存 allergies 内容**或其他自由文本患者数据。
CREATE TABLE IF NOT EXISTS assess_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at      TEXT NOT NULL,              -- ISO8601 UTC
  drugs_raw       TEXT NOT NULL,              -- JSON 数组：用户原始输入
  drugs_matched   TEXT,                       -- JSON 数组：归一化后的通用名
  profile_summary TEXT,                       -- JSON；仅 age/sex/hepatic/renal/pregnancy
  overall_risk    TEXT,
  hard_blocked    INTEGER NOT NULL DEFAULT 0,
  use_llm         INTEGER NOT NULL DEFAULT 0,
  use_rag         INTEGER NOT NULL DEFAULT 0,
  used_llm        INTEGER,                    -- 实际是否调用了大模型
  degraded_reason TEXT,
  elapsed_ms      INTEGER,
  ok              INTEGER NOT NULL DEFAULT 1,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_assess_log_created ON assess_log(created_at DESC);
