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
    if (!token) { showLogin('请输入访问令牌'); return Promise.resolve(false); }
    var btn = $('login-btn');
    btn.disabled = true;
    btn.textContent = '登录中…';
    function done() { btn.disabled = false; btn.textContent = '登录'; }
    return fetch(API + '/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token })
    }).then(function (res) {
      if (res.ok) {
        setToken(token);
        showShell();
        // 必须进默认标签页并拉数据，否则壳子是空的，看起来像“没登上”
        switchTab((location.hash || '#overview').slice(1) || 'overview');
        return true;
      }
      return res.json().catch(function () { return {}; }).then(function (d) {
        showLogin(d.error || '登录失败');
        return false;
      });
    }).catch(function () {
      showLogin('无法连接后端');
      return false;
    }).then(function (r) { done(); return r; });
  }

  // ── 表格状态行：加载中 / 空 / 出错 ──────────
  function tbodyState(tbody, colSpan, msg, kind) {
    tbody.textContent = '';
    var tr = el('tr');
    var cls = 'is-state' + (kind ? ' is-' + kind : '');
    var td = el('td', cls, msg);
    td.colSpan = colSpan;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  // ── 轻提示（替代 alert：不阻塞、可堆叠、textContent 渲染）──
  var toastTimer = null;
  function toast(msg, ok) {
    var box = $('toast');
    if (!box) return;
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
    if (!msg) { box.hidden = true; return; }
    box.textContent = msg;
    box.className = 'toast' + (ok ? ' is-ok' : ' is-err');
    box.hidden = false;
    toastTimer = setTimeout(function () { box.hidden = true; }, 3600);
  }

  // ── 提交中防重复点击：按钮置灰换文案，结束（无论成败）恢复 ──
  function busy(btn, busyLabel, fn) {
    if (!btn || btn.disabled) return Promise.resolve();
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = busyLabel;
    return fn().finally(function () {
      btn.disabled = false;
      btn.textContent = old;
    });
  }

  // ── 弹窗管理：编辑/核对/详情面板居中浮层 ──────
  // 统一走 showModal/hideModal，蒙层与滚动锁由这里维护，
  // 各处业务代码只管"开哪个/关哪个"。
  var MODAL_IDS = ['rule-editor', 'drug-editor', 'drug-detail',
                   'import-editor', 'log-detail'];

  function showModal(id) {
    $(id).hidden = false;
    $('modal-backdrop').hidden = false;
    document.body.classList.add('has-modal');
  }

  function hideModal(id) {
    $(id).hidden = true;
    var anyOpen = MODAL_IDS.some(function (i) { return !$(i).hidden; });
    if (!anyOpen) {
      $('modal-backdrop').hidden = true;
      document.body.classList.remove('has-modal');
    }
  }

  // Esc / 点蒙层统一入口：关掉所有面板并复位编辑态
  function closeTopLayer() {
    MODAL_IDS.forEach(function (i) { $(i).hidden = true; });
    $('modal-backdrop').hidden = true;
    document.body.classList.remove('has-modal');
    currentRule = null;
    creating = false;
    currentDraft = null;
  }

  // 整行可点（按钮/表单控件点击不触发）——扩大热区，少一次精确点按钮
  function clickable(tr, open) {
    tr.classList.add('is-clickable');
    tr.tabIndex = 0;
    tr.addEventListener('click', function (ev) {
      if (ev.target && ev.target.closest &&
          ev.target.closest('button, input, select, textarea, a')) return;
      open();
    });
    tr.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); }
    });
  }

  // ── 标签页 ────────────────────────────────
  var TABS = ['overview', 'rules', 'drugs', 'import', 'logs', 'config'];

  function switchTab(name) {
    // hash 里可能残留旧标签名（如早期书签），兜底回总览，避免页面全空
    if (TABS.indexOf(name) < 0) name = 'overview';
    TABS.forEach(function (t) {
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
    if (name === 'overview') loadOverview();
    if (name === 'rules') loadRules();
    if (name === 'drugs') loadDrugs();
    if (name === 'import') loadImportDrafts();
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
  //  总览
  // ════════════════════════════════════════
  function ovCard(label, value, sub) {
    var card = el('div', 'stat-card');
    card.appendChild(el('span', 'stat-label', label));
    card.appendChild(el('span', 'stat-value', String(value)));
    if (sub) card.appendChild(el('span', 'stat-sub', sub));
    return card;
  }

  function ovBars(box, entries, total) {
    box.textContent = '';
    entries.forEach(function (e) {
      var pct = total ? Math.round(e.n / total * 100) : 0;
      var row = el('div', 'bar-row');
      row.appendChild(el('span', 'bar-label', e.cn));
      var track = el('div', 'bar-track');
      var fill = el('div', 'bar-fill ' + (e.cls || ''));
      fill.style.width = pct + '%';
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('span', 'bar-num', e.n + ' 条 · ' + pct + '%'));
      box.appendChild(row);
    });
  }

  function loadOverview() {
    var cards = $('ov-cards');
    cards.textContent = '';
    cards.appendChild(ovCard('加载中…', '', ''));
    api('/admin/stats').then(function (d) {
      var r = d.rules;
      var box = $('ov-cards');
      box.textContent = '';
      box.appendChild(ovCard('规则总数', r.total,
        r.by_status.draft + ' 条草稿待审'));
      box.appendChild(ovCard('溯源覆盖',
        Math.round(r.source_rate * 100) + '%',
        r.with_source + ' / ' + r.total + ' 条'));
      box.appendChild(ovCard('药品', d.drugs, d.ingredients + ' 个成分'));
      box.appendChild(ovCard('人群禁忌', d.population, ''));
      box.appendChild(ovCard('来源库', d.sources, ''));
      box.appendChild(ovCard('评估记录', d.logs.total,
        '24h 内 ' + d.logs.recent_24h + ' 次'));
      box.appendChild(ovCard('待确认草稿',
        (d.imports && d.imports.pending) || 0, '智能导入'));

      ovBars($('ov-status-bars'), [
        { cn: '草稿', n: r.by_status.draft, cls: 'is-draft' },
        { cn: '已审核', n: r.by_status.reviewed, cls: 'is-reviewed' },
        { cn: '已发布', n: r.by_status.published, cls: 'is-published' }
      ], r.total);
      ovBars($('ov-sev-bars'), [
        { cn: '禁忌', n: r.by_severity.contraindicated, cls: 'is-contraindicated' },
        { cn: '慎用', n: r.by_severity.caution, cls: 'is-caution' },
        { cn: '关注', n: r.by_severity.monitor, cls: 'is-monitor' }
      ], r.total);

      var sys = $('ov-sys');
      sys.textContent = '';
      function row(label, value) {
        var line = el('p');
        line.appendChild(el('strong', null, label));
        line.appendChild(el('span', null, String(value)));
        sys.appendChild(line);
      }
      var s = d.service;
      row('大模型', s.llm_configured
        ? ('已配置 · ' + [s.llm_provider, s.llm_model].filter(Boolean).join(' · '))
        : '未配置（评估自动走规则降级）');
      row('检索 RAG', s.rag_ready ? '就绪' : '不可用');
      row('嵌入后端', s.embed_backend +
        (s.embed_backend === 'hash' ? '（字面匹配，非语义检索）' : ''));
      row('引擎已加载', s.engine_rules + ' 条规则 · ' + s.drugs_indexed + ' 个药品');
    }).catch(function (e) {
      var box = $('ov-cards');
      box.textContent = '';
      var card = el('div', 'stat-card is-err');
      card.appendChild(el('span', 'stat-label', '统计加载失败'));
      card.appendChild(el('span', 'stat-sub', e.message));
      box.appendChild(card);
    });
  }

  // ════════════════════════════════════════
  //  规则库
  // ════════════════════════════════════════
  var rulesPage = 1;
  var currentRule = null;   // 编辑态 = 规则对象；新建态 = null 且 creating=true
  var creating = false;
  var ingIndex = [];        // [{id, name_cn}] —— 成分选择器的数据源
  var selectedRules = {};   // {id: true} —— 批量勾选，跨页保留

  function selectedRuleIds() {
    return Object.keys(selectedRules).map(Number);
  }

  function syncBatchBar() {
    var n = selectedRuleIds().length;
    $('rule-batch').hidden = n === 0;
    $('rule-batch-count').textContent = '已选 ' + n + ' 条';
    if (!n) setBatchMsg('');
  }

  function setBatchMsg(text, ok) {
    var box = $('rule-batch-msg');
    box.textContent = text || '';
    box.className = 'editor-msg' + (text ? (ok ? ' is-ok' : ' is-err') : '');
  }

  function loadRules(page) {
    rulesPage = page || 1;
    var tbody = $('rule-table').querySelector('tbody');
    tbodyState(tbody, 8, '加载中…', 'loading');
    $('rule-pager').textContent = '';
    var params = new URLSearchParams({
      q: $('rule-q').value.trim(),
      severity: $('rule-severity').value,
      status: $('rule-status').value,
      page: rulesPage,
      page_size: 20
    });
    api('/admin/rules?' + params).then(function (data) {
      // 删除/筛选后当前页可能超出范围（如只剩 3 页却停在第 5 页）
      if (data.total && rulesPage > data.pages) { loadRules(data.pages); return; }
      tbody.textContent = '';
      if (!data.items.length) {
        tbodyState(tbody, 8, '没有符合条件的规则');
        renderPager($('rule-pager'), data, loadRules);
        syncBatchBar();
        return;
      }
      data.items.forEach(function (r) {
        var tr = el('tr');
        var checkTd = el('td', 'col-check');
        var cb = el('input');
        cb.type = 'checkbox';
        cb.className = 'rule-check';
        cb.value = String(r.id);
        cb.checked = !!selectedRules[r.id];
        cb.setAttribute('aria-label', '选择规则 #' + r.id);
        cb.addEventListener('change', function () {
          if (cb.checked) selectedRules[r.id] = true;
          else delete selectedRules[r.id];
          syncBatchBar();
        });
        checkTd.appendChild(cb);
        tr.appendChild(checkTd);
        tr.appendChild(el('td', 'col-id', String(r.id)));
        var pair = el('td');
        pair.appendChild(el('span', 'pair-name', r.ing_a_name));
        pair.appendChild(el('span', 'pair-sep', '+'));
        pair.appendChild(el('span', 'pair-name', r.ing_b_name));
        tr.appendChild(pair);

        var lv = LEVELS[r.severity] || LEVELS.monitor;
        var sev = el('td');
        sev.appendChild(el('span', 'risk-tag ' + lv.cls, lv.icon + ' ' + lv.cn));
        tr.appendChild(sev);

        var st = el('td');
        var stCls = r.status === 'published' ? 'badge-ok'
                  : r.status === 'reviewed'  ? 'badge-accent' : 'badge-muted';
        st.appendChild(el('span', 'badge ' + stCls, STATUS_CN[r.status] || r.status));
        tr.appendChild(st);

        tr.appendChild(el('td', 'col-mono', r.evidence_level || '—'));
        tr.appendChild(el('td', 'col-mono', String(r.source_count)));

        var actions = el('td', 'col-actions');
        var viewBtn = el('button', 'btn btn-ghost btn-sm', '编辑');
        viewBtn.type = 'button';
        viewBtn.addEventListener('click', function () { openRule(r.id); });
        actions.appendChild(viewBtn);
        tr.appendChild(actions);

        (function (rid) { clickable(tr, function () { openRule(rid); }); })(r.id);
        tbody.appendChild(tr);
      });
      renderPager($('rule-pager'), data, loadRules);
      syncBatchBar();
    }).catch(function (e) {
      tbodyState(tbody, 8, '加载失败：' + e.message, 'err');
    });
  }

  function loadIngIndex() {
    // 成分全量拉一次（当前 66 条；上限 500，选择器用 datalist 前端过滤）
    return api('/admin/ingredients?page_size=500').then(function (data) {
      ingIndex = data.items || [];
      var box = $('ing-options');
      box.textContent = '';
      ingIndex.forEach(function (i) {
        var opt = document.createElement('option');
        opt.value = i.name_cn;
        if (i.category) opt.label = i.category;
        box.appendChild(opt);
      });
      return ingIndex;
    });
  }

  function resolveIng(name) {
    var hit = ingIndex.filter(function (i) { return i.name_cn === name; })[0];
    return hit ? hit.id : null;
  }

  function resetRuleForm() {
    $('rule-f-severity').value = 'caution';
    $('rule-f-status').value = 'draft';
    $('rule-f-evidence').value = '';
    $('rule-f-reviewed-by').value = '';
    $('rule-f-mechanism').value = '';
    $('rule-f-consequence').value = '';
    $('rule-f-suggestion').value = '';
    $('rule-pick-a').value = '';
    $('rule-pick-b').value = '';
    $('ing-new-name').value = '';
    $('ing-new-cat').value = '';
    $('ing-new-msg').textContent = '';
  }

  function newRule() {
    creating = true;
    currentRule = null;
    resetRuleForm();
    showModal('rule-editor');
    $('rule-editor-title').textContent = '新建规则';
    $('rule-pair-view').hidden = true;
    $('rule-pair-pick').hidden = false;
    $('rule-delete').hidden = true;
    $('rule-sources').textContent = '';
    setMsg('');
    loadIngIndex().catch(function (e) { setMsg('成分列表加载失败：' + e.message, false); });
    $('rule-pick-a').focus();
    $('rule-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function openRule(id) {
    api('/admin/rules/' + id).then(function (r) {
      creating = false;
      currentRule = r;
      showModal('rule-editor');
      $('rule-editor-title').textContent = '编辑规则 #' + r.id;
      $('rule-pair-view').hidden = false;
      $('rule-pair-pick').hidden = true;
      $('rule-delete').hidden = false;
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
    }).catch(function (e) { toast('加载规则失败：' + e.message); });
  }

  function setMsg(text, ok) {
    var box = $('rule-editor-msg');
    box.textContent = text || '';
    box.className = 'editor-msg' + (text ? (ok ? ' is-ok' : ' is-err') : '');
  }

  function saveRule() {
    var payload = {
      severity: $('rule-f-severity').value,
      status: $('rule-f-status').value,
      evidence_level: $('rule-f-evidence').value || null,
      reviewed_by: $('rule-f-reviewed-by').value.trim() || null,
      mechanism: $('rule-f-mechanism').value.trim() || null,
      consequence: $('rule-f-consequence').value.trim() || null,
      suggestion: $('rule-f-suggestion').value.trim() || null
    };

    if (creating) {
      var na = $('rule-pick-a').value.trim();
      var nb = $('rule-pick-b').value.trim();
      if (!na || !nb) { setMsg('请填写两个成分', false); return; }
      if (na === nb) { setMsg('成分对不能相同', false); return; }
      var ida = resolveIng(na);
      var idb = resolveIng(nb);
      if (!ida || !idb) {
        setMsg('成分「' + (!ida ? na : nb) + '」不在库中，请先用下方「快速新建成分」创建', false);
        return;
      }
      payload.ing_a_id = ida;
      payload.ing_b_id = idb;
      busy($('rule-save'), '保存中…', function () {
        return api('/admin/rules', payload, 'POST')
          .then(function (d) {
            creating = false;
            loadRules(1);
            openRule(d.id);
            setMsg('已创建并重载规则索引', true);
          });
      }).catch(function (e) { setMsg('创建失败：' + e.message, false); });
      return;
    }

    if (!currentRule) return;
    busy($('rule-save'), '保存中…', function () {
      return api('/admin/rules/' + currentRule.id, payload, 'PUT')
        .then(function () {
          setMsg('已保存并重载规则索引', true);
          loadRules(rulesPage);
        });
    }).catch(function (e) { setMsg('保存失败：' + e.message, false); });
  }

  function createIngredient() {
    var name = $('ing-new-name').value.trim();
    var msg = $('ing-new-msg');
    if (!name) { msg.textContent = '请填成分名'; msg.className = 'editor-msg is-err'; return; }
    api('/admin/ingredients', {
      name_cn: name,
      category: $('ing-new-cat').value.trim() || null
    }, 'POST').then(function (d) {
      msg.textContent = '已创建「' + d.name_cn + '」';
      msg.className = 'editor-msg is-ok';
      $('ing-new-name').value = '';
      $('ing-new-cat').value = '';
      return loadIngIndex();
    }).then(function () {
      // 自动填进还空着的那侧，少一次手动搜索
      if (!$('rule-pick-a').value) $('rule-pick-a').value = name;
      else if (!$('rule-pick-b').value) $('rule-pick-b').value = name;
    }).catch(function (e) {
      msg.textContent = '创建失败：' + e.message;
      msg.className = 'editor-msg is-err';
    });
  }

  function deleteRule() {
    if (!currentRule || creating) return;
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
        hideModal('rule-editor');
        currentRule = null;
        creating = false;
        loadRules(rulesPage);
      })
      .catch(function (e) { setMsg('删除失败：' + e.message, false); });
  }

  // ── 批量操作（审核流主入口）──────────────────
  function batchSetStatus() {
    var status = $('rule-batch-status').value;
    var ids = selectedRuleIds();
    if (!ids.length) return;
    if (!status) { setBatchMsg('请先选择目标状态', false); return; }
    busy($('rule-batch-apply'), '处理中…', function () {
      return api('/admin/rules/batch', {
        ids: ids, action: 'set_status', status: status
      }).then(function (d) {
        selectedRules = {};
        toast('已将 ' + d.affected + ' 条规则改为「' +
              (STATUS_CN[status] || status) + '」', true);
        loadRules(rulesPage);
      });
    }).catch(function (e) { setBatchMsg(e.message, false); });
  }

  function batchDelete() {
    var btn = $('rule-batch-del');
    // 两段式确认，防误触 —— 与单条删除同一套交互
    if (btn.dataset.confirm !== '1') {
      btn.dataset.confirm = '1';
      btn.textContent = '确认删除选中？';
      setTimeout(function () {
        btn.dataset.confirm = '';
        btn.textContent = '批量删除';
      }, 4000);
      return;
    }
    btn.dataset.confirm = '';
    btn.textContent = '批量删除';
    var ids = selectedRuleIds();
    if (!ids.length) return;
    busy(btn, '删除中…', function () {
      return api('/admin/rules/batch', { ids: ids, action: 'delete' })
        .then(function (d) {
          selectedRules = {};
          toast('已删除 ' + d.affected + ' 条规则', true);
          loadRules(rulesPage);
        });
    }).catch(function (e) { setBatchMsg(e.message, false); });
  }

  // ════════════════════════════════════════
  //  药品数据
  // ════════════════════════════════════════
  var drugsPage = 1;

  function loadDrugs(page) {
    drugsPage = page || 1;
    var tbody = $('drug-table').querySelector('tbody');
    tbodyState(tbody, 6, '加载中…', 'loading');
    $('drug-pager').textContent = '';
    var params = new URLSearchParams({ q: $('drug-q').value.trim(), page: drugsPage, page_size: 20 });
    api('/admin/drugs?' + params).then(function (data) {
      tbody.textContent = '';
      if (!data.items.length) {
        tbodyState(tbody, 6, '没有符合条件的药品');
        renderPager($('drug-pager'), data, loadDrugs);
        return;
      }
      data.items.forEach(function (d) {
        var tr = el('tr');
        tr.appendChild(el('td', 'col-id', String(d.id)));
        tr.appendChild(el('td', null, d.name_cn));
        tr.appendChild(el('td', null, d.dosage_form || '—'));
        tr.appendChild(el('td', null, d.ingredients || '—'));
        tr.appendChild(el('td', 'col-mono', d.pinyin || '—'));
        var actions = el('td', 'col-actions');
        var btn = el('button', 'btn btn-ghost btn-sm', '详情');
        btn.type = 'button';
        btn.addEventListener('click', function () { openDrug(d.id); });
        actions.appendChild(btn);
        tr.appendChild(actions);
        (function (did) { clickable(tr, function () { openDrug(did); }); })(d.id);
        tbody.appendChild(tr);
      });
      renderPager($('drug-pager'), data, loadDrugs);
    }).catch(function (e) {
      tbodyState(tbody, 6, '加载失败：' + e.message, 'err');
    });
  }

  function openDrug(id) {
    api('/admin/drugs/' + id).then(function (d) {
      var box = $('drug-detail');
      hideModal('drug-editor');   // 详情与新建面板互斥，别叠两层弹窗
      showModal('drug-detail');
      box.textContent = '';
      var head = el('div', 'editor-head');
      var h = el('h3', null, d.name_cn + (d.dosage_form ? '（' + d.dosage_form + '）' : ''));
      head.appendChild(h);
      var close = el('button', 'btn btn-ghost btn-sm', '关闭');
      close.type = 'button';
      close.addEventListener('click', function () { hideModal('drug-detail'); });
      head.appendChild(close);
      box.appendChild(head);

      var rows = box.appendChild(el('div', 'detail-rows'));
      function row(label, value) {
        var line = el('p');
        line.appendChild(el('strong', null, label));
        line.appendChild(el('span', null, value == null ? '—' : String(value)));
        rows.appendChild(line);
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
    }).catch(function (e) { toast('加载详情失败：' + e.message); });
  }

  // ── 新建药品：动态成分行（每行 = 名称 + 规格 + 删除）────
  function addIngRow(name, strength) {
    var row = el('div', 'ing-row');
    var nameInput = el('input');
    nameInput.type = 'text';
    nameInput.className = 'ing-name';
    nameInput.setAttribute('list', 'ing-options');
    nameInput.placeholder = '成分名，如 阿莫西林';
    nameInput.value = name || '';
    var strInput = el('input');
    strInput.type = 'text';
    strInput.className = 'ing-strength';
    strInput.placeholder = '规格（选填）';
    strInput.maxLength = 50;
    strInput.value = strength || '';
    var rm = el('button', 'btn btn-ghost btn-sm ing-rm', '移除');
    rm.type = 'button';
    rm.addEventListener('click', function () {
      row.remove();
      if (!$('drug-ing-rows').children.length) addIngRow();
    });
    row.appendChild(nameInput);
    row.appendChild(strInput);
    row.appendChild(rm);
    $('drug-ing-rows').appendChild(row);
  }

  function setDrugMsg(text, ok) {
    var box = $('drug-editor-msg');
    box.textContent = text || '';
    box.className = 'editor-msg' + (text ? (ok ? ' is-ok' : ' is-err') : '');
  }

  function newDrug() {
    showModal('drug-editor');
    hideModal('drug-detail');
    $('drug-f-name').value = '';
    $('drug-f-form').value = '';
    $('drug-f-alias').value = '';
    $('drug-f-trade').value = '';
    $('drug-f-otc').checked = false;
    $('drug-f-tcm').checked = false;
    $('drug-ing-rows').textContent = '';
    addIngRow();
    setDrugMsg('');
    loadIngIndex().catch(function (e) { setDrugMsg('成分列表加载失败：' + e.message, false); });
    $('drug-f-name').focus();
    $('drug-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function saveDrug() {
    var name = $('drug-f-name').value.trim();
    if (!name) { setDrugMsg('请填通用名', false); return; }

    var ings = [];
    Array.prototype.forEach.call($('drug-ing-rows').children, function (row) {
      var n = row.querySelector('.ing-name').value.trim();
      if (!n) return;
      var s = row.querySelector('.ing-strength').value.trim();
      var id = resolveIng(n);
      var item = id ? { id: id } : { name: n };   // 没有的名字交给服务端自动创建
      if (s) item.strength = s;
      ings.push(item);
    });
    if (!ings.length) { setDrugMsg('至少关联一个成分', false); return; }

    var payload = {
      name_cn: name,
      dosage_form: $('drug-f-form').value.trim() || null,
      aliases: $('drug-f-alias').value.trim() || null,
      trade_names: $('drug-f-trade').value.trim() || null,
      is_otc: $('drug-f-otc').checked,
      is_tcm: $('drug-f-tcm').checked,
      ingredients: ings
    };
    busy($('drug-save'), '保存中…', function () {
      return api('/admin/drugs', payload, 'POST')
        .then(function (d) {
          setDrugMsg('已创建（id=' + d.id + '），归一化索引已重载', true);
          loadDrugs(1);
          loadIngIndex().catch(function () { /* 列表刷新失败不影响主流程 */ });
          // 留在面板方便继续建下一条
          $('drug-f-name').value = '';
          $('drug-ing-rows').textContent = '';
          addIngRow();
          $('drug-f-name').focus();
        });
    }).catch(function (e) { setDrugMsg('创建失败：' + e.message, false); });
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
  //  智能导入（LLM 解析 → 待确认 → 确认入库）
  // ════════════════════════════════════════
  var importPage = 1;
  var currentDraft = null;    // 正在核对的草稿对象
  var importSourceName = '';  // 上传文件名 → parse 的 source_name

  function setImportParseMsg(text, ok) {
    var box = $('import-parse-msg');
    box.textContent = text || '';
    box.className = 'editor-msg' + (text ? (ok ? ' is-ok' : ' is-err') : '');
  }

  function setImpMsg(text, ok) {
    var box = $('import-editor-msg');
    box.textContent = text || '';
    box.className = 'editor-msg' + (text ? (ok ? ' is-ok' : ' is-err') : '');
  }

  function loadImportDrafts(page) {
    importPage = page || 1;
    var tbody = $('import-table').querySelector('tbody');
    tbodyState(tbody, 6, '加载中…', 'loading');
    $('import-pager').textContent = '';
    var params = new URLSearchParams({
      status: $('import-status').value,
      page: importPage,
      page_size: 20
    });
    api('/admin/import/drafts?' + params).then(function (data) {
      if (data.total && importPage > data.pages) { loadImportDrafts(data.pages); return; }
      tbody.textContent = '';
      if (!data.items.length) {
        var empty = $('import-status').value === 'confirmed'
          ? '暂无已入库记录'
          : '暂无待确认草稿 —— 上方粘贴文本解析即可';
        tbodyState(tbody, 6, empty);
        renderPager($('import-pager'), data, loadImportDrafts);
        return;
      }
      data.items.forEach(function (d) {
        var tr = el('tr');
        tr.appendChild(el('td', 'col-id', String(d.id)));
        tr.appendChild(el('td', null, d.kind_cn || d.kind || '—'));

        var f = d.fields || {};
        var summary;
        if (f.ing_a_name) {
          summary = f.ing_a_name + ' + ' + (f.ing_b_name || '?');
        } else if (f.name_cn) {
          summary = f.name_cn + (f.dosage_form ? '（' + f.dosage_form + '）' : '');
        } else {
          summary = d.raw_span || '';
        }
        if (summary.length > 60) summary = summary.slice(0, 60) + '…';
        tr.appendChild(el('td', null, summary));

        var warns = d.warnings || [];
        var warnTd = el('td');
        warnTd.appendChild(el('span',
          'badge ' + (warns.length ? 'badge-warn' : 'badge-muted'),
          warns.length ? warns.length + ' 条提示' : '—'));
        tr.appendChild(warnTd);

        var st = el('td');
        st.appendChild(el('span',
          'badge ' + (d.status === 'confirmed' ? 'badge-ok' : 'badge-accent'),
          d.status === 'confirmed' ? '已入库' : '待确认'));
        tr.appendChild(st);

        var actions = el('td', 'col-actions');
        var btn = el('button', 'btn btn-ghost btn-sm',
          d.status === 'confirmed' ? '查看' : '核对');
        btn.type = 'button';
        (function (id) {
          btn.addEventListener('click', function () { openImportDraft(id); });
        })(d.id);
        actions.appendChild(btn);
        tr.appendChild(actions);
        (function (id) {
          clickable(tr, function () { openImportDraft(id); });
        })(d.id);
        tbody.appendChild(tr);
      });
      renderPager($('import-pager'), data, loadImportDrafts);
    }).catch(function (e) {
      tbodyState(tbody, 6, '加载失败：' + e.message, 'err');
    });
  }

  function onImportFile(ev) {
    var file = ev.target.files && ev.target.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result || '');
      $('import-text').value = text;
      importSourceName = file.name;
      $('import-file-name').textContent = file.name + '（' + text.length + ' 字）';
      if (text.length > 8000) {
        setImportParseMsg('文件超过 8000 字，请删减后分批解析', false);
      } else {
        setImportParseMsg('');
      }
    };
    reader.onerror = function () { setImportParseMsg('读取文件失败', false); };
    reader.readAsText(file, 'utf-8');
    ev.target.value = '';   // 清掉，允许再选同一个文件
  }

  function parseImport() {
    var text = $('import-text').value.trim();
    if (!text) { setImportParseMsg('请先粘贴文本', false); return; }
    if (text.length > 8000) {
      setImportParseMsg('文本过长（' + text.length + ' 字），上限 8000 字', false);
      return;
    }
    var box = $('import-parse-result');
    box.textContent = '';
    busy($('import-parse'), '解析中…（大模型通常需要几秒）', function () {
      var payload = { text: text };
      if (importSourceName) payload.source_name = importSourceName;
      return api('/admin/import/parse', payload).then(function (d) {
        setImportParseMsg(
          '解析完成：新增 ' + d.drafts.length + ' 条待确认' +
          (d.skipped.length ? '，跳过 ' + d.skipped.length + ' 条' : ''),
          true
        );
        d.drafts.forEach(function (dr) {
          var f = dr.fields || {};
          box.appendChild(el('span', 'chip chip-ok',
            '✓ ' + (f.ing_a_name || '') + ' + ' + (f.ing_b_name || '')));
        });
        d.skipped.forEach(function (s) {
          box.appendChild(el('span', 'chip chip-error',
            '✕ ' + (s.reason || '跳过')));
        });
        loadImportDrafts(1);
      });
    }).catch(function (e) {
      // LLM 未配置/挂了走 502 —— 服务端一行没写，提示后留在本页
      setImportParseMsg(e.message, false);
    });
  }

  function addSrcRow(src) {
    src = src || {};
    var row = el('div', 'src-row');
    var title = el('input', 'src-title');
    title.type = 'text';
    title.maxLength = 200;
    title.placeholder = '出处标题 *，如 华法林钠片说明书';
    title.value = src.title || '';
    var stype = el('input', 'src-type');
    stype.type = 'text';
    stype.maxLength = 100;
    stype.placeholder = '类型，如 说明书';
    stype.value = src.source_type || '';
    var year = el('input', 'src-year');
    year.type = 'number';
    year.min = '1000';
    year.max = '2100';
    year.placeholder = '年份';
    year.value = src.year != null ? String(src.year) : '';
    var rm = el('button', 'btn btn-ghost btn-sm src-rm', '移除');
    rm.type = 'button';
    rm.addEventListener('click', function () { row.remove(); });
    var excerpt = el('textarea', 'src-excerpt');
    excerpt.rows = 2;
    excerpt.maxLength = 2000;
    excerpt.placeholder = '原文摘录（选填，溯源展示用）';
    excerpt.value = src.excerpt || '';
    row.appendChild(title);
    row.appendChild(stype);
    row.appendChild(year);
    row.appendChild(rm);
    row.appendChild(excerpt);
    $('imp-src-rows').appendChild(row);
  }

  // 药品草稿的成分行（与药品页 .ing-row 同款，挂到 #imp-ing-rows）
  function addImpIngRow(name, strength) {
    var row = el('div', 'ing-row');
    var nameInput = el('input', 'ing-name');
    nameInput.type = 'text';
    nameInput.setAttribute('list', 'ing-options');
    nameInput.placeholder = '成分名，如 阿莫西林';
    nameInput.value = name || '';
    var strInput = el('input', 'ing-strength');
    strInput.type = 'text';
    strInput.placeholder = '规格（选填）';
    strInput.maxLength = 50;
    strInput.value = strength || '';
    var rm = el('button', 'btn btn-ghost btn-sm ing-rm', '移除');
    rm.type = 'button';
    rm.addEventListener('click', function () {
      row.remove();
      if (!$('imp-ing-rows').children.length) addImpIngRow();
    });
    row.appendChild(nameInput);
    row.appendChild(strInput);
    row.appendChild(rm);
    $('imp-ing-rows').appendChild(row);
  }

  function openImportDraft(id) {
    api('/admin/import/drafts/' + id).then(function (d) {
      currentDraft = d;
      var f = d.fields || {};
      var confirmed = d.status === 'confirmed';
      var isDrug = d.kind === 'drug';

      showModal('import-editor');
      $('import-editor-title').textContent =
        (d.kind_cn || d.kind || '草稿') + ' · 草稿 #' + d.id +
        (confirmed ? ('（已入库 → #' + d.target_id + '）') : '');

      // 按 kind 切换字段组（两类字段都填，隐藏组的值不参与收集）
      $('import-f-rule').hidden = isDrug;
      $('import-f-drug').hidden = !isDrug;

      // 规则字段
      $('imp-a-name').value = f.ing_a_name || '';
      $('imp-b-name').value = f.ing_b_name || '';
      $('imp-severity').value = f.severity || 'caution';
      $('imp-evidence').value = f.evidence_level || '';
      $('imp-status').value = f.status || 'reviewed';
      $('imp-reviewed-by').value = f.reviewed_by || '';
      $('imp-mechanism').value = f.mechanism || '';
      $('imp-consequence').value = f.consequence || '';
      $('imp-suggestion').value = f.suggestion || '';
      $('imp-src-rows').textContent = '';
      (f.sources || []).forEach(function (s) { addSrcRow(s); });

      // 药品字段
      $('imp-d-name').value = f.name_cn || '';
      $('imp-d-form').value = f.dosage_form || '';
      $('imp-d-trade').value = (f.trade_names || []).join('、');
      $('imp-d-alias').value = (f.aliases || []).join('、');
      $('imp-d-approval').value = f.approval_no || '';
      $('imp-d-otc').checked = !!f.is_otc;
      $('imp-d-tcm').checked = !!f.is_tcm;
      $('imp-ing-rows').textContent = '';
      var ings = f.ingredients || [];
      if (ings.length) {
        ings.forEach(function (it) { addImpIngRow(it.name, it.strength); });
      } else if (isDrug) {
        addImpIngRow();
      }

      var raw = $('imp-raw');
      raw.textContent = d.raw_span ? '原文片段：' + d.raw_span : '';
      raw.hidden = !d.raw_span;

      var wbox = $('imp-warnings');
      wbox.textContent = '';
      var warns = d.warnings || [];
      if (warns.length) {
        wbox.appendChild(el('p', null, '解析提示（' + warns.length + ' 条）：'));
        warns.forEach(function (w) {
          wbox.appendChild(el('p', 'imp-warn-item', '· ' + w));
        });
        wbox.hidden = false;
      } else {
        wbox.hidden = true;
      }

      // 已入库的草稿只读（PUT 服务端也会 409），但保留丢弃做清理
      var editable = !confirmed;
      [$('import-f-rule'), $('import-f-drug')].forEach(function (grp) {
        Array.prototype.forEach.call(
          grp.querySelectorAll('input, select, textarea, button'),
          function (n) { n.disabled = !editable; }
        );
      });
      $('imp-src-add').hidden = !editable;
      $('imp-ing-add').hidden = !editable;
      $('imp-save').hidden = !editable;
      $('imp-confirm').hidden = !editable;
      setImpMsg('');
      $('import-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }).catch(function (e) { toast('加载草稿失败：' + e.message); });
  }

  function collectImportForm() {
    if (currentDraft && currentDraft.kind === 'drug') {
      var ings = [];
      Array.prototype.forEach.call($('imp-ing-rows').children, function (row) {
        var n = row.querySelector('.ing-name').value.trim();
        if (!n) return;
        var s = row.querySelector('.ing-strength').value.trim();
        var item = { name: n };
        if (s) item.strength = s;
        ings.push(item);
      });
      return {
        name_cn: $('imp-d-name').value.trim(),
        dosage_form: $('imp-d-form').value.trim() || null,
        trade_names: $('imp-d-trade').value.trim() || null,
        aliases: $('imp-d-alias').value.trim() || null,
        approval_no: $('imp-d-approval').value.trim() || null,
        is_otc: $('imp-d-otc').checked,
        is_tcm: $('imp-d-tcm').checked,
        ingredients: ings
      };
    }

    var sources = [];
    Array.prototype.forEach.call($('imp-src-rows').children, function (row) {
      var title = row.querySelector('.src-title').value.trim();
      if (!title) return;
      var s = { title: title };
      var stype = row.querySelector('.src-type').value.trim();
      if (stype) s.source_type = stype;
      var year = parseInt(row.querySelector('.src-year').value, 10);
      if (!isNaN(year)) s.year = year;
      var excerpt = row.querySelector('.src-excerpt').value.trim();
      if (excerpt) s.excerpt = excerpt;
      sources.push(s);
    });
    return {
      ing_a_name: $('imp-a-name').value.trim(),
      ing_b_name: $('imp-b-name').value.trim(),
      severity: $('imp-severity').value,
      evidence_level: $('imp-evidence').value || null,
      status: $('imp-status').value,
      reviewed_by: $('imp-reviewed-by').value.trim() || null,
      mechanism: $('imp-mechanism').value.trim() || null,
      consequence: $('imp-consequence').value.trim() || null,
      suggestion: $('imp-suggestion').value.trim() || null,
      sources: sources
    };
  }

  function saveImportDraft() {
    if (!currentDraft) return;
    var fields = collectImportForm();
    busy($('imp-save'), '保存中…', function () {
      return api('/admin/import/drafts/' + currentDraft.id,
                 { fields: fields }, 'PUT').then(function () {
        setImpMsg('已保存（尚未入库）', true);
      });
    }).catch(function (e) { setImpMsg('保存失败：' + e.message, false); });
  }

  function confirmImportDraft() {
    if (!currentDraft) return;
    var kindCn = currentDraft.kind_cn || '条目';
    var fields = collectImportForm();
    var payload = { status: fields.status };
    if (fields.reviewed_by) payload.reviewed_by = fields.reviewed_by;
    busy($('imp-confirm'), '入库中…', function () {
      return api('/admin/import/drafts/' + currentDraft.id + '/confirm',
                 payload).then(function (d) {
        var msg = '已入库（' + kindCn + ' #' + d.target_id + '）';
        if (d.created_ingredients && d.created_ingredients.length) {
          msg += '，新建成分：' + d.created_ingredients.join('、');
        }
        toast(msg, true);
        hideModal('import-editor');
        currentDraft = null;
        loadImportDrafts(importPage);
        loadIngIndex().catch(function () { /* 选择器刷新失败不影响主流程 */ });
      });
    }).catch(function (e) { setImpMsg('入库失败：' + e.message, false); });
  }

  function rejectImportDraft() {
    if (!currentDraft) return;
    var btn = $('imp-reject');
    // 两段式确认，防误触 —— 与删除规则同一套交互
    if (btn.dataset.confirm !== '1') {
      btn.dataset.confirm = '1';
      btn.textContent = '确认丢弃？';
      setTimeout(function () {
        btn.dataset.confirm = '';
        btn.textContent = '丢弃';
      }, 4000);
      return;
    }
    btn.dataset.confirm = '';
    btn.textContent = '丢弃';
    var id = currentDraft.id;
    busy(btn, '丢弃中…', function () {
      return api('/admin/import/drafts/' + id, undefined, 'DELETE')
        .then(function () {
          toast('已丢弃草稿 #' + id, true);
          hideModal('import-editor');
          currentDraft = null;
          loadImportDrafts(importPage);
        });
    }).catch(function (e) { setImpMsg('丢弃失败：' + e.message, false); });
  }

  // ════════════════════════════════════════
  //  评估记录
  // ════════════════════════════════════════
  var logsPage = 1;

  function loadLogs(page) {
    logsPage = page || 1;
    var tbody = $('log-table').querySelector('tbody');
    tbodyState(tbody, 7, '加载中…', 'loading');
    $('log-pager').textContent = '';
    api('/admin/logs?page=' + logsPage + '&page_size=20').then(function (data) {
      tbody.textContent = '';
      if (!data.items.length) {
        tbodyState(tbody, 7, '暂无评估记录');
        renderPager($('log-pager'), data, loadLogs);
        return;
      }
      data.items.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', 'col-mono', (r.created_at || '').replace('T', ' ').slice(0, 19)));

        var drugs = Array.isArray(r.drugs_raw) ? r.drugs_raw.join('、') : (r.drugs_raw || '');
        tr.appendChild(el('td', null, drugs));

        var lv = LEVELS[r.overall_risk] || LEVELS.none;
        var risk = el('td');
        risk.appendChild(el('span', 'risk-tag ' + lv.cls,
          (r.overall_risk ? lv.icon + ' ' + lv.cn : '—')));
        tr.appendChild(risk);

        var hb = el('td');
        hb.appendChild(el('span', 'badge ' + (r.hard_blocked ? 'badge-critical' : 'badge-muted'),
          r.hard_blocked ? '是' : '否'));
        tr.appendChild(hb);

        tr.appendChild(el('td', 'col-mono', r.elapsed_ms != null ? r.elapsed_ms + 'ms' : '—'));
        var llm = el('td');
        llm.appendChild(el('span', 'badge ' +
          (r.used_llm == null ? 'badge-muted' : r.used_llm ? 'badge-ok' : 'badge-muted'),
          r.used_llm == null ? '—' : (r.used_llm ? '是' : '否')));
        tr.appendChild(llm);

        var actions = el('td', 'col-actions');
        var btn = el('button', 'btn btn-ghost btn-sm', '详情');
        btn.type = 'button';
        btn.addEventListener('click', function () { openLog(r.id); });
        actions.appendChild(btn);
        tr.appendChild(actions);
        (function (lid) { clickable(tr, function () { openLog(lid); }); })(r.id);
        tbody.appendChild(tr);
      });
      renderPager($('log-pager'), data, loadLogs);
    }).catch(function (e) {
      tbodyState(tbody, 7, '加载失败：' + e.message, 'err');
    });
  }

  function openLog(id) {
    api('/admin/logs/' + id).then(function (r) {
      var box = $('log-detail');
      showModal('log-detail');
      box.textContent = '';
      var head = el('div', 'editor-head');
      head.appendChild(el('h3', null, '日志 #' + r.id));
      var close = el('button', 'btn btn-ghost btn-sm', '关闭');
      close.type = 'button';
      close.addEventListener('click', function () { hideModal('log-detail'); });
      head.appendChild(close);
      box.appendChild(head);

      var rows = box.appendChild(el('div', 'detail-rows'));
      function row(label, value) {
        var line = el('p');
        line.appendChild(el('strong', null, label));
        line.appendChild(el('span', null, value == null ? '—' : String(value)));
        rows.appendChild(line);
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
    }).catch(function (e) { toast('加载日志失败：' + e.message); });
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

    busy($('config-save'), '保存中…', function () {
      return api('/admin/config', payload, 'PUT').then(function (r) {
        var msg = '已保存';
        if (r.restart_required && r.restart_required.length) {
          msg += '。需重启生效：' + r.restart_required.join(', ');
        }
        if (r.llm_hot_swapped) msg += '；LLM 配置已热替换';
        setConfigMsg(msg, true);
        loadConfig();
      });
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

    $('ov-refresh').addEventListener('click', loadOverview);

    $('rule-search').addEventListener('click', function () { loadRules(1); });
    $('rule-q').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') loadRules(1);
    });
    $('rule-new').addEventListener('click', newRule);
    $('rule-check-all').addEventListener('change', function () {
      var on = this.checked;
      Array.prototype.forEach.call(
        document.querySelectorAll('#rule-table .rule-check'),
        function (cb) {
          cb.checked = on;
          var id = parseInt(cb.value, 10);
          if (on) selectedRules[id] = true;
          else delete selectedRules[id];
        }
      );
      syncBatchBar();
    });
    $('rule-batch-apply').addEventListener('click', batchSetStatus);
    $('rule-batch-del').addEventListener('click', batchDelete);
    $('rule-editor-close').addEventListener('click', function () {
      hideModal('rule-editor'); currentRule = null; creating = false;
    });
    $('rule-save').addEventListener('click', saveRule);
    $('rule-delete').addEventListener('click', deleteRule);
    $('ing-new-save').addEventListener('click', createIngredient);
    $('ing-new-name').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); createIngredient(); }
    });

    $('drug-search').addEventListener('click', function () { loadDrugs(1); });
    $('drug-q').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') loadDrugs(1);
    });
    $('drug-new').addEventListener('click', newDrug);
    $('drug-editor-close').addEventListener('click', function () {
      hideModal('drug-editor');
    });
    $('drug-ing-add').addEventListener('click', function () { addIngRow(); });
    $('drug-save').addEventListener('click', saveDrug);
    $('test-run').addEventListener('click', runMatchTest);

    $('import-search').addEventListener('click', function () { loadImportDrafts(1); });
    $('import-pick').addEventListener('click', function () { $('import-file').click(); });
    $('import-file').addEventListener('change', onImportFile);
    $('import-parse').addEventListener('click', parseImport);
    $('import-editor-close').addEventListener('click', function () {
      hideModal('import-editor');
      currentDraft = null;
    });
    $('imp-save').addEventListener('click', saveImportDraft);
    $('imp-confirm').addEventListener('click', confirmImportDraft);
    $('imp-reject').addEventListener('click', rejectImportDraft);
    $('imp-src-add').addEventListener('click', function () { addSrcRow(); });
    $('imp-ing-add').addEventListener('click', function () { addImpIngRow(); });

    $('config-form').addEventListener('submit', saveConfig);

    // Esc / 点蒙层 = 关闭所有弹窗（与点「关闭」等价）
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      closeTopLayer();
    });
    $('modal-backdrop').addEventListener('click', closeTopLayer);

    // 有令牌就直接进壳子，验证失败会退回登录页
    if (getToken()) {
      api('/admin/config').then(function () {
        showShell();
        switchTab((location.hash || '#overview').slice(1) || 'overview');
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
