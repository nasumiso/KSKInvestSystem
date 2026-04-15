/* 銘柄詳細ビュー: click-to-edit + 変更検知 + 四季報追加/削除 */

var MAX_SHIKIHO = 8;

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
      el.blur();
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

/* --- 非同期フォーム送信（ページリロードなし） --- */
function submitFormAsync(form) {
  var formData = new FormData(form);
  fetch(form.action, { method: 'POST', body: formData }).then(function(response) {
    if (!response.ok) return;
    /* 保存完了: dirty フラグをリセットして初期値を更新 */
    form.querySelectorAll('.editable-field, .editable-select').forEach(function(el) {
      initialValues[el.name] = el.value;
      el.classList.remove('dirty');
    });
    var formName = (form.querySelector('[data-form]') || {}).dataset;
    if (formName && formName.form) updateSaveBar(formName.form);
  });
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
function updateShikihoState() {
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
  /* 件数変更時は非同期で自動保存 */
  var form = document.getElementById('shikiho-form');
  if (form) submitFormAsync(form);
}

function addShikiho() {
  var area = document.getElementById('shikiho-edit-area');
  if (!area) return;
  var entries = area.querySelectorAll('.shikiho-entry');
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
  updateShikihoState();
  ta.focus();
}

function removeShikiho(btn) {
  btn.closest('.shikiho-entry').remove();
  updateShikihoState();
}
