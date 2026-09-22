/* ══════════════════════════════════════════════════════════════
   管理后台 —— 前端逻辑

   安全约束（与 app.js 相同，改动时请保留）：

   1. **所有数据一律 textContent，绝不用 innerHTML** —— 规则文本来自
      数据库，是自由文本；这里是后台，一旦 XSS 就是管理员会话被劫。
   2. **令牌只存 sessionStorage**，关标签页即失效；每次请求随
      Authorization 头带上，不用 cookie（无 CSRF 面）。
   3. 响应里永远拿不到 api_key / admin.token 的值 —— 服务端只回布尔标记，
      前端也绝不渲染任何密钥。
   ══════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  var API = (window.DDI_API_BASE || '/api/v1').replace(/\/$/, '');
  var TOKEN_KEY = 'ddi-admin-token';

  var LEVELS = {
    contraindicated: { cn: '禁忌', icon: '⛔', cls: 'is-contraindicated' },
    caution:         { cn: '慎用', icon: '⚠',  cls: 'is-caution' },
    monitor:         { cn: '关注', icon: 'ℹ',  cls: 'is-monitor' },
    none:            { cn: '无',   icon: '✓',  cls: '' }
  };
  var STATUS_CN = { draft: '草稿', reviewed: '已审核', published: '已发布' };

  // ── DOM 工具（与 app.js 同款）──────────────
  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;   // 永远用 textContent
    return n;
  }

  function getToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function setToken(t) {
    try { t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY); }
    catch (e) { /* 隐私模式 */ }
  }

  // ── 网络 ──────────────────────────────────
  function api(path, body, method) {
    var headers = { 'Content-Type': 'application/json' };
    var token = getToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    var m = method || (body === undefined ? 'GET' : 'POST');
    return fetch(API + path, {
      method: m,
      headers: headers,
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 401) { showLogin('登录已过期，请重新输入令牌'); throw new Error('未授权'); }
        if (res.status === 403) { throw new Error(data.error || '管理后台未启用'); }
        if (!res.ok) throw new Error(data.error || ('请求失败（HTTP ' + res.status + '）'));
        return data;
      });
    });
  }

  // ── 登录门 ────────────────────────────────
  function showLogin(msg) {
    setToken('');
    $('admin-shell').hidden = true;
    $('login-panel').hidden = false;
    var err = $('login-error');
    if (msg) { err.textContent = msg; err.hidden = false; }
    else { err.hidden = true; }
  }

  function showShell() {
    $('login-panel').hidden = true;
    $('admin-shell').hidden = false;
  }

  function tryLogin(token) {
    return fetch(API + '/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token })
    }).then(function (res) {
      if (res.ok) { setToken(token); showShell(); return true; }
      return res.json().catch(function () { return {}; }).then(function (d) {
        showLogin(d.error || '登录失败');
        return false;
      });
    }).catch(function () {
      showLogin('无法连接后端');
      return false;
    });
  }

  // ── 标签页 ────────────────────────────────
  function switchTab(name) {
    ['rules', 'drugs', 'logs', 'config'].forEach(function (t) {
      var panel = $('tab-' + t);
      var btn = document.querySelector('.tab[data-tab="' + t + '"]');
      if (panel) panel.hidden = (t !== name);
      if (btn) {
        btn.classList.toggle('is-active', t === name);
        btn.setAttribute('aria-selected', t === name ? 'true' : 'false');
      }
    });
    if (location.hash.slice(1) !== name) {
      try { history.replaceState(null, '', '#' + name); } catch (e) { /* file:// */ }
    }
    if (name === 'rules') loadRules();
    if (name === 'drugs') loadDrugs();
    if (name === 'logs') loadLogs();
    if (name === 'config') loadConfig();
  }

  // ── 分页渲染助手 ──────────────────────────
  function renderPager(box, data, onGo) {
    box.textContent = '';
    if (!data.total) { box.appendChild(el('span', null, '共 0 条')); return; }
    var info = el('span', null,
      '共 ' + data.total + ' 条 · 第 ' + data.page + '/' + data.pages + ' 页');
    box.appendChild(info);

    function mkBtn(label, page, disabled) {
      var b = el('button', 'btn btn-ghost btn-sm', label);
      b.type = 'button';
      b.disabled = !!disabled;
      b.addEventListener('click', function () { onGo(page); });
      return b;
    }
    box.appendChild(mkBtn('上一页', data.page - 1, data.page <= 1));
    box.appendChild(mkBtn('下一页', data.page + 1, data.page >= data.pages));
  }

  // ════════════════════════════════════════
  //  规则库
  // ════════════════════════════════════════
  var rulesPage = 1;
  var currentRule = null;

  function loadRules(page) {
    rulesPage = page || 1;
    var params = new URLSearchParams({
      q: $('rule-q').value.trim(),
      severity: $('rule-severity').value,
      status: $('rule-status').value,
      page: rulesPage,
      page_size: 20
    });
    api('/admin/rules?' + params).then(function (data) {
      var tbody = $('rule-table').querySelector('tbody');
      tbody.textContent = '';
      data.items.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', null, String(r.id)));
        var pair = el('td');
        pair.appendChild(el('span', 'chip-name', r.ing_a_name));
        pair.appendChild(el('span', null, ' + '));
        pair.appendChild(el('span', 'chip-name', r.ing_b_name));
        tr.appendChild(pair);

        var lv = LEVELS[r.severity] || LEVELS.monitor;
        var sev = el('td');
        sev.appendChild(el('span', 'risk-tag ' + lv.cls, lv.icon + ' ' + lv.cn));
        tr.appendChild(sev);

        var st = el('td');
        st.appendChild(el('span', 'badge ' + (r.status === 'published' ? 'badge-ok' : 'badge-muted'),
          STATUS_CN[r.status] || r.status));
        tr.appendChild(st);

        tr.appendChild(el('td', null, r.evidence_level || '—'));
        tr.appendChild(el('td', null, String(r.source_count)));

        var actions = el('td', 'col-actions');
        var viewBtn = el('button', 'btn btn-ghost btn-sm', '编辑');
        viewBtn.type = 'button';
        viewBtn.addEventListener('click', function () { openRule(r.id); });
        actions.appendChild(viewBtn);
        tr.appendChild(actions);
        tbody.appendChild(tr);
      });
      renderPager($('rule-pager'), data, loadRules);
    }).catch(function (e) {
      var tbody = $('rule-table').querySelector('tbody');
      tbody.textContent = '';
      var tr = el('tr');
      var td = el('td', 'is-err');
      td.colSpan = 7;
      td.textContent = '加载失败：' + e.message;
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
  }

  function openRule(id) {
    api('/admin/rules/' + id).then(function (r) {
      currentRule = r;
      $('rule-editor').hidden = false;
      $('rule-editor-title').textContent = '编辑规则 #' + r.id;
      $('rule-ing-a').textContent = r.ing_a_name;
      $('rule-ing-b').textContent = r.ing_b_name;
      $('rule-f-severity').value = r.severity;
      $('rule-f-status').value = r.status;
      $('rule-f-evidence').value = r.evidence_level || '';
      $('rule-f-reviewed-by').value = r.reviewed_by || '';
      $('rule-f-mechanism').value = r.mechanism || '';
      $('rule-f-consequence').value = r.consequence || '';
      $('rule-f-suggestion').value = r.suggestion || '';

      var box = $('rule-sources');
      box.textContent = '';
      if (r.sources && r.sources.length) {
        box.appendChild(el('h4', 'subhead', '溯源依据（' + r.sources.length + ' 条）'));
        r.sources.forEach(function (s) {
          var item = el('div', 'source-item');
          var title = el('p', 'source-title', s.title || '未命名');
          if (s.source_type) title.appendChild(el('span', 'source-type', ' ' + s.source_type));
          item.appendChild(title);
          if (s.excerpt) item.appendChild(el('p', 'source-excerpt', s.excerpt));
          box.appendChild(item);
        });
      } else {
        box.appendChild(el('p', 'source-caveat', '本条规则暂无溯源依据。'));
      }
      setMsg('');
      $('rule-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }).catch(function (e) { alert('加载规则失败：' + e.message); });
  }

  function setMsg(text, ok) {
    var box = $('rule-editor-msg');
    box.textContent = text || '';
    box.className = 'editor-msg' + (text ? (ok ? ' is-ok' : ' is-err') : '');
  }

  function saveRule() {
    if (!currentRule) return;
    var payload = {
      severity: $('rule-f-severity').value,
      status: $('rule-f-status').value,
      evidence_level: $('rule-f-evidence').value || null,
      reviewed_by: $('rule-f-reviewed-by').value.trim() || null,
      mechanism: $('rule-f-mechanism').value.trim() || null,
      consequence: $('rule-f-consequence').value.trim() || null,
      suggestion: $('rule-f-suggestion').value.trim() || null
    };
    api('/admin/rules/' + currentRule.id, payload, 'PUT')
      .then(function () {
        setMsg('已保存并重载规则索引', true);
        loadRules(rulesPage);
      })
      .catch(function (e) { setMsg('保存失败：' + e.message, false); });
  }

  function deleteRule() {
    if (!currentRule) return;
    var btn = $('rule-delete');
    if (btn.dataset.confirm !== '1') {
      btn.dataset.confirm = '1';
      btn.textContent = '确认删除？';
      setTimeout(function () { btn.dataset.confirm = ''; btn.textContent = '删除'; }, 4000);
      return;
    }
    btn.dataset.confirm = '';
    btn.textContent = '删除';
    api('/admin/rules/' + currentRule.id, undefined, 'DELETE')
      .then(function () {
        $('rule-editor').hidden = true;
        currentRule = null;
        loadRules(rulesPage);
      })
      .catch(function (e) { setMsg('删除失败：' + e.message, false); });
  }

  // ════════════════════════════════════════
  //  药品数据
  // ════════════════════════════════════════
  var drugsPage = 1;

  function loadDrugs(page) {
    drugsPage = page || 1;
    var params = new URLSearchParams({ q: $('drug-q').value.trim(), page: drugsPage, page_size: 20 });
    api('/admin/drugs?' + params).then(function (data) {
      var tbody = $('drug-table').querySelector('tbody');
      tbody.textContent = '';
      data.items.forEach(function (d) {
        var tr = el('tr');
        tr.appendChild(el('td', null, String(d.id)));
        tr.appendChild(el('td', null, d.name_cn));
        tr.appendChild(el('td', null, d.dosage_form || '—'));
        tr.appendChild(el('td', null, d.ingredients || '—'));
        tr.appendChild(el('td', null, d.pinyin || '—'));
        var actions = el('td', 'col-actions');
        var btn = el('button', 'btn btn-ghost btn-sm', '详情');
        btn.type = 'button';
        btn.addEventListener('click', function () { openDrug(d.id); });
        actions.appendChild(btn);
        tr.appendChild(actions);
        tbody.appendChild(tr);
      });
      renderPager($('drug-pager'), data, loadDrugs);
    }).catch(function (e) { alert('加载药品失败：' + e.message); });
  }

  function openDrug(id) {
    api('/admin/drugs/' + id).then(function (d) {
      var box = $('drug-detail');
      box.hidden = false;
      box.textContent = '';
      var head = el('div', 'editor-head');
      var h = el('h3', null, d.name_cn + (d.dosage_form ? '（' + d.dosage_form + '）' : ''));
      head.appendChild(h);
      var close = el('button', 'btn btn-ghost btn-sm', '关闭');
      close.type = 'button';
      close.addEventListener('click', function () { box.hidden = true; });
      head.appendChild(close);
      box.appendChild(head);

      function row(label, value) {
        var line = el('p');
        line.appendChild(el('strong', null, label + '：'));
        line.appendChild(el('span', null, value == null ? '—' : String(value)));
        box.appendChild(line);
      }
      row('ID', d.id);
      row('别名', (d.aliases || []).join('、') || '—');
      row('商品名', (d.trade_names || []).join('、') || '—');
      row('拼音', d.pinyin || '—');
      row('拼音缩写', d.pinyin_abbr || '—');
      row('OTC', d.is_otc ? '是' : '否');
      row('中成药', d.is_tcm ? '是' : '否');
      row('批准文号', d.approval_no || '—');
      var ing = box.appendChild(el('div', 'sources-box'));
      ing.appendChild(el('h4', 'subhead', '成分'));
      (d.ingredients || []).forEach(function (i) {
        var item = el('div', 'source-item');
        item.appendChild(el('p', null,
          i.name_cn + (i.strength ? ' ' + i.strength : '') +
          (i.category ? '（' + i.category + '）' : '')));
        ing.appendChild(item);
      });
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }).catch(function (e) { alert('加载详情失败：' + e.message); });
  }

  function runMatchTest() {
    var raw = $('test-input').value;
    var drugs = raw.split(/[、,，;；\n]+/).map(function (s) { return s.trim(); }).filter(Boolean);
    if (!drugs.length) return;
    var box = $('test-result');
    box.textContent = '';
    box.appendChild(el('p', 'chip-note', '识别中…'));
    api('/normalize', { drugs: drugs }).then(function (data) {
      box.textContent = '';
      (data.results || []).forEach(function (r) {
        var chip = el('div', 'chip');
        var top = el('div', 'chip-top');
        if (r.matched) {
          chip.classList.add('chip-ok');
          top.appendChild(el('span', 'chip-icon', r.matched.needs_confirmation ? '⚠' : '✓'));
          top.appendChild(el('span', 'chip-raw', r.query));
          top.appendChild(el('span', 'chip-arrow', '→'));
          var name = el('span', 'chip-name', r.matched.name_cn);
          name.appendChild(el('em', 'chip-conf',
            ' ' + Math.round(r.matched.confidence * 100) + '% · ' + r.matched.match_type));
          top.appendChild(name);
        } else {
          chip.classList.add('chip-error');
          top.appendChild(el('span', 'chip-icon', '✕'));
          top.appendChild(el('span', 'chip-raw', r.query));
          top.appendChild(el('span', 'chip-note', '未识别'));
        }
        chip.appendChild(top);
        if (!r.matched && r.candidates && r.candidates.length) {
          var note = el('p', 'chip-note', '候选：');
          chip.appendChild(note);
          var cands = el('div', 'candidates');
          r.candidates.forEach(function (c) {
            cands.appendChild(el('span', 'cand-btn',
              c.name_cn + ' ' + Math.round(c.confidence * 100) + '%'));
          });
          chip.appendChild(cands);
        }
        box.appendChild(chip);
      });
    }).catch(function (e) {
      box.textContent = '';
      box.appendChild(el('p', 'chip-note is-err', '测试失败：' + e.message));
    });
  }

  // ════════════════════════════════════════
  //  评估记录
  // ════════════════════════════════════════
  var logsPage = 1;

  function loadLogs(page) {
    logsPage = page || 1;
    api('/admin/logs?page=' + logsPage + '&page_size=20').then(function (data) {
      var tbody = $('log-table').querySelector('tbody');
      tbody.textContent = '';
      data.items.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', null, (r.created_at || '').replace('T', ' ').slice(0, 19)));

        var drugs = Array.isArray(r.drugs_raw) ? r.drugs_raw.join('、') : (r.drugs_raw || '');
        tr.appendChild(el('td', null, drugs));

        var lv = LEVELS[r.overall_risk] || LEVELS.none;
        var risk = el('td');
        risk.appendChild(el('span', 'risk-tag ' + lv.cls,
          (r.overall_risk ? lv.icon + ' ' + lv.cn : '—')));
        tr.appendChild(risk);

        tr.appendChild(el('td', null, r.hard_blocked ? '是' : '否'));
        tr.appendChild(el('td', null, r.elapsed_ms != null ? r.elapsed_ms + 'ms' : '—'));
        tr.appendChild(el('td', null,
          r.used_llm == null ? '—' : (r.used_llm ? '是' : '否')));

        var actions = el('td', 'col-actions');
        var btn = el('button', 'btn btn-ghost btn-sm', '详情');
        btn.type = 'button';
        btn.addEventListener('click', function () { openLog(r.id); });
        actions.appendChild(btn);
        tr.appendChild(actions);
        tbody.appendChild(tr);
      });
      renderPager($('log-pager'), data, loadLogs);
    }).catch(function (e) { alert('加载日志失败：' + e.message); });
  }

  function openLog(id) {
    api('/admin/logs/' + id).then(function (r) {
      var box = $('log-detail');
      box.hidden = false;
      box.textContent = '';
      var head = el('div', 'editor-head');
      head.appendChild(el('h3', null, '日志 #' + r.id));
      var close = el('button', 'btn btn-ghost btn-sm', '关闭');
      close.type = 'button';
      close.addEventListener('click', function () { box.hidden = true; });
      head.appendChild(close);
      box.appendChild(head);

      function row(label, value) {
        var line = el('p');
        line.appendChild(el('strong', null, label + '：'));
        line.appendChild(el('span', null, value == null ? '—' : String(value)));
        box.appendChild(line);
      }
      row('时间', r.created_at);
      row('总体风险', r.overall_risk || '—');
      row('硬拦截', r.hard_blocked ? '是' : '否');
      row('请求用了 LLM 开关', r.use_llm ? '开' : '关');
      row('实际调用 LLM', r.used_llm == null ? '—' : (r.used_llm ? '是' : '否'));
      row('降级原因', r.degraded_reason || '—');
      row('耗时', r.elapsed_ms != null ? r.elapsed_ms + 'ms' : '—');
      row('错误', r.error || '无');

      function jsonBlock(label, val) {
        box.appendChild(el('p', null, label));
        var pre = el('pre', 'code-block');
        pre.textContent = JSON.stringify(val, null, 2);
        box.appendChild(pre);
      }
      jsonBlock('输入药品', r.drugs_raw);
      jsonBlock('匹配结果', r.drugs_matched);
      jsonBlock('患者画像（白名单）', r.profile_summary);
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }).catch(function (e) { alert('加载详情失败：' + e.message); });
  }

  // ════════════════════════════════════════
  //  配置
  // ════════════════════════════════════════
  var configData = null;

  function notice(msg) {
    var box = $('config-notice');
    if (msg) { $('config-notice-msg').textContent = msg; box.hidden = false; }
    else { box.hidden = true; }
  }

  function loadConfig() {
    api('/admin/config').then(function (d) {
      configData = d;
      var sel = $('cfg-provider');
      sel.textContent = '';
      (d.providers || []).forEach(function (p) {
        var opt = el('option', null, p);
        opt.value = p;
        if (p === d.llm.provider) opt.selected = true;
        sel.appendChild(opt);
      });
      $('cfg-model').value = d.llm.model || '';
      $('cfg-max-tokens').value = d.llm.max_tokens;
      $('cfg-timeout').value = d.llm.timeout;
      $('cfg-thinking').checked = !!d.llm.thinking;
      $('cfg-api-key').value = '';
      $('cfg-api-key').disabled = false;
      $('cfg-api-key-clear').checked = false;
      $('cfg-api-key-hint').textContent = d.llm.api_key_set
        ? '已配置（留空 = 保持不变）' : '未配置（填入以启用大模型）';
      $('cfg-port').value = d.api.port;
      $('cfg-debug').checked = !!d.api.debug;

      var meta = ['配置来源：' + d.config_source];
      if (d.env_overrides && d.env_overrides.length) {
        meta.push('⚠ 环境变量覆盖：' + d.env_overrides.join(', ') +
          '（环境变量优先级更高，写文件不会生效）');
      }
      if (d.restart_required_keys && d.restart_required_keys.length) {
        meta.push('改以下项需重启服务：' + d.restart_required_keys.join(', '));
      }
      $('cfg-meta').textContent = meta.join('；');
      notice('');
      setConfigMsg('');
    }).catch(function (e) { notice('加载配置失败：' + e.message); });
  }

  function setConfigMsg(text, ok) {
    var box = $('config-msg');
    box.textContent = text || '';
    box.className = 'editor-msg' + (text ? (ok ? ' is-ok' : ' is-err') : '');
  }

  function saveConfig(ev) {
    ev.preventDefault();
    if (!configData) return;
    var payload = {
      llm: {
        provider: $('cfg-provider').value,
        model: $('cfg-model').value.trim(),
        max_tokens: parseInt($('cfg-max-tokens').value, 10) || 2048,
        timeout: parseInt($('cfg-timeout').value, 10) || 60,
        thinking: $('cfg-thinking').checked
      },
      api: {
        port: parseInt($('cfg-port').value, 10) || 5000,
        debug: $('cfg-debug').checked
      }
    };
    if ($('cfg-api-key-clear').checked) {
      payload.llm.api_key = '__CLEAR__';
    } else if ($('cfg-api-key').value) {
      payload.llm.api_key = $('cfg-api-key').value;
    }
    // base_url/model 留空时服务端会用 provider 预设填充
    if (!$('cfg-model').value.trim()) payload.llm.base_url = '';

    api('/admin/config', payload, 'PUT').then(function (r) {
      var msg = '已保存';
      if (r.restart_required && r.restart_required.length) {
        msg += '。需重启生效：' + r.restart_required.join(', ');
      }
      if (r.llm_hot_swapped) msg += '；LLM 配置已热替换';
      setConfigMsg(msg, true);
      loadConfig();
    }).catch(function (e) { setConfigMsg('保存失败：' + e.message, false); });
  }

  // ── 主题（与 app.js 相同）──────────────────
  function initTheme() {
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
  }

  // ── 初始化 ────────────────────────────────
  function init() {
    initTheme();

    $('login-btn').addEventListener('click', function () {
      tryLogin($('login-token').value.trim());
    });
    $('login-token').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); $('login-btn').click(); }
    });
    $('logout-btn').addEventListener('click', function () { showLogin(''); });

    document.querySelectorAll('.tab').forEach(function (btn) {
      btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
    });

    $('rule-search').addEventListener('click', function () { loadRules(1); });
    $('rule-q').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') loadRules(1);
    });
    $('rule-editor-close').addEventListener('click', function () {
      $('rule-editor').hidden = true; currentRule = null;
    });
    $('rule-save').addEventListener('click', saveRule);
    $('rule-delete').addEventListener('click', deleteRule);

    $('drug-search').addEventListener('click', function () { loadDrugs(1); });
    $('drug-q').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') loadDrugs(1);
    });
    $('test-run').addEventListener('click', runMatchTest);

    $('config-form').addEventListener('submit', saveConfig);

    // 有令牌就直接进壳子，验证失败会退回登录页
    if (getToken()) {
      api('/admin/config').then(function () {
        showShell();
        switchTab((location.hash || '#rules').slice(1) || 'rules');
      }).catch(function () {
        showLogin('登录已过期，请重新输入令牌');
      });
    } else {
      showLogin('');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
