/* 像素日夜开关 —— 原生实现（无框架、无依赖、无远程素材）
 *
 * 逻辑画布固定 520×272，按钮靠 CSS 等比缩放；所有绘制按逻辑坐标进行。
 * 主路径：WebGL2 单 pass 全屏 shader 画椭圆背景、轨道、磨砂玻璃折射与光晕；
 *        Canvas 2D 叠加层画绕行光带、像素月亮/太阳与散落重组的粒子。
 * 回退：WebGL2 不可用或上下文丢失时，CSS/2D 画轨道与半透明玻璃，其余照旧。
 *
 * 公开 API：
 *   new PixelToggle(button, options)
 *   mountPixelToggle(host, options) -> PixelToggle
 *   options: checked / onCheckedChange / sound / reducedMotion / height / label
 *   instance: setChecked(value, user) / update(options) / destroy()
 */
(function (global) {
  'use strict';

  var W = 520, H = 272, TRACK_R = 86;
  var KNOB = 240, KNOB_R = 69, KNOB_X0 = 16, TRAVEL = 248, KNOB_CY = 136;
  var GRID = 6.85, TAU = Math.PI * 2;

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function smooth(t) { t = clamp(t, 0, 1); return t * t * (3 - 2 * t); }
  function logistic(x) { return 1 / (1 + Math.exp(-x)); }
  function mix(a, b, t) { return a + (b - a) * t; }

  function sdRoundBox(px, py, hw, hh, r) {
    var qx = Math.abs(px) - hw + r, qy = Math.abs(py) - hh + r;
    return Math.min(Math.max(qx, qy), 0) +
      Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) - r;
  }

  function rgbMix(a, b, t) {
    return 'rgb(' + Math.round(mix(a[0], b[0], t)) + ',' +
      Math.round(mix(a[1], b[1], t)) + ',' +
      Math.round(mix(a[2], b[2], t)) + ')';
  }

  // ---------------------------------------------------------------- 像素图形
  function buildCells() {
    var moon = [], sun = [], x, y;
    for (y = -8; y <= 8; y++) {
      for (x = -8; x <= 8; x++) {
        if (Math.hypot(x, y) > 6.6) continue;
        if (Math.hypot(x - 3.3, y + 2.4) > 5.4) {
          moon.push({ x: x, y: y, a: 1 });
        } else if ((x + y) % 2 === 0) {
          moon.push({ x: x, y: y, a: 0.15 });
        }
      }
    }
    // 中心 (7,-7) 的十字星
    moon.push({ x: 7, y: -7, a: 1 });
    [[1, 0], [-1, 0], [0, 1], [0, -1]].forEach(function (d) {
      moon.push({ x: 7 + d[0], y: -7 + d[1], a: 0.45 });
    });

    var seen = {};
    for (y = -8; y <= 8; y++) {
      for (x = -8; x <= 8; x++) {
        if (Math.hypot(x, y) <= 3.7) {
          seen[x + ',' + y] = 1;
          sun.push({ x: x, y: y, a: 1 });
        }
      }
    }
    for (var k = 0; k < 8; k++) {
      var angle = k * Math.PI / 4;
      var radii = (k % 2 === 0) ? [5.6, 6.6, 7.6] : [5.9, 7.2];
      radii.forEach(function (r) {
        var gx = Math.round(Math.cos(angle) * r);
        var gy = Math.round(Math.sin(angle) * r);
        var key = gx + ',' + gy;
        if (!seen[key]) { seen[key] = 1; sun.push({ x: gx, y: gy, a: 1 }); }
      });
    }
    return {
      moon: moon, sun: sun,
      count: Math.max(moon.length, sun.length) + 20
    };
  }
  var CELLS = buildCells();

  // ---------------------------------------------------------------- 样式
  var STYLE_ID = 'pixel-toggle-style';
  var STYLE_TEXT = [
    '.pixel-toggle{position:relative;display:block;margin:0;padding:0;border:0;',
    'background:transparent;overflow:hidden;border-radius:72px;cursor:pointer;',
    'width:432px;max-width:100%;aspect-ratio:520 / 272;',
    'touch-action:pan-y;-webkit-tap-highlight-color:transparent;}',
    '.pixel-toggle>canvas{position:absolute;inset:0;width:100%;height:100%;',
    'display:block;pointer-events:none;}',
    '.pixel-toggle[data-dragging="1"]{cursor:grabbing;}',
    '.pixel-toggle:focus-visible{outline:3px solid var(--accent,#7C6BF2);',
    'outline-offset:4px;}',
    '.pixel-toggle.mini{width:84px;border-radius:18px;}'
  ].join('');

  function ensureStyle(doc) {
    if (doc.getElementById(STYLE_ID)) return;
    var style = doc.createElement('style');
    style.id = STYLE_ID;
    style.textContent = STYLE_TEXT;
    doc.head.appendChild(style);
  }

  // ---------------------------------------------------------------- WebGL2
  var VERT = [
    '#version 300 es',
    'out vec2 v_uv;',
    'void main(){',
    '  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));',
    '  v_uv = p;',
    '  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);',
    '}'
  ].join('\n');

  // 520 / 272 与上面的逻辑尺寸保持一致，写死进 shader 省一组 uniform
  var FRAG = [
    '#version 300 es',
    'precision highp float;',
    'in vec2 v_uv;',
    'out vec4 fragColor;',
    'uniform float u_theme,u_knobX,u_press,u_squashW,u_squashH,u_phase,u_grain;',
    'float sdBox(vec2 p,vec2 b,float r){',
    '  vec2 q=abs(p)-b+r;',
    '  return min(max(q.x,q.y),0.0)+length(max(q,0.0))-r;',
    '}',
    'vec3 background(vec2 p){',
    '  vec2 q=(p-vec2(520.0*0.92,272.0*(-0.08)))/vec2(520.0*1.40,272.0*1.55);',
    '  float d=clamp(length(q),0.0,1.0);',
    '  vec3 na=vec3(0.129,0.129,0.137),nb=vec3(0.051,0.051,0.055);',
    '  vec3 da=vec3(0.925,0.925,0.918),db=vec3(0.804,0.804,0.800);',
    '  return mix(mix(na,da,u_theme),mix(nb,db,u_theme),d);',
    '}',
    'void main(){',
    '  vec2 p=vec2(v_uv.x*520.0,(1.0-v_uv.y)*272.0);',
    '  vec3 col=background(p);',
    '  float sdTrack=sdBox(p-vec2(260.0,136.0),vec2(260.0,136.0),86.0);',
    '  float sdShadow=sdBox(p-vec2(260.0,153.0),vec2(260.0,136.0),86.0);',
    '  float shadow=(1.0-smoothstep(0.0,60.0,max(sdShadow,0.0)))*0.36*(1.0-u_theme*0.45);',
    '  col*=(1.0-shadow);',
    '  vec3 trackCol=mix(vec3(0.072,0.073,0.080),vec3(0.902,0.902,0.898),u_theme);',
    '  float trackAA=1.0-smoothstep(-0.6,0.9,sdTrack);',
    '  col=mix(col,trackCol,trackAA);',
    '  float rim=(1.0-smoothstep(0.0,1.6,abs(sdTrack)))*trackAA;',
    '  col+=rim*(p.y<136.0?vec3(-0.055):vec3(0.075))*(0.5+0.5*u_theme);',
    '  float kw=120.0*(1.0-0.055*u_press+u_squashW);',
    '  float kh=120.0*(1.0-0.055*u_press+u_squashH);',
    '  vec2 kc=vec2(u_knobX+120.0,136.0);',
    '  vec2 kp=p-kc;',
    '  float sdKnob=sdBox(kp,vec2(kw,kh),69.0);',
    '  float inside=1.0-smoothstep(-0.7,0.8,sdKnob);',
    '  if(inside>0.001){',
    '    float d=max(0.0,-sdKnob);',
    '    float slope=pow(1.0-clamp(d/31.0,0.0,1.0),2.6);',
    '    vec2 g=vec2(',
    '      sdBox(kp+vec2(1.0,0.0),vec2(kw,kh),69.0)-sdBox(kp-vec2(1.0,0.0),vec2(kw,kh),69.0),',
    '      sdBox(kp+vec2(0.0,1.0),vec2(kw,kh),69.0)-sdBox(kp-vec2(0.0,1.0),vec2(kw,kh),69.0));',
    '    vec2 n=normalize(g+vec2(0.0001));',
    '    vec2 src=kc+(kp/vec2(240.0,240.0))*0.945*240.0-n*slope*47.0;',
    '    vec3 refr=background(src);',
    '    vec3 refrRGB=vec3(background(src+vec2(2.0,0.0)).r,refr.g,background(src-vec2(2.0,0.0)).b);',
    '    vec3 frost=(background(src)+background(src+vec2(4.0,0.0))',
    '      +background(src-vec2(4.0,0.0))+background(src+vec2(0.0,4.0))',
    '      +background(src-vec2(0.0,4.0)))/5.0;',
    '    vec3 glass=mix(refrRGB,frost,clamp(slope*0.85,0.0,1.0));',
    '    float gy=clamp((p.y-(kc.y-120.0))/240.0,0.0,1.0);',
    '    vec3 grad=mix(mix(vec3(0.42,0.43,0.47),vec3(0.15,0.16,0.19),gy),',
    '                  mix(vec3(0.98,0.98,0.97),vec3(0.86,0.86,0.85),gy),u_theme);',
    '    vec3 knob=mix(grad,glass,0.22+0.35*slope);',
    '    float hi=clamp((-kp.x-kp.y)/(kw+kh)*1.4,0.0,1.0)',
    '      *(1.0-smoothstep(0.0,12.0,abs(sdKnob)));',
    '    knob+=hi*mix(vec3(0.10,0.11,0.16),vec3(0.16,0.15,0.13),u_theme);',
    '    float grain=fract(sin(dot(p,vec2(12.9898,78.233))+u_grain)*43758.5453);',
    '    knob+=(grain-0.5)*0.02;',
    '    col=mix(col,knob,inside);',
    '  }',
    '  float dist=abs(sdKnob-8.0);',
    '  float ang=atan(kp.y,kp.x);',
    '  float centerA=1.5707963-6.2831853*u_phase;',
    '  float da=abs(atan(sin(ang-centerA),cos(ang-centerA)));',
    '  float along=1.0-smoothstep(0.0,0.92,da);',
    '  float outer=0.43*exp(-dist/21.0)+0.33*exp(-dist/85.0);',
    '  float inner=0.28*exp(-abs(sdKnob+28.0)/28.0)+0.18*exp(-abs(sdKnob+73.0)/73.0);',
    '  vec3 glowCol=mix(vec3(0.435,0.525,1.0),vec3(1.0,0.659,0.18),u_theme);',
    '  col+=glowCol*along*(outer+inner)*0.55;',
    '  fragColor=vec4(clamp(col,0.0,1.0),1.0);',
    '}'
  ].join('\n');

  var UNIFORMS = ['u_theme', 'u_knobX', 'u_press', 'u_squashW',
    'u_squashH', 'u_phase', 'u_grain'];

  function createGL(canvas) {
    var gl = null;
    try {
      gl = canvas.getContext('webgl2', {
        alpha: false, antialias: false, depth: false, stencil: false,
        preserveDrawingBuffer: false
      });
    } catch (e) { gl = null; }
    if (!gl) return null;

    function compile(type, source) {
      var sh = gl.createShader(type);
      gl.shaderSource(sh, source);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        console.warn('[pixel-toggle] shader 编译失败，改用 Canvas 2D：' +
          gl.getShaderInfoLog(sh));
        gl.deleteShader(sh);
        return null;
      }
      return sh;
    }
    var vs = compile(gl.VERTEX_SHADER, VERT);
    var fs = compile(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) return null;
    var prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.warn('[pixel-toggle] shader 链接失败，改用 Canvas 2D：' +
        gl.getProgramInfoLog(prog));
      gl.deleteProgram(prog);
      return null;
    }
    var vao = gl.createVertexArray();
    var u = {};
    UNIFORMS.forEach(function (name) {
      u[name] = gl.getUniformLocation(prog, name);
    });
    return {
      gl: gl,
      draw: function (s) {
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.useProgram(prog);
        gl.bindVertexArray(vao);
        gl.uniform1f(u.u_theme, s.theme);
        gl.uniform1f(u.u_knobX, s.knobX);
        gl.uniform1f(u.u_press, s.press);
        gl.uniform1f(u.u_squashW, s.squashW);
        gl.uniform1f(u.u_squashH, s.squashH);
        gl.uniform1f(u.u_phase, s.phase);
        gl.uniform1f(u.u_grain, s.grain);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        gl.bindVertexArray(null);
      },
      destroy: function () {
        try { gl.deleteVertexArray(vao); gl.deleteProgram(prog); } catch (e) {}
      }
    };
  }

  function roundRectPath(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.arcTo(x + w, y, x + w, y + r, r);
    ctx.lineTo(x + w, y + h - r);
    ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h);
    ctx.arcTo(x, y + h, x, y + h - r, r);
    ctx.lineTo(x, y + r);
    ctx.arcTo(x, y, x + r, y, r);
    ctx.closePath();
  }

  // ---------------------------------------------------------------- 引擎
  function PixelToggle(button, options) {
    if (!(this instanceof PixelToggle)) return new PixelToggle(button, options);
    var doc = button.ownerDocument;
    ensureStyle(doc);

    this.options = Object.assign({
      checked: false, sound: true, reducedMotion: null,
      onCheckedChange: null, height: 0
    }, options || {});
    this.button = button;
    this.doc = doc;
    this.win = doc.defaultView || global;
    this.ownedButton = false;
    this.checked = !!this.options.checked;
    this.destroyed = false;
    this.raf = 0;
    this.lastTime = 0;
    this.idleFrames = 0;
    this.suppressClick = false;

    var media = this.win.matchMedia ?
      this.win.matchMedia('(prefers-reduced-motion: reduce)') : null;
    this.media = media;
    this.reduced = this.options.reducedMotion === null ?
      !!(media && media.matches) : !!this.options.reducedMotion;

    // 连续量：theme 0=夜 1=日，pos 0=左 1=右，phase 可带整数圈
    this.theme = this.checked ? 1 : 0;
    this.themeFrom = this.theme;
    this.themeTo = this.theme;
    this.themeAnim = null;
    this.pos = this.checked ? 1 : 0;
    this.posTarget = this.pos;
    this.posVel = 0;
    this.press = 0;
    this.pressTarget = 0;
    this.squashW = 0; this.squashH = 0;
    this.sqWv = 0; this.sqHv = 0;
    this.phase = this.checked ? 0.5 : 0;
    this.phaseTo = this.phase;
    this.phaseAnim = null;
    this.excite = 0;
    this.grain = Math.random() * 100;
    this.pointer = null;
    this.drag = null;
    this.dragging = false;
    this.sx = 1; this.sy = 1;

    this.particles = [];
    for (var i = 0; i < CELLS.count; i++) {
      var angle = (i / CELLS.count) * TAU;
      var radius = Math.sqrt((i * 0.7548776662) % 1);
      var ax = Math.cos(angle) * 74 * radius;
      var ay = Math.sin(angle) * 63 * radius;
      this.particles.push({
        x: ax, y: ay, vx: 0, vy: 0, a: 0, ax: ax, ay: ay
      });
    }
    this.snapParticles();

    // 两层画布：WebGL 在下，2D 叠加在上
    this.glCanvas = doc.createElement('canvas');
    this.overlay = doc.createElement('canvas');
    this.glCanvas.setAttribute('aria-hidden', 'true');
    this.overlay.setAttribute('aria-hidden', 'true');
    button.appendChild(this.glCanvas);
    button.appendChild(this.overlay);
    this.ctx = this.overlay.getContext('2d');

    button.setAttribute('role', 'switch');
    button.setAttribute('aria-checked', String(this.checked));
    if (!button.getAttribute('aria-label')) {
      button.setAttribute('aria-label', '切换日间/夜间主题');
    }
    if (this.options.height) {
      var h = Math.max(48, Number(this.options.height) || 0);
      button.style.height = h + 'px';
      button.style.width = (h * W / H) + 'px';
    }

    audioRefs++;
    this.glgfx = createGL(this.glCanvas);
    button.setAttribute('data-renderer', this.glgfx ? 'webgl2' : 'canvas2d');
    this.bindEvents();
    this.resize();
    this.requestFrame();
  }

  PixelToggle.prototype.knobX = function () {
    return KNOB_X0 + this.pos * TRAVEL;
  };

  PixelToggle.prototype.bindEvents = function () {
    var self = this;
    var b = this.button;
    this.handlers = {};

    function on(name, target, fn, capture) {
      self.handlers[name] = fn;
      target.addEventListener(name, fn, !!capture);
    }
    on('pointerdown', b, function (e) { self.onDown(e); });
    on('pointermove', b, function (e) { self.onMove(e); });
    on('pointerup', b, function (e) { self.onUp(e); });
    on('pointercancel', b, function (e) { self.onCancel(e); });
    on('lostpointercapture', b, function (e) { self.onCancel(e); });
    on('click', b, function (e) {
      if (self.suppressClick) { e.preventDefault(); return; }
      self.toggle(true);
    });
    on('keydown', b, function (e) {
      if (e.key === ' ' || e.key === 'Enter' || e.key === 'Spacebar') {
        e.preventDefault();
        self.toggle(true);
      }
    });
    on('webglcontextlost', this.glCanvas, function (e) {
      e.preventDefault();
      if (self.glgfx) { self.glgfx.destroy(); self.glgfx = null; }
      b.setAttribute('data-renderer', 'canvas2d');
      self.requestFrame();
    });
    on('webglcontextrestored', this.glCanvas, function () {
      if (!self.destroyed) {
        self.glgfx = createGL(self.glCanvas);
        b.setAttribute('data-renderer', self.glgfx ? 'webgl2' : 'canvas2d');
        self.resize();
        self.requestFrame();
      }
    });
    on('visibilitychange', this.doc, function () {
      if (self.doc.hidden) self.settleNow();
      else self.requestFrame();
    });
    if (this.media && this.media.addEventListener) {
      on('change', this.media, function (e) {
        if (self.options.reducedMotion === null) {
          self.reduced = !!e.matches;
          self.requestFrame();
        }
      });
    }
    if (this.win.ResizeObserver) {
      this.ro = new this.win.ResizeObserver(function () {
        self.resize(); self.requestFrame();
      });
      this.ro.observe(b);
    } else {
      on('resize', this.win, function () {
        self.resize(); self.requestFrame();
      });
    }
  };

  PixelToggle.prototype.removeEvents = function () {
    var b = this.button, h = this.handlers || {};
    var self = this;
    Object.keys(h).forEach(function (name) {
      var targets = [b, self.glCanvas, self.doc, self.win, self.media];
      targets.forEach(function (t) {
        if (!t || !t.removeEventListener) return;
        // 同一个处理器只挂在一个目标上，重复 remove 是安全的
        try { t.removeEventListener(name, h[name]); } catch (e) {}
      });
    });
    if (this.ro) { this.ro.disconnect(); this.ro = null; }
  };

  PixelToggle.prototype.resize = function () {
    var rect = this.button.getBoundingClientRect();
    var cssW = rect.width || 432, cssH = rect.height || cssW * H / W;
    var dpr = Math.min(this.win.devicePixelRatio || 1, 2);
    var pw = Math.max(1, Math.round(cssW * dpr));
    var ph = Math.max(1, Math.round(cssH * dpr));
    [this.glCanvas, this.overlay].forEach(function (c) {
      if (c.width !== pw) c.width = pw;
      if (c.height !== ph) c.height = ph;
    });
    this.sx = pw / W;
    this.sy = ph / H;
  };

  PixelToggle.prototype.requestFrame = function () {
    if (this.destroyed || this.raf || this.doc.hidden) return;
    this.lastTime = 0;
    var self = this;
    this.raf = this.win.requestAnimationFrame(function (t) { self.tick(t); });
  };

  PixelToggle.prototype.tick = function (now) {
    if (this.destroyed) return;
    var dt = this.lastTime ? Math.min(0.033, (now - this.lastTime) / 1000) : 0.016;
    this.lastTime = now;
    this.step(dt, now);
    this.drawFrame(now);
    this.raf = 0;
    if (!this.settled()) this.requestFrame();
  };

  // 物理推进（对外公开的 update(options) 是另一个方法，别同名覆盖）
  PixelToggle.prototype.step = function (dt, now) {
    if (this.reduced) {
      this.themeAnim = null;
      this.phaseAnim = null;
      this.theme = this.dragging ? logistic((this.pos - 0.5) / 0.07) : this.themeTo;
      if (!this.dragging) {
        this.pos = this.posTarget; this.posVel = 0;
      }
      this.press = this.pressTarget;
      this.squashW = 0; this.squashH = 0;
      this.sqWv = 0; this.sqHv = 0;
      this.phase = this.phaseTo;
      this.excite = 0;
      this.snapParticles(now);
      return;
    }

    if (this.themeAnim) {
      var ta = this.themeAnim;
      var tt = (now - ta.start) / ta.dur;
      this.theme = lerp(ta.from, ta.to, smooth(tt));
      if (tt >= 1) { this.theme = ta.to; this.themeAnim = null; }
    } else if (this.dragging) {
      this.theme += (logistic((this.pos - 0.5) / 0.07) - this.theme) *
        (1 - Math.exp(-14 * dt));
    } else {
      this.theme += (this.themeTo - this.theme) * (1 - Math.exp(-14 * dt));
    }

    if (this.dragging) {
      // 拖动时位置直接跟手，弹簧只负责放手后的归位
      this.posVel = 0;
      this.phase += (this.pos * 0.5 - this.phase) * (1 - Math.exp(-18 * dt));
    } else {
      var sub = dt / 4;
      for (var i = 0; i < 4; i++) {
        var acc = (this.posTarget - this.pos) * 224 - this.posVel * 15.5;
        this.posVel += acc * sub;
        this.pos += this.posVel * sub;
      }
    }

    var pressRate = this.pressTarget > this.press ? 30 : 21;
    this.press += (this.pressTarget - this.press) * (1 - Math.exp(-pressRate * dt));

    var over = this.pos < 0 ? -this.pos : (this.pos > 1 ? this.pos - 1 : 0);
    var compression = Math.min(0.16, over * 1.12);
    var stretch = Math.min(0.18, Math.abs(this.posVel) * 0.024);
    var targetW = stretch - compression;
    var targetH = -0.45 * stretch + 0.42 * compression;
    this.sqWv += ((targetW - this.squashW) * 420 - this.sqWv * 24) * dt;
    this.sqHv += ((targetH - this.squashH) * 420 - this.sqHv * 24) * dt;
    this.squashW += this.sqWv * dt;
    this.squashH += this.sqHv * dt;

    if (this.phaseAnim) {
      var pa = this.phaseAnim;
      var pt = (now - pa.start) / pa.dur;
      if (pt < 0) pt = 0;
      var eased = 1 - Math.pow(1 - Math.min(1, pt), 3);
      this.phase = lerp(pa.from, pa.to, eased);
      if (pt >= 1) { this.phase = pa.to; this.phaseAnim = null; }
    }

    this.excite *= Math.exp(-7 * dt);
    this.updateParticles(dt, now);
  };

  PixelToggle.prototype.updateParticles = function (dt, now) {
    var scatter = Math.max(this.excite,
      clamp(1 - Math.abs(2 * this.theme - 1) * 1.6, 0, 1));
    var cells = this.theme < 0.5 ? CELLS.moon : CELLS.sun;
    var pointer = this.pointer;
    var recent = pointer && (now - pointer.t) < 160;
    for (var i = 0; i < this.particles.length; i++) {
      var p = this.particles[i];
      var cell = i < cells.length ? cells[i] : null;
      var gx = cell ? cell.x * GRID : p.ax;
      var gy = cell ? cell.y * GRID : p.ay;
      var tx = lerp(gx, p.ax, scatter) + 6 * scatter * Math.sin(now * 0.0021 + i * 0.7);
      var ty = lerp(gy, p.ay, scatter) + 6 * scatter * Math.cos(now * 0.0017 + i * 1.3);
      var ax = (tx - p.x) * 340 - p.vx * 27;
      var ay = (ty - p.y) * 340 - p.vy * 27;
      if (recent) {
        var dx = p.x - pointer.x, dy = p.y - pointer.y;
        var dist = Math.hypot(dx, dy);
        if (dist < 49 && dist > 0.001) {
          var force = 2800 * (1 - Math.pow(dist / 49, 2)) / dist;
          ax += dx * force;
          ay += dy * force;
        }
      }
      p.vx += ax * dt;
      p.vy += ay * dt;
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      var targetA = cell ? cell.a : 0;
      var rate = targetA > p.a ? 9 : 12;
      p.a += (targetA - p.a) * (1 - Math.exp(-rate * dt));
    }
  };

  PixelToggle.prototype.snapParticles = function (now) {
    var t = now || 0;
    var cells = this.theme < 0.5 ? CELLS.moon : CELLS.sun;
    for (var i = 0; i < this.particles.length; i++) {
      var p = this.particles[i];
      var cell = i < cells.length ? cells[i] : null;
      p.x = cell ? cell.x * GRID : p.ax;
      p.y = cell ? cell.y * GRID : p.ay;
      p.vx = 0; p.vy = 0;
      p.a = cell ? cell.a : 0;
      void t;
    }
  };

  PixelToggle.prototype.settled = function () {
    if (this.dragging || this.themeAnim || this.phaseAnim) return false;
    var quiet = Math.abs(this.theme - this.themeTo) < 0.002 &&
      Math.abs(this.pos - this.posTarget) < 0.0008 &&
      Math.abs(this.posVel) < 0.01 &&
      Math.abs(this.press - this.pressTarget) < 0.005 &&
      Math.abs(this.squashW) < 0.003 && Math.abs(this.squashH) < 0.003 &&
      Math.abs(this.excite) < 0.01 &&
      Math.abs(this.phase - this.phaseTo) < 0.002;
    if (quiet && !this.reduced) {
      var moving = false;
      for (var i = 0; i < this.particles.length; i++) {
        var p = this.particles[i];
        if (Math.abs(p.vx) > 1 || Math.abs(p.vy) > 1) { moving = true; break; }
      }
      quiet = !moving;
    }
    if (!quiet) { this.idleFrames = 0; return false; }
    this.idleFrames++;
    return this.idleFrames > 8;
  };

  PixelToggle.prototype.settleNow = function () {
    if (this.raf) { this.win.cancelAnimationFrame(this.raf); this.raf = 0; }
    this.themeAnim = null;
    this.phaseAnim = null;
    this.theme = this.themeTo;
    this.pos = this.posTarget;
    this.posVel = 0;
    this.press = this.pressTarget;
    this.squashW = 0; this.squashH = 0;
    this.sqWv = 0; this.sqHv = 0;
    this.phase = this.phaseTo;
    this.excite = 0;
    this.dragging = false;
    this.button.removeAttribute('data-dragging');
    this.snapParticles();
    this.drawFrame(0);
  };

  // ---------------------------------------------------------------- 绘制
  PixelToggle.prototype.drawFrame = function (now) {
    var s = {
      theme: clamp(this.theme, 0, 1),
      knobX: this.knobX(),
      press: clamp(this.press, 0, 1),
      squashW: this.squashW,
      squashH: this.squashH,
      phase: this.phase % 1,
      grain: this.grain
    };
    if (this.glgfx) {
      try { this.glgfx.draw(s); }
      catch (e) {
        this.glgfx = null;
        this.button.setAttribute('data-renderer', 'canvas2d');
        this.drawFallback(s);
      }
    } else {
      this.drawFallback(s);
    }
    this.drawOverlay(s, now || 0);
  };

  PixelToggle.prototype.drawFallback = function (s) {
    var ctx = this.ctx;
    ctx.setTransform(this.sx, 0, 0, this.sy, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // 椭圆渐变背景
    ctx.save();
    ctx.translate(W * 0.92, -H * 0.08);
    ctx.scale(W * 1.4, H * 1.55);
    var g = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
    g.addColorStop(0, rgbMix([33, 33, 35], [236, 236, 234], s.theme));
    g.addColorStop(1, rgbMix([13, 13, 14], [205, 205, 204], s.theme));
    ctx.fillStyle = g;
    ctx.fillRect(-1, -1, 2, 2);
    ctx.restore();

    // 轨道 + 向下 17px 的柔投影
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,' + (0.32 - 0.14 * s.theme) + ')';
    ctx.shadowBlur = 34;
    ctx.shadowOffsetY = 17;
    ctx.fillStyle = rgbMix([18, 19, 21], [230, 230, 230], s.theme);
    roundRectPath(ctx, 0, 0, W, H, TRACK_R);
    ctx.fill();
    ctx.restore();

    // 玻璃滑块：半透明渐变 + 左上高光
    var kw = KNOB * (1 - 0.055 * s.press + s.squashW);
    var kh = KNOB * (1 - 0.055 * s.press + s.squashH);
    var kx = s.knobX + KNOB / 2 - kw / 2;
    var ky = KNOB_CY - kh / 2;
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,.25)';
    ctx.shadowBlur = 22;
    ctx.shadowOffsetY = 8;
    var glass = ctx.createLinearGradient(kx, ky, kx, ky + kh);
    glass.addColorStop(0, rgbMix([107, 110, 120], [250, 250, 249], s.theme));
    glass.addColorStop(1, rgbMix([38, 40, 48], [219, 219, 217], s.theme));
    ctx.fillStyle = glass;
    roundRectPath(ctx, kx, ky, kw, kh, KNOB_R);
    ctx.fill();
    ctx.restore();

    ctx.save();
    roundRectPath(ctx, kx, ky, kw, kh, KNOB_R);
    ctx.clip();
    var hi = ctx.createLinearGradient(kx, ky, kx + kw, ky + kh);
    hi.addColorStop(0, 'rgba(255,255,255,.16)');
    hi.addColorStop(0.5, 'rgba(255,255,255,0)');
    ctx.fillStyle = hi;
    ctx.fillRect(kx, ky, kw, kh);
    ctx.restore();
  };

  PixelToggle.prototype.rayToEdge = function (dx, dy, target, kw, kh) {
    var lo = 0, hi = 420;
    for (var i = 0; i < 24; i++) {
      var mid = (lo + hi) / 2;
      if (sdRoundBox(dx * mid, dy * mid, kw, kh, KNOB_R) < target) lo = mid;
      else hi = mid;
    }
    return (lo + hi) / 2;
  };

  PixelToggle.prototype.drawOverlay = function (s, now) {
    var ctx = this.ctx;
    ctx.setTransform(this.sx, 0, 0, this.sy, 0, 0);
    if (this.glgfx) ctx.clearRect(0, 0, W, H);

    var cx = s.knobX + KNOB / 2;
    var cy = KNOB_CY;
    var kw = KNOB / 2 * (1 - 0.055 * s.press + s.squashW);
    var kh = KNOB / 2 * (1 - 0.055 * s.press + s.squashH);

    // 光带：沿圆角玻璃外沿 8px 采样，多层描边模拟外晕与亮核心
    var centerA = Math.PI / 2 - TAU * s.phase;
    var points = [];
    for (var i = -32; i <= 32; i++) {
      var a = centerA + (i / 32) * 0.92;
      var dx = Math.cos(a), dy = Math.sin(a);
      var t = this.rayToEdge(dx, dy, 8, kw, kh);
      points.push([cx + dx * t, cy + dy * t]);
    }
    var bandColor = rgbMix([111, 134, 255], [255, 168, 46], s.theme);
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    [[64, 0.045], [18, 0.09], [9, 0.20], [3.5, 0.78]].forEach(function (layer) {
      ctx.beginPath();
      for (var j = 0; j < points.length; j++) {
        if (j === 0) ctx.moveTo(points[j][0], points[j][1]);
        else ctx.lineTo(points[j][0], points[j][1]);
      }
      ctx.lineWidth = layer[0];
      ctx.globalAlpha = layer[1];
      ctx.strokeStyle = bandColor;
      ctx.stroke();
    });
    ctx.restore();

    // 像素月亮 / 太阳：粒子位置即格点，切换中散开重组
    var ink = rgbMix([246, 240, 224], [55, 56, 62], s.theme);
    var cells = s.theme < 0.5 ? CELLS.moon : CELLS.sun;
    var size = 0.82 * GRID;
    ctx.save();
    ctx.fillStyle = ink;
    if (s.theme < 0.5) {
      ctx.shadowColor = 'rgba(255,244,214,.55)';
      ctx.shadowBlur = 7;
    }
    for (var k = 0; k < this.particles.length; k++) {
      var p = this.particles[k];
      var cell = k < cells.length ? cells[k] : null;
      var alpha = p.a * (cell ? 0.55 + 0.45 * cell.a : 1);
      if (alpha < 0.012) continue;
      ctx.globalAlpha = clamp(alpha, 0, 1);
      ctx.fillRect(cx + p.x - size / 2, cy + p.y - size / 2, size, size);
    }
    ctx.restore();
    void now;
  };

  // ---------------------------------------------------------------- 交互
  PixelToggle.prototype.logicalPoint = function (e) {
    var rect = this.button.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) * W / (rect.width || W),
      y: (e.clientY - rect.top) * H / (rect.height || H)
    };
  };

  PixelToggle.prototype.onDown = function (e) {
    if (this.destroyed || e.button > 0) return;
    var p = this.logicalPoint(e);
    this.drag = {
      id: e.pointerId, startX: p.x, startPos: this.pos,
      samples: [{ x: p.x, t: performance.now() }], moved: false
    };
    this.pressTarget = 1;
    this.pointer = { x: p.x - (this.knobX() + 120), y: p.y - KNOB_CY,
                     t: performance.now() };
    this.excite = Math.max(this.excite, 0.35);
    try { this.button.setPointerCapture(e.pointerId); } catch (err) {}
    this.requestFrame();
  };

  PixelToggle.prototype.onMove = function (e) {
    if (this.destroyed) return;
    var p = this.logicalPoint(e);
    this.pointer = { x: p.x - (this.knobX() + 120), y: p.y - KNOB_CY,
                     t: performance.now() };
    var d = this.drag;
    if (!d || d.id !== e.pointerId) { this.requestFrame(); return; }
    var dx = p.x - d.startX;
    if (!d.moved && Math.abs(dx) > 3) {
      d.moved = true;
      this.dragging = true;
      this.button.setAttribute('data-dragging', '1');
    }
    if (d.moved) {
      var raw = d.startPos + dx / TRAVEL;
      // 越界橡皮筋：0.05 * |越界|^0.6
      var over = raw < 0 ? -raw : (raw > 1 ? raw - 1 : 0);
      this.pos = raw < 0 ? -0.05 * Math.pow(over, 0.6)
        : (raw > 1 ? 1 + 0.05 * Math.pow(over, 0.6) : raw);
      this.excite = Math.max(this.excite, 0.55);
      d.samples.push({ x: p.x, t: performance.now() });
      if (d.samples.length > 12) d.samples.shift();
      this.requestFrame();
    }
  };

  PixelToggle.prototype.onUp = function (e) {
    if (this.destroyed || !this.drag || this.drag.id !== e.pointerId) return;
    var d = this.drag;
    this.drag = null;
    this.pressTarget = 0;
    this.dragging = false;
    this.button.removeAttribute('data-dragging');
    try { this.button.releasePointerCapture(e.pointerId); } catch (err) {}

    if (!d.moved) {
      // 短点击交给随后的原生 click 统一切换，不能在这里抑制掉
      this.requestFrame();
      return;
    }
    // 拖动结束时浏览器仍会派发一次 click，必须吞掉，否则会再翻转一次
    this.suppressClick = true;
    var self = this;
    setTimeout(function () { self.suppressClick = false; }, 0);

    var samples = d.samples;
    var first = samples[0], last = samples[samples.length - 1];
    var dt = Math.max(1, last.t - first.t);
    var velocity = (last.x - first.x) / dt;      // 逻辑像素/毫秒
    var value;
    if (Math.abs(velocity) > 0.6) value = velocity > 0;
    else value = this.pos >= 0.5;
    this.setChecked(value, true);
  };

  PixelToggle.prototype.onCancel = function () {
    // releasePointerCapture 会派发 lostpointercapture；此时 onUp 已经收尾，
    // 不能再回滚状态，否则会把刚落位的选择又弹回去。
    if (this.destroyed || (!this.drag && !this.dragging)) return;
    this.drag = null;
    this.dragging = false;
    this.pressTarget = 0;
    this.button.removeAttribute('data-dragging');
    this.themeTo = this.checked ? 1 : 0;
    this.posTarget = this.checked ? 1 : 0;
    this.phaseAnim = null;
    var base = this.checked ? 0.5 : 0;
    this.phaseTo = base + Math.round(this.phase - base);
    this.phaseAnim = { from: this.phase, to: this.phaseTo,
                       start: performance.now(), dur: 700 };
    this.requestFrame();
  };

  PixelToggle.prototype.toggle = function (user) {
    this.setChecked(!this.checked, user !== false);
  };

  PixelToggle.prototype.setChecked = function (value, user) {
    if (this.destroyed) return;
    value = !!value;
    var changed = value !== this.checked;
    this.checked = value;
    this.button.setAttribute('aria-checked', String(value));

    var now = performance.now();
    this.themeFrom = this.theme;
    this.themeTo = value ? 1 : 0;
    this.themeAnim = { from: this.theme, to: this.themeTo,
                       start: now, dur: value ? 100 : 70 };
    this.posTarget = value ? 1 : 0;

    // 光带逆时针找到至少领先 0.1 圈的下一目标，夜停 0、日停 .5
    var base = value ? 0.5 : 0;
    var target = base;
    while (target < this.phase + 0.1) target += 1;
    this.phaseTo = target;
    this.phaseAnim = { from: this.phase, to: target,
                       start: now + 10, dur: 900 };

    if (changed || user) {
      this.excite = Math.max(this.excite, 1);
      this.particles.forEach(function (p) {
        p.vx += (Math.random() - 0.5) * 260;
        p.vy += (Math.random() - 0.5) * 260;
      });
    }
    if (user) this.press = Math.max(this.press, 0.65);  // 短点击也要有按压反馈

    if (user && typeof this.options.onCheckedChange === 'function') {
      this.options.onCheckedChange(value);
    }
    if (user && this.options.sound) playClick(value);
    this.requestFrame();
  };

  PixelToggle.prototype.update = function (options) {
    if (this.destroyed || !options) return;
    if (typeof options.checked === 'boolean') {
      this.options.checked = options.checked;
      if (options.checked !== this.checked) this.setChecked(options.checked, false);
    }
    if (typeof options.sound === 'boolean') this.options.sound = options.sound;
    if (typeof options.reducedMotion === 'boolean') {
      this.reduced = options.reducedMotion;
    }
    if (typeof options.onCheckedChange === 'function') {
      this.options.onCheckedChange = options.onCheckedChange;
    }
    if (options.label) this.button.setAttribute('aria-label', options.label);
    if (options.height) {
      var h = Math.max(48, Number(options.height) || 0);
      this.button.style.height = h + 'px';
      this.button.style.width = (h * W / H) + 'px';
    }
    this.resize();
    this.requestFrame();
  };

  PixelToggle.prototype.destroy = function () {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.raf) { this.win.cancelAnimationFrame(this.raf); this.raf = 0; }
    this.removeEvents();
    if (this.glgfx) { this.glgfx.destroy(); this.glgfx = null; }
    [this.glCanvas, this.overlay].forEach(function (c) {
      if (c && c.parentNode) c.parentNode.removeChild(c);
    });
    this.ctx = null;
    this.particles.length = 0;
    audioRefs = Math.max(0, audioRefs - 1);
    if (audioRefs === 0 && audioCtx) {
      try { audioCtx.close(); } catch (e) {}
      audioCtx = null;
    }
  };

  // ---------------------------------------------------------------- 声音
  var audioCtx = null, audioRefs = 0;
  function playClick(isDay) {
    var AC = global.AudioContext || global.webkitAudioContext;
    if (!AC) return;
    try {
      if (!audioCtx) audioCtx = new AC();
      if (audioCtx.state === 'suspended' && audioCtx.resume) audioCtx.resume();
      var t = audioCtx.currentTime;
      var osc = audioCtx.createOscillator();
      var gain = audioCtx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(isDay ? 1500 : 900, t);
      osc.frequency.exponentialRampToValueAtTime(isDay ? 2300 : 480, t + 0.1);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.06, t + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.1);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start(t);
      osc.stop(t + 0.11);
    } catch (e) { /* 音频不可用时静默 */ }
  }

  // ---------------------------------------------------------------- 挂载
  function mountPixelToggle(host, options) {
    if (!host) throw new Error('mountPixelToggle: 需要挂载节点');
    var doc = host.ownerDocument;
    var btn = doc.createElement('button');
    btn.type = 'button';
    btn.className = 'pixel-toggle' +
      (options && options.className ? ' ' + options.className : '');
    if (options && options.label) btn.setAttribute('aria-label', options.label);
    host.appendChild(btn);
    var instance = new PixelToggle(btn, options);
    instance.ownedButton = true;
    instance.destroy = (function (original) {
      return function () {
        original.call(this);
        if (btn.parentNode) btn.parentNode.removeChild(btn);
      };
    })(instance.destroy);
    return instance;
  }

  global.PixelToggle = PixelToggle;
  global.mountPixelToggle = mountPixelToggle;
})(window);
