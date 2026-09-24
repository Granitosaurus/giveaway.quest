// Progressive enhancement only: theme toggle, localised timestamps, countdowns,
// copy button, live filter, Markdown write/preview toggle, confirm dialogs. The page works without it.
// Kept as an external file (no inline scripts/handlers) so the CSP can be `script-src 'self'`.
(function () {
  // Theme toggle. Untouched, the page follows the OS (prefers-color-scheme);
  // once the user picks, the choice is stored and wins until they pick again.
  var root = document.documentElement;
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  var stored = function () { try { return localStorage.getItem('theme'); } catch (e) { return null; } };
  var isDark = function () { return (root.dataset.theme || (mq.matches ? 'questdark' : 'quest')) === 'questdark'; };
  var toggle = document.querySelector('[data-theme-toggle]');
  if (toggle) {
    toggle.checked = isDark();
    toggle.addEventListener('change', function () {
      var t = toggle.checked ? 'questdark' : 'quest';
      root.dataset.theme = t;
      try { localStorage.setItem('theme', t); } catch (e) {}
    });
    mq.addEventListener('change', function () { if (!stored()) toggle.checked = mq.matches; });
  }

  var fmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  document.querySelectorAll('time[data-local]').forEach(function (el) {
    var d = new Date(el.getAttribute('datetime'));
    if (!isNaN(d)) { el.textContent = fmt.format(d); el.title = el.getAttribute('datetime'); }
  });
  var timers = document.querySelectorAll('[data-countdown]');
  if (timers.length) {
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var tick = function () {
      timers.forEach(function (el) {
        var ms = new Date(el.getAttribute('data-countdown')) - Date.now();
        if (ms <= 0) { el.textContent = 'ended · picking a winner'; return; }
        var s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
            m = Math.floor(s % 3600 / 60), sec = s % 60;
        el.textContent = (d ? d + 'd ' : '') + pad(h) + ':' + pad(m) + ':' + pad(sec);
      });
    };
    tick(); setInterval(tick, 1000);
  }

  document.querySelectorAll('[data-copy-url]').forEach(function (btn) {
    var label = btn.textContent;
    btn.addEventListener('click', function () {
      navigator.clipboard.writeText(btn.getAttribute('data-copy-url')).then(function () {
        btn.textContent = 'Copied!';
        setTimeout(function () { btn.textContent = label; }, 1500);
      });
    });
  });

  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      if (!window.confirm(form.getAttribute('data-confirm'))) event.preventDefault();
    });
  });

  document.querySelectorAll('[data-live-filter]').forEach(function (form) {
    var submitBtn = form.querySelector('[data-filter-submit]');
    var spinner = form.querySelector('[data-filter-spinner]');
    if (submitBtn) submitBtn.classList.add('hidden');
    var submit = function () {
      if (spinner) spinner.outerHTML = '<span class="loading loading-spinner loading-xs" data-filter-spinner></span>';
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    };
    var timer;
    form.querySelectorAll('input[type="search"]').forEach(function (input) {
      input.addEventListener('input', function () {
        clearTimeout(timer);
        timer = setTimeout(submit, 400);
      });
    });
    form.querySelectorAll('select').forEach(function (select) {
      select.addEventListener('change', submit);
    });
  });

  document.querySelectorAll('[data-winner-count]').forEach(function (input) {
    var slots = document.querySelectorAll('[data-winner-slot]');
    var apply = function () {
      var count = parseInt(input.value, 10) || 1;
      slots.forEach(function (slot) {
        slot.classList.toggle('hidden', parseInt(slot.getAttribute('data-winner-slot'), 10) > count);
      });
    };
    input.addEventListener('input', apply);
    apply();
  });

  document.querySelectorAll('[data-md-editor]').forEach(function (root) {
    var input = root.querySelector('[data-md-input]');
    var preview = root.querySelector('[data-md-preview]');
    var tabs = root.querySelectorAll('[data-md-tab]');
    var csrfToken = function () {
      var m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
      return m ? decodeURIComponent(m[1]) : '';
    };
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        var name = tab.getAttribute('data-md-tab');
        tabs.forEach(function (t) { t.classList.toggle('btn-active', t === tab); });
        input.classList.toggle('hidden', name === 'preview');
        preview.classList.toggle('hidden', name !== 'preview');
        if (name !== 'preview') return;
        preview.textContent = 'Loading…';
        fetch('/md-preview', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'x-csrftoken': csrfToken()
          },
          body: 'text=' + encodeURIComponent(input.value)
        }).then(function (r) {
          if (!r.ok) throw new Error('bad response');
          return r.text();
        }).then(function (html) {
          preview.innerHTML = html;
        }).catch(function () {
          preview.textContent = 'Preview failed to load.';
        });
      });
    });
  });
})();
