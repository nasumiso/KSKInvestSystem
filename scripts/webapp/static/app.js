/* 銘柄詳細ビュー: click-to-edit + 変更検知 + 四季報追加/削除 */

var MAX_SHIKIHO = 8;

/* --- 決算コメント履歴テーブル: 行クリックで「見通し・反応」セルの折返し表示をトグル (issue #131) --- */
/* デフォルトは1行ellipsis表示。クリックで full-text 折返し表示に展開。 */
function toggleKessanRow(event) {
  var tr = event.target.closest('tr');
  if (!tr) return;
  tr.classList.toggle('kessan-row-expanded');
}

/* --- リッチテキスト表示 → 編集モード切替 (issue #115) --- */
function switchToEdit(displayEl) {
  var editEl = displayEl.nextElementSibling;
  if (!editEl) return;
  displayEl.style.display = 'none';
  editEl.style.display = '';
  editEl.focus();
}

/* --- Escape キーでフォーカスを外す（→ 自動保存が発火） --- */
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var el = document.activeElement;
    if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.tagName === 'SELECT')) {
      /* 決算エディタ内の field ならその場で保存してからアコーディオン閉じる */
      if (el.classList && el.classList.contains('kessan-field')) {
        var editor = el.closest('.kessan-editor');
        var li = editor ? editor.closest('.kessan-stock') : null;
        el.blur();
        if (li) {
          saveKessanFromEditor(li);
          if (editor) editor.style.display = 'none';
        }
      } else {
        el.blur();
      }
    }
  }
});

/* --- 初期値を記録（変更検知用、display:none の要素も含む） --- */
var initialValues = {};
document.querySelectorAll('.editable-field, .editable-select').forEach(function(el) {
  initialValues[el.name] = el.value;
});

/* --- 四季報エリアのDOM構造スナップショット（revert用） --- */
var shikihoEditArea = document.getElementById('shikiho-edit-area');
var shikihoInitialHTML = shikihoEditArea ? shikihoEditArea.innerHTML : '';

/* --- 変更検知: 編集すると保存バーを表示 --- */
document.addEventListener('input', function(e) {
  var el = e.target;
  if (!el.classList.contains('editable-field') && !el.classList.contains('editable-select')) return;
  var formName = el.dataset.form;
  if (!formName) return;
  var changed = el.value !== (initialValues[el.name] || '');
  el.classList.toggle('dirty', changed);
  updateSaveBar(formName);
});

function updateSaveBar(formName) {
  var fields = document.querySelectorAll('[data-form="' + formName + '"]');
  var hasDirty = Array.from(fields).some(function(f) { return f.classList.contains('dirty'); });
  var bar = document.getElementById('save-bar-' + formName);
  if (bar) bar.classList.toggle('visible', hasDirty);
}

/* --- 元に戻す --- */
function revertForm(formName) {
  if (formName === 'shikiho') {
    revertShikiho();
    return;
  }
  document.querySelectorAll('[data-form="' + formName + '"]').forEach(function(el) {
    if (initialValues[el.name] !== undefined) {
      el.value = initialValues[el.name];
    }
    el.classList.remove('dirty');
  });
  updateSaveBar(formName);
}

/* --- 四季報: DOM構造ごと復元して初期値を再適用 --- */
function revertShikiho() {
  var area = document.getElementById('shikiho-edit-area');
  if (!area) return;
  /* DOM構造を初期状態に復元（行の追加/削除を巻き戻す） */
  area.innerHTML = shikihoInitialHTML;
  /* 復元されたフィールドに初期値を再適用 */
  area.querySelectorAll('.editable-field').forEach(function(el) {
    if (initialValues[el.name] !== undefined) {
      el.value = initialValues[el.name];
    }
    el.classList.remove('dirty');
  });
  /* overview も初期値に戻す（shikihoフォーム内の非四季報エリアフィールド） */
  document.querySelectorAll('[data-form="shikiho"]').forEach(function(el) {
    if (!area.contains(el)) {
      if (initialValues[el.name] !== undefined) {
        el.value = initialValues[el.name];
      }
      el.classList.remove('dirty');
    }
  });
  updateSaveBar('shikiho');
}

/* --- 非同期フォーム送信（直列化でリクエスト順序を保証） --- */
var _saveQueue = Promise.resolve();
function submitFormAsync(form) {
  var formData = new FormData(form);
  _saveQueue = _saveQueue.then(function() {
    return fetch(form.action, { method: 'POST', body: formData }).then(function(response) {
      if (!response.ok) return;
      /* 保存完了: dirty フラグをリセットして初期値を更新 */
      form.querySelectorAll('.editable-field, .editable-select').forEach(function(el) {
        initialValues[el.name] = el.value;
        el.classList.remove('dirty');
      });
      var formName = (form.querySelector('[data-form]') || {}).dataset;
      if (formName && formName.form) updateSaveBar(formName.form);
    });
  }).catch(function() { /* ネットワークエラー時もキューを継続 */ });
}

/* --- フォーカスアウト時の自動保存 --- */
var _autoSaveFormIds = {
  'ir': 'ir-comment-form',
  'memo': 'memo-form',
  'shikiho': 'shikiho-form'
};

document.addEventListener('focusout', function(e) {
  var el = e.target;
  if (!el.classList.contains('editable-field') && !el.classList.contains('editable-select')) return;
  var formName = el.dataset.form;
  if (!formName) return;

  /* フォーカス移動先が同じフォーム内なら保存しない（タブ移動対応） */
  setTimeout(function() {
    var next = document.activeElement;
    if (next && next.dataset && next.dataset.form === formName) return;

    /* dirty なフィールドがあれば自動保存 */
    var fields = document.querySelectorAll('[data-form="' + formName + '"]');
    var hasDirty = Array.from(fields).some(function(f) { return f.classList.contains('dirty'); });
    if (!hasDirty) return;

    var formId = _autoSaveFormIds[formName];
    if (!formId) return;
    var form = document.getElementById(formId);
    if (form) submitFormAsync(form);
  }, 100);
});

/* --- 四季報: 時期自動判定 --- */
function currentShikihoPeriod() {
  var now = new Date();
  var yy = now.getFullYear() % 100;
  var m = now.getMonth() + 1;
  /* 発売月の翌月まで: 12-2月→X.12, 3-5月→X.3, 6-8月→X.6, 9-11月→X.9 */
  if (m <= 2) return (yy - 1) + '.12';
  if (m <= 5) return yy + '.3';
  if (m <= 8) return yy + '.6';
  if (m <= 11) return yy + '.9';
  return yy + '.12';
}

/* --- 四季報コメント: 追加/削除 --- */
function updateShikihoState(opts) {
  var save = (opts && opts.save !== undefined) ? opts.save : true;
  var entries = document.querySelectorAll('#shikiho-edit-area .shikiho-entry');
  var count = entries.length;
  var countEl = document.getElementById('shikiho-count');
  if (countEl) countEl.textContent = count;
  entries.forEach(function(entry, i) {
    var hidden = entry.querySelector('input[type=hidden]');
    var span = entry.querySelector('span');
    if (hidden) {
      span.textContent = hidden.value || '-';
      hidden.name = 'shikiho_periods_' + i;
    }
    var ta = entry.querySelector('textarea');
    ta.name = 'shikiho_comments_' + i;
  });
  var btn = document.getElementById('btn-add-shikiho');
  if (btn) {
    btn.style.display = '';
    btn.textContent = '+ 追加';
  }
  /* 件数変更時は非同期で自動保存（save=false で抑制可能） */
  if (save) {
    var form = document.getElementById('shikiho-form');
    if (form) submitFormAsync(form);
  }
}

function addShikiho() {
  var area = document.getElementById('shikiho-edit-area');
  if (!area) return;
  var entries = area.querySelectorAll('.shikiho-entry');
  /* 8件以上なら最古（末尾）を削除してから追加 */
  if (entries.length >= MAX_SHIKIHO) {
    entries[entries.length - 1].remove();
    entries = area.querySelectorAll('.shikiho-entry');
  }
  var idx = entries.length;
  var period = currentShikihoPeriod();
  var div = document.createElement('div');
  div.className = 'shikiho-entry';
  div.style.cssText = 'display:flex;align-items:start;gap:0.3em;margin-bottom:0.3em;';
  div.innerHTML =
    '<span style="font-size:0.8em;color:#888;min-width:3em;padding-top:0.6em;">' + period + '</span>' +
    '<input type="hidden" name="shikiho_periods_' + idx + '" value="' + period + '">' +
    '<textarea class="editable-field" name="shikiho_comments_' + idx + '" rows="4" style="flex:1;" data-form="shikiho" placeholder="四季報コメントを入力..."></textarea>';
  area.insertBefore(div, document.getElementById('btn-add-shikiho'));
  var ta = div.querySelector('textarea');
  /* UI更新のみ、保存はしない（空のtextareaで保存するとデータロスになる） */
  updateShikihoState({ save: false });
  ta.focus();
}

function removeShikiho(btn) {
  btn.closest('.shikiho-entry').remove();
  updateShikihoState();
}

/* ==========================================================
 * 決算カレンダー (issue #127)
 *   インラインアコーディオン: クリックで入力欄を展開
 *   blur で自動保存 (_saveQueue 経由で直列化)
 * ========================================================== */

/* 銘柄行クリック: リンク・エディタ内クリックは除外して編集UIを展開 */
function handleKessanRowClick(e, li) {
  /* 親要素を辿って、クリック起点が除外対象（リンク・エディタ内）か判定 */
  var target = e.target;
  while (target && target !== li) {
    if (target.tagName === 'A') return;
    if (target.classList && target.classList.contains('kessan-editor')) return;
    if (target.classList && target.classList.contains('kessan-comment-view')) return;
    target = target.parentNode;
  }
  openKessanEditor(li);
}

function openKessanEditor(li) {
  var editor = li.querySelector('.kessan-editor');
  if (!editor) return;

  var isOpen = editor.style.display !== 'none';
  if (isOpen) {
    editor.style.display = 'none';
    return;
  }

  editor.style.display = '';
  if (editor.dataset.loaded === '0') {
    var code = li.dataset.code;
    var kessanbi = li.dataset.kessanbi;
    var url = '/api/kessan_comment/' + encodeURIComponent(code) +
              '?kessanbi=' + encodeURIComponent(kessanbi);
    fetch(url).then(function(res) {
      return res.ok ? res.json() : null;
    }).then(function(data) {
      if (!data) return;
      var preExp = editor.querySelector('.kessan-pre-expectation');
      var preOut = editor.querySelector('.kessan-pre-outlook');
      var postCom = editor.querySelector('.kessan-post-comment');
      var postPc = editor.querySelector('.kessan-post-price-change');
      var matagi = editor.querySelector('.kessan-matagi');
      if (preExp) preExp.value = data.pre_expectation || '';
      if (preOut) preOut.value = data.pre_outlook || '';
      if (postCom) postCom.value = data.post_comment || '';
      if (matagi) matagi.checked = !!data.kessan_matagi;
      if (postPc && data.post_price_change) {
        postPc.textContent = data.post_price_change;
      }
      editor.dataset.loaded = '1';
      rememberKessanInitialValues(editor);
    }).catch(function() { /* ネットワーク失敗時は静かに無視 */ });
  }

  var preOutEl = editor.querySelector('.kessan-pre-outlook');
  if (preOutEl) preOutEl.focus();
}

function rememberKessanInitialValues(editor) {
  editor.querySelectorAll('.kessan-field').forEach(function(el) {
    if (el.type === 'checkbox') {
      el.dataset.initial = el.checked ? '1' : '0';
    } else {
      el.dataset.initial = el.value;
    }
  });
}

function isKessanDirty(editor) {
  var dirty = false;
  editor.querySelectorAll('.kessan-field').forEach(function(el) {
    var current = el.type === 'checkbox' ? (el.checked ? '1' : '0') : el.value;
    if (current !== (el.dataset.initial || '')) dirty = true;
  });
  return dirty;
}

function saveKessanFromEditor(li) {
  var editor = li.querySelector('.kessan-editor');
  if (!editor) return;
  if (!isKessanDirty(editor)) return;

  var code = li.dataset.code;
  var kessanbi = li.dataset.kessanbi;
  var quarter = li.dataset.quarter || '0';
  var preExp = editor.querySelector('.kessan-pre-expectation');
  var preOut = editor.querySelector('.kessan-pre-outlook');
  var postCom = editor.querySelector('.kessan-post-comment');
  var matagi = editor.querySelector('.kessan-matagi');

  var formData = new FormData();
  formData.append('kessanbi', kessanbi);
  formData.append('quarter', quarter);
  formData.append('pre_expectation', preExp ? preExp.value : '');
  formData.append('pre_outlook', preOut ? preOut.value : '');
  formData.append('post_comment', postCom ? postCom.value : '');
  if (matagi) {
    formData.append('kessan_matagi', matagi.checked ? '1' : '0');
  }

  var url = '/api/kessan_comment/' + encodeURIComponent(code);
  _saveQueue = _saveQueue.then(function() {
    return fetch(url, { method: 'POST', body: formData }).then(function(res) {
      if (!res.ok) return null;
      return res.json();
    }).then(function(data) {
      if (!data) return;
      /* 初期値リセット */
      rememberKessanInitialValues(editor);
      /* post_price_change 表示更新 */
      var postPc = editor.querySelector('.kessan-post-price-change');
      if (postPc && data.post_price_change) {
        postPc.textContent = data.post_price_change;
      }
      /* 閲覧用 view DOM を再構築（リロード不要で反映） */
      updateKessanViewDOM(li, data);
      /* has-comment クラス付与で左縁に色がつく */
      if (data.pre_outlook || data.post_comment || data.pre_expectation) {
        li.classList.add('has-comment');
      } else {
        li.classList.remove('has-comment');
      }
    });
  }).catch(function() { /* エラー時もキュー継続 */ });
}

/* 保存成功後、li 内の表示用 DOM（見通し・反応・期待度バッジ・決算またぎ）を更新 */
function updateKessanViewDOM(li, data) {
  var isPast = li.dataset.isPast === '1';

  /* 決算またぎ ◆ マーク */
  var matagiMark = li.querySelector('.kessan-matagi-mark');
  if (data.kessan_matagi) {
    if (!matagiMark) {
      matagiMark = document.createElement('span');
      matagiMark.className = 'kessan-matagi-mark';
      matagiMark.title = '決算またぎ保有';
      matagiMark.textContent = '◆';
      var possessMark = li.querySelector('.kessan-possess-mark');
      var refNode = possessMark ? possessMark.nextSibling : li.firstChild;
      li.insertBefore(matagiMark, refNode);
    }
  } else if (matagiMark) {
    matagiMark.remove();
  }

  /* 期待度バッジ */
  var badge = li.querySelector('.kessan-expectation-badge');
  if (data.pre_expectation) {
    if (badge) {
      badge.className = 'kessan-expectation-badge exp-' + data.pre_expectation;
      badge.textContent = data.pre_expectation;
    } else {
      badge = document.createElement('span');
      badge.className = 'kessan-expectation-badge exp-' + data.pre_expectation;
      badge.textContent = data.pre_expectation;
      /* 4Q ラベルの後ろ、変動率の前に挿入 */
      var qLabel = li.querySelector('.kessan-q-label');
      var refNode = qLabel ? qLabel.nextSibling : li.querySelector('.kessan-comment-view');
      li.insertBefore(badge, refNode || li.firstChild);
    }
  } else if (badge) {
    badge.remove();
  }

  /* 見通し・反応の view ブロック */
  var view = li.querySelector('.kessan-comment-view');
  var hasPreOut = !!data.pre_outlook;
  var hasPostCom = isPast && !!data.post_comment;

  if (!hasPreOut && !hasPostCom) {
    if (view) view.remove();
    return;
  }

  if (!view) {
    view = document.createElement('div');
    view.className = 'kessan-comment-view';
    /* エディタの直前に挿入 */
    var editor = li.querySelector('.kessan-editor');
    li.insertBefore(view, editor);
  }
  view.innerHTML = '';
  if (hasPreOut) {
    var pre = document.createElement('div');
    pre.className = 'kessan-pre-view';
    var preLabel = document.createElement('span');
    preLabel.className = 'kessan-view-label';
    preLabel.textContent = '見通し:';
    pre.appendChild(preLabel);
    pre.appendChild(document.createTextNode(' ' + data.pre_outlook));
    view.appendChild(pre);
  }
  if (hasPostCom) {
    var post = document.createElement('div');
    post.className = 'kessan-post-view';
    var postLabel = document.createElement('span');
    postLabel.className = 'kessan-view-label';
    postLabel.textContent = '反応:';
    post.appendChild(postLabel);
    post.appendChild(document.createTextNode(' ' + data.post_comment));
    view.appendChild(post);
  }
}

/* フォーカスアウトで保存。kessan-field からのフォーカス遷移先が
   同じエディタ内なら保存しない（タブ移動対応） */
document.addEventListener('focusout', function(e) {
  var el = e.target;
  if (!el.classList || !el.classList.contains('kessan-field')) return;
  var editor = el.closest('.kessan-editor');
  if (!editor) return;

  setTimeout(function() {
    var next = document.activeElement;
    if (next && editor.contains(next)) return;
    var li = editor.closest('.kessan-stock');
    if (li) saveKessanFromEditor(li);
  }, 100);
});

/* Enter 押下でセレクトを確定させた時にもフォーカスアウトを促す（select は change で明示的に） */
document.addEventListener('change', function(e) {
  var el = e.target;
  if (!el.classList || !el.classList.contains('kessan-pre-expectation')) return;
  var li = el.closest('.kessan-stock');
  if (li) saveKessanFromEditor(li);
});
