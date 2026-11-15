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
  var TIP_GAP = 10;          // 胶囊与图标列的间距

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
    var compact = false, dismissed = false;
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
      if (tween && tween.guard) clearTimeout(tween.guard);
      raf = 0;
      tween = null;
    }
    function animateTo(target, duration, ease) {
      killTween();
      var from = { y: state.y, width: state.width, opacity: state.opacity };
      var tw = { start: clock(), duration: duration, target: target, guard: 0 };
      tween = tw;
      // 兜底：窗口最小化 / 后台标签 / 无头环境里 RAF 可能整段停摆，
      // 到点直接落位，避免标签换了但胶囊还停在上一项的高度。
      tw.guard = setTimeout(function () {
        if (dead || tween !== tw) return;
        if (raf) cancelAnimationFrame(raf);
        raf = 0; tw.guard = 0; tween = null;
        state.y = target.y; state.width = target.width; state.opacity = target.opacity;
        paint();
      }, duration + 40);
      function frame() {
        if (dead || tween !== tw) return;
        // 统一用 clock()，别混用 RAF 帧时间戳：帧时间可能早于 start，
        // 会让 p 变负、opacity 变负，补间还会一直悬着不结束。
        var p = Math.max(0, Math.min(1, (clock() - tw.start) / duration));
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
      // 必须在 hint = i 之前判断：否则常驻状态下每次 show 都会被自己短路
      if (hint === i && state.opacity > 0.99 && !tween) return;
      cancelHide();
      dismissed = false;
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
      dismissed = true;
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
        if (focused >= 0) { show(focused); return; }   // 键盘焦点还在就保持
        if (compact) { hide(); return; }               // 窄栏空间不够，退回悬停模式
        if (dismissed) return;                        // Esc 显式收起后不自动弹回
        if (hint === active && state.opacity > 0.99) return;  // 本来就在当前页，别重启补间
        show(active >= 0 ? active : 0);               // 常驻：回到当前选中项
      }, LEAVE_DELAY);
    }

    function fit() {
      if (dead) return;
      var h = tip.offsetHeight || 64;
      var sr = sidebar.getBoundingClientRect
        ? sidebar.getBoundingClientRect() : { right: 0, width: 0 };
      var br = buttons[0].getBoundingClientRect();
      var wasCompact = compact;
      compact = sr.width < 240;
      // 宽栏：胶囊留在侧栏内，紧贴靠左的图标列；窄栏：退回浮在主区左缘
      var leftX = compact ? (sr.right + TIP_GAP) : (br.right + TIP_GAP);
      tip.style.left = Math.round(leftX) + 'px';
      tip.dataset.placement = compact ? 'floating' : 'inside';
      buttons.forEach(function (b, i) {
        var r = b.getBoundingClientRect();
        centers[i] = r.top + r.height / 2;      // 视口坐标，胶囊是 fixed
        rows[i].style.top = (centers[i] - h / 2) + 'px';
        widths[i] = Math.ceil(rows[i].getBoundingClientRect().width);
      });
      if (compact !== wasCompact) {
        // 窗口跨过 900px：窄栏改回「悬停才显示」，宽栏恢复常驻
        if (compact) hide(true);
        else show(active >= 0 ? active : 0, true);
      } else if (hint >= 0) {
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
      // 宽栏常驻：点输入框、按钮或主区任意位置都不收起提示
      if (!compact) return;
      var t = e.target;
      if (!t || !t.closest) return;
      if (t.closest('.sidebar')) {
        if (!t.closest('.tab')) hide();
        return;
      }
      if (t !== tip && !tip.contains(t)) hide();
    });

    on(document, 'visibilitychange', function () {
      if (!document.hidden || !tween) return;
      // 后台 RAF 会停摆，直接落位到目标，避免补间永远悬在半路
      var target = tween.target;
      killTween();
      state.y = target.y; state.width = target.width; state.opacity = target.opacity;
      paint();
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
    // 宽栏一开始就把当前页的提示淡入，之后任何点击都不会让它消失
    if (!compact) show(initial < 0 ? 0 : initial, true);

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
          if (buttons[i].dataset.tab === name) {
            setActiveIndex(i);
            if (compact) return;      // 窄栏不自动弹出，仍靠 hover / focus
            show(i);                 // 切页时胶囊跟着滑到新页
            return;
          }
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
                 left: parseFloat(tip.style.left) || 0,
                 compact: compact, dismissed: dismissed,
                 placement: tip.dataset.placement || '' };
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
