/* ══════════════════════════════════════════════════════════════
   AI 联合用药安全评估 —— 前端逻辑

   两条安全相关的界面约束，改动时请保留：

   1. **识别不确定必须显式提示**。后端归一化层"宁可让用户确认，不要猜"，
      前端要把这个判断透出来 —— 用户以为查的是 A 药、系统实际算的是 B 药，
      整个评估就是无效的。
   2. **未识别的药品必须显式告警**。后端会跳过没匹配上的药名，
      如果界面不说，用户会以为"没报风险 = 安全"。

   所有服务端与用户数据一律走 textContent，不用 innerHTML —— 药名是自由输入。
   ══════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  var API = (window.DDI_API_BASE || '/api/v1').replace(/\/$/, '');

  // 风险等级 → 图标 + 中文标签 + 样式类。
  // 颜色永远和图标、文字一起出现，不让颜色单独承载含义。
  var LEVELS = {
    contraindicated: { cn: '禁忌', icon: '⛔', risk: 'is-contraindicated', verdict: 'is-critical' },
    caution:         { cn: '慎用', icon: '⚠',  risk: 'is-caution',         verdict: 'is-serious'  },
    monitor:         { cn: '关注', icon: 'ℹ',  risk: 'is-monitor',         verdict: 'is-warning'  },
    none:            { cn: '未发现风险', icon: '✓', risk: '',              verdict: 'is-good'     }
  };

  var VERDICT_SUB = {
    contraindicated: '存在禁忌级联用，规则引擎已硬拦截。请勿自行服用，立即咨询医师或药师。',
    caution:         '存在需要慎重的联用组合，请在医师或药师指导下使用。',
    monitor:         '存在需要关注的联用组合，用药期间请留意身体反应。',
    none:            '在已收录的规则范围内未发现相互作用。这不等于绝对安全 —— 规则库覆盖有限。'
  };

  // ── DOM 快捷方式 ──────────────────────────────────
  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;   // 永远用 textContent
    return n;
  }

  // ── 状态 ──────────────────────────────────────────
  // drugs: [{ raw, matched: {...}|null, candidates: [...], pending: bool }]
  var state = { drugs: [], busy: false };

  // ══════════════════════════════════════════════════
  //  网络
  // ══════════════════════════════════════════════════
  function api(path, body) {
    return fetch(API + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          throw new Error(data.error || ('请求失败（HTTP ' + res.status + '）'));
        }
        return data;
      });
    });
  }

  function checkHealth() {
    var badge = $('health-badge');
    api('/health').then(function (h) {
      var parts = [];
      if (h.llm_configured) {
        parts.push((h.llm_provider || 'LLM') + ' 已连接');
        badge.className = 'badge badge-ok';
      } else {
        parts.push('未配置大模型，仅规则结论');
        badge.className = 'badge badge-warn';
      }
      badge.textContent = parts.join(' · ');
      badge.title = '规则 ' + h.rules + ' 条 · 药品 ' + h.drugs_indexed +
                    ' 种 · 检索 ' + (h.rag_ready ? '就绪' : '未构建') +
                    (h.llm_model ? ' · 模型 ' + h.llm_model : '');
    }).catch(function () {
      badge.className = 'badge badge-warn';
      badge.textContent = '后端未连接';
      badge.title = '无法访问 ' + API + '/health';
    });
  }

  // ══════════════════════════════════════════════════
  //  药品条目
  // ══════════════════════════════════════════════════
  function addDrug(raw) {
    var name = String(raw || '').trim();
    if (!name) return;
    if (state.drugs.some(function (d) { return d.raw === name; })) return;

    var entry = { raw: name, matched: null, candidates: [], pending: true };
    state.drugs.push(entry);
    renderChips();
    syncAssessButton();

    api('/normalize', { drugs: [name] }).then(function (data) {
      var r = (data.results || [])[0] || {};
      entry.matched = r.matched || null;
      entry.candidates = r.candidates || [];
      entry.pending = false;
      renderChips();
      syncAssessButton();
    }).catch(function () {
      entry.pending = false;
      entry.error = true;
      renderChips();
      syncAssessButton();
    });
  }

  function removeDrug(index) {
    state.drugs.splice(index, 1);
    renderChips();
    syncAssessButton();
  }

  function pickCandidate(index, drugId) {
    var entry = state.drugs[index];
    var hit = entry.candidates.filter(function (c) { return c.drug_id === drugId; })[0];
    if (!hit) return;
    entry.matched = hit;
    entry.manual = true;      // 用户手动选的，不再提示"置信度低"
    renderChips();
  }

  function renderChips() {
    var box = $('drug-chips');
    box.textContent = '';

    state.drugs.forEach(function (d, i) {
      var chip = el('div', 'chip chip-pending');
      var top = el('div', 'chip-top');

      if (d.pending) {
        chip.className = 'chip chip-pending';
        top.appendChild(el('span', 'chip-icon', '⋯'));
        top.appendChild(el('span', 'chip-raw', d.raw));
        top.appendChild(el('span', 'chip-note', '识别中…'));
      } else if (d.error) {
        chip.className = 'chip chip-error';
        top.appendChild(el('span', 'chip-icon', '✕'));
        top.appendChild(el('span', 'chip-raw', d.raw));
        top.appendChild(el('span', 'chip-note', '无法连接后端'));
      } else if (d.matched) {
        var low = d.matched.needs_confirmation && !d.manual;
        chip.className = 'chip ' + (low ? 'chip-warn' : 'chip-ok');
        top.appendChild(el('span', 'chip-icon', low ? '⚠' : '✓'));
        top.appendChild(el('span', 'chip-raw', d.raw));
        top.appendChild(el('span', 'chip-arrow', '→'));

        var name = el('span', 'chip-name', d.matched.name_cn);
        var conf = el('em', 'chip-conf', ' ' + Math.round(d.matched.confidence * 100) + '%');
        name.appendChild(conf);
        top.appendChild(name);

        if (low) {
          var note = el('p', 'chip-note',
            '匹配置信度偏低，请确认是不是这个药。系统不会替你猜。');
          chip.appendChild(top);
          chip.appendChild(note);
          chip.appendChild(buildCandidates(i, d));
          chip.appendChild(buildRemove(i));
          box.appendChild(chip);
          return;
        }
      } else {
        chip.className = 'chip chip-error';
        top.appendChild(el('span', 'chip-icon', '✕'));
        top.appendChild(el('span', 'chip-raw', d.raw));
        top.appendChild(el('span', 'chip-note', '未识别到该药品'));
      }

      chip.appendChild(top);

      if (!d.matched && d.candidates.length) {
        chip.appendChild(el('p', 'chip-note', '你是不是想找：'));
        chip.appendChild(buildCandidates(i, d));
      } else if (!d.matched && !d.pending && !d.error) {
        chip.appendChild(el('p', 'chip-note',
          '规则库中没有这个药，它不会参与本次评估。请换用通用名再试。'));
      }

      chip.appendChild(buildRemove(i));
      box.appendChild(chip);
    });

    $('drug-empty').hidden = state.drugs.length > 0;
  }

  function buildCandidates(index, d) {
    var wrap = el('div', 'candidates');
    d.candidates.slice(0, 5).forEach(function (c) {
      var btn = el('button', 'cand-btn', c.name_cn + ' ' + Math.round(c.confidence * 100) + '%');
      btn.type = 'button';
      btn.addEventListener('click', function () { pickCandidate(index, c.drug_id); });
      wrap.appendChild(btn);
    });
    return wrap;
  }

  function buildRemove(index) {
    var btn = el('button', 'chip-remove', '×');
    btn.type = 'button';
    btn.setAttribute('aria-label', '移除 ' + state.drugs[index].raw);
    btn.addEventListener('click', function () { removeDrug(index); });
    return btn;
  }

  // 已识别（matched 存在）的药才计入，未识别的会被后端跳过
  function resolvedCount() {
    return state.drugs.filter(function (d) { return d.matched; }).length;
  }

  function syncAssessButton() {
    var btn = $('assess-btn');
    var pending = state.drugs.some(function (d) { return d.pending; });
    var ok = !state.busy && !pending && resolvedCount() >= 2;
    btn.disabled = !ok;
    btn.textContent = state.busy
      ? '评估中…'
      : (resolvedCount() >= 2 ? '开始评估' : '至少需要 2 种已识别的药品');
  }

  // ══════════════════════════════════════════════════
  //  患者画像
  // ══════════════════════════════════════════════════
  function collectProfile() {
    var p = {};
    var age = $('p-age').value.trim();
    if (age) p.age = parseInt(age, 10);
    var sex = $('p-sex').value;
    if (sex) p.sex = sex;
    var hep = $('p-hepatic').value;
    if (hep && hep !== 'normal') p.hepatic = hep;
    var ren = $('p-renal').value;
    if (ren && ren !== 'normal') p.renal = ren;
    if ($('p-pregnancy').checked) p.pregnancy = true;
    var allergies = $('p-allergies').value.trim();
    if (allergies) {
      p.allergies = allergies.split(/[、,，;；\s]+/).filter(Boolean);
    }
    return p;
  }

  // ══════════════════════════════════════════════════
  //  评估
  // ══════════════════════════════════════════════════
  function showState(which) {
    ['state-idle', 'state-loading', 'state-error', 'state-result'].forEach(function (id) {
      $(id).hidden = (id !== which);
    });
  }

  function assess() {
    var drugs = state.drugs.filter(function (d) { return d.matched; })
                           .map(function (d) { return d.matched.name_cn; });
    if (drugs.length < 2) return;

    state.busy = true;
    syncAssessButton();
    showState('state-loading');
    $('loading-hint').textContent = $('opt-llm').checked
      ? '正在做相互作用匹配，随后由大模型生成解释'
      : '正在做相互作用匹配';

    var t0 = Date.now();
    api('/assess', {
      drugs: drugs,
      profile: collectProfile(),
      use_llm: $('opt-llm').checked,
      use_rag: $('opt-rag').checked
    }).then(function (report) {
      render(report);
      showState('state-result');
    }).catch(function (e) {
      $('error-msg').textContent = e.message + '（' + ((Date.now() - t0) / 1000).toFixed(1) + ' 秒）';
      showState('state-error');
    }).then(function () {
      state.busy = false;
      syncAssessButton();
    });
  }

  // ── 渲染 ──────────────────────────────────────────
  function render(r) {
    var lv = LEVELS[r.overall_risk] || LEVELS.none;

    // 总体横幅
    var verdict = $('verdict');
    verdict.className = 'verdict ' + lv.verdict;
    $('verdict-icon').textContent = lv.icon;
    $('verdict-level').textContent = r.hard_blocked
      ? '⛔ 禁忌联用 · 已拦截'
      : lv.cn;
    $('verdict-sub').textContent = r.hard_blocked
      ? VERDICT_SUB.contraindicated
      : (VERDICT_SUB[r.overall_risk] || VERDICT_SUB.none);

    // 降级说明 —— 用户有权知道这次结果是不是模型给的
    var deg = $('degraded');
    var eng = r.engine || {};
    if (eng.degraded_reason) {
      deg.hidden = false;
      $('degraded-msg').textContent =
        '本次未使用大模型解释（' + eng.degraded_reason + '），下方内容来自规则引擎。';
    } else if (eng.recheck_passed === false) {
      deg.hidden = false;
      $('degraded-msg').textContent =
        '模型输出与规则引擎结论不一致，已整体回退为规则答案。' +
        (eng.recheck_notes && eng.recheck_notes.length
          ? '（' + eng.recheck_notes.join('；') + '）' : '');
    } else {
      deg.hidden = true;
    }

    // 识别结果
    $('drug-count').textContent = r.drugs.length;
    renderResolved(r.drugs, r.ingredients);

    // 风险明细
    var items = r.items || [];
    $('risk-count').textContent = items.length;
    renderRisks(items);
    $('risk-none').hidden = items.length > 0;

    // 摘要
    var hasSummary = !!(r.summary || r.patient_note);
    $('sec-summary').hidden = !hasSummary;
    $('summary-text').textContent = r.summary || '';
    $('patient-note').hidden = !r.patient_note;
    $('patient-note').textContent = r.patient_note || '';

    // 免责声明
    $('disclaimer').textContent = r.disclaimer || '';

    // 元信息
    renderMeta(r);
  }

  function renderResolved(drugs, ingredients) {
    var box = $('drug-resolved');
    box.textContent = '';

    (drugs || []).forEach(function (d) {
      var row = el('div', 'resolved-row');
      if (d.name_cn) {
        row.appendChild(el('span', 'ok', '✓'));
        row.appendChild(el('span', 'raw', d.raw));
        row.appendChild(el('span', 'arrow', '→'));
        row.appendChild(el('span', null, d.name_cn));
        if (d.needs_confirmation) {
          row.appendChild(el('span', 'ing',
            '（匹配置信度 ' + Math.round(d.confidence * 100) + '%，建议核对）'));
        }
      } else {
        row.appendChild(el('span', 'bad', '✕'));
        row.appendChild(el('span', 'raw', d.raw));
        row.appendChild(el('span', 'ing', '未识别，未参与评估'));
      }
      box.appendChild(row);
    });

    if (ingredients && ingredients.length) {
      var row = el('div', 'resolved-row');
      row.appendChild(el('span', 'ing', '拆解出的成分：' + ingredients.join('、')));
      box.appendChild(row);
    }
  }

  function renderRisks(items) {
    var box = $('risk-list');
    box.textContent = '';

    items.forEach(function (it) {
      var lv = LEVELS[it.level] || LEVELS.monitor;
      var card = el('article', 'risk ' + lv.risk);

      var head = el('div', 'risk-head');
      head.appendChild(el('span', 'risk-icon', lv.icon));
      head.appendChild(el('span', 'risk-title', it.title || ''));
      head.appendChild(el('span', 'risk-tag', it.level_cn || lv.cn));
      card.appendChild(head);

      var who = (it.drugs && it.drugs.length) ? it.drugs.join(' + ') : '';
      var kicker = [who, it.kind === 'population' ? '人群禁忌' : '药物相互作用',
                    it.evidence_level ? '证据等级 ' + it.evidence_level : '']
                   .filter(Boolean).join(' · ');
      if (kicker) card.appendChild(el('p', 'risk-kicker', kicker));

      var body = el('dl', 'risk-body');
      addField(body, '机制', it.mechanism);
      addField(body, '后果', it.consequence);
      addField(body, '触发条件', it.note);
      addField(body, '建议', it.suggestion, true);
      card.appendChild(body);

      if (it.sources && it.sources.length) {
        card.appendChild(buildSources(it.sources));
      } else {
        // 没有依据就必须说出来。悄悄不显示，读者会以为这条结论是有出处的。
        card.appendChild(el('p', 'source-caveat',
          '本条规则暂未录入溯源依据，结论仅来自规则库本身。'));
      }
      box.appendChild(card);
    });
  }

  function addField(dl, label, value, emphasise) {
    if (!value) return;
    var f = el('div', 'risk-field' + (emphasise ? ' is-suggestion' : ''));
    f.appendChild(el('dt', null, label));
    f.appendChild(el('dd', null, value));
    dl.appendChild(f);
  }

  function buildSources(sources) {
    var wrap = el('details', 'sources');
    wrap.appendChild(el('summary', null, '查看依据（' + sources.length + ' 条）'));

    sources.forEach(function (s) {
      var item = el('div', 'source-item');
      var title = el('p', 'source-title', s.title || '未命名来源');
      if (s.type) title.appendChild(el('span', 'source-type', s.type));
      item.appendChild(title);
      if (s.excerpt) item.appendChild(el('p', 'source-excerpt', s.excerpt));
      wrap.appendChild(item);
    });

    // 数据现状必须写在用户看得到的地方，不能只写在 README 里
    wrap.appendChild(el('p', 'source-caveat',
      '以上为依据说明书/指南整理的要义，非逐字原文；规则库当前为待审核草稿，未经执业药师核验。'));
    return wrap;
  }

  function renderMeta(r) {
    var eng = r.engine || {};
    var dl = $('meta-list');
    dl.textContent = '';

    var rows = [
      ['总耗时', (r.elapsed_ms || 0) + ' ms'],
      ['是否使用大模型', eng.used_llm ? '是' : '否'],
      ['使用模型', eng.model || '—'],
      ['二次校验', eng.recheck_passed === null || eng.recheck_passed === undefined
        ? '—' : (eng.recheck_passed ? '通过' : '未通过，已回退')],
      ['检索命中片段', String(eng.rag_hits || 0)]
    ];
    if (eng.usage && eng.usage.output_tokens) {
      rows.push(['输出 tokens', String(eng.usage.output_tokens)]);
    }

    rows.forEach(function (pair) {
      dl.appendChild(el('dt', null, pair[0]));
      dl.appendChild(el('dd', null, pair[1]));
    });
  }

  // ══════════════════════════════════════════════════
  //  事件绑定
  // ══════════════════════════════════════════════════
  function init() {
    $('drug-add').addEventListener('click', function () {
      var input = $('drug-input');
      // 支持一次粘贴多个：按中英文逗号、顿号、分号、换行拆开
      input.value.split(/[、,，;；\n]+/).forEach(addDrug);
      input.value = '';
      input.focus();
    });

    $('drug-input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        $('drug-add').click();
      }
    });

    $('assess-btn').addEventListener('click', assess);

    $('reset-btn').addEventListener('click', function () {
      state.drugs = [];
      ['p-age', 'p-allergies'].forEach(function (id) { $(id).value = ''; });
      ['p-sex'].forEach(function (id) { $(id).value = ''; });
      ['p-hepatic', 'p-renal'].forEach(function (id) { $(id).value = 'normal'; });
      $('p-pregnancy').checked = false;
      $('drug-input').value = '';
      renderChips();
      syncAssessButton();
      showState('state-idle');
    });

    $('theme-toggle').addEventListener('click', function () {
      var root = document.documentElement;
      var now = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', now);
      try { localStorage.setItem('ddi-theme', now); } catch (e) { /* 隐私模式 */ }
    });

    try {
      var saved = localStorage.getItem('ddi-theme');
      if (saved) document.documentElement.setAttribute('data-theme', saved);
    } catch (e) { /* 隐私模式 */ }

    renderChips();
    syncAssessButton();
    checkHealth();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
