/* 流体提示气泡导航（原生实现，无 React / GSAP / 外部素材）
 *
 * 核心：只有一枚胶囊 DOM。胶囊 translateY(y)，内部 label-track 同时
 * translateY(-y)，于是每行文字在整个视口里的高度始终不变；上下移动的是
 * 那个 overflow:hidden 的裁切窗口。切换过程中旧文字从一端移出、新文字
 * 从另一端露出，而不是给每个按钮各淡入一个气泡。
 *
 * 支持同页多实例：window.mountFluidNav(root) 返回独立实例；页面自动挂载
 * 一个到 window.FluidNav。所有状态按实例独立，destroy() 清理干净。
 */
(function () {
  'use strict';

  var EASE_IN_MS = 150;      // 首次出现：只淡入
  var MOVE_MS = 430;         // 换入口：位置 / 宽度一起走 expo.out
  var HIDE_MS = 150;
  var LEAVE_DELAY = 75;      // 经过按钮间隙时不闪烁
  var TIP_GAP = 14;          // 胶囊距侧栏右缘

  function easePower2Out(t) { return 1 - (1 - t) * (1 - t); }
  function easeExpoOut(t) { return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t); }
  function clock() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }

  function mountFluidNav(root) {
    root = root || document;
    var q = function (sel) { return root.querySelector(sel); };
    var nav = q('.nav');
    var tip = q('.fluid-tip');
    if (!nav || !tip) return null;

    var sidebar = q('.sidebar') || nav.parentNode;
    var track = tip.querySelector('.fluid-tip-track');
    var buttons = Array.prototype.slice.call(nav.querySelectorAll('.tab'));
    var rows = Array.prototype.slice.call(tip.querySelectorAll('.fluid-tip-row'));
    var n = Math.min(buttons.length, rows.length);
    if (!n || !track) return null;

    var labels = rows.map(function (row, i) {
      var el = row.querySelector('.fluid-tip-label');
      return (el && el.textContent.trim()) ||
             buttons[i].getAttribute('aria-label') || '';
    });

    // 同页多实例时 aria-describedby 必须指向各自的胶囊
    if (!tip.id) tip.id = 'fluid-tip-' + Math.random().toString(36).slice(2, 10);

    var state = { y: 0, width: 0, opacity: 0 };
    var centers = [], widths = [];
    var hint = -1, active = -1;
    var tween = null, raf = 0, hideTimer = 0, dead = false;
    var cleanups = [], ro = null;

    function on(el, type, fn, opts) {
      if (!el || !el.addEventListener) return;
      el.addEventListener(type, fn, opts);
      cleanups.push(function () { el.removeEventListener(type, fn, opts); });
    }
    function reducedMotion() {
      return !!(window.matchMedia &&
                window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    }

    function paint() {
      tip.style.transform = 'translateY(' + state.y + 'px)';
      tip.style.width = state.width + 'px';
      tip.style.opacity = String(state.opacity);
      track.style.transform = 'translateY(' + (-state.y) + 'px)';
    }
    function cancelHide() {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = 0; }
    }
    function killTween() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      tween = null;
    }
    function animateTo(target, duration, ease) {
      killTween();
      var from = { y: state.y, width: state.width, opacity: state.opacity };
      var tw = { start: clock(), duration: duration, paused: false, pauseAt: 0 };
      tween = tw;
      function frame(now) {
        if (dead || tween !== tw) return;
        if (tw.paused) { raf = requestAnimationFrame(frame); return; }
        var p = Math.min(1, (now - tw.start) / duration);
        var k = ease(p);
        // 从「当前正在渲染的值」接续，快速连续跨项不会跳回旧目标
        state.y = from.y + (target.y - from.y) * k;
        state.width = from.width + (target.width - from.width) * k;
        state.opacity = from.opacity + (target.opacity - from.opacity) * k;
        paint();
        if (p < 1) raf = requestAnimationFrame(frame);
        else { raf = 0; tween = null; }
      }
      raf = requestAnimationFrame(frame);
    }

    function setActiveIndex(i) {
      active = i;
      buttons.forEach(function (b, j) {
        b.setAttribute('aria-current', j === i ? 'page' : 'false');
      });
    }

    function show(i, immediate) {
      if (dead || i < 0 || i >= n) return;
      cancelHide();
      hint = i;
      tip.setAttribute('aria-hidden', 'false');
      tip.setAttribute('aria-label', labels[i]);
      buttons.forEach(function (b, j) {
        if (j === i) b.setAttribute('aria-describedby', tip.id);
        else b.removeAttribute('aria-describedby');
      });

      var h = tip.offsetHeight || 64;
      var next = { y: centers[i] - h / 2, width: widths[i], opacity: 1 };
      if (immediate || reducedMotion()) {
        killTween();
        state.y = next.y; state.width = next.width; state.opacity = next.opacity;
        paint();
        return;
      }
      var first = state.opacity < 0.01;
      if (first) {
        // 第一次进入先把几何放在对应入口，只做 0→1 淡入
        state.y = next.y;
        state.width = next.width;
        paint();
      }
      animateTo(next, first ? EASE_IN_MS : MOVE_MS,
                first ? easePower2Out : easeExpoOut);
    }

    function hide(immediate) {
      cancelHide();
      hint = -1;
      tip.setAttribute('aria-hidden', 'true');
      buttons.forEach(function (b) { b.removeAttribute('aria-describedby'); });
      if (immediate || reducedMotion()) {
        killTween();
        state.opacity = 0;              // y / width 保留，重新进入时从这里继续
        paint();
      } else {
        animateTo({ y: state.y, width: state.width, opacity: 0 },
                  HIDE_MS, easePower2Out);
      }
    }

    function leave() {
      cancelHide();
      hideTimer = setTimeout(function () {
        hideTimer = 0;
        var focused = buttons.indexOf(document.activeElement);
        if (focused >= 0) show(focused);   // 键盘焦点还在就别收
        else hide();
      }, LEAVE_DELAY);
    }

    function fit() {
      if (dead) return;
      var h = tip.offsetHeight || 64;
      var right = sidebar.getBoundingClientRect ? sidebar.getBoundingClientRect().right : 0;
      tip.style.left = Math.round(right + TIP_GAP) + 'px';
      buttons.forEach(function (b, i) {
        var r = b.getBoundingClientRect();
        centers[i] = r.top + r.height / 2;      // 视口坐标，胶囊是 fixed
        rows[i].style.top = (centers[i] - h / 2) + 'px';
        widths[i] = Math.ceil(rows[i].getBoundingClientRect().width);
      });
      if (hint >= 0) {
        // 尺寸变化 / 字体加载完成后按新的真实文字宽度重新落位
        killTween();
        state.y = centers[hint] - h / 2;
        state.width = widths[hint];
      }
      paint();
    }

    // ---- 交互 ----
    buttons.forEach(function (b, i) {
      on(b, 'pointerenter', function (e) {
        if (e.pointerType !== 'touch') show(i);
      });
      on(b, 'pointerleave', function (e) {
        if (e.pointerType !== 'touch') leave();
      });
      on(b, 'focus', function () { show(i); });
      on(b, 'blur', leave);
      on(b, 'click', function () {
        // activateTab 由页面原有的 onclick 负责，这里只同步本地选中态
        setActiveIndex(i);
        show(i);
      });
    });

    on(nav, 'keydown', function (e) {
      var i = buttons.indexOf(document.activeElement);
      if (i < 0) return;
      var next = i;
      if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = (i + 1) % n;
      else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = (i - 1 + n) % n;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = n - 1;
      else if (e.key === 'Escape') {
        e.preventDefault();
        hide(true);                     // 收起提示但保留焦点
        return;
      } else return;                   // Tab / Shift+Tab 交回浏览器
      e.preventDefault();
      buttons[next].focus();
    });

    on(document, 'keydown', function (e) {
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      var i = Number(e.key) - 1;
      if (i >= 0 && i < n) {
        e.preventDefault();
        buttons[i].focus();
        buttons[i].click();             // 走页面原有 onclick → activateTab
      }
    });

    on(document, 'pointerdown', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t.closest('.sidebar')) {
        if (!t.closest('.tab')) hide();
        return;
      }
      if (t !== tip && !tip.contains(t)) hide();
    });

    on(document, 'visibilitychange', function () {
      if (!tween) return;
      if (document.hidden) {
        if (!tween.paused) { tween.paused = true; tween.pauseAt = clock(); }
      } else if (tween.paused) {
        tween.start += clock() - tween.pauseAt;
        tween.paused = false;
      }
    });

    var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    on(mq, 'change', function () {
      if (reducedMotion()) {
        killTween();
        if (hint >= 0) show(hint, true);
        else hide(true);
      }
    });

    if (window.ResizeObserver) {
      ro = new ResizeObserver(fit);
      ro.observe(sidebar);
      ro.observe(nav);
    }
    on(window, 'resize', fit);
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(function () { if (!dead) fit(); });
    }

    // 初始选中态按页面已有的 .active 对齐
    var initial = buttons.findIndex ? buttons.findIndex(function (b) {
      return b.classList.contains('active');
    }) : 0;
    setActiveIndex(initial < 0 ? 0 : initial);
    fit();
    paint();

    function destroy() {
      if (dead) return;
      dead = true;
      killTween();
      cancelHide();
      cleanups.forEach(function (fn) { fn(); });
      cleanups.length = 0;
      if (ro) { ro.disconnect(); ro = null; }
      buttons.forEach(function (b) { b.removeAttribute('aria-describedby'); });
      tip.setAttribute('aria-hidden', 'true');
    }

    return {
      syncActive: function (name) {
        for (var i = 0; i < n; i++) {
          if (buttons[i].dataset.tab === name) { setActiveIndex(i); return; }
        }
      },
      show: show,
      hide: hide,
      fit: fit,
      destroy: destroy,
      // 验收用的只读快照，不参与动画
      snapshot: function () {
        return { y: state.y, width: state.width, opacity: state.opacity,
                 hint: hint, active: active, centers: centers.slice(),
                 widths: widths.slice(), height: tip.offsetHeight,
                 left: parseFloat(tip.style.left) || 0 };
      }
    };
  }

  window.mountFluidNav = mountFluidNav;
  function boot() {
    if (window.FluidNav && window.FluidNav.destroy) return;
    window.FluidNav = mountFluidNav(document);
  }
  if (document.querySelector('.fluid-tip') && document.querySelector('.nav')) boot();
  else document.addEventListener('DOMContentLoaded', boot, { once: true });
})();
