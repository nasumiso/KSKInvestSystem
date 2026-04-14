/* 銘柄詳細ビュー: click-to-edit + 変更検知 + 四季報追加/削除 */

var MAX_SHIKIHO = 5;

/* --- リッチテキスト表示 → 編集モード切替 (issue #115) --- */
function switchToEdit(displayEl) {
  var editEl = displayEl.nextElementSibling;
  if (!editEl) return;
  displayEl.style.display = 'none';
  editEl.style.display = '';
  editEl.focus();
}

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
    if (form) form.submit();
  }, 100);
});

/* --- 四季報コメント: 追加/削除 --- */
function updateShikihoState() {
  var entries = document.querySelectorAll('#shikiho-edit-area .shikiho-entry');
  var count = entries.length;
  var countEl = document.getElementById('shikiho-count');
  if (countEl) countEl.textContent = count;
  entries.forEach(function(entry, i) {
    entry.querySelector('span').textContent = (i + 1) + '.';
    var ta = entry.querySelector('textarea');
    ta.name = 'shikiho_comments_' + i;
  });
  var btn = document.getElementById('btn-add-shikiho');
  if (btn) {
    var remaining = MAX_SHIKIHO - count;
    btn.style.display = remaining <= 0 ? 'none' : '';
    btn.textContent = '+ 追加 (残り' + remaining + '件)';
  }
  /* 件数変更は常にdirty扱い */
  var bar = document.getElementById('save-bar-shikiho');
  if (bar) bar.classList.add('visible');
}

function addShikiho() {
  var area = document.getElementById('shikiho-edit-area');
  if (!area) return;
  var entries = area.querySelectorAll('.shikiho-entry');
  if (entries.length >= MAX_SHIKIHO) return;
  var idx = entries.length;
  var div = document.createElement('div');
  div.className = 'shikiho-entry';
  div.style.cssText = 'display:flex;align-items:start;gap:0.3em;margin-bottom:0.3em;';
  div.innerHTML =
    '<span style="font-size:0.8em;color:#888;width:1.5em;padding-top:0.6em;">' + (idx + 1) + '.</span>' +
    '<textarea class="editable-field" name="shikiho_comments_' + idx + '" rows="2" style="flex:1;" data-form="shikiho" placeholder="四季報コメントを入力..."></textarea>' +
    '<button type="button" class="outline secondary" style="padding:0.2em 0.5em;font-size:0.8em;margin:0;margin-top:0.4em;" onclick="removeShikiho(this)" title="削除">&times;</button>';
  area.insertBefore(div, document.getElementById('btn-add-shikiho'));
  updateShikihoState();
  div.querySelector('textarea').focus();
}

function removeShikiho(btn) {
  btn.closest('.shikiho-entry').remove();
  updateShikihoState();
}
