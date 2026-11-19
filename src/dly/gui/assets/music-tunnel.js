/* 歌单三维封面隧道：穿行、悬停吸起、点击飞入中央并开始播放。
 *
 * 二期的核心场景，用 three.js 实现；网格视图保留，随时可以切回去。
 *
 * 这一版先把「空间 + 交互」做实：真实纵深、有厚度的封面、雾、背景光晕、粒子、
 * 悬停抬起与点击飞入。覆膜扫光、纸张颗粒、体积光和 Bloom 属于质感层，下一轮补。
 *
 * 坐标约定：封面边长 S、格距 G、层距 D，第 k 层在 z = -k*D。
 * 相机沿 z 穿行，一层的 z 超过相机就搬到 LAYERS*D 之外，形成无限隧道。
 */

import * as THREE from './three/three.module.min.js';

const S = 2.9;              // 封面边长
const G = 4.4;              // 格间距
const D = 5.5;              // 层间距
const TH = 0.12 * S;        // 盒子厚度
const LAYERS = 14;          // 同屏层数
const CAM_Z0 = 14;          // 相机初始 z：离最近一层 14 个单位，正好看见整条隧道
const COVER_LIMIT = 32;     // 同时上 GPU 的封面数上限
const FLY_MS = 1150;        // 飞入动画时长

const damp = (a, b, k, dt) => a + (b - a) * (1 - Math.exp(-k * dt));
const easeInOut = t => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

function hash2(i, j) {
  const v = Math.abs(Math.sin(i * 12.9898 + j * 78.233) * 43758.5453);
  return v - Math.floor(v);
}

/* 棋盘格 + 中央留白 + 随机稀疏，避免糊成一面墙。
   中央格（0,0）永远空着：相机贴着层飞过，正中要是站着一张封面就直接糊住镜头了。
   点某张封面时再把它飞到中央当主封面。 */
function slotVisible(i, j, k, h) {
  if ((i + j + k) % 2 !== 0) return false;
  if (Math.max(Math.abs(i), Math.abs(j)) > 1 && h < 0.5) return false;
  return true;
}

function radialTexture() {
  const size = 32;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d');
  const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, 'rgba(255,252,255,1)');
  grad.addColorStop(0.45, 'rgba(206,196,255,.55)');
  grad.addColorStop(1, 'rgba(160,150,255,0)');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

export function mountMusicTunnel(host, options = {}) {
  const state = {
    tracks: (options.tracks || []).slice(0, COVER_LIMIT),
    onSelect: options.onSelect || (() => {}),
    reduced: !!options.reducedMotion,
    travel: 0,
    vel: 0,
    lastInput: performance.now(),
    hovered: null,
    autoUsed: false,
    paused: false,
    destroyed: false,
    raf: 0,
    flyers: [],
  };

  const tracks = state.tracks;
  const count = Math.max(1, tracks.length);
  const bgColor = new THREE.Color('#05060f');

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false,
                                             powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.18;
  renderer.domElement.style.display = 'block';
  host.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = bgColor;
  scene.fog = new THREE.Fog(bgColor, D * 2.2, D * 10.5);

  const camera = new THREE.PerspectiveCamera(32, 16 / 9, 0.5, 200);
  camera.position.set(0, 0, CAM_Z0);

  // ---- 背景板：隧道尽头的一团光，跟着相机走 ----
  const bgMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(160, 90),
    new THREE.ShaderMaterial({
      depthWrite: false,
      vertexShader: `varying vec2 vUv;
        void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`,
      fragmentShader: `varying vec2 vUv;
        void main(){
          vec3 top = vec3(0.008,0.010,0.030);
          vec3 bottom = vec3(0.020,0.020,0.060);
          vec3 col = mix(bottom, top, pow(vUv.y, 0.85));
          vec2 c = vec2(0.56,0.62);
          float d = length((vUv - c) * vec2(1.0,1.35));
          col += vec3(0.22,0.19,0.48) * smoothstep(0.40,0.0,d) * 0.42;
          col += vec3(0.80,0.76,1.00) * smoothstep(0.12,0.0,d) * 0.34;
          gl_FragColor = vec4(col,1.0);
        }`,
    }),
  );
  bgMesh.position.set(0, 0, CAM_Z0 - 60);
  scene.add(bgMesh);

  // ---- 灯光只影响盒子侧面 ----
  scene.add(new THREE.AmbientLight(0x5a5680, 1.1));
  const key = new THREE.DirectionalLight(0xffffff, 1.4);
  key.position.set(-4, 8, 12);
  scene.add(key);

  // ---- 粒子 ----
  const particleCount = state.reduced ? 260 : 900;
  const pGeo = new THREE.BufferGeometry();
  const pPos = new Float32Array(particleCount * 3);
  for (let i = 0; i < particleCount; i++) {
    pPos[i * 3] = (Math.random() - 0.5) * 60;
    pPos[i * 3 + 1] = (Math.random() - 0.5) * 36;
    pPos[i * 3 + 2] = 14 - Math.random() * 80;
  }
  pGeo.setAttribute('position', new THREE.BufferAttribute(pPos, 3));
  const points = new THREE.Points(pGeo, new THREE.PointsMaterial({
    size: 0.09, map: radialTexture(), transparent: true, opacity: 0.75,
    depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true,
  }));
  scene.add(points);

  // ---- 封面素材 ----
  const loader = new THREE.TextureLoader();
  // 封面是异步解码的：没加载完时贴图是黑的，一路挂上去会看到一批封面「啪啪」刷出来。
  // 全部就绪后再统一显示，只闪一下（通常几十毫秒）。
  let pendingCovers = tracks.length;
  let coversReady = tracks.length === 0;
  const textures = tracks.map(track => {
    const tex = loader.load(track.coverUrl, () => {
      pendingCovers -= 1;
      if (pendingCovers <= 0) coversReady = true;
    }, undefined, () => {
      pendingCovers -= 1;
      if (pendingCovers <= 0) coversReady = true;
    });
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 4;
    tex.minFilter = THREE.LinearMipmapLinearFilter;
    tex.magFilter = THREE.LinearFilter;
    return tex;
  });

  // ---- 槽位：每层一批格子 ----
  const slots = [];
  for (let layer = 0; layer < LAYERS; layer++) {
    const k = layer;                         // 0 … 13，第 0 层在 z=0，相机在 z=14
    for (let j = -5; j <= 5; j++) {
      for (let i = -7; i <= 7; i++) {
        const h = hash2(i + k * 3, j);
        if (!slotVisible(i, j, k, h)) continue;
        const isCenter = i === 0 && j === 0 && k === 0;
        slots.push({
          i, j, k, layer,
          z: -k * D,
          baseX: i * G,
          baseY: j * G,
          fromX: 0, fromY: 0, toX: i * G, toY: j * G,
          lift: 0, hero: 0, flying: 0,
          track: isCenter ? 0
            : 1 + (Math.abs(3 * i + 5 * j + 2 * k) % Math.max(1, count - 1)),
        });
      }
    }
  }
  slots.forEach((slot, index) => { slot.index = index; });

  const frontGeo = new THREE.PlaneGeometry(S, S);
  const bodyGeo = new THREE.BoxGeometry(S, S, TH);

  const bodyMesh = new THREE.InstancedMesh(
    bodyGeo,
    // 盒子的正面和封面几乎贴在一起，靠多边形偏移把盒子往后推，
    // 否则在近距离会逐像素闪烁（z-fighting）
    new THREE.MeshLambertMaterial({
      color: 0x1a1726, polygonOffset: true,
      polygonOffsetFactor: 1, polygonOffsetUnits: 1,
    }),
    slots.length);
  bodyMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  bodyMesh.frustumCulled = false;
  scene.add(bodyMesh);

  // 每张封面一个 InstancedMesh：素材通常就几十张，比纹理数组省内存也好维护
  const byTrack = new Map();
  slots.forEach(slot => {
    if (!byTrack.has(slot.track)) byTrack.set(slot.track, []);
    byTrack.get(slot.track).push(slot);
  });

  const frontMeshes = [];
  byTrack.forEach((list, trackIndex) => {
    const tex = textures[trackIndex % Math.max(1, textures.length)] || null;
    const mat = new THREE.MeshBasicMaterial({ map: tex });
    if (!tex) mat.color.set(0x272238);
    const mesh = new THREE.InstancedMesh(frontGeo, mat, list.length);
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.frustumCulled = false;
    list.forEach((slot, index) => { slot.frontIdx = index; });
    mesh.userData.slots = list;
    scene.add(mesh);
    frontMeshes.push(mesh);
  });

  const dummy = new THREE.Object3D();
  const pointer = new THREE.Vector2();
  const pointerTarget = new THREE.Vector2();
  const raycaster = new THREE.Raycaster();
  const clock = new THREE.Clock();

  // ---- 尺寸：16:9 舞台在容器里等比居中 ----
  function layout() {
    const width = host.clientWidth || 640;
    const height = host.clientHeight || 360;
    const targetAspect = 16 / 9;
    let w = width;
    let h = width / targetAspect;
    if (h > height) { h = height; w = height * targetAspect; }
    renderer.setSize(Math.round(w), Math.round(h), true);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  // ---- 飞入：把一格挪到该层中央，同时把原来的中央挤走 ----
  function flyToCenter(slot) {
    const layerCenter = slots.find(s => s.layer === slot.layer
      && Math.abs(s.baseX) < 0.01 && Math.abs(s.baseY) < 0.01);
    const now = performance.now();
    state.flyers.push({
      slot, start: now,
      fromX: slot.baseX, fromY: slot.baseY, toX: 0, toY: 0,
      arc: Math.min(3.2, Math.hypot(slot.baseX, slot.baseY) * 0.32),
      dir: Math.random() < 0.5 ? -1 : 1,
    });
    if (layerCenter && layerCenter !== slot) {
      state.flyers.push({
        slot: layerCenter, start: now,
        fromX: layerCenter.baseX, fromY: layerCenter.baseY,
        toX: slot.baseX, toY: slot.baseY, arc: 1.2,
        dir: -Math.sign(slot.baseX || 1),
      });
    }
    slot.hero = 0;
    return slot.z;
  }

  function stepFlyers(now) {
    if (!state.flyers.length) return;
    state.flyers = state.flyers.filter(fly => {
      const t = Math.min(1, (now - fly.start) / FLY_MS);
      const e = easeInOut(t);
      const arc = Math.sin(Math.PI * e) * fly.arc * fly.dir;
      fly.slot.baseX = fly.fromX + (fly.toX - fly.fromX) * e + arc * 0.25;
      fly.slot.baseY = fly.fromY + (fly.toY - fly.fromY) * e + arc * 0.35;
      fly.slot.flying = 1 - t;
      if (fly.slot === state.hovered) fly.slot.hero = Math.sin(Math.PI * e) * 0.6;
      if (t >= 1) {
        fly.slot.baseX = fly.toX;
        fly.slot.baseY = fly.toY;
        fly.slot.flying = 0;
        return false;
      }
      return true;
    });
  }

  // ---- 每帧摆放实例 ----
  function updateSlots(dt) {
    const camZ = camera.position.z;
    for (const slot of slots) {
      if (slot.z > camZ + 1 && !slot.flying) {
        slot.z -= LAYERS * D;
        // 搬到新的一层时回到自己的格位，否则「中央」会继承上一层的偏移
        slot.baseX = slot.i * G;
        slot.baseY = slot.j * G;
        slot.hero = 0;
        slot.lift = 0;
      }
    }

    for (const slot of slots) {
      const isHovered = slot === state.hovered;
      const want = state.reduced ? (isHovered ? 1 : 0)
        : damp(slot.lift, isHovered ? 1 : 0, 8, dt);
      slot.lift = want;

      const liftZ = 2 * slot.lift;
      const scale = 1 + 0.14 * slot.lift + 0.7 * slot.hero;
      const z = slot.z + liftZ + 1.3 * slot.hero;

      // 悬停的封面朝指针轻轻歪一下，露出侧面厚度
      const tiltX = isHovered && !state.reduced ? pointer.y * 0.35 : 0;
      const tiltY = isHovered && !state.reduced ? -pointer.x * 0.35 : 0;

      dummy.position.set(slot.baseX, slot.baseY, z + TH / 2 + 0.04);
      dummy.rotation.set(tiltX, tiltY, 0);
      dummy.scale.setScalar(scale);
      dummy.updateMatrix();
      // 复用同一块 Matrix4，别每帧 clone（700 个格子 × 60fps 会很可观）
      (slot.matrixFront ||= new THREE.Matrix4()).copy(dummy.matrix);

      dummy.position.set(slot.baseX, slot.baseY, z);
      dummy.rotation.set(0, 0, 0);
      dummy.updateMatrix();
      bodyMesh.setMatrixAt(slot.index, dummy.matrix);
    }
    bodyMesh.instanceMatrix.needsUpdate = true;

    frontMeshes.forEach(mesh => {
      mesh.visible = coversReady;
      mesh.userData.slots.forEach(slot => {
        mesh.setMatrixAt(slot.frontIdx, slot.matrixFront);
      });
      mesh.instanceMatrix.needsUpdate = true;
    });
  }

  let pickTick = 0;
  function pick() {
    if ((pickTick++ % 3) !== 0) return;       // 每三帧拾取一次，够跟手也省 CPU
    if (Math.abs(state.vel) > 0.5) {          // 正在穿行：封面一直从指针下掠过，
      state.hovered = null;                   // 抬起/放下会此起彼伏，看着就是频闪
      return;
    }
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(frontMeshes, false);
    const hit = hits.find(h => h.instanceId !== undefined);
    if (!hit) { state.hovered = null; return; }
    const slot = hit.object.userData.slots[hit.instanceId];
    state.hovered = (slot && slot.z < camera.position.z - 1) ? slot : null;
  }

  // ---- 输入 ----
  function onWheel(event) {
    event.preventDefault();
    state.vel += event.deltaY * 0.004;
    state.lastInput = performance.now();
    state.autoUsed = false;
  }

  let dragging = false, dragStart = 0, dragMoved = 0, lastY = 0;
  function onPointerDown(event) {
    dragging = true;
    dragStart = performance.now();
    dragMoved = 0;
    lastY = event.clientY;
    // 捕获必须设在画布自己身上：设在 host 上会把后续事件从画布里抢走，
    // pointerup 收不到，点击就永远不触发
    renderer.domElement.setPointerCapture?.(event.pointerId);
  }
  function onPointerMove(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointerTarget.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointerTarget.y = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
    pointer.x = pointerTarget.x;
    pointer.y = pointerTarget.y;
    if (dragging) {
      const delta = event.clientY - lastY;
      lastY = event.clientY;
      dragMoved += Math.abs(delta);
      state.vel += delta * 0.02;
      state.lastInput = performance.now();
    }
  }
  function onPointerUp(event) {
    const wasDrag = dragMoved > 6;
    dragging = false;
    renderer.domElement.releasePointerCapture?.(event.pointerId);
    if (wasDrag || performance.now() - dragStart > 700) return;
    const slot = state.hovered;
    if (!slot) return;
    const track = tracks[slot.track % Math.max(1, tracks.length)];
    flyToCenter(slot);
    if (track && state.onSelect) state.onSelect(track.id);
  }
  function onPointerLeave() { state.hovered = null; }

  renderer.domElement.addEventListener('wheel', onWheel, { passive: false });
  renderer.domElement.addEventListener('pointerdown', onPointerDown);
  renderer.domElement.addEventListener('pointermove', onPointerMove);
  renderer.domElement.addEventListener('pointerup', onPointerUp);
  renderer.domElement.addEventListener('pointerleave', onPointerLeave);

  const observer = new ResizeObserver(layout);
  observer.observe(host);
  layout();

  // ---- 主循环 ----
  let last = performance.now();
  function frame(now) {
    if (state.destroyed) return;
    state.raf = requestAnimationFrame(frame);
    if (state.paused) { last = now; return; }

    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const time = clock.getElapsedTime();

    if (!state.reduced) {
      state.vel *= Math.pow(0.06, dt);
      if (Math.abs(state.vel) < 0.001) state.vel = 0;
      state.travel += state.vel * dt * 6;
      // 长时间没操作时自动往前走一小段，避免画面彻底静止
      if (!state.autoUsed && now - state.lastInput > 5000
          && Math.abs(state.vel) < 0.2) {
        state.vel += 5.6;
        state.autoUsed = true;
        state.lastInput = now;
      }
    }
    stepFlyers(now);

    camera.position.z = CAM_Z0 - state.travel;
    if (!state.reduced) {
      camera.position.x = damp(camera.position.x,
        Math.sin(time * 0.2) * 0.35 + pointer.x * 0.9, 2.5, dt);
      camera.position.y = damp(camera.position.y,
        Math.cos(time * 0.17) * 0.25 + pointer.y * 0.55, 2.5, dt);
    }
    camera.lookAt(camera.position.x * 0.4, camera.position.y * 0.4,
                  camera.position.z - 20);

    bgMesh.position.z = camera.position.z - 60;
    points.position.z = camera.position.z;
    if (!state.reduced) points.rotation.z = time * 0.01;

    updateSlots(dt);
    pick();
    renderer.render(scene, camera);
  }
  state.raf = requestAnimationFrame(frame);

  return {
    destroy() {
      state.destroyed = true;
      cancelAnimationFrame(state.raf);
      observer.disconnect();
      const el = renderer.domElement;
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('pointerdown', onPointerDown);
      el.removeEventListener('pointermove', onPointerMove);
      el.removeEventListener('pointerup', onPointerUp);
      el.removeEventListener('pointerleave', onPointerLeave);
      textures.forEach(tex => tex.dispose());
      frontMeshes.forEach(mesh => {
        mesh.material.map?.dispose();
        mesh.material.dispose();
        mesh.geometry.dispose();
      });
      bodyMesh.geometry.dispose();
      bodyMesh.material.dispose();
      pGeo.dispose();
      points.material.map?.dispose();
      points.material.dispose();
      bgMesh.geometry.dispose();
      bgMesh.material.dispose();
      renderer.dispose();
      el.remove();
    },
    setPaused(value) { state.paused = !!value; },
    setTravel(value) { state.travel = value; },
    stats() {
      return {
        slots: slots.length,
        fronts: frontMeshes.length,
        tracks: tracks.length,
        camera: camera.position.toArray().map(v => +v.toFixed(2)),
        bg: bgMesh.position.z,
        firstSlot: slots[0] ? [slots[0].baseX, slots[0].baseY, slots[0].z] : null,
        visible: bodyMesh.count,
        paused: state.paused,
        travel: +state.travel.toFixed(2),
        loadedTextures: textures.filter(t => !!t.image).length,
        ahead: slots.filter(s2 => s2.z < camera.position.z - 2
          && s2.z > camera.position.z - 40).length,
        near: slots.filter(s2 => Math.abs(s2.baseX) < 8 && Math.abs(s2.baseY) < 6
          && s2.z < camera.position.z - 2).length,
        calls: renderer.info.render.calls,
        hovered: !!state.hovered,
        tris: renderer.info.render.triangles,
      };
    },
  };
}

// 页面里是普通脚本（非模块），通过 window 拿到入口
window.MusicTunnel = { mount: mountMusicTunnel };
